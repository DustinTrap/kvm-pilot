"""Tests for host/credential resolution precedence (args > env > file)."""

import pytest

from kvm_pilot.config import resolve_host


def test_requires_a_host(monkeypatch):
    monkeypatch.delenv("KVM_PILOT_HOST", raising=False)
    with pytest.raises(ValueError):
        resolve_host(config_path=None, host=None)


def test_scheme_defaults_to_https(monkeypatch, tmp_path):
    monkeypatch.delenv("KVM_PILOT_SCHEME", raising=False)
    cfg = resolve_host(host="h", config_path=tmp_path / "none.toml")
    assert cfg.scheme == "https"


def test_scheme_arg_beats_env(monkeypatch, tmp_path):
    # scheme now flows through the same args > env > file precedence as the
    # other fields, instead of only reading the config file.
    monkeypatch.setenv("KVM_PILOT_SCHEME", "http")
    cfg_env = resolve_host(host="h", config_path=tmp_path / "none.toml")
    assert cfg_env.scheme == "http"  # env honored

    cfg_arg = resolve_host(host="h", scheme="https", config_path=tmp_path / "none.toml")
    assert cfg_arg.scheme == "https"  # explicit arg wins over env


def test_timeout_precedence(monkeypatch, tmp_path):
    monkeypatch.delenv("KVM_PILOT_TIMEOUT", raising=False)
    none = tmp_path / "none.toml"
    assert resolve_host(host="h", config_path=none).timeout == 30.0  # default
    monkeypatch.setenv("KVM_PILOT_TIMEOUT", "12.5")
    assert resolve_host(host="h", config_path=none).timeout == 12.5  # env
    assert resolve_host(host="h", timeout=99.0, config_path=none).timeout == 99.0  # arg wins


def test_profile_from_file(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text(
        '[hosts.lab]\nhost = "10.0.0.5"\nscheme = "http"\nport = 8080\n'
    )
    cfg = resolve_host("lab", config_path=f)
    assert cfg.host == "10.0.0.5"
    assert cfg.scheme == "http"
    assert cfg.port == 8080
