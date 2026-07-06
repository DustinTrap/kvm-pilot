"""Tests for the CLI parser and dispatch wiring."""

import types

import pytest

from kvm_pilot.cli import build_parser, main


def test_parser_requires_subcommand():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_power_subcommand_parsed():
    parser = build_parser()
    args = parser.parse_args(["power", "off-hard", "--host", "h", "--dry-run"])
    assert args.command == "power"
    assert args.action == "off-hard"
    assert args.dry_run is True


def test_ssh_check_forwards_ssh_target_overrides(monkeypatch):
    # A runtime --ssh-host (e.g. an install-time DHCP IP the profile can't know)
    # flows through _resolve_cfg into the HostConfig, beating profile/env (#81).
    # (ssh-check still resolves a full HostConfig, so give it a KVM host.)
    from kvm_pilot.cli import _resolve_cfg

    monkeypatch.setenv("KVM_PILOT_HOST", "kvm.local")
    parser = build_parser()
    args = parser.parse_args(
        ["ssh-check", "--ssh-host", "10.9.9.9", "--ssh-port", "2222", "--ssh-user", "root"]
    )
    cfg = _resolve_cfg(args)
    assert cfg.ssh_host == "10.9.9.9"
    assert cfg.ssh_port == 2222
    assert cfg.ssh_user == "root"


def test_watch_requires_phase():
    parser = build_parser()
    args = parser.parse_args(
        ["watch", "grub_menu", "--profile", "p", "--backend", "local",
         "--vision-url", "http://x/v1", "--vision-model", "m"]
    )
    assert args.phase == "grub_menu"
    assert args.backend == "local"
    assert args.vision_model == "m"


def test_dry_run_blocks_real_call(monkeypatch):
    # info needs a host; provide via env so resolve_host succeeds, and stub the
    # network by making get_info return a constant.
    monkeypatch.setenv("KVM_PILOT_HOST", "fake")
    from kvm_pilot import client as client_mod

    monkeypatch.setattr(client_mod.KVMClient, "get_info", lambda self: {"ok": True})
    rc = main(["info", "--host", "fake"])
    assert rc == 0


def test_global_timeout_precedes_subcommand():
    parser = build_parser()
    args = parser.parse_args(["--timeout", "45", "info", "--host", "h"])
    assert args.http_timeout == 45.0
    # `watch` keeps its own --timeout (vision deadline) without colliding.
    args = parser.parse_args(
        ["--timeout", "5", "watch", "grub_menu", "--host", "h", "--timeout", "120"]
    )
    assert args.http_timeout == 5.0
    assert args.timeout == 120.0


def test_capabilities_command_offline(capsys):
    # capabilities() is structural and makes no network call.
    rc = main(["capabilities", "--host", "fake"])
    assert rc == 0
    out = capsys.readouterr().out
    for cap in ("power", "hid", "video", "logs"):
        assert cap in out


def test_events_streams_and_respects_count(monkeypatch, capsys):
    from kvm_pilot import client as client_mod

    def fake_watch(self, on_event=None, stream=True, timeout=None):
        for i in range(10):
            yield {"event_type": "atx_state", "event": {"n": i}}

    monkeypatch.setattr(client_mod.KVMClient, "watch_events", fake_watch)
    rc = main(["events", "--host", "fake", "--count", "3"])
    assert rc == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert len(lines) == 3  # stopped at --count, not all 10


def test_events_reports_missing_ws_extra(monkeypatch, capsys):
    from kvm_pilot import client as client_mod

    def boom(self, on_event=None, stream=True, timeout=None):
        raise ImportError("websocket-client is required for watch_events().")

    monkeypatch.setattr(client_mod.KVMClient, "watch_events", boom)
    rc = main(["events", "--host", "fake"])
    assert rc == 1
    assert "websocket-client" in capsys.readouterr().err


def test_driver_fake_needs_no_host_and_lists_boot_progress(capsys):
    # The fake driver runs fully offline: no --host, no network, no API key.
    rc = main(["capabilities", "--driver", "fake"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "boot_progress" in out  # only the fake reports this today


def test_driver_fake_power_action_is_dispatched():
    rc = main(["power", "on", "--driver", "fake", "--yes"])
    assert rc == 0


def test_key_routes_chords_to_send_shortcut(monkeypatch):
    # #112: `key` accepts a +/,-separated chord and sends it as one shortcut
    # (kvmd comma form) instead of failing on "single keys only".
    from kvm_pilot.drivers.fake import FakeDriver

    sent = {}
    monkeypatch.setattr(
        FakeDriver, "send_shortcut", lambda self, keys: sent.setdefault("keys", keys)
    )
    rc = main(["key", "ControlLeft+AltLeft+F2", "--driver", "fake", "--yes"])
    assert rc == 0
    assert sent["keys"] == "ControlLeft,AltLeft,F2"


def test_key_single_key_still_pressed(monkeypatch):
    from kvm_pilot.drivers.fake import FakeDriver

    pressed = {}
    monkeypatch.setattr(
        FakeDriver, "press_key", lambda self, key, **kw: pressed.setdefault("key", key)
    )
    rc = main(["key", "F2", "--driver", "fake", "--yes"])
    assert rc == 0
    assert pressed["key"] == "F2"


def test_ssh_bootstrap_plan_mode(capsys):
    # Plan mode (no --execute) prints the plan and sends nothing; also proves the
    # --command flag's dest doesn't collide with the subcommand name.
    import json

    rc = main(["ssh-bootstrap", "--driver", "fake"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["stage"] == "plan"
    assert any("send_shortcut" in step["detail"] for step in data["steps"])


def test_driver_glkvm_builds_the_glkvm_subclass():
    from kvm_pilot.cli import _build_client, build_parser
    from kvm_pilot.drivers.pikvm import GLKVMDriver

    args = build_parser().parse_args(["info", "--driver", "glkvm", "--host", "h"])
    kvm = _build_client(args)  # construction only; no network
    assert isinstance(kvm, GLKVMDriver)


def test_driver_defaults_to_pikvm_when_unset(monkeypatch):
    from kvm_pilot.cli import _build_client, build_parser
    from kvm_pilot.client import PiKVMDriver

    monkeypatch.delenv("KVM_PILOT_DRIVER", raising=False)
    args = build_parser().parse_args(["info", "--host", "h"])  # no --driver
    kvm = _build_client(args)
    assert isinstance(kvm, PiKVMDriver) and not type(kvm).__name__.startswith(("GL", "Bli"))


def test_unsupported_driver_via_env_is_a_clean_error(monkeypatch, capsys):
    # An unknown driver kind (no from-config support) must produce a clean error
    # (exit 1), not a crash.
    monkeypatch.setenv("KVM_PILOT_DRIVER", "ipmi")
    rc = main(["info", "--host", "h"])
    assert rc == 1
    assert "does not support" in capsys.readouterr().err


def test_driver_redfish_builds_redfish_driver():
    # --driver redfish is now a CLI choice and constructs a RedfishDriver
    # (construction only; login is lazy so no network call here).
    from kvm_pilot.cli import _build_client, build_parser
    from kvm_pilot.drivers.redfish import RedfishDriver

    args = build_parser().parse_args(["info", "--driver", "redfish", "--host", "h"])
    kvm = _build_client(args)
    assert isinstance(kvm, RedfishDriver)


def test_redfish_capabilities_are_offline(capsys):
    # capabilities() is structural — a BMC's set, no network, no key.
    rc = main(["capabilities", "--driver", "redfish", "--host", "h"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "power" in out and "virtual_media" in out and "system_info" in out
    assert "hid" not in out and "video" not in out  # a BMC has neither


@pytest.mark.parametrize(
    "argv, capability",
    [
        (["type", "hello", "--driver", "redfish", "--host", "h"], "hid"),
        (["key", "Return", "--driver", "redfish", "--host", "h"], "hid"),
        (["snapshot", "out.jpg", "--driver", "redfish", "--host", "h"], "video"),
        (["classify", "--driver", "redfish", "--host", "h"], "video"),
        (["watch", "grub_menu", "--driver", "redfish", "--host", "h"], "video"),
        (["events", "--driver", "redfish", "--host", "h"], "events"),
    ],
)
def test_capability_partial_driver_fails_cleanly(argv, capability, capsys, monkeypatch):
    # The gate fires before any network call (and before the vision backend is
    # built): a BMC lacks HID/Video/Events, so these subcommands exit 1 with a
    # clear message instead of an AttributeError.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    rc = main(argv)
    assert rc == 1
    err = capsys.readouterr().err
    assert capability in err and "redfish" in err


def test_capability_gate_leaves_full_drivers_working(capsys):
    # The fake driver has HID, so `type` still dispatches through the gate.
    # HID input is a gated destructive op now, so --yes stands in for the prompt.
    rc = main(["type", "hello world", "--driver", "fake", "--yes"])
    assert rc == 0


def test_type_dry_run_sends_nothing_and_exits_zero():
    # HID is gated: --dry-run must log-and-skip without prompting (exit 0, not 3).
    rc = main(["type", "hello world", "--driver", "fake", "--dry-run"])
    assert rc == 0


def test_fake_via_env_needs_no_host(monkeypatch):
    # Parity with --driver fake: KVM_PILOT_DRIVER=fake must not require a host.
    monkeypatch.setenv("KVM_PILOT_DRIVER", "fake")
    assert main(["capabilities"]) == 0


def test_missing_host_is_a_clean_error(monkeypatch, capsys):
    monkeypatch.delenv("KVM_PILOT_HOST", raising=False)
    monkeypatch.delenv("KVM_PILOT_DRIVER", raising=False)
    rc = main(["info"])  # no host, pikvm driver -> resolve_host ValueError, caught cleanly
    assert rc == 1
    assert "host" in capsys.readouterr().err.lower()


def test_classify_driver_fake_is_offline_without_api_key(monkeypatch, capsys):
    # With the lazy API-key check, the analyzer resolves power_off from the fake's
    # cheap power gate with no model call — so no ANTHROPIC_API_KEY is required.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    rc = main(["classify", "--driver", "fake"])
    assert rc == 0
    assert "power_off" in capsys.readouterr().out


def test_classify_local_backend_missing_url_is_a_clean_error(monkeypatch, capsys):
    # A missing --vision-url must surface as a clean error + exit 1, not an
    # uncaught ValueError traceback.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    rc = main(["classify", "--driver", "fake", "--backend", "local"])
    assert rc == 1
    assert "base_url" in capsys.readouterr().err


def test_watch_rejects_unknown_phase(capsys):
    # A typo'd phase would otherwise burn the whole timeout in paid model calls.
    rc = main(["watch", "grub_menuu", "--driver", "fake"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "grub_menuu" in err and "grub_menu" in err  # names the valid tokens


def test_eject_dispatches_and_honors_dry_run(capsys):
    rc = main(["eject", "--driver", "fake", "--dry-run"])
    assert rc == 0
    rc = main(["eject", "--driver", "fake", "--yes"])
    assert rc == 0
    assert "ejected" in capsys.readouterr().out


def test_passwd_file_supplies_password(tmp_path, monkeypatch):
    from kvm_pilot.cli import _build_client, build_parser
    pf = tmp_path / "pw"
    pf.write_text("filesecret\n")  # trailing newline must be stripped
    monkeypatch.delenv("KVM_PILOT_PASSWD", raising=False)
    args = build_parser().parse_args(["info", "--host", "h", "--passwd-file", str(pf)])
    kvm = _build_client(args)
    assert kvm._http._passwd == "filesecret"


def test_passwd_flag_wins_over_passwd_file(tmp_path):
    from kvm_pilot.cli import _build_client, build_parser
    pf = tmp_path / "pw"
    pf.write_text("fromfile\n")
    args = build_parser().parse_args(
        ["info", "--host", "h", "--passwd", "fromflag", "--passwd-file", str(pf)]
    )
    assert _build_client(args)._http._passwd == "fromflag"


def test_ask_passwd_prompts_via_getpass(monkeypatch):
    import getpass

    from kvm_pilot.cli import _build_client, build_parser
    monkeypatch.delenv("KVM_PILOT_PASSWD", raising=False)
    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "prompted")
    args = build_parser().parse_args(["info", "--host", "h", "--ask-passwd"])
    assert _build_client(args)._http._passwd == "prompted"


def test_fake_driver_never_prompts_without_ask_flag(monkeypatch):
    # No --ask-passwd => getpass must not be called (would hang in CI).
    import getpass

    def boom(*a, **k):
        raise AssertionError("getpass should not be called without --ask-passwd")

    monkeypatch.setattr(getpass, "getpass", boom)
    assert main(["capabilities", "--driver", "fake"]) == 0


def test_cli_boot_progress_on_fake(capsys):
    # FakeDriver serves BootProgress; powered off -> "unknown" (None).
    rc = main(["boot-progress", "--driver", "fake"])
    assert rc == 0
    assert "unknown" in capsys.readouterr().out


def test_cli_sensors_unsupported_on_pikvm_fails_cleanly(capsys):
    # The PiKVM family has no Sensors capability -> clean exit 1, not a crash.
    rc = main(["sensors", "--host", "h"])
    assert rc == 1
    assert "sensors" in capsys.readouterr().err


# -- healthcheck command + destructive gate (#80) -------------------------- #


def test_healthcheck_command_on_fake_driver(capsys):
    from kvm_pilot.cli import main

    rc = main(["healthcheck", "--driver", "fake"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "Healthcheck: fake@" in out
    assert "recovery-path" in out or "Out-of-band" in out


def test_healthcheck_command_json(capsys):
    import json as _json

    from kvm_pilot.cli import main

    rc = main(["healthcheck", "--driver", "fake", "--json"])
    assert rc == 0
    report = _json.loads(capsys.readouterr().out)
    assert report["driver"] == "fake"
    assert report["worst"] in {"OK", "INFO", "WARNING", "CRITICAL"}


def test_destructive_gate_blocks_on_critical(monkeypatch, capsys):
    # A driver whose recovery-path is CRITICAL must block a power action when
    # unattended (no --yes -> interactive confirm, which fails closed with no TTY).
    from kvm_pilot import cli

    def fake_gate(kvm, confirm, *, skip):
        from kvm_pilot.health import HealthGateError

        if not skip:
            raise HealthGateError("no out-of-band recovery path")

    monkeypatch.setattr(cli, "_preflight_gate", fake_gate)
    rc = cli.main(["power", "off-hard", "--driver", "fake"])
    assert rc == 1  # HealthGateError is a KVMPilotError -> exit 1


def test_destructive_gate_skipped_with_flag(monkeypatch):
    from kvm_pilot import cli

    calls = {"n": 0}

    def fake_gate(kvm, confirm, *, skip):
        calls["n"] += 1
        assert skip is True  # --skip-healthcheck propagates

    monkeypatch.setattr(cli, "_preflight_gate", fake_gate)
    rc = cli.main(["power", "on", "--driver", "fake", "--yes", "--skip-healthcheck"])
    assert rc == 0 and calls["n"] == 1


def test_destructive_gate_not_run_in_dry_run(monkeypatch):
    from kvm_pilot import cli

    def boom(*a, **k):
        raise AssertionError("preflight must not run under --dry-run")

    monkeypatch.setattr(cli, "_preflight_gate", boom)
    rc = cli.main(["power", "off-hard", "--driver", "fake", "--dry-run"])
    assert rc == 0


# -- read-only first-connection audit (issue #80) -------------------------- #


def test_readonly_command_audits_on_connect(monkeypatch):
    from kvm_pilot import cli

    calls = {"n": 0, "skip": None}

    def spy(kvm, *, skip):
        calls["n"] += 1
        calls["skip"] = skip

    monkeypatch.setattr(cli, "_inform_on_connect", spy)
    assert cli.main(["info", "--driver", "fake"]) == 0
    assert calls["n"] == 1 and calls["skip"] is False


def test_readonly_audit_respects_skip_flag(monkeypatch):
    from kvm_pilot import cli

    seen = {}
    monkeypatch.setattr(cli, "_inform_on_connect", lambda kvm, *, skip: seen.update(skip=skip))
    assert cli.main(["info", "--driver", "fake", "--skip-healthcheck"]) == 0
    assert seen["skip"] is True


def test_readonly_audit_never_blocks_the_read(monkeypatch):
    # The audit runs for real (fake driver is all-OK) and the read still succeeds.
    from kvm_pilot import cli

    assert cli.main(["info", "--driver", "fake"]) == 0


def test_capabilities_does_not_preflight(monkeypatch):
    # capabilities is offline (uses _build_client) and must trigger no audit.
    from kvm_pilot import cli

    def boom(*a, **k):
        raise AssertionError("capabilities must not preflight")

    monkeypatch.setattr(cli, "_inform_on_connect", boom)
    monkeypatch.setattr(cli, "_preflight_gate", boom)
    assert cli.main(["capabilities", "--driver", "fake"]) == 0


def test_inform_on_connect_prints_findings_to_stderr(monkeypatch, capsys):
    from kvm_pilot import cli
    from kvm_pilot.health import CheckResult, HealthReport, Pillar, Severity

    rep = HealthReport(
        "h", "glkvm", "4.82",
        [CheckResult("recovery-path", Pillar.READINESS, Severity.CRITICAL,
                     "Out-of-band recovery path", "No out-of-band reset")],
    )
    monkeypatch.setattr("kvm_pilot.health.preflight_once", lambda *a, **k: rep)
    cli._inform_on_connect(object(), skip=False)
    err = capsys.readouterr().err
    assert "preflight glkvm@h" in err
    assert "CRITICAL" in err and "recovery path" in err.lower()


def test_inform_on_connect_swallows_audit_errors(monkeypatch, capsys):
    # An informational audit that blows up must not break the command.
    from kvm_pilot import cli

    def boom(*a, **k):
        raise RuntimeError("network guard")

    monkeypatch.setattr("kvm_pilot.health.preflight_once", boom)
    cli._inform_on_connect(object(), skip=False)  # must not raise
    assert capsys.readouterr().err == ""


# -- firmware-update command -----------------------------------------------


class _FakeFwu:
    """A driver stub exposing just the surface cmd_firmware_update touches."""

    def __init__(self, *, enabled=True):
        self._status = {"enabled": enabled, "current": "V1.5.1 release2",
                        "image_size": 307581578}
        self.calls: list[tuple] = []

    def supports(self, cap):
        from kvm_pilot.drivers.base import Capability

        return cap == Capability.FIRMWARE_UPDATE  # VIRTUAL_MEDIA False -> eject skipped

    def get_upgrade_status(self):
        return self._status

    def get_firmware_info(self):
        return {"vendor": "gl.inet", "product": "RM1PE", "version": "V1.5.1 release2"}

    def apply_firmware_update(self, *, image=None, dry_run=True):
        self.calls.append(("apply", dry_run, image))
        return {"sent": not dry_run, "dry_run": dry_run,
                "plan": [{"method": "POST", "path": "/api/upgrade/start", "note": "flash"}]}

    def close(self):
        pass


def _recovery_report(severity):
    r = types.SimpleNamespace(id="recovery-path", severity=severity)
    return types.SimpleNamespace(results=[r])


def _patch_fwu(monkeypatch, driver, severity):
    from kvm_pilot import cli, health

    monkeypatch.setattr(cli, "_build_client", lambda args: driver)
    monkeypatch.setattr(health, "run_healthcheck", lambda kvm: _recovery_report(severity))


def test_firmware_update_dry_run_is_default_and_sends_nothing(monkeypatch, capsys):
    from kvm_pilot.health import Severity

    d = _FakeFwu()
    _patch_fwu(monkeypatch, d, Severity.CRITICAL)
    rc = main(["firmware-update", "--host", "h"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "RISK HIGH" in out
    assert d.calls == [("apply", True, None)]  # dry-run plan only, never executed


def test_firmware_update_execute_refuses_without_recovery_or_override(monkeypatch, capsys):
    from kvm_pilot.health import Severity

    d = _FakeFwu()
    _patch_fwu(monkeypatch, d, Severity.CRITICAL)
    rc = main(["firmware-update", "--host", "h", "--execute"])
    assert rc == 1
    assert "Refusing to flash" in capsys.readouterr().err
    assert d.calls == []  # the real flash was never attempted


def test_firmware_update_execute_with_override_flashes(monkeypatch, capsys):
    from kvm_pilot.health import Severity

    d = _FakeFwu()
    _patch_fwu(monkeypatch, d, Severity.CRITICAL)
    rc = main(["firmware-update", "--host", "h", "--execute",
               "--i-have-physical-access", "--yes"])
    assert rc == 0
    assert ("apply", False, None) in d.calls
    assert "flash started" in capsys.readouterr().out.lower()


def test_firmware_update_execute_allowed_when_recovery_present(monkeypatch, capsys):
    from kvm_pilot.health import Severity

    d = _FakeFwu()
    _patch_fwu(monkeypatch, d, Severity.OK)  # recovery-path OK, not CRITICAL
    rc = main(["firmware-update", "--host", "h", "--execute", "--yes"])
    assert rc == 0
    assert ("apply", False, None) in d.calls


def test_firmware_update_reports_when_subsystem_disabled(monkeypatch, capsys):
    from kvm_pilot.health import Severity

    d = _FakeFwu(enabled=False)
    _patch_fwu(monkeypatch, d, Severity.OK)
    rc = main(["firmware-update", "--host", "h"])
    assert rc == 1
    assert "not available" in capsys.readouterr().err
