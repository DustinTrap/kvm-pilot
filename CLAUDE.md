# CLAUDE.md

Guidance for AI agents (and humans) working in this repo.

> **Starting a session? Read `RESUME.md` first, if present** — the current working
> state, what's in flight, and next steps. It is a **local, untracked scratch file**
> (gitignored since #209: it carries fleet/opsec details that must not publish; a
> fresh clone won't have one — fall back to open issues + the CHANGELOG). It's
> refreshed by `/checkpoint` (`.claude/skills/checkpoint/`) at the end of each
> session, and never committed. This file is the standing doctrine; `RESUME.md`
> is where we are right now.

## What this is
`kvm-pilot` — a stdlib-only Python client + CLI for IP-KVM devices (PiKVM, the
GL.iNet GLKVM fork, BliKVM) with a pluggable LLM **vision** subsystem that
classifies a KVM screenshot into a boot/run phase. **Beta** (current version:
`src/kvm_pilot/__about__.py` — don't restate it here, it drifts). Hardware
validation is tracked **per device + firmware + capability** in the support
matrix (#96) and the community
[Hardware-Compatibility list](https://github.com/DustinTrap/kvm-pilot/wiki/Hardware-Compatibility) —
that page is the source of truth for what has actually been exercised. GL-RM1PE
units have been run live (read/`healthcheck`/`logs` on firmware V1.5.1 release2
and V1.9.1 release1; `snapshot` verified on V1.9.1 only — on V1.5.1 it fails with
an undecodable H.264 frame, #107/#151; the remote firmware-flash is a known no-op
there, #94/#95), and a Dell iDRAC6 has exercised the IPMI driver live end-to-end
(power/boot-device/sensors/SEL/SOL, 2026-07-14), but **most device+capability
combos remain unverified / mock-only** — be honest about that in any docs or messaging: don't
claim a feature is "tested" or "beta" beyond what the matrix shows; point readers
to it for current truth.

## Non-negotiable conventions
- **Consult + update GitHub issues before any meaningful change.** This repo is
  issue-per-finding — an issue is the unit of record. Before editing code for a
  non-trivial change: search existing issues for one that covers it; if none, open
  one describing the problem, the decision, and the plan; reference it in the
  commit/PR and post material findings or scope changes back to the issue. Do not
  make meaningful changes with no tracked issue.
- **`pip install kvm-pilot` ships everything the user needs.** Every user-facing
  surface — the CLI, the bundled Claude skill, the MCP server, and anything added
  going forward — must live under `src/kvm_pilot/` so it lands in the wheel, and a
  surface's runtime dependency is a **base** dependency in `pyproject`
  `[project].dependencies` with a console script (`kvm-pilot`, `kvm-pilot-mcp`).
  Don't hide a user-facing surface behind an opt-in extra. (`dev` tooling stays an
  extra.)
- **Client/driver code is stdlib-only at import time.** The library modules
  (`client.py`, `drivers/`, `http.py`, `vision/`) import only the standard library;
  a third-party need (`mcp` for the server, `pyotp`/`ws` for a feature) is imported
  lazily inside its own subpackage/function (see `http.py:_totp_now`,
  `client.py:watch_events`/`_connect_event_ws`), never at core import. The
  *distribution* depends on `mcp` (the bundled server) and `websocket-client` —
  that's deliberate; see [batteries-included rule above and issue #109].
  `websocket-client` became a **base** dependency once headless GL `snapshot` needed
  it to start GL's on-demand streamer (#142) — a core user-facing surface can't hide
  behind an extra. `totp` remains an opt-in extra; `ws` is a no-op back-compat alias.
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
  procedure (`src/kvm_pilot/skill/SKILL.md`, `src/kvm_pilot/mcp/README.md`) requires running it
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
  (`__init__.py`), and drivers: `glkvm.py` (the GL.iNet fork — GL-specific behavior and
  quirks go HERE, #140), `pikvm.py` (other API-compatible forks, currently BliKVM),
  `fake.py`, `redfish/`.
- `src/kvm_pilot/vision/` — pluggable vision backends + `ScreenAnalyzer`.
- `src/kvm_pilot/{config,errors,cli}.py` — config resolution, exceptions, CLI.
- `tests/` — unit tests (HTTP + vision mocked, `tests/conftest.py`) plus pure-stdlib
  fake servers `emulator.py` (kvmd) and `redfish_emulator.py` exercised over the real transport.
- `docs/` — the docs hub (`README.md` index): `architecture.md` (driver-plugin design +
  diagrams), `redfish.md` (Redfish reference), `decisions.md` (design-decision records),
  `CONTRIBUTING.md`, `SECURITY.md`. `src/kvm_pilot/skill/SKILL.md` (bundled Claude
  skill, shipped as package data) and `src/kvm_pilot/mcp/README.md` stay next to
  their code but are mirrored into the docs too.
- `src/kvm_pilot/mcp/` — the bundled FastMCP stdio server (`server.py`, entry point
  `kvm-pilot-mcp`); `src/kvm_pilot/skill/SKILL.md` — the bundled Claude skill. Both
  ship in the wheel (see the batteries-included rule above).
- The GitHub wiki is auto-generated from `docs/` by `.github/workflows/wiki-sync.yml`
  (via `.github/scripts/build_wiki.py`) — edit the docs, never the wiki. `PAGES`
  in that script is the **single navigation manifest** (#221): it defines each
  page's sidebar section, and `--check` (CI) fails if `docs/README.md` or
  `llms.txt` drift from it. New doc → follow "Adding a doc" in docs/CONTRIBUTING.md.

## Dev workflow (Python ≥ 3.11)
```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev,totp,ws]"
.venv/bin/ruff check .
.venv/bin/mypy src/kvm_pilot
.venv/bin/pytest
```
`make emulators` starts the local emulator stack (Redfish reference on
127.0.0.1:8000) and `make integration` runs tests/integration against it;
`make kvmd-testenv` (Linux only) runs the genuine kvmd daemon. Details:
docs/CONTRIBUTING.md.

CI (`.github/workflows/ci.yml`) runs all three on Python 3.11/3.12/3.13/3.14
(with a 75% coverage gate), plus a security job (bandit + pip-audit), an
opt-in sushy-tools Redfish integration job, and an `emulator-stack` job that
stands up `compose.yaml` and runs the integration tests through it — keep them
green. See docs/CONTRIBUTING.md for the full pre-PR checklist.

## Safety in tests & dev
Never point destructive operations at real hardware from tests or examples. The
suite mocks the transport; to exercise a destructive path use `dry_run=True` or a
`confirm` callback that returns `False`.

## Release
The version lives in `src/kvm_pilot/__about__.py`. Releases publish to PyPI via
GitHub Trusted Publishing (`.github/workflows/release.yml`, environment `pypi`)
on a published GitHub Release. The current line is an opt-in (`--pre`) beta —
see `__about__.py`/CHANGELOG for the exact version; `0.1.0a1` is yanked.
