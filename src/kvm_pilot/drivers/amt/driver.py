"""Intel AMT / vPro driver — firmware-level out-of-band management.

AMT lives in the Management Engine, *below* the host OS, so it works when the OS
is down, hung, or still in firmware — and, unlike an HDMI-capture KVM, it can
see and drive BIOS / POST / the bootloader. This driver speaks AMT's three
native channels:

  * **WS-Man** (this file, via :mod:`.wsman`, port 16992/16993) — Power,
    BootConfig, SystemInfo.
  * **SOL** (this file, via the ``amtterm`` client, port 16994) — SerialConsole.
  * **KVM redirection / RFB** (:mod:`.rfb`, port 5900) — Video snapshot + HID.

Prerequisite: AMT must be *provisioned* (admin control mode) with the relevant
features enabled in MEBx. An un-provisioned or disabled ME answers nothing here
— the healthcheck surfaces that rather than hanging.

Capabilities are auto-detected from the methods present (``base.py``): this
file provides Power / SystemInfo / BootConfig / SerialConsole; Video + HID land
in :mod:`.rfb`.
"""

from __future__ import annotations

import os
import select
import shutil
import subprocess  # nosec B404 - fixed argv (no shell), AMT password via env not argv
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ...errors import CapabilityError, KVMPilotError
from ...safety import SafetyPolicy
from ..base import CapabilityMixin, PowerMixin
from .wsman import Wsman, WsmanError, amt, cim, escape, findtext

if TYPE_CHECKING:
    from ...config import HostConfig
    from .rfb import Rfb

# WS-Addressing / WS-Man URIs used when building Endpoint References (EPRs) and
# selector sets inside method-input bodies (the generic Wsman client stays
# class-agnostic, so the AMT-specific XML lives here).
_WSA = "http://schemas.xmlsoap.org/ws/2004/08/addressing"
_WSMAN = "http://schemas.dmtf.org/wbem/wsman/1/wsman.xsd"
_ANON = "http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous"

# CIM_PowerManagementService.RequestPowerStateChange PowerState codes. A soft
# request asks the OS to shut down (ACPI); a hard one cuts power immediately.
_POWER = {"on": 2, "off_soft": 8, "off_hard": 6, "reset": 10, "cycle_soft": 5}

# The service instance is selected by its four CIM keys.
_PWR_SVC_SEL = {
    "Name": "Intel(r) AMT Power Management Service",
    "SystemName": "Intel(r) AMT",
    "SystemCreationClassName": "CIM_ComputerSystem",
    "CreationClassName": "CIM_PowerManagementService",
}

# Normalized boot-device token -> CIM_BootSourceSetting InstanceID. AMT exposes
# only these forced sources; USB/diag have no AMT boot source, so they are
# rejected with a clear message (like the IPMI driver rejects 'usb'). 'bios' is
# special-cased to AMT_BootSettingData.BIOSSetup rather than a boot source.
_BOOT_SOURCE = {
    "pxe": "Intel(r) AMT: Force PXE Boot",
    "hdd": "Intel(r) AMT: Force Hard-drive Boot",
    "disk": "Intel(r) AMT: Force Hard-drive Boot",
    "cd": "Intel(r) AMT: Force CD/DVD Boot",
    "dvd": "Intel(r) AMT: Force CD/DVD Boot",
}
_BOOT_TOKENS = sorted(set(_BOOT_SOURCE) | {"bios", "none"})


class AmtDriver(PowerMixin, CapabilityMixin):
    """An Intel AMT / vPro platform over its native OOB channels."""

    def __init__(
        self,
        host: str,
        user: str = "admin",
        passwd: str = "",
        *,
        port: int = 16992,
        tls: bool = False,
        verify_ssl: bool = False,
        ssl_ca_file: str | None = None,
        sol_port: int = 16994,
        kvm_port: int = 5900,
        kvm_password: str | None = None,
        amtterm: str = "amtterm",
        timeout: float = 30.0,
        dry_run: bool = False,
        confirm: Any = None,
    ):
        self.host = host
        self._user = user
        self._passwd = passwd
        self._tls = tls
        self._sol_port = sol_port
        self._kvm_port = kvm_port
        # The KVM/RFB password is a separate MEBx credential; fall back to the
        # WS-Man admin password when it isn't configured separately.
        self._kvm_password = kvm_password if kvm_password is not None else passwd
        self._amtterm = amtterm
        self._timeout = timeout
        self.safety = SafetyPolicy(dry_run=dry_run, confirm=confirm)
        self._wsman = Wsman(
            host, user, passwd, port=port, tls=tls, verify_ssl=verify_ssl,
            ssl_ca_file=ssl_ca_file, timeout=timeout,
        )
        # Lazily-opened SOL session: an amtterm child on a PTY (see SerialConsole).
        self._sol: subprocess.Popen | None = None
        self._sol_fd: int | None = None
        # Persistent RFB session for HID so move-then-click share a connection
        # (Video snapshot uses its own short-lived session). Lazy; see .rfb.
        self._hid: Any = None

    @classmethod
    def from_config(cls, cfg: HostConfig, *, confirm: Any = None, dry_run: bool = False) -> AmtDriver:
        """Build from a resolved :class:`~kvm_pilot.config.HostConfig`.

        Uses ``host``/``user``/``passwd`` (shared with the other OOB drivers) and
        the ``amt_*`` fields (WS-Man port + TLS). TLS verification follows the
        shared ``verify_ssl`` / ``ssl_ca_file``.
        """
        return cls(
            cfg.host,
            cfg.user,
            cfg.passwd,
            port=getattr(cfg, "amt_port", 16992),
            tls=getattr(cfg, "amt_tls", False),
            kvm_password=getattr(cfg, "amt_kvm_password", None),
            verify_ssl=cfg.verify_ssl,
            ssl_ca_file=cfg.ssl_ca_file,
            timeout=cfg.timeout,
            dry_run=dry_run,
            confirm=confirm,
        )

    def close(self) -> None:
        """Tear down the SOL + RFB sessions if open (WS-Man itself is stateless)."""
        self.serial_close()
        hid = self._hid
        self._hid = None
        if hid is not None:
            try:
                hid.close()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass

    # -- SystemInfo -----------------------------------------------------

    def get_info(self, fields: list | None = None) -> dict:
        """Best-effort AMT identity: chassis make/model/serial, firmware version,
        provisioning state, power, and platform UUID. Each lookup is independent
        so a firmware that omits one field never blanks the rest."""
        chassis = self._safe(self._chassis) or {}
        info: dict[str, Any] = {
            "manufacturer": chassis.get("manufacturer"),
            "model": chassis.get("model"),
            "serial_number": chassis.get("serial_number"),
            "uuid": self._system_uuid(),
            "amt_version": self._amt_version(),
            "provisioning_state": self._provisioning_state(),
            "power_state": "on" if self._safe(self.is_powered_on) else "off",
        }
        if fields:
            info = {k: v for k, v in info.items() if k in fields}
        return info

    def _chassis(self) -> dict:
        for inst in self._wsman.enumerate(cim("CIM_Chassis")):
            return {
                "manufacturer": findtext(inst, "Manufacturer"),
                "model": findtext(inst, "Model"),
                "serial_number": findtext(inst, "SerialNumber"),
            }
        return {}

    def get_firmware_info(self) -> dict:
        """Firmware version for the healthcheck label (``amt@host#<ver>``)."""
        return {"version": self._amt_version()}

    def _amt_version(self) -> str | None:
        # AMT core version lives in the AMT_SetupAndConfigurationService or in a
        # CIM_SoftwareIdentity whose VersionString looks like "16.1.25".
        try:
            svc = self._wsman.get(amt("AMT_SetupAndConfigurationService"))
            for tag in ("CoreVersion", "VersionString", "Version"):
                v = findtext(svc, tag)
                if v:
                    return v.strip()
        except KVMPilotError:
            pass
        try:
            for inst in self._wsman.enumerate(cim("CIM_SoftwareIdentity")):
                v = findtext(inst, "VersionString")
                iid = (findtext(inst, "InstanceID") or "").lower()
                if v and ("amt" in iid or v[:2].isdigit()):
                    return v.strip()
        except KVMPilotError:
            pass
        return None

    def _provisioning_state(self) -> str | None:
        try:
            svc = self._wsman.get(amt("AMT_SetupAndConfigurationService"))
            state = findtext(svc, "ProvisioningState")
            return {"0": "pre", "1": "in", "2": "post"}.get((state or "").strip(), state)
        except KVMPilotError:
            return None

    def _system_uuid(self) -> str | None:
        try:
            for inst in self._wsman.enumerate(cim("CIM_ComputerSystemPackage")):
                guid = findtext(inst, "PlatformGUID")
                if guid:
                    return guid.strip()
        except KVMPilotError:
            pass
        return None

    @staticmethod
    def _safe(fn: Any) -> Any:
        try:
            return fn()
        except KVMPilotError:
            return None

    # -- Power ----------------------------------------------------------

    def is_powered_on(self) -> bool:
        """True when the platform's CIM PowerState is 2 (On)."""
        for inst in self._wsman.enumerate(cim("CIM_AssociatedPowerManagementService")):
            ps = findtext(inst, "PowerState")
            if ps is not None:
                return ps.strip() == str(_POWER["on"])
        raise WsmanError(f"AMT on {self.host} did not report a PowerState")

    def _request_power(self, state: int, op: str, desc: str) -> None:
        if not self.safety.guard(op, desc):
            return  # dry-run: gated + skipped
        body = (
            f'<p:RequestPowerStateChange_INPUT xmlns:p="{cim("CIM_PowerManagementService")}" '
            f'xmlns:wsa="{_WSA}" xmlns:wsman="{_WSMAN}">'
            f"<p:PowerState>{state}</p:PowerState>"
            "<p:ManagedElement>"
            f"<wsa:Address>{_ANON}</wsa:Address>"
            "<wsa:ReferenceParameters>"
            f'<wsman:ResourceURI>{cim("CIM_ComputerSystem")}</wsman:ResourceURI>'
            "<wsman:SelectorSet>"
            '<wsman:Selector Name="CreationClassName">CIM_ComputerSystem</wsman:Selector>'
            '<wsman:Selector Name="Name">ManagedSystem</wsman:Selector>'
            "</wsman:SelectorSet></wsa:ReferenceParameters></p:ManagedElement>"
            "</p:RequestPowerStateChange_INPUT>"
        )
        out = self._wsman.invoke(
            cim("CIM_PowerManagementService"), "RequestPowerStateChange", body, selectors=_PWR_SVC_SEL
        )
        rv = findtext(out, "ReturnValue")
        if rv not in (None, "0"):
            raise WsmanError(
                f"AMT RequestPowerStateChange({state}) on {self.host} returned {rv} "
                "(non-zero = the ME refused it; check AMT power-package support / provisioning)"
            )

    def power_on(self, wait: bool = True) -> None:
        self._request_power(_POWER["on"], "amt.power_on", f"Power ON {self.host} (AMT)")

    def power_off(self, wait: bool = True) -> None:
        self._request_power(
            _POWER["off_soft"], "amt.power_off", f"Graceful power OFF {self.host} (AMT, ACPI soft-off)"
        )

    def power_off_hard(self, wait: bool = True) -> None:
        self._request_power(
            _POWER["off_hard"], "amt.power_off_hard", f"HARD power off {self.host} (AMT, data-loss risk)"
        )

    def reset_hard(self, wait: bool = True) -> None:
        self._request_power(
            _POWER["reset"], "amt.reset_hard", f"HARD reset {self.host} (AMT master-bus reset)"
        )

    # -- BootConfig -----------------------------------------------------
    #
    # AMT's boot override is inherently *single-use*: the ME applies it on the
    # next boot, then clears it (SetBootConfigRole role 1 = IsNextSingleUse).
    # There is no persistent equivalent, so ``once=False`` is rejected rather
    # than silently downgraded. Flow: (1) reset AMT_BootSettingData flags (and
    # set BIOSSetup for 'bios'); (2) ChangeBootOrder to the chosen source (or
    # none); (3) SetBootConfigRole single-use.

    _BOOT_CFG = "Intel(r) AMT: Boot Configuration 0"

    def get_boot_options(self) -> dict:
        setting = self._safe(lambda: self._wsman.get(amt("AMT_BootSettingData")))
        bios_setup = (findtext(setting, "BIOSSetup") or "").lower() == "true" if setting is not None else None
        # AMT reports the pending source via CIM_BootConfigSetting/BootOrder; a
        # firmware that omits it just yields target=None (still a valid answer).
        target = self._pending_boot_target()
        return {
            "enabled": "Once" if (target and target != "none") or bios_setup else "Disabled",
            "once": True,  # AMT overrides are always single-use
            "persistent": False,
            "target": "bios" if bios_setup else (target or "none"),
            "mode": "UEFI",  # AMT boots the platform's native mode; not separately settable here
            "mode_settable": False,
            "allowable": _BOOT_TOKENS,
        }

    def _pending_boot_target(self) -> str | None:
        cfg = self._safe(lambda: self._wsman.get(cim("CIM_BootConfigSetting"), {"InstanceID": self._BOOT_CFG}))
        if cfg is None:
            return None
        for src, token in {v: k for k, v in _BOOT_SOURCE.items() if k not in ("disk", "dvd")}.items():
            if src in (findtext(cfg, "BootOrder") or ""):
                return token
        return "none"

    def set_boot_device(self, device: str, *, once: bool = True, uefi: bool = True) -> dict:
        key = str(device).strip().lower()
        if key not in _BOOT_TOKENS:
            raise KVMPilotError(
                f"unknown boot device {device!r}; AMT supports {_BOOT_TOKENS} "
                "(no 'usb'/'diag' boot source in AMT)"
            )
        if not once:
            raise CapabilityError(
                "AMT boot overrides are single-use only (the ME clears them after the "
                "next boot); persistent override is not available — omit --persistent."
            )
        desc = f"Set next boot -> {key} (AMT single-use) on {self.host}"
        if not self.safety.guard("amt.set_boot_device", desc):
            return self.get_boot_options()  # dry-run
        self._put_boot_setting_data(bios_setup=(key == "bios"))
        source_id = None if key in ("bios", "none") else _BOOT_SOURCE[key]
        self._change_boot_order(source_id)
        self._set_boot_config_role(single_use=True)
        return self.get_boot_options()

    def _put_boot_setting_data(self, *, bios_setup: bool) -> None:
        """Reset AMT_BootSettingData, setting BIOSSetup for the 'bios' target.

        Echoes the existing instance back with the boolean flags normalized —
        AMT rejects a partial Put, so we read-modify-write the whole element.
        """
        el = self._wsman.get(amt("AMT_BootSettingData"))
        # Re-serialize every child, overriding the boot-control booleans.
        overrides = {
            "BIOSSetup": "true" if bios_setup else "false",
            "BIOSPause": "false",
            "BootMediaIndex": "0",
            "UserPasswordBypass": "false",
        }
        parts = []
        seen = set()
        for child in list(el):
            name = child.tag.rsplit("}", 1)[-1]
            seen.add(name)
            val = overrides.get(name, child.text if child.text is not None else "")
            parts.append(f"<p:{name}>{escape(val)}</p:{name}>")
        for name, val in overrides.items():
            if name not in seen:
                parts.append(f"<p:{name}>{escape(val)}</p:{name}>")
        body = f'<p:AMT_BootSettingData xmlns:p="{amt("AMT_BootSettingData")}">{"".join(parts)}</p:AMT_BootSettingData>'
        self._wsman.put(amt("AMT_BootSettingData"), body)

    def _change_boot_order(self, source_instance_id: str | None) -> None:
        if source_instance_id is None:
            source_xml = ""  # empty Source clears the boot order
        else:
            source_xml = (
                "<p:Source>"
                f"<wsa:Address>{_ANON}</wsa:Address>"
                "<wsa:ReferenceParameters>"
                f'<wsman:ResourceURI>{cim("CIM_BootSourceSetting")}</wsman:ResourceURI>'
                "<wsman:SelectorSet>"
                f'<wsman:Selector Name="InstanceID">{escape(source_instance_id)}</wsman:Selector>'
                "</wsman:SelectorSet></wsa:ReferenceParameters></p:Source>"
            )
        body = (
            f'<p:ChangeBootOrder_INPUT xmlns:p="{cim("CIM_BootConfigSetting")}" '
            f'xmlns:wsa="{_WSA}" xmlns:wsman="{_WSMAN}">{source_xml}</p:ChangeBootOrder_INPUT>'
        )
        out = self._wsman.invoke(
            cim("CIM_BootConfigSetting"), "ChangeBootOrder", body, selectors={"InstanceID": self._BOOT_CFG}
        )
        rv = findtext(out, "ReturnValue")
        if rv not in (None, "0"):
            raise WsmanError(f"AMT ChangeBootOrder on {self.host} returned {rv}")

    def _set_boot_config_role(self, *, single_use: bool) -> None:
        # CIM_BootService.SetBootConfigRole, Role 1 = IsNextSingleUse.
        body = (
            f'<p:SetBootConfigRole_INPUT xmlns:p="{cim("CIM_BootService")}" '
            f'xmlns:wsa="{_WSA}" xmlns:wsman="{_WSMAN}">'
            "<p:BootConfigSetting>"
            f"<wsa:Address>{_ANON}</wsa:Address>"
            "<wsa:ReferenceParameters>"
            f'<wsman:ResourceURI>{cim("CIM_BootConfigSetting")}</wsman:ResourceURI>'
            "<wsman:SelectorSet>"
            f'<wsman:Selector Name="InstanceID">{escape(self._BOOT_CFG)}</wsman:Selector>'
            "</wsman:SelectorSet></wsa:ReferenceParameters></p:BootConfigSetting>"
            f"<p:Role>{1 if single_use else 2}</p:Role>"
            "</p:SetBootConfigRole_INPUT>"
        )
        out = self._wsman.invoke(
            cim("CIM_BootService"), "SetBootConfigRole", body,
            selectors={"Name": "Intel(r) AMT Boot Service", "SystemName": "Intel(r) AMT",
                       "SystemCreationClassName": "CIM_ComputerSystem", "CreationClassName": "CIM_BootService"},
        )
        rv = findtext(out, "ReturnValue")
        if rv not in (None, "0"):
            raise WsmanError(f"AMT SetBootConfigRole on {self.host} returned {rv}")

    # -- SerialConsole (SOL via amtterm) --------------------------------
    #
    # AMT Serial-over-LAN relays the host's serial console as text — BIOS/GRUB
    # (when the platform's serial redirect is on), dmesg, a getty, kernel panics.
    # Rather than hand-roll AMT's binary redirection+auth handshake (which we
    # can't yet live-validate), this shells out to ``amtterm`` — the battle-tested
    # SOL client — exactly as the IPMI driver shells out to ``ipmitool sol
    # activate``. The password rides ``AMT_PASSWORD`` in the env, never argv/ps.
    # ``serial_read``/``serial_write`` drive a persistent amtterm child on a PTY;
    # the session opens lazily and is gated once (SOL can inject keystrokes into a
    # live host — the same reason HID is gated). SOL is single-session on the ME.

    def _sol_argv(self) -> list[str]:
        return [self._amtterm, self.host, str(self._sol_port)]

    def _sol_activate(self) -> int | None:
        """Ensure a live amtterm SOL session; return its PTY master fd (None if the
        gate skipped it under dry-run)."""
        if self._sol is not None and self._sol.poll() is None:
            return self._sol_fd
        if shutil.which(self._amtterm) is None:
            raise CapabilityError(
                f"'{self._amtterm}' was not found on PATH; the AMT driver shells out to it "
                "for SOL (install the 'amtterm' package)."
            )
        if not self.safety.guard("amt.serial_console", f"Open SOL serial console to {self.host} (AMT)"):
            return None  # dry-run: gated + skipped
        import pty  # Unix-only; imported lazily so the module still imports elsewhere

        master, slave = pty.openpty()
        env = {**os.environ, "AMT_PASSWORD": self._passwd}
        self._sol = subprocess.Popen(  # nosec B603 - fixed argv from config, shell=False
            self._sol_argv(), stdin=slave, stdout=slave, stderr=slave, env=env, close_fds=True
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
        """Send text (keystrokes) to the host serial console. A trailing '\\r' is
        Enter. Gated on first activation."""
        fd = self._sol_activate()
        if fd is None:
            return
        os.write(fd, data.encode("utf-8"))

    def serial_interactive(self) -> int:
        """Attach an interactive amtterm SOL console to the CURRENT terminal and
        block until the user exits. Returns amtterm's exit code — the
        human-drives-an-install path (the ``console`` CLI); serial_read/write is
        the programmatic pair. Gated."""
        if shutil.which(self._amtterm) is None:
            raise CapabilityError(
                f"'{self._amtterm}' was not found on PATH; install the 'amtterm' package."
            )
        if not self.safety.guard(
            "amt.serial_console", f"Open interactive SOL console to {self.host} (AMT)"
        ):
            return 0  # dry-run
        env = {**os.environ, "AMT_PASSWORD": self._passwd}
        # stdio inherited => a real interactive console; amtterm manages the terminal.
        proc = subprocess.run(self._sol_argv(), env=env)  # nosec B603 - fixed argv, shell=False
        return proc.returncode

    def serial_close(self) -> None:
        """Tear down the SOL session: stop the amtterm child and free the ME's
        single SOL channel. Safe when nothing is open."""
        fd, proc = self._sol_fd, self._sol
        self._sol_fd, self._sol = None, None
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

    # -- Video + HID (KVM redirection / RFB) ----------------------------
    #
    # Delegates to :mod:`.rfb` (a stdlib RFB/VNC client on port 5900). snapshot()
    # captures the ME's framebuffer — BIOS/POST/GRUB included, the whole point —
    # over a short-lived session; HID reuses one persistent session so a
    # move-then-click land on the same connection. Uses the KVM/RFB password.

    def _rfb_snapshot_session(self) -> Rfb:
        from .rfb import Rfb

        return Rfb(self.host, self._kvm_port, self._kvm_password, timeout=self._timeout)

    def _hid_session(self) -> Rfb:
        from .rfb import Rfb

        if self._hid is None or getattr(self._hid, "_sock", None) is None:
            self._hid = Rfb(self.host, self._kvm_port, self._kvm_password, timeout=self._timeout)
            self._hid.connect()
        return self._hid

    def snapshot(self) -> bytes:
        """A PNG of the platform framebuffer — BIOS/POST/GRUB included (the reason
        AMT matters here). Needs KVM redirection + standard-port 5900 in MEBx."""
        with self._rfb_snapshot_session() as r:
            return r.framebuffer_png()

    def snapshot_base64(self) -> str:
        import base64

        return base64.b64encode(self.snapshot()).decode("ascii")

    def snapshot_save(self, path: str) -> Path:
        p = Path(path)
        p.write_bytes(self.snapshot())
        return p

    def type_text(self, text: str) -> None:
        if not self.safety.guard("hid.type_text", f"Type {len(text)} chars into {self.host} (AMT RFB)"):
            return
        from .rfb import key_to_keysym

        r = self._hid_session()
        for ch in text:
            r.tap(key_to_keysym(ch))

    def press_key(self, key: str) -> None:
        if not self.safety.guard("hid.press_key", f"Press {key!r} on {self.host} (AMT RFB)"):
            return
        from .rfb import key_to_keysym

        self._hid_session().tap(key_to_keysym(key))

    def send_shortcut(self, keys: str) -> None:
        parts = [k for k in keys.replace("+", ",").split(",") if k.strip()]
        if not self.safety.guard("hid.send_shortcut", f"Send {keys!r} to {self.host} (AMT RFB)"):
            return
        from .rfb import key_to_keysym

        syms = [key_to_keysym(k) for k in parts]
        r = self._hid_session()
        for s in syms:
            r.key(s, True)
        for s in reversed(syms):
            r.key(s, False)

    def mouse_move(self, x: int, y: int) -> None:
        # Mouse *moves* are not gated (matches the HID protocol — only keys/clicks).
        self._hid_session().pointer(int(x), int(y))

    def mouse_click(self, button: str = "left") -> None:
        if not self.safety.guard("hid.mouse_click", f"{button}-click on {self.host} (AMT RFB)"):
            return
        b = {"left": 1, "middle": 2, "right": 3}.get(button, 1)
        self._hid_session().click(b)

