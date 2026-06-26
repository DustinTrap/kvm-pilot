"""
ScreenAnalyzer — snapshot + classify loop over any VisionBackend.

Takes a KVMClient (for snapshots) and a VisionBackend (for classification) and
provides single-shot classify plus blocking wait-for-state loops with bounded
backoff. Backend-agnostic: Claude or a local VLM, identical call sites.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from ..errors import TimeoutError, VisionError
from .base import ScreenState, VisionBackend

logger = logging.getLogger("kvm_pilot.vision")


class ScreenAnalyzer:
    def __init__(
        self,
        kvm,
        backend: VisionBackend,
        *,
        default_poll_interval: float = 3.0,
        min_confidence: float = 0.70,
        on_state_change: Callable | None = None,
    ):
        self._kvm = kvm
        self._backend = backend
        self._poll_interval = default_poll_interval
        self._min_confidence = min_confidence
        self._on_state_change = on_state_change
        self._last_state: ScreenState | None = None

    @property
    def backend(self) -> VisionBackend:
        return self._backend

    def __enter__(self) -> ScreenAnalyzer:
        return self

    def __exit__(self, *_) -> None:
        pass

    # -- single shot -----------------------------------------------------

    def classify(self, hint: str = "", image_b64: str | None = None) -> ScreenState:
        if image_b64 is None:
            try:
                image_b64 = self._kvm.snapshot_base64()
            except Exception as exc:  # noqa: BLE001
                raise VisionError(f"KVM snapshot failed: {exc}") from exc

        state = self._backend.classify(image_b64, hint=hint)

        if self._on_state_change and (
            self._last_state is None or self._last_state.phase != state.phase
        ):
            try:
                self._on_state_change(self._last_state, state)
            except Exception:  # noqa: BLE001 - never let a callback crash the loop
                logger.exception("on_state_change callback raised")
        self._last_state = state
        return state

    detect_state = classify  # ergonomic alias

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


__all__ = ["ScreenAnalyzer"]
