"""Guards that keep the user-facing docs truthful (#209).

Two drift classes actually shipped before these existed: the README status
line froze at v0.1.0b2 while releases moved on to b5+ (the README is the PyPI
long-description, so the stale claim was published), and the bundled skill's
tool list silently lost 5 of the MCP server's tools. These tests turn both
drifts into a test failure instead of a doc-review catch.
"""

from __future__ import annotations

import re
from pathlib import Path

from kvm_pilot.__about__ import __version__
from test_mcp_server import EXPECTED_TOOLS

_ROOT = Path(__file__).resolve().parents[1]
_README = _ROOT / "README.md"
_SKILL = _ROOT / "src" / "kvm_pilot" / "skill" / "SKILL.md"
_SKILL_REFS = _ROOT / "src" / "kvm_pilot" / "skill" / "references"
# The skill's install/MCP-enablement doctrine moved to a reference file (#222);
# the guards follow the content, not the filename.
_SKILL_SETUP = _SKILL_REFS / "setup.md"
_GETTING_STARTED = _ROOT / "docs" / "getting-started.md"
_MCP_README = _ROOT / "src" / "kvm_pilot" / "mcp" / "README.md"

# Version literals allowed to differ from __about__.__version__: the yanked
# first alpha, which the README warns readers away from by name.
_ALLOWED_STALE = {"0.1.0a1"}

# Optional leading "v" ("v0.1.0b2" has no word boundary between v and 0, so a
# bare \b0\. pattern silently misses exactly the string that shipped stale);
# generalized numerics so a future 0.2.x line stays guarded.
_VERSION_RE = re.compile(r"\bv?(\d+\.\d+\.\d+(?:a|b|rc)\d+)\b")


def test_readme_version_literals_match_package_version():
    """Any concrete version literal in README.md must be the shipped version.

    Status prose should stay version-agnostic (CLAUDE.md: "don't restate it,
    it drifts"); this guard allows the current version and the yanked-release
    warning, nothing else.
    """
    found = set(_VERSION_RE.findall(_README.read_text(encoding="utf-8")))
    stale = found - _ALLOWED_STALE - {__version__}
    assert not stale, (
        f"README.md hard-codes version(s) {sorted(stale)} but the package is "
        f"{__version__} — make the prose version-agnostic or update it"
    )


def _skill_listed_tools() -> set[str]:
    text = _SKILL_SETUP.read_text(encoding="utf-8")
    start = text.index("**The tools it exposes**")
    end = text.index("**Approval posture", start)
    section = text[start:end]
    # Tool names are lowercase_with_underscores in backticks. Env gates are
    # uppercase and kwargs contain "=", so neither matches this shape.
    return set(re.findall(r"`([a-z][a-z0-9_]*)`", section))


def test_skill_tool_list_matches_server_surface():
    """The skill's "tools it exposes" list == the registered MCP tools.

    EXPECTED_TOOLS is itself asserted against the live server's list_tools()
    in test_mcp_server, so this transitively pins the skill doc to the real
    surface. The list stays hand-curated prose — the guard only ensures no
    tool is missing and no stale/phantom name survives.
    """
    listed = _skill_listed_tools()
    missing = EXPECTED_TOOLS - listed
    phantom = listed - EXPECTED_TOOLS
    assert not missing, (
        f"skill references/setup.md 'tools it exposes' is missing MCP tools: "
        f"{sorted(missing)}"
    )
    assert not phantom, (
        f"skill references/setup.md 'tools it exposes' names things that are "
        f"not registered tools (stale or typo): {sorted(phantom)}"
    )


# The install command is duplicated across every self-sufficient surface (the
# shipped SKILL.md / mcp README must work offline, README is the PyPI page).
# This guard turns a many-file drift into one failure — it will fire usefully
# at GA, when `--pre` stops being the working command everywhere at once.
_INSTALL_CMD = "pip install --pre kvm-pilot"
_INSTALL_DOCS = (_README, _GETTING_STARTED, _SKILL_SETUP, _MCP_README)
# A bare `pip install kvm-pilot` may appear only when *warning* that it does
# nothing on a pre-release line, as the named batteries-included doctrine
# ("`pip install kvm-pilot` ships everything", CLAUDE.md), or as a VCS install
# (`@ git+...`, which ignores pre-release gating) — never as a working
# release-install instruction. These words mark the allowed contexts.
_BARE_OK_WORDS = ("deliberately", "pre-release", "nothing", "ships everything", "git+")


def _current_doc_files() -> list[Path]:
    """Docs that describe the present. Dated records (decisions.md entries,
    docs/analysis/ narratives) quote history verbatim and are never edited to
    track the current command line."""
    historical = {_ROOT / "docs" / "decisions.md"}
    return (
        [p for p in sorted(_ROOT.glob("docs/*.md")) if p not in historical]
        + sorted(_SKILL_REFS.glob("*.md"))
        + [_README, _SKILL, _MCP_README]
    )


def test_install_command_consistent():
    """The working install command appears verbatim on every install surface,
    and a bare (broken) install is only ever shown as a warning."""
    for doc in _INSTALL_DOCS:
        assert _INSTALL_CMD in doc.read_text(encoding="utf-8"), (
            f"{doc.relative_to(_ROOT)}: missing the canonical install command "
            f"{_INSTALL_CMD!r}"
        )
    bare = re.compile(r'pip install "?kvm-pilot')
    for doc in _current_doc_files():
        # Collapse whitespace so a command wrapped across a line break (as
        # markdown prose does) still matches.
        flat = " ".join(doc.read_text(encoding="utf-8").split())
        for m in bare.finditer(flat):
            window = flat[max(0, m.start() - 120): m.end() + 120]
            assert any(w in window for w in _BARE_OK_WORDS), (
                f"{doc.relative_to(_ROOT)}: shows `pip install kvm-pilot` "
                f"without --pre as if it works (context: ...{window}...) — "
                f"use {_INSTALL_CMD!r}, or mark it as the deliberate "
                f"no-pre-release warning"
            )


def test_skill_description_within_budget():
    """The frontmatter description is the skill's only always-loaded part —
    trigger-matching favors <=1024 chars; doctrine belongs in the body (#227)."""
    text = _SKILL.read_text(encoding="utf-8")
    m = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
    assert m, "SKILL.md must open with YAML frontmatter"
    desc_lines = []
    in_desc = False
    for line in m.group(1).splitlines():
        if line.startswith("description:"):
            in_desc = True
            continue
        if in_desc:
            if line.startswith("  "):
                desc_lines.append(line.strip())
            else:
                break
    description = " ".join(desc_lines)
    assert description, "no description found in SKILL.md frontmatter"
    assert len(description) <= 1024, (
        f"SKILL.md description is {len(description)} chars (budget 1024) — "
        "move doctrine into the body; keep the description trigger-focused"
    )


def test_install_skill_command_documented():
    """`kvm-pilot install-skill` (#226) is the one bridge between `pip install`
    and Claude Code actually discovering the skill — every install surface must
    name it verbatim, or the skill silently stays undiscovered package data."""
    cmd = "kvm-pilot install-skill"
    for doc in (_README, _GETTING_STARTED, _SKILL_SETUP):
        assert cmd in doc.read_text(encoding="utf-8"), (
            f"{doc.relative_to(_ROOT)}: missing the skill install command {cmd!r}"
        )
    # cli.md documents it as a table row (bare subcommand, house style there).
    assert "`install-skill`" in (_ROOT / "docs" / "cli.md").read_text(encoding="utf-8")


def _mcp_add_snippet(path: Path) -> str:
    """The `claude mcp add kvm-pilot ... kvm-pilot-mcp` command block."""
    text = path.read_text(encoding="utf-8")
    m = re.search(
        r"^(claude mcp add kvm-pilot.*?^\s*kvm-pilot-mcp\s*$)",
        text, re.MULTILINE | re.DOTALL,
    )
    assert m, f"{path.relative_to(_ROOT)}: no `claude mcp add kvm-pilot` snippet"
    return m.group(1).strip()


def test_mcp_add_snippet_consistent():
    """The MCP registration command stays identical across its three homes.

    README and getting-started must match byte-for-byte (both show a human's
    first contact). SKILL.md deliberately demos `KVM_PILOT_MCP_DRY_RUN=1`
    (agent rehearsal posture) where the other two demo the safest first rung
    `KVM_PILOT_MCP_READ_ONLY=1` — that one env gate is the only allowed
    difference; everything else (server name, `-s user` scope, profile env,
    launcher) is pinned.
    """
    canonical = _mcp_add_snippet(_GETTING_STARTED)
    assert _mcp_add_snippet(_README) == canonical, (
        "README.md's `claude mcp add` snippet differs from getting-started.md"
    )
    skill = _mcp_add_snippet(_SKILL_SETUP)
    assert skill.replace("KVM_PILOT_MCP_DRY_RUN", "KVM_PILOT_MCP_READ_ONLY") == canonical, (
        "the skill's setup.md `claude mcp add` snippet differs from "
        "getting-started.md beyond the deliberate DRY_RUN-vs-READ_ONLY "
        "trust-ladder gate"
    )
