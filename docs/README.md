# kvm-pilot documentation

Everything for **using and developing** `kvm-pilot`, in one place. This folder is
the source of truth; the [project wiki](https://github.com/DustinTrap/kvm-pilot/wiki)
is an auto-generated, nicely formatted mirror of these pages (built by
[`.github/workflows/wiki-sync.yml`](https://github.com/DustinTrap/kvm-pilot/blob/main/.github/workflows/wiki-sync.yml)
on every push to `main` — edit the files here, never the wiki directly).

## Start here

- **Overview, install, quickstart** → [README](https://github.com/DustinTrap/kvm-pilot/blob/main/README.md)
- **Release history** → [CHANGELOG](https://github.com/DustinTrap/kvm-pilot/blob/main/CHANGELOG.md)

## Guides

- [Architecture](architecture.md) — the driver-plugin design, capability protocols, and diagrams.
- [Configuration](configuration.md) — the config file, every `KVM_PILOT_*` env var, and precedence.
- [Design decisions](decisions.md) — the "looks wrong but is intentional" record, newest first.
- [Redfish reference](redfish.md) — the BMC driver: hypermedia navigation, auth, and firmware quirks.
- [Claude skill](../skill/SKILL.md) — the bundled skill for driving `kvm-pilot` from Claude.
- [MCP server](../mcp_server/README.md) — the experimental Model Context Protocol server.

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
