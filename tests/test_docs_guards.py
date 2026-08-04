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
    """The canonical install command appears verbatim on every install surface.

    A bare ``pip install kvm-pilot`` must never be shown as the instruction. Not
    because it fails — measured 2026-08-03, it resolves to the current beta,
    since pip falls back to pre-releases when no stable release exists (#243
    corrected the docs, which claimed it "installs nothing") — but because that
    behavior flips silently the day a stable release ships. ``--pre`` is
    unambiguous in both worlds.
    """
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
                f"without --pre as the instruction (context: ...{window}...) — "
                f"use {_INSTALL_CMD!r}, or keep the surrounding prose that "
                f"explains why the bare form is not what you want"
            )


def test_mcp_readme_declares_the_real_mcp_dependency():
    """The mcp/README states the SDK range it pulls — it must be pyproject's.

    This one drifts silently: the README file itself need never change for the
    claim to go false, because the fact it states lives in pyproject. It went
    stale exactly that way when the floor moved off `mcp>=1.10` (#241), and the
    README both ships in the wheel and mirrors to the wiki.
    """
    declared = re.search(
        r'"(mcp(?:[><=!,.\d\s]+))"', (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert declared, "no mcp requirement found in pyproject [project].dependencies"
    spec = declared.group(1).replace(" ", "")
    text = _MCP_README.read_text(encoding="utf-8")
    quoted = {m.replace(" ", "") for m in re.findall(r"`(mcp[><=!,.\d]+)`", text)}
    assert quoted, "src/kvm_pilot/mcp/README.md no longer states the mcp SDK range"
    assert spec in quoted, (
        f"src/kvm_pilot/mcp/README.md says {sorted(quoted)} but pyproject "
        f"declares `{spec}` — update the README to the shipped range"
    )


def test_mcp_readme_tool_table_matches_surface():
    """#232: the mcp/README `## Tools` table is hand-curated prose, but its
    row set and destructive-claims must match the live server surface —
    a checker, deliberately not a generator (the prose is richer than the
    docstrings and should stay that way)."""
    from test_mcp_server import EXPECTED_ANNOTATIONS

    readme = _MCP_README.read_text(encoding="utf-8")
    tools_section = readme.split("## Tools", 1)[1].split("\n## ", 1)[0]
    rows = dict(re.findall(r"^\| `([a-z][a-z0-9_]*)` \|([^|]*)\|", tools_section, re.M))

    missing = EXPECTED_TOOLS - set(rows)
    phantom = set(rows) - EXPECTED_TOOLS
    assert not missing, f"mcp/README.md Tools table is missing: {sorted(missing)}"
    assert not phantom, f"mcp/README.md Tools table has stale rows: {sorted(phantom)}"

    for name, cell in rows.items():
        claims_destructive = "destructiveHint" in cell
        is_destructive = EXPECTED_ANNOTATIONS[name][1] is True
        assert claims_destructive == is_destructive, (
            f"mcp/README.md row `{name}` annotation cell says "
            f"{'destructive' if claims_destructive else 'non-destructive'} but the "
            f"registered annotation says the opposite — fix the doc or the tool"
        )


def test_cli_doc_table_matches_parser():
    """#232: docs/cli.md's command table vs the argparse surface, both
    directions — upgrades the checkpoint scan's advisory check into CI."""
    import argparse

    from kvm_pilot.cli import build_parser

    parser = build_parser()
    sub = next(
        a for a in parser._actions if isinstance(a, argparse._SubParsersAction)
    )
    parser_cmds = set(sub.choices)
    doc_cmds = set(
        re.findall(r"^\| `([a-z][a-z0-9-]*)`", (_ROOT / "docs" / "cli.md").read_text(
            encoding="utf-8"), re.M)
    )
    missing = parser_cmds - doc_cmds
    phantom = doc_cmds - parser_cmds
    assert not missing, f"docs/cli.md command table is missing: {sorted(missing)}"
    assert not phantom, f"docs/cli.md documents commands that don't exist: {sorted(phantom)}"


def test_tool_docstrings_within_budget():
    """Tool docstrings are an unconditional per-session token tax (#230): every
    registered tool's schema loads into every agent session. Keep the call-time
    contract in-schema; mechanism narrative belongs in the MCP README or the
    doctrine playbooks (both re-servable mid-session). Largest post-diet
    docstring is ~770 chars — 900 leaves headroom without letting the
    1,400-char monsters back in."""
    from kvm_pilot.mcp import server

    over = {
        name: len(getattr(server, name).__doc__ or "")
        for name in EXPECTED_TOOLS
        if len(getattr(server, name).__doc__ or "") > 900
    }
    assert not over, (
        f"tool docstring(s) over the 900-char budget: {over} — move mechanism "
        "narrative to mcp/README.md or a doctrine playbook"
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


def test_server_json_version_matches_the_package():
    """The MCP registry manifest must not drift from the shipped version.

    The release workflows rewrite ``server.json``'s version from the release tag
    at publish time, so a stale literal here never reaches the registry — but it
    misleads every human who reads the file (it sat at 0.1.0b8 through four
    releases, #243). Keeping it honest costs one guard.
    """
    import json

    manifest = json.loads((_ROOT / "server.json").read_text(encoding="utf-8"))
    versions = {manifest["version"], *(p["version"] for p in manifest["packages"])}
    assert versions == {__version__}, (
        f"server.json declares {sorted(versions)} but the package is {__version__}"
    )


def test_server_json_declares_every_env_var_it_documents():
    """An env var named in a description must also be declared.

    ``KVM_PILOT_PASSWD``'s description told registry users that
    ``KVM_PILOT_HOST``/``KVM_PILOT_USER`` accompany it for the no-profile
    connection path, while the manifest declared neither — so that path was not
    configurable from the registry entry (#243).
    """
    import json

    manifest = json.loads((_ROOT / "server.json").read_text(encoding="utf-8"))
    declared = {e["name"] for p in manifest["packages"] for e in p["environmentVariables"]}
    mentioned = set()
    for p in manifest["packages"]:
        for e in p["environmentVariables"]:
            mentioned |= set(re.findall(r"\bKVM_PILOT_[A-Z_]+\b", e["description"]))
    # Descriptions also use the "_ALLOW_*" shorthand for sibling gates; those are
    # documented in mcp/README.md, not required to be declared here.
    missing = {v for v in mentioned - declared if not v.startswith("KVM_PILOT_MCP_")}
    assert not missing, f"server.json documents {sorted(missing)} but declares none of them"


def test_every_mcp_env_gate_is_documented():
    """Each ``KVM_PILOT_MCP_*`` the server reads must appear in configuration.md.

    Four were undocumented — including ``KVM_PILOT_MCP_READ_ONLY``, the posture
    the docs elsewhere *recommend for first contact* (#243). An operator can't
    choose a control they can't find.
    """
    mcp_dir = _ROOT / "src" / "kvm_pilot" / "mcp"
    used = set()
    for src in mcp_dir.glob("*.py"):
        used |= set(re.findall(r"\bKVM_PILOT_MCP_[A-Z_]+\b", src.read_text(encoding="utf-8")))
    documented = (_ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    missing = sorted(v for v in used if v not in documented)
    assert not missing, f"MCP env gates read by the server but undocumented: {missing}"


def test_firmware_registry_updated_covers_its_own_evidence():
    """``updated`` must not predate the run-ledger evidence the entries derive from.

    It sat at 2026-07-03 while carrying maturity computed from July-15/18 runs, so
    anything judging registry freshness by that field got a wrong answer (#243).
    """
    import json

    data = _ROOT / "src" / "kvm_pilot" / "data"
    registry = json.loads((data / "firmware_registry.json").read_text(encoding="utf-8"))
    ledger_dates = [
        json.loads(line)["utc_date"][:10]
        for line in (data / "test_runs.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert registry["updated"] >= max(ledger_dates), (
        f"firmware_registry.json says updated={registry['updated']} but the run ledger "
        f"it derives from has evidence through {max(ledger_dates)}"
    )


def test_user_facing_surfaces_do_not_depend_on_an_extra():
    """Batteries-included (#109, #244): `pip install kvm-pilot` must give a
    working CLI/MCP surface, not one that errors until you find the right extra.

    Pillow is the case that caught this — `calibrate-mouse` is a CLI *and* MCP
    surface, and its JPEG/PNG decode sat behind `[calibrate]`, so a plain install
    shipped a command that could not run. `websocket-client` had already made the
    same move for `snapshot` (#142). Any future surface-level dependency belongs
    in `[project].dependencies` too.
    """
    import tomllib

    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    base = " ".join(pyproject["project"]["dependencies"]).lower()
    for dep in ("pillow", "mcp", "websocket-client"):
        assert dep in base, f"{dep} powers a user-facing surface and must be a base dependency"


def test_docs_do_not_tell_users_to_install_a_surface_extra():
    """No doc may present an extra as a prerequisite for a shipped surface."""
    offenders = []
    for doc in _current_doc_files():
        text = doc.read_text(encoding="utf-8")
        for extra in ("calibrate", "ws"):
            needle = f"kvm-pilot[{extra}]"
            if needle in text and "back-compat" not in text and "no-op" not in text:
                offenders.append(f"{doc.relative_to(_ROOT)} -> {needle}")
    assert not offenders, (
        "these docs still require a now-base dependency via an extra: " + ", ".join(offenders)
    )


def test_every_ledger_row_joins_a_registry_entry():
    """A run-ledger row that cannot join the firmware registry yields no derived
    maturity, silently.

    Found live: the hand-authored AMT rows carried an ``AMT `` prefix on
    ``firmware_version`` that the driver never emits, and the auto-generated row
    said vendor ``Dell Inc.`` where the ledger said ``dell`` — so one physical
    laptop produced three non-joining identities and `maturity: None`.
    """
    import json

    data = _ROOT / "src" / "kvm_pilot" / "data"
    registry = json.loads((data / "firmware_registry.json").read_text(encoding="utf-8"))
    known = {
        (e["vendor"], e["product"], v["version"])
        for e in registry["firmware"] for v in e.get("versions", [])
    }
    orphans = []
    for line in (data / "test_runs.jsonl").read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        if r.get("source") == "synthetic":
            continue  # emulator runs are not expected to name real firmware
        key = (r["vendor"], r["product"], r["firmware_version"])
        if key not in known:
            orphans.append(key)
    assert not orphans, (
        "ledger rows with no matching firmware-registry entry (they will derive no "
        f"maturity): {sorted(set(orphans))}"
    )
