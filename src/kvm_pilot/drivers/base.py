"""
Device-driver capability model for kvm-pilot.

Many KVM / BMC devices exist and they expose very different feature sets, so a
single monolithic interface is the wrong shape. Instead each capability is a
small, ``@runtime_checkable`` ``Protocol``: a driver implements only the ones
its hardware actually supports, and advertises them via ``capabilities()``.

The protocols are defined here; the PiKVM client (``KVMClient``) implements
them via ``CapabilityMixin``, the ``make_driver`` registry lives in
``drivers/__init__.py``, and the concrete non-PiKVM drivers (``FakeDriver``,
``RedfishDriver``) prove the seam; see ``docs/architecture.md``.
"""

from __future__ import annotations

import logging
import time
from enum import StrEnum
from typing import Protocol, runtime_checkable

logger = logging.getLogger("kvm_pilot.drivers")


class Capability(StrEnum):
    """A feature area a KVM driver may or may not support."""

    SYSTEM_INFO = "system_info"
    POWER = "power"
    HID = "hid"
    VIDEO = "video"
    VIRTUAL_MEDIA = "virtual_media"
    GPIO = "gpio"
    EVENTS = "events"
    # Sensing capabilities — cheaper-than-vision signals. A driver that has any
    # of these lets the analyzer answer "what phase / is it alive / did it crash"
    # without a VLM. See docs/sensing-hierarchy.svg.
    LOGS = "logs"
    BOOT_PROGRESS = "boot_progress"
    SENSORS = "sensors"
    SERIAL_CONSOLE = "serial_console"
    WATCHDOG = "watchdog"
    FIRMWARE_UPDATE = "firmware_update"
    # In-band control of the managed HOST's OS over SSH (the machine behind the
    # KVM, not the KVM appliance). A per-profile channel, not something a KVM
    # driver implements — see src/kvm_pilot/ssh.py and RemoteShell below.
    SSH = "ssh"


@runtime_checkable
class SystemInfo(Protocol):
    """Read device / host information."""

    def get_info(self, fields: list | None = None) -> dict: ...


@runtime_checkable
class Power(Protocol):
    """Control and read host power state."""

    def power_on(self, wait: bool = True) -> None: ...
    def power_off(self, wait: bool = True) -> None: ...
    def power_off_hard(self, wait: bool = True) -> None: ...
    def reset_hard(self, wait: bool = True) -> None: ...
    def is_powered_on(self) -> bool: ...


@runtime_checkable
class HID(Protocol):
    """Emulate keyboard and mouse input."""

    def type_text(self, text: str) -> None: ...
    def press_key(self, key: str) -> None: ...
    def send_shortcut(self, keys: str) -> None: ...
    def mouse_move(self, x: int, y: int) -> None: ...
    def mouse_click(self, button: str = "left") -> None: ...


@runtime_checkable
class Video(Protocol):
    """Capture a still frame of the host console (feeds the vision layer)."""

    def snapshot(self) -> bytes: ...
    def snapshot_base64(self) -> str: ...


@runtime_checkable
class VirtualMedia(Protocol):
    """Attach / detach virtual media (ISO or USB image)."""

    def mount_iso(self, source: str, image_name: str | None = None, cdrom: bool = True) -> str: ...
    def msd_connect(self) -> None: ...
    def msd_disconnect(self) -> None: ...


@runtime_checkable
class GPIO(Protocol):
    """Drive GPIO channels (relays, power buttons, LEDs)."""

    def gpio_switch(self, channel: str, state: bool) -> None: ...
    def gpio_pulse(self, channel: str) -> None: ...


@runtime_checkable
class Events(Protocol):
    """Stream asynchronous device events."""

    def watch_events(self) -> object: ...


# -- sensing protocols -----------------------------------------------------
#
# These are the structured / text signals that let a driver report state more
# cheaply than classifying a screenshot. The PiKVM client implements ``Logs``
# today (``/api/log``); the rest are the seam for the Redfish and IPMI drivers
# (``BootProgress`` from ComputerSystem, ``Sensors``/``SerialConsole`` from the
# BMC, ``Watchdog`` from IPMI). A driver implements only what its hardware has.
# They are the intended cheaper-than-vision source for boot phase / liveness:
# today ``ScreenAnalyzer`` gates on the device's own probes (``is_powered_on``,
# ``has_video_signal``, ``snapshot_ocr``); a driver that implements these will
# let it answer from structured state in more cases. See docs/sensing-hierarchy.svg.


@runtime_checkable
class Logs(Protocol):
    """Read device or host event logs (kvmd journal, Redfish SEL / lifecycle
    log, IPMI SEL).

    ``seek`` is **seconds of lookback** — ``seek=3600`` returns the last hour —
    uniformly across drivers (kvmd's fixed ``/api/log?seek=N`` semantics; the
    Redfish driver filters ``LogEntry.Created`` to match). ``seek=0`` (default)
    returns everything available.

    ``follow`` (tail -f) is a streaming mode the current blocking transport
    cannot serve; drivers raise ``CapabilityError`` for it until a streaming
    entry point exists (see ``PiKVMDriver`` / ``RedfishDriver``).
    """

    def get_logs(self, seek: int = 0, follow: bool = False) -> str: ...


@runtime_checkable
class BootProgress(Protocol):
    """Report the host POST / boot phase as a structured value rather than a
    screenshot (e.g. Redfish ``ComputerSystem.BootProgress.LastState``).

    ``get_boot_progress`` returns a driver-agnostic phase token (the same
    vocabulary the vision backend emits) or ``None`` when the device cannot
    report it.
    """

    def get_boot_progress(self) -> str | None: ...


@runtime_checkable
class Sensors(Protocol):
    """Read structured environmental / power telemetry — temperatures, fan
    RPM, voltages, watts (Redfish Sensors/Power, IPMI SDR/DCMI)."""

    def read_sensors(self) -> dict: ...


@runtime_checkable
class SerialConsole(Protocol):
    """Read and write the host serial console as text (Redfish SOL, IPMI SOL,
    or a wired serial line) — GRUB, dmesg, kernel panics, getty."""

    def serial_read(self, timeout: float = 1.0) -> str: ...
    def serial_write(self, data: str) -> None: ...


@runtime_checkable
class RemoteShell(Protocol):
    """In-band control of the managed HOST's OS over SSH — reachability plus
    command execution, once the target OS is on the network.

    This targets the host *behind* the KVM, not the KVM appliance, and is a
    per-profile channel (built from the profile's ``ssh_*`` config) rather than a
    KVM-driver capability. ``ssh_reachable`` is a read-only probe; ``ssh_exec``
    can change the target's state and MUST route through
    ``safety.guard("ssh.exec", …)``. See ``src/kvm_pilot/ssh.py``."""

    def ssh_reachable(self) -> bool: ...
    def ssh_exec(self, command: str, *, timeout: float | None = None) -> dict: ...


@runtime_checkable
class Watchdog(Protocol):
    """Arm / pet / inspect a hardware watchdog timer (IPMI) — an OS-liveness
    primitive whose expiry pinpoints a hang."""

    def watchdog_status(self) -> dict: ...
    def watchdog_arm(self, timeout_s: int) -> None: ...
    def watchdog_pet(self) -> None: ...


@runtime_checkable
class FirmwareUpdate(Protocol):
    """Flash the **KVM/BMC's own firmware** over the network (GL ``/api/upgrade/*``,
    Redfish ``UpdateService``).

    This is the most destructive op a driver can expose: the device flashes itself
    and reboots, dropping the very control channel used to observe it, and a failed
    flash may need physical recovery. ``get_upgrade_status`` is a read-only probe;
    ``apply_firmware_update`` performs the flash and MUST route through
    ``safety.guard("firmware.flash", …)`` and default to ``dry_run=True``. The
    per-model reliability (risk, recovery-required) lives in the firmware registry
    ``profile.remote_update``, not here — see ``docs/firmware-update.md``.
    """

    def get_upgrade_status(self) -> dict: ...
    def apply_firmware_update(self, *, image: str | None = None, dry_run: bool = True) -> dict: ...


# Maps each capability to the protocol that defines it, so support can be
# detected structurally rather than declared by hand.
CAPABILITY_PROTOCOLS: dict[Capability, type] = {
    Capability.SYSTEM_INFO: SystemInfo,
    Capability.POWER: Power,
    Capability.HID: HID,
    Capability.VIDEO: Video,
    Capability.VIRTUAL_MEDIA: VirtualMedia,
    Capability.GPIO: GPIO,
    Capability.EVENTS: Events,
    Capability.LOGS: Logs,
    Capability.BOOT_PROGRESS: BootProgress,
    Capability.SENSORS: Sensors,
    Capability.SERIAL_CONSOLE: SerialConsole,
    Capability.WATCHDOG: Watchdog,
    Capability.FIRMWARE_UPDATE: FirmwareUpdate,
    Capability.SSH: RemoteShell,
}


def detect_capabilities(obj: object) -> set[Capability]:
    """Return the set of capabilities ``obj`` structurally satisfies."""
    return {cap for cap, proto in CAPABILITY_PROTOCOLS.items() if isinstance(obj, proto)}


class CapabilityMixin:
    """Gives a driver ``capabilities()`` / ``supports()`` for free.

    Both are derived structurally from the capability protocols above, so a
    driver never hand-maintains a list — it simply implements the methods for
    the capabilities its hardware has.
    """

    def capabilities(self) -> set[Capability]:
        return detect_capabilities(self)

    def supports(self, capability: Capability | str) -> bool:
        return Capability(capability) in self.capabilities()

    def close(self) -> None:
        """Release any device-side resources. No-op for stateless drivers.

        Overridden by drivers that hold server-side state — notably
        ``RedfishDriver``, whose BMC session must be DELETEd (BMCs cap
        concurrent sessions, so a leak locks operators out). Callers should
        ``close()`` every driver they build; the CLI and MCP server do.
        """

    def __enter__(self):
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class PowerMixin:
    """A default ``hard_cycle`` composed from the ``Power`` protocol methods.

    Every POWER driver needs the same ``power_off_hard`` → settle → ``power_on`` →
    settle composition; this keeps it in one place instead of a copy per driver.
    The settle delays are overridable class attributes: the PiKVM ATX path needs
    a pause (its power ops don't block on the state change), while Redfish blocks
    on the PowerState transition and Fake is instant, so both leave them 0.
    """

    _hard_cycle_off_delay: float = 0.0
    _hard_cycle_on_delay: float = 0.0

    def hard_cycle(self, off_delay: float | None = None, on_delay: float | None = None) -> None:
        off = self._hard_cycle_off_delay if off_delay is None else off_delay
        on = self._hard_cycle_on_delay if on_delay is None else on_delay
        logger.info("Hard power cycling %s", getattr(self, "host", "?"))
        self.power_off_hard()  # type: ignore[attr-defined]
        time.sleep(off)
        self.power_on()  # type: ignore[attr-defined]
        time.sleep(on)


@runtime_checkable
class KVMDriver(Protocol):
    """Anything that exposes a host and reports its capabilities."""

    host: str

    def capabilities(self) -> set[Capability]: ...
    def supports(self, capability: Capability | str) -> bool: ...


__all__ = [
    "Capability",
    "SystemInfo",
    "Power",
    "HID",
    "Video",
    "VirtualMedia",
    "GPIO",
    "Events",
    "Logs",
    "BootProgress",
    "Sensors",
    "SerialConsole",
    "Watchdog",
    "FirmwareUpdate",
    "RemoteShell",
    "KVMDriver",
    "CapabilityMixin",
    "PowerMixin",
    "CAPABILITY_PROTOCOLS",
    "detect_capabilities",
]
