"""
ScreenAnalyzer — snapshot + classify loop over any VisionBackend.

Takes a KVMClient (for snapshots) and a VisionBackend (for classification) and
provides single-shot classify plus blocking wait-for-state loops with bounded
backoff. Backend-agnostic: Claude or a local VLM, identical call sites.

A model call is the most expensive way to read the screen, so ``classify`` is
*gated* by cheap signals first (see ``docs/sensing-hierarchy.svg``):

  1. power / no-signal short-circuit — if the device reports the host powered
     off or no video source, return ``power_off`` / ``no_signal`` with no model
     call (and no snapshot);
  2. optional OCR-assist — if ``ocr_rules`` are set and the device has on-board
     OCR, resolve text screens (GRUB, a kernel panic) without a model;
  3. unchanged-frame skip — if the captured frame is identical to the last
     classified one, reuse that result instead of re-classifying.

Only when none of these resolve does the vision backend run. The gates are
duck-typed and defensive: a backend or device that lacks a probe simply skips
it, so this never breaks a driver that only does snapshots.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from ..errors import TimeoutError, VisionError
from .base import (
    PHASE_CRASH_SCREEN,
    PHASE_GRUB_MENU,
    PHASE_NO_SIGNAL,
    PHASE_POWER_OFF,
    PHASE_UEFI_SHELL,
    ScreenState,
    VisionBackend,
)

logger = logging.getLogger("kvm_pilot.vision")

# Opt-in OCR rules: (case-insensitive substring, phase). Kept deliberately
# high-precision so a match is unambiguous; pass your own list to extend.
DEFAULT_OCR_RULES: list[tuple[str, str]] = [
    ("gnu grub", PHASE_GRUB_MENU),
    ("kernel panic", PHASE_CRASH_SCREEN),
    ("uefi interactive shell", PHASE_UEFI_SHELL),
]


class ScreenAnalyzer:
    def __init__(
        self,
        kvm,
        backend: VisionBackend,
        *,
        default_poll_interval: float = 3.0,
        min_confidence: float = 0.70,
        on_state_change: Callable | None = None,
        gate_on_power_signal: bool = True,
        skip_unchanged_frames: bool = True,
        ocr_rules: list[tuple[str, str]] | None = None,
    ):
        self._kvm = kvm
        self._backend = backend
        self._poll_interval = default_poll_interval
        self._min_confidence = min_confidence
        self._on_state_change = on_state_change
        self._gate_on_power_signal = gate_on_power_signal
        self._skip_unchanged_frames = skip_unchanged_frames
        self._ocr_rules = ocr_rules
        self._last_state: ScreenState | None = None
        # Observability: how often the cheap gates avoided a model call.
        self.vlm_calls = 0
        self.cheap_resolves = 0

    @property
    def backend(self) -> VisionBackend:
        return self._backend

    # -- single shot -----------------------------------------------------

    def classify(self, hint: str = "", image_b64: str | None = None) -> ScreenState:
        # The cheap gates apply only to the auto-snapshot path; when the caller
        # hands us a specific image they want that image classified verbatim.
        gates_on = image_b64 is None

        if gates_on and self._gate_on_power_signal:
            cheap = self._probe_power_signal()
            if cheap is not None:
                self.cheap_resolves += 1
                return self._finalize(cheap)

        if gates_on and self._ocr_rules:
            ocr_state = self._probe_ocr()
            if ocr_state is not None:
                self.cheap_resolves += 1
                return self._finalize(ocr_state)

        if image_b64 is None:
            try:
                image_b64 = self._kvm.snapshot_base64()
            except Exception as exc:  # noqa: BLE001
                raise VisionError(f"KVM snapshot failed: {exc}") from exc

        # Identical frame ⇒ identical phase: reuse the last result, no model call.
        if (
            gates_on
            and self._skip_unchanged_frames
            and self._last_state is not None
            and self._last_state.image_b64
            and image_b64 == self._last_state.image_b64
        ):
            self.cheap_resolves += 1
            return self._last_state

        state = self._backend.classify(image_b64, hint=hint)
        self.vlm_calls += 1
        return self._finalize(state)

    # -- gates -----------------------------------------------------------

    def _finalize(self, state: ScreenState) -> ScreenState:
        if self._on_state_change and (
            self._last_state is None or self._last_state.phase != state.phase
        ):
            try:
                self._on_state_change(self._last_state, state)
            except Exception:  # noqa: BLE001 - never let a callback crash the loop
                logger.exception("on_state_change callback raised")
        self._last_state = state
        return state

    def _probe_power_signal(self) -> ScreenState | None:
        """Resolve power_off / no_signal from cheap device state, or None."""
        kvm = self._kvm
        try:
            if hasattr(kvm, "is_powered_on") and not kvm.is_powered_on():
                return ScreenState(PHASE_POWER_OFF, "ATX power LED reports off.", 0.99, "")
            if hasattr(kvm, "has_video_signal") and not kvm.has_video_signal():
                return ScreenState(PHASE_NO_SIGNAL, "Capture reports no video source.", 0.99, "")
        except Exception:  # noqa: BLE001 - a probe must never break classification
            return None
        return None

    def _probe_ocr(self) -> ScreenState | None:
        """Resolve a phase from on-device OCR text via ocr_rules, or None."""
        kvm = self._kvm
        if not self._ocr_rules or not hasattr(kvm, "snapshot_ocr"):
            return None
        try:
            text = kvm.snapshot_ocr()
        except Exception:  # noqa: BLE001 - OCR is best-effort; fall through to the model
            return None
        low = (text or "").lower()
        for needle, phase in self._ocr_rules:
            if needle in low:
                return ScreenState(phase, f"OCR matched {needle!r}.", 0.95, text)
        return None

    # -- wait loops ------------------------------------------------------

    def wait_for_state(self, target_phase: str, **kw) -> ScreenState:
        return self.wait_for_any_state([target_phase], **kw)

    def wait_for_any_state(
        self,
        target_phases: list[str],
        *,
        timeout: float = 300.0,
        poll_interval: float | None = None,
        hint: str = "",
        min_confidence: float | None = None,
        on_poll: Callable | None = None,
    ) -> ScreenState:
        interval = poll_interval if poll_interval is not None else self._poll_interval
        threshold = min_confidence if min_confidence is not None else self._min_confidence
        deadline = time.monotonic() + timeout
        attempts = 0

        while True:
            elapsed = timeout - (deadline - time.monotonic())
            try:
                state = self.classify(hint=hint)
            except VisionError as exc:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Repeated classify failures; last: {exc}") from exc
                time.sleep(interval)
                continue

            attempts += 1
            if on_poll:
                try:
                    on_poll(state, elapsed)
                except Exception:  # noqa: BLE001
                    logger.exception("on_poll callback raised")

            if state.phase in target_phases and state.confidence >= threshold:
                return state
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Timed out after {timeout:.0f}s waiting for {target_phases!r}. "
                    f"Last: {state.phase} (confidence={state.confidence:.2f})"
                )

            backoff = interval * (1.0 + min(attempts // 10, 3) * 0.5)
            time.sleep(min(backoff, max(0.0, deadline - time.monotonic())))

    # -- conveniences ----------------------------------------------------

    def current_phase(self) -> str:
        return self.classify().phase

    def last_state(self) -> ScreenState | None:
        return self._last_state


__all__ = ["ScreenAnalyzer", "DEFAULT_OCR_RULES"]
