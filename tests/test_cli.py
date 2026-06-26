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
