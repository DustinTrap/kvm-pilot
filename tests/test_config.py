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


def test_driver_precedence(monkeypatch, tmp_path):
    monkeypatch.delenv("KVM_PILOT_DRIVER", raising=False)
    none = tmp_path / "none.toml"
    assert resolve_host(host="h", config_path=none).driver == "pikvm"  # default
    monkeypatch.setenv("KVM_PILOT_DRIVER", "glkvm")
    assert resolve_host(host="h", config_path=none).driver == "glkvm"  # env
    assert resolve_host(host="h", driver="blikvm", config_path=none).driver == "blikvm"  # arg wins


def test_driver_from_profile(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text('[hosts.gl]\nhost = "10.0.0.9"\ndriver = "glkvm"\n')
    assert resolve_host("gl", config_path=f).driver == "glkvm"


def test_redfish_auth_precedence(monkeypatch, tmp_path):
    monkeypatch.delenv("KVM_PILOT_REDFISH_AUTH", raising=False)
    none = tmp_path / "none.toml"
    assert resolve_host(host="h", config_path=none).redfish_auth == "session"  # BMC default
    monkeypatch.setenv("KVM_PILOT_REDFISH_AUTH", "basic")
    assert resolve_host(host="h", config_path=none).redfish_auth == "basic"  # env
    assert resolve_host(  # arg wins
        host="h", redfish_auth="session", config_path=none
    ).redfish_auth == "session"


def test_redfish_auth_threads_into_driver():
    # from_config must hand the resolved auth mode to the RedfishHTTP transport,
    # so `--redfish-auth basic` actually selects HTTP Basic.
    from kvm_pilot.config import HostConfig
    from kvm_pilot.drivers.redfish import RedfishDriver

    d = RedfishDriver.from_config(HostConfig(host="h", driver="redfish", redfish_auth="basic"))
    assert d._http._auth == "basic"


def test_profile_from_file(tmp_path):
    f = tmp_path / "config.toml"
    f.write_text(
        '[hosts.lab]\nhost = "10.0.0.5"\nscheme = "http"\nport = 8080\n'
    )
    cfg = resolve_host("lab", config_path=f)
    assert cfg.host == "10.0.0.5"
    assert cfg.scheme == "http"
    assert cfg.port == 8080


def test_profile_env_var_selects_profile(tmp_path, monkeypatch):
    # KVM_PILOT_PROFILE works for the CLI/library, not just the MCP server.
    cfg = tmp_path / "config.toml"
    cfg.write_text('[hosts.lab]\nhost = "10.0.0.9"\nuser = "u"\n')
    monkeypatch.setenv("KVM_PILOT_PROFILE", "lab")
    resolved = resolve_host(config_path=cfg)
    assert resolved.host == "10.0.0.9"


def test_unknown_profile_keys_warn_loudly(tmp_path, caplog):
    # A typo'd key ("password") silently dropping to the admin/admin defaults can
    # lock a real BMC account — it must at least warn.
    cfg = tmp_path / "config.toml"
    cfg.write_text('[hosts.lab]\nhost = "10.0.0.9"\npassword = "oops"\n')
    import logging

    with caplog.at_level(logging.WARNING, logger="kvm_pilot.config"):
        resolved = resolve_host("lab", config_path=cfg)
    assert resolved.passwd == "admin"  # the typo'd key was not applied
    assert any("password" in r.message and "IGNORED" in r.message for r in caplog.records)


def test_scheme_http_defaults_port_80(monkeypatch):
    monkeypatch.delenv("KVM_PILOT_PORT", raising=False)
    resolved = resolve_host(host="box", scheme="http")
    assert resolved.port == 80
    resolved = resolve_host(host="box", scheme="http", port=8443)
    assert resolved.port == 8443  # explicit port always wins
    resolved = resolve_host(host="box")
    assert resolved.port == 443  # https default unchanged


def test_ssl_ca_file_resolves_through_precedence(tmp_path, monkeypatch):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[hosts.lab]\nhost = "10.0.0.9"\nssl_ca_file = "/from/file.pem"\n')
    assert resolve_host("lab", config_path=cfg).ssl_ca_file == "/from/file.pem"
    monkeypatch.setenv("KVM_PILOT_SSL_CA_FILE", "/from/env.pem")
    assert resolve_host("lab", config_path=cfg).ssl_ca_file == "/from/env.pem"
    assert (
        resolve_host("lab", config_path=cfg, ssl_ca_file="/from/arg.pem").ssl_ca_file
        == "/from/arg.pem"
    )


def test_world_readable_config_with_secret_warns(tmp_path, caplog):
    import logging
    import os

    if os.name != "posix":
        import pytest
        pytest.skip("permission bits are POSIX-only")
    cfg = tmp_path / "config.toml"
    cfg.write_text('[hosts.lab]\nhost = "h"\npasswd = "s3cr3t"\n')
    cfg.chmod(0o644)  # group/other-readable
    with caplog.at_level(logging.WARNING, logger="kvm_pilot.config"):
        resolve_host("lab", config_path=cfg)
    assert any("chmod 600" in r.message for r in caplog.records)


def test_mode_600_config_with_secret_does_not_warn(tmp_path, caplog):
    import logging
    import os

    if os.name != "posix":
        import pytest
        pytest.skip("permission bits are POSIX-only")
    cfg = tmp_path / "config.toml"
    cfg.write_text('[hosts.lab]\nhost = "h"\npasswd = "s3cr3t"\n')
    cfg.chmod(0o600)
    with caplog.at_level(logging.WARNING, logger="kvm_pilot.config"):
        resolve_host("lab", config_path=cfg)
    assert not any("chmod 600" in r.message for r in caplog.records)


def test_world_readable_config_without_secret_does_not_warn(tmp_path, caplog):
    import logging
    import os

    if os.name != "posix":
        import pytest
        pytest.skip("permission bits are POSIX-only")
    cfg = tmp_path / "config.toml"
    cfg.write_text('[hosts.lab]\nhost = "h"\nuser = "admin"\n')  # no secret
    cfg.chmod(0o644)
    with caplog.at_level(logging.WARNING, logger="kvm_pilot.config"):
        resolve_host("lab", config_path=cfg)
    assert not any("chmod 600" in r.message for r in caplog.records)


# -- platform config-dir (#65). The base-dir logic is tested in string-space so
# monkeypatching os.name does not force Path() to build the other OS's flavour.

def test_config_base_dir_windows_uses_appdata(monkeypatch):
    import kvm_pilot.config as cfg
    monkeypatch.setenv("APPDATA", "/fake/AppData/Roaming")
    assert cfg._config_base_dir("nt") == "/fake/AppData/Roaming"


def test_config_base_dir_unix_honors_xdg(monkeypatch):
    import kvm_pilot.config as cfg
    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/xdg")
    assert cfg._config_base_dir("posix") == "/tmp/xdg"


def test_config_base_dir_unix_default_is_dot_config(monkeypatch):
    import os

    import kvm_pilot.config as cfg
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert cfg._config_base_dir("posix") == os.path.join(os.path.expanduser("~"), ".config")


def test_default_config_path_override_wins(monkeypatch, tmp_path):
    import kvm_pilot.config as cfg
    monkeypatch.setenv("KVM_PILOT_CONFIG", str(tmp_path / "custom.toml"))
    assert cfg._default_config_path() == tmp_path / "custom.toml"


def test_default_config_path_ends_with_kvm_pilot_config(monkeypatch):
    import kvm_pilot.config as cfg
    monkeypatch.delenv("KVM_PILOT_CONFIG", raising=False)
    p = cfg._default_config_path()
    assert p.name == "config.toml" and p.parent.name == "kvm-pilot"
