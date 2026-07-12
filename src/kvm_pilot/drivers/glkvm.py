"""
GL.iNet GLKVM driver — the GL fork of PiKVM (GL-RM1 "Comet", GL-RM1PE "Comet PE").

**Read this before assuming stock-PiKVM behavior.** GLKVM speaks the kvmd REST
API (``PiKVMDriver`` in :mod:`kvm_pilot.client` is the base), but the fork
diverges in ways that have repeatedly confused operators and agents:

  * **It looks like a Raspberry Pi PiKVM from ``/api/info``** — the firmware
    self-reports ``type: rpi, board: rpi4, model: v3`` even on Rockchip RV1126
    hardware, so nothing in the base API reveals it is a GL device. If a
    profile says ``driver = "pikvm"`` for a GL unit, none of the GL handling
    below applies (wrong-driver detection is tracked in the healthcheck).
  * **The REST API ships disabled** — every ``/api/*`` 404s until it is
    enabled in ``/etc/kvmd/nginx-kvmd.conf``. This driver turns that 404 into
    an actionable ``ApiDisabledError`` (see ``_GL_API_DISABLED_HINT``).
  * **Two version numbers.** The GL *product* firmware ("V1.9.1 release1", what
    the UI shows) lives at the proprietary ``/api/upgrade/version``; the kvmd
    component version is separate. ``get_firmware_info`` reports the product
    version as ``version`` and keeps ``kvmd_version`` alongside; quirks match
    against **both** (#139).
  * **Proprietary remote-flash layer** (``/api/upgrade/*``) — upstream PiKVM
    has no OS-update API. Request bodies are reverse-engineered/provisional
    (#95), and a ``start`` that returns 200 can no-op (#94), so the flash path
    verifies an observed state change before claiming success.
  * **Streamer/ATX behave differently on real units** — JPEG ``snapshot`` can
    503 or return H.264 while the WebRTC feed works (#107, #126), and ATX
    power/LED readings are unreliable (see ``GLKVM_QUIRKS``).

GL-specific fixes and quirks belong HERE, not in ``pikvm.py`` (which keeps the
other API-compatible forks, currently BliKVM) and not in the base client.
Quirk data is seeded **only** with what is actually documented/observed — do
not invent per-firmware quirks.
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from ..client import PiKVMDriver
from ..errors import KVMPilotError, SnapshotFormatError, UnavailableError

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


# The ONLY quirks we currently know for sure. Add entries here as real GL-RM1PE /
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
    Quirk(
        id="snapshot-needs-video-client",
        summary=(
            "The video encoder is on-demand: it runs only while a video client "
            "(WebRTC / the web console) is connected. With no client, /api/streamer "
            "reports streamer=null and /api/streamer/snapshot 503s indefinitely — it "
            "does NOT start the streamer, /api/streamer/stream is 404, and there is no "
            "saved frame. So headless snapshot/classify/watch are unavailable on an "
            "idle unit — the common AI-agent case (observed on V1.5.1 and V1.9.1, #142/#173)."
        ),
        workaround=(
            "kvm-pilot now auto-recovers this: `snapshot` registers a stream client "
            "over kvmd's WS to start the on-demand encoder, then retries (#142). For a "
            "snapshot-heavy flow, wrap it in `driver.streamer_warm()` (also used by "
            "`watch`) to hold the encoder warm so every frame is instant. Needs the ws "
            "dependency (now a base install). keep-awake does not help — the streamer is "
            "off regardless of whether the display is awake. If the warmed encoder then "
            "emits H.264 at the current resolution, see snapshot-h264-at-native-res."
        ),
        firmware="all",
        source="observed",
    ),
    Quirk(
        id="snapshot-h264-at-native-res",
        summary=(
            "At native/high resolutions the streamer encodes H.264, and "
            "/api/streamer/snapshot returns a lone H.264 NAL mislabeled "
            "image/jpeg — undecodable as a still (#107; a single non-IDR "
            "P-frame, so local decode is impossible even in principle). "
            "Observed at 2560x1440 on V1.5.1 and V1.9.1."
        ),
        workaround=(
            "kvm-pilot now auto-recovers this on firmware that exposes "
            "params.video_format (V1.9.1+): `snapshot` flips the encoder to "
            "MJPEG (POST /api/streamer/set_params?video_format=1), retries, and "
            "restores the prior format — a JPEG at native res, no EDID change "
            "(#187; the flip is held for the whole `streamer_warm()` block). "
            "V1.5.1 does not expose video_format, so units stuck there get the "
            "honest SnapshotFormatError — upgrade via the web UI (#177; the "
            "API flash is a no-op). MJPEG at native res raises encoder load: "
            "if snapshots start 503-ing after the flip, check the healthcheck's "
            "encoder-wedge finding (#107)."
        ),
        firmware="all",
        source="observed",
    ),
]


class GLKVMDriver(PiKVMDriver):
    """GL.iNet GLKVM fork (GL-RM1 "Comet" / GL-RM1PE "Comet PE") — first hardware target.

    API-compatible with PiKVM, but surfaces the 'API disabled' 404 as an
    ``ApiDisabledError``, exposes the known per-firmware quirks, and adds GL's
    proprietary ``/api/upgrade/*`` remote-flash capability. See the module
    docstring for how the fork diverges from stock PiKVM.
    """

    _NOT_FOUND_HINT = _GL_API_DISABLED_HINT
    _vendor = "gl.inet"

    # Observed on a real GL-RM1PE (#78): with media truly presented, the Dell
    # T7610 F12 boot menu listed "UEFI: Glinet Optical Drive 1.00"; the entry was
    # absent while /api/msd reported online=false. Substring match — the "1.00"
    # USB revision may vary.
    virtual_media_host_pattern = "Glinet Optical Drive"

    # -- Video: on-demand-streamer recovery (#142) ------------------------

    # streamer_warm() holds the MJPEG flip (#187) instead of restoring per
    # snapshot: one flip, one restore at warm-exit — flipping per frame would
    # re-init the encoder every call (slow, and it pokes the #107 wedge).
    _mjpeg_hold: bool = False
    _mjpeg_restore: int | None = None
    # Once the firmware is SEEN to lack params.video_format (V1.5.1) that fact
    # holds for the connection — skip the per-snapshot probe round-trip.
    _video_format_absent: bool = False

    def snapshot(self) -> bytes:
        """Capture the screen, recovering GL's two known snapshot failures.

        * **Streamer asleep (#142):** GL runs the video encoder only while a
          video client is connected, and the JPEG snapshot path cannot start it —
          so a headless snapshot 503s with ``streamer: null`` (the common
          AI-agent case; see ``snapshot-needs-video-client`` in ``GLKVM_QUIRKS``).
          On *that specific* failure this registers a stream client (kvmd's
          ``/api/ws`` in stream mode), waits for the encoder to come up, and
          retries; the streamer stays up briefly after the client leaves, so the
          frame lands. Any other 503 — a *running* streamer that still fails (a
          genuine wedge or a mid-reinit) — is re-raised unchanged, and without
          the ``ws`` extra it degrades to the base honest error.
        * **H.264 at native res (#107/#187):** at native/high resolutions the
          encoder emits H.264 and the snapshot bytes fail the JPEG guard. When
          the firmware exposes ``video_format`` (V1.9.1+), flip the encoder to
          MJPEG, retry, and restore — a JPEG at native res with no EDID change
          and no decoder (see ``snapshot-h264-at-native-res``).
        """
        try:
            return self._snapshot_with_stream_recovery()
        except SnapshotFormatError as exc:
            # H.264 at this resolution — whether from a cold call or from the
            # freshly-warmed encoder (the exact .39 shape: offline -> warm ->
            # format error), the MJPEG flip is the same recovery.
            return self._snapshot_via_mjpeg_flip(exc)

    def _snapshot_with_stream_recovery(self) -> bytes:
        """The #142 half of ``snapshot``: recover an offline on-demand streamer."""
        try:
            return super().snapshot()
        except UnavailableError as exc:
            try:
                offline = bool(self.video_signal_info().get("streamer_offline"))
            except KVMPilotError:
                offline = False
            if not offline:
                raise  # a running streamer that still 503s is a different fault
            return self._snapshot_via_stream_trigger(exc)

    def _snapshot_via_stream_trigger(
        self, original: UnavailableError, *, wait: float = 10.0, poll: float = 0.5
    ) -> bytes:
        """Hold a stream client open to start the on-demand encoder, then snapshot."""
        try:
            ws = self._connect_event_ws(stream=True)
        except ImportError:
            # Auto-recovery needs the ws extra; fall back to the honest 503 (#173).
            raise original from None
        logger.info(
            "snapshot: GL streamer was offline (no video client); opened a stream "
            "client to start the on-demand encoder (#142)"
        )
        try:
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                time.sleep(poll)
                try:
                    if not self.video_signal_info().get("streamer_offline"):
                        return super().snapshot()  # encoder up -> a real frame
                except UnavailableError:
                    continue  # encoder still spinning up — keep waiting
                except KVMPilotError:
                    break  # e.g. SnapshotFormatError (H.264 at this res, #107/#151);
                    # waiting won't turn H.264 into JPEG — surface it now, don't spin.
        finally:
            try:
                ws.close()
            except Exception:  # noqa: BLE001 - best-effort close
                pass
        # Grace window after close covers a last try; else the base raises honestly.
        return super().snapshot()

    # -- Video: MJPEG flip for a native-res JPEG (#187) --------------------

    def _streamer_params(self) -> dict | None:
        """The GL ``params`` block of ``/api/streamer`` (``video_format`` etc.),
        tolerating both envelope shapes via the base helpers. ``{}`` = read fine
        but the firmware doesn't expose it (V1.5.1, a durable fact); ``None`` =
        the state couldn't be read right now (transient — don't conclude)."""
        try:
            state = self.get_streamer_state()
        except KVMPilotError:
            return None
        params = state.get("params")
        if not isinstance(params, dict):
            params = self._streamer_block(state).get("params")
        return self._as_dict(params)

    def _set_video_format(self, fmt: int) -> None:
        """GL-proprietary encoder switch: ``0`` = H.264, ``1`` = MJPEG (#187)."""
        self._http.post("/api/streamer/set_params", params={"video_format": fmt})

    def _snapshot_via_mjpeg_flip(
        self, original: SnapshotFormatError, *, wait: float = 5.0, poll: float = 0.5
    ) -> bytes:
        """Flip the encoder to MJPEG, retry the snapshot, restore (#187).

        At native/high resolutions the GL streamer emits H.264, so
        ``/api/streamer/snapshot`` returns undecodable bytes (#107). On firmware
        that exposes ``params.video_format`` (V1.9.1+),
        ``POST /api/streamer/set_params?video_format=1`` switches the encoder to
        MJPEG and the very next snapshot is a valid JPEG at full native
        resolution — no EDID change, no H.264 decode (live-proven on V1.9.1).
        The prior format is restored afterwards so an interactive H.264 video
        client doesn't find the stream silently switched — unless a
        ``streamer_warm()`` hold is active, which restores once at warm-exit.
        Gated on the device advertising ``video_format``: V1.5.1 doesn't, and it
        gets the honest ``SnapshotFormatError`` (remediation: web-UI upgrade,
        #177) rather than a blind POST.
        """
        if self._video_format_absent:
            raise original
        params = self._streamer_params()
        if params is not None and "video_format" not in params:
            self._video_format_absent = True  # durable: firmware won't grow it
        current = (params or {}).get("video_format")
        if not isinstance(current, int) or current == 1:
            # Not exposed (old firmware), unreadable right now, or already
            # MJPEG — the bad bytes have some other cause; surface the
            # original honest error.
            raise original
        self._set_video_format(1)
        logger.info(
            "snapshot: flipped the GL encoder to MJPEG for a native-res JPEG "
            "(#187); will restore video_format=%d", current
        )
        try:
            deadline = time.monotonic() + wait
            while True:
                try:
                    return super().snapshot()
                except UnavailableError:
                    # The encoder re-initializes after the switch; a wedge
                    # (#107) surfaces as this raising past the deadline.
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(poll)
        finally:
            if self._mjpeg_hold:
                if self._mjpeg_restore is None:
                    self._mjpeg_restore = current  # restore once, at warm-exit
            else:
                self._restore_video_format(current)

    def _restore_video_format(self, fmt: int) -> None:
        try:
            self._set_video_format(fmt)
        except KVMPilotError as exc:
            # Benign: the stream is left on MJPEG (the web UI can switch back);
            # the snapshot itself already succeeded or raised its own error.
            logger.warning(
                "snapshot: could not restore video_format=%d after the MJPEG "
                "flip (%s); the live stream stays MJPEG until changed", fmt, exc
            )

    @contextmanager
    def streamer_warm(self, *, drain_interval: float = 1.0):
        """Keep-alive: hold a video client open so GL's on-demand encoder stays
        running for the whole block — every ``snapshot`` inside is instant, with no
        ~1.5s cold start (#142). Pure code, no vision/LLM: wrap any snapshot-driven
        flow (a vision loop, an agent look→act cycle, a boot watch) to raise its
        success rate, since the encoder is warm before the first look instead of
        503-ing on it.

        A background thread holds kvmd's ``/api/ws`` (stream mode) open and drains
        frames so the socket can't back up over a long hold; the client is closed on
        exit and the encoder lapses after its grace period. Best-effort and never
        raises: if the WS can't be opened it yields un-warmed (each ``snapshot``
        still self-recovers per call), so it is always safe to wrap.

        An MJPEG flip (#187) inside the block is *held* rather than restored per
        snapshot — one flip serves the whole loop, and the prior ``video_format``
        is put back once on exit (flipping per frame would re-init the encoder
        every call).
        """
        self._mjpeg_hold = True
        try:
            with self._streamer_warm_ws(drain_interval=drain_interval):
                yield
        finally:
            self._mjpeg_hold = False
            if self._mjpeg_restore is not None:
                self._restore_video_format(self._mjpeg_restore)
                self._mjpeg_restore = None

    @contextmanager
    def _streamer_warm_ws(self, *, drain_interval: float):
        """The #142 keep-alive itself: hold + drain the stream-mode WS client."""
        ws = None
        try:
            ws = self._connect_event_ws(stream=True)
        except Exception as exc:  # noqa: BLE001 - warming is best-effort
            logger.debug("streamer_warm: could not open a stream client (%s); "
                         "snapshots will self-recover per call", exc)
            yield
            return
        logger.info("streamer_warm: holding a stream client open to keep the GL "
                    "encoder warm (#142)")
        stop = threading.Event()

        def _drain() -> None:
            import websocket  # ws extra (a base dependency); for the timeout type
            try:
                ws.settimeout(drain_interval)
                while not stop.is_set():
                    try:
                        msg = ws.recv()
                    except websocket.WebSocketTimeoutException:
                        continue  # no frame this interval — keep the client registered
                    except Exception:  # noqa: BLE001 - a dropped WS ends the warm-up
                        break
                    if not msg:
                        break  # server closed the stream
            finally:
                try:
                    ws.close()
                except Exception:  # noqa: BLE001 - best-effort close
                    pass

        t = threading.Thread(target=_drain, name="glkvm-streamer-warm", daemon=True)
        t.start()
        try:
            yield
        finally:
            stop.set()
            t.join(timeout=drain_interval + 2.0)

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


__all__ = ["GLKVMDriver", "Quirk", "GLKVM_QUIRKS"]
