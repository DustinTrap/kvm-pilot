"""End-to-end test for the community-HCL wiki page generation (#105).

Exercises the whole slice the pipeline hinges on: a JSONL run ledger is loaded
into ephemeral SQLite, aggregated to a (device x capability) matrix with a
min-sample gate, and rendered to the wiki markdown page — including the way the
real RM1PE firmware-update failure must surface as a ``0%`` cell.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / ".github" / "scripts" / "build_wiki.py"


@pytest.fixture(scope="module")
def build_wiki():
    """Import the standalone build_wiki.py script as a module."""
    spec = importlib.util.spec_from_file_location("build_wiki", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules["build_wiki"] = mod
    spec.loader.exec_module(mod)
    return mod


def _write_ledger(path: Path, runs: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(r) for r in runs) + "\n", encoding="utf-8")
    return path


def _run(run_id, vendor, product, fw, caps, source="synthetic"):
    return {
        "run_id": run_id, "source": source, "vendor": vendor, "product": product,
        "firmware_version": fw, "kvm_pilot_version": "0.1.0a1", "driver": "glkvm",
        "os_family": "linux", "python_version": "3.13", "utc_date": "2026-07-03T00:00:00Z",
        "capabilities": [{"capability": c, "passed": p, "outcome": ""} for c, p in caps],
    }


def test_failing_destructive_cap_renders_zero_percent(build_wiki, tmp_path):
    # 3 runs of one combo, all with firmware_update failing -> ❌ 0% (n=3), flagged †.
    runs = [
        _run(f"r{i}", "gl.inet", "RM1PE", "V1.5.1 release2",
             [("info", True), ("firmware_update", False)])
        for i in range(3)
    ]
    page = build_wiki.render_hcl(_write_ledger(tmp_path / "l.jsonl", runs))
    assert "Hardware Compatibility" in page
    assert "gl.inet RM1PE" in page
    assert "❌&nbsp;0%†" in page          # the real signal: flash reported broken
    assert "✅&nbsp;100%" in page         # info passes
    assert "n=3" in page


def test_below_threshold_is_insufficient_not_a_verdict(build_wiki, tmp_path):
    # Only 2 runs -> must render "insufficient", never a pass/fail verdict.
    runs = [
        _run(f"r{i}", "pikvm", "PiKVM v4", "4.82", [("info", True)])
        for i in range(2)
    ]
    page = build_wiki.render_hcl(_write_ledger(tmp_path / "l.jsonl", runs))
    assert "…&nbsp;n=2" in page
    assert "100%" not in page             # 2 < min samples -> no verdict emitted


def test_synthetic_data_shows_preview_banner(build_wiki, tmp_path):
    runs = [_run("r0", "dell", "iDRAC9", "6.10", [("info", True)], source="synthetic")]
    page = build_wiki.render_hcl(_write_ledger(tmp_path / "l.jsonl", runs))
    assert "PREVIEW" in page and "synthetic" in page


def test_idempotent_on_duplicate_run_id(build_wiki, tmp_path):
    # A re-submitted run_id must not double-count (matches the ingestion contract).
    runs = [
        _run("dup", "gl.inet", "RM1PE", "V1.5.1 release2", [("info", True)]),
        _run("dup", "gl.inet", "RM1PE", "V1.5.1 release2", [("info", True)]),
        _run("b", "gl.inet", "RM1PE", "V1.5.1 release2", [("info", True)]),
        _run("c", "gl.inet", "RM1PE", "V1.5.1 release2", [("info", True)]),
    ]
    page = build_wiki.render_hcl(_write_ledger(tmp_path / "l.jsonl", runs))
    assert "n=3" in page                  # dup collapsed -> 3 distinct runs, not 4


def test_full_build_emits_hcl_page_and_sidebar(build_wiki, tmp_path):
    # The real repo ledger drives build(); the generated page + sidebar link appear.
    out = tmp_path / "wiki"
    build_wiki.build(out)
    assert (out / "Hardware-Compatibility.md").exists()
    assert "Hardware compatibility" in (out / "_Sidebar.md").read_text()


def test_unattended_install_page_ships_on_both_surfaces(build_wiki, tmp_path):
    # #129's deliverable: the text-mode+SSH distro matrix must exist on the
    # durable docs surface (wiki page, navigable) AND on the in-wheel agent
    # surface (the bundled skill). build() itself SystemExits on any
    # unresolved relative link, so a passing build proves the links resolved.
    out = tmp_path / "wiki"
    build_wiki.build(out)
    page = (out / "unattended-install.md").read_text()
    assert "inst.sshd inst.text" in page      # the matrix made it to the wiki
    assert "network-console" in page
    # The compact rule ships on the in-wheel agent surface — since #222 it
    # lives in the skill's linux-install playbook, and the core SKILL.md must
    # route readers to it.
    assert "inst.sshd" in (out / "linux-install.md").read_text()
    assert "linux-install" in (out / "skill.md").read_text()
    assert "Unattended Linux installs" in (out / "_Sidebar.md").read_text()


def test_every_docs_page_is_registered(build_wiki):
    # The wiki publishes an allowlist (PAGES), not a glob: a docs page missing
    # from it silently never syncs (#175). This runs the CI guard locally too.
    assert build_wiki.unregistered_docs() == []


def test_unregistered_doc_is_detected(build_wiki, monkeypatch):
    # Drop one registered page from PAGES; the guard must name its source path.
    dropped = build_wiki.PAGES[1]
    monkeypatch.setattr(
        build_wiki, "PAGES", [p for p in build_wiki.PAGES if p is not dropped]
    )
    assert dropped[0] in build_wiki.unregistered_docs()


# --- #103: derived-maturity column on the generated page ---------------------


def _registry(tmp_path, entries):
    path = tmp_path / "reg.json"
    path.write_text(json.dumps({"schema_version": 2, "firmware": entries}))
    return path


def test_hcl_renders_derived_maturity_level(build_wiki, tmp_path):
    ledger = _write_ledger(tmp_path / "l.jsonl", [
        _run("r1", "gl.inet", "RM1PE", "V1.5.1 release2",
             [("snapshot", True)], source="real"),
    ])
    reg = _registry(tmp_path, [{
        "vendor": "GL.iNet", "product": "rm1pe",     # case-insensitive join
        "versions": [{"version": "v1.5.1 RELEASE2",
                      "maturity": {"level": "beta"}}],
    }])
    page = build_wiki.render_hcl(ledger, reg)
    row = next(line for line in page.splitlines() if "RM1PE" in line)
    assert "| beta |" in row


def test_hcl_maturity_dash_when_no_derived_row(build_wiki, tmp_path):
    # Synthetic-only combos (and anything the registry has no derived row for)
    # honestly show a dash, never an invented level.
    ledger = _write_ledger(tmp_path / "l.jsonl", [
        _run("r1", "acme", "KVM1", "V1.0", [("info", True)]),
    ])
    page = build_wiki.render_hcl(ledger, _registry(tmp_path, []))
    row = next(line for line in page.splitlines() if "KVM1" in line)
    assert "| — |" in row
    assert "derived from live runs only" in page


def test_hcl_maturity_column_in_header(build_wiki, tmp_path):
    ledger = _write_ledger(tmp_path / "l.jsonl", [
        _run("r1", "acme", "KVM1", "V1.0", [("info", True)]),
    ])
    page = build_wiki.render_hcl(ledger, _registry(tmp_path, []))
    header = next(line for line in page.splitlines() if line.startswith("| Device"))
    assert "| Maturity |" in header


def test_readme_has_no_relative_links():
    # The README is the PyPI long description: a relative link/image resolves
    # against pypi.org and 404s there (#193) — every target must be absolute.
    import re

    readme = (_ROOT / "README.md").read_text(encoding="utf-8")
    bad = [
        target
        for target in re.findall(r"\]\(([^)]+)\)", readme)
        if not target.startswith(("http://", "https://", "#", "mailto:"))
    ]
    assert bad == [], f"relative links break on the PyPI page: {bad}"
