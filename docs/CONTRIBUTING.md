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
ruff check .                                # lint (and `ruff format .` if you like)
mypy src/kvm_pilot                          # types
pytest                                      # tests
bandit -c pyproject.toml -r src/kvm_pilot   # SAST — CI gates on this
pip-audit                                   # dependency CVEs — CI gates on this
```

All of these are installed by the `[dev]` extra above; run `pip-audit` inside
the project venv so it scans the same environment CI does.

CI runs the lint/type/test trio on Python 3.11, 3.12, and 3.13, **plus** a
`security` job (`bandit` + `pip-audit`) and a `redfish-integration` job that
drives the Redfish CLI path end-to-end against the DMTF-conformant sushy-tools
emulator. PRs should keep them all green. If your change touches the Redfish
driver or CLI dispatch, run what CI runs:
`pip install "sushy-tools==2.2.0" && pytest tests/integration -m integration`
(without `sushy-emulator` on PATH those tests silently skip).

## Principles

- **Client/driver code stays stdlib-only at import time**, but `pip install
  kvm-pilot` ships everything a user needs — CLI, skill, and MCP server. A
  user-facing surface lives under `src/kvm_pilot/` and its runtime dep is a **base**
  dependency (`mcp` for the server); feature deps like `totp` / `ws` stay optional
  extras, imported lazily. Don't hide a user-facing surface behind an extra.
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
project's standards (optional aids — the CI checks above remain the gates):

- **`/security-review`** — run before opening a PR. This project drives real
  hardware and handles credentials + secret redaction, so security review matters.
- **`/code-review`** — review your own diff for bugs and `CLAUDE.md` compliance.
- **`/claude-api`** — read before changing anything under `src/kvm_pilot/vision/`;
  it covers current Anthropic model ids and vision parameters.
- **`/find-skills`** — discover other useful skills in the ecosystem.
