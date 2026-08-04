"""Tests for the coverage ratchet gate (#246).

Hermetic: synthetic cobertura fixtures in a temp dir, no real suite, no network.
The gate is a *control* — an untested control is a hope, not a guarantee — so
every branch that can wave a build through gets a test, especially the
degenerate inputs where a naive gate would report success.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_GATE = Path(__file__).resolve().parents[1] / "scripts" / "coverage_gate.py"


def _load():
    spec = importlib.util.spec_from_file_location("coverage_gate", _GATE)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["coverage_gate"] = mod
    spec.loader.exec_module(mod)
    return mod


gate = _load()


def _report(tmp_path: Path, files: dict[str, tuple[int, int]]) -> Path:
    """A cobertura report: ``{filename: (hit_lines, missed_lines)}``."""
    classes = []
    for name, (hit, missed) in files.items():
        lines = "".join(
            f'<line number="{i + 1}" hits="1"/>' for i in range(hit)
        ) + "".join(
            f'<line number="{hit + i + 1}" hits="0"/>' for i in range(missed)
        )
        classes.append(f'<class filename="{name}"><lines>{lines}</lines></class>')
    xml = (
        '<?xml version="1.0" ?><coverage><packages><package><classes>'
        + "".join(classes)
        + "</classes></package></packages></coverage>"
    )
    path = tmp_path / "coverage.xml"
    path.write_text(xml, encoding="utf-8")
    return path


def _floor(tmp_path: Path, value: str) -> Path:
    path = tmp_path / ".coverage-floor"
    path.write_text(value, encoding="utf-8")
    return path


def _run(tmp_path, files, floor, monkeypatch=None, summary: Path | None = None):
    report = _report(tmp_path, files)
    floor_file = _floor(tmp_path, floor)
    if monkeypatch is not None:
        if summary is not None:
            monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
        else:
            monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    return gate.main(["--report", str(report), "--floor", str(floor_file)])


# -- the two states that matter --------------------------------------------


def test_passes_above_the_floor(tmp_path, monkeypatch, capsys):
    rc = _run(tmp_path, {"src/a.py": (90, 10)}, "85.0", monkeypatch)
    assert rc == 0
    assert "90.00%" in capsys.readouterr().out


def test_fails_below_the_floor(tmp_path, monkeypatch, capsys):
    rc = _run(tmp_path, {"src/a.py": (80, 20)}, "85.0", monkeypatch)
    assert rc == 1
    out = capsys.readouterr().out
    assert "below the floor" in out
    assert "5.00 points" in out  # names the gap, not just the failure


def test_exactly_at_the_floor_passes(tmp_path, monkeypatch):
    # Boundary: the floor is a minimum, not a threshold to exceed. Float
    # comparison here is why the gate carries an epsilon.
    assert _run(tmp_path, {"src/a.py": (85, 15)}, "85.0", monkeypatch) == 0


# -- exclusions really leave the denominator -------------------------------


def test_exclusions_are_removed_from_the_denominator(tmp_path, monkeypatch, capsys):
    """A fully-uncovered excluded file must not drag the percentage down.

    Without this, a version constant or codegen blob distorts the number and the
    floor stops meaning anything.
    """
    files = {
        "src/kvm_pilot/a.py": (90, 10),        # 90%
        "src/kvm_pilot/__about__.py": (0, 50),  # excluded — would drop it to 60%
    }
    rc = _run(tmp_path, files, "85.0", monkeypatch)
    assert rc == 0
    out = capsys.readouterr().out
    assert "90.00%" in out and "100/150" not in out
    assert "1 file(s) excluded" in out


@pytest.mark.parametrize("name", [
    "src/kvm_pilot/__about__.py",
    "src/pkg/__main__.py",
    "src/pkg/schema.g.py",
    "src/pkg/api_pb2.py",
    "src/pkg/generated_models.py",
    "__about__.py",           # top-level, no directory component
])
def test_exclusion_patterns_match_what_they_claim(name):
    assert gate.is_excluded(name), name


@pytest.mark.parametrize("name", [
    "src/kvm_pilot/cli.py",
    "src/kvm_pilot/mcp/server.py",      # the biggest gap — must stay measured
    "src/kvm_pilot/safety.py",
    "src/kvm_pilot/generator.py",       # 'generator' != 'generated'
])
def test_real_source_is_never_excluded(name):
    assert not gate.is_excluded(name), name


# -- the nudge is what makes the ratchet move ------------------------------


def test_nudge_fires_when_the_gain_is_worth_banking(tmp_path, monkeypatch, capsys):
    summary = tmp_path / "summary.md"
    rc = _run(tmp_path, {"src/a.py": (90, 10)}, "85.0", monkeypatch, summary=summary)
    assert rc == 0
    written = summary.read_text(encoding="utf-8")
    assert "can be raised" in written
    assert "**89.5**" in written  # measured 90.00 - 0.5 buffer
    assert "never applied" in written  # no auto-commit promise is explicit


def test_nudge_stays_quiet_inside_the_margin(tmp_path, monkeypatch, capsys):
    """A 0.5pt clearance is noise, not a gain — nudging on it trains people to
    ignore the nudge."""
    summary = tmp_path / "summary.md"
    rc = _run(tmp_path, {"src/a.py": (855, 145)}, "85.0", monkeypatch, summary=summary)
    assert rc == 0  # 85.50% — above the floor
    body = summary.read_text(encoding="utf-8") if summary.exists() else ""
    assert "can be raised" not in body
    assert "passed" in body            # a quiet pass still reports the verdict
    assert "passed" in capsys.readouterr().out


def test_failure_is_reported_to_the_step_summary(tmp_path, monkeypatch):
    summary = tmp_path / "summary.md"
    rc = _run(tmp_path, {"src/a.py": (10, 90)}, "85.0", monkeypatch, summary=summary)
    assert rc == 1
    assert "below the floor" in summary.read_text(encoding="utf-8")


def test_a_broken_summary_file_never_fails_the_build(tmp_path, monkeypatch):
    # The summary is reporting, not gating: if CI hands us an unwritable path the
    # verdict must still stand on its own.
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "nope" / "s.md"))
    report = _report(tmp_path, {"src/a.py": (90, 10)})
    floor = _floor(tmp_path, "85.0")
    assert gate.main(["--report", str(report), "--floor", str(floor)]) == 0


# -- degenerate inputs must never look like a pass -------------------------


def test_fails_when_nothing_is_instrumented(tmp_path, monkeypatch, capsys):
    """The gate catching its own plumbing break: a test step that ran without
    coverage produces an empty report, which must not read as success."""
    rc = _run(tmp_path, {}, "85.0", monkeypatch)
    assert rc == 2
    assert "no instrumented lines" in capsys.readouterr().err


def test_fails_when_everything_was_excluded(tmp_path, monkeypatch, capsys):
    rc = _run(tmp_path, {"src/pkg/__about__.py": (5, 5)}, "85.0", monkeypatch)
    assert rc == 2
    assert "after exclusions" in capsys.readouterr().err


def test_fails_when_the_floor_file_is_missing(tmp_path, monkeypatch, capsys):
    report = _report(tmp_path, {"src/a.py": (90, 10)})
    rc = gate.main(["--report", str(report), "--floor", str(tmp_path / "absent")])
    assert rc == 2
    assert "missing or unreadable" in capsys.readouterr().err


@pytest.mark.parametrize("content", ["", "   ", "not-a-number", "eighty five"])
def test_fails_when_the_floor_file_is_unparseable(tmp_path, capsys, content):
    report = _report(tmp_path, {"src/a.py": (90, 10)})
    floor = _floor(tmp_path, content)
    rc = gate.main(["--report", str(report), "--floor", str(floor)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "empty" in err or "does not contain a number" in err


def test_fails_when_the_report_is_missing(tmp_path, capsys):
    floor = _floor(tmp_path, "85.0")
    rc = gate.main(["--report", str(tmp_path / "absent.xml"), "--floor", str(floor)])
    assert rc == 2
    assert "missing" in capsys.readouterr().err


def test_fails_when_the_report_is_not_xml(tmp_path, capsys):
    bad = tmp_path / "coverage.xml"
    bad.write_text("<<< not xml", encoding="utf-8")
    floor = _floor(tmp_path, "85.0")
    rc = gate.main(["--report", str(bad), "--floor", str(floor)])
    assert rc == 2
    assert "not parseable" in capsys.readouterr().err


# -- the committed floor is real and honest --------------------------------


def test_the_repo_floor_is_present_and_sane():
    root = Path(__file__).resolve().parents[1]
    floor = gate.read_floor(root / gate.FLOOR_FILE)
    assert 0.0 < floor <= 100.0
    # A floor at/above 100 can never pass; a floor at 0 is not a gate.
    assert floor >= 50.0, "a floor this low is not gating anything meaningful"


def test_ci_actually_runs_the_gate():
    """The ratchet only gates if CI invokes it.

    pyproject's `fail_under` was deliberately removed in favour of this gate
    (#246), so nothing else would catch a collapse if the step were dropped —
    which makes 'is the step still wired?' part of the control, not trivia.
    """
    ci = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml")
    text = ci.read_text(encoding="utf-8")
    assert "scripts/coverage_gate.py" in text, "the CI test job no longer runs the coverage gate"
    assert "--cov" in text, "the test step no longer produces a coverage report"


def test_no_second_coverage_floor_competes_with_the_ratchet():
    """A `fail_under` in pyproject would fire before the gate and report a
    different number (branch vs line coverage)."""
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml")
    body = pyproject.read_text(encoding="utf-8")
    active = [ln for ln in body.splitlines()
              if ln.strip().startswith("fail_under") and not ln.strip().startswith("#")]
    assert not active, f"a second coverage floor is configured: {active}"


# -- per-category reporting (#246) -----------------------------------------


def test_categories_are_first_match_wins():
    """Order matters: a specific category must not be re-claimed by a broader
    one below it, or `mcp/act.py` lands in 'MCP server' and the approval path
    stops being visible as its own line."""
    assert gate.category_of("src/kvm_pilot/mcp/act.py") == "Safety & approval"
    assert gate.category_of("src/kvm_pilot/safety.py") == "Safety & approval"
    assert gate.category_of("src/kvm_pilot/mcp/server.py") == "MCP server"
    assert gate.category_of("src/kvm_pilot/drivers/glkvm.py") == "Drivers"
    assert gate.category_of("src/kvm_pilot/client.py") == "Drivers"
    assert gate.category_of("src/kvm_pilot/nowhere.py") == "Other"


def test_every_category_pattern_claims_something_real():
    """A category matching nothing is a stale pattern quietly reporting 0 files."""
    import xml.etree.ElementTree as ET

    root = Path(__file__).resolve().parents[1]
    report = root / "coverage.xml"
    if not report.exists():
        pytest.skip("no coverage.xml in the tree (run pytest --cov first)")
    seen = {
        gate.category_of(cls.get("filename") or "")
        for cls in ET.parse(report).getroot().iter("class")
    }
    named = {name for name, _ in gate.CATEGORIES}
    assert not (named - seen), f"categories matching no file: {sorted(named - seen)}"


def test_breakdown_is_ordered_worst_first(tmp_path, monkeypatch):
    summary = tmp_path / "s.md"
    files = {
        "src/kvm_pilot/drivers/a.py": (95, 5),    # Drivers  95%
        "src/kvm_pilot/mcp/server.py": (40, 60),  # MCP      40%
        "src/kvm_pilot/cli.py": (70, 30),         # CLI      70%
    }
    _run(tmp_path, files, "50.0", monkeypatch, summary=summary)
    body = summary.read_text(encoding="utf-8")
    order = [body.index(c) for c in ("MCP server", "CLI", "Drivers")]
    assert order == sorted(order), "the worst category must be listed first"


def test_breakdown_appears_on_pass_and_on_failure(tmp_path, monkeypatch):
    # 89.5 keeps the clearance inside the nudge margin so this exercises the
    # PLAIN pass path; 99.0 exercises the failure path.
    for floor, expect in (("89.5", "passed"), ("99.0", "below the floor")):
        summary = tmp_path / f"s{floor}.md"
        _run(tmp_path, {"src/kvm_pilot/drivers/a.py": (90, 10)},
             floor, monkeypatch, summary=summary)
        body = summary.read_text(encoding="utf-8")
        assert expect in body
        assert "| Category |" in body, "the breakdown must accompany every verdict"


def test_watch_trigger_reports_but_never_fails(tmp_path, monkeypatch):
    """Tripping the approval-path threshold starts a conversation, not a build
    failure — the gate's verdict stays the blended number."""
    summary = tmp_path / "s.md"
    files = {
        "src/kvm_pilot/mcp/act.py": (80, 20),     # 80% — under the 90 watch floor
        "src/kvm_pilot/drivers/a.py": (99, 1),    # keeps the blend well above
    }
    rc = _run(tmp_path, files, "85.0", monkeypatch, summary=summary)
    assert rc == 0, "a tripped watch threshold must not fail the build"
    body = summary.read_text(encoding="utf-8")
    assert "revisit trigger met" in body and "act.py" in body


def test_watch_trigger_silent_when_the_approval_path_is_healthy(tmp_path, monkeypatch):
    summary = tmp_path / "s.md"
    files = {"src/kvm_pilot/mcp/act.py": (95, 5), "src/kvm_pilot/drivers/a.py": (90, 10)}
    _run(tmp_path, files, "85.0", monkeypatch, summary=summary)
    assert "revisit trigger" not in summary.read_text(encoding="utf-8")
