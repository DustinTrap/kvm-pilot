"""The #99 live-test harness: `kvm-pilot test-report`.

Acceptance from the issue: against the fake driver the read-only caps are
recorded (snapshot with #156 conditions), destructive caps are skipped unless
--include'd (and refused without --attest), a destructive cap whose effect
didn't occur records an honest FAIL (the #94 regression guard), and the row
round-trips through every ledger consumer.
"""

from __future__ import annotations

import json

import pytest

from kvm_pilot.cli import main
from kvm_pilot.drivers.fake import FakeDriver


@pytest.fixture(autouse=True)
def _fake_env(monkeypatch):
    monkeypatch.setenv("KVM_PILOT_DRIVER", "fake")
    monkeypatch.delenv("KVM_PILOT_TEST_LEDGER", raising=False)


def _run(tmp_path, *argv):
    ledger = tmp_path / "runs.jsonl"
    rc = main(["test-report", "--ledger", str(ledger), *argv])
    rows = []
    if ledger.exists():
        rows = [json.loads(line) for line in ledger.read_text().splitlines() if line]
    return rc, rows, ledger


def _caps(row):
    return {c["capability"]: c for c in row["capabilities"]}


def test_readonly_run_records_the_five_probes(tmp_path, capsys):
    rc, rows, _ = _run(tmp_path)
    assert rc == 0 and len(rows) == 1
    row = rows[0]
    caps = _caps(row)
    assert set(caps) == {"info", "snapshot", "healthcheck", "logs", "power_state"}
    assert all(c["passed"] for c in caps.values())
    assert row["source"] == "synthetic"          # fake is never "real"
    assert row["run_id"].startswith("synthetic-fake-")
    assert "operator" not in row                 # no attestation on a read-only run
    assert "recorded run" in capsys.readouterr().out


def test_snapshot_row_carries_conditions_on_pass_and_fail(tmp_path, monkeypatch):
    rc, rows, _ = _run(tmp_path)
    assert rows[0] and _caps(rows[0])["snapshot"]["conditions"]["resolution"] == "1920x1080"

    # Failure injection: non-JPEG bytes. Conditions must STILL be recorded —
    # a fail row without its operating point is the #180 contradiction machine.
    monkeypatch.setattr(FakeDriver, "snapshot", lambda self: b"\x00\x00\x00\x01junk")
    rc, rows, _ = _run(tmp_path / "b")
    snap = _caps(rows[0])["snapshot"]
    assert rc == 0 and snap["passed"] is False
    assert "non-JPEG" in snap["outcome"]
    assert snap["conditions"]["resolution"] == "1920x1080"


def test_destructive_not_exercised_without_include(tmp_path):
    actions = []
    orig = FakeDriver.power_off

    def spying(self, wait=True):
        actions.append("power_off")
        return orig(self, wait)

    import unittest.mock as mock
    with mock.patch.object(FakeDriver, "power_off", spying):
        _rc, rows, _ = _run(tmp_path)
    assert "power" not in _caps(rows[0])
    assert "virtual_media" not in _caps(rows[0])
    assert actions == []


def test_include_without_attest_is_refused(tmp_path, capsys):
    rc, rows, ledger = _run(tmp_path, "--include", "power", "--yes")
    assert rc == 2
    assert not ledger.exists()                   # nothing probed, nothing recorded
    assert "--attest" in capsys.readouterr().err


def test_include_unknown_cap_is_refused(tmp_path, capsys):
    rc, _rows, _ = _run(tmp_path, "--include", "bogus", "--attest", "x", "--yes")
    assert rc == 2
    assert "virtual_media" in capsys.readouterr().err  # names the valid set


def test_include_virtual_media_needs_iso(tmp_path, capsys):
    rc, _rows, _ = _run(tmp_path, "--include", "virtual_media", "--attest", "x", "--yes")
    assert rc == 2
    assert "--iso" in capsys.readouterr().err


def test_power_probe_passes_and_restores_with_attestation(tmp_path):
    rc, rows, _ = _run(tmp_path, "--include", "power", "--attest",
                       "op: lab unit, ok to cycle", "--yes")
    assert rc == 0
    row = rows[0]
    power = _caps(row)["power"]
    assert power["passed"] is True
    assert row["operator"] == "op: lab unit, ok to cycle"


def test_power_probe_records_honest_fail_when_effect_not_observed(tmp_path, monkeypatch):
    # The #94 regression guard: the action "succeeds" but the state never flips.
    def lying_power_on(self, wait=True):
        if self.safety.guard("atx.power_on", "x"):
            self._record("power_on")             # accepted... but no effect

    monkeypatch.setattr(FakeDriver, "power_on", lying_power_on)
    monkeypatch.setattr("kvm_pilot.test_report._wait_until",
                        lambda pred, timeout=10.0, poll=0.5: pred())
    rc, rows, _ = _run(tmp_path, "--include", "power", "--attest", "op", "--yes")
    power = _caps(rows[0])["power"]
    assert rc == 0                               # a FAIL is data, not a harness error
    assert power["passed"] is False
    assert "not observed" in power["outcome"]


def test_virtual_media_probe_mount_and_eject_observed(tmp_path):
    rc, rows, _ = _run(tmp_path, "--include", "virtual_media", "--attest", "op",
                       "--iso", "http://srv/demo.iso", "--yes")
    assert rc == 0
    vm = _caps(rows[0])["virtual_media"]
    assert vm["passed"] is True
    assert "eject observed" in vm["outcome"]


def test_firmware_update_unsupported_is_skipped_not_failed(tmp_path, capsys):
    rc, rows, _ = _run(tmp_path, "--include", "firmware_update", "--attest", "op", "--yes")
    assert rc == 0
    assert "firmware_update" not in _caps(rows[0])
    assert "skip" in capsys.readouterr().out


def test_declined_confirm_skips_not_fails(tmp_path, monkeypatch):
    # Without --yes the guard prompts; deny -> the capability was NOT exercised.
    monkeypatch.setattr("kvm_pilot.safety.interactive_confirm",
                        lambda op, desc: False)
    import kvm_pilot.cli as cli_mod
    monkeypatch.setattr(cli_mod, "interactive_confirm", lambda op, desc: False)
    rc, rows, _ = _run(tmp_path, "--include", "power", "--attest", "op",
                       "--skip-healthcheck")
    assert rc == 0
    assert "power" not in _caps(rows[0])


def test_ledger_env_fallback_and_flag_precedence(tmp_path, monkeypatch, capsys):
    env_ledger = tmp_path / "env.jsonl"
    monkeypatch.setenv("KVM_PILOT_TEST_LEDGER", str(env_ledger))
    assert main(["test-report"]) == 0            # no --ledger: env wins
    assert env_ledger.exists()
    flag_ledger = tmp_path / "flag.jsonl"
    assert main(["test-report", "--ledger", str(flag_ledger)]) == 0
    assert flag_ledger.exists()
    assert len(env_ledger.read_text().splitlines()) == 1  # flag beat env
    assert str(flag_ledger) in capsys.readouterr().out


def test_rows_roundtrip_through_every_ledger_consumer(tmp_path):
    _rc, rows, ledger = _run(tmp_path)
    from kvm_pilot import support_matrix
    from kvm_pilot.maturity import compute_matrix, load_ledger

    records = load_ledger(ledger)
    assert [r["run_id"] for r in records] == [rows[0]["run_id"]]
    assert compute_matrix(records) == {}          # synthetic never promotes
    assert support_matrix.rollup(records) == []   # and produces no evidence row

    import importlib.util
    import sys
    from pathlib import Path
    spec = importlib.util.spec_from_file_location(
        "build_wiki_tr",
        Path(__file__).resolve().parents[1] / ".github" / "scripts" / "build_wiki.py",
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules["build_wiki_tr"] = spec.loader.exec_module(mod) or mod
    db = mod._hcl_load(ledger)                    # ingests without KeyError
    assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1


def test_two_runs_append_two_distinct_rows(tmp_path):
    ledger = tmp_path / "runs.jsonl"
    assert main(["test-report", "--ledger", str(ledger)]) == 0
    assert main(["test-report", "--ledger", str(ledger)]) == 0
    rows = [json.loads(line) for line in ledger.read_text().splitlines()]
    assert len(rows) == 2                         # append-only, no truncation


def test_json_output_carries_row_and_ledger(tmp_path, capsys):
    rc, _rows, ledger = _run(tmp_path, "--json")
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["ledger"] == str(ledger)
    assert payload["row"]["source"] == "synthetic"


def test_dry_run_probes_nothing_and_records_nothing(tmp_path, capsys):
    rc, rows, ledger = _run(tmp_path, "--dry-run")
    assert rc == 0 and not ledger.exists()
    assert "Would probe" in capsys.readouterr().out


def test_build_row_uses_driver_normalized_identity():
    # The maturity.py dedupe contract: identity comes from get_firmware_info
    # (the drivers' normalized path), not from raw device strings.
    import types

    from kvm_pilot.test_report import build_row

    stub = types.SimpleNamespace(
        get_firmware_info=lambda: {"vendor": " gl.inet ", "product": "RM1PE",
                                   "version": "V1.9.1 release1"},
    )
    row = build_row(stub, [], source="real")
    assert row["vendor"] == "gl.inet"
    assert row["product"] == "RM1PE"
    assert row["firmware_version"] == "V1.9.1 release1"
    assert row["run_id"].startswith("real-rm1pe-")
