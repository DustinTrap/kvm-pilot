#!/usr/bin/env python3
"""Build a GitHub-wiki-formatted mirror of the project documentation.

The canonical docs live in ``docs/`` (plus two guides that must stay next to the
code they document: ``skill/SKILL.md`` and ``mcp_server/README.md``). A GitHub
wiki is a flat namespace of pages with no subfolders, so this script copies each
source page to a flat output directory, rewrites intra-doc links for that flat
namespace (``architecture.md`` -> ``architecture``, ``docs/README.md`` -> the
wiki ``Home``), copies the diagrams alongside, and generates ``Home`` +
``_Sidebar`` navigation.

Run by ``.github/workflows/wiki-sync.yml``. Also runnable locally to preview the
output without touching the wiki::

    python .github/scripts/build_wiki.py --out /tmp/wiki && ls /tmp/wiki
"""

from __future__ import annotations

import argparse
import json
import posixpath
import re
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPO_URL = "https://github.com/DustinTrap/kvm-pilot"

# (repo-relative source, wiki page filename, sidebar title). Order = sidebar order.
# The wiki filename keeps the source stem so link rewriting is a plain ``.md``
# strip; ``Home`` is the wiki's reserved landing page.
PAGES: list[tuple[str, str, str | None]] = [
    ("docs/README.md", "Home.md", None),
    ("docs/architecture.md", "architecture.md", "Architecture"),
    ("docs/configuration.md", "configuration.md", "Configuration"),
    ("docs/decisions.md", "decisions.md", "Design decisions"),
    ("docs/redfish.md", "redfish.md", "Redfish reference"),
    ("skill/SKILL.md", "skill.md", "Claude skill"),
    ("mcp_server/README.md", "mcp-server.md", "MCP server"),
    ("docs/CONTRIBUTING.md", "CONTRIBUTING.md", "Contributing"),
    ("docs/SECURITY.md", "SECURITY.md", "Security policy"),
    # Analysis output: session-level review narratives (docs/analysis/).
    # NB: the wiki filename must keep the source stem (link rewriting maps by stem).
    ("docs/analysis/2026-07-01-deep-review.md", "2026-07-01-deep-review.md",
     "Analysis: 2026-07-01 deep review"),
]

# Link/image markdown: capture the ``[label]`` and the ``(target)`` separately.
_LINK = re.compile(r"(!?\[[^\]]*\])\(([^)]+)\)")
_IMAGE_EXTS = (".svg", ".png", ".jpg", ".jpeg", ".gif")

# --- Community hardware-compatibility (HCL) page -----------------------------
# Generated (not mirrored from a doc) from the lossless run ledger. The ledger is
# the git-friendly source of truth; we load it into an ephemeral in-memory SQLite
# purely to aggregate, then render. See issue #96 / #105.
HCL_LEDGER = ROOT / "data" / "test_runs.jsonl"
HCL_PAGE = "Hardware-Compatibility.md"
HCL_MIN_SAMPLES = 3  # a cell below this shows "insufficient data", not a verdict
# capability -> is it destructive (safety-relevant verdict)? Column order preserved.
HCL_CAPS: list[tuple[str, bool]] = [
    ("info", False), ("snapshot", False), ("healthcheck", False),
    ("logs", False), ("power_state", False),
    ("virtual_media", True), ("power", True), ("firmware_update", True),
]


def _hcl_load(ledger: Path) -> sqlite3.Connection:
    """Load the JSONL run ledger into an ephemeral in-memory SQLite DB.

    Mirrors the #104 ``runs`` / ``run_capabilities`` schema so the same
    aggregation SQL serves a future managed-Postgres import unchanged. Idempotent
    on ``run_id`` (``INSERT OR IGNORE``), matching the ingestion contract.
    """
    db = sqlite3.connect(":memory:")
    db.executescript(
        "CREATE TABLE runs (run_id TEXT PRIMARY KEY, source TEXT, vendor TEXT, "
        "product TEXT, firmware_version TEXT, kvm_pilot_version TEXT, driver TEXT, "
        "os_family TEXT, python_version TEXT, utc_date TEXT);"
        "CREATE TABLE run_capabilities (run_id TEXT, capability TEXT, passed INTEGER, "
        "outcome TEXT, PRIMARY KEY (run_id, capability));"
    )
    for line in ledger.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        db.execute(
            "INSERT OR IGNORE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?)",
            (r["run_id"], r.get("source", "unknown"), r["vendor"], r["product"],
             r.get("firmware_version"), r.get("kvm_pilot_version"), r.get("driver"),
             r.get("os_family"), r.get("python_version"), r["utc_date"]),
        )
        for c in r.get("capabilities", []):
            db.execute(
                "INSERT OR IGNORE INTO run_capabilities VALUES (?,?,?,?)",
                (r["run_id"], c["capability"], 1 if c["passed"] else 0, c.get("outcome", "")),
            )
    return db


def _hcl_cell(agg: dict[str, tuple[int, int]], cap: str, destructive: bool) -> str:
    """Render one matrix cell: pass-rate + sample count, gated by min samples."""
    if cap not in agg:
        return "·"  # never exercised on this combo
    passes, n = agg[cap]
    if n < HCL_MIN_SAMPLES:
        return f"…&nbsp;n={n}"  # insufficient data — not yet a verdict
    rate = round(100 * passes / n)
    mark = "✅" if rate == 100 else ("❌" if rate == 0 else "⚠️")
    flag = "†" if destructive else ""
    return f"{mark}&nbsp;{rate}%{flag}<br><sub>n={n}</sub>"


def render_hcl(ledger: Path = HCL_LEDGER) -> str:
    """Render the community compatibility matrix as a wiki markdown page."""
    db = _hcl_load(ledger)
    total, real, synthetic = db.execute(
        "SELECT COUNT(*), "
        "SUM(source='real'), SUM(source='synthetic') FROM runs"
    ).fetchone()
    rows = db.execute(
        "SELECT r.vendor, r.product, r.firmware_version, rc.capability, "
        "SUM(rc.passed), COUNT(*) FROM runs r JOIN run_capabilities rc "
        "ON r.run_id = rc.run_id "
        "GROUP BY r.vendor, r.product, r.firmware_version, rc.capability"
    ).fetchall()
    combos: dict[tuple[str, str, str], dict[str, tuple[int, int]]] = {}
    for vendor, product, fw, cap, passes, n in rows:
        combos.setdefault((vendor, product, fw or ""), {})[cap] = (passes, n)

    cap_order = [c for c, _ in HCL_CAPS]
    destructive = {c for c, d in HCL_CAPS if d}
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    out = [
        "# Hardware Compatibility (community-reported)",
        "",
    ]
    if synthetic:
        out += [
            "> ⚠️ **PREVIEW — not yet real community data.** This page is seeded "
            f"with **{synthetic} synthetic** demo run(s) and **{real} real** run(s) "
            "to exercise the reporting pipeline. Cells marked with synthetic data "
            "are illustrative only; do **not** rely on them. Synthetic rows are "
            "purged once genuine submissions arrive.",
            "",
        ]
    out += [
        f"_Auto-generated from {total} test run(s) · last updated {now}_",
        "",
        "Each cell shows the **pass rate** and sample count **n** for a capability "
        f"on a device+firmware combo. Cells with fewer than {HCL_MIN_SAMPLES} runs "
        "show `…` (insufficient data); `·` = never exercised.",
        "",
        "> **†  destructive capability.** `power`, `virtual_media` and "
        "`firmware_update` verdicts are safety-relevant — a green cell means "
        "*reported working*, not a guarantee. Read the device caveats before "
        "relying on a remote flash.",
        "",
    ]
    header = "| Device | Firmware | " + " | ".join(
        f"{c}{'†' if c in destructive else ''}" for c in cap_order
    ) + " |"
    out += [header, "|" + "---|" * (len(cap_order) + 2)]
    for vendor, product, fw in sorted(combos):
        agg = combos[(vendor, product, fw)]
        cells = " | ".join(_hcl_cell(agg, c, c in destructive) for c in cap_order)
        out.append(f"| **{vendor} {product}** | {fw} | {cells} |")
    out += [
        "",
        f"Legend: ✅ all-pass · ⚠️ mixed · ❌ all-fail · … insufficient (n<{HCL_MIN_SAMPLES}) "
        "· · not tested",
        "",
    ]
    return "\n".join(out)


def _rewrite_target(target: str, src_dir: str) -> str:
    """Map a link target as written in the repo to its flat-wiki equivalent.

    ``src_dir`` is the repo-relative directory of the source page (``docs``,
    ``skill``, ``mcp_server``) so remaining relative targets can be resolved.
    """
    if re.match(r"^(https?:|#|mailto:|/)", target):
        return target  # external, in-page anchor, or absolute — leave untouched
    path, _, anchor = target.partition("#")
    anchor = f"#{anchor}" if anchor else ""
    name = Path(path).name
    lower = name.lower()
    if lower.endswith(_IMAGE_EXTS):
        return name + anchor  # images are copied to the wiki root; keep basename
    # The two co-located guides map to their renamed wiki pages.
    if lower in ("skill.md",) or name == "SKILL.md":
        return "skill" + anchor
    if "mcp_server" in path or lower == "mcp-server.md":
        return "mcp-server" + anchor
    if name == "README.md":  # the docs index is the wiki landing page
        return "Home" + anchor
    if lower.endswith(".md"):
        return Path(path).stem + anchor
    # Anything left is a repo file or directory (e.g. ``../src/...``). The wiki
    # has no source tree, so resolve it against the page's repo directory and
    # link to the file on GitHub instead of shipping a dead relative link.
    resolved = posixpath.normpath(posixpath.join(src_dir, path))
    kind = "tree" if path.endswith("/") else "blob"
    return f"{REPO_URL}/{kind}/main/{resolved}{anchor}"


def _transform(md: str, src_dir: str) -> str:
    return _LINK.sub(lambda m: f"{m.group(1)}({_rewrite_target(m.group(2), src_dir)})", md)


def build(out: Path) -> None:
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    for src_rel, wiki_name, _ in PAGES:
        src = ROOT / src_rel
        if not src.exists():
            raise SystemExit(f"missing doc source: {src_rel}")
        page = _transform(src.read_text(), posixpath.dirname(src_rel))
        if "](../" in page or "](./" in page:
            raise SystemExit(f"unresolved relative link left in {wiki_name}")
        (out / wiki_name).write_text(page, encoding="utf-8")

    # Diagrams referenced by the pages, copied flat next to them.
    for svg in sorted((ROOT / "docs").glob("*.svg")):
        shutil.copy2(svg, out / svg.name)

    # Generated (not mirrored) page: the community compatibility matrix, rendered
    # from the run ledger. Skipped cleanly if the ledger has not been seeded yet.
    hcl_built = False
    if HCL_LEDGER.exists():
        (out / HCL_PAGE).write_text(render_hcl(), encoding="utf-8")
        hcl_built = True

    # Sidebar navigation, in PAGES order (Home first, then the guides).
    lines = ["### kvm-pilot docs", "", "- [[Home]]"]
    for _, wiki_name, title in PAGES:
        if title is None:
            continue
        lines.append(f"- [[{title}|{Path(wiki_name).stem}]]")
    if hcl_built:
        lines.append(f"- [[Hardware compatibility|{Path(HCL_PAGE).stem}]]")
    (out / "_Sidebar.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"built {len(PAGES)} pages{' + HCL' if hcl_built else ''} + sidebar into {out}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, required=True, help="output directory for wiki pages")
    build(ap.parse_args().out)


if __name__ == "__main__":
    main()
