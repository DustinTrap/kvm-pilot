---
name: kvm-pilot
description: >-
  AI-driven bare-metal control of PiKVM and GL.iNet GLKVM devices (GL-RM1 /
  GL-RM1PE). Use whenever the user wants to remotely operate a headless server
  or workstation through a KVM — power on/off/cycle, mount an install ISO,
  enter BIOS/UEFI, type at a console, or watch the screen to detect boot phase
  (POST, GRUB, installer, login, crash). Backed by the `kvm-pilot` Python
  package; vision runs on Claude or a local OpenAI-compatible VLM. **Prefer the
  bundled MCP server (`mcp_server/`) as the interface — it exposes
  info/snapshot/classify/power_state/power as first-class tools; the CLI and
  Python library are fallbacks.** Early alpha, never validated on real hardware
  — treat every operation as unverified and confirm destructive steps with the
  user.
---

# kvm-pilot skill

> ⚠️ **Untested alpha.** `kvm-pilot` has **never been run against real
> hardware** — it is unit-tested with mocks only. Treat every result as
> unverified, expect bugs, and never point a destructive operation (power,
> reset, media, keystrokes) at a machine the user can't afford to have
> power-cycled unexpectedly. Surface each destructive step to the user before
> executing it.

This skill is a thin wrapper over the installable `kvm-pilot` package. The code
lives in the package, not here — install it and import it rather than copying
client logic into a script.

## Preferred interface: the MCP server (enable this first)

**Before driving a KVM through the CLI or raw library, check whether the
`kvm-pilot` MCP server is enabled, and if not, prompt the user to install and
enable it.** It is the most efficient interface: `snapshot` returns the screen
as a real image content block the model can see directly, and
`info`/`healthcheck`/`power_state`/`classify_screen`/`power` are first-class
tools with `readOnlyHint`/`destructiveHint` annotations the host can gate on —
no shelling out, no screenshot-file round-trips, no ad-hoc `curl`. The CLI and Python
library below are **fallbacks** for when the MCP server isn't available or for
capabilities it doesn't expose yet (mouse moves/clicks, MSD mode switching).

If you find yourself scripting `make_driver(...)` or `curl`-ing `/api/*` to do
something the MCP server already exposes, stop and enable the server instead.

**Is it enabled?** Look for `mcp__kvm-pilot__*` tools (e.g.
`mcp__kvm-pilot__snapshot`). If they're absent, register it and tell the user to
restart the session so the tools load:

```bash
# from a repo checkout (server deps: pip install -r mcp_server/requirements.txt)
claude mcp add kvm-pilot -s local -e KVM_PILOT_PROFILE=<profile> -- \
    /path/to/.venv/bin/python /path/to/mcp_server/server.py
claude mcp list          # expect: kvm-pilot ... ✔ Connected
```

Point it at a config-file **profile** (`KVM_PILOT_PROFILE`) so the device
password lives in `~/.config/kvm-pilot/config.toml`, not the MCP host config.
The `power` tool is **disabled by default** — it only works if the operator
sets `KVM_PILOT_MCP_ALLOW_POWER=1` in the server's `env` — and even then MCP
hosts should require per-call human approval (never "always allow"). Full
operator guide: [`mcp_server/README.md`](https://github.com/DustinTrap/kvm-pilot/blob/main/mcp_server/README.md).

## Setup

```bash
pip install kvm-pilot==0.1.0a1            # core, stdlib-only
pip install "kvm-pilot[totp]==0.1.0a1"    # if the device has 2FA enabled
```

`0.1.0a1` is an untested early alpha and is **yanked** on PyPI (opt-in only),
so pin the exact version — a bare `pip install kvm-pilot` installs nothing.
It also predates the newer drivers and CLI (`make_driver`, the
GLKVM/BliKVM/Redfish/fake drivers, `capabilities`/`events`/`eject`); for those,
install the current tree instead:

```bash
pip install "kvm-pilot[totp,ws] @ git+https://github.com/DustinTrap/kvm-pilot"
```

Credentials resolve from `KVM_PILOT_HOST` / `KVM_PILOT_USER` / `KVM_PILOT_PASSWD`
(or a `--profile` in `~/.config/kvm-pilot/config.toml` — full reference:
[docs/configuration.md](https://github.com/DustinTrap/kvm-pilot/blob/main/docs/configuration.md)).
For Claude vision set `ANTHROPIC_API_KEY`; for a local VLM, point at its `/v1`
URL and model.

**GLKVM devices:** the PiKVM REST API is disabled by default on GL firmware.
The user must enable it in `/etc/kvmd/nginx-kvmd.conf` on the device first, or
every call returns 404. A firmware upgrade can revert it.

## First contact: run the healthcheck (preflight) — do this first

**The moment you connect to a KVM — before you drive it, and before you record
it as a "managed" profile — run the device healthcheck.** This is the intake
gate, not an optional extra: it audits the KVM appliance *itself* (readiness /
recovery, security posture, firmware currency) and is the safety net for the
whole tool (issue #80). A preventable KVM-side fault during a remote
power/boot/install can brick or strand a machine you can't physically reach.

- **How:** MCP — call the `healthcheck` tool. CLI — `kvm-pilot healthcheck
  --profile <name>`. Library — `run_healthcheck(driver)` from `kvm_pilot`.
- **Treat it as a severity-tiered gate.** Surface every `WARNING`/`CRITICAL` to
  the user with its implication; a `CRITICAL` **blocks** — do not proceed to a
  destructive or multi-step flow until the user explicitly decides to continue.
- **The highest-value finding is `recovery-path`** — whether *any* out-of-band
  reset exists (ATX wired / GPIO / Redfish / IPMI) if the guest hangs. On GLKVM
  units the ATX is frequently unwired, leaving only in-guest levers; the operator
  must learn this *before* committing to a remote install, not mid-outage.
- **Coverage caveat (know this):** destructive CLI subcommands auto-run the gate
  (`--skip-healthcheck` / `KVM_PILOT_SKIP_HEALTHCHECK=1` bypasses it), but
  **read-only intake — `info`/`capabilities`/`snapshot` — does _not_ auto-run it
  yet.** So on first contact you must run `healthcheck` yourself; don't assume a
  clean `info` means the device was vetted.

## Use the library, not raw HTTP

**First contact: rehearse with `dry_run=True`.** Dry-run short-circuits before
anything else — destructive calls are logged and skipped (the confirm callback
is never invoked), so the whole flow can be validated without changing the
machine's state:

```python
from kvm_pilot import KVMClient

kvm = KVMClient("192.168.8.1", "admin", "secret", dry_run=True)
kvm.mount_iso("https://example.com/distro.iso")   # logged, not sent
kvm.hard_cycle()                                  # logged, not sent
```

**Real run: gate every destructive step on explicit approval.**
`interactive_confirm` prompts on stdin and *fails closed* (denies) when there
is no TTY. In an agent context, ask the user in chat before each destructive
step and wire their answer into the callback:

```python
from kvm_pilot import KVMClient
from kvm_pilot.safety import interactive_confirm
from kvm_pilot.vision import ScreenAnalyzer, make_backend

kvm = KVMClient("192.168.8.1", "admin", "secret", confirm=interactive_confirm)
analyzer = ScreenAnalyzer(kvm, make_backend("anthropic"))   # or "local"

kvm.mount_iso("https://example.com/distro.iso")   # gate: asks before mounting
kvm.hard_cycle()                                  # gate: asks before power off/on
analyzer.wait_for_state("grub_menu", timeout=120)
kvm.press_key("Enter")                            # keystroke injection is gated too
analyzer.wait_for_state("installer_complete", timeout=1800)
```

**Never pass an allow-all confirm callback** (e.g. `lambda op, d: True`) unless
the user has explicitly approved unattended destructive operation in this
session. And note that **omitting `confirm` is also unattended** — the library
default allows everything so plain scripts work — so actively pass
`interactive_confirm` (or a callback that relays the question to the user);
the ask-first duty sits with you, not the library.

## Safety

Destructive operations — power off/reset, virtual-media connect/disconnect and
image uploads, keyboard/mouse injection (`type_text`, `press_key`, shortcuts,
clicks), GPIO, Redfish resets — are gated by `SafetyPolicy`
(`kvm_pilot.safety.DESTRUCTIVE_OPS` is the explicit, auditable set):

- `dry_run=True` short-circuits **first**: the call is logged and skipped and
  the confirm callback is never invoked, so dry runs never prompt or block.
- The `confirm` callback runs only for calls that would really be sent;
  returning `False` blocks the call with `SafetyError`.

When acting on a user's real hardware — which, again, this package has never
been validated against — confirm each destructive step with the user first
unless they have explicitly said otherwise.

## CLI

The CLI is a **fallback** — prefer the MCP server (see above) when it's
enabled. Reach for the CLI for one-off checks, for capabilities the MCP server
doesn't expose, or when no MCP host is in the loop.

`kvm-pilot info | capabilities | healthcheck | snapshot | power | power-cycle |
type | key | mount | eject | classify | watch | events`. Run `healthcheck` on
first contact (see above); it also auto-runs ahead of destructive subcommands.
`--dry-run` logs destructive
actions without sending them (it short-circuits before any prompt, so it is
safe in automation); `--yes` skips the interactive y/N confirmation on a real
run. See `kvm-pilot --help`.

## Worked examples

In the repository (the `examples/` directory is **not shipped inside the pip
package**):

- [`examples/unattended_install.py`](https://github.com/DustinTrap/kvm-pilot/blob/main/examples/unattended_install.py) — mount an ISO and drive an OS install by watching the screen.
- [`examples/bios_audit.py`](https://github.com/DustinTrap/kvm-pilot/blob/main/examples/bios_audit.py) — hard-cycle into firmware setup and OCR what's on screen.
- [`examples/power_cycle_verify.py`](https://github.com/DustinTrap/kvm-pilot/blob/main/examples/power_cycle_verify.py) — hard power-cycle and verify the host comes back.

All three default to the safe path (dry run and/or interactive confirmation);
copy that pattern, not an allow-all one.
