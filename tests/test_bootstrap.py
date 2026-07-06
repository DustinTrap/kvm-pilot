"""Tests for the guided SSH bootstrap helper (``kvm_pilot.bootstrap``, issue #81).

Driven entirely against the in-process FakeDriver (which records ``typed`` /
``shortcuts`` and returns canned ``snapshot_ocr`` / boot phase) with an injected
fake SSH channel — no real network, HID, or ssh binary.
"""

from __future__ import annotations

from types import SimpleNamespace

from kvm_pilot.bootstrap import DEFAULT_BOOTSTRAP_COMMANDS, _valid_ip, ssh_bootstrap
from kvm_pilot.config import HostConfig
from kvm_pilot.drivers.fake import FakeDriver
from kvm_pilot.vision.analyzer import ScreenAnalyzer


class _StubBackend:
    """A vision backend that must never be reached (boot_progress resolves first)."""

    def classify(self, image_b64, hint=""):
        raise AssertionError("the VLM backend should not be called")


def _analyzer(kvm):
    return ScreenAnalyzer(kvm, _StubBackend())


def _cfg(**kw):
    base = {"host": "kvm.local", "driver": "fake"}
    base.update(kw)
    return HostConfig(**base)


def _channel(*, reachable=True, auth_ok=True):
    return SimpleNamespace(
        ssh_reachable=lambda: reachable,
        ssh_exec=lambda cmd: {"ok": auth_ok, "returncode": 0 if auth_ok else 1},
    )


def test_valid_ip_rejects_loopback_linklocal_and_junk():
    assert _valid_ip("192.168.1.50")
    assert not _valid_ip("127.0.0.1")
    assert not _valid_ip("0.0.0.0")
    assert not _valid_ip("169.254.1.1")
    assert not _valid_ip("999.1.1.1")
    assert not _valid_ip("nope")


def test_plan_mode_sends_nothing():
    kvm = FakeDriver(powered=True, phase="installer_progress")
    res = ssh_bootstrap(kvm, _cfg(), analyzer=_analyzer(kvm), execute=False)
    assert res.ok is True
    assert res.stage == "plan"
    assert kvm.typed == [] and kvm.shortcuts == []  # nothing was sent to the device


def test_execute_success_discovers_ip_and_hands_off():
    kvm = FakeDriver(powered=True, phase="installer_progress", ocr_text="KVMIP=192.168.1.50")
    cfg = _cfg()
    res = ssh_bootstrap(
        kvm, cfg, analyzer=_analyzer(kvm), execute=True,
        channel_factory=lambda c: _channel(), sleep=lambda _: None,
    )
    assert res.ok is True
    assert res.stage == "done"
    assert res.discovered_host == "192.168.1.50"
    assert res.reachable is True
    assert cfg.ssh_host == "192.168.1.50"  # channel repointed at the discovered IP
    assert kvm.shortcuts == ["ControlLeft,AltLeft,F2"]  # VT-switch happened
    typed = "".join(kvm.typed)
    assert all(command in typed for command in DEFAULT_BOOTSTRAP_COMMANDS)


def test_canary_abort_types_no_sshd_commands():
    # No marker echoes back -> the console was not reached -> abort before sshd.
    kvm = FakeDriver(powered=True, phase="installer_progress", ocr_text="")
    res = ssh_bootstrap(
        kvm, _cfg(), analyzer=_analyzer(kvm), execute=True,
        channel_factory=lambda c: _channel(), sleep=lambda _: None,
    )
    assert res.ok is False
    assert res.stage == "read-ip"
    typed = "".join(kvm.typed)
    for command in DEFAULT_BOOTSTRAP_COMMANDS:
        assert command not in typed  # the anti-catastrophe gate held


def test_reachable_but_auth_fails_escalates():
    kvm = FakeDriver(powered=True, phase="installer_progress", ocr_text="KVMIP=10.0.0.9")
    res = ssh_bootstrap(
        kvm, _cfg(), analyzer=_analyzer(kvm), execute=True,
        channel_factory=lambda c: _channel(reachable=True, auth_ok=False), sleep=lambda _: None,
    )
    assert res.ok is False
    assert res.stage == "auth"
    assert res.discovered_host == "10.0.0.9"
    assert res.reachable is True  # a reachable port is not a working channel


def test_refuses_when_not_an_installer():
    kvm = FakeDriver(powered=True, phase="login_prompt")
    res = ssh_bootstrap(
        kvm, _cfg(), analyzer=_analyzer(kvm), execute=True,
        channel_factory=lambda c: _channel(), sleep=lambda _: None,
    )
    assert res.ok is False
    assert res.stage == "detect-installer"
    assert kvm.typed == [] and kvm.shortcuts == []  # never typed into an unknown screen


def test_no_installer_check_skips_the_precondition():
    kvm = FakeDriver(powered=True, phase="login_prompt", ocr_text="KVMIP=10.0.0.9")
    res = ssh_bootstrap(
        kvm, _cfg(), analyzer=_analyzer(kvm), execute=True, require_installer=False,
        channel_factory=lambda c: _channel(), sleep=lambda _: None,
    )
    assert res.ok is True
    assert res.discovered_host == "10.0.0.9"


def test_unreachable_target_escalates_with_discovered_host():
    kvm = FakeDriver(powered=True, phase="installer_progress", ocr_text="KVMIP=10.0.0.9")
    res = ssh_bootstrap(
        kvm, _cfg(), analyzer=_analyzer(kvm), execute=True, reachable_timeout=0.0,
        channel_factory=lambda c: _channel(reachable=False), sleep=lambda _: None,
    )
    assert res.ok is False
    assert res.stage == "reachability"
    assert res.discovered_host == "10.0.0.9"
    assert res.reachable is False
