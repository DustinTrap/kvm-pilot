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

from dataclasses import dataclass

from ..client import PiKVMDriver
from ..errors import KVMPilotError

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

    def known_quirks(self, firmware: str | None = None) -> list[Quirk]:
        """Quirks that apply to ``firmware`` (auto-detected from the device if omitted)."""
        if firmware is None:
            try:
                firmware = self.get_firmware_info().get("version")
            except KVMPilotError:
                firmware = None
        return [q for q in GLKVM_QUIRKS if q.applies_to(firmware)]


class BliKVMDriver(PiKVMDriver):
    """BliKVM — PiKVM-API-compatible hardware.

    No deltas from the base client are known yet; this subclass exists so any
    BliKVM-specific behavior or quirks have a home.
    """

    _vendor = "blikvm"


__all__ = ["GLKVMDriver", "BliKVMDriver", "Quirk", "GLKVM_QUIRKS"]
