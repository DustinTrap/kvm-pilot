"""Tests for the ScreenAnalyzer wait loop (uses fake backend, no network)."""

import pytest

from kvm_pilot.errors import TimeoutError
from kvm_pilot.vision import ScreenAnalyzer
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
    def snapshot_base64(self, quality: int = 85) -> str:
        return "ZmFrZQ=="  # "fake"


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
