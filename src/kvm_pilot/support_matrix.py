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

**Only real-hardware runs count as evidence** (``source == "real"``), matching
what ``kvm_pilot.maturity`` promotes on: a synthetic/emulator run exercises the
code path but proves nothing about the device, so it never contributes a pass,
a fail, or an "exercised" mark. Combos that were only ever run synthetically
produce no row at all. Run counts still report the synthetic total for context.

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
# Capabilities whose outcome depends on operating conditions (#156): evidence
# recorded without a `conditions` dict gets the condition-blind caveat (#180).
CONDITION_SENSITIVE_CAPS = frozenset({"snapshot"})

# A capability's last recorded outcome, truncated for report/detail strings.
_OUTCOME_MAX = 120


def _condition_summary(cond: Any) -> str | None:
    """Render a capability row's ``conditions`` dict (#156) as one short string.

    The axes that decide a snapshot outcome on GL hardware (resolution ×
    encoder mode × cache/sink state) are recorded per result so two field
    reports at different operating points reconcile from data instead of
    reading as contradictions. E.g. ``"2560x1440 h264, cached, no-jpeg-clients"``.
    """
    if not isinstance(cond, dict) or not cond:
        return None
    parts: list[str] = []
    head = " ".join(
        str(cond[k]) for k in ("resolution", "encoder_format") if cond.get(k)
    )
    if head:
        parts.append(head)
    if "snapshot_cached" in cond:
        parts.append("cached" if cond["snapshot_cached"] else "uncached")
    if "jpeg_sink_clients" in cond:
        parts.append("jpeg-clients" if cond["jpeg_sink_clients"] else "no-jpeg-clients")
    # The schema is open ("MAY additionally carry", docs/test-plan.md §9): an
    # axis added after today must degrade to visible k=v, never vanish.
    known = {"resolution", "encoder_format", "snapshot_cached", "jpeg_sink_clients"}
    parts.extend(f"{k}={cond[k]}" for k in sorted(set(cond) - known))
    return ", ".join(parts) or None


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
    exact_product: bool = False,
) -> list[dict[str, Any]]:
    """Aggregate ledger records into per-combo *live* evidence rows (JSON-safe).

    Dedupes on ``run_id`` (first wins — the same contract as the wiki page's
    ``INSERT OR IGNORE`` ingestion). Filters: ``vendor``/``firmware_version``
    exact case-insensitive; ``product`` case-insensitive substring in either
    direction by default (so ``RM1`` finds ``RM1PE`` and a messy device board
    string finds its registry-style short name) — pass ``exact_product=True``
    for an exact match (the healthcheck wants THIS device, not a substring
    sibling); ``driver`` exact.

    **Per-capability evidence counts real-hardware runs only** (``source ==
    "real"``): a synthetic run never contributes a pass, a fail, or an
    "exercised" mark. A combo with no real runs produces no row.

    Each row:  ``vendor``/``product``/``firmware_version``, the ``drivers``
    that produced live runs, run counts (``runs``/``real_runs``/
    ``synthetic_runs`` — ``runs`` is the real count; ``synthetic_runs`` is
    context only), ``last_run_utc``, per-capability ``{passes, fails, status,
    destructive, last_utc, last_outcome}`` — plus ``pass_conditions``/
    ``fail_conditions`` (deduped #156 condition summaries, keys present only
    when some record carried ``conditions``) — ``never_exercised``
    (``KNOWN_CAPS`` order), and ``maturity`` — the #98-derived levels from the
    shipped registry (``None`` when the registry has no derived row for the
    combo).
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
            if exact_product:
                if have != want:
                    continue
            elif want not in have and have not in want:
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
        real_recs = [r for r in recs if r.get("source") == "real"]
        if not real_recs:
            continue  # no live evidence for this combo -> no row (see docstring)
        caps: dict[str, dict[str, Any]] = {}
        for rec in real_recs:
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
                summary = _condition_summary(c.get("conditions"))
                if summary:  # optional #156 axes; absent on pre-#156 rows
                    bucket = "pass_conditions" if c.get("passed") else "fail_conditions"
                    if summary not in cap.setdefault(bucket, []):
                        cap[bucket].append(summary)
        for cap in caps.values():
            cap["status"] = (
                "pass" if not cap["fails"] else "fail" if not cap["passes"] else "mixed"
            )
        rows.append({
            "vendor": v,
            "product": p,
            "firmware_version": fw,
            "drivers": sorted({str(r["driver"]) for r in real_recs if r.get("driver")}),
            "runs": len(real_recs),
            "real_runs": len(real_recs),
            "synthetic_runs": len(recs) - len(real_recs),
            "last_run_utc": max(str(r.get("utc_date", "")) for r in real_recs),
            "capabilities": caps,
            "never_exercised": [c for c in KNOWN_CAPS if c not in caps],
            "maturity": maturity.get((v.lower(), p.lower(), fw.lower())),
        })
    return rows
