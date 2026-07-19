#!/usr/bin/env python3
"""Build a GitHub-wiki-formatted mirror of the project documentation.

The canonical docs live in ``docs/`` (plus two guides that must stay next to the
code they document: ``src/kvm_pilot/skill/SKILL.md`` and
``src/kvm_pilot/mcp/README.md``). A GitHub
wiki is a flat namespace of pages with no subfolders, so this script copies each
source page to a flat output directory, rewrites intra-doc links for that flat
namespace (``architecture.md`` -> ``architecture``, ``docs/README.md`` -> the
wiki ``Home``), copies the diagrams alongside, and generates ``Home`` +
``_Sidebar`` navigation.

``PAGES`` below is the **single navigation manifest** (#221): each page carries
its section, the sidebar is generated from it, and ``--check`` (run in CI) fails
if the other two navigation surfaces drift — every page must be linked from the
``docs/README.md`` hub and be present in ``llms.txt`` (or deliberately opted
out), and no page may contain a dead relative link. Adding a doc = add the file,
register it here with a section, link it from the hub; CI enforces the rest.

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

# Sidebar section headings, in display order. Every PAGES entry (except Home)
# names one of these; the sidebar is generated grouped by section.
SECTIONS: list[str] = [
    "Start here",
    "Guides",
    "Reference",
    "Runbooks & test plans",
    "Design records",
    "Project",
    "Analysis (internal reports)",
]

# (repo-relative source, wiki page filename, sidebar title, section).
# THE navigation manifest (#221): sidebar order = PAGES order within a section;
# ``--check`` holds docs/README.md and llms.txt to this list. The wiki filename
# keeps the source stem so link rewriting is a plain ``.md`` strip; ``Home`` is
# the wiki's reserved landing page (section None).
PAGES: list[tuple[str, str, str | None, str | None]] = [
    ("docs/README.md", "Home.md", None, None),
    # Start here
    ("docs/getting-started.md", "getting-started.md", "Getting started", "Start here"),
    # Guides — task-oriented: how to accomplish something
    ("docs/amt-onboarding.md", "amt-onboarding.md", "Intel AMT onboarding runbook", "Guides"),
    ("docs/unattended-install.md", "unattended-install.md",
     "Unattended Linux installs", "Guides"),
    ("docs/firmware-update.md", "firmware-update.md", "Remote firmware update", "Guides"),
    ("docs/troubleshooting.md", "troubleshooting.md", "Troubleshooting & FAQ", "Guides"),
    # Reference — descriptive: what exists and how it behaves
    ("docs/cli.md", "cli.md", "CLI reference", "Reference"),
    ("docs/configuration.md", "configuration.md", "Configuration", "Reference"),
    ("docs/driver-features.md", "driver-features.md", "Driver features", "Reference"),
    ("docs/architecture.md", "architecture.md", "Architecture", "Reference"),
    ("docs/redfish.md", "redfish.md", "Redfish reference", "Reference"),
    ("docs/amt.md", "amt.md", "Intel AMT vPro reference", "Reference"),
    ("docs/firmware-registry.md", "firmware-registry.md", "Firmware registry", "Reference"),
    ("src/kvm_pilot/skill/SKILL.md", "skill.md", "Claude skill", "Reference"),
    # The skill's playbooks (#222): progressive-disclosure references bundled
    # beside SKILL.md and re-served at runtime by the MCP `doctrine` tool.
    ("src/kvm_pilot/skill/references/interfaces.md", "interfaces.md",
     "Skill playbook: interfaces", "Reference"),
    ("src/kvm_pilot/skill/references/recovery.md", "recovery.md",
     "Skill playbook: recovery", "Reference"),
    ("src/kvm_pilot/skill/references/setup.md", "setup.md",
     "Skill playbook: setup & gates", "Reference"),
    ("src/kvm_pilot/skill/references/linux-install.md", "linux-install.md",
     "Skill playbook: Linux installs", "Reference"),
    ("src/kvm_pilot/skill/references/target-context.md", "target-context.md",
     "Skill playbook: target context", "Reference"),
    ("src/kvm_pilot/skill/references/library.md", "library.md",
     "Skill playbook: Python library", "Reference"),
    ("src/kvm_pilot/mcp/README.md", "mcp-server.md", "MCP server", "Reference"),
    # Runbooks & test plans — procedures executed against real hardware
    ("docs/test-plan.md", "test-plan.md", "Test plan", "Runbooks & test plans"),
    ("docs/hardware-test-plan-ilo-idrac.md", "hardware-test-plan-ilo-idrac.md",
     "Hardware test plan: iLO / iDRAC", "Runbooks & test plans"),
    # Design records — decisions and RFCs, not how-tos
    ("docs/decisions.md", "decisions.md", "Design decisions", "Design records"),
    ("docs/reflexes.md", "reflexes.md", "Reflexes (RFC)", "Design records"),
    # Project — contributing, extending, security
    ("docs/CONTRIBUTING.md", "CONTRIBUTING.md", "Contributing", "Project"),
    ("docs/plugin-development.md", "plugin-development.md",
     "Writing a first-class driver", "Project"),
    ("docs/SECURITY.md", "SECURITY.md", "Security policy", "Project"),
    # Analysis — dated session-level review narratives (docs/analysis/), demoted
    # to the last sidebar section (#209). NB: the wiki filename must keep the
    # source stem (link rewriting maps by stem).
    ("docs/analysis/2026-07-01-deep-review.md", "2026-07-01-deep-review.md",
     "2026-07-01 deep review", "Analysis (internal reports)"),
    ("docs/analysis/2026-07-03-rm1pe-firmware-and-encoder.md",
     "2026-07-03-rm1pe-firmware-and-encoder.md",
     "2026-07-03 RM1PE firmware + encoder", "Analysis (internal reports)"),
    ("docs/analysis/2026-07-08-perf-a13-a14.md",
     "2026-07-08-perf-a13-a14.md",
     "2026-07-08 a13→a14 performance", "Analysis (internal reports)"),
    ("docs/analysis/2026-07-08-e2e-leaner-cut.md",
     "2026-07-08-e2e-leaner-cut.md",
     "2026-07-08 a13→a14 end-to-end tasks", "Analysis (internal reports)"),
]

# Docs that intentionally never publish to the wiki (repo-relative). Empty today;
# add a path here (with a reason) instead of leaving it silently unregistered.
OPT_OUT: frozenset[str] = frozenset()

# Pages deliberately absent from llms.txt — it is a curated agent-facing subset,
# not a mirror of PAGES. Everything else registered above must appear there
# (``--check`` enforces membership; the prose stays hand-written).
LLMS_OPT_OUT: frozenset[str] = frozenset({
    "docs/README.md",  # the hub duplicates llms.txt's own role as a doc map
    "docs/reflexes.md",  # draft RFC, post-GA scope — not agent operating material
    "docs/analysis/2026-07-01-deep-review.md",  # dated internal narratives
    "docs/analysis/2026-07-03-rm1pe-firmware-and-encoder.md",
    "docs/analysis/2026-07-08-perf-a13-a14.md",
    "docs/analysis/2026-07-08-e2e-leaner-cut.md",
})

# Link/image markdown: capture the ``[label]`` and the ``(target)`` separately.
_LINK = re.compile(r"(!?\[[^\]]*\])\(([^)]+)\)")
_IMAGE_EXTS = (".svg", ".png", ".jpg", ".jpeg", ".gif")

# --- Community hardware-compatibility (HCL) page -----------------------------
# Generated (not mirrored from a doc) from the lossless run ledger. The ledger is
# the git-friendly source of truth; we load it into an ephemeral in-memory SQLite
# purely to aggregate, then render. See issue #96 / #105.
HCL_LEDGER = ROOT / "src" / "kvm_pilot" / "data" / "test_runs.jsonl"
HCL_REGISTRY = ROOT / "src" / "kvm_pilot" / "data" / "firmware_registry.json"
HCL_PAGE = "Hardware-Compatibility.md"
HCL_MIN_SAMPLES = 3  # a cell below this shows "insufficient data", not a verdict
# capability -> is it destructive (safety-relevant verdict)? Column order preserved.
# Covers the PiKVM/GLKVM set plus the BMC/IPMI capabilities (sensors, boot_config,
# serial_console) so Redfish/IPMI devices are fully represented, not just partially.
HCL_CAPS: list[tuple[str, bool]] = [
    ("info", False), ("snapshot", False), ("healthcheck", False),
    ("logs", False), ("sensors", False), ("power_state", False),
    ("virtual_media", True), ("power", True), ("boot_config", True),
    ("serial_console", True), ("firmware_update", True),
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


def _hcl_maturity(registry: Path) -> dict[tuple[str, str, str], str]:
    """(vendor, product, firmware) lowered -> derived maturity level (#103).

    Read straight from the shipped registry's ``versions[].maturity`` rows —
    the #98-derived, CI-drift-guarded truth (mirrors
    ``support_matrix._registry_maturity``'s case-insensitive join). ``{}`` when
    the registry is missing/unreadable, so the page still builds.

    DELIBERATELY import-free of ``kvm_pilot`` (like the HCL ledger ingestion
    above): this script builds the wiki from data files alone, so a package
    import error can never take the docs pipeline down with it.
    """
    try:
        reg = json.loads(registry.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[tuple[str, str, str], str] = {}
    for entry in reg.get("firmware", []):
        vendor = str(entry.get("vendor", "")).strip().lower()
        product = str(entry.get("product", "")).strip().lower()
        for row in entry.get("versions", []) or []:
            level = (row.get("maturity") or {}).get("level")
            if level:
                out[(vendor, product, str(row.get("version", "")).strip().lower())] = level
    return out


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


def render_hcl(ledger: Path = HCL_LEDGER, registry: Path = HCL_REGISTRY) -> str:
    """Render the community compatibility matrix as a wiki markdown page."""
    db = _hcl_load(ledger)
    maturity = _hcl_maturity(registry)
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
    header = "| Device | Firmware | Maturity | " + " | ".join(
        f"{c}{'†' if c in destructive else ''}" for c in cap_order
    ) + " |"
    out += [header, "|" + "---|" * (len(cap_order) + 3)]
    for vendor, product, fw in sorted(combos):
        agg = combos[(vendor, product, fw)]
        level = maturity.get((vendor.lower(), product.lower(), fw.lower()), "—")
        cells = " | ".join(_hcl_cell(agg, c, c in destructive) for c in cap_order)
        out.append(f"| **{vendor} {product}** | {fw} | {level} | {cells} |")
    out += [
        "",
        f"Legend: ✅ all-pass · ⚠️ mixed · ❌ all-fail · … insufficient (n<{HCL_MIN_SAMPLES}) "
        "· · not tested",
        "",
        "Maturity (alpha→beta→rc→ga) is **derived from live runs only** by the "
        "promotion ladder (#98/#103) and committed in the firmware registry; "
        "`—` = no live-derived rating (e.g. synthetic-only rows).",
        "",
    ]
    return "\n".join(out)


def _rewrite_target(target: str, src_dir: str) -> str:
    """Map a link target as written in the repo to its flat-wiki equivalent.

    ``src_dir`` is the repo-relative directory of the source page (``docs``,
    ``src/kvm_pilot/skill``, ``src/kvm_pilot/mcp``) so remaining relative targets
    can be resolved.
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
    if "mcp_server" in path or "kvm_pilot/mcp/" in path or lower == "mcp-server.md":
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

    for src_rel, wiki_name, _, _ in PAGES:
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

    # Sidebar navigation: Home first, then one heading per SECTIONS entry with
    # its pages in PAGES order. The generated HCL page rides under Reference.
    by_section: dict[str, list[str]] = {}
    for _, wiki_name, title, section in PAGES:
        if title is None or section is None:
            continue
        by_section.setdefault(section, []).append(f"- [[{title}|{Path(wiki_name).stem}]]")
    if hcl_built:
        by_section.setdefault("Reference", []).append(
            f"- [[Hardware compatibility|{Path(HCL_PAGE).stem}]]"
        )
    unknown = set(by_section) - set(SECTIONS)
    if unknown:
        raise SystemExit(f"PAGES section(s) not in SECTIONS: {sorted(unknown)}")
    lines = ["### kvm-pilot docs", "", "- [[Home]]"]
    for section in SECTIONS:
        if section in by_section:
            lines += ["", f"#### {section}", ""] + by_section[section]
    (out / "_Sidebar.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"built {len(PAGES)} pages{' + HCL' if hcl_built else ''} + sidebar into {out}")


def unregistered_docs() -> list[str]:
    """Repo docs pages missing from ``PAGES`` (the wiki publishes an allowlist,
    not a glob, so an unregistered page silently never syncs — #175)."""
    registered = {p[0] for p in PAGES}
    candidates = sorted(
        p.relative_to(ROOT).as_posix()
        for pattern in ("docs/*.md", "docs/analysis/*.md")
        for p in ROOT.glob(pattern)
    )
    return [p for p in candidates if p not in registered and p not in OPT_OUT]


def _page_links(src_rel: str) -> list[str]:
    """Every markdown link target in a page, resolved to a repo-relative posix
    path (anchor stripped). External/absolute/in-page targets are skipped."""
    text = (ROOT / src_rel).read_text(encoding="utf-8")
    out = []
    for m in _LINK.finditer(text):
        target = m.group(2)
        if re.match(r"^(https?:|#|mailto:|/)", target):
            continue
        path, _, _ = target.partition("#")
        if not path:
            continue
        out.append(posixpath.normpath(posixpath.join(posixpath.dirname(src_rel), path)))
    return out


def hub_missing_links() -> list[str]:
    """The ``docs/README.md`` hub must link every registered page (#221) and
    must not link a missing file — the hub is the human nav, PAGES the truth."""
    hub = "docs/README.md"
    linked = set(_page_links(hub))
    problems = [
        f"{hub}: dead link to {path}" for path in sorted(linked)
        if not (ROOT / path).exists()
    ]
    problems += [
        f"{hub}: missing link to {src} (every PAGES entry must be in the hub)"
        for src, _, _, _ in PAGES
        if src != hub and src not in linked
    ]
    return problems


def llms_coverage() -> list[str]:
    """``llms.txt`` is the agent-facing doc map: every raw URL in it must point
    at a real repo file, and every PAGES source must be listed or in
    ``LLMS_OPT_OUT`` (#221)."""
    llms = ROOT / "llms.txt"
    text = llms.read_text(encoding="utf-8")
    raw_paths = re.findall(
        r"https://raw\.githubusercontent\.com/DustinTrap/kvm-pilot/main/([^)\s]+)", text
    )
    problems = [
        f"llms.txt: dead link to {path}" for path in sorted(set(raw_paths))
        if not (ROOT / path).exists()
    ]
    listed = set(raw_paths)
    problems += [
        f"llms.txt: missing {src} (list it, or add to LLMS_OPT_OUT with a reason)"
        for src, _, _, _ in PAGES
        if src not in listed and src not in LLMS_OPT_OUT
    ]
    return problems


def dead_relative_links() -> list[str]:
    """Relative link targets in every registered page must exist in the repo."""
    return [
        f"{src}: dead link to {path}"
        for src, _, _, _ in PAGES
        for path in sorted(set(_page_links(src)))
        if not (ROOT / path).exists()
    ]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--out", type=Path, help="output directory for wiki pages")
    mode.add_argument(
        "--check", action="store_true",
        help="fail if the docs navigation surfaces drift: unregistered pages "
        "(#175), hub/llms.txt coverage, dead relative links (#221)",
    )
    args = ap.parse_args()
    if args.check:
        problems = [
            f"{p}: not registered in PAGES (add it there, or to OPT_OUT with a reason)"
            for p in unregistered_docs()
        ] + hub_missing_links() + llms_coverage() + dead_relative_links()
        for problem in problems:
            print(problem)
        raise SystemExit(1 if problems else 0)
    build(args.out)


if __name__ == "__main__":
    main()
