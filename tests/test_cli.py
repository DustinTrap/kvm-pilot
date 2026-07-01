"""Tests for the CLI parser and dispatch wiring."""

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
