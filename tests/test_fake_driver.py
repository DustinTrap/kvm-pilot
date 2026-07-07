"""Behaviour of the in-process FakeDriver (no network, no hardware)."""

from __future__ import annotations

import pytest

from kvm_pilot.drivers import FakeDriver
from kvm_pilot.drivers.base import BootProgress, Capability
from kvm_pilot.errors import SafetyError
from kvm_pilot.safety import deny_all
from kvm_pilot.vision.base import PHASE_NO_SIGNAL, PHASE_POWER_OFF, ScreenState, VisionBackend


def test_capabilities_match_pikvm_plus_boot_progress():
    caps = FakeDriver().capabilities()
    expected = {
        Capability.SYSTEM_INFO,
        Capability.POWER,
        Capability.HID,
        Capability.VIDEO,
        Capability.VIRTUAL_MEDIA,
        Capability.GPIO,
        Capability.EVENTS,
        Capability.LOGS,
        Capability.BOOT_PROGRESS,  # FakeDriver is the first BootProgress implementer
    }
    assert caps == expected


def test_is_a_boot_progress_implementer():
    d = FakeDriver()
    assert isinstance(d, BootProgress)
    assert d.supports(Capability.BOOT_PROGRESS)


def test_power_toggles_state_and_records():
    d = FakeDriver(powered=False)
    assert d.is_powered_on() is False
    d.power_on()
    assert d.is_powered_on() is True
    assert ("power_on", None) in d.actions
    d.power_off()
    assert d.is_powered_on() is False


def test_destructive_op_gated_by_confirm():
    d = FakeDriver(powered=False, confirm=deny_all)
    with pytest.raises(SafetyError):
        d.power_on()
    assert d.is_powered_on() is False  # state untouched
    assert d.actions == []


def test_dry_run_skips_mutation():
    d = FakeDriver(powered=False, dry_run=True)
    d.power_on()  # gated path returns False -> body skipped
    assert d.is_powered_on() is False
    assert d.actions == []


def test_hid_is_recorded():
    d = FakeDriver()
    d.type_text("root\n")
    d.press_key("Enter")
    d.send_shortcut("ControlLeft,AltLeft,Delete")
    assert d.typed == ["root\n"]
    assert d.keys == ["Enter"]
    assert d.shortcuts == ["ControlLeft,AltLeft,Delete"]


def test_set_jiggler_toggles_state():  # #159 keep-awake
    d = FakeDriver()
    assert d.get_hid_state()["jiggler"]["active"] is False
    assert d.set_jiggler(True)["active"] is True
    assert d.get_hid_state()["jiggler"]["active"] is True
    assert ("set_jiggler", True) in d.actions


def test_snapshot_roundtrips(tmp_path):
    d = FakeDriver(image=b"\xff\xd8custom")
    assert d.snapshot() == b"\xff\xd8custom"
    import base64

    assert base64.b64decode(d.snapshot_base64()) == b"\xff\xd8custom"
    out = d.snapshot_save(str(tmp_path / "s.jpg"))
    assert out.read_bytes() == b"\xff\xd8custom"


def test_boot_progress_reflects_power_and_phase():
    d = FakeDriver(powered=False, phase="grub_menu")
    assert d.get_boot_progress() is None  # nothing reported while off
    d.power_on()
    assert d.get_boot_progress() == "grub_menu"


def test_watch_events_replays_queue():
    d = FakeDriver(events=[{"event_type": "atx_state", "event": {"on": True}}])
    d.push_event("hid_state", {"online": True})
    seen = []
    replayed = list(d.watch_events(on_event=lambda t, e: seen.append(t)))
    assert seen == ["atx_state", "hid_state"]
    assert len(replayed) == 2


def test_gpio_and_media_are_gated():
    d = FakeDriver(confirm=deny_all)
    with pytest.raises(SafetyError):
        d.gpio_switch("relay", True)
    with pytest.raises(SafetyError):
        d.msd_connect()


def test_mount_iso_records_when_allowed():
    d = FakeDriver()  # confirm=None -> allow_all
    name = d.mount_iso("https://example.com/isos/ubuntu-24.04.iso?sig=x")
    assert name == "ubuntu-24.04.iso"
    assert d.mounted == ["ubuntu-24.04.iso"]
    assert ("mount_iso", "ubuntu-24.04.iso") in d.actions


def test_mount_iso_dry_run_skips_everything_without_prompting():
    # Dry-run wins before confirmation: all three MSD guards (write, set_params,
    # connect) log-and-skip and the confirm callback is never consulted, so
    # --dry-run works unattended.
    seen: list[str] = []

    def counting_confirm(op: str, desc: str) -> bool:
        seen.append(op)
        return True

    d = FakeDriver(dry_run=True, confirm=counting_confirm)
    d.mount_iso("/isos/x.iso")
    assert seen == []  # confirm is never consulted under dry-run
    assert d.mounted == []  # dry-run mounts nothing


def test_mount_iso_confirm_sees_all_guards_when_live():
    # Live (no dry-run): the confirm callback sees every MSD guard in order,
    # including the msd.write gate that fires before any upload would start.
    seen: list[str] = []

    def counting_confirm(op: str, desc: str) -> bool:
        seen.append(op)
        return True

    d = FakeDriver(confirm=counting_confirm)
    d.mount_iso("/isos/x.iso")
    assert seen == ["msd.write", "msd.set_params", "msd.connect"]
    assert d.mounted == ["x.iso"]

    seen.clear()
    d.mount_iso("https://example.com/y.iso")
    assert seen[0] == "msd.write_remote"


def test_mount_iso_deny_raises_at_first_guard():
    d = FakeDriver(confirm=deny_all)
    with pytest.raises(SafetyError):
        d.mount_iso("/isos/x.iso")
    assert d.mounted == []


class _ExplodingBackend(VisionBackend):
    """Asserts the analyzer's cheap gates resolve without a model call."""

    def classify(self, image_b64: str, hint: str = "") -> ScreenState:
        raise AssertionError("vision backend must not be called when a cheap gate resolves")

    @property
    def model(self) -> str:
        return "none"


def test_analyzer_resolves_power_off_against_fake_without_model_call():
    from kvm_pilot.vision import ScreenAnalyzer

    analyzer = ScreenAnalyzer(FakeDriver(powered=False), _ExplodingBackend())
    state = analyzer.classify()
    assert state.phase == PHASE_POWER_OFF
    assert analyzer.vlm_calls == 0
    assert analyzer.cheap_resolves == 1


def test_analyzer_reports_no_signal_when_powered_but_dark():
    from kvm_pilot.vision import ScreenAnalyzer

    analyzer = ScreenAnalyzer(FakeDriver(powered=True, video_signal=False), _ExplodingBackend())
    assert analyzer.classify().phase == PHASE_NO_SIGNAL


# -- systematic safety-guard coverage (#52), FakeDriver side ---------------

_GATED_FAKE = [
    ("power_on", lambda d: d.power_on()),
    ("power_off", lambda d: d.power_off()),
    ("power_off_hard", lambda d: d.power_off_hard()),
    ("reset_hard", lambda d: d.reset_hard()),
    ("type_text", lambda d: d.type_text("x")),
    ("press_key", lambda d: d.press_key("Enter")),
    ("send_shortcut", lambda d: d.send_shortcut("MetaLeft")),
    ("mouse_click", lambda d: d.mouse_click()),
    ("msd_connect", lambda d: d.msd_connect()),
    ("msd_disconnect", lambda d: d.msd_disconnect()),
    ("gpio_switch", lambda d: d.gpio_switch("r", True)),
    ("gpio_pulse", lambda d: d.gpio_pulse("r")),
]
_GATED_FAKE_IDS = [e[0] for e in _GATED_FAKE]


@pytest.mark.parametrize("_id,call", _GATED_FAKE, ids=_GATED_FAKE_IDS)
def test_fake_gated_method_blocks_on_deny(_id, call):
    d = FakeDriver(powered=True, confirm=deny_all)
    with pytest.raises(SafetyError):
        call(d)
    assert d.actions == []  # a fail-open op would have recorded an action


@pytest.mark.parametrize("_id,call", _GATED_FAKE, ids=_GATED_FAKE_IDS)
def test_fake_gated_method_skipped_under_dry_run(_id, call):
    d = FakeDriver(powered=True, dry_run=True)
    call(d)
    assert d.actions == []
