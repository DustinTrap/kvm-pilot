"""The MCP Bundle (.mcpb) for Claude Desktop (#148).

The bundle's whole reason to exist is that someone installs it in one click, so
the thing that must be true is: **a one-click install cannot act on a machine.**
These tests enforce that, plus the version pin — a bundle that installs a
different kvm-pilot than it claims is worse than no bundle.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest

from kvm_pilot.__about__ import __version__

_ROOT = Path(__file__).resolve().parents[1]
_BUNDLE = _ROOT / "mcpb"
_MANIFEST = json.loads((_BUNDLE / "manifest.json").read_text(encoding="utf-8"))
_PYPROJECT = tomllib.loads((_BUNDLE / "pyproject.toml").read_text(encoding="utf-8"))

# Every effect gate the server actually reads. Sourced from the server, not from
# a list maintained here, so a new gate that the bundle forgets to surface fails
# this file rather than shipping silently unconfigurable.
_ALLOW_GATES = {
    m for path in (_ROOT / "src" / "kvm_pilot").rglob("*.py")
    for m in re.findall(r"\bKVM_PILOT_MCP_ALLOW_[A-Z_]+\b", path.read_text(encoding="utf-8"))
}


def _build_script():
    spec = importlib.util.spec_from_file_location(
        "build_mcpb", _ROOT / "scripts" / "build_mcpb.py"
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["build_mcpb"] = mod
    spec.loader.exec_module(mod)
    return mod


# -- the safety contract ---------------------------------------------------


@pytest.mark.parametrize("gate", sorted(_ALLOW_GATES))
def test_every_effect_gate_defaults_off(gate: str):
    """A one-click install must not be one click to an ungated power tool.

    Note this is belt AND braces: the server reads these with `env_flag`, so an
    unset var is already closed. The manifest default is the second layer — it
    also means the user SEES the capability exists and is off, rather than
    discovering it later.
    """
    key = gate.replace("KVM_PILOT_MCP_", "").lower()
    cfg = _MANIFEST["user_config"].get(key)
    assert cfg is not None, f"{gate} is read by the server but not offered in the bundle"
    assert cfg["type"] == "boolean"
    assert cfg["default"] is False, f"{gate} must default OFF"


def test_dry_run_defaults_on():
    """The one flag whose *unset* state is the unsafe direction — so the bundle
    has to positively set it, and set it to rehearse."""
    assert _MANIFEST["user_config"]["dry_run"]["default"] is True


def test_every_gate_is_wired_into_the_server_environment():
    """Declaring a config field that is never passed through would be a control
    that looks present and does nothing."""
    env = _MANIFEST["server"]["mcp_config"]["env"]
    for gate in _ALLOW_GATES | {"KVM_PILOT_MCP_DRY_RUN", "KVM_PILOT_MCP_READ_ONLY"}:
        assert gate in env, f"{gate} is not passed to the server"
        key = gate.replace("KVM_PILOT_MCP_", "").lower()
        assert env[gate] == f"${{user_config.{key}}}", f"{gate} is not bound to its config field"


def test_destructive_gates_describe_the_risk_not_just_the_feature():
    """An operator ticking a box needs to know what it costs them, in the moment
    they tick it — not in a doc they will not open."""
    cfg = _MANIFEST["user_config"]
    assert "DESTRUCTIVE" in cfg["allow_power"]["description"]
    assert "SECURITY POSTURE CHANGE" in cfg["allow_consent_off"]["description"]
    # The appliance reboot's specific hazard: no out-of-band power to the KVM.
    assert "physical access" in cfg["allow_appliance"]["description"]


def test_credentials_are_marked_sensitive():
    for key in ("password", "anthropic_api_key"):
        assert _MANIFEST["user_config"][key].get("sensitive") is True, key


# -- the version pin -------------------------------------------------------


def test_manifest_and_pin_track_the_package_version():
    assert _MANIFEST["version"] == __version__
    assert _PYPROJECT["project"]["version"] == __version__
    assert _PYPROJECT["project"]["dependencies"] == [f"kvm-pilot=={__version__}"]


def test_the_pin_is_exact_never_a_range():
    """A bundle is meant to be a reproducible artifact — '>=' would make it
    'whatever PyPI had that day'."""
    (dep,) = _PYPROJECT["project"]["dependencies"]
    assert "==" in dep and not any(op in dep for op in (">=", "<=", "~=", ">", "<"))


# -- manifest shape --------------------------------------------------------


def test_manifest_declares_what_the_spec_requires():
    for field in ("manifest_version", "name", "version", "description", "author", "server"):
        assert field in _MANIFEST, field
    assert _MANIFEST["author"].get("name")
    server = _MANIFEST["server"]
    assert server["type"] == "uv"
    assert server["entry_point"] == "server/main.py"


def test_the_declared_entry_point_exists_and_is_a_shim():
    entry = _BUNDLE / _MANIFEST["server"]["entry_point"]
    assert entry.exists()
    body = entry.read_text(encoding="utf-8")
    # It must delegate, not reimplement: a bundle user and a pip user run the
    # same server or the gates are only enforced in one of them.
    assert "from kvm_pilot.mcp.server import main" in body


def test_python_floor_matches_the_package():
    declared = _MANIFEST["compatibility"]["runtimes"]["python"]
    pkg = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert declared.replace(">=", "") == pkg["project"]["requires-python"].replace(">=", "")


# -- the build produces an installable artifact ----------------------------


def test_build_produces_a_bundle_with_everything_the_host_needs(tmp_path):
    out = _build_script().build(tmp_path / "kvm-pilot.mcpb")
    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
        assert {"manifest.json", "pyproject.toml", "server/main.py"} <= names
        manifest = json.loads(zf.read("manifest.json"))
        pyproject = tomllib.loads(zf.read("pyproject.toml").decode())
    assert manifest["version"] == __version__
    assert pyproject["project"]["dependencies"] == [f"kvm-pilot=={__version__}"]


def test_build_stamps_the_version_rather_than_trusting_the_checked_in_files(tmp_path, monkeypatch):
    """The release stamps the version at build time, so a forgotten manual bump
    cannot ship a bundle that lies about what it installs."""
    mod = _build_script()
    monkeypatch.setattr(mod, "package_version", lambda: "9.9.9")
    manifest_text, pyproject_text = mod.stamp("9.9.9")
    assert json.loads(manifest_text)["version"] == "9.9.9"
    assert 'kvm-pilot=="9.9.9"'.replace('"', "") in pyproject_text.replace('"', "")


def test_bundle_carries_no_vendored_wheels(tmp_path):
    """kvm-pilot depends on compiled packages whose wheels are per-platform;
    vendoring them makes the bundle large and wrong on some machines. uv resolves
    them on the user's own machine instead."""
    out = _build_script().build(tmp_path / "kvm-pilot.mcpb")
    with zipfile.ZipFile(out) as zf:
        assert not [n for n in zf.namelist() if n.endswith((".whl", ".so", ".pyd", ".dylib"))]
        assert out.stat().st_size < 100_000  # a manifest and a shim, nothing more


def test_the_build_is_reproducible(tmp_path):
    """Two builds of one commit must be byte-identical, or the artifact cannot be
    compared across machines or re-verified after the fact. `writestr` stamps
    "now" and `write` copies source mtime, so both need a fixed ZipInfo."""
    import hashlib

    mod = _build_script()
    a = mod.build(tmp_path / "a.mcpb")
    b = mod.build(tmp_path / "b.mcpb")
    digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()  # noqa: E731
    assert digest(a) == digest(b)


def test_the_bundle_is_attached_only_after_the_release_gate_passes():
    """A bundle on the Release must have cleared the same checks as the wheel.
    Attaching it from the build job would publish it before test/smoke-install
    had spoken."""
    import yaml

    wf = yaml.safe_load(
        (_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    )
    jobs = wf["jobs"]
    attach = [
        name for name, job in jobs.items()
        if any("gh release upload" in str(step.get("run", "")) for step in job.get("steps", []))
    ]
    assert attach, "no job attaches the bundle to the Release"
    for name in attach:
        needs = set(jobs[name].get("needs") or [])
        assert {"build", "test", "smoke-install"} <= needs, (
            f"{name} attaches the bundle without gating on build+test+smoke-install"
        )
    # And write access stays scoped to the attaching job.
    assert "contents" not in (jobs["build"].get("permissions") or {}), \
        "the build job must not hold contents:write"
