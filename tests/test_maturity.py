"""Maturity derivation from the run ledger (#98).

Levels are DERIVED, never hand-set: ``compute_maturity`` applies the promotion
ladder (alpha -> beta -> rc -> ga) to a combo's live run history,
``fold_into_registry`` writes the derived rows into ``versions[].maturity``,
and ``test_committed_registry_matches_ledger`` is the CI gate that re-derives
from the real ledger and fails on any hand-edit of the committed registry.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from kvm_pilot.firmware_registry import validate_registry
from kvm_pilot.maturity import (
    REGEN_COMMAND,
    compute_matrix,
    compute_maturity,
    drift,
    fold_into_registry,
    load_ledger,
    main,
    registry_matrix,
)

_ROOT = Path(__file__).resolve().parents[1]
_LEDGER = _ROOT / "src" / "kvm_pilot" / "data" / "test_runs.jsonl"
_REGISTRY = _ROOT / "src" / "kvm_pilot" / "data" / "firmware_registry.json"


def _run(day: str, caps: list[tuple[str, bool]], *, source: str | None = "real",
         fw: str = "V1.0", run_id: str | None = None) -> dict:
    rec = {
        "run_id": run_id or f"r-{day}-{len(caps)}-{caps[0]}",
        "vendor": "acme", "product": "KVM1", "firmware_version": fw,
        "utc_date": f"{day}T12:00:00Z",
        "capabilities": [{"capability": c, "passed": p, "outcome": ""} for c, p in caps],
    }
    if source is not None:
        rec["source"] = source
    return rec


# --------------------------------------------------------------------------- #
# The promotion ladder                                                        #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("events", "expected"),
    [
        # no records at all -> alpha
        ([], "alpha"),
        # 1 live pass -> beta
        ([("2026-07-01", True)], "beta"),
        # 3 passes but on a single distinct date -> beta (rc's date gate)
        ([("2026-07-01", True)] * 3, "beta"),
        # 3 passes across 2 distinct dates -> rc
        ([("2026-07-01", True), ("2026-07-01", True), ("2026-07-02", True)], "rc"),
        # 5 passes spanning only 13 days -> rc (ga's span gate)
        ([(f"2026-07-{d:02d}", True) for d in (1, 4, 7, 10, 14)], "rc"),
        # 5 passes spanning 14 days, zero failures -> ga
        ([(f"2026-07-{d:02d}", True) for d in (1, 4, 7, 10, 15)], "ga"),
        # an interleaved failure resets the ga window -> not ga (still rc)
        ([("2026-07-01", True), ("2026-07-02", False)]
         + [(f"2026-07-{d:02d}", True) for d in (3, 4, 7, 10, 15)], "rc"),
        # a failure AFTER a qualifying ga history demotes (window is empty) -> rc
        ([(f"2026-07-{d:02d}", True) for d in (1, 4, 7, 10, 15)]
         + [("2026-07-16", False)], "rc"),
    ],
    ids=["alpha-empty", "beta-one-pass", "beta-single-date", "rc-two-dates",
         "rc-13-day-span", "ga-14-day-span", "ga-window-reset", "fail-demotes-ga"],
)
def test_promotion_boundaries(events: list[tuple[str, bool]], expected: str) -> None:
    records = [_run(day, [("snapshot", ok)], run_id=f"r{i}") for i, (day, ok) in enumerate(events)]
    result = compute_maturity(records)
    assert result["capabilities"].get("snapshot", "alpha") == expected


def test_synthetic_runs_never_promote() -> None:
    records = [
        _run("2026-07-01", [("info", True)], source="synthetic", run_id="s1"),
        _run("2026-07-02", [("info", True)], source=None, run_id="s2"),  # missing source
    ]
    assert compute_maturity(records) == {"level": "alpha", "capabilities": {}}
    assert compute_matrix(records) == {}  # mock-only combos get no matrix row


def test_failed_only_capability_stays_alpha() -> None:
    # The real firmware_update case (#94): a live run that only ever failed.
    records = [_run("2026-07-03", [("firmware_update", False)], run_id="f1")]
    assert compute_maturity(records)["capabilities"]["firmware_update"] == "alpha"


def test_overall_level_is_min_of_capabilities() -> None:
    records = [
        _run("2026-07-01", [("info", True), ("firmware_update", False)], run_id="m1"),
        _run("2026-07-02", [("info", True)], run_id="m2"),
        _run("2026-07-03", [("info", True)], run_id="m3"),
    ]
    result = compute_maturity(records)
    assert result["capabilities"] == {"firmware_update": "alpha", "info": "rc"}
    assert result["level"] == "alpha"  # min() over every exercised capability


# --------------------------------------------------------------------------- #
# Writer                                                                      #
# --------------------------------------------------------------------------- #


def _minimal_registry() -> dict:
    return {
        "schema_version": 2,
        "updated": "2026-07-01",
        "firmware": [{"vendor": "acme", "product": "KVM1"}],
    }


def test_fold_writes_derived_version_rows_and_validates() -> None:
    registry = _minimal_registry()
    matrix = compute_matrix([
        _run("2026-07-01", [("info", True)], fw="V1.9", run_id="a"),
        _run("2026-07-02", [("info", True)], fw="V1.10", run_id="b"),
    ])
    folded = fold_into_registry(registry, matrix)
    assert validate_registry(folded) == []
    assert registry == _minimal_registry()  # input not mutated
    assert folded["updated"] == "2026-07-01"  # a recompute never bumps "updated"
    versions = folded["firmware"][0]["versions"]
    assert [v["version"] for v in versions] == ["V1.9", "V1.10"]  # numeric version order
    assert versions[0]["maturity"] == {"level": "beta", "capabilities": {"info": "beta"}}
    assert fold_into_registry(folded, matrix) == folded  # idempotent


def test_fold_creates_entry_for_unknown_device() -> None:
    matrix = compute_matrix([_run("2026-07-01", [("info", True)], run_id="n1")])
    folded = fold_into_registry({"schema_version": 2, "updated": "2026-07-01",
                                 "firmware": []}, matrix)
    assert validate_registry(folded) == []
    assert folded["firmware"] == [{
        "vendor": "acme", "product": "KVM1",
        "versions": [{"version": "V1.0",
                      "maturity": {"level": "beta", "capabilities": {"info": "beta"}}}],
    }]


# --------------------------------------------------------------------------- #
# Drift — the CI gate                                                         #
# --------------------------------------------------------------------------- #


def test_committed_registry_matches_ledger() -> None:
    # THE CI drift gate: the committed registry must equal what the committed
    # ledger derives. If this fails, someone hand-edited a maturity level (or
    # appended ledger runs without re-deriving).
    matrix = compute_matrix(load_ledger(_LEDGER))
    registry = json.loads(_REGISTRY.read_text("utf-8"))
    assert drift(registry, matrix) == [], f"registry maturity drifted; {REGEN_COMMAND}"


def test_hand_edited_level_is_reported_as_drift() -> None:
    registry = json.loads(_REGISTRY.read_text("utf-8"))
    matrix = compute_matrix(load_ledger(_LEDGER))
    row = registry["firmware"][0]["versions"][0]
    row["maturity"]["capabilities"]["snapshot"] = "ga"  # the forbidden hand-bump
    messages = drift(registry, matrix)
    assert len(messages) == 1
    assert "'snapshot' is 'ga' in the registry but the ledger derives 'beta'" in messages[0]
    assert "V1.5.1 release2" in messages[0]
    assert REGEN_COMMAND in messages[0]


def test_drift_reports_unbacked_and_missing_rows() -> None:
    registry = _minimal_registry()
    registry["firmware"][0]["versions"] = [
        {"version": "V0.9", "maturity": {"level": "beta", "capabilities": {}}},
    ]
    matrix = compute_matrix([_run("2026-07-01", [("info", True)], run_id="d1")])
    messages = drift(registry, matrix)
    assert any("V0.9" in m and "does not back" in m for m in messages)
    assert any("V1.0" in m and "missing from the registry" in m for m in messages)


# --------------------------------------------------------------------------- #
# CLI                                                                         #
# --------------------------------------------------------------------------- #


def test_main_write_then_check_roundtrip(tmp_path: Path, capsys) -> None:
    ledger = tmp_path / "runs.jsonl"
    shutil.copy(_LEDGER, ledger)
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps(_minimal_registry()), encoding="utf-8")

    assert main(["--ledger", str(ledger), "--registry", str(registry), "--write"]) == 0
    written = json.loads(registry.read_text("utf-8"))
    assert registry_matrix(written)  # derived rows landed
    assert main(["--ledger", str(ledger), "--registry", str(registry), "--check"]) == 0
    assert main(["--ledger", str(ledger), "--registry", str(registry), "--write"]) == 4  # no-op

    written["firmware"][-1]["versions"][0]["maturity"]["level"] = "ga"  # corrupt one level
    registry.write_text(json.dumps(written), encoding="utf-8")
    capsys.readouterr()
    assert main(["--ledger", str(ledger), "--registry", str(registry), "--check"]) == 5
    assert "'ga' in the registry but the ledger derives" in capsys.readouterr().out


def test_validate_rejects_unknown_maturity_level() -> None:
    registry = _minimal_registry()
    registry["firmware"][0]["versions"] = [
        {"version": "V1.0", "maturity": {"level": "solid", "capabilities": {"info": "great"}}},
    ]
    errors = validate_registry(registry)
    assert any("versions[0]" in e and "maturity.level" in e for e in errors)
    assert any("maturity.capabilities['info']" in e for e in errors)


def test_conditions_field_never_moves_a_derived_level() -> None:
    # #156 adds optional per-capability ``conditions`` axes to ledger rows.
    # Derivation must ignore them entirely — otherwise recording honest
    # conditions would churn the committed registry through the CI drift gate.
    bare = [_run("2026-07-01", [("snapshot", True)]),
            _run("2026-07-02", [("snapshot", True)], run_id="r2"),
            _run("2026-07-03", [("snapshot", True)], run_id="r3")]
    conditioned = json.loads(json.dumps(bare))
    for rec in conditioned:
        rec["capabilities"][0]["conditions"] = {
            "resolution": "2560x1440", "encoder_format": "h264",
            "snapshot_cached": False, "jpeg_sink_clients": False,
        }
    assert compute_maturity(bare) == compute_maturity(conditioned)
