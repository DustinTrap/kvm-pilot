"""Support-matrix evidence rollup from the run ledger (#102).

The rollup is EVIDENCE (pass/fail counts, last outcomes, never-exercised
capabilities) joined with the #98-derived maturity levels committed in the
shipped firmware registry. It is offline: everything here runs against a list
of records, an env-override file, or the ledger bundled in the package —
never a device.
"""

from __future__ import annotations

import json

from kvm_pilot.support_matrix import DESTRUCTIVE_CAPS, KNOWN_CAPS, load_ledger, rollup


def _run(run_id: str, caps: list[tuple[str, bool]], *, vendor: str = "acme",
         product: str = "KVM1", fw: str = "V1.0", driver: str = "glkvm",
         source: str = "real", utc: str = "2026-07-03T12:00:00Z",
         outcome: str = "") -> dict:
    return {
        "run_id": run_id, "source": source, "vendor": vendor, "product": product,
        "firmware_version": fw, "driver": driver, "utc_date": utc,
        "capabilities": [
            {"capability": c, "passed": p, "outcome": outcome} for c, p in caps
        ],
    }


def test_rollup_groups_per_device_firmware_capability():
    records = [
        _run("r1", [("info", True), ("snapshot", True), ("firmware_update", False)]),
        # A SYNTHETIC run on the same combo — it must NOT change any per-capability
        # evidence (this is live-hardware evidence). It flips snapshot to a fail
        # and adds an info pass; both must be ignored.
        _run("r2", [("info", True), ("snapshot", False)], utc="2026-07-04T09:00:00Z",
             source="synthetic"),
        _run("r3", [("info", True)], fw="V2.0"),  # second combo: same device, new firmware
    ]
    rows = rollup(records)
    assert [(r["vendor"], r["product"], r["firmware_version"]) for r in rows] == [
        ("acme", "KVM1", "V1.0"), ("acme", "KVM1", "V2.0"),
    ]
    v1 = rows[0]
    # runs is the LIVE count; the synthetic run shows only as context.
    assert v1["runs"] == 1 and v1["real_runs"] == 1 and v1["synthetic_runs"] == 1
    assert v1["drivers"] == ["glkvm"]
    # last_run_utc is the last LIVE run — the later synthetic run does not move it.
    assert v1["last_run_utc"] == "2026-07-03T12:00:00Z"
    caps = v1["capabilities"]
    assert caps["info"] == {
        "passes": 1, "fails": 0, "destructive": False, "status": "pass",
        "last_utc": "2026-07-03T12:00:00Z", "last_outcome": "",
    }
    assert caps["snapshot"]["status"] == "pass"           # the synthetic fail is ignored
    assert caps["snapshot"]["fails"] == 0
    assert caps["firmware_update"]["status"] == "fail"    # every live attempt failed
    assert caps["firmware_update"]["destructive"] is True
    assert caps["snapshot"]["destructive"] is False
    # never_exercised preserves KNOWN_CAPS order.
    assert v1["never_exercised"] == [
        c for c in KNOWN_CAPS if c not in ("info", "snapshot", "firmware_update")
    ]
    # An unregistered combo has no derived maturity row (#98) to join.
    assert v1["maturity"] is None
    assert set(DESTRUCTIVE_CAPS) == {"virtual_media", "power", "firmware_update"}


def test_rollup_omits_synthetic_only_combos():
    # A combo that was only ever run synthetically has no live evidence, so it
    # produces no row at all (the healthcheck then reports "unverified").
    rows = rollup([_run("s1", [("info", True)], source="synthetic")])
    assert rows == []


def test_rollup_exact_product_does_not_match_a_sibling():
    # The healthcheck path (exact_product=True) must not let an "RM1PE" device
    # pick up an "RM1" ledger row via the default bidirectional substring.
    records = [
        _run("a", [("info", True)], vendor="gl.inet", product="RM1"),
        _run("b", [("power", True)], vendor="gl.inet", product="RM1PE"),
    ]
    loose = rollup(records, vendor="gl.inet", product="RM1PE")
    assert {r["product"] for r in loose} == {"RM1", "RM1PE"}      # substring pulls both
    exact = rollup(records, vendor="gl.inet", product="RM1PE", exact_product=True)
    assert [r["product"] for r in exact] == ["RM1PE"]            # only THIS device


def test_rollup_dedupes_run_id():
    # A re-submitted run counts once — parity with the wiki ingestion's
    # INSERT OR IGNORE contract (first occurrence wins).
    rec = _run("dup", [("info", True)])
    rows = rollup([rec, dict(rec)])
    assert rows[0]["runs"] == 1
    assert rows[0]["capabilities"]["info"]["passes"] == 1


def test_rollup_filters_vendor_product_and_firmware():
    records = [
        _run("a", [("info", True)], vendor="gl.inet", product="RM1PE"),
        _run("b", [("info", True)], vendor="acme", product="RV1126B", driver="pikvm"),
    ]
    assert len(rollup(records, vendor="GL.iNet")) == 1          # exact, case-insensitive
    assert rollup(records, vendor="nonexistent") == []
    # product is a substring match in BOTH directions:
    assert len(rollup(records, product="RM1")) == 1             # lookup inside row
    assert len(rollup(records, product="Rockchip RV1126B-P EVB")) == 1  # row inside device string
    assert len(rollup(records, firmware_version="v1.0")) == 2   # exact, case-insensitive
    assert rollup(records, firmware_version="V9.9") == []
    assert [r["product"] for r in rollup(records, driver="pikvm")] == ["RV1126B"]


def test_load_ledger_env_override_and_skips_bad_lines(tmp_path, monkeypatch):
    good = _run("ok", [("info", True)])
    path = tmp_path / "ledger.jsonl"
    path.write_text(json.dumps(good) + "\nnot json at all\n\n", encoding="utf-8")
    monkeypatch.setenv("KVM_PILOT_TEST_LEDGER", str(path))
    assert load_ledger() == [good]  # override wins; the bad line is skipped
    # A missing override file degrades to "no evidence", never an error.
    monkeypatch.setenv("KVM_PILOT_TEST_LEDGER", str(tmp_path / "missing.jsonl"))
    assert load_ledger() == []


def test_bundled_ledger_ships_with_rm1pe_seed(monkeypatch):
    # No override: the ledger packaged under src/kvm_pilot/data/ answers offline —
    # the acceptance data for #102 travels with `pip install kvm-pilot`.
    monkeypatch.delenv("KVM_PILOT_TEST_LEDGER", raising=False)
    assert load_ledger()  # importlib.resources found the packaged copy
    rows = rollup(product="RM1PE")
    by_fw = {r["firmware_version"]: r for r in rows}
    assert {"V1.5.1 release2", "V1.9.1 release1"} <= set(by_fw)
    old = by_fw["V1.5.1 release2"]
    # The seeded live firmware-update failure (#94/#95) must surface as a fail.
    assert old["capabilities"]["firmware_update"]["status"] == "fail"
    assert "#94" in old["capabilities"]["firmware_update"]["last_outcome"]
    # ... and the #98-derived maturity from the shipped registry rides along.
    assert old["maturity"]["capabilities"]["firmware_update"] == "alpha"
    assert by_fw["V1.9.1 release1"]["maturity"]["level"] == "beta"
