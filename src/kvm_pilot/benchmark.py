"""Per-command latency + capability profiler — the data the interface router eats.

The adaptive interface router (#181) picks, for each intent on each device, the
**fastest interface that will actually produce the result right now**. To do that
it needs a *scorecard*: for every command, is this interface capable on this
device, and how fast is it? This module produces that scorecard for the
**library-direct** interface (the engine's own in-process driver calls — the
latency floor). MCP / browser rows are layered on by the router later; the
schema already carries an ``interface`` field so they slot in without a rewrite.

Two axes matter, and both are measured here because "fastest" is meaningless
without "that works":

* **capability** — did the call return without error? This is *state-dependent*,
  not static: on a GL-RM1PE the same ``snapshot`` yields a JPEG at 1024x768 or a
  warm streamer and an undecodable H.264 NAL at 2560x1440 (see the
  ``snapshot-needs-video-client`` quirk, #107/#151). So the scorecard is a
  snapshot-in-time the router must re-take when device/host state changes.
* **latency** — the warm p50, in milliseconds (the first, cold sample is dropped
  so a new-connection TLS handshake doesn't skew it).

stdlib-only; drivers are passed in already built (this module never opens a
connection or chooses credentials).
"""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .drivers.base import Capability

if TYPE_CHECKING:  # avoid importing driver internals at module import time
    from .drivers.base import KVMDriver

# The library-direct interface this module measures. Named so scorecards from
# here compose with the router's MCP/browser rows under one schema.
INTERFACE = "library"


def _ewma(old: float | None, new: float, alpha: float = 0.3) -> float:
    """Exponentially-weighted moving average — a light-touch online p50 update
    (recent observations weigh more; no sample history retained)."""
    return new if old is None else (1 - alpha) * old + alpha * new


@dataclass(frozen=True)
class Op:
    """One benchmarkable command: the capability it needs and how to invoke it.

    ``capability=None`` marks a structural/offline op (``capabilities``) that
    every driver serves without a network call. ``run`` must be **non-destructive**
    — reads, or the harmless absolute ``mouse_move`` used as the HID probe.
    """

    name: str
    capability: Capability | None
    run: Callable[[KVMDriver], object]
    is_hid: bool = False


# Absolute mid-screen in kvmd raw coordinate space (-32768..32767 → 16000 ≈ 74%).
# A move (never a click) is the safe HID probe: it perturbs nothing on the target.
_HID_PROBE = (16000, 16000)

DEFAULT_OPS: tuple[Op, ...] = (
    Op("capabilities", None, lambda d: d.capabilities()),
    Op("get_info", Capability.SYSTEM_INFO, lambda d: d.get_info()),  # type: ignore[attr-defined]
    Op("get_logs", Capability.LOGS, lambda d: d.get_logs(seek=0)),  # type: ignore[attr-defined]
    Op("is_powered_on", Capability.POWER, lambda d: d.is_powered_on()),  # type: ignore[attr-defined]
    Op("read_sensors", Capability.SENSORS, lambda d: d.read_sensors()),  # type: ignore[attr-defined]
    Op("boot_progress", Capability.BOOT_PROGRESS, lambda d: d.get_boot_progress()),  # type: ignore[attr-defined]
    Op("get_boot_options", Capability.BOOT_CONFIG, lambda d: d.get_boot_options()),  # type: ignore[attr-defined]
    Op("snapshot", Capability.VIDEO, lambda d: d.snapshot()),  # type: ignore[attr-defined]
    Op("mouse_move", Capability.HID, lambda d: d.mouse_move(*_HID_PROBE), is_hid=True),  # type: ignore[attr-defined]
)


@dataclass
class CommandResult:
    """How one command fared on one interface for one device, right now."""

    command: str
    interface: str
    capable: bool
    p50_ms: float | None
    samples: int
    note: str = ""

    def to_dict(self) -> dict:
        return {
            "command": self.command,
            "interface": self.interface,
            "capable": self.capable,
            "p50_ms": round(self.p50_ms, 1) if self.p50_ms is not None else None,
            "samples": self.samples,
            "note": self.note,
        }


@dataclass
class Scorecard:
    """A device's per-command profile at one point in time.

    ``fastest(command)`` is the router's core query: the cheapest *capable* row.
    Because capability is state-dependent, a scorecard is only valid until the
    device/host state changes (firmware, resolution, streamer warmth, power) —
    the router re-benchmarks on those signals (#181).
    """

    host: str
    driver: str
    firmware: str | None
    results: list[CommandResult] = field(default_factory=list)

    def fastest(self, command: str) -> CommandResult | None:
        """The lowest-latency *capable* result for ``command`` (None if none)."""
        candidates = [
            r for r in self.results
            if r.command == command and r.capable and r.p50_ms is not None
        ]
        return min(candidates, key=lambda r: r.p50_ms) if candidates else None  # type: ignore[arg-type,return-value]

    def to_dict(self) -> dict:
        return {
            "host": self.host,
            "driver": self.driver,
            "firmware": self.firmware,
            "results": [r.to_dict() for r in self.results],
        }

    @classmethod
    def from_dict(cls, data: dict) -> Scorecard:
        keys = ("command", "interface", "capable", "p50_ms", "samples", "note")
        results = [
            CommandResult(**{k: row.get(k, "" if k == "note" else None) for k in keys})
            for row in data.get("results", [])
        ]
        return cls(
            host=data["host"], driver=data["driver"],
            firmware=data.get("firmware"), results=results,
        )

    def record(self, command: str, interface: str, latency_ms: float | None, ok: bool) -> None:
        """Fold one real call's outcome into the scorecard — online learning (#181).

        The router's estimates self-tune from actual use: a matching row's p50 is
        nudged toward the observed latency (EWMA — recent calls weigh more, no
        history kept), and its capability is set to the last outcome so a freshly
        broken interface (auth lost, host down) stops being selected until it
        succeeds again. Unknown (command, interface) pairs are appended.
        """
        for r in self.results:
            if r.command == command and r.interface == interface:
                r.samples += 1
                r.capable = ok
                if ok and latency_ms is not None:
                    r.p50_ms = round(_ewma(r.p50_ms, latency_ms), 1)
                r.note = "observed" if ok else "last call failed"
                return
        self.results.append(
            CommandResult(
                command, interface, ok,
                round(latency_ms, 1) if (ok and latency_ms is not None) else None,
                1, "observed" if ok else "last call failed",
            )
        )


def _time_calls(
    thunk: Callable[[], object], samples: int, clock: Callable[[], float]
) -> tuple[float | None, int, str]:
    """Time ``thunk`` ``samples`` times → (warm p50 ms | None, ok_count, last_err).

    The cold first sample is dropped from the p50. A call that raises is caught
    and its type+message recorded — this is how a state-gated failure (a snapshot
    that comes back H.264, a 503 from a cold streamer, an SSH auth reject) is
    captured as *data* rather than crashing the sweep. Transport-agnostic, so the
    library, SSH, and WinRM probes all share it.
    """
    durations: list[float] = []
    ok = 0
    last_err = ""
    for _ in range(max(1, samples)):
        start = clock()
        try:
            thunk()
            durations.append((clock() - start) * 1000.0)
            ok += 1
        except Exception as exc:  # noqa: BLE001 - a failed probe is data, not a crash
            last_err = type(exc).__name__
            msg = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
            if msg:
                last_err = f"{last_err}: {msg[:80]}"
    warm = durations[1:] if len(durations) > 1 else durations
    return (statistics.median(warm) if warm else None), ok, last_err


def _measure(op: Op, driver: KVMDriver, samples: int, clock: Callable[[], float]) -> CommandResult:
    """Library-direct row for one op (see :func:`_time_calls` for the failure model)."""
    p50, ok, err = _time_calls(lambda: op.run(driver), samples, clock)
    return CommandResult(
        command=op.name,
        interface=INTERFACE,
        capable=ok > 0,
        p50_ms=p50,
        samples=ok,
        note="" if ok > 0 else (err or "no successful sample"),
    )


def benchmark_driver(
    driver: KVMDriver,
    *,
    host: str,
    driver_kind: str,
    firmware: str | None = None,
    samples: int = 6,
    hid: bool = True,
    ops: tuple[Op, ...] = DEFAULT_OPS,
    clock: Callable[[], float] = time.perf_counter,
) -> Scorecard:
    """Profile every op the driver *structurally* supports, in-process.

    Ops whose capability the driver lacks are recorded as ``capable=False`` with
    ``samples=0`` and no network call (so a capability-partial driver — e.g. a
    Redfish BMC with no video — is charted, not skipped silently). Set
    ``hid=False`` to omit the harmless HID mouse-move probe (it moves the target
    cursor). ``clock`` is injectable for deterministic tests.
    """
    results: list[CommandResult] = []
    for op in ops:
        if op.is_hid and not hid:
            continue
        if op.capability is not None and not driver.supports(op.capability):
            results.append(
                CommandResult(
                    command=op.name,
                    interface=INTERFACE,
                    capable=False,
                    p50_ms=None,
                    samples=0,
                    note=f"{op.capability.value} capability not supported by driver",
                )
            )
            continue
        results.append(_measure(op, driver, samples, clock))
    return Scorecard(host=host, driver=driver_kind, firmware=firmware, results=results)


def _allow(*_a: object) -> bool:
    # Benchmark probes are read-only and intentional; auto-approve the safety gate.
    return True


def _nonzero_raises(res: object) -> None:
    """An ``ssh_exec`` dict with a non-zero return code is a *failed* probe.

    ssh/PowerShell report unavailability (bad auth = 255, missing shell = 127)
    via the exit code, not a Python exception — so the timing loop only counts a
    call capable if the remote command actually succeeded.
    """
    if isinstance(res, dict):
        rc = res.get("returncode")
        if rc not in (0, None):
            stderr = (res.get("stderr") or "").strip().splitlines()
            raise RuntimeError(f"exit {rc}: {stderr[0][:60] if stderr else ''}")


def benchmark_ssh(
    cfg: object, *, samples: int = 4, probe: str = "true", persistent: bool = True,
    clock: Callable[[], float] = time.perf_counter,
) -> list[CommandResult]:
    """One ``exec`` row for the **SSH** interface (in-band shell on the target OS).

    ``persistent`` (default) reuses one OpenSSH ControlMaster connection so only
    the first (cold, dropped) call pays the handshake — measured ~10x faster on a
    LAN host. Set ``persistent=False`` to measure the fresh-connection-per-call
    cost. Not-capable (no timing) when SSH isn't configured or the port is
    closed — recorded honestly so the router won't pick a dead interface.
    """
    from .errors import CapabilityError
    from .ssh import SSHChannel

    try:
        ch = SSHChannel.from_config(cfg, confirm=_allow)  # type: ignore[arg-type]
    except CapabilityError:
        return [CommandResult("exec", "ssh", False, None, 0, "ssh_host not configured for profile")]
    ch.persist = persistent
    if not ch.ssh_reachable():
        return [CommandResult("exec", "ssh", False, None, 0, "ssh target not reachable")]

    def _probe() -> None:
        _nonzero_raises(ch.ssh_exec(probe))

    try:
        p50, ok, err = _time_calls(_probe, samples, clock)
    finally:
        ch.close()  # tear down the ControlMaster if one was started
    mode = "ControlPersist" if persistent else "fresh connection per call"
    return [CommandResult("exec", "ssh", ok > 0, p50, ok, mode if ok > 0 else (err or "exec failed"))]


def benchmark_winrm(
    cfg: object, *, samples: int = 4, shell: str = "powershell",
    probe: str = "$PSVersionTable.PSVersion.Major",
    clock: Callable[[], float] = time.perf_counter,
) -> list[CommandResult]:
    """One ``ps_exec`` row for the **WinRM / remote-PowerShell** interface.

    Ships over the SSH transport (:mod:`kvm_pilot.remote_ps`); a target reachable
    by SSH but without ``powershell``/``pwsh`` reports capable=False (exit 127),
    which is exactly the signal the router needs.
    """
    from .errors import CapabilityError
    from .remote_ps import RemotePowerShell

    try:
        rp = RemotePowerShell.from_config(cfg, confirm=_allow, shell=shell)  # type: ignore[arg-type]  # nosec B604 - 'shell' is a PowerShell interpreter name, not subprocess shell=True
    except CapabilityError:
        return [CommandResult("ps_exec", "winrm", False, None, 0, "ssh_host not configured (winrm-over-ssh)")]
    if not rp.reachable():
        return [CommandResult("ps_exec", "winrm", False, None, 0, "target not reachable")]

    def _probe() -> None:
        _nonzero_raises(rp.run_ps(probe))

    p50, ok, err = _time_calls(_probe, samples, clock)
    return [CommandResult("ps_exec", "winrm", ok > 0, p50, ok, "" if ok > 0 else (err or "ps_exec failed"))]


def benchmark_all(
    driver: KVMDriver, cfg: object, *, host: str, driver_kind: str,
    firmware: str | None = None, samples: int = 6, hid: bool = True,
    os_plane: bool = True, clock: Callable[[], float] = time.perf_counter,
) -> Scorecard:
    """Full multi-interface scorecard: library-direct rows + (if ``os_plane``) the
    in-band SSH and WinRM rows. This is what the router scores across."""
    card = benchmark_driver(
        driver, host=host, driver_kind=driver_kind, firmware=firmware,
        samples=samples, hid=hid, clock=clock,
    )
    if os_plane:
        card.results.extend(benchmark_ssh(cfg, samples=min(samples, 4), clock=clock))
        card.results.extend(benchmark_winrm(cfg, samples=min(samples, 4), clock=clock))
    return card
