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
  (`kvm-pilot-mcp`) for the visual loop (snapshot/classify), gated act tools
  (keyboard/mouse/media), and gated power; the CLI for
  logs/capabilities/firmware/events and scripting; the Python library for MSD
  mode switching; and SSH for appliance maintenance the tool can't do.
  See the interface matrix in the skill body.** Early alpha — most device/capability
  combos are still unverified (only a few exercised live, on a GL-RM1PE); treat
  every operation as unverified and confirm destructive steps with the user.
---

# kvm-pilot skill

> ⚠️ **Alpha — largely unverified.** Most of `kvm-pilot` is unit-tested with
> mocks only; only a handful of device+capability combos have been exercised on
> real hardware (see the
> [Hardware-Compatibility list](https://github.com/DustinTrap/kvm-pilot/wiki/Hardware-Compatibility)
> for what actually has). Treat every result as unverified, expect bugs, and
> never point a destructive operation (power, reset, media, keystrokes) at a
> machine the user can't afford to have power-cycled unexpectedly. Surface each
> destructive step to the user before executing it.

This skill is a thin wrapper over the installable `kvm-pilot` package. The code
lives in the package, not here — install it and import it rather than copying
client logic into a script.

## Choosing an interface — best tool per action

kvm-pilot is reachable through several interfaces, and **no single one is best
for everything.** Pick per action, and run more than one at once when the work
is independent (see [Multitasking](#multitasking--use-interfaces-in-parallel)).
This is the operator-side complement to the sensing hierarchy (#13, prefer
structured/text over vision) and the actuation-channel hierarchy (#81, hand off
KVM HID+vision → SSH once the target OS is reachable — see **Recovery order —
remote before physical**, below).

| Action | Best interface | Notes / fallback |
|---|---|---|
| See the screen as a model-visible image | **MCP** `snapshot` | Returns a real image content block — no screenshot-file round-trip. CLI `snapshot` writes a file. The JSON payload carries `signal` (online/resolution/fps/format) and `unchanged_since_last_snapshot` — a byte-identical frame when the screen should have changed means stale/cached pixels: check `signal` + `logs` before trusting or acting on it. |
| Classify boot/run phase | **MCP** `classify_screen` | Uses the server's vision backend if configured; **with no server key it falls back to caller-side** — hands you the screenshot + prompt to classify yourself (a `[json, image]` result). CLI: `classify` / `watch`. |
| Wait for a boot/run phase | **MCP** `wait_for_state` or CLI `watch` | Bounded server-side wait (≤ 300 s per call — chain calls for long installs; the timeout result carries the last observed state). Success returns the final `frame_ref` for a follow-up `mouse` click. Phases the cheap gates can't resolve need server-side vision — with no server key it errors fast: poll `classify_screen` (caller-side) instead. |
| Preflight audit (run first) | **MCP** `healthcheck` or CLI `healthcheck` | The intake gate — see below. |
| Device info / host power state | **MCP** `info` / `power_state`, or CLI | Either works. |
| List what the driver supports | **MCP** `capabilities` or CLI `capabilities` | Structural/offline — no network, no preflight. Use it to pick the right interface up front. |
| **Read the device/host event log** | **MCP** `logs` or **CLI `logs`** | The text diagnostic when video/streamer/power looks wrong — it names a fault (e.g. a stuck encoder behind a `snapshot` 503) a screenshot can't. |
| Type / press a key / send a shortcut on the host console | **MCP** `type_text` / `press_key` / `send_shortcut` / `ctrl_alt_delete`, or CLI `type` / `key` | HID input, gated by effect: needs `KVM_PILOT_MCP_ALLOW_HID` + per-call approval; a reboot chord (Ctrl+Alt+Del, SysRq) needs `ALLOW_POWER`. |
| Move / click the mouse (installers, BIOS, desktops) | **MCP** `mouse` | Absolute positioning; `percent` coords by default. A click must carry `observed_frame_ref` from a recent `snapshot` (refused if the host rebooted since). Needs `KVM_PILOT_MCP_ALLOW_HID`. |
| See what media is already on the KVM | **MCP** `list_virtual_media` or CLI `media-list` | Read-only MSD inventory. **Check this before asking the user to download or upload an ISO** — the image may already be in storage from an earlier install; mount it instead of round-tripping gigabytes. The reply's `host_visible_as` (when present) is the device name the target's boot menu shows for truly presented media — match it to confirm readiness and to pick the correct boot entry instead of guessing. |
| Mount / eject install media | **MCP** `mount_iso` / `eject`, or CLI `mount` / `eject` | Virtual media (ISO path or URL). MCP needs `KVM_PILOT_MCP_ALLOW_MEDIA` + approval. |
| firmware-check/update, events | **CLI only** | The MCP server does not expose these. |
| MSD mode switching | **Python library only** | Not in MCP or CLI. |
| Change **host** power (on/off/cycle/reset) | **MCP `power`** (gated) or CLI `power` / `power-cycle` | Destructive — confirm each step. MCP `power` is operator-enabled + per-call approval. |
| Reboot the **KVM appliance** / restart `kvmd` / inspect `/etc/kvmd` | **SSH to the appliance** | No kvm-pilot interface does this — out-of-band only. |
| Check if the **target host** is reachable / run commands on it once its OS is up | **MCP `ssh_reachable` / `ssh_exec`**, or CLI `ssh-check` / `ssh-exec` (in-band) | Prefer SSH over KVM keystrokes once the OS is up. Configure the target's IP/host/FQDN via `ssh_host` (≠ the KVM's address); `ssh_exec` is gated (operator opt-in `KVM_PILOT_MCP_ALLOW_SSH`). See "Recovery order" below. |
| Bootstrap SSH during an install (set up the cheap channel over the expensive one) | **CLI `ssh-bootstrap`** | Once an installer is up, switches to a text console, reads the DHCP IP off the screen, starts `sshd`, and hands off to SSH. **Plans by default** — pass `--execute` to run it; add a `--command` that installs a key/password for a usable channel. Guided/conservative (aborts if the console can't be confirmed); not an MCP tool. |
| View the screen when `snapshot` fails | **WebRTC/Janus stream or the vendor web UI** | The only way to see a unit that streams H.264 at its native resolution. |

**Host vs. appliance — keep these straight.** The `power` tool/CLI acts on the
**managed host** (the machine the KVM controls). Rebooting the **KVM appliance
itself** — e.g. to clear a stuck video encoder — is **out-of-band**: SSH into the
*appliance* and `reboot`, or restart `kvmd`. Nothing in kvm-pilot reboots the
appliance. And the appliance's address is **not** the managed host's address —
they are separate machines with separate IPs.

**Recovery order — remote before physical.** When the host is wedged or its screen
is black and you can't power-cycle it through the KVM (`recovery-path` is CRITICAL
— no ATX/GPIO wired), do **not** jump to asking the user to physically intervene.
Prefer remote recovery, in this order, and present the options in this order:
1. **SSH into the target host OS** (in-band) — if its OS is on the network this is
   the fastest, most reliable lever (and far better than typing through KVM HID).
   Probe with `ssh_reachable` / `ssh-check`, then act with `ssh_exec` / `ssh-exec`.
   You must **ask the user for the target's IP / hostname / FQDN** (set it as
   `ssh_host`) — it's a different machine from the KVM, so you cannot infer it from
   the KVM's address.
2. **Wake-on-LAN** — if the host is off but WoL-capable and you have its MAC.
3. Only after remote options are exhausted, suggest **physical intervention**
   (press the power button) or **wiring the ATX cable** for future remote control.

> **Network sweep is opt-in and risky.** If the user doesn't know the target's
> address, you may *offer* to scan a network range for SSH — but say plainly it's
> noisy and only acceptable on networks they own, get them to confirm the range
> first, and never sweep by default.

**Reading a failed `snapshot`:**
- **HTTP 503 / "Service Unavailable"** → the video subsystem is down. Pull `logs`
  and look for encoder errors; a stuck encoder often clears with an **appliance
  reboot** (SSH).
- **A tiny/empty frame while `has_video_signal` is True** → the JPEG path can't
  encode the current mode, typically **H.264 at the panel's native resolution**.
  Use the WebRTC stream, or drop the host to 1080p, to see the screen.
- **A black/blank screen while `power_state`/`powered_on` reads True** → on a
  device whose capability profile marks power readings **not trusted** (no ATX
  board), `powered_on: true` can be an HDMI/EDID artifact, not proof the OS is up —
  `is_powered_on` fails *open*. **Don't trust it.** Disambiguate by what the
  snapshot actually shows **and** an **SSH reachability check to the target host**
  (is its OS answering on the network?), not "verify visually" alone — visual
  checks are exactly what fails on a black screen.

### Enabling the MCP server

**The tools it exposes**, all named `mcp__kvm-pilot__<tool>`:
- Read-only: `info`, `power_state`, `capabilities`, `support_matrix` (what's
  been exercised live per device+firmware, plus its derived maturity — check it
  before trusting a capability that matters), `healthcheck`, `logs`,
  `snapshot` (model-visible JPEG), `classify_screen` (boot/run phase — uses a
  server-side vision backend if configured, else falls back to caller-side
  classification), `ssh_reachable`, `list_virtual_media` (MSD storage
  inventory — check it before requesting an ISO download/upload), and
  `ssh_discover` (CIDR scan — RISKY/opt-in, needs `confirm=true`, user-owned
  networks only)
- **Destructive act tools** — each needs the operator to opt the tool's *effect*
  in via an env flag **and** a per-invocation approval (a human elicitation, or
  `confirm=true` under a standing policy):
  - `power` — on/off/cycle/reset (`KVM_PILOT_MCP_ALLOW_POWER`)
  - `type_text` / `press_key` / `send_shortcut` / `mouse` — HID input
    (`KVM_PILOT_MCP_ALLOW_HID`); a reboot chord in `send_shortcut` needs `ALLOW_POWER`
  - `ctrl_alt_delete` — a reboot, so it needs `ALLOW_POWER` (not the HID gate)
  - `mount_iso` / `eject` — virtual media (`KVM_PILOT_MCP_ALLOW_MEDIA`)
  - `ssh_exec` — run a command over SSH (`KVM_PILOT_MCP_ALLOW_SSH`)

Every tool takes an optional `profile` argument to pick a device from
`~/.config/kvm-pilot/config.toml`; omit it to use the server's default profile.

**To use it:** look for `mcp__kvm-pilot__*` tools (e.g. `mcp__kvm-pilot__snapshot`).
If they're absent, `pip install --pre kvm-pilot` (which provides `kvm-pilot-mcp`),
then register the server and tell the user to restart the session so the tools
load:

```bash
# pip install --pre kvm-pilot   installs the CLI, the MCP server, and its deps
claude mcp add kvm-pilot -s user \
    -e KVM_PILOT_PROFILE=<profile> -e KVM_PILOT_MCP_DRY_RUN=1 -- \
    kvm-pilot-mcp
claude mcp list          # expect: kvm-pilot ... ✔ Connected
```

**Scope gotcha:** `-s local` registers the server under the **current
directory's** project scope — launch the agent from a different directory and
the tools silently don't load. Use `-s user` (or a committed repo `.mcp.json`)
so it's available wherever you start. Point it at a config-file **profile**
(`KVM_PILOT_PROFILE`) so the device password lives in
`~/.config/kvm-pilot/config.toml`, not the MCP host config; every tool also
takes a `profile` argument to retarget. Keep `KVM_PILOT_MCP_DRY_RUN=1` until the
user trusts a flow on their hardware — destructive calls are logged, not sent.
Every destructive tool is **disabled** until the operator opts its effect class
in via the server's own `env` (`KVM_PILOT_MCP_ALLOW_POWER` / `_ALLOW_HID` /
`_ALLOW_MEDIA` / `_ALLOW_SSH`), and even then each call needs per-invocation
approval (never "always allow"). Full operator guide:
[MCP server README](https://github.com/DustinTrap/kvm-pilot/blob/main/src/kvm_pilot/mcp/README.md).

## Multitasking — use interfaces in parallel

The interfaces don't contend; run independent work concurrently to cut latency
and cross-check signals:

- **Parallel intake.** Gather `healthcheck` + `info` + `capabilities` + `logs`
  (+ `firmware-check`) at once rather than serially.
- **Cross-signal during long waits.** While a vision wait (`wait_for_state` /
  CLI `watch`) waits for a boot
  phase, tail `logs`/`events` alongside it, so a text signal can confirm or
  contradict the pixel read (the operator-side of #13's sensing hierarchy).
- **Mix channels.** The in-session MCP image path and a CLI `events`/`logs`
  stream can run together — different transports, no conflict.
- **Never parallelize state changes.** Serialize anything destructive (power,
  media, keystrokes) behind a single confirm gate; concurrency is for read-only
  observation only.

## Installing Linux? Switch to text mode + SSH first

When the task is a Linux install through the KVM, do **not** click through the
graphical installer (coordinates are unreliable, #128/#129). Before the
installer boots, edit the boot entry over HID — `e` at GRUB / `Tab` at syslinux
(`press_key`/`send_shortcut` + `type_text`) — append the distro's text+SSH args,
boot (`Ctrl+X`/`Enter`), and finish over SSH:

| Family | Append / do |
|---|---|
| Fedora/RHEL/Rocky/Alma | `inst.sshd inst.text` (+`inst.lang=en_US`; `inst.ks=<url>` for fully automatic) |
| Debian / Ubuntu-legacy d-i | `anna/choose_modules=network-console network-console/password=<pw>` — SSH in as `installer` |
| Ubuntu Server (Subiquity) | live sshd already running; `autoinstall ds=nocloud-net;s=<url>` for hands-off |
| openSUSE/SLES | `ssh=1 ssh.password=<pw>`, then run `yast.ssh` in the session |
| Arch / Alpine | none — live-ISO shell: `passwd` + start `sshd` |

After boot: discover the DHCP IP (`ssh-bootstrap` OCRs it off the console; or
DHCP leases), verify `ssh_reachable(host=…)`, then drive via `ssh_exec`. Already
stuck in a GUI installer? `kvm-pilot ssh-bootstrap` retrofits the channel (see
the interface table). Caution: installer sshd is weakly authenticated (Anaconda
`inst.sshd` = passwordless root) — LAN-you-own only, set credentials immediately.
Full matrix + rationale:
[docs/unattended-install.md](https://github.com/DustinTrap/kvm-pilot/blob/main/docs/unattended-install.md).

## Setup

**First-time user? Offer a quick orientation.** If this looks like a first run —
no `~/.config/kvm-pilot/config.toml` (or it has no `[hosts.*]` profile), the user
is asking how to get started, or you're setting up credentials for the first time —
proactively share two or three tips and point them to the
[getting-started guide](https://github.com/DustinTrap/kvm-pilot/blob/main/docs/getting-started.md):
start with a **read-only status report**, keep **`KVM_PILOT_MCP_DRY_RUN=1`** on
until they trust a flow, run **`healthcheck` first**, and **name the machine you
mean** ("the connected server behind the KVM at `<ip>`", not the KVM appliance
itself). Don't repeat this for a user who is clearly already experienced.

```bash
pip install --pre kvm-pilot               # CLI + this skill + the MCP server
pip install --pre "kvm-pilot[totp]"       # add if the device has 2FA enabled
```

It's a pre-release, so `--pre` (or pinning the exact current version) is required — a bare
`pip install kvm-pilot` deliberately picks up no alpha. A single install brings
the `kvm-pilot` CLI, the `kvm-pilot-mcp` server, and this skill file. For the
latest unreleased tree, install from git:

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

When acting on a user's real hardware, remember most device+capability combos
are still unverified (check the `support_matrix` MCP tool or the
Hardware-Compatibility wiki page) — confirm each destructive step with the user
first unless they have explicitly said otherwise.

## Target context — whose locale, keyboard, and timezone? (#79)

The machine behind the KVM is not the machine the operator is sitting at. When
a flow asks for **language/locale, keyboard layout, or timezone** — an OS
installer's first screens, first-boot setup, or any answer file you generate —
**ask the user whether their local context applies to the target before
answering.** The target may be in another region (colo, remote DC, another
country) or destined for a different keyboard layout than the operator's laptop.

- Offer the operator's detected values as the **default-but-confirmable**
  answer, never a silent assumption. Detect them from `$LANG`,
  `localectl` / `timedatectl` (Linux), or `defaults read -g AppleLocale` +
  `readlink /etc/localtime` (macOS).
- One question covers the flow: *"Use this machine's settings (`en_US.UTF-8`,
  `us`, `America/Los_Angeles`) for the target, or configure it differently?"*
  Reuse the answer for every later locale/keyboard/timezone prompt in the same
  install rather than re-asking.
- **Keyboard layout also affects your own typing.** kvm-pilot sends text as HID
  scancodes translated with a US keymap (library default `keymap="en-us"`;
  the MCP and CLI act tools don't expose a keymap option), and the target
  decodes scancodes per *its* configured layout. If the user picks a non-US
  layout for the target, later `type_text` symbols/passwords can land wrong —
  prefer `press_key` navigation, or hand off to SSH once it's up.

## CLI

The CLI covers the **full surface** and is the only interface for
`firmware-check`/`firmware-update`, `events`, and `ssh-bootstrap`
(see the interface matrix above — `watch` now has an MCP twin,
`wait_for_state`; keyboard/mouse/media DO have MCP act tools
since 0.1.0a8). Use the MCP server for the visual loop (`snapshot`/`classify`)
and the gated act/power tools inside an agent session; use the CLI for
everything else and for one-off checks when no MCP host is in the loop.

The full command set (see [docs/cli.md](https://github.com/DustinTrap/kvm-pilot/blob/main/docs/cli.md)
for the reference table): `kvm-pilot info | capabilities | healthcheck |
firmware-check | firmware-update | snapshot | sensors | logs | ssh-check |
ssh-exec | ssh-discover | ssh-bootstrap | boot-progress | power | power-cycle |
type | key | mouse-move | click | media-list | mount | eject | classify |
watch | events`. Run
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
