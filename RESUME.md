# Resume — current working state

> The single doc the next work session reads first. Refreshed by `/checkpoint` at
> the end of every session (`.claude/skills/checkpoint/`). Standing project rules
> live in [CLAUDE.md](CLAUDE.md); this file is only **where we are right now**.

**Last updated:** 2026-07-08 · `main @ 303ed57` · **v0.1.0a14** (published to PyPI)

## Current state
- **Shipped v0.1.0a14 — the "interface-router" release** (`pip install --pre kvm-pilot`).
  An **adaptive interface router** picks the fastest interface that will actually
  produce the result, per device, benchmarked and self-tuning — across a KVM
  **control plane** (library/mcp/chrome) and an in-band **OS plane** (ssh/winrm).
  PR #182 merged to main; increments 3–4 + the perf plan landed direct-to-main after.
  New modules: `benchmark.py`, `router.py`, `remote_ps.py`. New CLI: `benchmark`
  (scorecard + `--save`), `route <cmd>` (seamless selection), `host-exec <cmd>`
  (in-band exec via the fastest ssh/winrm). Epic #181.
- **What the router knows:** per-(device,command) latency + capability (state-dependent:
  a GL snapshot is JPEG or H.264 by resolution/streamer; ssh only while the OS is up),
  cached per host (firmware-invalidated), self-tuning via `Scorecard.record()`.
- **New interfaces:** **SSH** + **WinRM/remote-PowerShell** (dep-free PowerShell-over-SSH);
  **persistent SSH** (ControlMaster, ~10×, #182); dep-free **SSH password auth**
  (SSH_ASKPASS, #183; `ssh_password`/`KVM_PILOT_SSH_PASSWORD`).
- **Perf plan (docs/test-plan.md §12):** measure-before-optimize. SSH is setup-bound
  → persistence is the 10× win (done). HTTP/KVM-API is **device-bound** → keep-alive is
  only ~1.1× on GL (the device's ~150ms response dwarfs the ~15ms handshake) → deferred
  (#185; re-measure on a fast BMC). CLI cold-start ~57ms; per-op preflight ~1s (intake,
  skipped on hot-path commands).
- Working tree clean, pushed, CI green on main; `CHANGELOG [Unreleased]` empty.

## Next steps
- **Router epic #181 remaining:** broaden intents beyond `route`/`host-exec`
  (read_screen / send_input as first-class), auto warm/EDID strategies for visual reads,
  and an MCP tool for routed selection/exec.
- **#184 — live in-band WinRM/SSH vs the real Windows target (BLOCKED on auth).**
  OpenSSH Server is **enabled + running on WHITESKELETON (10.0.1.19:22)**; we have the PIN
  not the account password, and the agent was correctly blocked from planting its own key.
  Resume by: `dusti` password (→ SSH_ASKPASS) or a user-installed key. Then benchmark
  in-band vs the KVM plane and feed the scorecard.
- **#185 — HTTP keep-alive**: re-measure on a fast BMC (Redfish) before implementing.
- Older open items: **#151** (H.264 keyframe decode, deferred), **#175** (docs-parity CI
  guard), path-to-`b1` (2nd hardware family + OOB power + HID verify).

## Device state left non-default (this tool mutates real hardware)
- **WHITESKELETON (10.0.1.19):** **OpenSSH Server enabled + running** (approved for the
  #184 benchmark; disable with `Stop-Service sshd; Set-Service sshd -StartupType Disabled`).
  An **admin PowerShell window is open** on its desktop. Left logged in; display 2560×1440.
- **.39 KVM: keep-awake / jiggler ON.** Scorecard cache written to
  `~/.config/kvm-pilot/scorecards/` (10.0.1.39.json, 10.0.1.18.json — the feature working).
- **.39 firmware is V1.9.1** (flashed from V1.5.1 this session). .11/.20 appliance-SSH keys
  still onboarded.

## Fleet facts
- **.18 = host `server11`** (Linux x86_64), user `dtrapani`, **password auth only** — the
  host that exercised #183 + the persistent-SSH numbers. `.11`'s managed host is also
  `server11` (Fedora 44); appliance `dell-02-kvm`, RV1126 aarch64. ATX unwired fleet-wide.

_Standing rules (issue-per-finding · direct commits to `main` — this session's #182 branch
was an operator-requested exception, since resolved · stdlib-only at core import · `pip
install` ships every surface · docs↔shipped parity · run `healthcheck` first · never
hand-edit the auto-generated wiki): see [CLAUDE.md](CLAUDE.md)._
