# Reflexes — edge autonomy / on-demand playbook runner (RFC)

> **Status:** draft RFC · **Target:** first major release *after* GA · **Tracking:**
> [epic #117](https://github.com/DustinTrap/kvm-pilot/issues/117). This is a
> **request for comment** — the design is settled enough to build against and open
> enough to change. Pressure-test it on the epic.

## The gap

Today kvm-pilot is *smart hands*, not an autopilot. It already has the hard part:
a working observe→act primitive (`ScreenAnalyzer.wait_for_state`) sitting on a
**sensing hierarchy** that answers "what state is the host in?" from cheap signals
— power / no-signal, structured `BootProgress`, on-device OCR, an unchanged frame
— and only pays for a vision-model call when none of those resolve (see
[architecture](architecture.md) and `src/kvm_pilot/vision/analyzer.py`). What it
lacks is the loop that strings steps together; that sequencing lives only in
hand-written `examples/*.py`.

**Reflexes** is a thin, on-demand **playbook runner** over that primitive. A
playbook's recognizable steps advance with **no model round-trip on the hot
path**; anything unrecognized or off-script **escalates back to the calling
agent** with full context, then resumes from the same step.

**Headline value: unattended reliability** — flows advance on *real device state*,
not by sleeping on a timer, so they adapt to a slow disk or a stalled mirror.
Lower latency (the model is consulted only on surprises) and lower vision-model
spend follow from the same design.

## The reflex arc

A reflex acts before the signal reaches the brain. One observation, two paths:

```
                          ┌─ phase is in-playbook ─▶ act ─▶ next step      REFLEX · local, no round-trip
 observe & recognize ─────┤
  (cheap gates first)     └─ unknown / off-script ─▶ hold ─▶ ask agent ─▶ resume    ESCALATE · to the agent
```

- **Reflex (local).** Non-destructive actions — keystrokes, menu navigation —
  fire at full speed, no gate, no round-trip.
- **Escalate (to the agent).** On `unknown` / low-confidence / off-script / a
  precondition mismatch / a timeout, the runner **holds** and hands control back.
  It never calls a planning model itself.

"Hold" is **passive**: the runner stops sending input and the host sits at its
current screen. The escalation payload carries the step index, the observed phase,
confidence, the OCR text, and a snapshot, plus a `reason`:

| `reason` | Meaning |
| --- | --- |
| `unknown_phase` | The classifier could not place the screen. |
| `low_confidence` | A phase was guessed below the threshold. |
| `precondition_mismatch` | A destructive step's expected phase was not observed. |
| `timeout` | The `wait_for` phase never arrived. |
| `needs_authorization` | A destructive step was not covered by the run's pre-authorization. |

The agent replies with an amended / extended playbook (or an inline next action),
and the runner **resumes from `step_index`** rather than restarting.

## Decisions (locked for v1)

Recorded, with rationale, in [design decisions](decisions.md) › *Orchestration*.

| Area | Choice | Why |
| --- | --- | --- |
| Escalation | Return to the calling agent | The runner is a bounded tool; on a surprise it hands control back. Keeps "the AI agent gives the instructions." |
| Authoring | Both — agent + library | Agent-generated for novel goals; a curated / community set for common, vetted flows. |
| Format | Ansible-*style* YAML + JSON | The readability of Ansible tasks, run on our own stdlib runner — **not** the Ansible engine. |
| Dependency | PyYAML as a base dep | A user-facing surface ships included, per the batteries rule. Core import stays stdlib via lazy import. |
| Runtime | On-demand now | Fits the current one-shot / stateless-MCP model. A continuous watcher is a designed follow-on. |
| Destructive steps | Pre-authorize the run + verify each precondition | The human authorizes up front; the runner re-checks the expected phase before each destructive step fires. |
| Hold | Passive + documented limits | Stop advancing; document the flows a host won't wait on (auto-continuing boot / POST / watchdog timers). |
| Positioning | Unattended reliability | Advance on real state, not timers. Latency and cost efficiency are the supporting beats. |

### Why not Ansible-the-engine?

Playbooks *read* like Ansible tasks because that YAML is what humans find easiest
to author and enhance — but they run on a small stdlib runner. Adopting Ansible
itself was rejected: it fights the stdlib-only-core + `pip`-ships-everything thesis
(it becomes a heavy shell-out extra, not "included"); its model *converges
idempotent tasks to a desired state*, which does not fit a **reactive** watch →
act → escalate-on-unknown loop; and its host/connection model is wrong for a
managed host that has **no agent** and is driven through the KVM's REST API. A
real, opt-in Ansible collection may still come later as an ecosystem integration
— it is just not the core format.

## A playbook

Human-authored YAML and agent-emitted JSON load to one step model. A step names
the phase it `wait_for`s and the action it takes; a destructive step also declares
the `precondition` phase the runner must confirm before it fires.

```yaml
# operator pre-authorizes this run's destructive steps
name: unattended-ubuntu-install
authorize:
  destructive: true
  ops: [media.mount, power.cycle, media.eject]
on_unknown: escalate            # surprise → return to agent
steps:
  - name: Mount install media
    action: mount_iso
    args: { source: "{{ iso }}", cdrom: true }
    destructive: true
  - name: Boot from media
    action: hard_cycle
    destructive: true
    precondition: power_off      # verified via cheap gate first
  - name: Take default at GRUB
    wait_for: grub_menu          # OCR resolves "GNU GRUB" — no model call
    action: press_key
    args: { key: Enter }
  - name: Wait out package install
    wait_for: installer_progress
    timeout: 3600
  - name: Confirm complete + detach
    wait_for: installer_complete
    action: msd_disconnect
    destructive: true
```

**Step schema** (both formats): `name`, `wait_for`, `action`, `args`,
`destructive`, `precondition`, `timeout`, `min_confidence`, and — stretch —
`when` / `register` for conditional flows.

## Safety model

**A classification never authorizes a destructive act.** Destructive steps are
authorized **once, up front** — the safety decision moves to authoring time, not
the hot path. But pre-authorization is **not blanket**: before each destructive
step the runner re-verifies, through the cheap gates, that the host is actually in
that step's `precondition` phase. A mismatch does not fire the step — it
**escalates**. That is what stops a surprise state from triggering the wrong
destructive action, the exact risk the project's invariant guards against. Every
destructive op keeps its `safety.guard()` routing, and the health preflight gate
([health.py](architecture.md)) still runs first.

## Scope

**In — v1 (on-demand playbook runner):**

- Runner + step model over `ScreenAnalyzer` — [#118](https://github.com/DustinTrap/kvm-pilot/issues/118)
- YAML + JSON loaders (PyYAML base dep) — [#119](https://github.com/DustinTrap/kvm-pilot/issues/119)
- `run_playbook` MCP tool + escalation contract — [#120](https://github.com/DustinTrap/kvm-pilot/issues/120)
- Pre-authorize + per-step precondition verification — [#121](https://github.com/DustinTrap/kvm-pilot/issues/121)
- Curated playbook library (from `examples/*.py`) — [#122](https://github.com/DustinTrap/kvm-pilot/issues/122)
- Telemetry: cheap-resolves vs model calls — [#123](https://github.com/DustinTrap/kvm-pilot/issues/123)

**Out — deliberately deferred:** a resident daemon / continuous watcher; outbound
push / callback to the agent; active host-freeze (hold beyond "stop sending
input"); a real opt-in Ansible collection.

## What we'll measure

The numbers that show known steps are really staying off the model, surfaced per
run and fed into the support-matrix telemetry work
([#96](https://github.com/DustinTrap/kvm-pilot/issues/96)):

- **cheap-resolves ÷ vlm-calls** — share of state reads resolved without a model call.
- **escalations** — count and `reason` on each hand-back to the agent.
- **per-step wall-clock** — hot path vs escalated.
- **run outcome** — success / failure across the curated library.

## Feedback

Comment on the [epic (#117)](https://github.com/DustinTrap/kvm-pilot/issues/117)
or the individual child issues. The reflex arc, the YAML shape, and the
pre-authorize-plus-verify safety model are the parts most worth challenging before
code lands.
