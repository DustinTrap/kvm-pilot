# First run — the whole loop in ten minutes, no hardware

This is a tutorial: follow it top to bottom and you will have installed
`kvm-pilot`, read a (simulated) machine's state, watched the safety layer refuse
a destructive call, rehearsed that call, and finally let it through — first from
the command line, then from your AI agent. Nothing here touches a real device;
the built-in `fake` driver stands in for the KVM. When you are done you will
know exactly what a real first contact will look like, and the
[Getting started](getting-started.md) guide becomes a matter of swapping in an
address.

You need Python 3.11+ and ten minutes. Every command below is copy-pasteable and
every output shown is what the command actually prints.

## 1. Install

```bash
pip install --pre kvm-pilot
kvm-pilot --version
```

`--pre` matters because `kvm-pilot` is a release candidate
([why](troubleshooting.md#which-pip-install-line-should-i-use)). One install
brings the `kvm-pilot` CLI, the `kvm-pilot-mcp` server your agent will load in
step 5, and the bundled Claude skill.

## 2. Meet the stand-in device

Every real driver (PiKVM, GLKVM, Redfish, IPMI, AMT, SSH) declares which
*capabilities* its hardware has. The `fake` driver declares all of them, so you
can exercise every path:

```bash
kvm-pilot capabilities --driver fake
```

```
system_info, power, hid, video, virtual_media, boot_config, gpio, events, logs, boot_progress
video scope: capture — captures an external output; on LAPTOPS firmware screens are invisible
```

That second line is the first honest thing kvm-pilot tells you: a capture-style
KVM only sees what the host routes to its HDMI port, which on a laptop means
nothing before the OS. You will see this distinction again in the healthcheck.

## 3. Read before you act

The read plane is always open. Three commands cover it.

**What is this?**

```bash
kvm-pilot info --driver fake
```

```
{
  "host": "fake",
  "driver": "fake",
  "powered": false,
  "phase": "power_off"
}
```

**Is it ready to be trusted?** The healthcheck is the intake gate: on a real
device you run it before anything else, and the destructive subcommands run it
for you.

```bash
kvm-pilot healthcheck --driver fake
```

```
Healthcheck: fake@fake (firmware=?) — worst: OK
  [OK      ] readiness: Device API reachable — Authenticated and responding.
  [OK      ] readiness: Out-of-band recovery path — Driver exposes out-of-band power/reset.
  [OK      ] readiness: Pre-boot (firmware) video — This interface CAPTURES an external video output, ...
  [OK      ] readiness: Video signal — Capture stream is live (online=True, hdmi_signal=True, width=1920, ...
  [OK      ] readiness: HID reaches the target — Emulated keyboard/mouse online (keyboard=True, mouse=True).
  [OK      ] readiness: Virtual media — No image attached.
```

On a real PiKVM without the ATX cable wired, the second line reads `CRITICAL`
instead: there is no out-of-band reset, so a hung host cannot be recovered
through the KVM. Read that line every time.

**What is on the screen?**

```bash
kvm-pilot snapshot first.jpg --driver fake
```

```
Saved first.jpg
```

The screen is kvm-pilot's only sensor on a KVM. A vision model turns that JPEG
into a boot phase (`bios_menu`, `grub_menu`, `installer_progress`,
`login_prompt`, `crash_screen` ...) via `kvm-pilot classify`; that step needs a
vision backend (an Anthropic key, or any local OpenAI-compatible VLM), so it is
the one thing this tutorial skips. Everything else is the same with or without
it.

## 4. Watch the safety layer work

Now ask for something that changes the machine. Run this exactly as written,
with input closed, so there is no terminal to confirm on — which is how an
automation or an agent would run it:

```bash
kvm-pilot power on --driver fake </dev/null
```

```
[kvm-pilot] Power ON fake
  Refusing: this is a destructive operation and there is no terminal to confirm on. Re-run with --yes (or set a confirm callback in the library) to authorize it non-interactively.
blocked: Operation 'atx.power_on' was not confirmed: Power ON fake
```

Nothing happened, and it told you why. Every operation that can change a
target's running state (power, reset, keystrokes, virtual media, boot order)
goes through this gate.

**Rehearse it.** Dry run logs what *would* be sent and sends nothing:

```bash
kvm-pilot power on --driver fake --yes --dry-run
```

```
WARNING kvm_pilot.safety: DRY-RUN: skipping destructive op 'atx.power_on' (Power ON fake)
power on: requested
```

A compound operation shows each step it would take:

```bash
kvm-pilot power-cycle --driver fake --dry-run
```

```
INFO kvm_pilot.drivers: Hard power cycling fake
WARNING kvm_pilot.safety: DRY-RUN: skipping destructive op 'atx.power_off_hard' (HARD power off fake)
WARNING kvm_pilot.safety: DRY-RUN: skipping destructive op 'atx.power_on' (Power ON fake)
hard power cycle: requested
```

**Let it through.** `--yes` is the explicit authorization:

```bash
kvm-pilot power on --driver fake --yes
```

```
INFO kvm_pilot.safety: Executing destructive op 'atx.power_on' (Power ON fake)
power on: requested
```

That is the whole contract: refuse by default, rehearse on request, execute only
when authorized, and say which of the three happened. On a real device the
executed call is also verified against the device's reported state afterwards
and leaves a signed receipt in the audit log.

## 5. The same loop from your agent

The MCP server exposes the same operations as tools, behind the same gate, plus
a **trust ladder** that decides which tools even exist. Register it against the
fake driver on the lowest rung. On Claude Code:

```bash
claude mcp add kvm-pilot-tutorial -s user \
    -e KVM_PILOT_DRIVER=fake -e KVM_PILOT_MCP_READ_ONLY=1 -- \
    kvm-pilot-mcp
```

Start a new agent session (MCP servers load at session start) and try:

```
Use kvm-pilot for a status report on the connected server
```

The agent runs the healthcheck, info and snapshot you ran by hand. Now ask:

```
Use kvm-pilot to power on the connected server
```

It cannot: on the `READ_ONLY` rung the power tool is not registered at all, so
the agent has nothing to call. That is rung one of the ladder.

Climb one rung. Re-register with dry run instead of read-only:

```bash
claude mcp remove kvm-pilot-tutorial -s user
claude mcp add kvm-pilot-tutorial -s user \
    -e KVM_PILOT_DRIVER=fake -e KVM_PILOT_MCP_DRY_RUN=1 -- \
    kvm-pilot-mcp
```

Restart the session and repeat the power-on prompt. The act tools now exist, so
the agent calls one; the server logs the intended action and sends nothing, and
the agent reports the rehearsal — the same `DRY-RUN` line you saw in step 4.

The top rung opens one effect at a time. Replace `KVM_PILOT_MCP_DRY_RUN=1` with
`KVM_PILOT_MCP_ALLOW_POWER=1` and repeat: this time the server asks *you* to
approve the call at the moment it happens, then executes it and returns a
receipt. Keyboard, media, boot configuration, SSH and appliance changes each
have their own `ALLOW_*` switch; none of them are implied by another.

![The trust ladder: rung one READ_ONLY — only read tools are registered and every effect gate is force-closed; rung two DRY_RUN — act tools appear but destructive calls are logged, never sent; rung three per-effect ALLOW_* flags — power, HID, media, boot-config, SSH, appliance and external writes each opted in individually, with per-call approval on top.](trust-ladder.svg)

When you are done, remove the tutorial server:

```bash
claude mcp remove kvm-pilot-tutorial -s user
```

## 6. Now a real device

You have now seen every state the tool can be in. For a real KVM or BMC, the
sequence is identical with one addition at the front: the healthcheck is run
*first*, on the real device, before you write a profile or open a gate.

- [Getting started](getting-started.md) — register the server against a real
  device, credentials, the two-machines rule (the KVM's address vs. the server
  behind it), and what a good first status report looks like.
- Bring-up runbooks for each device family:
  [GLKVM](glkvm-onboarding.md), [IPMI](ipmi-onboarding.md),
  [Intel AMT](amt-onboarding.md). Redfish BMCs are configured the same way as
  IPMI with `driver = "redfish"`; see the [Redfish reference](redfish.md).
- The [Hardware-Compatibility list](https://github.com/DustinTrap/kvm-pilot/wiki/Hardware-Compatibility)
  shows what has actually been exercised on real hardware. If your device is
  not on it, `kvm-pilot healthcheck` plus `kvm-pilot test-report` and a
  [hardware report](https://github.com/DustinTrap/kvm-pilot/issues/new?template=hardware-report.yml)
  is the most useful contribution you can make.
