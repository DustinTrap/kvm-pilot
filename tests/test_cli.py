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
