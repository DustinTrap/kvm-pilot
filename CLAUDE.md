# CLAUDE.md

Guidance for AI agents (and humans) working in this repo.

## What this is
`kvm-pilot` — a stdlib-only Python client + CLI for IP-KVM devices (PiKVM, the
GL.iNet GLKVM fork, BliKVM) with a pluggable LLM **vision** subsystem that
classifies a KVM screenshot into a boot/run phase. Early **alpha**. Hardware
validation is tracked **per device + firmware + capability** in the support
matrix (#96) and the community
[Hardware-Compatibility list](https://github.com/DustinTrap/kvm-pilot/wiki/Hardware-Compatibility) —
that page is the source of truth for what has actually been exercised. As of
2026-07-03 a GL-RM1PE has been run live (read/`snapshot`/`healthcheck`/`logs`
paths on firmware V1.5.1 release2 and V1.9.1 release1; the remote firmware-flash
is a known no-op there, #94/#95), but **most device+capability combos remain
unverified / mock-only** — be honest about that in any docs or messaging: don't
claim a feature is "tested" or "beta" beyond what the matrix shows; point readers
to it for current truth.

## Non-negotiable conventions
- **Consult + update GitHub issues before any meaningful change.** This repo is
  issue-per-finding — an issue is the unit of record. Before editing code for a
  non-trivial change: search existing issues for one that covers it; if none, open
  one describing the problem, the decision, and the plan; reference it in the
  commit/PR and post material findings or scope changes back to the issue. Do not
  make meaningful changes with no tracked issue.
- **Core is stdlib-only / zero runtime deps.** Anything needing a third-party
  package goes behind an optional extra in `pyproject.toml` (`totp`, `ws`, …) and
  is imported lazily inside the function that uses it (see `http.py:_totp_now`
  and `client.py:watch_events`).
- **No hard-coded model versions.** The Anthropic vision backend resolves the
  newest model at runtime (`src/kvm_pilot/vision/anthropic.py`); never bake a
  `claude-*` version string into the code.
- **Destructive operations are gated.** Any op that can change a target's running
  state (power, reset, virtual media, GPIO, Redfish reset) must be added to
  `DESTRUCTIVE_OPS` in `src/kvm_pilot/safety.py` and routed through
  `self.safety.guard(op, description)`. A vision classification must never trigger
  a destructive action on its own.
- **Preflight before trust (issue #80).** Bringing a device into use — first
  connection, adding a managed profile, or ahead of any destructive/multi-step
  flow — runs through the device `healthcheck` (`src/kvm_pilot/health.py`;
  `run_healthcheck`). It is the intake gate, and `#80`'s intent is that it
  auto-runs *on first connection*, not only before destructive ops. Today only
  the destructive-subcommand path auto-gates (`cli.py` `_preflight_gate`) and the
  MCP server does not auto-run it at all — until that gap is closed, the operating
  procedure (`skill/SKILL.md`, `mcp_server/README.md`) requires running it
  explicitly on first contact. Don't treat a bare `info`/`snapshot` as vetting.
- **Capabilities, not a monolith.** New device support = a driver implementing the
  relevant capability protocols in `src/kvm_pilot/drivers/base.py` (`Power`,
  `HID`, `Video`, `VirtualMedia`, `GPIO`, `Events`, `SystemInfo`). See
  `docs/architecture.md`.

## Engineering principles (how to make changes)
Optimize for the next person reading this, not for cleverness.
- Prefer the boring, standard solution; use existing utilities instead of new ones.
- Smallest change that works. No speculative generality.
- No new abstraction/layer/dependency unless it's used in ≥2 places.
- Prefer composition and early returns over inheritance and nesting.
- Delete more than you add where you can.
- Add tests that read as documentation of intent.
- Write the simplest thing that could work; ask for more if needed rather than
  building it speculatively.
- After a change, run `/simplify` and report what you cut.
- Record non-obvious "looks wrong but is intentional" choices in
  `docs/decisions.md` so they aren't re-litigated.

## Layout
- `src/kvm_pilot/client.py` — `PiKVMDriver`, the PiKVM-family REST client (`KVMClient`/`PiKVMClient` are aliases).
- `src/kvm_pilot/http.py` — stdlib HTTP transport (retry/backoff, secret redaction).
- `src/kvm_pilot/safety.py` — `SafetyPolicy`, `DESTRUCTIVE_OPS`.
- `src/kvm_pilot/drivers/` — capability protocols (`base.py`), the `make_driver()` registry
  (`__init__.py`), and drivers: `pikvm.py` (`GLKVMDriver`/`BliKVMDriver`), `fake.py`, `redfish/`.
- `src/kvm_pilot/vision/` — pluggable vision backends + `ScreenAnalyzer`.
- `src/kvm_pilot/{config,errors,cli}.py` — config resolution, exceptions, CLI.
- `tests/` — unit tests (HTTP + vision mocked, `tests/conftest.py`) plus pure-stdlib
  fake servers `emulator.py` (kvmd) and `redfish_emulator.py` exercised over the real transport.
- `docs/` — the docs hub (`README.md` index): `architecture.md` (driver-plugin design +
  diagrams), `redfish.md` (Redfish reference), `decisions.md` (design-decision records),
  `CONTRIBUTING.md`, `SECURITY.md`. `skill/SKILL.md` (bundled Claude skill) and
  `mcp_server/README.md` stay next to their code but are mirrored into the docs too.
- The GitHub wiki is auto-generated from `docs/` by `.github/workflows/wiki-sync.yml`
  (via `.github/scripts/build_wiki.py`) — edit the docs, never the wiki.

## Dev workflow (Python ≥ 3.11)
```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev,totp,ws]"
.venv/bin/ruff check .
.venv/bin/mypy src/kvm_pilot
.venv/bin/pytest
```
CI (`.github/workflows/ci.yml`) runs all three on Python 3.11/3.12/3.13, plus a
security job (bandit + pip-audit) and an opt-in sushy-tools Redfish integration
job — keep them green. See docs/CONTRIBUTING.md for the full pre-PR checklist.

## Safety in tests & dev
Never point destructive operations at real hardware from tests or examples. The
suite mocks the transport; to exercise a destructive path use `dry_run=True` or a
`confirm` callback that returns `False`.

## Release
The version lives in `src/kvm_pilot/__about__.py`. Releases publish to PyPI via
GitHub Trusted Publishing (`.github/workflows/release.yml`, environment `pypi`)
on a published GitHub Release. The current `0.1.0a1` is a yanked, opt-in alpha.
