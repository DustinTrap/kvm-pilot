# kvm-pilot documentation

Everything for **using and developing** `kvm-pilot`, in one place. This folder is
the source of truth; the [project wiki](https://github.com/DustinTrap/kvm-pilot/wiki)
is an auto-generated, nicely formatted mirror of these pages (built by
[`.github/workflows/wiki-sync.yml`](https://github.com/DustinTrap/kvm-pilot/blob/main/.github/workflows/wiki-sync.yml)
on every push to `main` — edit the files here, never the wiki directly). The
sections below mirror the navigation manifest (`PAGES` in
[`build_wiki.py`](https://github.com/DustinTrap/kvm-pilot/blob/main/.github/scripts/build_wiki.py));
CI fails if this index, `llms.txt`, and the wiki sidebar drift apart (#221).
Adding a doc? See "Adding a doc" in [Contributing](CONTRIBUTING.md).

## Start here

- **New here? Drive a KVM from your agent** → [Getting started](getting-started.md)
- **Overview, install, quickstart** → [README](https://github.com/DustinTrap/kvm-pilot/blob/main/README.md)
- **Something's broken?** → [Troubleshooting & FAQ](troubleshooting.md)
- **Release history** → [CHANGELOG](https://github.com/DustinTrap/kvm-pilot/blob/main/CHANGELOG.md)
- **AI agents:** the repo root carries an [`llms.txt`](https://github.com/DustinTrap/kvm-pilot/blob/main/llms.txt) doc map (per [llmstxt.org](https://llmstxt.org/)).

## Guides

Task-oriented: how to accomplish something.

- [Intel AMT onboarding runbook](amt-onboarding.md) — the operator/agent guide to bringing an AMT box online: expectations, the ordered bring-up steps (provision → healthcheck → enable listeners), the ME-firmware-update hazard, and symptom→fix troubleshooting.
- [Unattended Linux installs](unattended-install.md) — prefer text mode + SSH over driving a graphical installer via KVM HID: the per-distro boot-arg matrix (Anaconda `inst.sshd`/`inst.text`, d-i network-console, Subiquity autoinstall, linuxrc `ssh=1`) and the SSH handoff ([#129](https://github.com/DustinTrap/kvm-pilot/issues/129)).
- [Remote firmware update](firmware-update.md) — the GL `/api/upgrade/*` surface, the reliability/risk model, and the gated `firmware-update` command (#92).
- [Troubleshooting & FAQ](troubleshooting.md) — symptom-first fixes: GLKVM API 404, snapshot failures, approval cancel, dark-host recovery, mouse calibration, SOL noise.

## Reference

Descriptive: what exists and how it behaves.

- [CLI reference](cli.md) — every `kvm-pilot` subcommand: capability required, destructive gating, key flags.
- [Configuration](configuration.md) — the config file, every `KVM_PILOT_*` env var, and precedence.
- [Driver features](driver-features.md) — the complete per-driver capability list with per-feature reliability + testing level (honest maturity; points at the support-matrix source of truth) ([#171](https://github.com/DustinTrap/kvm-pilot/issues/171)).
- [Architecture](architecture.md) — the driver-plugin design, capability protocols, and diagrams.
- [Redfish reference](redfish.md) — the BMC driver: hypermedia navigation, auth, and firmware quirks.
- [Intel AMT / vPro reference](amt.md) — the AMT driver: WS-Man power/boot/inventory, SOL serial, and RFB KVM-redirection (firmware-level BIOS/GRUB screenshot + HID) ([#211](https://github.com/DustinTrap/kvm-pilot/issues/211)).
- [Firmware registry](firmware-registry.md) — the firmware-currency check, the community registry data model, and the GitHub-based single-source-of-truth + ingestion design (#80 follow-up).
- [Claude skill](../src/kvm_pilot/skill/SKILL.md) — the bundled skill for driving `kvm-pilot` from Claude: the core rules plus a map of when to read each playbook.
  Its playbooks (read at need-time; also re-served mid-session by the MCP `doctrine` tool, [#222](https://github.com/DustinTrap/kvm-pilot/issues/222)):
  [interfaces](../src/kvm_pilot/skill/references/interfaces.md) —
  [recovery](../src/kvm_pilot/skill/references/recovery.md) —
  [setup & gates](../src/kvm_pilot/skill/references/setup.md) —
  [Linux installs](../src/kvm_pilot/skill/references/linux-install.md) —
  [target context](../src/kvm_pilot/skill/references/target-context.md) —
  [Python library](../src/kvm_pilot/skill/references/library.md).
- [MCP server](../src/kvm_pilot/mcp/README.md) — the bundled Model Context Protocol server (`kvm-pilot-mcp`).
- [Hardware-Compatibility list](https://github.com/DustinTrap/kvm-pilot/wiki/Hardware-Compatibility) — generated from the community run ledger: what has actually been exercised live, per device + firmware + capability.

## Runbooks & test plans

Procedures executed against real hardware.

- [Hardware reliability test plan](test-plan.md) — the reusable full-fleet sweep procedure: exercise every CLI + MCP function across device states, cross-check each result against independent ground truth to catch false reports, and feed the support matrix ([#172](https://github.com/DustinTrap/kvm-pilot/issues/172)).
- [Hardware test plan: iLO / iDRAC](hardware-test-plan-ilo-idrac.md) — quick-execute runbook to validate the Redfish driver (boot-device + power) against an HPE DL380 G9 (iLO4) and a Dell R710 (iDRAC6, no-Redfish → IPMI) ([#200](https://github.com/DustinTrap/kvm-pilot/issues/200)/[#29](https://github.com/DustinTrap/kvm-pilot/issues/29)).

## Design records

Decisions and RFCs, not how-tos.

- [Design decisions](decisions.md) — the "looks wrong but is intentional" record, newest first.
- [Reflexes (RFC)](reflexes.md) — the post-GA edge-autonomy playbook runner: act locally on known steps, escalate surprises to the agent ([#117](https://github.com/DustinTrap/kvm-pilot/issues/117)).

## Project

- [Contributing](CONTRIBUTING.md) — dev setup, the pre-PR checklist, and engineering principles.
- [Writing a first-class driver](plugin-development.md) — the procedural guide for adding a new device driver/plugin to the fleet-wide quality bar: capabilities, the five test layers, every doc surface, and the reliability/maturity evidence chain, with a Definition-of-Done checklist ([#211](https://github.com/DustinTrap/kvm-pilot/issues/211)).
- [Security policy](SECURITY.md) — reporting a vulnerability and operational guidance.

## Analysis (internal reports)

Session-level review narratives — what was reviewed, how, what was found, and
what changed. Individual judgment calls live in [decisions.md](decisions.md);
these are the stories around them.

- [2026-07-01 deep review](analysis/2026-07-01-deep-review.md) — the top-to-bottom
  multi-agent review: 86 verified findings, 10 same-day fix commits, issues #37–#65,
  and the milestones that came out of it.
- [2026-07-03 RM1PE firmware + encoder](analysis/2026-07-03-rm1pe-firmware-and-encoder.md) —
  the first real-hardware run: the firmware-update live no-op (#94/#95) and the
  H.264 encoder wedge behind the snapshot 503s.
- [2026-07-08 a13→a14 performance](analysis/2026-07-08-perf-a13-a14.md) —
  the first performance baseline: honest before/after library-level latencies
  for the a14 interface router + persistent SSH, measured on the real fleet.
- [2026-07-08 a13→a14 end-to-end tasks](analysis/2026-07-08-e2e-leaner-cut.md) —
  whole operator tasks timed start→result: persistent SSH's ~2.2–2.5× health-check
  win is real; every interactive KVM-plane task is flat.
