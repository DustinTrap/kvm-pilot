# Contributing to kvm-pilot

Thanks for considering a contribution. This project aims to stay small, honest
about what it has and hasn't been tested against, and dependency-light.

## Dev setup

```bash
git clone https://github.com/DustinTrap/kvm-pilot
cd kvm-pilot
pip install -e ".[dev,totp,ws]"
```

## Before opening a PR

```bash
ruff check .          # lint (and `ruff format .` if you like)
mypy src/kvm_pilot    # types
pytest                # tests
```

CI runs all three on Python 3.11, 3.12, and 3.13. PRs should keep them green.

## Principles

- **Core stays stdlib-only.** Anything needing a third-party package goes behind
  an optional extra (see `totp` / `ws` in `pyproject.toml`), imported lazily.
- **No hard-coded model versions.** The vision backends resolve or accept a
  model at runtime; don't bake a version string into the code.
- **Destructive operations are gated.** If you add a method that can change a
  target's running state (power, reset, media, GPIO, resets), add it to
  `DESTRUCTIVE_OPS` and route it through `self.safety.guard(...)`.
- **Be honest about hardware.** If you've tested on real hardware, say which
  device and firmware in the PR. The compatibility table in the README should
  reflect what's actually been verified versus assumed.

## Hardware reports welcome

You don't need to write code to help. If you run `kvm-pilot` against a device
not in the compatibility table (PiKVM v3/v4, BliKVM, GL-RM1, etc.), open an
issue with what worked and what didn't — that's directly useful.

## Testing without hardware

The test suite mocks the HTTP and vision layers, so you can run and extend it
with no device. See `tests/conftest.py` for the fakes. For end-to-end transport
coverage, `tests/test_emulator.py` drives the real `KVMClient` against a
pure-stdlib fake kvmd (`tests/emulator.py`) on `127.0.0.1` — no Docker, runs on
macOS and Linux.

## Recommended Claude skills

If you use Claude Code, these skills help keep contributions consistent with the
project's standards (optional aids — `ruff`, `mypy`, and `pytest` remain the gates):

- **`/security-review`** — run before opening a PR. This project drives real
  hardware and handles credentials + secret redaction, so security review matters.
- **`/code-review`** — review your own diff for bugs and `CLAUDE.md` compliance.
- **`/claude-api`** — read before changing anything under `src/kvm_pilot/vision/`;
  it covers current Anthropic model ids and vision parameters.
- **`/find-skills`** — discover other useful skills in the ecosystem.
