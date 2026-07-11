"""Supply-chain hygiene: GitHub Actions must stay SHA-pinned (#58).

A mutable tag (e.g. ``@v7`` or ``@release/v1``) lets an upstream compromise run
arbitrary code — with PyPI OIDC publish rights on the release path. Every
``uses:`` must reference a full 40-char commit SHA (with a version comment).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_WORKFLOWS = sorted((Path(__file__).resolve().parents[1] / ".github" / "workflows").glob("*.yml"))
_USES = re.compile(r"uses:\s*(\S+)")
_SHA_PINNED = re.compile(r"^[^@]+@[0-9a-f]{40}$")


def test_workflows_exist():
    assert _WORKFLOWS, "no workflow files found"


@pytest.mark.parametrize("wf", _WORKFLOWS, ids=[w.name for w in _WORKFLOWS])
def test_every_action_is_sha_pinned(wf: Path):
    refs = _USES.findall(wf.read_text())
    assert refs, f"{wf.name} has no `uses:` steps"
    unpinned = [r for r in refs if not _SHA_PINNED.match(r)]
    assert not unpinned, f"{wf.name} has non-SHA-pinned actions: {unpinned}"


def test_dependabot_config_present():
    cfg = Path(__file__).resolve().parents[1] / ".github" / "dependabot.yml"
    assert cfg.exists(), "dependabot.yml missing — SHA pins won't be kept fresh"
    text = cfg.read_text()
    assert "github-actions" in text


# -- release path must be gated (#57) --------------------------------------

def _release_yml() -> str:
    return (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "release.yml").read_text()


def test_release_publish_needs_a_test_gate():
    rel = _release_yml()
    assert "run: pytest" in rel, "release.yml must run pytest before publishing"
    # The publish job must be gated on both `build` and `test`; additional gates
    # (e.g. the `smoke-install` pre-publish check) are allowed, so match the
    # publish job's own `needs:` list and require build+test to be present.
    m = re.search(r"^  publish:\n(?:.*\n)*?    needs:\s*\[([^\]]+)\]", rel, re.M)
    assert m, "publish job must declare a `needs:` list"
    needs = {n.strip() for n in m.group(1).split(",")}
    assert {"build", "test"} <= needs, \
        f"publish must gate on at least build+test; got needs={sorted(needs)}"


def test_release_verifies_tag_matches_version():
    rel = _release_yml()
    # a step derives the version from the tag and greps the built artifacts
    assert "GITHUB_REF_NAME" in rel and "kvm_pilot-${V}" in rel, \
        "release.yml must verify the built artifact version matches the release tag"


# -- firmware ingest must park invalid submissions (#188) -------------------

def test_firmware_ingest_parks_invalid_submissions():
    """An invalid firmware-report gets ONE ❌ comment + the ingest-error label and
    leaves the hourly queue — without this, a malformed issue is re-commented
    forever (#177/#180 spammed hourly for 3 days)."""
    wf = (Path(__file__).resolve().parents[1] / ".github" / "workflows"
          / "firmware-ingest.yml").read_text()
    assert "-label:ingest-error" in wf, "selection query must exclude parked issues"
    assert re.search(r"invalid\).*--add-label ingest-error", wf), \
        "the invalid branch must park the issue with the ingest-error label"
    assert "gh label create ingest-error" in wf, "the label must be bootstrapped"
