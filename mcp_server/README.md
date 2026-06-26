# kvm-pilot MCP server (experimental)

A local **stdio MCP server** that exposes a KVM device to MCP-capable agents
(Claude Desktop, other agents). It is a *separate component* from the
stdlib-only core library — it depends on the [`mcp`](https://pypi.org/project/mcp/)
SDK.

> ⚠️ **Experimental.** The core library is an untested alpha — **never run on
> real hardware** (see issue #7). Destructive actions are gated; treat every
> result as unverified.

## Tools

| Tool | Kind | Notes |
|---|---|---|
| `info` | read-only | device / system info |
| `power_state` | read-only | ATX state + `powered_on` |
| `snapshot` | read-only | current screen as base64 JPEG |
| `classify_screen` | read-only | boot/run phase via the vision backend (needs `ANTHROPIC_API_KEY`) |
| `power` | **destructive** | `on`/`off`/`off-hard`/`reset`; **refuses unless `confirm=true`** |

The read-only tools build the client with a deny-all confirm callback; the
`power` tool refuses unless an agent explicitly passes `confirm=true`. A model
can never power-cycle a machine implicitly.

## Run

```bash
# from the repo root
pip install -e ".[totp,ws]"      # the core library
pip install mcp                  # the MCP SDK (see requirements.txt)
python mcp_server/server.py
```

Configure the device with the usual env vars or a profile
(`KVM_PILOT_HOST` / `KVM_PILOT_USER` / `KVM_PILOT_PASSWD`, or `KVM_PILOT_PROFILE`
naming a profile in `~/.config/kvm-pilot/config.toml`). Set `ANTHROPIC_API_KEY`
for `classify_screen`.

## Claude Desktop

Add to your MCP config (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "kvm-pilot": {
      "command": "python",
      "args": ["/abs/path/to/kvm-pilot/mcp_server/server.py"],
      "env": { "KVM_PILOT_PROFILE": "homelab", "ANTHROPIC_API_KEY": "sk-..." }
    }
  }
}
```

A single-file `.mcpb` bundle (zero-toolchain install) is tracked in issue #7.
