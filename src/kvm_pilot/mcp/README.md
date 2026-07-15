# kvm-pilot MCP server

A local **stdio MCP server** that exposes a KVM device to MCP-capable agents
(Claude Desktop, Claude Code, other agent hosts). It **ships in the wheel** —
`pip install kvm-pilot` installs it and the `kvm-pilot-mcp` launcher, pulling the
[`mcp`](https://pypi.org/project/mcp/) SDK (`mcp>=1.10`) as a base dependency. The
client/driver code stays stdlib-only; `mcp` is imported only in this subpackage.

> ⚠️ **Beta.** The core read paths are live-verified on real hardware
> (GL-RM1PE, beta maturity in the support matrix), but many device+capability
> combos are still tested against mocks and emulators only (see the
> [Hardware-Compatibility list](https://github.com/DustinTrap/kvm-pilot/wiki/Hardware-Compatibility)
> for what actually has; issue #7). Treat every result as unverified, and strongly
> consider running with dry-run enabled (below).

## Tools

| Tool | Annotations | What it does |
|---|---|---|
| `info` | `readOnlyHint` | Device / system info |
| `healthcheck` | `readOnlyHint` | **Preflight audit** of the KVM itself — readiness/recovery, security posture, firmware currency (issue #80). Run this **first, on connecting to any device**, before trusting it for real work; a `CRITICAL` (e.g. no out-of-band recovery path) should gate any subsequent destructive op. Includes a `support-evidence` finding naming what has (and has NOT) been live-verified on this exact device+firmware (#102) |
| `capabilities` | `readOnlyHint` | Which capabilities the driver supports — **structural/offline** (no network, no preflight). Answers "which tools/actions can this device serve?" so you can pick the right interface up front. Also carries `live_evidence`: which device+firmware combos this driver has real-hardware run evidence for |
| `support_matrix` | `readOnlyHint` | What has actually been **exercised on real hardware**, per device+firmware+capability — aggregated from the run ledger shipped in the package (the wiki Hardware-Compatibility data) plus each combo's derived maturity level (#98), offline, no device call. Anything in `never_exercised` (or a combo with no row) is unverified: confirm destructive steps with the user |
| `power_state` | `readOnlyHint` | `powered_on` plus ATX detail where the driver has it |
| `logs` | `readOnlyHint` | Device/host event log as text (`seek` = seconds of lookback). The text diagnostic when video/streamer/power looks wrong — it names a fault (e.g. a stuck encoder behind a `snapshot` 503) a screenshot can't |
| `snapshot` | `readOnlyHint` | Current screen, returned as a real JPEG **image** content block the model can see. The JSON payload carries the live `signal` state (`hdmi_signal` = the authoritative picture-present flag, plus online/resolution/fps/`streamer_idle`) and `unchanged_since_last_snapshot` — byte-identical pixels across an expected screen change are stale/cached: verify via `signal` + `logs` before acting (#141/#143) |
| `classify_screen` | `readOnlyHint` (open-world) | Boot/run phase. Uses the server-side vision backend (Anthropic or a local VLM, see below) when configured; **if the server has no vision key it falls back to caller-side** — returning the screenshot + prompt/schema for a vision-capable agent to classify. Cheap on-device gates (power-off/no-signal/boot-progress/OCR) resolve with no key at all. Result is a `mode="server"` dict, or a `[json, image]` list to classify yourself |
| `wait_for_state` | `readOnlyHint` (open-world) | Block (bounded) until the screen reaches a named boot/run phase — the MCP twin of CLI `watch`. Validates the phase token up front, polls the cheap gates + server-side vision with backoff, emits MCP progress per poll, and returns the reached phase + confidence + a `frame_ref` to anchor a follow-up `mouse` click; a timeout comes back as a same-path `reached: false` result with the last observed state. `timeout` is capped server-side at 300 s — chain calls for longer waits. **Needs server-side vision for phases the cheap gates can't resolve**: with no vision key it fails fast pointing you at `classify_screen` polling (caller-side classification) instead of burning the timeout |
| `ssh_reachable` | `readOnlyHint` | Is the **managed host's OS** reachable over SSH (in-band)? Targets the host *behind* the KVM (its own `ssh_host`), not the appliance — use it to prefer remote recovery before physical intervention. Pass `host=` to override the target at runtime (e.g. an install-time DHCP address the profile can't know); also surfaced as an `ssh-reachable` healthcheck when `ssh_host` is configured |
| `list_virtual_media` | `readOnlyHint` | Inventory the KVM's virtual-media (MSD) storage: stored images (name/size/completeness), the selected drive image, and whether media is attached. **Check this before asking the operator to download or upload an ISO** — the image may already be on the device from an earlier job; on brands with a known host-visible gadget name it also returns `host_visible_as` — the device name the target's boot menu shows when the medium is truly presented (e.g. 'Glinet Optical Drive' on GLKVM, #78); its absence next to a generic CD/DVD entry means the media is not really inserted |
| `boot_options` | `readOnlyHint` | Current boot-source override (target/enabled/mode) plus the device's **allowable** boot targets and whether the mode is settable — feature-detected from the BMC (Redfish `AllowableValues` / IPMI), never assumed. Read this before `set_boot_device` so you offer only targets the device accepts |
| `power` | `destructiveHint` | `on` / `off` / `off-hard` / `reset` — **disabled unless the operator opts in** (`KVM_PILOT_MCP_ALLOW_POWER`) |
| `wake` | `destructiveHint` | Send a Wake-on-LAN magic packet to the managed host (its wired NIC's MAC, from the profile or `mac=`) — the out-of-band power-on path when ATX isn't wired, and the first thing to try when a host stopped answering (suspended hosts wake in seconds). A state change, so it shares the power gate (`KVM_PILOT_MCP_ALLOW_POWER`) |
| `set_boot_device` | `destructiveHint` | Boot-source override on Redfish/IPMI devices (`pxe`/`cd`/`hdd`/`usb`/`bios`/…, one-time by default or `persistent=true`, optional UEFI/legacy mode) — a **config mutation**, its own effect class: needs `KVM_PILOT_MCP_ALLOW_CONFIG` + approval. Check `boot_options` first for what this BMC accepts |
| `type_text` | `destructiveHint` | Type text on the host console (HID keyboard) — needs `KVM_PILOT_MCP_ALLOW_HID` + per-invocation approval |
| `press_key` | `destructiveHint` | Press one key (e.g. `Enter`, `F2`) — needs `KVM_PILOT_MCP_ALLOW_HID` + approval |
| `send_shortcut` | `destructiveHint` | Send a key chord (e.g. `ControlLeft,AltLeft,F2`). Gated **by effect**: a reboot/power chord (Ctrl+Alt+Del, Magic SysRq) needs `ALLOW_POWER`; an ordinary session chord needs `ALLOW_HID` |
| `ctrl_alt_delete` | `destructiveHint` | Send Ctrl+Alt+Del (a reboot) — classified `power_soft`, so it needs `KVM_PILOT_MCP_ALLOW_POWER`, not the HID gate |
| `mouse` | `destructiveHint` | Move (and optionally click) the mouse. Coords in `percent` (0.0-1.0, default), `pixel`, or `raw` kvmd. A **click** must carry `observed_frame_ref` from a prior `snapshot`; it's refused if the host rebooted/swapped media since (generation changed) so it can't land on a stale screen. Needs `KVM_PILOT_MCP_ALLOW_HID` |
| `calibrate_mouse` | reversible write | Measure & store this host's mouse commanded→observed correction (#128): park → 5-point grid → fit → held-out verify. Afterwards `mouse` percent coords apply it transparently and report `calibrated: true`. Moves the live cursor ~10-30s on a **static** screen; pointer moves only, but gated like HID input (`KVM_PILOT_MCP_ALLOW_HID` + one approval for the whole run). Needs Pillow on the server (`pip install 'kvm-pilot[calibrate]'`); stored per (host, capture resolution) — a resolution change makes it stale, never applied |
| `mount_iso` | reversible write | Mount an ISO (local path or URL; `usb=true` for a flash drive) as virtual media — needs `KVM_PILOT_MCP_ALLOW_MEDIA` + approval |
| `eject` | reversible write | Detach virtual media (inverse of `mount_iso`) — needs `KVM_PILOT_MCP_ALLOW_MEDIA` + approval |
| `ssh_exec` | `destructiveHint` | Run a command on the managed host's OS over SSH — **disabled unless the operator opts in** (`KVM_PILOT_MCP_ALLOW_SSH`). `host=` overrides the target at runtime |
| `file_firmware_report` | reversible write (external) | Reconcile the device's firmware currency against the registry SSoT and, when the registry is behind, file the "Latest known release" report as a GitHub issue via the `gh` CLI (the MCP twin of CLI `firmware-check`, #189/#190). An **external write** (its own effect class): **disabled unless** `KVM_PILOT_MCP_ALLOW_EXTERNAL_WRITE` + per-invocation approval; validated and deduped exactly like the CLI path; `dry_run=true` previews the issue body; a missing/unauthenticated `gh` is a graceful `filed=false` reason |
| `ssh_discover` | `readOnlyHint` | Scan a CIDR for open SSH — **RISKY/opt-in** (active network scan; `confirm=true` required). Only to help find a target the user can't address, on networks they own |
| `appliance_status` | `readOnlyHint` | Read-only diagnostics from the **KVM appliance's own OS** over appliance-SSH (load, D-state video threads). Note: load is ~10 even when idle on these units, so it is not a health signal — use `healthcheck`'s `encoder-wedge` finding |
| `appliance_reboot` | `destructiveHint` | Reboot the **KVM appliance** (not the target) to clear a wedged encoder — **disabled unless the operator opts in** (`KVM_PILOT_MCP_ALLOW_APPLIANCE`) + `confirm=true`. Drops KVM control ~60s; target power untouched. Never automate it |
| `access_paths` | `readOnlyHint` | Which **independent recovery paths** are live for the device — the lockout-exposure view (#162): kvmd-REST / appliance-SSH / target-SSH / out-of-band power / console-HID, each labeled by failure *domain* so redundancy isn't oversold. `summary.out_of_band_live=false` means every path shares the appliance's fate — a fully hung appliance can't be recovered remotely |

### Annotation profiles (#195)

Every tool declares **all four** MCP hints explicitly (a CI test enforces it —
the spec defaults are punitive: an unset `destructiveHint` reads as *true* and
an unset `openWorldHint` reads as *"reaches the internet"*). Clients build real
policy from these bits (auto-approval and parallel dispatch of read-only tools,
confirmation UI on destructive ones), so precision matters. **Hints are
advisory, never a security boundary** — the `ALLOW_*` effect gates and
per-invocation approvals below apply regardless of annotation.

| Profile | readOnly | destructive | idempotent | openWorld | Tools |
|---|---|---|---|---|---|
| read | ✅ | — | ✅ | — | `info` `healthcheck` `capabilities` `support_matrix` `power_state` `boot_options` `logs` `snapshot` `list_virtual_media` `ssh_reachable` `ssh_discover` `appliance_status` `access_paths` |
| read, open-world | ✅ | — | ✅ | ⚠️ | `classify_screen` — the *server-side vision backend* may be a cloud VLM (a local backend never leaves your network) |
| read, open-world, timed | ✅ | — | — | ⚠️ | `wait_for_state` — same vision caveat, and a timed wait is not idempotent |
| destructive | — | ⚠️ | — | — | `power` `wake` `set_boot_device` `type_text` `press_key` `send_shortcut` `ctrl_alt_delete` `mouse` `ssh_exec` `appliance_reboot` — not safely repeatable (a second reset reboots again) |
| reversible write | — | — | ✅ | — | `eject` (undoable and convergent; still MEDIA-gated), `calibrate_mouse` (pointer moves only, re-run converges; still HID-gated) |
| reversible write, open-world | — | — | ✅ | ⚠️ | `mount_iso` (may fetch an ISO by URL), `file_firmware_report` (files a GitHub issue; dedupes against existing ones, hence idempotent) — still gated by MEDIA / EXTERNAL_WRITE |

Every tool result names the **host and driver it acted on**, and read-only
tools always run with a deny-all confirm callback, so a bug in a read path can
never trip a destructive operation. Drivers are built per call and closed
afterwards — important for Redfish BMCs, which cap concurrent sessions
device-side (a leaked session can lock operators out of the BMC).

### Act tools: two guarantees per call (issue #61)

![The approval lifecycle: an act call must pass the ALLOW_* effect gate, then per-invocation approval (a human elicitation, or confirm=true under a standing policy). Approval mints an HMAC-signed, single-use, expiring receipt bound to the exact tool, arguments and host; dispatch verifies and consumes it before touching the device. Expired, replayed or argument-drifted receipts fail closed and must be re-approved.](https://raw.githubusercontent.com/DustinTrap/kvm-pilot/main/docs/approval-lifecycle.svg)

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
come back as a normal result with `approved: false`, a typed `outcome`
(`cancelled` is a benign client-side interruption; `denied` is an explicit no —
branch on `outcome`, not on the human-facing strings, #149) and a reason — the
agent can re-plan, never left hanging.

**Approval receipts (#72):** an approval is a **signed, expiring, single-use
receipt** bound to the exact invocation (host, tool, effect, args-hash, dry-run,
approver). It is re-verified immediately before dispatch and consumed on use:
any bound field changing after approval, an expired receipt
(`KVM_PILOT_MCP_RECEIPT_TTL`, default 60 s), or a replay of a consumed receipt
fails closed as a denial-shaped result. Approved results carry
`receipt: {id, state}` and a real `approval.expires`. Every destructive
invocation terminal — approved, denied, consumed, expired, mismatched,
replayed, dispatch-exception — emits one JSON audit record on the
`kvm_pilot.mcp.audit` logger (capture the server's stderr/logging to retain the
trail; receipts are per-process, a server restart voids them).

#### Troubleshooting: act tools denied with `approval cancel` / `denied by approver` (#149)

**Symptom:** act tools return `approved: false`, `approver: null`, and
`denied_reason: "approval cancel"` (or `"denied by approver"`), while
read-only tools keep working. **Cause:** the chat client cancelled the pending
approval prompt — sending a new message kills the in-flight elicitation; the
action never reached the target, and the result's `remediation` field says so.
**Fix:** answer the prompt before typing the next message; if a client keeps
killing approvals, the operator may set `KVM_PILOT_MCP_ELICIT=off` (trade-off:
no per-call human approval — the remedy advertises this escape hatch only
after ≥2 consecutive client-side kills). Full narrative:
[Troubleshooting & FAQ](https://github.com/DustinTrap/kvm-pilot/blob/main/docs/troubleshooting.md#act-tools-denied-approval-cancel--denied-by-approver).
(A duration-scoped standing approval that would remove the per-keystroke
re-prompt without giving up human sign-off is a follow-up to #72.)

### Which tools work with which driver

| Driver kind | `info` | `healthcheck` | `capabilities` | `power_state` | `logs` | `snapshot` | `classify_screen` / `wait_for_state` | `power` |
|---|---|---|---|---|---|---|---|---|
| `pikvm` / `glkvm` / `blikvm` | ✅ | ✅ | ✅ | ✅ (with ATX detail) | ✅ | ✅ | ✅ | ✅ |
| `redfish` (iDRAC, iLO, XCC, OpenBMC, …) | ✅ | ✅ (checks it can't serve are omitted) | ✅ | ✅ | ✅ | ❌ no video capability | ❌ no video capability | ✅ |
| `fake` (in-process test double) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

A tool the active driver cannot serve returns a clean MCP tool error naming
the driver kind and the missing capability (it never `AttributeError`s).
`support_matrix` takes no profile and contacts no device — it works identically
for every driver.

### What this server does *not* expose — use another interface

The MCP surface is deliberately small. For anything outside the table, pick the
right interface (the skill's *Choosing an interface* matrix is the full guide):

- **`firmware-update`, `events`** → the **CLI** (`kvm-pilot <cmd>`); no MCP
  tool. (`firmware-check`'s report-filing half IS exposed as
  `file_firmware_report`, #190; the currency readout itself rides along in its
  result and in `healthcheck`.)
- **MSD mode switching** → the **Python library**.
- **Restart `kvmd` / inspect `/etc/kvmd` on the appliance** → **out-of-band
  SSH** to the appliance. (A full appliance reboot *does* have a tool now —
  `appliance_reboot`, `ALLOW_APPLIANCE`-gated; `power` still acts on the
  *managed host*, never the appliance.)
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
4. **Read-only launch mode (#196).** With `KVM_PILOT_MCP_READ_ONLY=1` the
   state-changing tools are **not registered at all** (`tools/list` shows only
   the read-only surface, minus `ssh_discover` — an active scan has no place in
   a least-privilege posture), and two independent layers back the filter up:
   every effect gate is force-closed regardless of `ALLOW_*`, and every driver
   is built with a deny-all confirm — so even a bypassed registration cannot
   mutate a target (tool filtering alone is not enforcement; that lesson is
   [CVE-2026-46519](https://www.manifold.security/blog/mcp-server-kubernetes-readonly-bypass)).
   Results carry `read_only: true`. The **trust ladder** for a new fleet:
   start `READ_ONLY` (intake, status reports, support evidence) → graduate to
   `DRY_RUN` (rehearse destructive flows, calls logged not sent) → open
   `ALLOW_*` effect gates one at a time as the matrix verifies your hardware.

   ![The trust ladder: READ_ONLY (see everything, touch nothing) → DRY_RUN (rehearse; destructive calls logged, never sent) → per-effect ALLOW_* opt-ins, each act still requiring per-call approval.](https://raw.githubusercontent.com/DustinTrap/kvm-pilot/main/docs/trust-ladder.svg)
5. **Dry-run.** With `KVM_PILOT_MCP_DRY_RUN=1` every driver is built with
   `dry_run=True`: destructive commands are logged and *skipped*, and every
   tool result says it ran in dry-run mode. **Recommended default until your
   own device+capability combos are verified in the matrix.**
6. **Untrusted screen content.** `snapshot` and `classify_screen` feed
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
| `KVM_PILOT_MCP_ALLOW_HID` | **Operator-only** opt-in enabling HID input (`type_text`, `press_key`, `mouse`, ordinary `send_shortcut`) |
| `KVM_PILOT_MCP_ALLOW_MEDIA` | **Operator-only** opt-in enabling virtual media (`mount_iso`, `eject`) |
| `KVM_PILOT_MCP_ALLOW_SSH` | **Operator-only** opt-in that enables the `ssh_exec` tool (`1`/`true`/`yes`) |
| `KVM_PILOT_MCP_ALLOW_CONFIG` | **Operator-only** opt-in enabling boot-configuration mutation (`set_boot_device`) (`1`/`true`/`yes`) |
| `KVM_PILOT_MCP_ALLOW_APPLIANCE` | **Operator-only** opt-in that enables `appliance_reboot` (rebooting the KVM appliance itself) (`1`/`true`/`yes`) |
| `KVM_PILOT_MCP_ALLOW_EXTERNAL_WRITE` | **Operator-only** opt-in enabling writes to systems *outside* the managed device — currently `file_firmware_report` (files a GitHub issue via the `gh` CLI, which must be installed + authed in the server's environment) (#190) |
| `KVM_PILOT_MCP_PROFILES` | **Fail-closed** allowlist of profile names the server may target (comma-separated). Unset = no allowlist; set-but-empty = allow nothing; a target not on the list is refused (never a fall-back to all configured hosts) |
| `KVM_PILOT_MCP_ELICIT` | Set to `off` to force the pre-authorized posture (the `ALLOW_*` env gate + per-call `confirm=true` become the standing authorization) even for elicitation-capable clients; otherwise a per-invocation human approval is requested when the client supports it. The escape hatch when a chat client keeps cancelling pending approvals (`denied_reason: "approval cancel"`, see troubleshooting above) — trade-off: `off` disables per-call human approval, an operator decision |
| `KVM_PILOT_SSH_HOST` / `KVM_PILOT_SSH_USER` / `KVM_PILOT_SSH_PORT` / `KVM_PILOT_SSH_KEY` | The **managed host's** SSH target (a different machine from the KVM) for `ssh_reachable` / `ssh_exec`; also settable per-profile as `ssh_host` etc. |
| `KVM_PILOT_MCP_READ_ONLY` | Least-privilege launch posture (#196): only read-only tools are registered (minus `ssh_discover`), every effect gate is force-closed regardless of `ALLOW_*`, and drivers are built deny-all. Wins over `ALLOW_*` and `DRY_RUN`. The recommended first rung of the trust ladder (`READ_ONLY` → `DRY_RUN` → per-effect `ALLOW_*`) |
| `KVM_PILOT_MCP_DRY_RUN` | Build every driver with `dry_run=True`; destructive calls are logged, never sent |
| `KVM_PILOT_MCP_RECEIPT_TTL` | Lifetime (seconds) of a per-invocation approval receipt (#72); default 60, clamped to [1, 3600]. A dispatch presented with an expired receipt fails closed and must be re-approved |
| `KVM_PILOT_VISION_BACKEND` | Vision backend for `classify_screen` / `wait_for_state`: `anthropic` (default) or `local` (any OpenAI-compatible VLM: LM Studio, Ollama, vLLM) |
| `KVM_PILOT_VISION_URL` | Base URL of the local VLM (required for `local`) |
| `KVM_PILOT_VISION_MODEL` | Vision model id — required for `local`; optional pin for `anthropic` (otherwise the newest model is auto-resolved once per process) |
| `ANTHROPIC_API_KEY` | The `anthropic` vision backend uses it when set; **not required** — without any vision key, `classify_screen` falls back to caller-side classification (returns the screenshot + prompt for a vision-capable agent); `wait_for_state` needs server-side vision for non-cheap-gate phases and errors fast otherwise (pointing at `classify_screen` polling) |

## Claude Desktop

Add to your MCP config (`claude_desktop_config.json`). Dry-run is enabled here
on purpose — remove it only once your hardware is verified in the matrix (or
you accept the risk), and add
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
