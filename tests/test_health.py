"""Tests for the device preflight healthcheck (issue #80)."""

from __future__ import annotations

import types

import pytest

from emulator import EmulatorServer
from kvm_pilot.drivers.fake import FakeDriver
from kvm_pilot.drivers.glkvm import GLKVMDriver
from kvm_pilot.health import (
    CheckResult,
    HealthCache,
    HealthGateError,
    HealthReport,
    Pillar,
    Severity,
    check_default_creds,
    check_exposed_services,
    check_msd_online,
    check_recovery_path,
    check_ssh_reachable,
    check_tls_posture,
    enforce_gate,
    preflight,
    run_healthcheck,
)


def stub(**attrs):
    """A minimal driver-shaped object; callables are set as bound-free lambdas."""
    ns = types.SimpleNamespace(host="stub")
    for k, v in attrs.items():
        setattr(ns, k, v)
    return ns


def gl(emu: EmulatorServer, **kw) -> GLKVMDriver:
    d = GLKVMDriver("127.0.0.1", "admin", "s3cr3t", port=emu.port, scheme="http", **kw)
    d._http._backoff_base = 0.0
    return d


# ---- framework ------------------------------------------------------------ #


def test_severity_is_ordered_worst_last():
    assert Severity.OK < Severity.INFO < Severity.WARNING < Severity.CRITICAL
    assert max([Severity.OK, Severity.WARNING, Severity.INFO]) is Severity.WARNING


def test_report_worst_and_criticals():
    r_ok = CheckResult("a", Pillar.READINESS, Severity.OK, "t", "d")
    r_crit = CheckResult("b", Pillar.READINESS, Severity.CRITICAL, "t", "d")
    rep = HealthReport("h", "glkvm", "4.82", [r_ok, r_crit])
    assert rep.worst is Severity.CRITICAL
    assert rep.criticals == [r_crit]
    assert rep.cache_key == "glkvm@h#4.82"
    assert rep.to_dict()["worst"] == "CRITICAL"


def test_empty_report_is_ok():
    assert HealthReport("h", "fake", None).worst is Severity.OK


# ---- individual check branches ------------------------------------------- #


def test_recovery_path_critical_when_atx_unwired_and_no_gpio():
    d = stub(
        get_atx_state=lambda: {"enabled": False, "leds": {"power": False}},
        get_gpio_state=lambda: {"state": {"outputs": {}}},
    )
    res = check_recovery_path(d)
    assert res.severity is Severity.CRITICAL
    assert "out-of-band" in res.detail.lower()


def test_recovery_path_ok_when_atx_wired():
    d = stub(get_atx_state=lambda: {"enabled": True})
    assert check_recovery_path(d).severity is Severity.OK


def test_recovery_path_ok_via_gpio_when_atx_off():
    d = stub(
        get_atx_state=lambda: {"enabled": False},
        get_gpio_state=lambda: {"state": {"outputs": {"reset": {}}}},
    )
    res = check_recovery_path(d)
    assert res.severity is Severity.OK
    assert "GPIO" in res.detail


def test_recovery_path_ok_for_power_capable_bmc_without_atx():
    # No ATX surface but advertises POWER -> genuine OOB reset (BMC/fake).
    assert check_recovery_path(FakeDriver()).severity is Severity.OK


def _ssh_channel(*, up: bool | None = True):
    def reach():
        if up is None:
            raise OSError("probe blew up")
        return up
    return types.SimpleNamespace(ssh_reachable=reach, target="root@10.0.0.2", port=22)


def test_ssh_reachable_check_skips_when_unconfigured():
    # No ssh_channel attached (profile has no ssh_host) -> the check self-skips.
    assert check_ssh_reachable(FakeDriver()) is None


def test_ssh_reachable_check_ok_when_up():
    res = check_ssh_reachable(stub(ssh_channel=_ssh_channel(up=True)))
    assert res is not None
    assert res.id == "ssh-reachable"
    assert res.severity is Severity.OK
    assert res.cacheable is False  # volatile: never cached


def test_ssh_reachable_check_info_when_down():
    # Down is INFO, not WARNING — a pre-network/installer host normally won't
    # answer and must not inflate the report or gate a destructive op.
    res = check_ssh_reachable(stub(ssh_channel=_ssh_channel(up=False)))
    assert res.severity is Severity.INFO


def test_ssh_reachable_check_info_when_probe_raises():
    res = check_ssh_reachable(stub(ssh_channel=_ssh_channel(up=None)))
    assert res.severity is Severity.INFO


def test_ssh_reachable_check_is_volatile():
    from kvm_pilot.health import _is_volatile

    assert _is_volatile(check_ssh_reachable) is True


def test_tls_posture_warns_when_verification_disabled():
    d = stub(_http=types.SimpleNamespace(_verify_ssl=False, _ssl_ca_file=None))
    assert check_tls_posture(d).severity is Severity.WARNING


def test_tls_posture_ok_when_pinned():
    d = stub(_http=types.SimpleNamespace(_verify_ssl=False, _ssl_ca_file="/pki/dev.pem"))
    assert check_tls_posture(d).severity is Severity.OK


def test_default_creds_warns_on_known_default():
    d = stub(_http=types.SimpleNamespace(_user="admin", _passwd="admin"))
    assert check_default_creds(d).severity is Severity.WARNING


def test_default_creds_ok_otherwise():
    d = stub(_http=types.SimpleNamespace(_user="admin", _passwd="s3cr3t"))
    assert check_default_creds(d).severity is Severity.OK


def test_msd_online_warns_when_attached_but_offline():
    d = stub(get_msd_state=lambda: {"online": False, "drive": {"image": {"name": "x.iso"}}})
    res = check_msd_online(d)
    assert res.severity is Severity.WARNING
    assert res.cacheable is False
    assert res.auto_fix is None  # this stub has no msd_connect/disconnect


def test_msd_online_ok_when_no_image():
    d = stub(get_msd_state=lambda: {"online": False, "drive": {"image": None}})
    assert check_msd_online(d).severity is Severity.OK


def test_msd_online_offers_autofix_when_reconnect_available():
    d = stub(
        get_msd_state=lambda: {"online": False, "drive": {"image": {"name": "x.iso"}}},
        msd_connect=lambda: None,
        msd_disconnect=lambda: None,
    )
    res = check_msd_online(d)
    assert res.auto_fix is not None and res.auto_fix.safe_reversible


def test_exposed_services_warns_on_vnc():
    d = stub(get_info=lambda: {"extras": {"vnc": {"enabled": True}, "webterm": {"enabled": True}}})
    res = check_exposed_services(d)
    assert res.severity is Severity.WARNING
    assert "vnc" in res.detail


# ---- run_healthcheck ------------------------------------------------------ #


def test_run_healthcheck_on_fake_is_all_ok():
    rep = run_healthcheck(FakeDriver())
    assert rep.driver_kind == "fake"
    assert rep.worst is Severity.OK
    assert {r.id for r in rep.results} >= {"api-reachable", "recovery-path"}


def test_broken_check_does_not_crash_audit():
    def boom(_driver):
        raise RuntimeError("kaboom")

    rep = run_healthcheck(FakeDriver(), checks=[boom])
    assert rep.results[0].severity is Severity.INFO
    assert "kaboom" in rep.results[0].detail


# ---- gate ----------------------------------------------------------------- #


def _crit_report():
    return HealthReport(
        "h", "glkvm", "4.82",
        [CheckResult("recovery-path", Pillar.READINESS, Severity.CRITICAL, "t", "d")],
    )


def test_gate_fails_closed_unattended():
    with pytest.raises(HealthGateError):
        enforce_gate(_crit_report(), confirm=None)


def test_gate_prompts_and_can_proceed():
    enforce_gate(_crit_report(), confirm=lambda op, d: True)  # no raise


def test_gate_aborts_when_operator_declines():
    with pytest.raises(HealthGateError):
        enforce_gate(_crit_report(), confirm=lambda op, d: False)


def test_gate_skips_acknowledged_criticals():
    enforce_gate(_crit_report(), confirm=None, acknowledged=frozenset({"recovery-path"}))


def test_gate_skip_flag_bypasses():
    enforce_gate(_crit_report(), confirm=None, skip=True)


def test_gate_noop_when_no_criticals():
    rep = HealthReport("h", "fake", None, [
        CheckResult("x", Pillar.SECURITY, Severity.WARNING, "t", "d"),
    ])
    enforce_gate(rep, confirm=None)  # warnings never block


# ---- cache ---------------------------------------------------------------- #


def test_cache_roundtrip_only_stores_stable(tmp_path):
    cache = HealthCache(tmp_path / "hc.json")
    rep = HealthReport("h", "glkvm", "4.82", [
        CheckResult("firmware-report", Pillar.FIRMWARE, Severity.INFO, "t", "d", cacheable=True),
        CheckResult("video-signal", Pillar.READINESS, Severity.OK, "t", "d", cacheable=False),
    ], ran_at=1000.0)
    cache.store_stable(rep)
    got = HealthCache(tmp_path / "hc.json").stable_results(rep.cache_key, now=1000.0)
    assert [r.id for r in got] == ["firmware-report"]  # volatile not cached


def test_cache_expires_after_max_age(tmp_path):
    cache = HealthCache(tmp_path / "hc.json", max_age=10.0)
    rep = HealthReport("h", "glkvm", "4.82",
                       [CheckResult("firmware-report", Pillar.FIRMWARE, Severity.INFO, "t", "d")],
                       ran_at=1000.0)
    cache.store_stable(rep)
    assert cache.stable_results(rep.cache_key, now=1005.0) is not None
    assert cache.stable_results(rep.cache_key, now=2000.0) is None  # stale


def test_cache_acknowledgements_persist(tmp_path):
    cache = HealthCache(tmp_path / "hc.json")
    cache.acknowledge("glkvm@h#4.82", ["recovery-path"])
    assert "recovery-path" in HealthCache(tmp_path / "hc.json").acknowledged("glkvm@h#4.82")


# ---- preflight (stable cache + volatile live + gate) ---------------------- #


def test_preflight_reprobes_volatile_but_reuses_stable_cache(tmp_path):
    cache = HealthCache(tmp_path / "hc.json")
    # `quirks` counts the STABLE audit (known_quirks only runs inside a stable
    # check); `api` counts the VOLATILE api-reachable check.
    calls = {"api": 0, "quirks": 0}

    def make_driver(video_ok):
        def get_info():
            calls["api"] += 1
            return {}

        def known_quirks(firmware=None):
            calls["quirks"] += 1
            return []

        return stub(
            get_info=get_info,
            get_firmware_info=lambda: {"version": "1.0", "model": "M"},
            known_quirks=known_quirks,
            has_video_signal=lambda: video_ok,
            supports=lambda cap: False,
        )

    # First run: populates the stable cache (stable audit runs once).
    preflight(make_driver(True), cache=cache)
    assert calls["quirks"] == 1
    api_after_first = calls["api"]

    # Second run: stable served from cache (audit NOT re-run), but the volatile
    # api-reachable/video checks DO run live again.
    rep = preflight(make_driver(False), cache=cache)
    assert calls["quirks"] == 1  # stable cached -> audit not re-run
    assert calls["api"] > api_after_first  # volatile re-probed live
    video = next(r for r in rep.results if r.id == "video-signal")
    assert video.severity is Severity.WARNING  # live change reflected, not stale OK


def test_preflight_enforces_gate_on_critical(tmp_path):
    d = stub(
        get_info=lambda: {},
        get_atx_state=lambda: {"enabled": False},
        get_gpio_state=lambda: {"state": {"outputs": {}}},
        has_video_signal=lambda: True,
        supports=lambda cap: False,
    )
    with pytest.raises(HealthGateError):
        preflight(d, cache=HealthCache(tmp_path / "hc.json"), confirm=None)


# ---- GLKVM real-transport integration ------------------------------------ #


@pytest.fixture()
def emu():
    with EmulatorServer() as server:
        yield server


def test_healthcheck_over_real_transport_glkvm(emu):
    rep = run_healthcheck(gl(emu))
    by_id = {r.id: r for r in rep.results}
    # TLS off by default -> warning; firmware quirks surfaced; recovery-path OK (atx wired).
    assert by_id["tls-posture"].severity is Severity.WARNING
    assert by_id["recovery-path"].severity is Severity.OK
    assert by_id["firmware-report"].severity is Severity.INFO
    assert by_id["firmware-quirks"].severity is Severity.WARNING  # api-disabled quirk applies


def test_recovery_path_critical_over_transport_when_atx_disabled(emu):
    emu.state.atx_enabled = False
    rep = run_healthcheck(gl(emu))
    rec = next(r for r in rep.results if r.id == "recovery-path")
    assert rec.severity is Severity.CRITICAL
    assert rep.worst is Severity.CRITICAL


# ---- wrong-driver fingerprint (#145) -------------------------------------- #


def _base_pikvm(emu):
    from kvm_pilot.client import PiKVMDriver

    d = PiKVMDriver("127.0.0.1", "admin", "s3cr3t", port=emu.port, scheme="http")
    d._http._backoff_base = 0.0
    return d


def test_driver_identity_warns_when_pikvm_profile_hits_gl_device(emu):
    # GL firmware self-reports as a stock rpi PiKVM (#126), so the healthcheck
    # probes GL's proprietary /api/upgrade/version to catch a wrong profile.
    from kvm_pilot.health import check_driver_identity

    emu.state.upgrade_present = True
    res = check_driver_identity(_base_pikvm(emu))
    assert res is not None and res.severity is Severity.WARNING
    assert 'driver = "glkvm"' in res.remediation


def test_driver_identity_silent_on_stock_pikvm(emu):
    from kvm_pilot.health import check_driver_identity

    assert check_driver_identity(_base_pikvm(emu)) is None  # 404 = stock answer


def test_driver_identity_skips_fork_drivers(emu):
    from kvm_pilot.health import check_driver_identity

    emu.state.upgrade_present = True
    assert check_driver_identity(gl(emu)) is None  # glkvm already knows who it is


# ---- first-connection audit (issue #80) ---------------------------------- #


def test_preflight_once_runs_then_skips_within_session():
    from kvm_pilot.health import preflight_once, reset_session_audit

    d = FakeDriver()
    assert preflight_once(d) is not None       # first connection audits
    assert preflight_once(d) is None           # already audited this session
    reset_session_audit()
    assert preflight_once(d) is not None        # reset re-enables the audit


def test_preflight_once_skip_returns_none():
    from kvm_pilot.health import preflight_once

    assert preflight_once(FakeDriver(), skip=True) is None


def test_preflight_once_informs_without_blocking_on_critical():
    # enforce=False must return the report (with the CRITICAL) and never raise —
    # a standing critical must not make a read impossible.
    from kvm_pilot.health import preflight_once

    d = stub(
        host="crit",
        get_atx_state=lambda: {"enabled": False, "leds": {"power": False}},
        get_gpio_state=lambda: {"state": {"outputs": {}}},
    )
    rep = preflight_once(d, enforce=False)
    assert rep is not None and rep.worst is Severity.CRITICAL


def test_preflight_once_enforces_and_fails_closed_on_critical():
    from kvm_pilot.health import preflight_once

    d = stub(
        host="crit",
        get_atx_state=lambda: {"enabled": False, "leds": {"power": False}},
        get_gpio_state=lambda: {"state": {"outputs": {}}},
    )
    with pytest.raises(HealthGateError):
        preflight_once(d, confirm=None, enforce=True)  # automation -> fail closed


# ---- firmware currency + capability profile (registry, #80 follow-up) ----- #


def _fw(**kw):
    base = {"vendor": "gl.inet", "product": "Rockchip RV1126B-P EVB", "version": "4.82"}
    base.update(kw)
    return stub(host="h", get_firmware_info=lambda: base)


def test_vercmp_orders_numeric_segments():
    from kvm_pilot.health import _vercmp

    assert _vercmp("4.82", "4.90") < 0
    assert _vercmp("6.10.30.00", "6.10.80.00") < 0
    assert _vercmp("2.78", "2.78") == 0
    assert _vercmp("7.0", "6.99") > 0


def test_affected_specs():
    from kvm_pilot.health import _affected

    assert _affected("<=4.82", "4.82") and not _affected("<=4.80", "4.82")
    assert _affected("4.82", "4.82") and not _affected("4.82", "4.83")
    assert _affected("<4.83", "4.82") and _affected(">=4.80", "4.82")


def test_currency_update_available(monkeypatch):
    from kvm_pilot import health

    monkeypatch.setattr(health, "_REGISTRY_CACHE", {"firmware": [
        {"vendor": "gl.inet", "product": "RV1126B", "latest": "4.90",
         "source": "https://x", "date": "2026-05-29"}]})
    r = health.check_firmware_currency(_fw())
    assert r.severity is Severity.WARNING and "latest known is 4.90" in r.detail


def test_currency_offers_remote_update_when_profile_supports_it(monkeypatch):
    from kvm_pilot import health

    monkeypatch.setattr(health, "_REGISTRY_CACHE", {"firmware": [
        {"vendor": "gl.inet", "product": "RV1126B", "latest": "4.90",
         "source": "https://x", "date": "2026-05-29",
         "profile": {"remote_update": {"supported": True, "risk": "high",
                                       "recovery_required": True}}}]})
    r = health.check_firmware_currency(_fw())
    assert r.severity is Severity.WARNING
    # The remediation becomes actionable: it names the command and the risk.
    assert "kvm-pilot firmware-update" in r.remediation
    assert "RISK: HIGH" in r.remediation
    assert "physical access" in r.remediation


def test_currency_falls_back_to_vendor_pointer_without_remote_update(monkeypatch):
    from kvm_pilot import health

    monkeypatch.setattr(health, "_REGISTRY_CACHE", {"firmware": [
        {"vendor": "gl.inet", "product": "RV1126B", "latest": "4.90",
         "source": "https://vendor", "date": "2026-05-29"}]})
    r = health.check_firmware_currency(_fw())
    # No remote_update profile -> plain pointer, no firmware-update offer.
    assert "firmware-update" not in r.remediation
    assert "https://vendor" in r.remediation


def test_currency_known_bad_range_is_critical(monkeypatch):
    from kvm_pilot import health

    monkeypatch.setattr(health, "_REGISTRY_CACHE", {"firmware": [
        {"vendor": "gl.inet", "product": "RV1126B", "known_bad": [
            {"affected": "<=4.82", "severity": "critical", "issue": "ATX unwired", "source": "https://x"}]}]})
    assert health.check_firmware_currency(_fw()).severity is Severity.CRITICAL


def test_currency_quiet_when_current(monkeypatch):
    from kvm_pilot import health

    monkeypatch.setattr(health, "_REGISTRY_CACHE", {"firmware": [
        {"vendor": "gl.inet", "product": "RV1126B", "latest": "4.82",
         "source": "https://x", "date": "2026-01-01"}]})
    assert health.check_firmware_currency(_fw()) is None


def test_currency_none_when_unmatched(monkeypatch):
    from kvm_pilot import health

    monkeypatch.setattr(health, "_REGISTRY_CACHE", {"firmware": []})
    assert health.check_firmware_currency(_fw()) is None


def test_capability_profile_warns_on_degraded_axis(monkeypatch):
    from kvm_pilot import health

    monkeypatch.setattr(health, "_REGISTRY_CACHE", {"firmware": [
        {"vendor": "gl.inet", "product": "RV1126B", "profile": {
            "mouse": "absolute", "vmedia": "reports-only",
            "power_state_trusted": False, "video": "h264/1080p60"}}]})
    r = health.check_capability_profile(_fw())
    assert r.severity is Severity.WARNING
    assert "vmedia=reports-only" in r.detail and "NOT trusted" in r.detail
    assert "boot-from-ISO" in r.remediation


def test_capability_profile_info_when_all_good(monkeypatch):
    from kvm_pilot import health

    monkeypatch.setattr(health, "_REGISTRY_CACHE", {"firmware": [
        {"vendor": "gl.inet", "product": "RV1126B", "profile": {
            "mouse": "absolute", "vmedia": "reliable", "power_state_trusted": True}}]})
    r = health.check_capability_profile(_fw())
    assert r.severity is Severity.INFO and r.remediation == ""


def test_capability_profile_none_without_profile(monkeypatch):
    from kvm_pilot import health

    monkeypatch.setattr(health, "_REGISTRY_CACHE", {"firmware": [
        {"vendor": "gl.inet", "product": "RV1126B", "latest": "4.90",
         "source": "https://x", "date": "2026-01-01"}]})
    assert health.check_capability_profile(_fw()) is None


def test_glkvm_firmware_identity(emu):
    fw = gl(emu).get_firmware_info()
    assert fw["vendor"] == "gl.inet"
    assert "product" in fw and "version" in fw
