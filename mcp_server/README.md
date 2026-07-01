# kvm-pilot MCP server (experimental)

A local **stdio MCP server** that exposes a KVM device to MCP-capable agents
(Claude Desktop, Claude Code, other agent hosts). It is a *separate component*
from the stdlib-only core library — it depends on the
[`mcp`](https://pypi.org/project/mcp/) SDK (`mcp>=1.10`, see
`requirements.txt`).

> ⚠️ **Experimental alpha.** The core library has **never been run on real
> hardware** — it is unit-tested against mocks and emulators only (see issue
> #7). Treat every result as unverified, and strongly consider running with
> dry-run enabled (below).

## Tools

| Tool | Annotations | What it does |
|---|---|---|
| `info` | `readOnlyHint` | Device / system info |
| `power_state` | `readOnlyHint` | `powered_on` plus ATX detail where the driver has it |
| `snapshot` | `readOnlyHint` | Current screen, returned as a real JPEG **image** content block the model can see |
| `classify_screen` | `readOnlyHint` | Boot/run phase via the vision backend (Anthropic or a local VLM, see below) |
| `power` | `destructiveHint` | `on` / `off` / `off-hard` / `reset` — **disabled unless the operator opts in** |

Every tool result names the **host and driver it acted on**, and read-only
tools always run with a deny-all confirm callback, so a bug in a read path can
never trip a destructive operation. Drivers are built per call and closed
afterwards — important for Redfish BMCs, which cap concurrent sessions
device-side (a leaked session can lock operators out of the BMC).

### Which tools work with which driver

| Driver kind | `info` | `power_state` | `snapshot` | `classify_screen` | `power` |
|---|---|---|---|---|---|
| `pikvm` / `glkvm` / `blikvm` | ✅ | ✅ (with ATX detail) | ✅ | ✅ | ✅ |
| `redfish` (iDRAC, iLO, XCC, OpenBMC, …) | ✅ | ✅ | ❌ no video capability | ❌ no video capability | ✅ |
| `fake` (in-process test double) | ✅ | ✅ | ✅ | ✅ | ✅ |

A tool the active driver cannot serve returns a clean MCP tool error naming
the driver kind and the missing capability (it never `AttributeError`s).

## Safety model

The gate is layered; no single layer is trusted on its own:

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
   untested alpha.**
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
# from the repo root
pip install -e ".[totp,ws]"                  # the core library
pip install -r mcp_server/requirements.txt   # the MCP SDK
python mcp_server/server.py
```

## Environment variables

| Variable | Meaning |
|---|---|
| `KVM_PILOT_PROFILE` | Default profile from `~/.config/kvm-pilot/config.toml` |
| `KVM_PILOT_HOST` / `KVM_PILOT_USER` / `KVM_PILOT_PASSWD` / … | Direct device config (see `kvm_pilot.config`); beats file-profile values |
| `KVM_PILOT_CONFIG` | Path of the config file (default `~/.config/kvm-pilot/config.toml`) |
| `KVM_PILOT_MCP_ALLOW_POWER` | **Operator-only** opt-in that enables the `power` tool (`1`/`true`/`yes`) |
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
      "command": "python",
      "args": ["/abs/path/to/kvm-pilot/mcp_server/server.py"],
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
