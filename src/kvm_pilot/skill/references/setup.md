# Setup — install, MCP server, gates & approvals

> Part of the bundled kvm-pilot skill. Read this when installing the package,
> registering or configuring the MCP server, checking which tool needs which
> effect gate, or handling an approval denial/cancel. Also served at runtime by
> the MCP `doctrine` tool (topic "setup").

## Install

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
`pip install kvm-pilot` deliberately picks up no pre-release. A single install brings
the `kvm-pilot` CLI, the `kvm-pilot-mcp` server, and this skill. To make Claude
Code discover the skill, run `kvm-pilot install-skill` (copies it to
`~/.claude/skills/kvm-pilot`; re-run after upgrading, then restart the session).
For the latest unreleased tree, install from git:

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

## Enabling the MCP server

**The tools it exposes**, all named `mcp__kvm-pilot__<tool>`:
- Read-only: `info`, `power_state`, `capabilities`, `support_matrix` (what's
  been exercised live per device+firmware, plus its derived maturity — check it
  before trusting a capability that matters), `healthcheck`, `logs`,
  `snapshot` (model-visible JPEG), `classify_screen` (boot/run phase — uses a
  server-side vision backend if configured, else falls back to caller-side
  classification), `wait_for_state` (bounded server-side wait for a phase,
  ≤ 300 s per call), `boot_options` (current boot-source override + the
  device's allowable targets), `ssh_reachable`, `list_virtual_media` (MSD
  storage inventory — check it before requesting an ISO download/upload),
  `appliance_status` (the KVM appliance's own OS diagnostics over
  appliance-SSH), `access_paths` (which independent recovery paths are live —
  the lockout-exposure view), `doctrine` (re-serve any topic of this operating
  doctrine — recovery, interfaces, setup, linux-install, target-context,
  library, or core — for mid-session re-anchoring; offline, no device I/O),
  `session` (this server's current posture: dry-run/read-only state, which
  effect gates are open by class name, the recent act journal, and the last
  `wait_for_state` breadcrumb — call it first when resuming after a context
  compaction), and `ssh_discover` (CIDR scan — RISKY/opt-in,
  needs `confirm=true`, user-owned networks only)
- **Destructive act tools** — each needs the operator to opt the tool's *effect*
  in via an env flag **and** a per-invocation approval (a human elicitation, or
  `confirm=true` under a standing policy):
  - `power` — on/off/cycle/reset (`KVM_PILOT_MCP_ALLOW_POWER`)
  - `wake` — Wake-on-LAN magic packet to the managed host, the out-of-band
    power-on path when ATX isn't wired (`ALLOW_POWER`)
  - `type_text` / `press_key` / `send_shortcut` / `mouse` — HID input
    (`KVM_PILOT_MCP_ALLOW_HID`); a reboot chord in `send_shortcut` needs `ALLOW_POWER`
  - `calibrate_mouse` — measure & store this host's pointer correction
    (pointer moves only, ~10-30 s on a static screen; `ALLOW_HID`, one
    approval for the whole run)
  - `ctrl_alt_delete` — a reboot, so it needs `ALLOW_POWER` (not the HID gate)
  - `mount_iso` / `eject` — virtual media (`KVM_PILOT_MCP_ALLOW_MEDIA`)
  - `set_boot_device` — boot-source override (pxe/cd/hdd/usb/bios…, one-time
    or persistent) on Redfish/IPMI/AMT devices (`KVM_PILOT_MCP_ALLOW_CONFIG`)
  - `amt_enable` — open the Intel AMT SOL (`feature='sol'`) or KVM 5900
    (`feature='kvm'`) redirection listener over WS-Man (`KVM_PILOT_MCP_ALLOW_CONFIG`);
    `consent_off=true` disables the on-screen user-consent prompt and needs the
    **second** gate `KVM_PILOT_MCP_ALLOW_CONSENT_OFF`
  - `ssh_exec` — run a command over SSH (`KVM_PILOT_MCP_ALLOW_SSH`)
  - `appliance_reboot` — reboot the **KVM appliance itself** to clear a wedged
    encoder; target power untouched, drops KVM control ~60 s
    (`KVM_PILOT_MCP_ALLOW_APPLIANCE`)
  - `file_firmware_report` — file the firmware-registry report as a GitHub
    issue (`KVM_PILOT_MCP_ALLOW_EXTERNAL_WRITE`; an external write, not a
    device op)

**Approval posture in chat clients:** an elicitation-capable chat client raises
a human approval prompt per act call, and sending a new chat message **cancels
the pending prompt**. The signature is act results with `approved: false` +
`approver: null` and `denied_reason: "approval cancel"` (or `"denied by
approver"` after a mis-click) while read-only tools keep working. That is an
approval-delivery problem, not a device fault — the action never reached the
target. Do **not** silently retry into repeated cancellations: relay the
result's `remediation` field to the user. Their options: answer the approval
prompt before typing the next message, or (operator decision) set
`KVM_PILOT_MCP_ELICIT=off` in the server env and reconnect, making the
`ALLOW_*` effect gate + per-call `confirm=true` the standing authorization —
at the cost of per-call human approval.

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
