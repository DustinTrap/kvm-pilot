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
