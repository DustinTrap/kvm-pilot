# CLAUDE.md

Guidance for AI agents (and humans) working in this repo.

## What this is
`kvm-pilot` — a stdlib-only Python client + CLI for IP-KVM devices (PiKVM, the
GL.iNet GLKVM fork, BliKVM) with a pluggable LLM **vision** subsystem that
classifies a KVM screenshot into a boot/run phase. Early **alpha**, and **never
run on real hardware** (unit-tested with mocks only) — be honest about that in
any docs or messaging; do not claim features are "tested" or "beta".

## Non-negotiable conventions
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
- **Capabilities, not a monolith.** New device support = a driver implementing the
  relevant capability protocols in `src/kvm_pilot/drivers/base.py` (`Power`,
  `HID`, `Video`, `VirtualMedia`, `GPIO`, `Events`, `SystemInfo`). See
  `docs/architecture.md`.

## Layout
- `src/kvm_pilot/client.py` — `KVMClient`, the PiKVM/GLKVM REST client.
- `src/kvm_pilot/http.py` — stdlib HTTP transport (retry/backoff, secret redaction).
- `src/kvm_pilot/safety.py` — `SafetyPolicy`, `DESTRUCTIVE_OPS`.
- `src/kvm_pilot/drivers/base.py` — capability protocols (driver-plugin model).
- `src/kvm_pilot/vision/` — pluggable vision backends + `ScreenAnalyzer`.
- `src/kvm_pilot/{config,errors,cli}.py` — config resolution, exceptions, CLI.
- `tests/` — unit tests; HTTP + vision are mocked (`tests/conftest.py`).
- `docs/architecture.md` — driver-plugin design + diagram. `skill/SKILL.md` — the bundled Claude skill.

## Dev workflow (Python ≥ 3.11)
```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev,totp,ws]"
.venv/bin/ruff check .
.venv/bin/mypy src/kvm_pilot
.venv/bin/pytest
```
CI (`.github/workflows/ci.yml`) runs all three on Python 3.11/3.12/3.13 — keep
them green.

## Safety in tests & dev
Never point destructive operations at real hardware from tests or examples. The
suite mocks the transport; to exercise a destructive path use `dry_run=True` or a
`confirm` callback that returns `False`.

## Release
The version lives in `src/kvm_pilot/__about__.py`. Releases publish to PyPI via
GitHub Trusted Publishing (`.github/workflows/release.yml`, environment `pypi`)
on a published GitHub Release. The current `0.1.0a1` is a yanked, opt-in alpha.
