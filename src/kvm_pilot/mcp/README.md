# kvm-pilot MCP server (experimental)

A local **stdio MCP server** that exposes a KVM device to MCP-capable agents
(Claude Desktop, Claude Code, other agent hosts). It **ships in the wheel** —
`pip install kvm-pilot` installs it and the `kvm-pilot-mcp` launcher, pulling the
[`mcp`](https://pypi.org/project/mcp/) SDK (`mcp>=1.10`) as a base dependency. The
client/driver code stays stdlib-only; `mcp` is imported only in this subpackage.

> ⚠️ **Experimental alpha.** The core library is **largely unverified** — mostly
> unit-tested against mocks and emulators, with only a handful of device+capability
> combos exercised on real hardware (see the
> [Hardware-Compatibility list](https://github.com/DustinTrap/kvm-pilot/wiki/Hardware-Compatibility)
> for what actually has; issue #7). Treat every result as unverified, and strongly
> consider running with dry-run enabled (below).

## Tools

| Tool | Annotations | What it does |
|---|---|---|
| `info` | `readOnlyHint` | Device / system info |
| `healthcheck` | `readOnlyHint` | **Preflight audit** of the KVM itself — readiness/recovery, security posture, firmware currency (issue #80). Run this **first, on connecting to any device**, before trusting it for real work; a `CRITICAL` (e.g. no out-of-band recovery path) should gate any subsequent destructive op |
| `capabilities` | `readOnlyHint` | Which capabilities the driver supports — **structural/offline** (no network, no preflight). Answers "which tools/actions can this device serve?" so you can pick the right interface up front |
| `power_state` | `readOnlyHint` | `powered_on` plus ATX detail where the driver has it |
| `logs` | `readOnlyHint` | Device/host event log as text (`seek` = seconds of lookback). The text diagnostic when video/streamer/power looks wrong — it names a fault (e.g. a stuck encoder behind a `snapshot` 503) a screenshot can't |
| `snapshot` | `readOnlyHint` | Current screen, returned as a real JPEG **image** content block the model can see |
| `classify_screen` | `readOnlyHint` | Boot/run phase via the vision backend (Anthropic or a local VLM, see below) |
| `ssh_reachable` | `readOnlyHint` | Is the **managed host's OS** reachable over SSH (in-band)? Targets the host *behind* the KVM (its own `ssh_host`), not the appliance — use it to prefer remote recovery before physical intervention |
| `power` | `destructiveHint` | `on` / `off` / `off-hard` / `reset` — **disabled unless the operator opts in** (`KVM_PILOT_MCP_ALLOW_POWER`) |
| `type_text` | `destructiveHint` | Type text on the host console (HID keyboard) — needs `KVM_PILOT_MCP_ALLOW_HID` + per-invocation approval |
| `press_key` | `destructiveHint` | Press one key (e.g. `Enter`, `F2`) — needs `KVM_PILOT_MCP_ALLOW_HID` + approval |
| `send_shortcut` | `destructiveHint` | Send a key chord (e.g. `ControlLeft,AltLeft,F2`). Gated **by effect**: a reboot/power chord (Ctrl+Alt+Del, Magic SysRq) needs `ALLOW_POWER`; an ordinary session chord needs `ALLOW_HID` |
| `ctrl_alt_delete` | `destructiveHint` | Send Ctrl+Alt+Del (a reboot) — classified `power_soft`, so it needs `KVM_PILOT_MCP_ALLOW_POWER`, not the HID gate |
| `mouse` | `destructiveHint` | Move (and optionally click) the mouse. Coords in `percent` (0.0-1.0, default), `pixel`, or `raw` kvmd. A **click** must carry `observed_frame_ref` from a prior `snapshot`; it's refused if the host rebooted/swapped media since (generation changed) so it can't land on a stale screen. Needs `KVM_PILOT_MCP_ALLOW_HID` |
| `ssh_exec` | `destructiveHint` | Run a command on the managed host's OS over SSH — **disabled unless the operator opts in** (`KVM_PILOT_MCP_ALLOW_SSH`) |
| `ssh_discover` | `readOnlyHint` | Scan a CIDR for open SSH — **RISKY/opt-in** (active network scan; `confirm=true` required). Only to help find a target the user can't address, on networks they own |

Every tool result names the **host and driver it acted on**, and read-only
tools always run with a deny-all confirm callback, so a bug in a read path can
never trip a destructive operation. Drivers are built per call and closed
afterwards — important for Redfish BMCs, which cap concurrent sessions
device-side (a leaked session can lock operators out of the BMC).

### Act tools: two guarantees per call (issue #61)

The act tools (`type_text`, `press_key`, `send_shortcut`, `ctrl_alt_delete`)
require **two** things, not one:

1. **Allowed** — the operator enabled the tool's *effect class* via an env flag in
   the server's own environment, and the target profile is on `KVM_PILOT_MCP_PROFILES`
   (if set). Tools are classified by **effect, not transport**: `ctrl_alt_delete`
   and a Ctrl+Alt+Del / Magic-SysRq chord are reboots, so they need
   `KVM_PILOT_MCP_ALLOW_POWER` — an agent can't reboot the box through the weaker
   HID gate by picking a different actuator.
2. **Approved at run time** — a per-invocation human approval via MCP **elicitation**
   when the client supports it (*interactive* posture); otherwise an explicit
   `confirm=true` under the operator's standing policy (*pre-authorized* posture —
   what an unattended install loop uses, since no human is present to answer).

Denials (gate closed, declined/cancelled, not confirmed, invalidated mid-approval)
come back as a normal result with `approved: false` and a reason — the agent can
re-plan, never left hanging. Each result carries a stable `invocation_id` and both
`transport` and `effect` (the signed/expiring audit receipt is a later step, #72).

### Which tools work with which driver

| Driver kind | `info` | `healthcheck` | `capabilities` | `power_state` | `logs` | `snapshot` | `classify_screen` | `power` |
|---|---|---|---|---|---|---|---|---|
| `pikvm` / `glkvm` / `blikvm` | ✅ | ✅ | ✅ | ✅ (with ATX detail) | ✅ | ✅ | ✅ | ✅ |
| `redfish` (iDRAC, iLO, XCC, OpenBMC, …) | ✅ | ✅ (checks it can't serve are omitted) | ✅ | ✅ | ✅ | ❌ no video capability | ❌ no video capability | ✅ |
| `fake` (in-process test double) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

A tool the active driver cannot serve returns a clean MCP tool error naming
the driver kind and the missing capability (it never `AttributeError`s).

### What this server does *not* expose — use another interface

The MCP surface is deliberately small. For anything outside the table, pick the
right interface (the skill's *Choosing an interface* matrix is the full guide):

- **`firmware-check`/`firmware-update`, `events`, `watch`, `mount`/`eject`** →
  the **CLI** (`kvm-pilot <cmd>`); no MCP tool.
- **MSD mode switching** → the **Python library**.
- **Reboot the KVM appliance / restart `kvmd`** → **out-of-band SSH** to the
  appliance. No kvm-pilot interface reboots the box; `power` acts on the
  *managed host*, not the appliance.
- **View the screen when `snapshot` fails** (503, or a tiny frame while a signal
  is present — typically H.264 at the panel's native resolution) → the
  **WebRTC/Janus stream or the vendor web UI**.

## Safety model

**Preflight first.** On connecting to any device, call `healthcheck` before you
trust it for real work (issue #80) — it surfaces readiness/recovery, security,
and firmware risks up front (most importantly, whether there is *any* out-of-band
recovery path if the guest hangs). A `CRITICAL` finding should gate the
destructive `power` tool below. Note: the server does not yet auto-run this on
connect, so the agent must call it as the first step.

The `power` gate is layered; no single layer is trusted on its own:

1. **Operator opt-in (the real gate).** The `power` tool is *disabled by
   default*. It only works when the human operator sets
   `KVM_PILOT_MCP_ALLOW_POWER=1` in the **server's own environment** (e.g. the
   `env` block of the MCP host config) before starting the server. The tool's
   refusal message deliberately does not spell out the variable assignment —
   an agent must not be able to relay the incantation; a human has to read
   this README and decide.
2. **`confirm=true` second factor.** Even with the gate open, `power` requires
   an explicit `confirm=true` argument. This flag is model-supplied — it is
   **not** human approval and must not be treated as such.
3. **Client-side human approval.** MCP hosts should require per-call human
   approval for `power` (in Claude Desktop / Claude Code, do **not** put this
   tool on an "always allow" list). The `destructiveHint`/`readOnlyHint`
   annotations are published so hosts can render a differentiated approval UI —
   but annotations are hints, never a security boundary.
4. **Dry-run.** With `KVM_PILOT_MCP_DRY_RUN=1` every driver is built with
   `dry_run=True`: destructive commands are logged and *skipped*, and every
   tool result says it ran in dry-run mode. **Recommended default for this
   largely-unverified alpha.**
5. **Untrusted screen content.** `snapshot` and `classify_screen` feed
   *target-controlled* console output into the agent's context. A compromised
   or hostile host can render text designed to steer the agent (prompt
   injection) — one more reason to keep layer 3 on.

### Profile retargeting (ops guidance)

Every tool accepts an optional `profile` argument, and a model-supplied value
takes precedence over the `KVM_PILOT_PROFILE` pin. `~/.config/kvm-pilot/config.toml`
therefore acts as a **credential store the agent can roam**: any profile the
file defines can be targeted by the agent, not just the pinned one. Keep only
hosts you are willing to expose in the config file this server can read (or
point `KVM_PILOT_CONFIG` at a dedicated file). Pinning via
`KVM_PILOT_HOST`/`KVM_PILOT_USER`/`KVM_PILOT_PASSWD` env vars instead is
immune to retargeting — env values beat file-profile values. Result
provenance (`host` / `driver` in every result) makes the acted-on target
visible in transcripts.

## Run

```bash
pip install --pre kvm-pilot     # installs the CLI, the MCP server, and the `mcp` SDK
kvm-pilot-mcp                    # start the stdio server (or: python -m kvm_pilot.mcp.server)
```

## Environment variables

| Variable | Meaning |
|---|---|
| `KVM_PILOT_PROFILE` | Default profile from `~/.config/kvm-pilot/config.toml` |
| `KVM_PILOT_HOST` / `KVM_PILOT_USER` / `KVM_PILOT_PASSWD` / … | Direct device config (see `kvm_pilot.config`); beats file-profile values |
| `KVM_PILOT_CONFIG` | Path of the config file (default `~/.config/kvm-pilot/config.toml`) |
| `KVM_PILOT_MCP_ALLOW_POWER` | **Operator-only** opt-in enabling the `power` tool **and** power-effect HID (`ctrl_alt_delete`, reboot chords) (`1`/`true`/`yes`) |
| `KVM_PILOT_MCP_ALLOW_HID` | **Operator-only** opt-in enabling HID input (`type_text`, `press_key`, ordinary `send_shortcut`) |
| `KVM_PILOT_MCP_ALLOW_SSH` | **Operator-only** opt-in that enables the `ssh_exec` tool (`1`/`true`/`yes`) |
| `KVM_PILOT_MCP_PROFILES` | **Fail-closed** allowlist of profile names the server may target (comma-separated). Unset = no allowlist; set-but-empty = allow nothing; a target not on the list is refused (never a fall-back to all configured hosts) |
| `KVM_PILOT_MCP_ELICIT` | Set to `off` to force the pre-authorized posture (env gate + `confirm=true`) even for elicitation-capable clients; otherwise a per-invocation human approval is requested when the client supports it |
| `KVM_PILOT_SSH_HOST` / `KVM_PILOT_SSH_USER` / `KVM_PILOT_SSH_PORT` / `KVM_PILOT_SSH_KEY` | The **managed host's** SSH target (a different machine from the KVM) for `ssh_reachable` / `ssh_exec`; also settable per-profile as `ssh_host` etc. |
| `KVM_PILOT_MCP_DRY_RUN` | Build every driver with `dry_run=True`; destructive calls are logged, never sent |
| `KVM_PILOT_VISION_BACKEND` | Vision backend for `classify_screen`: `anthropic` (default) or `local` (any OpenAI-compatible VLM: LM Studio, Ollama, vLLM) |
| `KVM_PILOT_VISION_URL` | Base URL of the local VLM (required for `local`) |
| `KVM_PILOT_VISION_MODEL` | Vision model id — required for `local`; optional pin for `anthropic` (otherwise the newest model is auto-resolved once per process) |
| `ANTHROPIC_API_KEY` | Required for the `anthropic` vision backend |

## Claude Desktop

Add to your MCP config (`claude_desktop_config.json`). Dry-run is enabled here
on purpose — remove it only once you accept the alpha status, and add
`KVM_PILOT_MCP_ALLOW_POWER` only if you want the `power` tool live:

```json
{
  "mcpServers": {
    "kvm-pilot": {
      "command": "kvm-pilot-mcp",
      "env": {
        "KVM_PILOT_PROFILE": "homelab",
        "KVM_PILOT_MCP_DRY_RUN": "1",
        "ANTHROPIC_API_KEY": "sk-..."
      }
    }
  }
}
```

Keep per-call approval enabled for `power` in the host UI.

A single-file `.mcpb` bundle (zero-toolchain install) is tracked in issue #7.
