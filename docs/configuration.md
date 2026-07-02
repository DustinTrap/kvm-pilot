# Configuration reference

Everything the CLI, library, and MCP server read to find and talk to a device:
the config file, every `KVM_PILOT_*` environment variable, and the precedence
between them.

## Precedence

Each field resolves independently, highest priority first:

1. **Explicit CLI flags / keyword arguments** (`--host`, `resolve_host(host=…)`).
2. **`KVM_PILOT_*` environment variables.**
3. **The profile** selected with `--profile NAME` (a `[hosts.NAME]` table in the
   config file).
4. **Built-in defaults** (see the table below).

So an env var overrides the same key in a profile, and a flag overrides both.
Resolution lives in [`src/kvm_pilot/config.py`](https://github.com/DustinTrap/kvm-pilot/blob/main/src/kvm_pilot/config.py)
(`resolve_host()`).

## The config file

Default location: `~/.config/kvm-pilot/config.toml` (honoring `$XDG_CONFIG_HOME`) on
Unix, `%APPDATA%\kvm-pilot\config.toml` on Windows. Override the path with the
`KVM_PILOT_CONFIG` environment variable (read once, at import time). The file
is optional — everything works from flags + env alone.

The format is TOML with one `[hosts.<name>]` table per named profile (there is
no `[defaults]` section; unknown keys and tables are ignored). A starter file
ships as [`config.example.toml`](https://github.com/DustinTrap/kvm-pilot/blob/main/config.example.toml).

```toml
[hosts.homelab]
host = "192.168.8.1"
user = "admin"
passwd = "changeme"        # prefer env injection (KVM_PILOT_PASSWD) over storing here
port = 443
scheme = "https"
verify_ssl = false          # GL/PiKVM ship self-signed certs
timeout = 30.0
driver = "glkvm"            # pikvm (default) | glkvm | blikvm | fake | redfish
# totp_secret = "BASE32SECRET"   # only if 2FA is enabled (needs the 'totp' extra)
# redfish_auth = "session"       # redfish driver only: "basic" for endpoints
#                                # without a SessionService (e.g. emulators)

[hosts.rack-bmc]
host = "idrac.lan"
driver = "redfish"
```

Every key `resolve_host()` reads, with its default:

| Profile key | Env var | Default | Notes |
|---|---|---|---|
| `host` | `KVM_PILOT_HOST` | — (required) | Optional only for `driver = "fake"`. |
| `user` | `KVM_PILOT_USER` | `admin` | |
| `passwd` | `KVM_PILOT_PASSWD` | `admin` | |
| `port` | `KVM_PILOT_PORT` | `443` | |
| `scheme` | `KVM_PILOT_SCHEME` | `https` | `http` or `https`. |
| `verify_ssl` | `KVM_PILOT_VERIFY_SSL` | `false` | Env value is truthy only for `1`/`true`/`yes` (case-insensitive); anything else is `false`. Unverified TLS logs a one-time warning. |
| `ssl_ca_file` | `KVM_PILOT_SSL_CA_FILE` | unset | PEM path: pin TLS verification to a CA bundle or the device's own self-signed cert. Overrides `verify_ssl`; the cert's SAN must cover the host/IP you connect to. |
| `timeout` | `KVM_PILOT_TIMEOUT` | `30.0` | HTTP per-request timeout (seconds); the CLI's global `--timeout` maps here. |
| `totp_secret` | `KVM_PILOT_TOTP_SECRET` | unset | Base32 secret for 2FA; needs the `totp` extra. |
| `driver` | `KVM_PILOT_DRIVER` | `pikvm` | `pikvm` \| `glkvm` \| `blikvm` \| `fake` \| `redfish`; the CLI `--driver` flag overrides. |
| `redfish_auth` | `KVM_PILOT_REDFISH_AUTH` | `session` | Redfish driver only: `session` or `basic` (for BMCs/emulators without a SessionService). Ignored by the PiKVM family. |

Naming a `--profile` that doesn't exist in the file is an error (`KeyError`),
not a silent fallback.

## Environment variables

All of the per-field vars in the table above, plus:

| Variable | Honored by | Purpose |
|---|---|---|
| `KVM_PILOT_SSL_CA_FILE` | CLI, library, MCP server | Pin TLS verification to a CA bundle or the device's own self-signed cert (PEM path). Overrides `verify_ssl`. The cert must include the host/IP you connect to in its SAN. |
| `KVM_PILOT_CONFIG` | CLI, library, MCP server | Path of the config file (default `~/.config/kvm-pilot/config.toml`). Read once at import time. |
| `KVM_PILOT_PROFILE` | CLI, library, MCP server | Names the `[hosts.NAME]` profile to use when none is given explicitly. An explicit `--profile` / `resolve_host("NAME")` argument wins. |
| `KVM_PILOT_VISION_MODEL` | Anthropic vision backend | Pin a vision model id; unset = auto-resolve the newest vision-capable model at runtime. |
| `ANTHROPIC_API_KEY` | Anthropic vision backend | Required for `classify`/`watch` with the default backend (validated lazily, at first network use). |
| `OPENAI_API_KEY` | local/OpenAI-compatible vision backend | Optional; most local servers ignore it (defaults to `not-needed`). |
| `KVM_PILOT_MCP_ALLOW_POWER` | MCP server only | Gates the destructive `power` tool — see [`mcp_server/README.md`](https://github.com/DustinTrap/kvm-pilot/blob/main/mcp_server/README.md). |
| `KVM_PILOT_MCP_DRY_RUN` | MCP server only | Forces dry-run: destructive tool calls are logged, not sent — see [`mcp_server/README.md`](https://github.com/DustinTrap/kvm-pilot/blob/main/mcp_server/README.md). |
| `KVM_PILOT_REDFISH_URL` | test suite only | Points the opt-in Redfish integration tests (`pytest tests/integration -m integration`) at an external emulator; not read by the library or CLI. |

A ready-to-copy env template ships as
[`.env.example`](https://github.com/DustinTrap/kvm-pilot/blob/main/.env.example).

## Keeping secrets safe

- If the config file holds a password or TOTP secret, restrict it:
  `chmod 600 ~/.config/kvm-pilot/config.toml` — and never commit a copy with
  real secrets.
- Passing `--passwd` on the command line exposes the secret to `ps` and your
  shell history; prefer `KVM_PILOT_PASSWD` (or the config file) for real
  credentials.
- The library never writes secrets back out, and passwords/session tokens are
  redacted from error text — but double-check logs before posting them.

See the [security policy](SECURITY.md) for the broader operational guidance
(don't expose a KVM to the internet, the safety layer is advisory, etc.).
