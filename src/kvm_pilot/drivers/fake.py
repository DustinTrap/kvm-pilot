"""
FakeDriver — an in-process, hardware-free KVM driver.

It implements the same capability protocols as the PiKVM client (``SystemInfo``,
``Power``, ``HID``, ``Video``, ``VirtualMedia``, ``GPIO``, ``Events``, ``Logs``)
plus ``BootProgress``, over mutable in-memory state. No network, no hardware: it
is the device double for tests, demos, and CI — and the first concrete proof
that the capability protocols work as a plugin seam. It is the first
``BootProgress`` implementer (the PiKVM client already supplies ``Logs``), so
the structured-boot-phase sensing protocol is no longer purely speculative.

Destructive operations still route through ``SafetyPolicy.guard()`` using the
same op identifiers as the real client, so dry-run and confirmation behave
identically to a real device. That makes a ``FakeDriver`` a faithful stand-in
for exercising the safety layer, the analyzer, and the CLI without touching
hardware — which is exactly what an alpha that must *never run on real hardware*
needs.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any

from ..safety import SafetyPolicy
from .base import CapabilityMixin

# A minimal valid JPEG (SOI + APP0), enough for snapshot()/base64 round-trips.
_FAKE_JPEG = bytes.fromhex("ffd8ffe000104a46494600010100000101000000")


class FakeDriver(CapabilityMixin):
    """A fully scriptable, in-memory KVM device.

    Args:
        host: A label for the device (no network is ever contacted).
        powered: Initial ATX power state.
        phase: A vision phase token reported by ``get_boot_progress()``.
        video_signal: Whether a live capture source is reported.
        ocr_text / logs: Canned text for ``snapshot_ocr()`` / ``get_logs()``.
        events: Events ``watch_events()`` will replay (then stop — unlike the
            real unbounded WebSocket stream).
        image: Raw bytes ``snapshot()`` returns (default: a tiny stub JPEG).
        dry_run / confirm: Wired straight into a ``SafetyPolicy`` so destructive
            ops gate exactly as on a real driver.
    """

    def __init__(
        self,
        host: str = "fake",
        *,
        powered: bool = False,
        phase: str = "power_off",
        video_signal: bool = True,
        ocr_text: str = "",
        logs: str = "",
        events: list[dict] | None = None,
        image: bytes | None = None,
        dry_run: bool = False,
        confirm: Callable[[str, str], bool] | None = None,
    ):
        self.host = host
        self.powered = powered
        self.phase = phase
        self.video_signal = video_signal
        self.ocr_text = ocr_text
        self.logs = logs
        self._events = list(events or [])
        self._image = image if image is not None else _FAKE_JPEG
        self.safety = SafetyPolicy(dry_run=dry_run, confirm=confirm)
        # Action log + per-channel records, for test assertions.
        self.actions: list[tuple[str, Any]] = []
        self.typed: list[str] = []
        self.keys: list[str] = []
        self.shortcuts: list[str] = []
        self.mounted: list[str] = []

    def _record(self, name: str, detail: Any = None) -> None:
        self.actions.append((name, detail))

    # -- SystemInfo ------------------------------------------------------

    def get_info(self, fields: list | None = None) -> dict:
        return {
            "host": self.host,
            "driver": "fake",
            "powered": self.powered,
            "phase": self.phase,
        }

    # -- Power (gated) ---------------------------------------------------

    def is_powered_on(self) -> bool:
        return self.powered

    def power_on(self, wait: bool = True) -> None:
        if self.safety.guard("atx.power_on", f"Power ON {self.host}"):
            self.powered = True
            self._record("power_on")

    def power_off(self, wait: bool = True) -> None:
        if self.safety.guard("atx.power_off", f"Graceful power OFF {self.host}"):
            self.powered = False
            self._record("power_off")

    def power_off_hard(self, wait: bool = True) -> None:
        if self.safety.guard("atx.power_off_hard", f"HARD power off {self.host}"):
            self.powered = False
            self._record("power_off_hard")

    def reset_hard(self, wait: bool = True) -> None:
        if self.safety.guard("atx.reset_hard", f"HARD reset {self.host}"):
            self._record("reset_hard")

    def hard_cycle(self, off_delay: float = 0.0, on_delay: float = 0.0) -> None:
        self.power_off_hard()
        self.power_on()

    # -- HID -------------------------------------------------------------

    def type_text(self, text: str, **kw: Any) -> None:
        if self.safety.guard("hid.type_text", f"Type {len(text)} characters into {self.host}"):
            self.typed.append(text)
            self._record("type_text", text)

    def press_key(self, key: str, **kw: Any) -> None:
        if self.safety.guard("hid.press_key", f"Press {key!r} on {self.host}"):
            self.keys.append(key)
            self._record("press_key", key)

    def send_shortcut(self, keys: str) -> None:
        if self.safety.guard("hid.send_shortcut", f"Send shortcut {keys!r} to {self.host}"):
            self.shortcuts.append(keys)
            self._record("send_shortcut", keys)

    def mouse_move(self, x: int, y: int) -> None:
        # kvmd coordinate contract: -32768..32767 per axis, (0, 0) at screen
        # center — mirrored from PiKVMDriver.mouse_move. Moves stay ungated.
        self._record("mouse_move", (x, y))

    def mouse_click(self, button: str = "left", **kw: Any) -> None:
        if self.safety.guard("hid.mouse_click", f"Mouse {button} click on {self.host}"):
            self._record("mouse_click", button)

    # -- Video -----------------------------------------------------------

    def snapshot(self) -> bytes:
        return self._image

    def snapshot_base64(self) -> str:
        return base64.b64encode(self._image).decode()

    def snapshot_save(self, path: str) -> Path:
        out = Path(path)
        out.write_bytes(self._image)
        return out

    def snapshot_ocr(self, lang: str = "eng", region: Any = None) -> str:
        return self.ocr_text

    def has_video_signal(self) -> bool:
        return self.video_signal

    # -- VirtualMedia (gated) --------------------------------------------

    def mount_iso(
        self, source: str, image_name: str | None = None, cdrom: bool = True
    ) -> str:
        name = image_name or source.split("/")[-1].split("?")[0]
        # Evaluate every guard as a separate statement (NOT `a and b`, which would
        # short-circuit and skip later guards under dry-run). This mirrors the
        # real client's sequential upload/msd_set_params()/msd_connect() so
        # dry-run fires all gates and deny raises at the first — keeping the fake
        # a faithful safety-layer stand-in.
        write_op = "msd.write_remote" if source.startswith(("http://", "https://")) else "msd.write"
        write_ok = self.safety.guard(write_op, f"Upload image '{name}' to {self.host}")
        set_ok = self.safety.guard("msd.set_params", f"Select MSD image {name!r} on {self.host}")
        connect_ok = self.safety.guard("msd.connect", f"Attach virtual media to {self.host}")
        if write_ok and set_ok and connect_ok:
            self.mounted.append(name)
            self._record("mount_iso", name)
        return name

    def msd_connect(self) -> None:
        if self.safety.guard("msd.connect", f"Attach virtual media to {self.host}"):
            self._record("msd_connect")

    def msd_disconnect(self) -> None:
        if self.safety.guard("msd.disconnect", f"Detach virtual media from {self.host}"):
            self._record("msd_disconnect")

    # -- GPIO (gated) ----------------------------------------------------

    def gpio_switch(self, channel: str, state: bool, wait: bool = True) -> None:
        if self.safety.guard("gpio.switch", f"GPIO {channel!r} -> {'on' if state else 'off'}"):
            self._record("gpio_switch", (channel, state))

    def gpio_pulse(self, channel: str, delay: float = 0.1, wait: bool = True) -> None:
        if self.safety.guard("gpio.pulse", f"GPIO {channel!r} pulse"):
            self._record("gpio_pulse", channel)

    # -- Events ----------------------------------------------------------

    def watch_events(
        self, on_event: Callable | None = None, stream: bool = True,
        timeout: float | None = None,
    ) -> Generator:
        """Replay the queued events, then stop (the real driver streams forever)."""
        for evt in list(self._events):
            if on_event:
                on_event(evt.get("event_type"), evt.get("event", {}))
            yield evt

    def push_event(self, event_type: str, event: dict | None = None) -> None:
        """Queue an event for the next ``watch_events()`` call."""
        self._events.append({"event_type": event_type, "event": event or {}})

    # -- Logs ------------------------------------------------------------

    def get_logs(self, seek: int = 0, follow: bool = False) -> str:
        return self.logs

    # -- BootProgress (structured phase — first BootProgress implementer) -

    def get_boot_progress(self) -> str | None:
        """Report the boot phase as a structured token, no screenshot needed.

        Returns ``None`` while powered off, mirroring a BMC that reports nothing
        until the host is up.
        """
        return self.phase if self.powered else None


__all__ = ["FakeDriver"]
