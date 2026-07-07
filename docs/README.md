# kvm-pilot documentation

Everything for **using and developing** `kvm-pilot`, in one place. This folder is
the source of truth; the [project wiki](https://github.com/DustinTrap/kvm-pilot/wiki)
is an auto-generated, nicely formatted mirror of these pages (built by
[`.github/workflows/wiki-sync.yml`](https://github.com/DustinTrap/kvm-pilot/blob/main/.github/workflows/wiki-sync.yml)
on every push to `main` — edit the files here, never the wiki directly).

## Start here

- **New here? Drive a KVM from your agent** → [Getting started](getting-started.md)
- **Overview, install, quickstart** → [README](https://github.com/DustinTrap/kvm-pilot/blob/main/README.md)
- **Release history** → [CHANGELOG](https://github.com/DustinTrap/kvm-pilot/blob/main/CHANGELOG.md)

## Guides

- [Architecture](architecture.md) — the driver-plugin design, capability protocols, and diagrams.
- [CLI reference](cli.md) — every `kvm-pilot` subcommand: capability required, destructive gating, key flags.
- [Configuration](configuration.md) — the config file, every `KVM_PILOT_*` env var, and precedence.
- [Design decisions](decisions.md) — the "looks wrong but is intentional" record, newest first.
- [Reflexes (RFC)](reflexes.md) — the post-GA edge-autonomy playbook runner: act locally on known steps, escalate surprises to the agent ([#117](https://github.com/DustinTrap/kvm-pilot/issues/117)).
- [Redfish reference](redfish.md) — the BMC driver: hypermedia navigation, auth, and firmware quirks.
- [Firmware registry](firmware-registry.md) — the firmware-currency check, the community registry data model, and the GitHub-based single-source-of-truth + ingestion design (#80 follow-up).
- [Remote firmware update](firmware-update.md) — the GL `/api/upgrade/*` surface, the reliability/risk model, and the gated `firmware-update` command (#92).
- [Unattended Linux installs](unattended-install.md) — prefer text mode + SSH over driving a graphical installer via KVM HID: the per-distro boot-arg matrix (Anaconda `inst.sshd`/`inst.text`, d-i network-console, Subiquity autoinstall, linuxrc `ssh=1`) and the SSH handoff ([#129](https://github.com/DustinTrap/kvm-pilot/issues/129)).
- [Claude skill](../src/kvm_pilot/skill/SKILL.md) — the bundled skill for driving `kvm-pilot` from Claude.
- [MCP server](../src/kvm_pilot/mcp/README.md) — the bundled Model Context Protocol server (`kvm-pilot-mcp`).

## Contributing & security

- [Contributing](CONTRIBUTING.md) — dev setup, the pre-PR checklist, and engineering principles.
- [Security policy](SECURITY.md) — reporting a vulnerability and operational guidance.

## Analysis

Session-level review narratives — what was reviewed, how, what was found, and
what changed. Individual judgment calls live in [decisions.md](decisions.md);
these are the stories around them.

- [2026-07-01 deep review](analysis/2026-07-01-deep-review.md) — the top-to-bottom
  multi-agent review: 86 verified findings, 10 same-day fix commits, issues #37–#65,
  and the milestones that came out of it.
