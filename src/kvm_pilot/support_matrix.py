"""
Support-matrix evidence: what has actually been exercised on real hardware
(issue #102, part of #96).

Aggregates the test-run ledger shipped in the package
(``src/kvm_pilot/data/test_runs.jsonl`` — the same data behind the wiki
Hardware-Compatibility page) into per ``(vendor, product, firmware_version)``
× capability EVIDENCE: pass/fail counts, the last outcome, and which known
capabilities were never exercised live. Each combo row also carries the
**derived maturity levels** (#98) that ``kvm_pilot.maturity`` computed from
this same ledger and folded into the shipped firmware registry — this module
never derives levels itself (that is #98's job, guarded by a CI drift check);
it only joins the committed result in.

Evidence is not a guarantee: a capability in ``never_exercised`` (or a combo
with no row at all) is unverified on that hardware — treat it as mock-only
maturity. Stdlib-only, offline: it never contacts a device.

Consumers: the MCP ``support_matrix`` tool, the MCP ``capabilities`` tool's
``live_evidence`` annotation, and ``health.check_support_evidence``.
"""

from __future__ import annotations

import json
import os
from typing import Any

# Keep in sync with .github/scripts/build_wiki.py HCL_CAPS (same order, same
# destructive flags) — the wiki page and this module must tell one story.
KNOWN_CAPS: list[str] = [
    "info", "snapshot", "healthcheck", "logs", "power_state",
    "virtual_media", "power", "firmware_update",
]
DESTRUCTIVE_CAPS = frozenset({"virtual_media", "power", "firmware_update"})

# A capability's last recorded outcome, truncated for report/detail strings.
_OUTCOME_MAX = 120


def load_ledger() -> list[dict[str, Any]]:
    """Parse the run ledger: ``KVM_PILOT_TEST_LEDGER`` override > bundled copy.

    Skips blank/unparseable lines; returns ``[]`` when the source is missing or
    unreadable — evidence lookup degrades to "nothing verified", never an error.
    """
    override = os.environ.get("KVM_PILOT_TEST_LEDGER")
    try:
        if override:
            from pathlib import Path

            raw = Path(override).expanduser().read_text("utf-8")
        else:
            from importlib.resources import files

            raw = (files("kvm_pilot") / "data" / "test_runs.jsonl").read_text("utf-8")
    except (OSError, ModuleNotFoundError):
        return []
    records: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict):
            records.append(rec)
    return records


def _registry_maturity() -> dict[tuple[str, str, str], dict[str, Any]]:
    """The derived maturity rows (#98) committed in the shipped registry,
    keyed case-insensitively — the join source for ``rollup``'s ``maturity``."""
    from .firmware_registry import load_registry
    from .maturity import registry_matrix

    return {
        (v.strip().lower(), p.strip().lower(), fw.strip().lower()): m
        for (v, p, fw), m in registry_matrix(load_registry()).items()
    }


def rollup(
    records: list[dict[str, Any]] | None = None,
    *,
    vendor: str | None = None,
    product: str | None = None,
    firmware_version: str | None = None,
    driver: str | None = None,
) -> list[dict[str, Any]]:
    """Aggregate ledger records into per-combo evidence rows (JSON-safe).

    Dedupes on ``run_id`` (first wins — the same contract as the wiki page's
    ``INSERT OR IGNORE`` ingestion). Filters: ``vendor``/``firmware_version``
    exact case-insensitive; ``product`` case-insensitive substring in either
    direction (so ``RM1`` finds ``RM1PE`` and a messy device board string finds
    its registry-style short name); ``driver`` exact.

    Each row:  ``vendor``/``product``/``firmware_version``, the ``drivers``
    that produced runs, run counts (``runs``/``real_runs``/``synthetic_runs``),
    ``last_run_utc``, per-capability ``{passes, fails, status, destructive,
    last_utc, last_outcome}``, ``never_exercised`` (``KNOWN_CAPS`` order), and
    ``maturity`` — the #98-derived levels from the shipped registry (``None``
    when the registry has no derived row for the combo).
    """
    if records is None:
        records = load_ledger()

    seen: set[str] = set()
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for rec in records:
        run_id = rec.get("run_id")
        if run_id is not None:
            if run_id in seen:
                continue
            seen.add(run_id)
        v = str(rec.get("vendor", "")).strip()
        p = str(rec.get("product", "")).strip()
        fw = str(rec.get("firmware_version", "")).strip()
        if not (v and p and fw):
            continue
        if vendor is not None and v.lower() != vendor.strip().lower():
            continue
        if product is not None:
            have, want = p.lower(), product.strip().lower()
            if want not in have and have not in want:
                continue
        if firmware_version is not None and fw.lower() != firmware_version.strip().lower():
            continue
        if driver is not None and rec.get("driver") != driver:
            continue
        groups.setdefault((v, p, fw), []).append(rec)

    maturity = _registry_maturity()
    rows: list[dict[str, Any]] = []
    for key in sorted(groups):
        v, p, fw = key
        recs = groups[key]
        caps: dict[str, dict[str, Any]] = {}
        for rec in recs:
            when = str(rec.get("utc_date", ""))
            for c in rec.get("capabilities", []) or []:
                name = str(c.get("capability", "")).strip()
                if not name:
                    continue
                cap = caps.setdefault(name, {
                    "passes": 0, "fails": 0, "destructive": name in DESTRUCTIVE_CAPS,
                    "last_utc": "", "last_outcome": "",
                })
                cap["passes" if c.get("passed") else "fails"] += 1
                if when >= cap["last_utc"]:
                    cap["last_utc"] = when
                    cap["last_outcome"] = str(c.get("outcome", ""))[:_OUTCOME_MAX]
        for cap in caps.values():
            cap["status"] = (
                "pass" if not cap["fails"] else "fail" if not cap["passes"] else "mixed"
            )
        real = sum(1 for r in recs if r.get("source") == "real")
        rows.append({
            "vendor": v,
            "product": p,
            "firmware_version": fw,
            "drivers": sorted({str(r["driver"]) for r in recs if r.get("driver")}),
            "runs": len(recs),
            "real_runs": real,
            "synthetic_runs": len(recs) - real,
            "last_run_utc": max(str(r.get("utc_date", "")) for r in recs),
            "capabilities": caps,
            "never_exercised": [c for c in KNOWN_CAPS if c not in caps],
            "maturity": maturity.get((v.lower(), p.lower(), fw.lower())),
        })
    return rows
