"""Tests for the firmware-registry ingestion pipeline (issue #80 follow-up)."""

from __future__ import annotations

import json

from kvm_pilot.firmware_registry import (
    _FIELD_LABELS,
    ingest_batch,
    load_bundled_registry,
    load_registry,
    main,
    merge_submission,
    parse_issue_form,
    validate_registry,
    validate_submission,
)

_LABEL_FOR = {v: k for k, v in _FIELD_LABELS.items()}


def body(**fields: str) -> str:
    """Render an issue body the way a GitHub Issue Form would, from internal keys."""
    return "\n".join(f"### {_LABEL_FOR[k]}\n\n{v}\n" for k, v in fields.items())


def _empty_registry() -> dict:
    return {"schema_version": 2, "updated": "2026-01-01", "firmware": []}


# ---- parsing -------------------------------------------------------------- #


def test_parse_issue_form_roundtrips_and_drops_no_response():
    b = body(vendor="gl.inet", product="RV1126B", kind="Latest known release",
             latest="4.90", date="2026-05-29", source="https://x")
    b += "\n### Fixed in (optional, known-bad)\n\n_No response_\n"
    sub = parse_issue_form(b)
    assert sub["vendor"] == "gl.inet" and sub["latest"] == "4.90"
    assert "fixed_in" not in sub  # _No response_ is dropped


# ---- submission validation ------------------------------------------------ #


def test_latest_requires_source_and_date():
    errs = validate_submission({"vendor": "v", "product": "p", "kind": "Latest known release", "latest": "1.0"})
    assert any("source" in e for e in errs) and any("date" in e for e in errs)


def test_known_bad_requires_affected_severity_issue():
    errs = validate_submission({"vendor": "v", "product": "p", "kind": "Known-bad firmware", "source": "https://x"})
    assert any("affected" in e for e in errs)
    assert any("severity" in e for e in errs)
    assert any("issue" in e for e in errs)


def test_profile_needs_at_least_one_field():
    errs = validate_submission({"vendor": "v", "product": "p", "kind": "Capability profile"})
    assert any("at least one" in e for e in errs)


def test_profile_source_optional_but_validated():
    ok = validate_submission({"vendor": "v", "product": "p", "kind": "Capability profile", "mouse": "absolute"})
    assert ok == []
    bad = validate_submission({"vendor": "v", "product": "p", "kind": "Capability profile",
                               "mouse": "absolute", "source": "not-a-url"})
    assert any("source" in e for e in bad)


# ---- merge ---------------------------------------------------------------- #


def test_merge_latest_creates_entry_and_validates():
    out = merge_submission(_empty_registry(), {
        "vendor": "gl.inet", "product": "RV1126B", "kind": "Latest known release",
        "latest": "4.90", "date": "2026-05-29", "source": "https://x"}, today="2026-07-02")
    assert out["firmware"][0]["latest"] == "4.90" and out["updated"] == "2026-07-02"
    assert validate_registry(out) == []


def test_profile_merges_field_by_field_across_reports():
    reg = _empty_registry()
    reg = merge_submission(reg, {"vendor": "gl.inet", "product": "RV1126B",
                                 "kind": "Capability profile", "mouse": "absolute",
                                 "power_state_trusted": "false"}, today="2026-07-02")
    reg = merge_submission(reg, {"vendor": "gl.inet", "product": "RV1126B",
                                 "kind": "Capability profile", "vmedia": "reports-only"}, today="2026-07-03")
    assert reg["firmware"][0]["profile"] == {
        "mouse": "absolute", "power_state_trusted": False, "vmedia": "reports-only"}
    assert validate_registry(reg) == []


def test_known_bad_replaces_same_affected_range():
    reg = _empty_registry()
    kb = {"vendor": "gl.inet", "product": "RV1126B", "kind": "Known-bad firmware",
          "affected": "<=4.82", "severity": "warning", "issue": "x", "source": "https://x"}
    reg = merge_submission(reg, kb, today="2026-07-02")
    reg = merge_submission(reg, {**kb, "severity": "critical"}, today="2026-07-03")
    bads = reg["firmware"][0]["known_bad"]
    assert len(bads) == 1 and bads[0]["severity"] == "critical"


# ---- bundled data + end-to-end CLI --------------------------------------- #


def test_bundled_registry_is_schema_valid():
    assert validate_registry(load_bundled_registry()) == []


def test_loader_prefers_valid_override_over_bundled(tmp_path, monkeypatch):
    db = tmp_path / "db.json"
    db.write_text(json.dumps({"schema_version": 2, "updated": "2026-07-02", "firmware": [
        {"vendor": "acme", "product": "widget"}]}))
    monkeypatch.setenv("KVM_PILOT_FIRMWARE_DB", str(db))
    assert load_registry()["firmware"] == [{"vendor": "acme", "product": "widget"}]


def test_loader_falls_back_when_override_invalid(tmp_path, monkeypatch):
    db = tmp_path / "db.json"
    db.write_text(json.dumps({"schema_version": 99, "firmware": "nope"}))  # invalid
    monkeypatch.setenv("KVM_PILOT_FIRMWARE_DB", str(db))
    assert load_registry() == load_bundled_registry()  # bundled, not the bad override


def test_main_ingests_then_is_idempotent(tmp_path):
    reg = tmp_path / "reg.json"
    reg.write_text(json.dumps(_empty_registry()))
    bf = tmp_path / "body.txt"
    bf.write_text(body(vendor="gl.inet", product="RV1126B", kind="Known-bad firmware",
                       affected="<=4.82", severity="critical", issue="ATX unwired", source="https://x"))
    assert main(["--issue-body-file", str(bf), "--registry", str(reg), "--today", "2026-07-02"]) == 0
    data = json.loads(reg.read_text())
    assert data["firmware"][0]["known_bad"][0]["severity"] == "critical"
    # Re-running the same submission changes nothing -> no-op exit 4.
    assert main(["--issue-body-file", str(bf), "--registry", str(reg), "--today", "2026-07-02"]) == 4


def test_main_rejects_invalid_submission(tmp_path):
    reg = tmp_path / "reg.json"
    reg.write_text(json.dumps(_empty_registry()))
    bf = tmp_path / "body.txt"
    bf.write_text(body(vendor="v", product="p", kind="Latest known release", latest="1.0"))  # no source/date
    assert main(["--issue-body-file", str(bf), "--registry", str(reg), "--today", "2026-07-02"]) == 3
    assert json.loads(reg.read_text()) == _empty_registry()  # untouched


# ---- batch (hourly workflow) --------------------------------------------- #


def test_ingest_batch_mixed_results_land_on_one_entry():
    items = [
        {"number": 1, "body": body(vendor="gl.inet", product="RV1126B", kind="Latest known release",
                                    latest="4.90", date="2026-05-29", source="https://x")},
        {"number": 2, "body": body(vendor="v", product="p", kind="Latest known release", latest="1.0")},  # invalid
        {"number": 3, "body": body(vendor="gl.inet", product="RV1126B", kind="Capability profile", mouse="absolute")},
    ]
    merged, results = ingest_batch(_empty_registry(), items, today="2026-07-02")
    assert {r["number"]: r["status"] for r in results} == {1: "ingested", 2: "invalid", 3: "ingested"}
    e = merged["firmware"][0]
    assert e["latest"] == "4.90" and e["profile"]["mouse"] == "absolute"
    assert validate_registry(merged) == []


def test_ingest_batch_dedup_is_noop_on_rerun():
    item = {"number": 5, "body": body(vendor="gl.inet", product="RV1126B", kind="Known-bad firmware",
                                      affected="<=4.82", severity="critical", issue="x", source="https://x")}
    reg, r1 = ingest_batch(_empty_registry(), [item], today="2026-07-02")
    assert r1[0]["status"] == "ingested"
    _, r2 = ingest_batch(reg, [item], today="2026-07-02")
    assert r2[0]["status"] == "noop"


def test_main_batch_mode_writes_registry_and_results(tmp_path):
    reg = tmp_path / "reg.json"
    reg.write_text(json.dumps(_empty_registry()))
    items = tmp_path / "items.json"
    items.write_text(json.dumps([{"number": 1, "body": body(
        vendor="gl.inet", product="RV1126B", kind="Latest known release",
        latest="4.90", date="2026-05-29", source="https://x")}]))
    res = tmp_path / "results.json"
    rc = main(["--batch", str(items), "--registry", str(reg), "--today", "2026-07-02", "--results", str(res)])
    assert rc == 0
    assert json.loads(reg.read_text())["firmware"][0]["latest"] == "4.90"
    assert json.loads(res.read_text())[0]["status"] == "ingested"


# ---- reconcile (device-reported latest vs the SSoT) ---------------------- #


def test_reconcile_flags_when_registry_missing_latest():
    from kvm_pilot.firmware_registry import reconcile

    reg = {"schema_version": 2, "updated": "2026-01-01",
           "firmware": [{"vendor": "gl.inet", "product": "RM1PE"}]}  # profile only, no latest
    sub = reconcile("gl.inet", "RM1PE", "V1.9.1 release1", registry=reg)
    assert sub == {"vendor": "gl.inet", "product": "RM1PE",
                   "kind": "Latest known release", "latest": "V1.9.1 release1"}


def test_reconcile_none_when_ssot_current_or_ahead():
    from kvm_pilot.firmware_registry import reconcile

    reg = {"schema_version": 2, "updated": "2026-01-01",
           "firmware": [{"vendor": "gl.inet", "product": "RM1PE", "latest": "V1.9.1 release1"}]}
    assert reconcile("gl.inet", "RM1PE", "V1.9.1 release1", registry=reg) is None      # equal
    assert reconcile("gl.inet", "RM1PE", "V1.9.0 release1", registry=reg) is None      # device older
    assert reconcile("gl.inet", "RM1PE", "V1.9.2 release1", registry=reg) is not None  # newer -> drift
