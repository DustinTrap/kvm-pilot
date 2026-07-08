# Resume — current working state

> The single doc the next work session reads first. Refreshed by `/checkpoint` at
> the end of every session (`.claude/skills/checkpoint/`). Standing project rules
> live in [CLAUDE.md](CLAUDE.md); this file is only **where we are right now**.

**Last updated:** 2026-07-07 · `main @ 103c9dc` (+ checkpoint doc fixes) · **v0.1.0a13** (published to PyPI)

## Current state
- **Shipped v0.1.0a13 — the "headless-vision" release** (`pip install --pre kvm-pilot`).
  The headline: **GL's video encoder is on-demand** (runs only while a video client is
  connected), so a headless `snapshot`/`classify`/`watch` used to 503 forever on an idle
  unit. Fixed in **#142**: the GLKVM driver now registers a stream client over kvmd
  `/api/ws` to start the encoder (~1.5s), then snapshots; **`streamer_warm()`** keep-alive
  holds it warm (~0.1s/frame). `websocket-client` promoted to a **base** dep. Verified live
  returning real JPEGs on RM1PE V1.9.1 (.11/.20).
- Also this session: **#173** (snapshot-503 no longer misdiagnoses an idle streamer as a
  wedged encoder; healthcheck remediation fixed), **#174** (power on unwired ATX → clear
  `CapabilityError`, not opaque HTTP 500), the GLKVM quirk `snapshot-needs-video-client`,
  and docs: **driver-features** (#171) + reusable **test-plan** (#172) published to the wiki.
- Grounded in a **full live reliability sweep** of the GL fleet (.11/.20/.39) — umbrella
  findings + reliability matrix in **#176**. Sweep evidence recorded in the run ledger
  (`test_runs.jsonl`); maturity re-derived (V1.9.1 = beta, V1.5.1 = alpha; `virtual_media`
  now live-verified on all three; snapshot PASS on V1.9.1).
- Working tree clean, pushed, `CHANGELOG [Unreleased]` empty, CI + release workflow green.

## Next steps — all user-gated; nothing is in progress
- **#151 — H.264 keyframe decode** (deferred by operator). On V1.5.1 (.39) the streamer
  starts but `/api/streamer/snapshot` returns a lone **non-IDR P-frame** (no SPS/PPS/IDR) —
  undecodable standalone even by ffmpeg. Needs capturing a full keyframe from the h264
  stream + an **optional** ffmpeg/PyAV decode extra. Old-firmware-only; lower ROI than a
  firmware upgrade. ffmpeg 8.1.1 is on this machine.
- **#175 — docs parity** (open): add a CI guard so a new `docs/*.md` can't silently miss
  the `build_wiki.py` `PAGES` allowlist; add `access_paths` to the MCP README tool table.
- **Path to `b1`** (the operator asked; recommendation was to stay alpha): validate a
  **second hardware family** (a real PiKVM or a Redfish BMC — #29), and close the two
  fleet-wide gaps — **out-of-band power** (a networked smart plug lifts `recovery-path` off
  CRITICAL) and **HID-delivery verification** (no video/in-band SSH to confirm a keystroke
  landed). Do those and beta is defensible.
- **LATENT reliability #167–#170** — real in code, Redfish/ATX-shaped, unreproducible on
  the all-GL fleet; filed, not built.

## Device state left non-default (this tool mutates real hardware)
- **Appliance-SSH keys onboarded on .11 and .20** (my `~/.ssh/id_ed25519` in each unit's
  `/root/.ssh/authorized_keys`); `~/.config/kvm-pilot/config.toml` now has `appliance_ssh=true`
  + key for both. `appliance`/`access_paths` work live there. **.39 runs dropbear and rejected
  the key** — its appliance-SSH feature is unusable (password ground-truth still works).
- Otherwise **the fleet is at rest**: keep-awake/jiggler OFF on all three, the test ISO was
  ejected, no power or firmware state changed (the .39 firmware flash no-op'd, #94/#95).

## Fleet facts (verified this session)
- **.11 = host `server11`** (Fedora Linux 44 Server, at login prompt, 1600×900); appliance
  hostname `dell-02-kvm`. **loadavg is not a health signal** (idle load ~8.6 = the 8 D-state
  video threads). ATX is unwired on the whole fleet (no OOB power → `recovery-path` CRITICAL).

_Standing rules (issue-per-finding · direct commits to `main` · stdlib-only at core import ·
`pip install` ships every surface · docs↔shipped parity · run `healthcheck` first · never
hand-edit the auto-generated wiki): see [CLAUDE.md](CLAUDE.md)._
