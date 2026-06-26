"""
KVMClient — full PiKVM / GLKVM (GL-RM1 / GL-RM1PE) REST client.

Covers auth (incl. TOTP/2FA), keyboard + mouse HID, snapshots/OCR, ATX power,
Mass Storage Device (virtual media), GPIO, Redfish, WebSocket event streaming,
and system info/logs/metrics.

Destructive operations (power, reset, virtual-media connect/disconnect, GPIO,
Redfish resets) pass through a SafetyPolicy: dry-run skips them, and an
optional confirmation callback can veto them. See kvm_pilot.safety.

Compatibility note for GLKVM (GL.iNet fork): the PiKVM REST API is disabled by
default in GL firmware. Enable it by uncommenting the relevant block in
/etc/kvmd/nginx-kvmd.conf on the device (it may reset on firmware upgrade).
Until then every /api/* call returns 404.
"""

from __future__ import annotations

import base64
import json
import logging
import ssl
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any

from .drivers.base import CapabilityMixin
from .errors import AuthError, TimeoutError
from .http import HTTP
from .safety import SafetyPolicy

logger = logging.getLogger("kvm_pilot.client")


class KVMClient(CapabilityMixin):
    """Full PiKVM / GLKVM API client.

    Args:
        host: IP or hostname of the KVM device.
        user: Username (default "admin").
        passwd: Password (default "admin").
        port: HTTPS port (default 443).
        scheme: "https" or "http" (default "https").
        verify_ssl: Verify TLS cert (default False — GL/PiKVM ship self-signed).
        timeout: Default per-request timeout in seconds.
        totp_secret: Optional TOTP secret for 2FA-enabled devices. Requires the
            'totp' extra (pyotp).
        dry_run: If True, destructive operations are logged and skipped.
        confirm: Optional callback (op, description) -> bool gating destructive ops.
        max_retries: Bounded retries on transient errors (busy/unavailable/network).
    """

    def __init__(
        self,
        host: str,
        user: str = "admin",
        passwd: str = "admin",
        *,
        port: int = 443,
        scheme: str = "https",
        verify_ssl: bool = False,
        timeout: float = 30.0,
        totp_secret: str | None = None,
        dry_run: bool = False,
        confirm=None,
        max_retries: int = 3,
    ):
        self.host = host
        self._http = HTTP(
            host,
            user,
            passwd,
            verify_ssl=verify_ssl,
            timeout=timeout,
            port=port,
            scheme=scheme,
            totp_secret=totp_secret,
            max_retries=max_retries,
        )
        self.safety = SafetyPolicy(dry_run=dry_run, confirm=confirm)

    # -- auth ------------------------------------------------------------

    def login(self) -> str:
        """Obtain and store a session token (used for subsequent requests)."""
        return self._http.login()

    def check_auth(self) -> bool:
        try:
            self._http.get("/api/auth/check", retry=False)
            return True
        except AuthError:
            return False

    def logout(self) -> None:
        self._http.post("/api/auth/logout")
        self._http._auth_token = None

    # -- system info -----------------------------------------------------

    def get_info(self, fields: list | None = None) -> dict:
        params = {"fields": ",".join(fields)} if fields else None
        return self._http.get("/api/info", params=params)

    def get_logs(self, seek: int = 0, follow: bool = False) -> str:
        params: dict[str, Any] = {}
        if seek:
            params["seek"] = seek
        if follow:
            params["follow"] = "1"
        return self._http.get("/api/log", params=params or None, raw_response=True).decode()

    def get_metrics(self) -> str:
        return self._http.get(
            "/api/export/prometheus/metrics", raw_response=True
        ).decode()

    # -- streamer / snapshots -------------------------------------------

    def snapshot(self, quality: int = 85) -> bytes:
        return self._http.get(
            "/api/streamer/snapshot", params={"preview_quality": quality}, raw_response=True
        )

    def snapshot_save(self, path: str, quality: int = 85) -> Path:
        out = Path(path)
        out.write_bytes(self.snapshot(quality=quality))
        return out

    def snapshot_base64(self, quality: int = 85) -> str:
        return base64.b64encode(self.snapshot(quality=quality)).decode()

    def snapshot_ocr(
        self, lang: str = "eng", region: tuple[int, int, int, int] | None = None
    ) -> str:
        params: dict[str, Any] = {"ocr": "true", "ocr_langs": lang}
        if region:
            params["ocr_left"], params["ocr_top"], params["ocr_right"], params["ocr_bottom"] = region
        return self._http.get(
            "/api/streamer/snapshot", params=params, raw_response=True
        ).decode()

    def get_streamer_state(self) -> dict:
        return self._http.get("/api/streamer")

    # -- HID: keyboard ---------------------------------------------------

    def get_hid_state(self) -> dict:
        return self._http.get("/api/hid")

    def reset_hid(self) -> None:
        self._http.post("/api/hid/reset")

    def type_text(
        self, text: str, keymap: str = "en-us", slow: bool = False, delay: float = 0.0
    ) -> None:
        params: dict[str, Any] = {"keymap": keymap, "limit": 0}
        if slow:
            params["slow"] = "true"
            if delay:
                params["delay"] = delay
        self._http.post(
            "/api/hid/print", params=params, body=text.encode(), content_type="text/plain"
        )

    def send_shortcut(self, keys: str) -> None:
        self._http.post("/api/hid/events/send_shortcut", params={"keys": keys})

    def press_key(self, key: str, hold_ms: int = 50) -> None:
        self._http.post("/api/hid/events/send_key", params={"key": key, "state": "true"})
        time.sleep(hold_ms / 1000)
        self._http.post("/api/hid/events/send_key", params={"key": key, "state": "false"})

    def key_event(self, key: str, state: bool) -> None:
        self._http.post(
            "/api/hid/events/send_key",
            params={"key": key, "state": "true" if state else "false"},
        )

    def ctrl_alt_delete(self) -> None:
        if self.safety.guard("hid.ctrl_alt_delete", f"Send Ctrl+Alt+Del to {self.host}"):
            self.send_shortcut("ControlLeft,AltLeft,Delete")

    def ctrl_c(self) -> None:
        self.send_shortcut("ControlLeft,KeyC")

    def ctrl_z(self) -> None:
        self.send_shortcut("ControlLeft,KeyZ")

    def set_hid_params(
        self,
        keyboard_output: str | None = None,
        mouse_output: str | None = None,
        jiggler: bool | None = None,
    ) -> None:
        params: dict[str, Any] = {}
        if keyboard_output is not None:
            params["keyboard_output"] = keyboard_output
        if mouse_output is not None:
            params["mouse_output"] = mouse_output
        if jiggler is not None:
            params["jiggler"] = "true" if jiggler else "false"
        self._http.post("/api/hid/set_params", params=params)

    # -- HID: mouse ------------------------------------------------------

    def mouse_move(self, x: int, y: int) -> None:
        self._http.post("/api/hid/events/send_mouse_move", params={"to_x": x, "to_y": y})

    def mouse_move_rel(self, dx: int, dy: int) -> None:
        self._http.post(
            "/api/hid/events/send_mouse_relative", params={"delta_x": dx, "delta_y": dy}
        )

    def mouse_click(self, button: str = "left", hold_ms: int = 50, double: bool = False) -> None:
        for _ in range(2 if double else 1):
            self._http.post(
                "/api/hid/events/send_mouse_button", params={"button": button, "state": "true"}
            )
            time.sleep(hold_ms / 1000)
            self._http.post(
                "/api/hid/events/send_mouse_button", params={"button": button, "state": "false"}
            )
            if double:
                time.sleep(0.1)

    def mouse_scroll(self, delta_x: int = 0, delta_y: int = -3) -> None:
        self._http.post(
            "/api/hid/events/send_mouse_wheel", params={"delta_x": delta_x, "delta_y": delta_y}
        )

    # -- ATX power (gated) ----------------------------------------------

    def get_atx_state(self) -> dict:
        return self._http.get("/api/atx")

    def _atx_power(self, action: str, op: str, desc: str, wait: bool) -> None:
        if self.safety.guard(op, desc):
            self._http.post(
                "/api/atx/power", params={"action": action, "wait": "1" if wait else "0"}
            )

    def power_on(self, wait: bool = True) -> None:
        self._atx_power("on", "atx.power_on", f"Power ON {self.host}", wait)

    def power_off(self, wait: bool = True) -> None:
        self._atx_power("off", "atx.power_off", f"Graceful power OFF {self.host}", wait)

    def power_off_hard(self, wait: bool = True) -> None:
        self._atx_power(
            "off_hard", "atx.power_off_hard", f"HARD power off {self.host} (data loss risk)", wait
        )

    def reset_hard(self, wait: bool = True) -> None:
        self._atx_power(
            "reset_hard", "atx.reset_hard", f"HARD reset {self.host} (data loss risk)", wait
        )

    def atx_click(self, button: str = "power", wait: bool = True) -> None:
        if self.safety.guard("atx.click", f"ATX '{button}' click on {self.host}"):
            self._http.post(
                "/api/atx/click", params={"button": button, "wait": "1" if wait else "0"}
            )

    def is_powered_on(self) -> bool:
        return self.get_atx_state().get("leds", {}).get("power", False)

    # -- MSD / virtual media --------------------------------------------

    def get_msd_state(self) -> dict:
        return self._http.get("/api/msd")

    def msd_upload_file(
        self, local_path: str, image_name: str | None = None
    ) -> None:
        path = Path(local_path)
        name = image_name or path.name
        size = path.stat().st_size
        logger.info("Uploading %s (%.1f MB) to %s", name, size / 1024 / 1024, self.host)
        self._http.post(
            "/api/msd/write",
            params={"image": name},
            body=path.read_bytes(),
            content_type="application/octet-stream",
            long_timeout=600,
            retry=False,  # never retry a partial multi-MB upload
        )
        logger.info("Upload complete: %s", name)

    def msd_upload_url(
        self, url: str, image_name: str | None = None, timeout: int = 3600
    ) -> None:
        params: dict[str, Any] = {"url": url, "timeout": timeout}
        if image_name:
            params["image"] = image_name
        logger.info("Pulling ISO from %s (long-poll, do not interrupt)", url)
        self._http.post(
            "/api/msd/write_remote", params=params, long_timeout=timeout + 30, retry=False
        )
        logger.info("Remote ISO download complete")

    def msd_set_params(
        self, image: str | None = None, cdrom: bool = True, rw: bool = False
    ) -> None:
        params: dict[str, Any] = {"cdrom": "1" if cdrom else "0"}
        if image:
            params["image"] = image
        if not cdrom:
            params["rw"] = "1" if rw else "0"
        self._http.post("/api/msd/set_params", params=params)

    def msd_connect(self) -> None:
        if self.safety.guard("msd.connect", f"Attach virtual media to {self.host}"):
            self._http.post("/api/msd/set_connected", params={"connected": "1"})

    def msd_disconnect(self) -> None:
        if self.safety.guard("msd.disconnect", f"Detach virtual media from {self.host}"):
            self._http.post("/api/msd/set_connected", params={"connected": "0"})

    def msd_remove_image(self, image_name: str) -> None:
        if self.safety.guard("msd.remove_image", f"Delete image '{image_name}' from {self.host}"):
            self._http.post("/api/msd/remove", params={"image": image_name})

    def msd_reset(self) -> None:
        if self.safety.guard("msd.reset", f"Reset MSD on {self.host}"):
            self._http.post("/api/msd/reset")

    def mount_iso(
        self, source: str, image_name: str | None = None, cdrom: bool = True
    ) -> str:
        """Upload-or-pull an image, select it, and attach it. Returns image name."""
        if source.startswith(("http://", "https://")):
            name = image_name or source.split("/")[-1].split("?")[0]
            self.msd_upload_url(source, image_name=name)
        else:
            name = image_name or Path(source).name
            self.msd_upload_file(source, image_name=name)
        self.msd_set_params(image=name, cdrom=cdrom)
        self.msd_connect()
        logger.info("ISO mounted: %s (%s)", name, "CD-ROM" if cdrom else "USB flash")
        return name

    # -- GPIO ------------------------------------------------------------

    def get_gpio_state(self) -> dict:
        return self._http.get("/api/gpio")

    def gpio_switch(self, channel: str, state: bool, wait: bool = True) -> None:
        if self.safety.guard("gpio.switch", f"GPIO '{channel}' -> {'on' if state else 'off'}"):
            self._http.post(
                "/api/gpio/switch",
                params={"channel": channel, "state": "1" if state else "0", "wait": "1" if wait else "0"},
            )

    def gpio_pulse(self, channel: str, delay: float = 0.1, wait: bool = True) -> None:
        if self.safety.guard("gpio.pulse", f"GPIO '{channel}' pulse ({delay}s)"):
            self._http.post(
                "/api/gpio/pulse",
                params={"channel": channel, "delay": delay, "wait": "1" if wait else "0"},
            )

    # -- Redfish ---------------------------------------------------------

    def redfish_get_system(self) -> dict:
        return self._http.get("/api/redfish/v1/Systems/0")

    def redfish_power_action(self, action: str) -> None:
        if self.safety.guard("redfish.power_action", f"Redfish '{action}' on {self.host}"):
            self._http.request(
                "POST",
                "/api/redfish/v1/Systems/0/Actions/ComputerSystem.Reset",
                data=json.dumps({"ResetType": action}).encode(),
                content_type="application/json",
            )

    # -- WebSocket (optional dep) ---------------------------------------

    def watch_events(
        self, on_event=None, stream: bool = True, timeout: float | None = None
    ) -> Generator:
        try:
            import websocket  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "websocket-client is required for watch_events(). "
                "Install:  pip install 'kvm-pilot[ws]'"
            ) from exc

        wss = self._http._base.replace("https://", "wss://").replace("http://", "ws://")
        uri = f"{wss}/api/ws" + ("" if stream else "?stream=0")
        headers = {
            "X-KVMD-User": self._http._user,
            "X-KVMD-Passwd": self._http._effective_passwd(),
        }
        ws = websocket.WebSocket(sslopt={"cert_reqs": ssl.CERT_NONE})
        ws.connect(uri, header=headers)
        deadline = time.time() + timeout if timeout else None
        try:
            while True:
                if deadline and time.time() > deadline:
                    break
                ws.settimeout(1.0)
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if not raw:
                    break
                evt = json.loads(raw)
                if on_event:
                    on_event(evt.get("event_type"), evt.get("event", {}))
                yield evt
        finally:
            ws.close()

    # -- high-level helpers ---------------------------------------------

    def wait_for_power_state(self, target: bool, timeout: float = 60, poll: float = 2.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.is_powered_on() == target:
                return
            time.sleep(poll)
        raise TimeoutError(
            f"Timed out waiting for power={'on' if target else 'off'} after {timeout}s"
        )

    def hard_cycle(self, off_delay: float = 5.0, on_delay: float = 3.0) -> None:
        logger.info("Hard power cycling %s", self.host)
        self.power_off_hard()
        time.sleep(off_delay)
        self.power_on()
        time.sleep(on_delay)

    def send_password(self, passwd: str, keymap: str = "en-us") -> None:
        """Type a password + Enter slowly. Avoids logging the secret."""
        self.type_text(passwd + "\n", keymap=keymap, slow=True)

    def enter_bios(self, key: str = "F2", wait_s: float = 3.0) -> None:
        self.hard_cycle()
        time.sleep(wait_s)
        for _ in range(8):
            self.press_key(key, hold_ms=100)
            time.sleep(0.2)

    def __enter__(self) -> KVMClient:
        return self

    def __exit__(self, *_) -> None:
        pass


# Backwards-compatible alias for anyone porting from the old skill module.
PiKVMClient = KVMClient

__all__ = ["KVMClient", "PiKVMClient"]
