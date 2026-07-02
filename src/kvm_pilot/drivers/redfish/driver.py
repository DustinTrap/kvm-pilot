"""
RedfishDriver — a DMTF Redfish (BMC) driver for kvm-pilot.

Speaks the standard Redfish REST API, so one driver covers Dell iDRAC, HPE iLO,
Supermicro, Lenovo XClarity (XCC), and OpenBMC. It is **portable by navigating
hypermedia** — it follows ``@odata.id`` links and reads
``@Redfish.ActionInfo``/``AllowableValues`` rather than hard-coding vendor ids
(Dell ``System.Embedded.1`` vs HPE ``1`` vs OpenBMC ``system``) or version
strings; features are detected by the presence of a property/link in the actual
payload.

Capabilities (a BMC's set is *complementary* to a PiKVM's — strong on structured
state, no pixels): ``SystemInfo``, ``Power``, ``BootProgress``, ``Sensors``,
``Logs``, ``VirtualMedia``. It deliberately does **not** implement ``HID`` /
``Video`` / ``GPIO`` (a BMC has no keyboard/mouse/screenshot/relay), nor — in
this version — ``SerialConsole`` (Redfish exposes SOL as an SSH/IPMI connection
descriptor, not an HTTP byte stream), ``Events`` (push/SSE), or ``Watchdog``
(an IPMI primitive). See ``docs/architecture.md``.

Every state-changing call (power reset, virtual-media insert/eject) routes
through ``SafetyPolicy.guard()``; reads are never gated.

Alpha, mock-tested only — never run against real hardware.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ...errors import CapabilityError, KVMPilotError, TimeoutError
from ...safety import SafetyPolicy
from ...vision.base import (
    PHASE_BIOS_MENU,
    PHASE_BOOTING,
    PHASE_OS_RUNNING,
    PHASE_POST_SCREEN,
    PHASE_POWER_OFF,
    PHASE_UNKNOWN,
)
from ..base import CapabilityMixin, PowerMixin
from .transport import RedfishHTTP

if TYPE_CHECKING:
    from ...config import HostConfig

logger = logging.getLogger("kvm_pilot.redfish")

# The DMTF ResetType enum — used only as a last-resort fallback when a target
# advertises no AllowableValues. We never assume a value is supported; the
# per-method preference lists are intersected with the target's actual set.
_DEFAULT_RESET_TYPES = [
    "On", "ForceOff", "GracefulShutdown", "GracefulRestart", "ForceRestart",
    "Nmi", "ForceOn", "PushPowerButton", "PowerCycle",
]

# Intent -> preferred ResetType order (first one the target advertises wins).
# reset_hard prefers ForceRestart because GracefulRestart is documented to behave
# as a forceful restart on some firmware (Dell iDRAC9 v3.36; HPE iLO5 advisory).
_RESET_PREFERENCES: dict[str, list[str]] = {
    "power_on": ["On", "ForceOn"],
    "power_off": ["GracefulShutdown", "PushPowerButton", "ForceOff"],
    "power_off_hard": ["ForceOff", "PushPowerButton"],
    "reset_hard": ["ForceRestart", "PowerCycle", "GracefulRestart"],
}

# Redfish BootProgressTypes -> the project's phase vocabulary (vision.base).
_BOOT_PROGRESS_MAP: dict[str, str] = {
    "PrimaryProcessorInitializationStarted": PHASE_POST_SCREEN,
    "BusInitializationStarted": PHASE_POST_SCREEN,
    "MemoryInitializationStarted": PHASE_POST_SCREEN,
    "SecondaryProcessorInitializationStarted": PHASE_POST_SCREEN,
    "PCIResourceConfigStarted": PHASE_POST_SCREEN,
    "SystemHardwareInitializationComplete": PHASE_POST_SCREEN,
    "SetupEntered": PHASE_BIOS_MENU,
    "OSBootStarted": PHASE_BOOTING,
    "OSRunning": PHASE_OS_RUNNING,
}

# Preference order when several LogServices exist (lifecycle/IML before raw SEL).
_LOG_PREFERENCE = ("lclog", "lifecycle", "iml", "eventlog", "sel", "log")

_TERMINAL_TASK_STATES = {"Completed", "Killed", "Exception", "Cancelled"}
_FAILED_TASK_STATES = {"Killed", "Exception", "Cancelled"}

# Bound hypermedia pagination so a misbehaving BMC that returns a cyclic/self-
# referential Members@odata.nextLink cannot spin forever.
_MAX_PAGES = 64

# MediaTypes that count as removable (non-CD) media when cdrom=False.
_REMOVABLE_MEDIA = {"USBStick", "Floppy", "RemovableDisk"}

# URL scheme -> Redfish TransferProtocolType, for BMCs that require it explicitly.
_TRANSFER_PROTOCOLS = {
    "http": "HTTP", "https": "HTTPS", "nfs": "NFS",
    "cifs": "CIFS", "smb": "CIFS", "ftp": "FTP", "tftp": "TFTP", "scp": "SCP",
}


def _transfer_protocol(source: str) -> str | None:
    scheme = source.split("://", 1)[0].lower() if "://" in source else ""
    return _TRANSFER_PROTOCOLS.get(scheme)


def _param_missing(exc: KVMPilotError, name: str) -> bool:
    """True if a Redfish error reports ``name`` as a missing action parameter."""
    for info in getattr(exc, "extended_info", []) or []:
        if "ParameterMissing" not in str(info.get("MessageId", "")):
            continue
        if name in (info.get("MessageArgs") or []) or name in str(info.get("Message", "")):
            return True
    return False


# LogEntry.Created at/before this epoch (~1971) means an unset RTC (a fresh or
# clockless BMC); a time-based log seek includes such entries rather than
# dropping them.
_UNSET_RTC_EPOCH = 31_536_000


def _log_entry_epoch(created: object) -> float | None:
    """Parse a Redfish LogEntry.Created (ISO 8601) to a Unix epoch, or None."""
    if not isinstance(created, str) or not created:
        return None
    try:
        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def _log_within_lookback(created: object, cutoff: float | None) -> bool:
    """Whether a log entry falls within a ``seconds`` lookback (cutoff epoch).

    ``cutoff`` is None for seek==0 (include everything). Otherwise include
    entries at/after the cutoff — but a missing/unparseable timestamp or an
    unset-RTC (epoch) stamp is included, since a strict filter would silently
    return nothing on fresh or clockless BMCs, and LogEntry ordering varies by
    vendor (iDRAC newest-first, OpenBMC oldest-first) so index skipping is
    unstable.
    """
    if cutoff is None:
        return True
    ts = _log_entry_epoch(created)
    if ts is None or ts <= _UNSET_RTC_EPOCH:
        return True
    return ts >= cutoff


class RedfishDriver(PowerMixin, CapabilityMixin):
    """A Redfish BMC driver. See module docstring for the capability scope."""

    def __init__(
        self,
        host: str,
        user: str = "root",
        passwd: str = "",
        *,
        port: int = 443,
        scheme: str = "https",
        verify_ssl: bool = False,
        timeout: float = 30.0,
        auth: str = "session",
        # Which ComputerSystem member to drive on multi-node gear (0-based).
        # Programmatic-only for now: from_config / the CLI do not expose it, so a
        # profile always targets member 0. An out-of-range value is a hard error
        # (never a silent fall-back). Chassis/manager are resolved from the chosen
        # system's Links, not this index.
        system_index: int = 0,
        power_wait_timeout: float = 60.0,
        async_timeout: float = 120.0,
        dry_run: bool = False,
        confirm: Callable[[str, str], bool] | None = None,
        max_retries: int = 3,
        ssl_ca_file: str | None = None,
    ):
        self.host = host
        self._http = RedfishHTTP(
            host, user, passwd, port=port, scheme=scheme, verify_ssl=verify_ssl,
            timeout=timeout, auth=auth, max_retries=max_retries, ssl_ca_file=ssl_ca_file,
        )
        self.safety = SafetyPolicy(dry_run=dry_run, confirm=confirm)
        self._system_index = system_index
        self._power_wait_timeout = power_wait_timeout
        self._async_timeout = async_timeout
        # Discovery cache (resolved lazily, memoized). Static topology only;
        # volatile fields (PowerState, BootProgress) are always re-read fresh.
        self._logged_in = False
        self._root_doc: dict | None = None
        self._system_uri: str | None = None
        self._system_doc: dict | None = None
        self._chassis_uri: str | None = None
        self._manager_uri: str | None = None
        self._reset_target: str | None = None
        self._reset_allowable: list[str] | None = None
        # (insert, eject, slot) keyed by cdrom flag — CD vs removable media differ.
        self._vm: dict[bool, tuple[str, str, str]] = {}
        self._log_entries_uri: str | None = None
        # $expand support for collection fan-out (sensors): None=untried,
        # True=works, False=known-unsupported (learned from a 501).
        self._expand_ok: bool | None = None

    @classmethod
    def from_config(
        cls,
        cfg: HostConfig,
        *,
        confirm: Callable[[str, str], bool] | None = None,
        dry_run: bool = False,
        max_retries: int = 3,
    ) -> RedfishDriver:
        """Build a driver from a resolved :class:`~kvm_pilot.config.HostConfig`.

        Mirrors ``PiKVMDriver.from_config`` so the CLI and MCP server construct a
        BMC the same way they build a PiKVM. ``totp_secret`` has no Redfish
        analogue (a BMC authenticates with HTTP Basic or a Redfish session), so it
        is ignored here.
        """
        return cls(
            cfg.host,
            cfg.user,
            cfg.passwd,
            port=cfg.port,
            scheme=cfg.scheme,
            verify_ssl=cfg.verify_ssl,
            timeout=cfg.timeout,
            auth=cfg.redfish_auth,
            dry_run=dry_run,
            confirm=confirm,
            max_retries=max_retries,
            ssl_ca_file=cfg.ssl_ca_file,
        )

    # -- discovery -------------------------------------------------------

    def _ensure_login(self) -> None:
        if not self._logged_in:
            self._http.login()
            self._logged_in = True

    def _root(self) -> dict:
        if self._root_doc is None:
            self._ensure_login()
            self._root_doc = self._http.get_json("/redfish/v1/")
        return self._root_doc

    def _members(self, collection_uri: str) -> list[str]:
        ids: list[str] = []
        uri: str | None = collection_uri
        seen: set[str] = set()
        while uri:
            if uri in seen or len(seen) >= _MAX_PAGES:
                raise KVMPilotError(f"Redfish pagination did not terminate at {collection_uri}")
            seen.add(uri)
            coll = self._http.get_json(uri)
            for m in coll.get("Members", []) or []:
                if isinstance(m, dict) and m.get("@odata.id"):
                    ids.append(m["@odata.id"])
            uri = coll.get("Members@odata.nextLink")
        return ids

    def _pick_member(self, collection_link: dict | None, what: str) -> str:
        uri = (collection_link or {}).get("@odata.id")
        if not uri:
            raise CapabilityError(f"Redfish service exposes no {what} collection")
        members = self._members(uri)
        if not members:
            raise CapabilityError(f"Redfish {what} collection is empty")
        idx = self._system_index
        if not 0 <= idx < len(members):
            # NEVER silently fall back to member 0: on multi-node gear that would
            # target the wrong ComputerSystem with a destructive op.
            raise CapabilityError(
                f"system_index={idx} is out of range for the {what} collection "
                f"({len(members)} member(s)): {members}. Pass a valid system_index."
            )
        if len(members) > 1:
            logger.info("Redfish %s has %d members; using index %d (%s)",
                        what, len(members), idx, members[idx])
        return members[idx]

    def _linked_uri(self, link_name: str, collection_link: dict | None, what: str) -> str:
        """Resolve a chassis/manager URI from the chosen System's ``Links``.

        DSP0268 associates a ComputerSystem with its chassis (``Links.Chassis``)
        and manager (``Links.ManagedBy``). The global Chassis/Managers collection
        ordering has NO defined correspondence to Systems ordering, so indexing it
        (as the old code did) can target a different node than power ops on
        multi-node gear (blades, Supermicro twins). Fall back to the collection
        only when the System advertises no such link.
        """
        linked = (self._system().get("Links") or {}).get(link_name)
        if isinstance(linked, list) and linked and isinstance(linked[0], dict):
            uri = linked[0].get("@odata.id")
            if uri:
                return uri
        return self._pick_member(collection_link, what)

    def _system(self) -> dict:
        if self._system_doc is None:
            root = self._root()
            self._system_uri = self._pick_member(root.get("Systems"), "Systems")
            self._system_doc = self._http.get_json(self._system_uri)
        return self._system_doc

    def _system_fresh(self) -> dict:
        """Re-read the ComputerSystem (for volatile fields: PowerState, BootProgress)."""
        self._system()  # resolve the uri
        assert self._system_uri is not None
        return self._http.get_json(self._system_uri)

    def _chassis(self) -> dict:
        if self._chassis_uri is None:
            self._chassis_uri = self._linked_uri("Chassis", self._root().get("Chassis"), "Chassis")
        return self._http.get_json(self._chassis_uri)

    def _manager_uri_resolved(self) -> str:
        if self._manager_uri is None:
            self._manager_uri = self._linked_uri(
                "ManagedBy", self._root().get("Managers"), "Managers"
            )
        return self._manager_uri

    def _reset_info(self) -> tuple[str, list[str]]:
        if self._reset_target is None:
            actions = self._system().get("Actions", {}) or {}
            reset = actions.get("#ComputerSystem.Reset", {}) or {}
            target = reset.get("target")
            if not target:
                raise CapabilityError("ComputerSystem exposes no Reset action")
            allow = reset.get("ResetType@Redfish.AllowableValues")
            if not allow and reset.get("@Redfish.ActionInfo"):
                try:
                    info = self._http.get_json(reset["@Redfish.ActionInfo"])
                    for p in info.get("Parameters", []) or []:
                        if p.get("Name") == "ResetType":
                            allow = p.get("AllowableValues")
                            break
                except KVMPilotError:
                    allow = None
            self._reset_target = target
            self._reset_allowable = list(allow) if allow else list(_DEFAULT_RESET_TYPES)
        assert self._reset_target is not None and self._reset_allowable is not None
        return self._reset_target, self._reset_allowable

    def _choose_reset_type(self, intent: str, powered_on: bool | None = None) -> str:
        _, allowable = self._reset_info()
        for candidate in _RESET_PREFERENCES[intent]:
            if candidate not in allowable:
                continue
            if candidate == "PushPowerButton" and powered_on is not None:
                # PushPowerButton pulses the power button — a state-dependent
                # toggle (DSP0268). Pick it only when the pulse moves toward the
                # intent's target; otherwise it would invert the intent (e.g.
                # power_off on an already-off host powers it ON). On iDRAC8,
                # whose off set is [ForceOff, PushPowerButton], this correctly
                # falls through to ForceOff when the host is already off.
                if powered_on == (intent == "power_on"):
                    continue
            return candidate
        raise CapabilityError(
            f"No supported ResetType for {intent} on {self.host}; "
            f"target advertises {allowable}"
        )

    def _virtual_media(self, cdrom: bool = True) -> tuple[str, str, str]:
        """Resolve (InsertMedia target, EjectMedia target, slot uri).

        ``cdrom`` selects the media class: a CD/DVD slot (default) or a removable
        (USB/floppy) slot — a cross-driver contract the PiKVM driver and the CLI
        ``--usb`` flag rely on. Slots from BOTH the System and the Manager are
        collected before choosing, since vendors place VirtualMedia under either.
        """
        if cdrom not in self._vm:
            slots: list[dict] = []
            bases = [self._system()]
            try:
                bases.append(self._http.get_json(self._manager_uri_resolved()))
            except KVMPilotError:
                pass
            for base in bases:
                vm_link = (base.get("VirtualMedia") or {}).get("@odata.id")
                if vm_link:
                    slots.extend(self._http.get_json(u) for u in self._members(vm_link))

            def matches(s: dict) -> bool:
                types = set(s.get("MediaTypes") or [])
                return bool(types & ({"CD", "DVD"} if cdrom else _REMOVABLE_MEDIA))

            preferred = [s for s in slots if matches(s)]
            empty = [s for s in preferred if not s.get("Inserted")]
            candidates = empty or preferred or slots
            if not candidates:
                raise CapabilityError(f"No Redfish VirtualMedia slot found on {self.host}")
            chosen = candidates[0]
            actions = chosen.get("Actions", {}) or {}
            insert = (actions.get("#VirtualMedia.InsertMedia") or {}).get("target")
            eject = (actions.get("#VirtualMedia.EjectMedia") or {}).get("target")
            slot = chosen.get("@odata.id", "")
            if not insert or not eject:
                raise CapabilityError(
                    f"Redfish VirtualMedia slot {slot} lacks Insert/Eject actions"
                )
            self._vm[cdrom] = (insert, eject, slot)
        return self._vm[cdrom]

    def _resolve_log_entries(self) -> str:
        if self._log_entries_uri is None:
            services: list[str] = []
            # Follow the LogServices navigation link (hypermedia), not a fabricated
            # path: Dell puts SEL/Lclog under the Manager, HPE puts IML under the
            # System, so scan whichever of the two docs advertises the link.
            docs: list[dict] = [self._system()]
            try:
                docs.append(self._http.get_json(self._manager_uri_resolved()))
            except KVMPilotError:
                pass
            for doc in docs:
                link = (doc.get("LogServices") or {}).get("@odata.id")
                if not link:
                    continue
                try:
                    coll = self._http.get_json(link)
                except KVMPilotError:
                    continue
                services.extend(
                    m["@odata.id"] for m in coll.get("Members", []) or []
                    if isinstance(m, dict) and m.get("@odata.id")
                )
            if not services:
                raise CapabilityError(f"Redfish target {self.host} exposes no LogServices")

            def rank(uri: str) -> int:
                low = uri.lower()
                for i, key in enumerate(_LOG_PREFERENCE):
                    if key in low:
                        return i
                return len(_LOG_PREFERENCE)

            chosen = sorted(services, key=rank)[0]
            entries = (self._http.get_json(chosen).get("Entries") or {}).get("@odata.id")
            if not entries:
                raise CapabilityError(f"Redfish LogService {chosen} exposes no Entries")
            self._log_entries_uri = entries
        return self._log_entries_uri

    # -- async / waiting -------------------------------------------------

    def _handle_async(self, resp) -> None:
        """If an action returned 202 Accepted, poll its Task to a terminal state."""
        if resp.status != 202:
            return
        monitor = resp.header("location")
        if not monitor:
            return
        deadline = time.monotonic() + self._async_timeout
        while time.monotonic() < deadline:
            try:
                r = self._http.request("GET", monitor)
            except KVMPilotError as exc:
                # iDRAC/iLO garbage-collect finished tasks; a 404/410 after a 202
                # means the (accepted) action ran to completion, not that it failed.
                if exc.status_code in (404, 410):
                    return
                raise
            body = r.body or {}
            state = body.get("TaskState")
            if r.status != 202 and state is None:
                return  # task resource gone / completed with no state
            if state in _FAILED_TASK_STATES:
                raise KVMPilotError(f"Redfish task failed: {state}")
            if state == "Completed" or (r.status != 202 and state in _TERMINAL_TASK_STATES):
                # A Completed task can still have failed — TaskStatus=Critical.
                if body.get("TaskStatus") == "Critical":
                    raise KVMPilotError(
                        f"Redfish task completed with Critical status: {body.get('Messages')}"
                    )
                return
            time.sleep(2.0)
        raise TimeoutError(f"Redfish async task did not finish within {self._async_timeout}s")

    def _wait_power(self, target_on: bool) -> None:
        deadline = time.monotonic() + self._power_wait_timeout
        while time.monotonic() < deadline:
            if self.is_powered_on() == target_on:
                return
            time.sleep(2.0)
        raise TimeoutError(
            f"Timed out waiting for power={'on' if target_on else 'off'} on {self.host}"
        )

    def _reset(self, intent: str, op: str, desc: str, wait: bool, target_on: bool | None) -> None:
        # Read PowerState first: skip a redundant reset (which on many BMCs is a
        # no-op toggle that inverts intent, or a 400/409), and so PushPowerButton
        # is only chosen when the pulse moves toward the target.
        powered_on = self.is_powered_on() if target_on is not None else None
        if target_on is not None and powered_on == target_on:
            logger.info("%s already powered %s — no reset issued", self.host,
                        "on" if target_on else "off")
            return
        reset_type = self._choose_reset_type(intent, powered_on)
        # Name the resolved system in the prompt so the operator can see exactly
        # which ComputerSystem member a destructive op targets on multi-node gear.
        target_desc = f"{desc} [{self._system_uri}] via ComputerSystem.Reset({reset_type})"
        if not self.safety.guard(op, target_desc):
            return  # dry-run: gated and skipped
        target, _ = self._reset_info()
        try:
            resp = self._http.request("POST", target, json_body={"ResetType": reset_type})
        except KVMPilotError as exc:
            # Vendor non-idempotence / TOCTOU: some BMCs reject a reset that is
            # already in the requested state (iLO InvalidOperationForSystemState
            # -> 400/409). Re-read; if we reached the target, treat as success.
            if (
                exc.status_code in (400, 409)
                and target_on is not None
                and self.is_powered_on() == target_on
            ):
                logger.info("%s reached power=%s despite %s — treating as success",
                            self.host, "on" if target_on else "off", exc.status_code)
                return
            raise
        self._handle_async(resp)
        if wait and target_on is not None:
            self._wait_power(target_on)

    # -- SystemInfo ------------------------------------------------------

    def get_info(self, fields: list | None = None) -> dict:
        stable = self._system()        # cached identity/topology fields
        volatile = self._system_fresh()  # re-read so power/boot are not stale
        status = volatile.get("Status") or {}
        info: dict[str, Any] = {
            "manufacturer": stable.get("Manufacturer"),
            "model": stable.get("Model"),
            "serial_number": stable.get("SerialNumber"),
            "uuid": stable.get("UUID"),
            "bios_version": stable.get("BiosVersion"),
            "power_state": volatile.get("PowerState"),
            "health": status.get("Health"),
            "state": status.get("State"),
            "boot_progress": (volatile.get("BootProgress") or {}).get("LastState"),
            "redfish_version": self._root().get("RedfishVersion"),
            "odata_type": stable.get("@odata.type"),
        }
        if fields:
            info = {k: v for k, v in info.items() if k in fields}
        return info

    # -- Power -----------------------------------------------------------

    def is_powered_on(self) -> bool:
        return self._system_fresh().get("PowerState") == "On"

    def power_on(self, wait: bool = True) -> None:
        self._reset("power_on", "redfish.power_on", f"Power ON {self.host}", wait, True)

    def power_off(self, wait: bool = True) -> None:
        self._reset("power_off", "redfish.power_off", f"Graceful power OFF {self.host}", wait, False)

    def power_off_hard(self, wait: bool = True) -> None:
        self._reset(
            "power_off_hard", "redfish.power_off_hard",
            f"HARD power off {self.host} (data loss risk)", wait, False,
        )

    def reset_hard(self, wait: bool = True) -> None:
        # A restart ends powered on (and may already read On), so do not poll a
        # power transition — _handle_async covers the action's completion.
        self._reset(
            "reset_hard", "redfish.reset_hard",
            f"HARD reset {self.host} (data loss risk)", wait, None,
        )

    # hard_cycle (PowerMixin): power_off_hard → power_on, both blocking on the
    # real PowerState transition, so the settle delays stay 0.

    # -- BootProgress ----------------------------------------------------

    def get_boot_progress(self) -> str | None:
        sysd = self._system_fresh()
        bp = sysd.get("BootProgress")
        if not isinstance(bp, dict):
            return None  # device does not report BootProgress at all
        last = bp.get("LastState")
        if last in (None, "None"):
            # No boot progress reported: infer only what PowerState actually
            # supports. Transitional states (PoweringOn/PoweringOff/Paused,
            # DSP0268 Resource.PowerState) are NOT power_off — reporting them
            # as off would tell a wait loop the host is down mid-transition.
            return PHASE_POWER_OFF if sysd.get("PowerState") == "Off" else PHASE_UNKNOWN
        return _BOOT_PROGRESS_MAP.get(str(last), PHASE_UNKNOWN)

    # -- Logs ------------------------------------------------------------

    def get_logs(self, seek: int = 0, follow: bool = False) -> str:
        if follow:
            raise CapabilityError("Redfish has no log tail-follow; call get_logs() without follow")
        # seek is SECONDS of lookback — the same contract as the PiKVM driver
        # (kvmd's /api/log?seek=N), NOT an entry index. Filter by LogEntry.Created.
        cutoff = time.time() - seek if seek else None
        lines: list[str] = []
        uri: str | None = self._resolve_log_entries()
        seen: set[str] = set()
        while uri:
            if uri in seen or len(seen) >= _MAX_PAGES:
                break  # cyclic/over-long nextLink: stop rather than spin
            seen.add(uri)
            coll = self._http.get_json(uri)
            for e in coll.get("Members", []) or []:
                if not _log_within_lookback(e.get("Created"), cutoff):
                    continue
                sev = e.get("Severity") or e.get("MessageSeverity") or ""
                lines.append(
                    f"{e.get('Created', '')}\t{sev}\t"
                    f"{e.get('MessageId', '')}\t{e.get('Message', '')}"
                )
            uri = coll.get("Members@odata.nextLink")
        return "\n".join(lines)

    # -- Sensors ---------------------------------------------------------

    def read_sensors(self) -> dict:
        chassis = self._chassis()
        sensors_link = (chassis.get("Sensors") or {}).get("@odata.id")
        if sensors_link:
            return self._read_sensors_unified(sensors_link)
        return self._read_thermal_power(chassis)

    def _read_sensors_unified(self, sensors_link: str) -> dict:
        buckets = {"Temperature": "temperatures", "Rotational": "fans",
                   "Voltage": "voltages", "Power": "power"}
        out: dict[str, list] = {v: [] for v in buckets.values()}
        out["other"] = []
        for s in self._sensor_members(sensors_link):
            entry = {"name": s.get("Name"), "reading": s.get("Reading"),
                     "units": s.get("ReadingUnits"), "status": (s.get("Status") or {}).get("Health")}
            out[buckets.get(s.get("ReadingType", ""), "other")].append(entry)
        return out

    def _sensor_members(self, collection_uri: str) -> list[dict]:
        """Full Sensor docs — one ``?$expand`` GET where the service supports it,
        else one GET per member.

        Real BMCs expose 100–400 Sensor resources; the per-member fan-out (a
        fresh request each) is 10s of seconds to minutes, and the sensing
        hierarchy (#13) polls it. ``$expand`` collapses that to a single request.
        """
        if self._expand_ok is not False:
            expanded = self._try_expand(collection_uri)
            if expanded is not None:
                return expanded
        return [self._http.get_json(u) for u in self._members(collection_uri)]

    def _expand_operator(self) -> str | None:
        """The ``$expand`` operator the service advertises (ProtocolFeaturesSupported)."""
        eq = (self._root().get("ProtocolFeaturesSupported") or {}).get("ExpandQuery") or {}
        if eq.get("ExpandAll"):
            return "*"
        if eq.get("Levels"):
            return "."
        return None

    def _try_expand(self, collection_uri: str) -> list[dict] | None:
        op = self._expand_operator()
        if op is None:
            return None  # not advertised — go straight to per-member
        sep = "&" if "?" in collection_uri else "?"
        try:
            coll = self._http.get_json(f"{collection_uri}{sep}$expand={op}($levels=1)")
        except KVMPilotError as exc:
            # DSP0266 requires 501 for an unsupported $-query — remember it.
            if exc.status_code == 501:
                self._expand_ok = False
            return None
        members = coll.get("Members") or []
        # Members are actually expanded only if they carry more than an @odata.id.
        if members and any(isinstance(m, dict) and len(m) > 1 for m in members):
            self._expand_ok = True
            return [m for m in members if isinstance(m, dict)]
        return None

    def _read_thermal_power(self, chassis: dict) -> dict:
        out: dict[str, list] = {"temperatures": [], "fans": [], "voltages": [], "power": []}
        thermal_link = (chassis.get("Thermal") or {}).get("@odata.id")
        power_link = (chassis.get("Power") or {}).get("@odata.id")
        if thermal_link:
            t = self._http.get_json(thermal_link)
            for temp in t.get("Temperatures", []) or []:
                out["temperatures"].append({
                    "name": temp.get("Name"), "reading": temp.get("ReadingCelsius"),
                    "units": "Cel", "status": (temp.get("Status") or {}).get("Health")})
            for fan in t.get("Fans", []) or []:
                out["fans"].append({
                    "name": fan.get("Name"), "reading": fan.get("Reading"),
                    "units": fan.get("ReadingUnits"), "status": (fan.get("Status") or {}).get("Health")})
        if power_link:
            p = self._http.get_json(power_link)
            for v in p.get("Voltages", []) or []:
                out["voltages"].append({
                    "name": v.get("Name"), "reading": v.get("ReadingVolts"),
                    "units": "V", "status": (v.get("Status") or {}).get("Health")})
            for pc in p.get("PowerControl", []) or []:
                out["power"].append({
                    "name": pc.get("Name"), "reading": pc.get("PowerConsumedWatts"),
                    "units": "Watts", "status": (pc.get("Status") or {}).get("Health")})
        return out

    # -- VirtualMedia ----------------------------------------------------

    def mount_iso(self, source: str, image_name: str | None = None, cdrom: bool = True) -> str:
        insert, _eject, _slot = self._virtual_media(cdrom)
        name = image_name or source.rsplit("/", 1)[-1].split("?")[0]
        if not self.safety.guard(
            "redfish.virtual_media_insert", f"Insert virtual media {source!r} on {self.host}"
        ):
            return name
        self._handle_async(self._insert_media(insert, source))
        return name

    def _insert_media(self, target: str, source: str):
        """POST InsertMedia with the minimal body, adapting to strict BMCs.

        ``Inserted``/``WriteProtected`` are optional (DSP2046) and merely restate
        the insert defaults, but strict firmware (Supermicro X11/X12, some
        Lenovo/older iDRAC) rejects an InsertMedia body that carries them — so we
        send only ``Image``. The inverse quirk (a BMC that *requires*
        ``TransferProtocolType``) answers 400 ``ActionParameterMissing``; retry
        once with it derived from the URL scheme. The safety guard already ran in
        the caller, so the retry does not re-gate.
        """
        try:
            return self._http.request("POST", target, json_body={"Image": source})
        except KVMPilotError as exc:
            proto = _transfer_protocol(source)
            if exc.status_code == 400 and proto and _param_missing(exc, "TransferProtocolType"):
                logger.info("BMC requires TransferProtocolType; retrying InsertMedia with %s", proto)
                return self._http.request(
                    "POST", target, json_body={"Image": source, "TransferProtocolType": proto}
                )
            raise

    def msd_disconnect(self) -> None:
        _insert, eject, _slot = self._virtual_media()
        if not self.safety.guard(
            "redfish.virtual_media_eject", f"Eject virtual media on {self.host}"
        ):
            return
        resp = self._http.request("POST", eject, json_body={})
        self._handle_async(resp)

    def msd_connect(self) -> None:
        """Redfish has no separate connect step — InsertMedia attaches and connects.

        This validates that media is currently inserted (a no-op re-assert), and
        otherwise points the caller at ``mount_iso``.
        """
        _insert, _eject, slot = self._virtual_media()
        if not self._http.get_json(slot).get("Inserted"):
            raise CapabilityError(
                "Redfish has no separate virtual-media connect; "
                "use mount_iso(source) to insert and attach"
            )

    # -- lifecycle -------------------------------------------------------

    def close(self) -> None:
        """Delete the Redfish session (best-effort)."""
        self._http.logout()
        self._logged_in = False

    def __enter__(self) -> RedfishDriver:
        self._ensure_login()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


__all__ = ["RedfishDriver"]
