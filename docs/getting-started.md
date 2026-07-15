# Getting started — drive a KVM from your AI agent

This is the fastest path from zero to "my agent is operating a remote machine
through its KVM." It targets the **agent + MCP** workflow (Claude Code, Claude
Desktop, or any MCP host). For the Python library and CLI, see the
[README](https://github.com/DustinTrap/kvm-pilot/blob/main/README.md#quickstart).

> ⚠️ Beta. The core read paths are live-verified on GL-RM1PE (the
> [Hardware-Compatibility list](https://github.com/DustinTrap/kvm-pilot/wiki/Hardware-Compatibility)
> shows what's actually been exercised); treat anything not on that list as
> unverified, and **confirm destructive steps** (power, media, keystrokes)
> before running them.

## 1. Install

```bash
pip install --pre kvm-pilot
```

`kvm-pilot` is a pre-release, so `--pre` is required — a bare `pip install
kvm-pilot` deliberately installs nothing. One install brings the `kvm-pilot` CLI,
the `kvm-pilot-mcp` server, and the bundled Claude skill.

## 2. Enable the MCP server in your agent

The `pip install` above provides the `kvm-pilot-mcp` launcher; your agent still
has to be told to load it. You can just ask your agent — e.g. *"load the kvm-pilot
MCP server that pip installed and walk me through enabling it"* — or register it
yourself. On **Claude Code**:

```bash
claude mcp add kvm-pilot -s user \
    -e KVM_PILOT_PROFILE=<profile> -e KVM_PILOT_MCP_READ_ONLY=1 -- \
    kvm-pilot-mcp
```

- **`-s user`** (not `-s local`) makes it available no matter which directory you
  launch the agent from — a `-s local` registration only loads in the current
  project's directory.
- **`KVM_PILOT_MCP_READ_ONLY=1`** is the safest first rung: destructive tools
  aren't registered at all, so the agent can run status reports, healthchecks,
  and snapshots but *cannot* touch power, keyboard, or media. Climb the **trust
  ladder** as your hardware gets verified: swap it for
  `KVM_PILOT_MCP_DRY_RUN=1` to rehearse destructive flows (calls logged, not
  sent), then open the per-effect `KVM_PILOT_MCP_ALLOW_*` gates one at a time
  (see the [MCP server README](https://github.com/DustinTrap/kvm-pilot/blob/main/src/kvm_pilot/mcp/README.md)).

![The trust ladder: rung one READ_ONLY — only read tools are registered and every effect gate is force-closed; rung two DRY_RUN — act tools appear but destructive calls are logged, never sent; rung three per-effect ALLOW_* flags — power, HID, media, boot-config, SSH, appliance and external writes each opted in individually, with per-call approval on top.](trust-ladder.svg)

**Then restart your agent session.** MCP servers load at session start, so on
Claude Code you must **exit your current session and start a new one** for the
tools to appear. On relaunch you may get a prompt asking you to **activate the
kvm-pilot MCP interface — accept it.**

Verify it's live:

```bash
claude mcp list          # expect: kvm-pilot ... ✔ Connected
```

or ask the agent to list its tools and look for `mcp__kvm-pilot__*` (e.g.
`mcp__kvm-pilot__snapshot`).

## 3. Point it at your KVM (credentials)

For a quick test, set the KVM's password in your agent's environment — most agents
accept a plain instruction like:

```
set KVM_PILOT_PASSWD=<your-kvm-password>
```

(and `KVM_PILOT_HOST` / `KVM_PILOT_USER` if you aren't using a profile). This is
**per-session and stored in plaintext** in the agent's environment — fine for a
first run, not for anything lasting.

For anything persistent, put a **profile** in
`~/.config/kvm-pilot/config.toml` and reference it with `KVM_PILOT_PROFILE`, so the
password lives in a `chmod 600` file rather than the agent/host config. See
[Configuration](configuration.md) for the file format, every `KVM_PILOT_*`
variable, and precedence.

> **GLKVM (GL.iNet) devices:** the PiKVM REST API is **disabled by default** on GL
> firmware. Enable it in `/etc/kvmd/nginx-kvmd.conf` on the device first, or every
> call returns 404 — and note a firmware upgrade can revert it.

## 4. Know the difference: the KVM vs. the server it controls

This trips up almost every first run. There are **two machines**:

- The **KVM appliance** (PiKVM / GL-RM1PE / BliKVM) — it has its own IP, e.g.
  `10.0.1.11`. That's the address you give kvm-pilot.
- The **connected server** — the machine the KVM plugs into and controls. It's a
  *separate* host with its own IP, OS, and SSH.

`kvm-pilot` acts on the **connected server** through the KVM (power, screen,
keyboard). Rebooting the **KVM appliance itself** is a different, separately
gated action (the `appliance_reboot` tool, or SSH to the appliance). So phrase
prompts about *the server behind the KVM*, e.g. "on `10.0.1.11`'s connected
server," to avoid ambiguity.

![Two machines: the KVM appliance has its own IP, runs kvmd, and receives kvm-pilot's REST calls and the appliance tools; the managed host is a separate machine with its own IP that receives keystrokes and power through the appliance's HDMI, USB and ATX wiring — and, once its OS is up, its own in-band SSH channel.](two-machines.svg)

## 5. Try it — sample prompts

```
Use kvm-pilot for a status report on 10.0.1.11's connected server
Use kvm-pilot to reboot 10.0.1.11's connected server
Use kvm-pilot to install Red Hat Enterprise Linux on 10.0.1.11's connected server
Use kvm-pilot to troubleshoot 10.0.1.11's connected server — it's <describe the problem>
```

Start with the **status report** — it's read-only and runs the healthcheck, so
it's the safe way to confirm everything's wired up before you touch power or media.

## 6. What a good first run looks like

A healthy status report reads roughly like this (abridged):

```
Status Report — 10.0.1.11

KVM Device
  Driver           PiKVM
  Model            v3 (Rockchip RV1126B)
  Firmware (KVMD)  4.82

Connected Server
  Power            On (video signal detected)
  Screen           Black / blank (display may be asleep or at a dark console)
  ATX Control      Not wired — no remote power/reset capability
  Virtual Media    Available, no image attached

Health Summary
  API Reachable        OK
  Video Signal         OK
  Recovery Path        CRITICAL — no ATX cable, can't remotely power-cycle a hung server
  TLS Verification     WARNING — disabled (self-signed cert, MITM risk on LAN)
```

Two things to internalise from that example:

- **Read the `Recovery Path` finding.** `CRITICAL` here means there's no
  out-of-band reset — if the server hangs you can't power-cycle it through the KVM.
  Wire the ATX cable to the server's front-panel header to fix it.
- **"Power: On" is not always the truth.** On devices where power readings aren't
  trustworthy (no ATX board), a live "power on" can come from an HDMI handshake
  while the server is actually off or asleep — which is why the screen is black.
  Don't take `powered_on: true` as proof the OS is up; confirm with the screen and,
  if you can reach it, an SSH check to the server.

## Tips & tricks

A short list that saves most new users their first few mistakes:

- **Start read-only.** A status report and the healthcheck change nothing — run
  them first to confirm the wiring and surface risks (like a missing ATX cable)
  *before* you touch power or media.
- **Keep dry-run on at first.** With `KVM_PILOT_MCP_DRY_RUN=1`, destructive tool
  calls are logged instead of sent. Drop it per-flow once you trust it.
- **Run the healthcheck before anything destructive.** It's the intake gate; a
  `CRITICAL` finding (e.g. no recovery path) is your cue to stop and wire hardware
  or line up a remote fallback before committing to a remote power/boot/install.
- **Use a profile, not an env password, for anything you'll reuse** — it keeps the
  credential in a `chmod 600` file, out of shell history and agent config.
- **Name the machine you mean** — "the connected server behind the KVM at `<ip>`"
  vs. "the KVM appliance itself" (see §4). Ambiguity here causes wrong-target ops.
- **Installing an OS? Decide whose locale wins.** The installer's language /
  keyboard-layout / timezone prompts describe the *target server*, not your
  laptop. Your agent should ask whether your local settings apply — answer
  deliberately for a machine in another region instead of accepting your own
  locale by default.
- **Parallelize read-only work** — ask for `healthcheck` + `info` + `logs` at once;
  never parallelize destructive steps.
- **Once the server's OS is on the network, prefer SSH to it** for in-band work —
  it's faster and more reliable than typing through the KVM's keyboard. Have the
  server's IP / hostname / FQDN ready (it's *not* the KVM's address).
- **A black screen isn't necessarily "off."** See §6 — confirm with the screen and
  an SSH reachability check, not the power reading alone.

- **Close the loop — this beta runs on community evidence.** After a good first
  run, `kvm-pilot test-report --profile <p>` probes your device (read-only by
  default) and appends an evidence row you can paste into a two-minute
  [hardware report](https://github.com/DustinTrap/kvm-pilot/issues/new?template=hardware-report.yml).
  **Success or failure both count** — a failure report on your device+firmware
  is exactly what moves the
  [Hardware-Compatibility matrix](https://github.com/DustinTrap/kvm-pilot/wiki/Hardware-Compatibility),
  and the hourly ingest does the rest automatically.

New to this? Your agent can also just walk you through it — ask it to *"get me
started with kvm-pilot"* and it will point you back here and offer the safe first
steps.

## Next steps

- [Claude skill](https://github.com/DustinTrap/kvm-pilot/blob/main/src/kvm_pilot/skill/SKILL.md)
  — how the agent chooses the best interface per action, and its safety rules.
- [MCP server](https://github.com/DustinTrap/kvm-pilot/blob/main/src/kvm_pilot/mcp/README.md)
  — the full operator guide (tools, env vars, dry-run, the `power` gate).
- [Configuration](configuration.md) — profiles and every `KVM_PILOT_*` variable.
