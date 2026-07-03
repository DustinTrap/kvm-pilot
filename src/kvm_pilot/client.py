"""
KVMClient — full PiKVM / GLKVM (GL-RM1 / GL-RM1PE) REST client.

Covers auth (incl. TOTP/2FA), keyboard + mouse HID, snapshots/OCR, ATX power,
Mass Storage Device (virtual media), GPIO, Redfish, WebSocket event streaming,
and system info/logs/metrics.

Destructive operations (power, reset, virtual-media writes/attach, GPIO, HID
keystrokes/clicks, Redfish resets) pass through a SafetyPolicy: dry-run skips
them, and an optional confirmation callback can veto them. See kvm_pilot.safety.

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
from typing import TYPE_CHECKING, Any

from .drivers.base import CapabilityMixin, PowerMixin
from .errors import (
    ApiDisabledError,
    AuthError,
    CapabilityError,
    KVMPilotError,
    TimeoutError,
)
from .http import HTTP
from .safety import SafetyPolicy

if TYPE_CHECKING:
    from .config import HostConfig

logger = logging.getLogger("kvm_pilot.client")


def _pixel_to_kvmd(v: int, extent: int) -> int:
    """Map pixel 0..extent-1 edge-exactly onto kvmd's -32768..32767 axis."""
    if extent <= 1:
        return 0
    return max(-32768, min(32767, round(-32768 + v * 65535 / (extent - 1))))


class PiKVMDriver(PowerMixin, CapabilityMixin):
    """Full PiKVM-family REST driver (canonical base of the PiKVM/GLKVM/BliKVM family).

    This is the concrete client for stock PiKVM and any API-compatible device.
    ``GLKVMDriver`` / ``BliKVMDriver`` (in ``kvm_pilot.drivers.pikvm``) subclass it
    and override only the deltas. ``KVMClient`` and ``PiKVMClient`` are kept as
    back-compatible aliases of this class.

    Args:
        host: IP or hostname of the KVM device.
        user: Username (default "admin").
        passwd: Password (default "admin").
        port: HTTPS port (default 443).
        scheme: "https" or "http" (default "https").
        verify_ssl: Verify TLS cert (default False — GL/PiKVM ship self-signed;
            the first unverified transport per process logs a warning).
        ssl_ca_file: Pin verification to a CA bundle or the device's own
            self-signed cert (PEM). Overrides verify_ssl.
        timeout: Default per-request timeout in seconds.
        totp_secret: Optional TOTP secret for 2FA-enabled devices. Requires the
            'totp' extra (pyotp).
        dry_run: If True, destructive operations are logged and skipped.
        confirm: Optional callback (op, description) -> bool gating destructive ops.
        max_retries: Bounded retries on transient errors (busy/unavailable/network).
    """

    # Subclasses (GLKVMDriver) set this to make a 404 across /api/* surface as a
    # clear ApiDisabledError with device-specific guidance. None for stock PiKVM.
    _NOT_FOUND_HINT: str | None = None

    # Vendor identity for the firmware registry; subclasses override (GL/BliKVM).
    _vendor: str = "pikvm"

    # ATX power ops don't block on the state change, so hard_cycle (from
    # PowerMixin) settles between the off and on. Overridable per call.
    _hard_cycle_off_delay: float = 5.0
    _hard_cycle_on_delay: float = 3.0

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
        ssl_ca_file: str | None = None,
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
            not_found_hint=self._NOT_FOUND_HINT,
            ssl_ca_file=ssl_ca_file,
        )
        self.safety = SafetyPolicy(dry_run=dry_run, confirm=confirm)

    @classmethod
    def from_config(
        cls,
        cfg: HostConfig,
        *,
        confirm=None,
        dry_run: bool = False,
        max_retries: int = 3,
    ) -> PiKVMDriver:
        """Build a driver from a resolved :class:`~kvm_pilot.config.HostConfig`.

        Centralizes the field-by-field construction the CLI, MCP server, and
        examples would otherwise each repeat (and keeps ``scheme``/``timeout``
        from silently drifting between call sites). Subclasses build their own
        type (``cls``), so ``GLKVMDriver.from_config(cfg)`` returns a GLKVMDriver.
        """
        return cls(
            cfg.host,
            cfg.user,
            cfg.passwd,
            port=cfg.port,
            scheme=cfg.scheme,
            verify_ssl=cfg.verify_ssl,
            timeout=cfg.timeout,
            totp_secret=cfg.totp_secret,
            dry_run=dry_run,
            confirm=confirm,
            max_retries=max_retries,
            ssl_ca_file=cfg.ssl_ca_file,
        )

    # -- firmware / preflight -------------------------------------------

    def get_firmware_info(self) -> dict:
        """Best-effort firmware/version snapshot from ``/api/info``.

        Normalizes the kvmd version, platform, and model so callers (and the
        per-firmware quirk registry) can reason about the running release. Shapes
        vary across PiKVM/GLKVM firmware, so every field is read defensively.
        """
        info = self.get_info()

        def _sub(d: object, key: str) -> dict:
            val = d.get(key) if isinstance(d, dict) else None
            return val if isinstance(val, dict) else {}

        system = _sub(info, "system")
        kvmd = _sub(system, "kvmd")
        # GLKVM exposes platform under system.platform; stock PiKVM under hw.platform.
        platform = _sub(system, "platform") or _sub(_sub(info, "hw"), "platform")
        version = kvmd.get("version")
        product = platform.get("base") or platform.get("type")
        return {
            "version": version,
            "kvmd_version": version,
            "platform": product,
            "model": platform.get("model"),
            # Normalized identity for the firmware registry (health.check_firmware_currency).
            # The comparable version on the PiKVM family is kvmd's; a device does not
            # report its GL/vendor product-firmware version, so kvmd is the currency proxy.
            "vendor": self._vendor,
            "product": product,
        }

    def check_api_enabled(self) -> dict:
        """Preflight: confirm the PiKVM REST API is reachable, else raise clearly.

        On GL.iNet (GLKVM) firmware the API is disabled by default and every
        ``/api/*`` returns 404; this turns that bare 404 into an actionable
        :class:`~kvm_pilot.errors.ApiDisabledError`. Returns ``/api/info`` on success.
        """
        try:
            return self.get_info()
        except ApiDisabledError:
            raise
        except KVMPilotError as exc:
            if exc.status_code == 404:
                raise ApiDisabledError(
                    "The PiKVM REST API returned 404. On GL.iNet (GLKVM) firmware "
                    "the API is disabled by default — enable it in "
                    "/etc/kvmd/nginx-kvmd.conf and restart kvmd (it can revert on "
                    "a firmware upgrade).",
                    404,
                ) from exc
            raise

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
        # seek is seconds of lookback (kvmd's /api/log?seek=N) — the cross-driver
        # Logs contract; the Redfish driver matches it. See drivers.base.Logs.
        if follow:
            # kvmd streams /api/log?follow=1 forever (tail -f). The blocking
            # transport buffers the whole response, so follow would just block to
            # the timeout and raise — refuse cleanly instead (mirrors
            # RedfishDriver.get_logs). A streaming generator can land later behind
            # a dedicated HTTP.stream() entry point.
            raise CapabilityError(
                "PiKVM log tail-follow is not supported over the blocking "
                "transport; call get_logs() without follow"
            )
        params: dict[str, Any] = {}
        if seek:
            params["seek"] = seek
        return self._http.get("/api/log", params=params or None, raw_response=True).decode()

    def get_metrics(self) -> str:
        return self._http.get(
            "/api/export/prometheus/metrics", raw_response=True
        ).decode()

    # -- streamer / snapshots -------------------------------------------

    def snapshot(self) -> bytes:
        """Return the current screen as a full-resolution JPEG.

        No quality knob: kvmd's ``preview_quality`` applies only to its
        downscaled preview (which would break OCR/vision), and the full-size
        snapshot has no re-encode-at-quality path.
        """
        return self._http.get("/api/streamer/snapshot", raw_response=True)

    def snapshot_save(self, path: str) -> Path:
        out = Path(path)
        out.write_bytes(self.snapshot())
        return out

    def snapshot_base64(self) -> str:
        return base64.b64encode(self.snapshot()).decode()

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

    def has_video_signal(self) -> bool:
        """True if the capture pipeline reports a live video source.

        Parses ``/api/streamer`` defensively: it returns ``False`` only when the
        device *positively* reports the source offline, so a missing or unknown
        field never suppresses a real frame. Lets the vision layer skip a model
        call when there is simply nothing on screen (powered off, between mode
        sets, sleeping).
        """
        try:
            state = self.get_streamer_state()
        except Exception:  # noqa: BLE001 - a liveness probe must never raise
            return True
        source = state.get("source") or {}
        if not source and isinstance(state.get("streamer"), dict):
            source = state["streamer"].get("source") or {}
        online = source.get("online") if isinstance(source, dict) else None
        return True if online is None else bool(online)

    # -- HID: keyboard ---------------------------------------------------

    def get_hid_state(self) -> dict:
        return self._http.get("/api/hid")

    def reset_hid(self) -> None:
        self._http.post("/api/hid/reset")

    def type_text(
        self, text: str, keymap: str = "en-us", slow: bool = False, delay: float = 0.0
    ) -> None:
        # The description gives only the length: the text may be a password
        # (send_password routes through here) and guard descriptions get logged.
        if not self.safety.guard(
            "hid.type_text", f"Type {len(text)} characters into {self.host}"
        ):
            return
        params: dict[str, Any] = {"keymap": keymap, "limit": 0}
        if slow:
            params["slow"] = "true"
            if delay:
                params["delay"] = delay
        self._http.post(
            "/api/hid/print", params=params, body=text.encode(), content_type="text/plain"
        )

    def send_shortcut(self, keys: str) -> None:
        if self.safety.guard("hid.send_shortcut", f"Send shortcut {keys!r} to {self.host}"):
            self._send_shortcut(keys)

    def _send_shortcut(self, keys: str) -> None:
        self._http.post("/api/hid/events/send_shortcut", params={"keys": keys})

    def press_key(self, key: str, hold_ms: int = 50) -> None:
        if not self.safety.guard("hid.press_key", f"Press {key!r} on {self.host}"):
            return
        self._http.post("/api/hid/events/send_key", params={"key": key, "state": "true"})
        try:
            time.sleep(hold_ms / 1000)
        finally:
            self._release_or_reset(
                "/api/hid/events/send_key", {"key": key, "state": "false"}, f"key {key!r}"
            )

    def _release_or_reset(self, path: str, params: dict[str, Any], what: str) -> None:
        """Send an up-event; if that fails, reset HID so ``what`` can't stay held down."""
        try:
            self._http.post(path, params=params)
        except Exception as exc:  # noqa: BLE001 - always attempt the reset fallback
            logger.warning("Releasing %s on %s failed (%s); resetting HID", what, self.host, exc)
            try:
                self.reset_hid()
            except Exception:  # noqa: BLE001 - surface the original failure below
                logger.error("HID reset failed too — %s may be stuck down on %s", what, self.host)
            raise

    def key_event(self, key: str, state: bool) -> None:
        if not self.safety.guard(
            "hid.key_event", f"Key event {key!r} {'down' if state else 'up'} on {self.host}"
        ):
            return
        self._http.post(
            "/api/hid/events/send_key",
            params={"key": key, "state": "true" if state else "false"},
        )

    def ctrl_alt_delete(self) -> None:
        if self.safety.guard("hid.ctrl_alt_delete", f"Send Ctrl+Alt+Del to {self.host}"):
            self._send_shortcut("ControlLeft,AltLeft,Delete")

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
        """Absolute mouse move in kvmd's fixed coordinate space — NOT pixels.

        kvmd expects -32768..32767 on each axis with (0, 0) at the screen
        CENTER, and only honors this in absolute mouse mode (use
        :meth:`mouse_move_rel` for relative mode). For pixel coordinates use
        :meth:`mouse_move_pixels`. See https://docs.pikvm.org/mouse/.
        """
        self._http.post("/api/hid/events/send_mouse_move", params={"to_x": x, "to_y": y})

    def mouse_move_pixels(
        self, x: int, y: int, width: int | None = None, height: int | None = None
    ) -> None:
        """Move to pixel ``(x, y)``, mapped into kvmd's centered -32768..32767 space.

        ``width``/``height`` default to the streamer's current source resolution.
        """
        if width is None or height is None:
            state = self.get_streamer_state()
            source = state.get("source") or {}
            if not source and isinstance(state.get("streamer"), dict):
                source = state["streamer"].get("source") or {}
            res = source.get("resolution") if isinstance(source, dict) else None
            res = res if isinstance(res, dict) else {}
            width = width or res.get("width")
            height = height or res.get("height")
            if not width or not height:
                raise KVMPilotError(
                    "Could not read the screen resolution from /api/streamer; "
                    "pass width= and height= explicitly"
                )
        self.mouse_move(_pixel_to_kvmd(x, width), _pixel_to_kvmd(y, height))

    def mouse_move_rel(self, dx: int, dy: int) -> None:
        self._http.post(
            "/api/hid/events/send_mouse_relative", params={"delta_x": dx, "delta_y": dy}
        )

    def mouse_click(self, button: str = "left", hold_ms: int = 50, double: bool = False) -> None:
        if not self.safety.guard(
            "hid.mouse_click",
            f"Mouse {button} {'double-click' if double else 'click'} on {self.host}",
        ):
            return
        for _ in range(2 if double else 1):
            self._http.post(
                "/api/hid/events/send_mouse_button", params={"button": button, "state": "true"}
            )
            try:
                time.sleep(hold_ms / 1000)
            finally:
                self._release_or_reset(
                    "/api/hid/events/send_mouse_button",
                    {"button": button, "state": "false"},
                    f"mouse button {button!r}",
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
        """True unless the device *positively* reports host power off.

        Fail-open like ``has_video_signal``: with no ATX board wired the kvmd
        ATX plugin reports ``enabled: false`` and its LEDs are meaningless, so
        returning False there would make the vision layer report ``power_off``
        for a machine that is actually running. See docs/decisions.md.
        """
        atx = self.get_atx_state()
        if not atx.get("enabled", True):
            return True
        return atx.get("leds", {}).get("power", False)

    # -- MSD / virtual media --------------------------------------------

    def get_msd_state(self) -> dict:
        return self._http.get("/api/msd")

    def msd_upload_file(
        self, local_path: str, image_name: str | None = None
    ) -> None:
        path = Path(local_path)
        name = image_name or path.name
        # Guarded before any device (or even local file) I/O so --dry-run and a
        # confirm veto really mean "nothing was uploaded".
        if not self.safety.guard("msd.write", f"Upload image '{name}' to {self.host}"):
            return
        size = path.stat().st_size
        logger.info("Uploading %s (%.1f MB) to %s", name, size / 1024 / 1024, self.host)
        # Stream the file rather than read it all into RAM — boot ISOs are
        # multi-GB and would OOM a small jump host/container. urllib streams a
        # file object in 8 KiB blocks once Content-Length is pinned. retry=False
        # (and the transport enforces it for file bodies): a consumed stream
        # can't be resent.
        with path.open("rb") as fh:
            self._http.post(
                "/api/msd/write",
                params={"image": name},
                body=fh,
                content_type="application/octet-stream",
                extra_headers={"Content-Length": str(size)},
                long_timeout=600,
                retry=False,
            )
        logger.info("Upload complete: %s", name)

    def msd_upload_url(
        self, url: str, image_name: str | None = None, timeout: int = 3600
    ) -> None:
        if not self.safety.guard(
            "msd.write_remote", f"Pull image from {url} onto {self.host}"
        ):
            return
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
        if not self.safety.guard(
            "msd.set_params",
            f"Select MSD image {image!r} on {self.host} (cdrom={cdrom}, rw={rw})",
        ):
            return
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
        # Honor the client's TLS choice on this credential-bearing channel
        # instead of always disabling verification; a pinned CA file wins.
        verify_ws = self._http._verify_ssl or bool(self._http._ssl_ca_file)
        sslopt: dict = {"cert_reqs": ssl.CERT_REQUIRED if verify_ws else ssl.CERT_NONE}
        if self._http._ssl_ca_file:
            sslopt["ca_certs"] = self._http._ssl_ca_file
        if not verify_ws:
            sslopt["check_hostname"] = False
        ws = websocket.WebSocket(sslopt=sslopt)
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

    def send_password(self, passwd: str, keymap: str = "en-us") -> None:
        """Type a password + Enter slowly. Avoids logging the secret."""
        self.type_text(passwd + "\n", keymap=keymap, slow=True)

    def enter_bios(self, key: str = "F2", wait_s: float = 3.0) -> None:
        self.hard_cycle()
        time.sleep(wait_s)
        for _ in range(8):
            self.press_key(key, hold_ms=100)
            time.sleep(0.2)


# ``KVMClient`` is the long-standing public name and stays as the canonical alias
# of ``PiKVMDriver``; ``PiKVMClient`` is the older skill-module alias. All three
# are the same class.
KVMClient = PiKVMDriver
PiKVMClient = PiKVMDriver

__all__ = ["PiKVMDriver", "KVMClient", "PiKVMClient"]
