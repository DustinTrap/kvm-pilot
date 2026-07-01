"""
Configuration resolution for the CLI and library convenience.

Precedence (highest first):
  1. Explicit keyword arguments / CLI flags.
  2. Environment variables (KVM_PILOT_*).
  3. A config file (TOML), default ~/.config/kvm-pilot/config.toml, with named
     host profiles.

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

DEFAULT_CONFIG_PATH = Path(
    os.environ.get("KVM_PILOT_CONFIG", Path.home() / ".config" / "kvm-pilot" / "config.toml")
)


@dataclass
class HostConfig:
    host: str
    user: str = "admin"
    passwd: str = "admin"
    port: int = 443
    scheme: str = "https"
    verify_ssl: bool = False
    timeout: float = 30.0
    totp_secret: str | None = None
    driver: str = "pikvm"
    # Redfish-only: HTTP auth mode ("session" — the BMC default — or "basic", for
    # endpoints without a SessionService, e.g. emulators or BMCs with session
    # auth disabled). Ignored by the PiKVM family.
    redfish_auth: str = "session"


def _load_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as fh:
        return tomllib.load(fh)


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
    driver: str | None = None,
    redfish_auth: str | None = None,
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

    return HostConfig(
        host=resolved_host,
        user=pick("user", user, "KVM_PILOT_USER", "admin"),
        passwd=pick("passwd", passwd, "KVM_PILOT_PASSWD", "admin"),
        port=int(port_val),
        scheme=resolved_scheme,
        verify_ssl=bool(verify_val),
        timeout=float(pick("timeout", timeout, "KVM_PILOT_TIMEOUT", 30.0)),
        totp_secret=pick("totp_secret", totp_secret, "KVM_PILOT_TOTP_SECRET"),
        driver=resolved_driver,
        redfish_auth=pick("redfish_auth", redfish_auth, "KVM_PILOT_REDFISH_AUTH", "session"),
    )


__all__ = ["HostConfig", "resolve_host", "DEFAULT_CONFIG_PATH"]
