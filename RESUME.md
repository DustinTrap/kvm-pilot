# Resume — current working state

> The single doc the next work session reads first. Refreshed by `/checkpoint` at
> the end of every session (`.claude/skills/checkpoint/`). Standing project rules
> live in [CLAUDE.md](CLAUDE.md); this file is only **where we are right now**.

**Last updated:** 2026-07-07 · `main @ e61351d` · **v0.1.0a12** (published to PyPI)

## Current state
- **Shipped v0.1.0a12** — the "resilient-access release": reliability trio **#164**
  (consecutive-failure retry damper — fast-fail a wedged device), **#165**
  (video-signal honest "unconfirmed" on a null streamer), **#166** (multi-frame
  vision consensus), plus **#159–#162** (keep-awake, self-healing AutoFixes, the
  appliance-SSH channel + `kvm-pilot paths` lockout map + MCP appliance tools).
- Prior in the same session: **a11** honest-sensor (#154/#158/#141/#155).
- Working tree clean, nothing unpushed, `CHANGELOG [Unreleased]` empty.

## Next steps — all user-gated; nothing is in progress
- **Smart-plug OOB power** — the only truly power-independent recovery path;
  hardware-blocked (needs a networked plug per rig). Would flip `recovery-path`
  off CRITICAL in `kvm-pilot paths`.
- **RFE #163** — ProxyJump target-SSH *through* the appliance; feedback-gated
  (use case unclear), no build planned until validated.
- **LATENT reliability #167–#170** — real in code, but preconditions don't
  reproduce on the all-GL fleet (Redfish/ATX-shaped). Filed, not built; need
  Redfish/ATX hardware to reproduce before shipping.
- **Appliance-SSH live** — end-to-end needs a one-time `ssh-copy-id root@<kvm-ip>`
  (key onboarding); the channel is unit-tested, not yet run against real hardware.
- **#4** — the empty PyPI protection gate is still open (deprioritized by the operator).

## Device state left non-default (this tool mutates real hardware)
- **keep-awake / jiggler is ON on .20 and .11.** Clear with
  `kvm-pilot keep-awake off --profile homelab2` (and `homelab`) to return them to rest.
- server11 (the .11 target) and the .39 / .11 / .20 appliances were rebooted this session.

## Doctrine reinforced this session
- **Test assumptions on the real fleet before recommending.** A reliability survey's
  "top correctness landmines" turned out Redfish-shaped and unreproducible on the GL
  fleet; hardware testing reprioritized the work (and refuted two proposals outright).
- **loadavg is useless as a health signal on the RV1126** — it self-inflates to ~10
  even when idle (D-state kernel threads), so health checks key on function, not load.

_Standing rules (issue-per-finding · direct commits to `main` · stdlib-only at core
import · `pip install` ships every surface · docs↔shipped parity · run `healthcheck`
first · never hand-edit the auto-generated wiki): see [CLAUDE.md](CLAUDE.md)._
