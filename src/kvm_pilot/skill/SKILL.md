---
name: kvm-pilot
description: >-
  AI-driven bare-metal control of headless machines through IP-KVMs (PiKVM,
  GL.iNet GLKVM GL-RM1/RM1PE, BliKVM), server BMCs (Redfish iDRAC/iLO/XCC,
  IPMI), and Intel AMT/vPro. Use whenever the user wants to remotely operate,
  install, or recover a physical machine: power on/off/cycle, mount an install
  ISO, set the next boot device, enter BIOS/UEFI, type or click at the console,
  watch the screen for a boot phase (POST, GRUB, installer, login, crash), run
  an unattended OS install, or diagnose a host that went dark. Also use when
  choosing between the bundled MCP server (kvm-pilot-mcp), the kvm-pilot CLI,
  the Python library, and SSH for a given action. Intel AMT is the out-of-band
  answer for business laptops where an HDMI-capture KVM is blind below the OS.
  Beta: treat device+capability combos absent from the support matrix as
  unverified and confirm destructive steps with the user.
---

# kvm-pilot skill

> ⚠️ **Beta — verify against the matrix.** The core read paths are
> live-verified on GL-RM1PE at beta maturity, but many device+capability combos
> are still unit-tested with mocks only (see the
> [Hardware-Compatibility list](https://github.com/DustinTrap/kvm-pilot/wiki/Hardware-Compatibility)
> for what actually has). Treat every result as unverified, expect bugs, and
> never point a destructive operation (power, reset, media, keystrokes) at a
> machine the user can't afford to have power-cycled unexpectedly. Surface each
> destructive step to the user before executing it.

This skill is a thin wrapper over the installable `kvm-pilot` package. The code
lives in the package, not here — install it and import it rather than copying
client logic into a script. Vision (screen classification) runs on Claude or
any local OpenAI-compatible VLM — see
[references/setup.md](references/setup.md).

## How to use this skill — read the playbook at need-time

The detailed playbooks live in `references/` next to this file, split so you
can read exactly the one the moment calls for instead of holding all of them
in mind. **When one of these situations comes up, (re)read its file — even if
you read it hours ago. A fresh read at need-time beats a faded memory,
especially late in a long session:**

| Situation | Read |
|---|---|
| Picking how to do an action (screen, HID, media, boot control, logs, SSH) — the full per-action interface matrix, multitasking rules, CLI command set | [references/interfaces.md](references/interfaces.md) |
| Host dark / wedged / unreachable; a snapshot failed or can't be trusted; before ever asking the user to physically intervene | [references/recovery.md](references/recovery.md) |
| Installing the package, registering the MCP server, the full tool list + effect gates, approval cancel/denied handling, first-run orientation | [references/setup.md](references/setup.md) |
| Any OS install through the KVM | [references/linux-install.md](references/linux-install.md) |
| An installer or answer file asks for locale, keyboard, or timezone | [references/target-context.md](references/target-context.md) |
| Driving the Python library directly; worked examples | [references/library.md](references/library.md) |

No file access, or the session has compacted since you read this? The MCP
server serves the same content: call the read-only `doctrine` tool — no
arguments lists the topics; `topic="recovery"` returns that playbook. And when
resuming a long flow, call `session` first: it reports what this server has
already done (act journal), what it's allowed to do (open effect gates by
name), and what you were last waiting for.

## The rules that must survive a long session

1. **Confirm every destructive step with the user** — power, reset, media,
   keystrokes, clicks — unless they have explicitly approved unattended
   operation *in this session*. Most device+capability combos are unverified;
   check the `support_matrix` tool before trusting one that matters.
2. **Never pass an allow-all confirm callback** (e.g. `lambda op, d: True`) —
   and note that *omitting* `confirm` is also unattended (the library default
   allows everything so plain scripts work). Actively pass
   `interactive_confirm` or a callback that relays each question to the user;
   the ask-first duty sits with you, not the library.
3. **Rehearse first**: `dry_run=True` (library) / `KVM_PILOT_MCP_DRY_RUN=1`
   (MCP) on any flow the user hasn't already trusted on their hardware.
4. **Never parallelize state changes.** Concurrency is for read-only
   observation only; serialize anything destructive behind a single confirm.
5. **A vision classification must never trigger a destructive action on its
   own.**

## First contact: run the healthcheck — the intake gate

The moment you connect to a KVM — before you drive it, and before you record
it as a managed profile — run the device healthcheck (MCP `healthcheck` tool,
or `kvm-pilot healthcheck --profile <name>`). It audits the KVM appliance
*itself* (readiness/recovery, security posture, firmware currency) and is the
safety net for the whole tool (#80). Treat it as a severity-tiered gate:
surface every `WARNING`/`CRITICAL` to the user with its implication, and a
`CRITICAL` **blocks** — do not proceed to a destructive or multi-step flow
until the user explicitly decides to continue. The highest-value finding is
`recovery-path` — whether *any* out-of-band reset exists (ATX wired / GPIO /
Redfish / IPMI) if the guest hangs; the operator must learn this *before*
committing to a remote install, not mid-outage. **Read-only intake
(`info`/`capabilities`/`snapshot`) does not auto-run it** — run it yourself on
first contact; a clean `info` does not mean the device was vetted.

## Host vs. appliance — keep these straight

The `power` tool/CLI acts on the **managed host** (the machine the KVM
controls). Rebooting the **KVM appliance itself** — e.g. to clear a stuck video
encoder — is a separate, separately-gated act: MCP `appliance_reboot`
(`KVM_PILOT_MCP_ALLOW_APPLIANCE` + confirm) or SSH into the *appliance* and
`reboot`; restarting just `kvmd` remains SSH-only. And the appliance's address
is **not** the managed host's address — they are separate machines with
separate IPs.

## Recovery order — memorize the shape, read the playbook when it happens

When the host is dark or wedged, prefer remote recovery, in this order:

1. **Wake-on-LAN first** — cheap, non-invasive, and instantly wakes a host
   that idle-suspended (the most common cause of "went dark"). It is a
   diagnostic, not a last resort.
2. **In-band SSH to the target OS** — once it answers, the fastest and most
   reliable lever; prefer it over KVM keystrokes.
3. **KVM-side recovery** — `recover-hid`, then an appliance reboot for a
   wedged encoder — only once WoL/ping show the host is up while video/HID
   are stuck.
4. **Intel AMT/vPro on business machines** — power, firmware-level
   BIOS/POST/GRUB screenshot, and SOL, all below the OS.
5. **Physical intervention** — only after the remote options are exhausted.

The full playbook — symptom signatures, how to read a failed snapshot,
keep-awake, network-sweep rules — is
[references/recovery.md](references/recovery.md). Re-read it *at that moment*
rather than working from memory; it is also served by the MCP `doctrine` tool
(topic "recovery").

## Interface quick picks

The full per-action matrix is
[references/interfaces.md](references/interfaces.md). The picks needed most:

- **See the screen**: MCP `snapshot` — returns a model-visible image plus
  `signal` (`hdmi_signal` is the authoritative picture-present flag) and a
  `frame_ref` for follow-up mouse clicks. A byte-identical repeat frame is
  flagged as possibly stale — verify via `signal` + `logs` before acting on it.
- **Diagnose "video/power looks wrong"**: MCP/CLI `logs` — the text log names
  a fault (e.g. a stuck encoder behind a `snapshot` 503) a screenshot can't.
- **Wait for a boot phase**: MCP `wait_for_state` (bounded ≤ 300 s per call;
  chain calls for long installs) or CLI `watch`.
- **Media**: check `list_virtual_media` **before** asking the user to download
  or upload an ISO — it may already be in storage.
- **Once the target OS is up**: prefer `ssh_reachable`/`ssh_exec` over KVM
  keystrokes (the actuation-channel hierarchy, #81).
- **Firmware, events, serial console (SOL), SSH bootstrap**: CLI only.
