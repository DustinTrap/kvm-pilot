"""
Support-matrix maturity: derive per-capability / overall levels from the run
ledger (issue #98, part of #96).

Maturity levels are **DERIVED, never hand-set**. The input is the append-only
run ledger (``src/kvm_pilot/data/test_runs.jsonl``, shipped in the wheel since
#102); the output is the ``versions[].maturity`` rows inside the shipped
firmware registry (``src/kvm_pilot/data/firmware_registry.json``), so installed
consumers (#102) read maturity from ``load_registry()`` without re-deriving it.
CI re-derives the levels and fails on drift, so a hand-edited level cannot
survive a pull request.

Promotion policy (issue #98's starter constants — editable, see below), applied
per ``(vendor, product, firmware_version)`` combo and per capability, counting
only **live** runs (``source == "real"``; synthetic/mock runs never promote):

* ``alpha`` — no live passes (mocks only, or live failures only).
* ``beta``  — at least ``BETA_MIN_PASSES`` live passes.
* ``rc``    — at least ``RC_MIN_PASSES`` live passes across at least
  ``RC_MIN_DISTINCT_DATES`` distinct UTC calendar dates.
* ``ga``    — at least ``GA_MIN_PASSES`` live passes spanning at least
  ``GA_MIN_SPAN_DAYS`` days, **all after the capability's most recent live
  failure** (a new failure resets the ga window).

Interpretation decisions (recorded on issue #98):

1. *"live"* means ``source == "real"`` in the ledger; a record with a missing
   or different ``source`` contributes nothing.
2. The issue's rc rule "incl. any destructive path" is read as *destructive
   capabilities use the same ladder* — deliberateness of a destructive live run
   is enforced upstream by the harness's explicit include gate (#99), so any
   destructive pass in the ledger was deliberate. rc does not *require* a
   destructive pass.
3. The issue's ga rule "zero failures in that window" is read as: the
   qualifying >=5-pass / >=14-day run must sit entirely **after** the most
   recent live failure (dates compare at UTC calendar-day granularity, the
   ledger's ``utc_date`` truncated to a date).
4. The registry stores **every exercised capability's level explicitly** (not
   the epic's "overrides of overall" shape) — explicit output keeps the drift
   check a plain dict comparison. There is no hand-override mechanism yet; if
   one is ever needed it must be a separate input, never an edit to the
   derived output.

Note for #99: the writer keys entries on exact case-insensitive
``(vendor, product)`` via ``_find_entry``; a harness that logs a raw board
string as ``product`` would create a duplicate registry entry, so #99 must
normalize ``product`` the way the drivers do.

Stdlib-only, like the rest of the library core.
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .firmware_registry import (
    MATURITY_LEVELS,
    _find_entry,
    _ver_tuple,
    _write_registry,
    validate_registry,
)

# Promotion thresholds (issue #98's "starter — editable" policy constants).
BETA_MIN_PASSES = 1          # >=1 live pass
RC_MIN_PASSES = 3            # >=3 live passes ...
RC_MIN_DISTINCT_DATES = 2    # ... across >=2 distinct UTC calendar dates
GA_MIN_PASSES = 5            # >=5 live passes ...
GA_MIN_SPAN_DAYS = 14        # ... spanning >=14 days, zero failures in the window

# The command that regenerates the derived rows when drift is reported.
REGEN_COMMAND = (
    "python -m kvm_pilot.maturity --ledger src/kvm_pilot/data/test_runs.jsonl "
    "--registry src/kvm_pilot/data/firmware_registry.json --write"
)

# (vendor, product, firmware_version) — one row of the support matrix.
ComboKey = tuple[str, str, str]


# --------------------------------------------------------------------------- #
# Ledger                                                                      #
# --------------------------------------------------------------------------- #


def load_ledger(path: Path) -> list[dict[str, Any]]:
    """Parse the JSONL run ledger; skip blank lines; dedupe by ``run_id``.

    First occurrence of a ``run_id`` wins — the same contract as the wiki
    builder's ``INSERT OR IGNORE`` ingestion, so both consumers see one run
    exactly once even if an append is accidentally repeated.
    """
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in path.read_text("utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue  # a malformed append must not crash the CI drift gate
        if not isinstance(rec, dict):
            continue
        run_id = rec.get("run_id")
        if run_id is not None:
            if run_id in seen:
                continue
            seen.add(run_id)
        records.append(rec)
    return records


# --------------------------------------------------------------------------- #
# The promotion ladder (pure)                                                 #
# --------------------------------------------------------------------------- #


def _parse_utc_date(value: Any) -> date | None:
    """A ledger record's UTC calendar day, or None if missing/unparseable.

    A real-source run with no/garbage ``utc_date`` cannot be placed in a
    promotion window, so it is dropped rather than crashing the derivation (and
    the CI drift gate that runs it) with a KeyError/ValueError.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None


def _capability_level(events: list[tuple[date, bool]]) -> str:
    """One capability's maturity from its live ``(utc_date, passed)`` history."""
    passes = sorted(d for d, ok in events if ok)
    if len(passes) < BETA_MIN_PASSES:
        return "alpha"
    fails = [d for d, ok in events if not ok]
    last_fail = max(fails) if fails else None
    # ga considers only passes strictly after the most recent live failure —
    # a failure resets the window (interpretation decision 3).
    window = [d for d in passes if last_fail is None or d > last_fail]
    if len(window) >= GA_MIN_PASSES and (window[-1] - window[0]).days >= GA_MIN_SPAN_DAYS:
        return "ga"
    if len(passes) >= RC_MIN_PASSES and len(set(passes)) >= RC_MIN_DISTINCT_DATES:
        return "rc"
    return "beta"


def compute_maturity(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive one combo's maturity from its ledger records (the pure function).

    Returns ``{"level": <overall>, "capabilities": {cap: level, ...}}`` —
    exactly the shape stored under ``versions[].maturity`` in the registry.
    Only live records (``source == "real"``) count; the overall ``level`` is
    the minimum of the exercised capabilities' levels (``alpha`` when nothing
    was exercised live).
    """
    events: dict[str, list[tuple[date, bool]]] = {}
    for rec in records:
        if rec.get("source") != "real":
            continue
        when = _parse_utc_date(rec.get("utc_date"))
        if when is None:
            continue  # undated/malformed run can't be placed in a promotion window
        for cap in rec.get("capabilities", []) or []:
            name = str(cap.get("capability", "")).strip()
            if name:
                events.setdefault(name, []).append((when, bool(cap.get("passed"))))
    capabilities = {name: _capability_level(evs) for name, evs in sorted(events.items())}
    overall = min(
        capabilities.values(), key=MATURITY_LEVELS.index, default=MATURITY_LEVELS[0]
    )
    return {"level": overall, "capabilities": capabilities}


def compute_matrix(records: list[dict[str, Any]]) -> dict[ComboKey, dict[str, Any]]:
    """Group live ledger records by combo and derive each combo's maturity.

    Combos that only ever ran synthetically (or lack a firmware version) get
    **no** row — mock runs never promote and never appear in the matrix.
    """
    groups: dict[ComboKey, list[dict[str, Any]]] = {}
    for rec in records:
        if rec.get("source") != "real":
            continue
        key: ComboKey = (
            str(rec.get("vendor", "")).strip(),
            str(rec.get("product", "")).strip(),
            str(rec.get("firmware_version", "")).strip(),
        )
        if not all(key):
            continue
        groups.setdefault(key, []).append(rec)
    return {key: compute_maturity(recs) for key, recs in groups.items()}


# --------------------------------------------------------------------------- #
# Registry writer + drift check                                               #
# --------------------------------------------------------------------------- #


def _norm(key: ComboKey) -> ComboKey:
    """Case-insensitive (vendor, product) matching, same as ``_find_entry``."""
    vendor, product, fw = key
    return (vendor.strip().lower(), product.strip().lower(), fw.strip())


def fold_into_registry(
    registry: dict[str, Any], matrix: dict[ComboKey, dict[str, Any]]
) -> dict[str, Any]:
    """Fold the derived matrix into a *copy* of the registry and return it.

    The ``maturity`` key on each ``versions[]`` row is derived; any OTHER keys on
    a row (e.g. a future hand-authored ``known_bad`` from #97) are preserved.
    A backed combo's ``maturity`` is set/updated; a version the ledger no longer
    backs loses only its ``maturity`` (and the row is dropped if nothing else
    remains on it), so a ``--write`` always clears maturity drift without
    deleting non-derived data. Never touches ``updated`` — a recompute that
    changes nothing must be a byte-level no-op. Idempotent.
    """
    reg: dict[str, Any] = json.loads(json.dumps(registry))  # deep copy, like merge_submission
    reg.setdefault("schema_version", 2)
    reg.setdefault("firmware", [])

    by_device: dict[tuple[str, str], dict[str, dict[str, Any]]] = {}
    for (vendor, product, fw), maturity in matrix.items():
        by_device.setdefault((vendor, product), {})[fw] = maturity

    for (vendor, product), rows in sorted(by_device.items()):
        entry = _find_entry(reg, vendor, product)
        if entry is None:
            entry = {"vendor": vendor, "product": product}
            reg["firmware"].append(entry)
        existing = {
            str(r.get("version", "")): r
            for r in entry.get("versions", []) or []
            if isinstance(r, dict)
        }
        merged: list[dict[str, Any]] = []
        for fw in sorted(set(rows) | set(existing), key=_ver_tuple):
            row = existing.get(fw, {"version": fw})
            if fw in rows:
                row["maturity"] = rows[fw]
            else:
                row.pop("maturity", None)  # ledger no longer backs this version's maturity
                if set(row) <= {"version"}:
                    continue  # nothing but the version left -> drop the empty row
            merged.append(row)
        entry["versions"] = merged

    backed = {(v.strip().lower(), p.strip().lower()) for v, p in by_device}
    for entry in reg["firmware"]:
        ekey = (
            str(entry.get("vendor", "")).strip().lower(),
            str(entry.get("product", "")).strip().lower(),
        )
        if ekey in backed:
            continue
        kept = [row for row in entry.get("versions", []) or [] if "maturity" not in row]
        if kept:
            entry["versions"] = kept
        elif "versions" in entry:
            del entry["versions"]
    return reg


def registry_matrix(registry: dict[str, Any]) -> dict[ComboKey, dict[str, Any]]:
    """The derived view committed in the registry: every maturity-bearing row."""
    out: dict[ComboKey, dict[str, Any]] = {}
    for entry in registry.get("firmware", []) or []:
        if not isinstance(entry, dict):
            continue
        for row in entry.get("versions", []) or []:
            if isinstance(row, dict) and "maturity" in row:
                key: ComboKey = (
                    str(entry.get("vendor", "")),
                    str(entry.get("product", "")),
                    str(row.get("version", "")),
                )
                out[key] = row["maturity"]
    return out


def drift(registry: dict[str, Any], matrix: dict[ComboKey, dict[str, Any]]) -> list[str]:
    """Human-readable mismatches between the committed registry and the ledger.

    Empty list = in sync. Each message ends with the regen command.
    """
    committed = {_norm(k): (k, v) for k, v in registry_matrix(registry).items()}
    derived = {_norm(k): (k, v) for k, v in matrix.items()}
    fix = f"regenerate with: {REGEN_COMMAND}"
    msgs: list[str] = []
    for nk in sorted(set(committed) | set(derived)):
        if nk not in derived:
            (vendor, product, fw), _ = committed[nk]
            msgs.append(
                f"{vendor} {product} {fw}: registry has a maturity row the ledger "
                f"does not back — {fix}"
            )
            continue
        if nk not in committed:
            (vendor, product, fw), _ = derived[nk]
            msgs.append(
                f"{vendor} {product} {fw}: ledger-derived maturity is missing from "
                f"the registry — {fix}"
            )
            continue
        (vendor, product, fw), have = committed[nk]
        _, want = derived[nk]
        if have == want:
            continue
        if have.get("level") != want.get("level"):
            msgs.append(
                f"{vendor} {product} {fw}: level is {have.get('level')!r} in the "
                f"registry but the ledger derives {want.get('level')!r} — {fix}"
            )
        have_caps = have.get("capabilities") or {}
        want_caps = want.get("capabilities") or {}
        for cap in sorted(set(have_caps) | set(want_caps)):
            if have_caps.get(cap) != want_caps.get(cap):
                msgs.append(
                    f"{vendor} {product} {fw}: capability {cap!r} is "
                    f"{have_caps.get(cap)!r} in the registry but the ledger derives "
                    f"{want_caps.get(cap)!r} — {fix}"
                )
    return msgs


# --------------------------------------------------------------------------- #
# CLI entry point (CI drift gate / ingest workflow, #101)                     #
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    """Re-derive maturity from the ledger and check or write the registry.

    ``--check`` prints drift lines and exits 5 on drift, 0 when in sync.
    ``--write`` folds the derived rows in, validates, and writes only when
    something changed. Exit codes: 0 changed / 4 no-op / 3 invalid / 5 drift.
    """
    import argparse

    p = argparse.ArgumentParser(
        description="Derive support-matrix maturity from the run ledger (#98)."
    )
    p.add_argument(
        "--ledger", required=True, help="JSONL run ledger (src/kvm_pilot/data/test_runs.jsonl)"
    )
    p.add_argument("--registry", required=True, help="firmware registry JSON to check/update")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="fail (exit 5) on drift")
    mode.add_argument("--write", action="store_true", help="fold the derived rows in")
    args = p.parse_args(argv)

    matrix = compute_matrix(load_ledger(Path(args.ledger)))
    with open(args.registry, encoding="utf-8") as fh:
        registry = json.load(fh)

    if args.check:
        lines = drift(registry, matrix)
        for line in lines:
            print(line)
        if lines:
            return 5
        print("registry maturity matches the ledger")
        return 0

    folded = fold_into_registry(registry, matrix)
    errs = validate_registry(folded)
    if errs:
        print("FOLDED REGISTRY FAILED VALIDATION:", file=sys.stderr)
        for e in errs:
            print(f"  - {e}", file=sys.stderr)
        return 3
    if folded == registry:
        print("no change: derived maturity already in the registry")
        return 4
    _write_registry(args.registry, folded)
    print(f"registry maturity updated: {len(matrix)} combo(s)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
