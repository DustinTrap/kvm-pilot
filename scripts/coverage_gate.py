#!/usr/bin/env python3
"""Coverage ratchet gate (#246): fail below a committed floor, nudge above it.

CI has measured coverage since #56 and gated it at a *static* ``fail_under``.
A static floor drifts down in effect — the suite can shed ten points and stay
green — and it never banks a gain. This gate makes the floor a **ratchet**:

* the floor is a number committed at :data:`FLOOR_FILE`, so the gate is
  deterministic, reviewable in the diff, and its history is the file's git log
  (no coverage service, no comparing against a previous run's artifact — those
  add flakiness, races, and an audit hole);
* measuring below it fails the job;
* clearing it by :data:`NUDGE_MARGIN` or more prints a suggestion to raise it,
  every green run, until a human banks the gain in the PR that earned it.

Deliberately a dumb, honest calculator: no auto-commits, no network, stdlib
only. If the test framework needs workarounds to measure coverage properly,
those belong in the suite, not here.

Usage::

    python scripts/coverage_gate.py [--report coverage.xml] [--floor .coverage-floor]

Exit codes: ``0`` at/above the floor, ``1`` below it, ``2`` for a degenerate
input (unreadable floor, missing report, nothing instrumented) — that last class
is the gate catching its own plumbing breaking, e.g. a test step that silently
ran without coverage, and it must never be mistaken for a pass.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPORT_FILE = "coverage.xml"
FLOOR_FILE = ".coverage-floor"

# Raise the suggestion only when the gain is worth banking, and suggest a floor
# below the measurement so ordinary refactors don't immediately breach it.
NUDGE_MARGIN = 1.0
NUDGE_BUFFER = 0.5

# Excluded BEFORE the percentage is computed. An unexcluded denominator is
# dishonest in both directions: it hides real gaps behind generated bulk, and it
# punishes refactors of wiring that cannot be meaningfully tested.
#
# Kept here rather than in config files so one file answers "what is measured?".
#
# This repo has NO generated code — nothing is codegen'd into src/ (the wiki is
# generated *from* docs, not into them), so the codegen patterns match nothing
# today and are carried for future-proofing. Note what is deliberately NOT here:
# mcp/server.py and cli.py are the two largest uncovered surfaces, and excluding
# them would flatter the number by hiding the most real gaps we have.
EXCLUDE_PATTERNS = (
    "*/__about__.py",   # version constant — metadata, not behavior
    "*/__main__.py",    # entrypoint wiring
    "*.g.py",           # codegen output (none today)
    "*_pb2.py",         # protobuf codegen (none today)
    "*_pb2_grpc.py",
    "*generated*",
)


# Reporting categories, in priority order — FIRST match wins, so a file named by
# a specific category is never re-claimed by a broader one below it. Reporting
# only: the gate's pass/fail verdict is the blended number (#246). A per-category
# breakdown is the cheap half of differential floors, and it is what tells us
# whether the expensive half is warranted — a small badly-covered area is
# invisible inside a large well-covered one, which is exactly what a blended
# floor cannot see.
CATEGORIES: tuple[tuple[str, tuple[str, ...]], ...] = (
    # The approval/gate path: where a regression is most expensive.
    ("Safety & approval", ("*/safety.py", "*/mcp/act.py")),
    ("MCP server",        ("*/mcp/*",)),
    ("Drivers",           ("*/drivers/*", "*/client.py")),
    ("Vision",            ("*/vision/*",)),
    ("CLI",               ("*/cli.py",)),
    ("Health & evidence", ("*/health.py", "*/support_matrix.py", "*/maturity.py",
                           "*/firmware_registry.py", "*/test_report.py")),
    ("In-band channels",  ("*/ssh.py", "*/remote_ps.py", "*/wol.py", "*/efiboot.py")),
    ("Core plumbing",     ("*/http.py", "*/config.py", "*/errors.py", "*/detect.py",
                           "*/router.py", "*/benchmark.py", "*/calibrate.py",
                           "*/bootstrap.py")),
)

# Named in docs/coverage-policy.md as the trigger for revisiting per-category
# floors. Reported, never enforced — tripping it starts a conversation, not a
# build failure.
WATCH_FILES = ("safety.py", "mcp/act.py")
WATCH_FLOOR = 90.0


class GateError(Exception):
    """A degenerate input: the gate cannot honestly answer, so it must not pass."""


def is_excluded(filename: str, patterns: tuple[str, ...] = EXCLUDE_PATTERNS) -> bool:
    """True when ``filename`` matches an exclusion pattern.

    Matched against the path as the report spells it and against a leading-slash
    form, so ``*/__about__.py`` catches a top-level ``__about__.py`` too.
    """
    candidates = (filename, "/" + filename.lstrip("/"))
    return any(fnmatch.fnmatch(c, p) for c in candidates for p in patterns)


def read_floor(path: Path) -> float:
    """The committed floor. Raises :class:`GateError` if absent or unparseable —
    a missing floor means the ratchet is not installed, which is not a pass."""
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise GateError(
            f"coverage floor file {path} is missing or unreadable ({exc}). "
            "The ratchet cannot run without a committed floor — restore the file "
            "rather than removing the gate."
        ) from exc
    if not raw:
        raise GateError(f"coverage floor file {path} is empty")
    try:
        return float(raw)
    except ValueError as exc:
        raise GateError(
            f"coverage floor file {path} does not contain a number (got {raw!r})"
        ) from exc


def category_of(filename: str) -> str:
    """The reporting category for ``filename``; first pattern match wins."""
    candidates = (filename, "/" + filename.lstrip("/"))
    for name, patterns in CATEGORIES:
        if any(fnmatch.fnmatch(c, p) for c in candidates for p in patterns):
            return name
    return "Other"


def measure(report: Path, patterns: tuple[str, ...] = EXCLUDE_PATTERNS) -> tuple[int, int, list[str]]:
    """``(hit, total, excluded, by_category, watched)`` from a cobertura report.

    Line coverage, not branch: it is the measure a floor can be reasoned about
    without knowing how the suite exercises each condition.
    """
    try:
        tree = ET.parse(report)  # nosec B314 - our own CI artifact
    except OSError as exc:
        raise GateError(
            f"coverage report {report} is missing ({exc}). Did the test step run "
            "with coverage enabled?"
        ) from exc
    except ET.ParseError as exc:
        raise GateError(f"coverage report {report} is not parseable XML ({exc})") from exc

    hit = total = 0
    excluded: list[str] = []
    by_cat: dict[str, list[int]] = {}
    watched: dict[str, list[int]] = {}
    for cls in tree.getroot().iter("class"):
        filename = cls.get("filename") or ""
        if is_excluded(filename, patterns):
            excluded.append(filename)
            continue
        f_hit = f_total = 0
        for line in cls.iter("line"):
            f_total += 1
            if int(line.get("hits", "0") or 0) > 0:
                f_hit += 1
        hit += f_hit
        total += f_total
        # A file can appear as several <class> elements; accumulate, never replace.
        cat = by_cat.setdefault(category_of(filename), [0, 0])
        cat[0] += f_hit
        cat[1] += f_total
        if any(filename.endswith(w) for w in WATCH_FILES):
            w = watched.setdefault(filename, [0, 0])
            w[0] += f_hit
            w[1] += f_total

    if total == 0:
        raise GateError(
            f"coverage report {report} has no instrumented lines after exclusions. "
            "That usually means the test step ran WITHOUT coverage, or every source "
            "file matched an exclusion pattern — either way the gate cannot measure "
            "anything and must not report success."
        )
    return hit, total, excluded, by_cat, watched


def write_summary(text: str) -> None:
    """Append to the CI step summary when one exists; always echo to stdout."""
    print(text)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    except OSError:
        pass  # a broken summary file must never fail the build


def format_breakdown(by_cat: dict[str, list[int]], watched: dict[str, list[int]]) -> str:
    """Per-category table, worst first, plus any watched file under its threshold.

    Reporting only — nothing here changes the verdict. The point is that a
    blended floor structurally cannot see a small badly-covered area inside a
    large well-covered one, and this is what makes that visible while the gate
    stays simple (#246).
    """
    rows = sorted(
        ((name, h, n) for name, (h, n) in by_cat.items() if n),
        key=lambda r: r[1] / r[2],
    )
    lines = ["| Category | Coverage | Lines | Uncovered |", "|---|---:|---:|---:|"]
    lines += [f"| {name} | {100.0 * h / n:.2f}% | {n} | {n - h} |" for name, h, n in rows]

    tripped = [
        (fn, 100.0 * h / n)
        for fn, (h, n) in sorted(watched.items())
        if n and 100.0 * h / n < WATCH_FLOOR
    ]
    if tripped:
        detail = ", ".join(f"`{fn}` at {pct:.2f}%" for fn, pct in tripped)
        lines += [
            "",
            f"> ⚠️ **Per-category revisit trigger met** — {detail}, below the "
            f"{WATCH_FLOOR:.0f}% watch threshold for the approval/gate path. "
            "This does not fail the build; see `docs/coverage-policy.md` for the "
            "decision it is meant to prompt.",
        ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--report", default=REPORT_FILE, type=Path,
                        help=f"cobertura XML from the test run (default {REPORT_FILE})")
    parser.add_argument("--floor", default=FLOOR_FILE, type=Path,
                        help=f"file holding the committed floor (default {FLOOR_FILE})")
    args = parser.parse_args(argv)

    try:
        floor = read_floor(args.floor)
        hit, total, excluded, by_cat, watched = measure(args.report)
    except GateError as exc:
        print(f"coverage gate: {exc}", file=sys.stderr)
        return 2

    pct = 100.0 * hit / total
    detail = (f"{pct:.2f}% line coverage ({hit}/{total} lines"
              + (f", {len(excluded)} file(s) excluded" if excluded else "")
              + f"); floor {floor:.2f}%")
    breakdown = format_breakdown(by_cat, watched)

    if pct + 1e-9 < floor:
        write_summary(
            f"### ❌ Coverage below the floor\n\n{detail}\n\n{breakdown}\n"
            f"Short by {floor - pct:.2f} points. Add tests, or — if the drop is "
            f"deliberate — lower the floor in `{args.floor}` **with a tracking issue "
            "explaining why**. Lowering it silently is the failure mode this gate exists "
            "to prevent."
        )
        return 1

    if pct - floor >= NUDGE_MARGIN:
        suggested = round(pct - NUDGE_BUFFER, 2)
        write_summary(
            f"### 📈 Coverage floor can be raised\n\n{detail}\n\n{breakdown}\n"
            f"Clears the floor by {pct - floor:.2f} points. Bank it: set "
            f"`{args.floor}` to **{suggested}**.\n\n"
            "This suggestion repeats on every green run until someone raises the "
            "floor — that is what makes the ratchet move. It is never applied "
            "automatically; a human raises it in the PR that earned it."
        )
        return 0

    write_summary(f"### ✅ Coverage gate passed\n\n{detail}\n\n{breakdown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
