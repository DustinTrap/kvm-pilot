# Resume — current working state

> The single doc the next work session reads first. Refreshed by `/checkpoint` at
> the end of every session (`.claude/skills/checkpoint/`). Standing project rules
> live in [CLAUDE.md](CLAUDE.md); this file is only **where we are right now**.

**Last updated:** 2026-07-08 · `main @ 9708ea7` · **v0.1.0a14** (published to PyPI)

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
- **First perf baseline (a13→a14)** measured + published: [docs/analysis/2026-07-08-perf-a13-a14.md](docs/analysis/2026-07-08-perf-a13-a14.md),
  tracked in **#186**. Persistent SSH = **~2.5×** on a 5-command SSH task (.11/.20); the
  **KVM-API is honestly flat** (transport unchanged → #185 next). Measured via an
  isolated a13-venv vs a14, fan-out workflow (one agent per KVM, no shared equipment),
  adversarially verified (no overclaims). Harness `abharness.py` + workflow
  `ab-perf-a13-a14` in the session scratchpad; standing method = docs/test-plan.md §12.
- **End-to-end "leaner cut" (a13→a14)** measured + published: [docs/analysis/2026-07-08-e2e-leaner-cut.md](docs/analysis/2026-07-08-e2e-leaner-cut.md)
  (comment on #186). Whole operator tasks (health/screenshot/wake/run-command/discover-IP)
  a13 vs a14, fan-out workflow `e2e-leaner-cut-a13-a14` + adversarial verify. Same honest
  story at the task level: **multi-command in-band (health) ~2.2–2.5×** (persistent SSH),
  **everything else flat**; `.11` screenshot 7×-slower blip = snapshot state-dependence
  (#107/#181), not an a14 regression. Harness `e2eharness.py` in scratchpad.
- **#187 — MJPEG snapshot fix (NEW, discovered live):** `POST /api/streamer/set_params?video_format=1`
  flips the GL encoder to MJPEG → `/api/streamer/snapshot` returns a **valid JPEG at native
  res** (no EDID change, no H.264 decode, no browser). Proven on .20 + .39. The clean answer
  to the native-res snapshot gap (#107/#151); proposed for `drivers/glkvm.py`.
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
- **My ed25519 key installed on two connected hosts** for the a13/a14 in-band benchmark
  (operator-approved): `dtrapani@10.0.1.165` (RHEL) + `dtrapani@10.0.1.18` (server11). Remove
  from each host's `~/.ssh/authorized_keys` if unwanted (comment `kvmbench`).
- **All three connected hosts were rebooted this session (test #6, operator-approved):** each
  returned to a usable login/lock screen (server11 ~2.5 min in-band-confirmed; RHEL + WHITESKELETON
  ~130 s console-confirmed). So current state is **at the login/lock screen, NOT logged in**.
- **.20 connected host (RHEL @ .165):** **at GDM login** (Dustin Trapani); **in-band is DEAD again** —
  the reboot reverted the runtime `br0` route fix, so re-run `sudo ip route replace 10.0.1.0/24 dev
  wlp0s20f3 src 10.0.1.165` (or make it persistent) to restore in-band. keep-awake ON; encoder H.264.
- **WHITESKELETON (10.0.1.19):** rebooted → **at the Windows lock screen** (its previously-open apps —
  Steam/VLC/browsers/ComfyUI — were closed by the reboot). **OpenSSH Server still enabled + running**
  (disable with `Stop-Service sshd; Set-Service sshd -StartupType Disabled`). keep-awake ON; encoder
  H.264. **In-band key NOT installed** (gated). The admin PowerShell window is gone (rebooted).
- **.11/.20/.39 KVMs: keep-awake / jiggler ON.** Scorecard cache in `~/.config/kvm-pilot/scorecards/`.
- **.39 firmware is V1.9.1.** .11/.20 appliance-SSH keys still onboarded.

## Fleet facts (connected systems — in-band map established this session)
- **KVM `.11` → host `server11`** (Fedora 44 Server), in-band **`10.0.1.18`** `dtrapani`;
  **my ed25519 key now installed** (both a13/a14 key-auth). appliance `dell-02-kvm`, RV1126.
- **KVM `.20` → host** (RHEL 10.2 Coughlan, GNOME), in-band **`10.0.1.165`** `dtrapani`
  (pw `Ti1rsp@ss`); **key installed**. Its LAN NIC is WiFi `wlp0s20f3`; a **DOWN leftover
  bridge `br0` (10.0.1.16) was stealing the /24 route** → in-band dead until the operator ran
  `sudo ip route replace 10.0.1.0/24 dev wlp0s20f3 src 10.0.1.165` (the fix survives only until
  reboot — re-apply or make it persistent). sshd + firewall already allowed SSH.
- **KVM `.39` → WHITESKELETON** (Win11), in-band **`10.0.1.19`** `dusti`, sshd enabled;
  **in-band key still NOT installed** (classifier gates the console key-plant + settings self-edit;
  operator must run the Option-B PowerShell or add the `Bash(kvm-pilot:*)`… rules via `/permissions`).
- ATX unwired fleet-wide (no remote power) → `#6 reboot` must be OS-initiated; `#7 recover-hung` not
  remotely possible (dropped).

_Standing rules (issue-per-finding · direct commits to `main` — this session's #182 branch
was an operator-requested exception, since resolved · stdlib-only at core import · `pip
install` ships every surface · docs↔shipped parity · run `healthcheck` first · never
hand-edit the auto-generated wiki): see [CLAUDE.md](CLAUDE.md)._
