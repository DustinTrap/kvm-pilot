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
    text = _SKILL.read_text(encoding="utf-8")
    start = text.index("**The tools it exposes**")
    end = text.index("**Approval posture", start)
    section = text[start:end]
    # Tool names are lowercase_with_underscores in backticks. Env gates are
    # uppercase and kwargs contain "=", so neither matches this shape.
    return set(re.findall(r"`([a-z][a-z0-9_]*)`", section))


def test_skill_tool_list_matches_server_surface():
    """SKILL.md's "tools it exposes" list == the registered MCP tools.

    EXPECTED_TOOLS is itself asserted against the live server's list_tools()
    in test_mcp_server, so this transitively pins the skill doc to the real
    surface. The list stays hand-curated prose — the guard only ensures no
    tool is missing and no stale/phantom name survives.
    """
    listed = _skill_listed_tools()
    missing = EXPECTED_TOOLS - listed
    phantom = listed - EXPECTED_TOOLS
    assert not missing, (
        f"SKILL.md 'tools it exposes' is missing MCP tools: {sorted(missing)}"
    )
    assert not phantom, (
        f"SKILL.md 'tools it exposes' names things that are not registered "
        f"tools (stale or typo): {sorted(phantom)}"
    )
