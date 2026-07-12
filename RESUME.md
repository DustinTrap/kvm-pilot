# Resume — current working state

> The single doc the next work session reads first. Refreshed by `/checkpoint` at
> the end of every session (`.claude/skills/checkpoint/`). Standing project rules
> live in [CLAUDE.md](CLAUDE.md); this file is only **where we are right now**.

**Last updated:** 2026-07-12 · `main @ 18e9d89` · **v0.1.0b1** on PyPI (nothing unreleased yet; `[Unreleased]` has the batch below)

## Current state
- **v0.1.0b1 — the FIRST BETA — is on PyPI** (2026-07-11 session: telemetry-loop
  fixes #188/#189, ingest-error parking, default-on auto-file; docs/PyPI page
  rewritten for the broader-testing call).
- **2026-07-12 session: shipped the "honest eyes at native res" batch** — 7
  commits direct to main (b9e9aff…18e9d89), CI + wiki-sync + firmware-ingest all
  green, 9 issues closed (#175 #156 #180 #187 #161 #190 + hygiene #151 #126 #144):
  - **#187 — GLKVM MJPEG snapshot flip** (`drivers/glkvm.py`): on
    `SnapshotFormatError` (H.264 at native res, #107) the driver flips the
    encoder (`POST /api/streamer/set_params?video_format=1`), retries, restores;
    held for the whole `streamer_warm()` block. Composes with the #142 offline
    recovery. Gated on the firmware exposing `video_format` (V1.9.1+; V1.5.1
    keeps the honest error → web-UI upgrade #177). New quirk
    `snapshot-h264-at-native-res`. **Mechanism live-proven on .20/.39; the shipped
    auto-path is emulator-verified only — first live run still pending.**
  - **#180 — firmware delta ⇒ reassess**: `preflight` persists last-assessed
    firmware (`assessed:{driver}@{host}` in the health cache), forces a live
    stable-check re-run on change, and emits a `firmware-delta` cleared/regressed
    diff. Evidence labeled **recorded vs tested-now**; condition-blind snapshot
    passes get an explicit caveat. Maturity ladder unchanged (docs/decisions.md).
  - **#156 — ledger conditions**: capability rows MAY carry
    `conditions {resolution, encoder_format, snapshot_cached, jpeg_sink_clients}`
    (schema in docs/test-plan.md §9); rollup surfaces `pass_conditions`/
    `fail_conditions`; maturity provably ignores it. **No live rows recorded yet.**
  - **#190 — MCP `file_firmware_report`** behind a new `EXTERNAL_WRITE` effect
    class (`KVM_PILOT_MCP_ALLOW_EXTERNAL_WRITE`, off by default); shared helpers
    `firmware_registry.check_currency()` + `file_firmware_report()` serve CLI and
    MCP; `gate_enabled` now **fails closed** for unmapped effects.
  - **#175 — docs-parity CI guard** (`build_wiki.py --check` in CI) + the missing
    `access_paths` README row. **#161** was already implemented — wiring tests
    added, closed.
- Post-batch `/simplify` pass applied (18e9d89): shared currency front half,
  single flip site, zero-write warm preflight, open condition schema rendering.
- Working tree clean, pushed, CI green on main.

## Next steps
- **Live-verify the #187 auto-path on the fleet** (.39/.20 at native res,
  headless): watch the #107 encoder-wedge under MJPEG-at-native (healthcheck
  `encoder-wedge`), and **record the first #156 conditions-bearing ledger rows**
  (e.g. `snapshot pass @ 2560x1440 mjpeg`). Also live-fire the #180
  firmware-delta path if any unit gets flashed.
- **Router epic #181 remaining:** broaden intents beyond `route`/`host-exec`
  (read_screen / send_input as first-class), auto warm/EDID strategies for
  visual reads, an MCP tool for routed selection/exec. The #187 flip should
  become a router strategy (snapshot capability is encoder-state-dependent).
- **#184 — live in-band WinRM/SSH vs WHITESKELETON (BLOCKED on auth)**: resume
  with the `dusti` password (SSH_ASKPASS) or a user-installed key.
- **#185 — HTTP keep-alive**: re-measure on a fast BMC before implementing.
- Next batch candidates: #183 (password-only targets for ssh/winrm interfaces),
  #157 (EDID/capture-resolution control), #152 (per-KVM-type operational skill),
  #148 (.mcpb bundle), #172/#171 (docs).

## Device state left non-default (this tool mutates real hardware)
*(unchanged since 2026-07-08 — no hardware was touched in the 07-11 or 07-12 sessions)*
- **My ed25519 key installed on two connected hosts** (operator-approved):
  `dtrapani@10.0.1.165` (RHEL) + `dtrapani@10.0.1.18` (server11). Remove from
  `~/.ssh/authorized_keys` if unwanted (comment `kvmbench`).
- **All three connected hosts are at the login/lock screen** (rebooted 07-08,
  operator-approved), NOT logged in.
- **.20 connected host (RHEL @ .165):** at GDM login; **in-band DEAD** — the
  reboot reverted the runtime `br0` route fix; re-run
  `sudo ip route replace 10.0.1.0/24 dev wlp0s20f3 src 10.0.1.165` to restore.
- **WHITESKELETON (10.0.1.19):** at the Windows lock screen; OpenSSH Server
  still enabled + running; in-band key NOT installed (gated).
- **.11/.20/.39 KVMs: keep-awake / jiggler ON.** Scorecard cache in
  `~/.config/kvm-pilot/scorecards/`.
- **.39 firmware is V1.9.1.** .11/.20 appliance-SSH keys still onboarded.

## Fleet facts (connected systems)
- **KVM `.11` → host `server11`** (Fedora 44), in-band **`10.0.1.18`** `dtrapani`
  (key installed). appliance `dell-02-kvm`, RV1126.
- **KVM `.20` → host** (RHEL 10.2, GNOME), in-band **`10.0.1.165`** `dtrapani`
  (key installed; WiFi NIC `wlp0s20f3`; the DOWN `br0` steals the /24 route
  after every reboot — re-apply the route fix or make it persistent).
- **KVM `.39` → WHITESKELETON** (Win11), in-band **`10.0.1.19`** `dusti`
  (sshd on; key NOT installed — operator must plant it or supply the password).
- ATX unwired fleet-wide (no remote power) → reboots must be OS-initiated;
  remote recovery of a hung host is not possible.

_Standing rules (issue-per-finding · direct commits to `main` · stdlib-only at
core import · `pip install` ships every surface · docs↔shipped parity · run
`healthcheck` first · never hand-edit the auto-generated wiki): see
[CLAUDE.md](CLAUDE.md)._
