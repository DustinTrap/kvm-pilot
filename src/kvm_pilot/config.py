"""
Configuration resolution for the CLI and library convenience.

Precedence (highest first):
  1. Explicit keyword arguments / CLI flags.
  2. Environment variables (KVM_PILOT_*).
  3. A config file (TOML) with named host profiles. Default location is
     platform-specific: ~/.config/kvm-pilot/config.toml (or $XDG_CONFIG_HOME)
     on Unix, and under %APPDATA% on Windows.

The config file is optional; everything works from flags + env alone. Secrets
may live in the file, env, or a password manager you template into env — the
library never writes secrets back out.
"""

from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

logger = logging.getLogger("kvm_pilot.config")

def _config_base_dir(name: str = os.name) -> str:
    """The platform config-dir base as a string (kept out of ``Path`` so it's
    testable on any OS without instantiating the other flavour of ``Path``).

    ``%APPDATA%`` on Windows, ``$XDG_CONFIG_HOME`` then ``~/.config`` elsewhere.
    """
    home = os.path.expanduser("~")
    if name == "nt":
        return os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
    return os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")


def _default_config_path() -> Path:
    """Per-platform default config path (stdlib-only, no platformdirs dep).

    ``KVM_PILOT_CONFIG`` overrides everything; otherwise it follows the platform's
    conventions so the package's "OS Independent" claim holds on Windows too.
    """
    override = os.environ.get("KVM_PILOT_CONFIG")
    if override:
        return Path(override)
    return Path(_config_base_dir()) / "kvm-pilot" / "config.toml"


DEFAULT_CONFIG_PATH = _default_config_path()


@dataclass
class HostConfig:
    host: str
    user: str = "admin"
    passwd: str = "admin"
    port: int = 443
    scheme: str = "https"
    verify_ssl: bool = False
    # Pin TLS verification to a CA bundle / the device's own self-signed cert
    # (PEM path). Overrides verify_ssl when set.
    ssl_ca_file: str | None = None
    timeout: float = 30.0
    totp_secret: str | None = None
    driver: str = "pikvm"
    # Redfish-only: HTTP auth mode ("session" — the BMC default — or "basic", for
    # endpoints without a SessionService, e.g. emulators or BMCs with session
    # auth disabled). Ignored by the PiKVM family.
    redfish_auth: str = "session"
    # In-band SSH to the managed HOST's OS (the machine behind the KVM), NOT the
    # In-band channel to the managed host behind the KVM (a *separate* machine
    # from the appliance). Used by the SSH channel (src/kvm_pilot/ssh.py) for
    # reachability probes and recovery commands on the target OS. Auth is
    # key-based: ssh_key is a private-key path, or omit it to use the agent's SSH
    # config / default keys. No password auth (avoids an sshpass dependency).
    ssh_host: str | None = None
    ssh_user: str | None = None
    ssh_port: int = 22
    ssh_key: str | None = None

    # Appliance-SSH channel — SSH to the KVM appliance's OWN OS (root@<kvm-ip>),
    # distinct from ssh_host above. The host is INFERRED from `host` (the
    # appliance IS the REST box). Opt-in; key-based only (no password/sshpass).
    # Powers wedge diagnostics + gated appliance-reboot recovery — the only path
    # to observe/recover the RV1126 encoder wedge that REST can't see (#162).
    appliance_ssh: bool = False
    appliance_ssh_user: str = "root"
    appliance_ssh_port: int = 22
    appliance_ssh_key: str | None = None


def _load_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        data = tomllib.load(fh)
    _warn_if_secrets_world_readable(path, data)
    return data


def _warn_if_secrets_world_readable(path: Path, data: dict[str, Any]) -> None:
    """Warn if a config holding a password/TOTP secret is group/other-readable.

    Matches the bar set by ssh/pgpass (0600). POSIX-only — Windows ACLs don't map
    to the Unix mode bits, so the check is skipped there.
    """
    if os.name != "posix":
        return
    hosts = data.get("hosts", {}) if isinstance(data, dict) else {}
    has_secret = any(
        isinstance(h, dict) and (h.get("passwd") or h.get("totp_secret"))
        for h in hosts.values()
    )
    if not has_secret:
        return
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        return
    if mode & 0o077:
        logger.warning(
            "Config %s holds a password/TOTP secret but is readable by group/other "
            "(mode %o). Restrict it: chmod 600 %s",
            path, mode, path,
        )


def resolve_host(
    profile: str | None = None,
    *,
    host: str | None = None,
    user: str | None = None,
    passwd: str | None = None,
    port: int | None = None,
    scheme: str | None = None,
    timeout: float | None = None,
    totp_secret: str | None = None,
    verify_ssl: bool | None = None,
    ssl_ca_file: str | None = None,
    driver: str | None = None,
    redfish_auth: str | None = None,
    ssh_host: str | None = None,
    ssh_user: str | None = None,
    ssh_port: int | None = None,
    ssh_key: str | None = None,
    appliance_ssh: bool | None = None,
    appliance_ssh_user: str | None = None,
    appliance_ssh_port: int | None = None,
    appliance_ssh_key: str | None = None,
    config_path: Path | None = None,
) -> HostConfig:
    """Resolve a HostConfig from args > env > file (in that priority)."""
    # KVM_PILOT_PROFILE works everywhere a profile does (CLI, library, MCP), not
    # just in the MCP server.
    profile = profile or os.environ.get("KVM_PILOT_PROFILE") or None
    data = _load_file(config_path or DEFAULT_CONFIG_PATH)
    profiles = data.get("hosts", {}) if isinstance(data, dict) else {}
    base: dict[str, Any] = {}
    if profile and profile in profiles:
        base = dict(profiles[profile])
        # A typo'd key ("password" for "passwd", "username" for "user") would
        # otherwise be dropped silently and the client would proceed with the
        # admin/admin defaults — repeated default-credential logins can lock a
        # BMC account. Warn loudly instead.
        known = {f.name for f in fields(HostConfig)}
        unknown = sorted(set(base) - known)
        if unknown:
            logger.warning(
                "Profile %r has unrecognized key(s) %s — they are IGNORED. "
                "Known keys: %s",
                profile,
                ", ".join(repr(k) for k in unknown),
                ", ".join(sorted(known)),
            )
    elif profile:
        raise KeyError(f"Host profile {profile!r} not found in config file.")

    def pick(key: str, arg, env: str, default=None):
        if arg is not None:
            return arg
        if os.environ.get(env) is not None:
            return os.environ[env]
        if key in base:
            return base[key]
        return default

    resolved_driver = pick("driver", driver, "KVM_PILOT_DRIVER", "pikvm")
    resolved_host = pick("host", host, "KVM_PILOT_HOST")
    if not resolved_host:
        if resolved_driver == "fake":
            resolved_host = "fake"  # the in-process fake driver needs no real host
        else:
            raise ValueError(
                "No host specified. Provide --host, set KVM_PILOT_HOST, or name a "
                "profile defined in the config file."
            )

    resolved_scheme = pick("scheme", scheme, "KVM_PILOT_SCHEME", "https")
    # The default port follows the scheme: --scheme http against the TLS port
    # 443 can only fail, confusingly. An explicit port always wins.
    port_val = pick("port", port, "KVM_PILOT_PORT", 80 if resolved_scheme == "http" else 443)
    verify_val = pick("verify_ssl", verify_ssl, "KVM_PILOT_VERIFY_SSL", False)
    if isinstance(verify_val, str):
        verify_val = verify_val.lower() in ("1", "true", "yes")
    appliance_val = pick("appliance_ssh", appliance_ssh, "KVM_PILOT_APPLIANCE_SSH", False)
    if isinstance(appliance_val, str):
        appliance_val = appliance_val.lower() in ("1", "true", "yes")

    return HostConfig(
        host=resolved_host,
        user=pick("user", user, "KVM_PILOT_USER", "admin"),
        passwd=pick("passwd", passwd, "KVM_PILOT_PASSWD", "admin"),
        port=int(port_val),
        scheme=resolved_scheme,
        verify_ssl=bool(verify_val),
        ssl_ca_file=pick("ssl_ca_file", ssl_ca_file, "KVM_PILOT_SSL_CA_FILE"),
        timeout=float(pick("timeout", timeout, "KVM_PILOT_TIMEOUT", 30.0)),
        totp_secret=pick("totp_secret", totp_secret, "KVM_PILOT_TOTP_SECRET"),
        driver=resolved_driver,
        redfish_auth=pick("redfish_auth", redfish_auth, "KVM_PILOT_REDFISH_AUTH", "session"),
        ssh_host=pick("ssh_host", ssh_host, "KVM_PILOT_SSH_HOST"),
        ssh_user=pick("ssh_user", ssh_user, "KVM_PILOT_SSH_USER"),
        ssh_port=int(pick("ssh_port", ssh_port, "KVM_PILOT_SSH_PORT", 22)),
        ssh_key=pick("ssh_key", ssh_key, "KVM_PILOT_SSH_KEY"),
        appliance_ssh=bool(appliance_val),
        appliance_ssh_user=pick(
            "appliance_ssh_user", appliance_ssh_user, "KVM_PILOT_APPLIANCE_SSH_USER", "root"),
        appliance_ssh_port=int(
            pick("appliance_ssh_port", appliance_ssh_port, "KVM_PILOT_APPLIANCE_SSH_PORT", 22)),
        appliance_ssh_key=pick(
            "appliance_ssh_key", appliance_ssh_key, "KVM_PILOT_APPLIANCE_SSH_KEY"),
    )


__all__ = ["HostConfig", "resolve_host", "DEFAULT_CONFIG_PATH"]
