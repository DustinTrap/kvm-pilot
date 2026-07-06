"""
PiKVM-family driver variants: GL.iNet GLKVM and BliKVM.

``PiKVMDriver`` (in :mod:`kvm_pilot.client`) is the canonical base — the full
PiKVM REST client. The two devices here are *API-compatible forks*, so each
subclass overrides only its deltas:

  * ``GLKVMDriver`` — the GL.iNet GLKVM fork (GL-RM1 / GL-RM1PE). Its firmware
    ships the PiKVM REST API **disabled** by default, so this driver turns the
    resulting 404s into a clear, actionable error and tracks known per-firmware
    quirks.
  * ``BliKVMDriver`` — BliKVM hardware. No deltas are known yet; the subclass
    exists so device-specific behavior has a home.

Quirk data is seeded **only** with what is actually documented/known — there is
no fabricated per-firmware data. The registry is the place to record real
findings as the GL-RM1PE (the first hardware target) and others get tested.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

from ..client import PiKVMDriver
from ..errors import KVMPilotError

logger = logging.getLogger("kvm_pilot.glkvm")

_GL_API_DISABLED_HINT = (
    "On GL.iNet (GLKVM) firmware the PiKVM REST API is disabled by default; all "
    "/api/* return 404 until you enable it in /etc/kvmd/nginx-kvmd.conf and "
    "restart kvmd (it can revert on a firmware upgrade)."
)


@dataclass(frozen=True)
class Quirk:
    """A known device/firmware quirk and how to work around it.

    ``firmware`` is ``"all"`` or a substring matched against the device's reported
    version; ``source`` records provenance — ``"documented"`` (from vendor docs)
    vs ``"observed"`` (confirmed on real hardware). Keep these honest.
    """

    id: str
    summary: str
    workaround: str
    firmware: str = "all"
    source: str = "documented"

    def applies_to(self, firmware: str | None) -> bool:
        return self.firmware == "all" or (firmware is not None and self.firmware in firmware)


# The ONLY quirk we currently know for sure. Add entries here as real GL-RM1PE /
# GLKVM testing reveals release-specific behavior — with source="observed" and a
# concrete `firmware` match. Do not invent version-specific quirks.
GLKVM_QUIRKS: list[Quirk] = [
    Quirk(
        id="api-disabled-by-default",
        summary=(
            "GL firmware ships the PiKVM REST API disabled; every /api/* returns "
            "404 until it is enabled."
        ),
        workaround=(
            "Enable the API block in /etc/kvmd/nginx-kvmd.conf and restart kvmd. "
            "It can revert on a firmware upgrade, so re-check after updates."
        ),
        firmware="all",
        source="documented",
    ),
    Quirk(
        id="atx-power-state-always-off",
        summary=(
            "GL-RM1PE ATX sensing is not wired like a stock PiKVM: /api/atx "
            "reports power='off', enabled=false, and both power/hdd LEDs false "
            "even while the host is fully powered on and booted. ATX power state "
            "and LEDs are therefore unreliable on this hardware."
        ),
        workaround=(
            "Do not trust ATX power/LED readings on GLKVM; infer host state from "
            "the video stream (a screenshot / vision classification) instead. "
            "This also means power on/off/cycle cannot be confirmed via ATX — "
            "verify the result visually."
        ),
        firmware="4.82",
        source="observed",
    ),
]


class GLKVMDriver(PiKVMDriver):
    """GL.iNet GLKVM fork (GL-RM1 / GL-RM1PE) — first hardware target.

    API-compatible with PiKVM, but surfaces the 'API disabled' 404 as an
    ``ApiDisabledError`` and exposes the known per-firmware quirks.
    """

    _NOT_FOUND_HINT = _GL_API_DISABLED_HINT
    _vendor = "gl.inet"

    def get_firmware_info(self) -> dict:
        """GL reports its **product** firmware (what the UI shows) at
        ``/api/upgrade/version`` — e.g. ``{"model": "RM1PE", "version": "V1.9.1
        release1"}``. Use that as the identity version/product so the report
        matches the UI; keep the kvmd component version alongside. Falls back to
        the base (kvmd-only) info on firmware that lacks the endpoint.
        """
        info = super().get_firmware_info()
        try:
            up = self._http.get("/api/upgrade/version")
        except KVMPilotError:
            return info
        if isinstance(up, dict):
            if up.get("version"):
                info["version"] = up["version"]        # "V1.9.1 release1" (what the UI shows)
            if up.get("model"):
                info["product"] = up["model"]          # "RM1PE"
                info["model"] = up["model"]
        return info

    def get_available_update(self) -> dict | None:
        """GL's own update check (``/api/upgrade/compare``): the installed version vs
        the latest GL publishes for this model. Returns
        ``{current, latest, beta, update_available}`` or ``None`` if unavailable.

        This is the telemetry that feeds firmware_registry.reconcile — a device
        that knows its vendor's newest release can contribute it to the SSoT.
        """
        try:
            c = self._http.get("/api/upgrade/compare")
        except KVMPilotError:
            return None
        if not isinstance(c, dict):
            return None
        local, server = c.get("local_version"), c.get("server_version")
        if not (local and server):
            return None
        return {
            "current": local,
            "latest": server,
            "beta": c.get("beta_version") or None,
            "update_available": local != server,
        }

    def known_quirks(self, firmware: str | None = None) -> list[Quirk]:
        """Quirks that apply to ``firmware`` (auto-detected from the device if omitted).

        Auto-detection matches each quirk against **every** version the device
        reports — the GL product firmware (``version``, what the UI shows) *and*
        the kvmd component (``kvmd_version``) — because quirks may be keyed to
        either (the ATX quirk was observed against kvmd 4.82, which
        ``get_firmware_info`` no longer reports as ``version``; #139).
        """
        if firmware is not None:
            versions: list[str | None] = [firmware]
        else:
            try:
                fw = self.get_firmware_info()
            except KVMPilotError:
                fw = {}
            versions = [v for v in (fw.get("version"), fw.get("kvmd_version")) if v] or [None]
        return [q for q in GLKVM_QUIRKS if any(q.applies_to(v) for v in versions)]

    # -- FirmwareUpdate capability (GL /api/upgrade/*) ---------------------
    #
    # These implement the ``FirmwareUpdate`` protocol (drivers/base.py), so a
    # GLKVMDriver advertises ``Capability.FIRMWARE_UPDATE``. GL's ``/api/upgrade/*``
    # is a proprietary layer on top of kvmd (upstream PiKVM has no OS-update API);
    # the endpoint set was reverse-engineered from live probing + the gl-inet/glkvm
    # source, so request bodies are provisional. Feature-detect via
    # ``/api/upgrade/status`` and degrade gracefully. See docs/firmware-update.md.

    def get_upgrade_status(self) -> dict:
        """Read-only view of GL's remote-upgrade subsystem — never flashes.

        Aggregates GET ``/api/upgrade/{status,version,download}``:
        ``{enabled, current, model, image_size}``. Sub-endpoints missing on older
        firmware are skipped, so the shape degrades gracefully; ``enabled`` is
        ``False`` when the subsystem is absent.
        """
        out: dict = {"enabled": False}
        try:
            st = self._http.get("/api/upgrade/status")
        except KVMPilotError:
            return out  # subsystem absent -> detect-only
        if isinstance(st, dict):
            out["enabled"] = bool(st.get("enabled"))
        try:
            ver = self._http.get("/api/upgrade/version")
            if isinstance(ver, dict):
                out["current"] = ver.get("version")
                out["model"] = ver.get("model")
        except KVMPilotError:
            pass
        try:
            dl = self._http.get("/api/upgrade/download")
            if isinstance(dl, dict) and dl.get("size"):
                out["image_size"] = dl["size"]
        except KVMPilotError:
            pass
        return out

    def _firmware_plan(self, image: str | None) -> list[dict]:
        """The ordered POSTs a flash would send (also the dry-run plan)."""
        steps: list[dict] = []
        if image:
            steps.append({"method": "POST", "path": "/api/upgrade/upload",
                          "note": f"upload local image {image}"})
        steps.append({"method": "POST", "path": "/api/upgrade/start",
                      "note": "flash the staged image; the device auto-reboots"})
        return steps

    def _upgrade_state_reached(self, baseline: object) -> bool:
        """One probe: has the device visibly entered an upgrade state?

        The ``/api/upgrade/*`` bodies are provisional (#95), so "entered" means
        any of: the status body carries a truthy upgrade-ish field, the body
        changed from the pre-start ``baseline``, or the endpoint dropped (the
        documented mid-flash reboot behavior).
        """
        try:
            st = self._http.get("/api/upgrade/status")
        except KVMPilotError:
            return True  # channel dropped -> the device is rebooting into the flash
        if isinstance(st, dict) and any(
            st.get(k) for k in ("status", "state", "percent", "progress", "upgrading")
        ):
            return True
        return baseline is not None and st != baseline

    def apply_firmware_update(
        self,
        *,
        image: str | None = None,
        dry_run: bool = True,
        verify_timeout: float = 15.0,
        poll_interval: float = 2.0,
    ) -> dict:
        """Flash the GLKVM's **own** firmware — the most destructive op we expose.

        The device reboots into the new image (dropping this REST channel) and a
        failed flash needs physical U-Boot recovery — callers must vet the reliability
        (registry ``profile.remote_update``) and recovery path first. With ``image``,
        a local firmware file is uploaded first (POST ``/api/upgrade/upload``); without
        it, the already-staged image (GET ``/api/upgrade/download``) is flashed. Then
        POST ``/api/upgrade/start``; the device auto-reboots.

        A non-error ``start`` response is NOT trusted (#94: on a real RM1PE it can
        200 and no-op): the device must visibly enter an upgrade state within
        ``verify_timeout`` seconds — status transition or channel drop — or the
        call reports failure (``sent: False`` + ``error``, raw response in
        ``result``).

        Gated: routed through ``safety.guard("firmware.flash", …)``. ``dry_run=True``
        (default) plans only and sends nothing. Returns
        ``{sent, dry_run, plan[, result, error, verified]}``.
        The ``/api/upgrade/*`` request bodies are provisional (not vendor-documented).
        """
        plan = self._firmware_plan(image)
        desc = (
            f"Flash firmware on {self.host} via GL /api/upgrade "
            f"({'upload ' + image + ' then ' if image else 'staged image, then '}"
            "reboot). The device drops this channel and a failed flash needs physical recovery."
        )
        if dry_run:
            logger.warning(
                "DRY-RUN firmware flash on %s — would run: %s",
                self.host, "; ".join(f"{s['method']} {s['path']}" for s in plan),
            )
            return {"sent": False, "dry_run": True, "plan": plan}
        if not self.safety.guard("firmware.flash", desc):
            return {"sent": False, "dry_run": False, "plan": plan}
        if image:
            p = Path(image)
            size = p.stat().st_size
            logger.info("Uploading firmware %s (%.0f MB) to %s", p.name, size / 1024 / 1024, self.host)
            with p.open("rb") as fh:
                self._http.post(
                    "/api/upgrade/upload", body=fh,
                    content_type="application/octet-stream",
                    extra_headers={"Content-Length": str(size)},
                    long_timeout=1800, retry=False,
                )
        try:
            baseline = self._http.get("/api/upgrade/status")
        except KVMPilotError:
            baseline = None
        logger.warning(
            "Starting firmware flash on %s — do NOT interrupt power; the device will "
            "reboot and this REST channel will drop.", self.host,
        )
        result = self._http.post("/api/upgrade/start", long_timeout=1800, retry=False)
        deadline = time.monotonic() + verify_timeout
        while not self._upgrade_state_reached(baseline):
            if time.monotonic() >= deadline:
                logger.error(
                    "Device did not enter an upgrade state within %.0fs of "
                    "/api/upgrade/start — treating the flash as a no-op (#94).",
                    verify_timeout,
                )
                return {
                    "sent": False, "dry_run": False, "plan": plan, "result": result,
                    "error": (
                        f"device did not enter upgrade state within {verify_timeout:.0f}s "
                        "after start; the start POST likely no-opped (#94/#95) — raw "
                        "response in 'result'"
                    ),
                }
            time.sleep(poll_interval)
        return {"sent": True, "dry_run": False, "plan": plan, "result": result,
                "verified": "upgrade-state"}


class BliKVMDriver(PiKVMDriver):
    """BliKVM — PiKVM-API-compatible hardware.

    No deltas from the base client are known yet; this subclass exists so any
    BliKVM-specific behavior or quirks have a home.
    """

    _vendor = "blikvm"


__all__ = ["GLKVMDriver", "BliKVMDriver", "Quirk", "GLKVM_QUIRKS"]
