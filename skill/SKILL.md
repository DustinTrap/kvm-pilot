---
name: kvm-pilot
description: >-
  AI-driven bare-metal control of PiKVM and GL.iNet GLKVM devices (GL-RM1 /
  GL-RM1PE). Use whenever the user wants to remotely operate a headless server
  or workstation through a KVM — power on/off/cycle, mount an install ISO,
  enter BIOS/UEFI, type at a console, or watch the screen to detect boot phase
  (POST, GRUB, installer, login, crash). Backed by the `kvm-pilot` Python
  package; vision runs on Claude or a local OpenAI-compatible VLM. **No single
  interface is best for everything — pick per action: the bundled MCP server
  (`mcp_server/`) for the visual loop (snapshot/classify) and gated power, the
  CLI for logs/capabilities/firmware/events/HID/media, the Python library for
  mouse and MSD switching, and SSH for appliance maintenance the tool can't do.
  See the interface matrix in the skill body.** Early alpha, never validated on real hardware
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

## Choosing an interface — best tool per action

kvm-pilot is reachable through several interfaces, and **no single one is best
for everything.** Pick per action, and run more than one at once when the work
is independent (see [Multitasking](#multitasking--use-interfaces-in-parallel)).
This is the operator-side complement to the sensing hierarchy (#13, prefer
structured/text over vision) and the actuation-channel hierarchy (#81, hand off
KVM HID+vision → SSH once the target OS is reachable).

| Action | Best interface | Notes / fallback |
|---|---|---|
| See the screen as a model-visible image | **MCP** `snapshot` | Returns a real image content block — no screenshot-file round-trip. CLI `snapshot` writes a file. |
| Classify boot/run phase | **MCP** `classify_screen` | Needs a vision backend (Anthropic key or local VLM). CLI: `classify` / `watch`. |
| Preflight audit (run first) | **MCP** `healthcheck` or CLI `healthcheck` | The intake gate — see below. |
| Device info / host power state | **MCP** `info` / `power_state`, or CLI | Either works. |
| **Read the device/host event log** | **MCP** `logs` or **CLI `logs`** | The text diagnostic when video/streamer/power looks wrong — it names a fault (e.g. a stuck encoder behind a `snapshot` 503) a screenshot can't. |
| capabilities, firmware-check/update, events, watch, type/key, mount/eject | **CLI only** | The MCP server does not expose these. |
| Mouse move/click, MSD mode switching | **Python library only** | Not in MCP or CLI. |
| Change **host** power (on/off/cycle/reset) | **MCP `power`** (gated) or CLI `power` / `power-cycle` | Destructive — confirm each step. MCP `power` is operator-enabled + per-call approval. |
| Reboot the **KVM appliance** / restart `kvmd` / inspect `/etc/kvmd` | **SSH to the appliance** | No kvm-pilot interface does this — out-of-band only. |
| View the screen when `snapshot` fails | **WebRTC/Janus stream or the vendor web UI** | The only way to see a unit that streams H.264 at its native resolution. |

**Host vs. appliance — keep these straight.** The `power` tool/CLI acts on the
**managed host** (the machine the KVM controls). Rebooting the **KVM appliance
itself** — e.g. to clear a stuck video encoder — is **out-of-band**: SSH in and
`reboot`, or restart `kvmd`. Nothing in kvm-pilot reboots the appliance.

**Reading a failed `snapshot`:**
- **HTTP 503 / "Service Unavailable"** → the video subsystem is down. Pull `logs`
  and look for encoder errors; a stuck encoder often clears with an **appliance
  reboot** (SSH).
- **A tiny/empty frame while `has_video_signal` is True** → the JPEG path can't
  encode the current mode, typically **H.264 at the panel's native resolution**.
  Use the WebRTC stream, or drop the host to 1080p, to see the screen.

### Enabling the MCP server

Look for `mcp__kvm-pilot__*` tools (e.g. `mcp__kvm-pilot__snapshot`). If they're
absent, register the server and tell the user to restart the session so the
tools load:

```bash
# server deps: pip install -r mcp_server/requirements.txt
claude mcp add kvm-pilot -s user \
    -e KVM_PILOT_PROFILE=<profile> -e KVM_PILOT_MCP_DRY_RUN=1 -- \
    /path/to/.venv/bin/python /path/to/mcp_server/server.py
claude mcp list          # expect: kvm-pilot ... ✔ Connected
```

**Scope gotcha:** `-s local` registers the server under the **current
directory's** project scope — launch the agent from a different directory and
the tools silently don't load. Use `-s user` (or a committed repo `.mcp.json`)
so it's available wherever you start. Point it at a config-file **profile**
(`KVM_PILOT_PROFILE`) so the device password lives in
`~/.config/kvm-pilot/config.toml`, not the MCP host config; every tool also
takes a `profile` argument to retarget. Keep `KVM_PILOT_MCP_DRY_RUN=1` for this
untested alpha — destructive calls are logged, not sent. The `power` tool is
**disabled** unless the operator sets `KVM_PILOT_MCP_ALLOW_POWER=1` in the
server's own `env`, and even then MCP hosts should require per-call human
approval (never "always allow"). Full operator guide:
[`mcp_server/README.md`](https://github.com/DustinTrap/kvm-pilot/blob/main/mcp_server/README.md).

## Multitasking — use interfaces in parallel

The interfaces don't contend; run independent work concurrently to cut latency
and cross-check signals:

- **Parallel intake.** Gather `healthcheck` + `info` + `capabilities` + `logs`
  (+ `firmware-check`) at once rather than serially.
- **Cross-signal during long waits.** While a vision `watch` waits for a boot
  phase, tail `logs`/`events` alongside it, so a text signal can confirm or
  contradict the pixel read (the operator-side of #13's sensing hierarchy).
- **Mix channels.** The in-session MCP image path and a CLI `events`/`logs`
  stream can run together — different transports, no conflict.
- **Never parallelize state changes.** Serialize anything destructive (power,
  media, keystrokes) behind a single confirm gate; concurrency is for read-only
  observation only.

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

The CLI is the **primary (often only) interface** for a large part of the
surface — `logs`, `capabilities`, `firmware-check`/`firmware-update`, `events`,
`watch`, `type`/`key`, `mount`/`eject` have no MCP tool (see the interface
matrix above). Use the MCP server for the visual loop (`snapshot`/`classify`)
and gated `power`; use the CLI for everything else and for one-off checks when
no MCP host is in the loop.

`kvm-pilot info | capabilities | healthcheck | firmware-check | snapshot | power |
power-cycle | type | key | mount | eject | classify | watch | events`. Run
`healthcheck` on first contact (see above); it also auto-runs ahead of destructive
subcommands. `firmware-check` reports firmware currency and, where a device knows
its vendor's latest, the update to contribute to the registry.
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
