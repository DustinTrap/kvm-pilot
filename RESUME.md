# Resume — current working state

> The single doc the next work session reads first. Refreshed by `/checkpoint` at
> the end of every session (`.claude/skills/checkpoint/`). Standing project rules
> live in [CLAUDE.md](CLAUDE.md); this file is only **where we are right now**.

**Last updated:** 2026-07-08 · `feat/interface-benchmark-181 @ 1cecc78` · **v0.1.0a13** (published; this session's work is unreleased on the branch)

## Current state
- **In flight on a feature branch + PR #182** (`feat/interface-benchmark-181`) — the
  **adaptive interface router** (epic #181). The operator explicitly asked for a
  branch+PR this session, so this work is **not** on `main` and **not** released
  (a13 is the released line). Everything committed + pushed; full pytest + ruff +
  mypy green this session (known-good).
  - **Increment 1** (`7de1d4b`): `benchmark.py` scorecard profiler + `kvm-pilot
    benchmark` (per-command latency + capability per interface).
  - **Increment 2** (`a61de9e`): `router.py` — KVM-control vs OS-in-band **planes**,
    `select_interface()` (cheapest *capable*), state-change invalidation
    (`is_stale`/`stale_rows`); **SSH + WinRM interfaces** (`remote_ps.py` = WinRM as
    dep-free PowerShell-over-SSH); **persistent SSH** (`SSHChannel(persist=True)`,
    ControlMaster).
  - **#183** (`0a86502`): dep-free **target-SSH password auth** via SSH_ASKPASS
    (`ssh_password`/`KVM_PILOT_SSH_PASSWORD`; appliance channel stays key-only).
  - Docs (`1cecc78`): CHANGELOG [Unreleased], cli.md, configuration.md, decisions.md.
- **Earlier this session (landed via other channels / issues):** flashed **.39
  RM1PE V1.5.1 → V1.9.1** (web-UI is the only working path, #177); **proved snapshot
  is resolution-gated** (H.264 @2560×1440, JPEG @≤1024×768 via GL EDID); RFEs
  #178/#179/#180 (interface doctrine + benchmark + reassess-after-firmware).
- **Benchmark evidence:** library/MCP ≈ 0.18s ≪ CLI-default ~1.28s ≪≪ Chrome
  (3 devices); SSH persistent-vs-fresh **263→26ms (.11 key), 372→61ms (.18 pw)** ~6–10×.

## Next steps
- **Merge PR #182** — human call (checkpoint doesn't merge/release).
- **Router Increment 3** — wire `select_interface` into the CLI/MCP high-level intents
  (the "seamless" auto-selection); **Increment 4** — online learning (update the
  scorecard from every real call).
- **#184 — live in-band WinRM/SSH vs the real Windows target (BLOCKED on auth).**
  OpenSSH Server is **enabled + running on WHITESKELETON (10.0.1.19:22)**; we have the
  PIN but not the account password, and the agent was correctly blocked from planting
  its own key. Resume by: `dusti` password (→ SSH_ASKPASS) **or** user-installed key.
- Prior open items still stand: **#151** (H.264 keyframe decode, deferred), **#175**
  (docs-parity CI guard), path-to-`b1` (2nd hardware family + OOB power + HID verify).

## Device state left non-default (this tool mutates real hardware)
- **WHITESKELETON (10.0.1.19, the host on .39):** **OpenSSH Server enabled + running**
  (a standing remote-access change the operator approved for benchmarking; disable with
  `Stop-Service sshd; Set-Service sshd -StartupType Disabled`). An **admin PowerShell
  window is left open** on its desktop. Left **logged in**; display restored to native
  2560×1440.
- **.39 KVM: keep-awake / mouse-jiggler is ON** (re-armed after the firmware reboot).
  *(Supersedes the prior "keep-awake OFF on all three".)*
- **.11 / .20:** appliance-SSH keys still onboarded (config has `appliance_ssh=true` +
  key); otherwise at rest. **.39 firmware is now V1.9.1** (was V1.5.1).

## Fleet facts
- **.18 = host `server11`** (Linux x86_64), user `dtrapani`, **password auth only** (no
  key) — the host that exercised #183. **.11**'s managed host is also named `server11`
  (Fedora 44); appliance = `dell-02-kvm`, RV1126 aarch64. ATX unwired fleet-wide (no OOB
  power → `recovery-path` CRITICAL). loadavg is not a health signal on RV1126.

_Standing rules (issue-per-finding · normally direct commits to `main`, but this session
is an operator-requested branch/PR · stdlib-only at core import · `pip install` ships every
surface · docs↔shipped parity · run `healthcheck` first · never hand-edit the auto-generated
wiki): see [CLAUDE.md](CLAUDE.md)._
