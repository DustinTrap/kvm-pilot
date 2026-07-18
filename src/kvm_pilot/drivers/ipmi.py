"""IPMI 2.0 driver — the universal path for pre-Redfish BMCs (Dell iDRAC6,
older HPE iLO, Supermicro, generic OpenBMC).

Shells out to ``ipmitool -I lanplus`` (the ubiquitous, battle-tested client)
rather than reimplementing the RMCP+/RAKP handshake in Python — mirroring how the
SSH channel shells out to ``ssh``. The password is passed via ``ipmitool -E``
(read from the ``IPMI_PASSWORD`` environment variable) so it never appears in the
process argv / ``ps``.

Capabilities: Power (``chassis power``), SystemInfo (``chassis status`` / ``fru`` /
``mc info``), BootConfig (``chassis bootdev`` / ``bootparam``), Sensors (``sdr``),
Logs (SEL), SerialConsole (SOL — ``sol activate`` over a PTY, #208). Watchdog is
deferred. IPMI has no video, HID, or virtual media — those need the vendor's own
KVM/OEM channel.
"""

from __future__ import annotations

import os
import select
import shutil
import subprocess  # nosec B404 - fixed argv (no shell), password via env not argv
import time
from typing import TYPE_CHECKING, Any

from ..errors import CapabilityError, KVMPilotError, TimeoutError
from ..safety import SafetyPolicy
from .base import CapabilityMixin, PowerMixin

if TYPE_CHECKING:
    from ..config import HostConfig

# Normalized boot-device token -> ipmitool `chassis bootdev` selector. IPMI has no
# USB selector (usb boot is board-specific); reject it with a clear message.
_BOOTDEV_MAP = {
    "pxe": "pxe", "hdd": "disk", "disk": "disk", "cd": "cdrom", "dvd": "cdrom",
    "bios": "bios", "setup": "bios", "diag": "diag", "none": "none",
}
# FRU string fields are frequently unset or filled with vendor placeholders; treat
# these as "no value" so a junk field never masks a real one in a later fallback.
_FRU_PLACEHOLDERS = {
    "", "localhost", "unknown", "none", "n/a", "not specified", "not available",
    "default string", "to be filled by o.e.m.", "system product name",
    "system manufacturer", "system serial number", "0123456789",
}

# `bootparam get 5` "Boot Device Selector" phrase -> normalized token.
_BOOT_SELECTOR_REVERSE = [
    ("no override", "none"),
    ("pxe", "pxe"),
    ("hard-drive", "hdd"),
    ("hard drive", "hdd"),
    ("cd/dvd", "cd"),
    ("bios setup", "bios"),
    ("floppy", "usb"),
]


class IpmiDriver(PowerMixin, CapabilityMixin):
    """A BMC over IPMI 2.0 via ``ipmitool``."""

    def __init__(
        self,
        host: str,
        user: str = "ADMIN",
        passwd: str = "",
        *,
        port: int = 623,
        interface: str = "lanplus",
        cipher: int | None = None,
        ipmitool: str = "ipmitool",
        timeout: float = 30.0,
        dry_run: bool = False,
        confirm: Any = None,
    ):
        self.host = host
        self._user = user
        self._passwd = passwd
        self._port = port
        self._interface = interface
        self._cipher = cipher
        self._ipmitool = ipmitool
        self._timeout = timeout
        self.safety = SafetyPolicy(dry_run=dry_run, confirm=confirm)
        # Lazily-opened SOL session (ipmitool `sol activate` on a PTY); see SerialConsole.
        self._sol: subprocess.Popen | None = None
        self._sol_fd: int | None = None

    @classmethod
    def from_config(
        cls, cfg: HostConfig, *, confirm: Any = None, dry_run: bool = False
    ) -> IpmiDriver:
        """Build from a resolved :class:`~kvm_pilot.config.HostConfig` (like the
        Redfish/PiKVM drivers). Uses ``cfg.host``/``user``/``passwd`` and the
        ``ipmi_*`` fields (interface/port/cipher)."""
        return cls(
            cfg.host,
            cfg.user,
            cfg.passwd,
            port=getattr(cfg, "ipmi_port", 623),
            interface=getattr(cfg, "ipmi_interface", "lanplus"),
            cipher=getattr(cfg, "ipmi_cipher", None),
            timeout=cfg.timeout,
            dry_run=dry_run,
            confirm=confirm,
        )

    # -- ipmitool plumbing ----------------------------------------------

    def _base_argv(self) -> list[str]:
        argv = [self._ipmitool, "-I", self._interface, "-H", self.host, "-U", self._user, "-E"]
        if self._cipher is not None:
            argv += ["-C", str(self._cipher)]
        if self._port and self._port != 623:
            argv += ["-p", str(self._port)]
        return argv

    def _run(self, *args: str) -> str:
        """Run a read-only ``ipmitool`` subcommand and return stdout. Raises
        CapabilityError if ipmitool is missing, KVMPilotError on a nonzero exit."""
        if shutil.which(self._ipmitool) is None:
            raise CapabilityError(
                f"'{self._ipmitool}' was not found on PATH; the IPMI driver shells "
                "out to it (install ipmitool / OpenIPMI)."
            )
        argv = self._base_argv() + list(args)
        env = {**os.environ, "IPMI_PASSWORD": self._passwd}
        try:
            proc = subprocess.run(  # nosec B603 - fixed argv from config, shell=False
                argv, capture_output=True, text=True, timeout=self._timeout, env=env
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"ipmitool timed out after {self._timeout}s") from exc
        if proc.returncode != 0:
            raise KVMPilotError(
                f"ipmitool {' '.join(args)} failed (rc={proc.returncode}): "
                f"{proc.stderr.strip() or proc.stdout.strip()}"
            )
        return proc.stdout

    def _run_gated(self, op: str, desc: str, *args: str) -> str | None:
        """A state-changing ``ipmitool`` subcommand, guarded via SafetyPolicy.
        Returns None under dry-run (gated + skipped)."""
        if not self.safety.guard(op, desc):
            return None
        return self._run(*args)

    # -- SystemInfo -----------------------------------------------------

    def get_info(self, fields: list | None = None) -> dict:
        chassis = _kv(self._run("chassis", "status"))
        try:
            fru = _kv(self._run("fru", "print", "0"))
        except KVMPilotError:
            fru = {}
        try:
            mc = _kv(self._run("mc", "info"))
        except KVMPilotError:
            mc = {}

        def pick(*values: str | None) -> str | None:
            # First value that isn't blank or a well-known placeholder. Dell's iDRAC
            # sets FRU "Product Name" to the BMC hostname (e.g. "localhost"), so the
            # server model lives in "Board Product" — hence Board Product is tried
            # first for the model, and junk like localhost/"To be filled by O.E.M."
            # is skipped so it never masks a real value in a later field.
            for v in values:
                if v and v.strip().lower() not in _FRU_PLACEHOLDERS:
                    return v.strip()
            return None

        info: dict[str, Any] = {
            "manufacturer": pick(fru.get("Product Manufacturer"), fru.get("Board Mfg"),
                                 mc.get("Manufacturer Name")),
            # Board Product = the server model on Dell/HPE; Product Name is often the
            # iDRAC hostname ("localhost") — prefer the former, fall back to the latter.
            "model": pick(fru.get("Board Product"), fru.get("Product Name")),
            "serial_number": pick(fru.get("Product Serial"), fru.get("Board Serial")),
            "power_state": "on" if "on" in (chassis.get("System Power", "")).lower() else "off",
            "bmc_version": mc.get("Firmware Revision"),
            "bmc_manufacturer": mc.get("Manufacturer Name"),
        }
        if fields:
            info = {k: v for k, v in info.items() if k in fields}
        return info

    def get_firmware_info(self) -> dict:
        """Normalized firmware identity — the path the run ledger + firmware
        registry join on (a driver without it records identity as ``fake/fake``).
        Mirrors the Redfish/AMT shape: vendor/product/version + raw fields."""
        try:
            info = self.get_info()
        except KVMPilotError:
            info = {}
        return {
            "vendor": info.get("manufacturer"),
            "product": info.get("model"),
            "version": info.get("bmc_version"),
            "manufacturer": info.get("manufacturer"),
            "model": info.get("model"),
        }

    # -- Power ----------------------------------------------------------

    def is_powered_on(self) -> bool:
        return "on" in self._run("chassis", "power", "status").lower()

    def power_on(self, wait: bool = True) -> None:
        self._run_gated("ipmi.power_on", f"Power ON {self.host}", "chassis", "power", "on")

    def power_off(self, wait: bool = True) -> None:
        # 'soft' = ACPI graceful shutdown (the graceful analogue of PiKVM/Redfish).
        self._run_gated(
            "ipmi.power_off", f"Graceful power OFF {self.host}", "chassis", "power", "soft"
        )

    def power_off_hard(self, wait: bool = True) -> None:
        self._run_gated(
            "ipmi.power_off_hard", f"HARD power off {self.host} (data loss risk)",
            "chassis", "power", "off",
        )

    def reset_hard(self, wait: bool = True) -> None:
        self._run_gated(
            "ipmi.reset_hard", f"HARD reset {self.host} (data loss risk)",
            "chassis", "power", "reset",
        )

    # -- BootConfig -----------------------------------------------------

    def get_boot_options(self) -> dict:
        try:
            raw = self._run("chassis", "bootparam", "get", "5")
        except KVMPilotError:
            return {"enabled": "Unknown", "once": None, "persistent": None,
                    "target": None, "mode": None, "mode_settable": True,
                    "allowable": sorted({v for v in _BOOTDEV_MAP if v != "setup"})}
        low = raw.lower()
        target = "none"
        for phrase, tok in _BOOT_SELECTOR_REVERSE:
            if phrase in low:
                target = tok
                break
        persistent = "all future boots" in low or "persistent" in low
        once = not persistent
        mode = "UEFI" if "efi" in low else "Legacy"
        return {
            "enabled": "Continuous" if persistent else ("Once" if target != "none" else "Disabled"),
            "once": once if target != "none" else False,
            "persistent": persistent,
            "target": target,
            "mode": mode,
            "mode_settable": True,
            "allowable": sorted({v for v in _BOOTDEV_MAP if v != "setup"}),
        }

    def set_boot_device(self, device: str, *, once: bool = True, uefi: bool = True) -> dict:
        key = str(device).strip().lower()
        if key not in _BOOTDEV_MAP:
            raise KVMPilotError(
                f"unknown boot device {device!r}; IPMI supports "
                f"{sorted({v for v in _BOOTDEV_MAP if v != 'setup'})} "
                "(no 'usb' selector in IPMI)"
            )
        selector = _BOOTDEV_MAP[key]
        opts = []
        if uefi and selector != "none":
            opts.append("efiboot")
        if not once and selector != "none":
            opts.append("persistent")
        args = ["chassis", "bootdev", selector]
        if opts:
            args.append("options=" + ",".join(opts))
        desc = (f"Set next boot -> {key} ({'once' if once else 'persistent'}"
                f"{', UEFI' if uefi else ''}) on {self.host}")
        if self._run_gated("ipmi.set_boot_device", desc, *args) is None:
            return self.get_boot_options()  # dry-run
        return self.get_boot_options()

    # -- Sensors --------------------------------------------------------

    def read_sensors(self) -> dict:
        out = self._run("sdr", "elist")
        readings = []
        for line in out.splitlines():
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 5 and parts[0]:
                readings.append({
                    "name": parts[0], "status": parts[2],
                    "reading": parts[4], "raw": line.strip(),
                })
        return {"sensors": readings, "count": len(readings)}

    # -- Logs (SEL) -----------------------------------------------------

    def get_logs(self, seek: int = 0, follow: bool = False) -> str:
        if follow:
            raise CapabilityError("IPMI SEL has no tail-follow; call get_logs() without follow")
        # seek (seconds lookback) has no cheap IPMI analogue — SEL is returned whole;
        # callers filter by the timestamps in each entry if needed.
        return self._run("sel", "list")

    # -- SerialConsole (SOL) --------------------------------------------
    #
    # IPMI Serial-over-LAN relays the host's serial port as text (GRUB, dmesg,
    # a getty, kernel panics, a text installer). ipmitool's `sol activate` is an
    # interactive streaming command that drives a terminal, so we back the
    # serial_read/serial_write protocol with a persistent `sol activate` child on
    # a PTY: writes go to the PTY master (→ ipmitool stdin → the host console),
    # reads drain the master with a select() timeout. The session is opened
    # lazily on first use and gated once (opening it can inject keystrokes into
    # the running host — the same reason HID input is gated). SOL is a
    # single-session channel, so we free any stale session before activating.

    def _sol_activate(self) -> int | None:
        """Ensure a live SOL session and return its PTY master fd (None if the
        gate skipped it under dry-run)."""
        if self._sol is not None and self._sol.poll() is None:
            return self._sol_fd
        if shutil.which(self._ipmitool) is None:
            raise CapabilityError(
                f"'{self._ipmitool}' was not found on PATH; the IPMI driver shells "
                "out to it (install ipmitool / OpenIPMI)."
            )
        if not self.safety.guard("ipmi.serial_console", f"Open SOL serial console to {self.host}"):
            return None  # dry-run: gated + skipped
        # SOL is single-session; drop any stale one so activate doesn't bounce.
        try:
            self._run("sol", "deactivate")
        except KVMPilotError:
            pass
        import pty  # Unix-only; imported lazily so the module still imports elsewhere

        master, slave = pty.openpty()
        env = {**os.environ, "IPMI_PASSWORD": self._passwd}
        argv = self._base_argv() + ["sol", "activate"]
        self._sol = subprocess.Popen(  # nosec B603 - fixed argv from config, shell=False
            argv, stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True
        )
        os.close(slave)
        os.set_blocking(master, False)
        self._sol_fd = master
        return master

    def serial_read(self, timeout: float = 1.0) -> str:
        """Drain pending SOL console output as text, blocking up to ``timeout`` for
        the first byte. Returns '' if nothing arrives (or under dry-run)."""
        fd = self._sol_activate()
        if fd is None:
            return ""
        chunks: list[str] = []
        deadline = time.monotonic() + max(0.0, timeout)
        first = True
        while True:
            wait = max(0.0, deadline - time.monotonic()) if first else 0.0
            ready, _, _ = select.select([fd], [], [], wait)
            if not ready:
                break
            try:
                data = os.read(fd, 65536)
            except OSError:
                break  # EIO once the PTY/child is gone
            if not data:
                break
            chunks.append(data.decode("utf-8", "replace"))
            first = False
        return "".join(chunks)

    def serial_write(self, data: str) -> None:
        """Send text (keystrokes) to the host serial console. A trailing '\\r'
        is Enter. Gated on first activation."""
        fd = self._sol_activate()
        if fd is None:
            return
        os.write(fd, data.encode("utf-8"))

    def serial_interactive(self) -> int:
        """Attach an interactive SOL console to the CURRENT terminal and block
        until the user exits (ipmitool escape ``~.``). Returns ipmitool's exit
        code. This is the human-drives-an-install path (the `console` CLI); the
        serial_read/serial_write pair is the programmatic one. Gated."""
        if shutil.which(self._ipmitool) is None:
            raise CapabilityError(
                f"'{self._ipmitool}' was not found on PATH; install ipmitool / OpenIPMI."
            )
        if not self.safety.guard(
            "ipmi.serial_console", f"Open interactive SOL console to {self.host} (exit with ~.)"
        ):
            return 0  # dry-run
        try:
            self._run("sol", "deactivate")
        except KVMPilotError:
            pass
        env = {**os.environ, "IPMI_PASSWORD": self._passwd}
        argv = self._base_argv() + ["sol", "activate"]
        # stdio inherited => a real interactive console; ipmitool manages raw mode.
        proc = subprocess.run(argv, env=env)  # nosec B603 - fixed argv, shell=False
        return proc.returncode

    def serial_close(self) -> None:
        """Tear down the SOL session: send ipmitool's ``~.`` escape, stop the
        child, and free the BMC's single SOL channel. Safe when nothing's open."""
        fd, proc = self._sol_fd, self._sol
        self._sol_fd, self._sol = None, None
        if fd is not None:
            try:
                os.write(fd, b"~.")
            except OSError:
                pass
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:  # noqa: BLE001 - best-effort teardown
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            self._run("sol", "deactivate")
        except KVMPilotError:
            pass


def _kv(text: str) -> dict[str, str]:
    """Parse ``ipmitool`` 'Key : Value' output into a dict (last wins)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            k, v = k.strip(), v.strip()
            if k:
                out[k] = v
    return out
