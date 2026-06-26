"""Tests for the ScreenAnalyzer wait loop (uses fake backend, no network)."""

import pytest

from kvm_pilot.errors import TimeoutError
from kvm_pilot.vision import DEFAULT_OCR_RULES, ScreenAnalyzer
from kvm_pilot.vision.base import ScreenState, VisionBackend


class FakeBackend(VisionBackend):
    """Returns a scripted sequence of phases, then repeats the last."""

    def __init__(self, phases, confidence=0.95):
        self._phases = phases
        self._confidence = confidence
        self._i = 0
        self.calls = 0

    @property
    def model(self) -> str:
        return "fake-model"

    def classify(self, image_b64: str, hint: str = "") -> ScreenState:
        self.calls += 1
        phase = self._phases[min(self._i, len(self._phases) - 1)]
        self._i += 1
        return ScreenState(phase, f"fake {phase}", self._confidence, "", image_b64)


class FakeKVM:
    """A live capture: every snapshot differs, so the unchanged-frame gate never
    fires and the backend is consulted on every poll."""

    def __init__(self):
        self._n = 0

    def snapshot_base64(self, quality: int = 85) -> str:
        self._n += 1
        return f"frame-{self._n}"


class StaticKVM:
    """A genuinely static screen: the same frame every time."""

    def snapshot_base64(self, quality: int = 85) -> str:
        return "ZmFrZQ=="


def test_classify_returns_phase():
    analyzer = ScreenAnalyzer(FakeKVM(), FakeBackend(["bios_menu"]))
    assert analyzer.classify().phase == "bios_menu"


def test_wait_for_state_reached_after_polls():
    backend = FakeBackend(["booting", "booting", "grub_menu"])
    analyzer = ScreenAnalyzer(FakeKVM(), backend, default_poll_interval=0.0)
    state = analyzer.wait_for_state("grub_menu", timeout=5.0)
    assert state.phase == "grub_menu"
    assert backend.calls == 3


def test_wait_for_state_times_out():
    backend = FakeBackend(["booting"])  # never reaches target
    analyzer = ScreenAnalyzer(FakeKVM(), backend, default_poll_interval=0.0)
    with pytest.raises(TimeoutError):
        analyzer.wait_for_state("desktop", timeout=0.2)


def test_low_confidence_does_not_match():
    backend = FakeBackend(["grub_menu"], confidence=0.30)
    analyzer = ScreenAnalyzer(FakeKVM(), backend, default_poll_interval=0.0)
    with pytest.raises(TimeoutError):
        analyzer.wait_for_state("grub_menu", timeout=0.2, min_confidence=0.70)


def test_wait_for_any_state():
    backend = FakeBackend(["booting", "login_prompt"])
    analyzer = ScreenAnalyzer(FakeKVM(), backend, default_poll_interval=0.0)
    state = analyzer.wait_for_any_state(["login_prompt", "desktop"], timeout=5.0)
    assert state.phase == "login_prompt"


def test_on_state_change_fires_once_per_change():
    changes = []
    backend = FakeBackend(["booting", "booting", "desktop"])
    analyzer = ScreenAnalyzer(
        FakeKVM(), backend, default_poll_interval=0.0,
        on_state_change=lambda old, new: changes.append(new.phase),
    )
    analyzer.wait_for_any_state(["desktop"], timeout=5.0)
    assert changes == ["booting", "desktop"]  # not three entries


# -- cheap gates (avoid a model call) --------------------------------------


def test_unchanged_frame_reuses_classification_without_model_call():
    backend = FakeBackend(["booting", "grub_menu"])  # would advance if re-called
    analyzer = ScreenAnalyzer(StaticKVM(), backend, default_poll_interval=0.0)
    first = analyzer.classify()
    second = analyzer.classify()
    assert first.phase == "booting"
    assert second.phase == "booting"  # reused, not re-classified
    assert backend.calls == 1
    assert analyzer.cheap_resolves == 1


def test_skip_unchanged_frames_can_be_disabled():
    backend = FakeBackend(["booting", "grub_menu"])
    analyzer = ScreenAnalyzer(
        StaticKVM(), backend, default_poll_interval=0.0, skip_unchanged_frames=False
    )
    analyzer.classify()
    analyzer.classify()
    assert backend.calls == 2  # no reuse


class PoweredOffKVM:
    def snapshot_base64(self, quality: int = 85) -> str:
        raise AssertionError("must not snapshot when powered off")

    def is_powered_on(self) -> bool:
        return False


def test_power_off_short_circuits_without_snapshot_or_model():
    backend = FakeBackend(["desktop"])
    analyzer = ScreenAnalyzer(PoweredOffKVM(), backend)
    state = analyzer.classify()
    assert state.phase == "power_off"
    assert backend.calls == 0
    assert analyzer.vlm_calls == 0
    assert analyzer.cheap_resolves == 1


class NoSignalKVM:
    def snapshot_base64(self, quality: int = 85) -> str:
        raise AssertionError("must not snapshot when there is no video signal")

    def is_powered_on(self) -> bool:
        return True

    def has_video_signal(self) -> bool:
        return False


def test_no_signal_short_circuits_without_model():
    backend = FakeBackend(["desktop"])
    analyzer = ScreenAnalyzer(NoSignalKVM(), backend)
    assert analyzer.classify().phase == "no_signal"
    assert backend.calls == 0


class OCRKVM:
    def __init__(self, text):
        self._text = text
        self._n = 0

    def snapshot_base64(self, quality: int = 85) -> str:
        self._n += 1
        return f"frame-{self._n}"

    def snapshot_ocr(self, *args, **kwargs) -> str:
        return self._text


def test_ocr_assist_resolves_text_screen_without_model():
    backend = FakeBackend(["desktop"])
    analyzer = ScreenAnalyzer(
        OCRKVM("GNU GRUB version 2.06"), backend, ocr_rules=DEFAULT_OCR_RULES
    )
    assert analyzer.classify().phase == "grub_menu"
    assert backend.calls == 0


def test_ocr_assist_falls_through_to_model_on_no_match():
    backend = FakeBackend(["desktop"])
    analyzer = ScreenAnalyzer(
        OCRKVM("some unrelated terminal text"), backend, ocr_rules=DEFAULT_OCR_RULES
    )
    assert analyzer.classify().phase == "desktop"  # OCR didn't match → model ran
    assert backend.calls == 1


def test_gates_default_on_but_inert_without_device_probes():
    # FakeKVM exposes only snapshot_base64; the power/signal probe must no-op.
    backend = FakeBackend(["bios_menu"])
    analyzer = ScreenAnalyzer(FakeKVM(), backend)
    assert analyzer.classify().phase == "bios_menu"
    assert analyzer.vlm_calls == 1
