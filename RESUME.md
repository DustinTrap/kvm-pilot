# Resume — current working state

> The single doc the next work session reads first. Refreshed by `/checkpoint` at
> the end of every session (`.claude/skills/checkpoint/`). Standing project rules
> live in [CLAUDE.md](CLAUDE.md); this file is only **where we are right now**.

**Last updated:** 2026-07-12 · `main @ ab61733` · **v0.1.0b1** on PyPI (a large `[Unreleased]` has accumulated — two full batches since b1; consider cutting **b2** next session)

## Current state
Two no-hardware batches shipped today, **18 issues closed**, CI green throughout.

- **Batch 1 — "honest eyes at native res"** (b9e9aff…0bc9580, 9 closed:
  #175 #156 #180 #187 #161 #190 + #151/#126/#144): GLKVM **MJPEG snapshot flip**
  (#187 — JPEG at native res via `set_params?video_format=1`, held across
  `streamer_warm()`, V1.5.1 gated out), **firmware-delta ⇒ forced reassessment**
  + recorded-vs-tested-now evidence (#180), ledger **`conditions` axes** (#156),
  MCP **`file_firmware_report`** behind the new `EXTERNAL_WRITE` gate (#190),
  docs-parity CI guard (#175).
- **Batch 2 — "safety-critical reliability + evidence harness"**
  (f565146…ab61733, 9 closed: #167 #170 #169 #168 #149 #72 #177 #103 #99):
  - **Transports** (#167/#170): 409/503 retry method-gated (a destructive POST is
    never re-fired; #164 breaker semantics preserved); typed `ProtocolError`
    instead of raw-bytes leak on JSON decode failure.
  - **Redfish** (#169): `mount_iso` verifies `Inserted=true` (MediaOfflineError
    on silent no-op); 401 re-auth `logout()`s the old session first (slot leak).
  - **MCP power** (#168): returns `{verified, observed, note}` + generation bump;
    honest `verified: null` on GL (ATX lies — quirk relayed from the driver).
  - **Approval layer** (#149+#72): typed `outcome` field; ELICIT=off hint only
    after ≥2 consecutive client-side kills; **single-use HMAC-signed expiring
    receipts** verified at all 3 dispatch sites (`KVM_PILOT_MCP_RECEIPT_TTL`,
    default 60s) + JSON audit records on `kvm_pilot.mcp.audit` (log lines only,
    operator's choice). Follow-up filed: **#192** duration-scoped standing approvals.
  - **`kvm-pilot test-report`** (#99): the first ledger writer — read-only probes
    auto (snapshot rows always carry #156 conditions), destructive via
    `--include` + `--attest` through the normal safety gates, pass = assertion +
    observed effect, honest FAILs. Ledger target `--ledger` >
    `$KVM_PILOT_TEST_LEDGER` > `~/.config/kvm-pilot/test_runs.jsonl` (never the
    installed package data).
  - Riders: #177 quirk `firmware-flash-webui-only` + upgrade-path docs;
    #103 maturity column on the generated wiki Hardware-Compatibility page.
- Both batches got a `/simplify` pass; working tree clean, pushed, CI green.

## Next steps
- **LIVE FLEET SESSION (the big owed item)** — everything shipped today is
  unit/emulator-verified only (user's explicit choice). One command now does the
  intake: **`kvm-pilot test-report --profile <p>`** on `.39`/`.20`/`.11`:
  - validates the **#187 MJPEG auto-flip** headless at native res (watch the
    #107 encoder wedge under MJPEG-at-native via the `encoder-wedge` finding),
  - records the **first conditions-bearing ledger rows** (#156) — e.g.
    `snapshot pass @ 2560x1440 mjpeg`,
  - exercises the **#180 firmware-delta** path if any unit gets flashed,
  - then regenerate maturity (`kvm_pilot.maturity --write`) and PR the rows.
- **Consider release v0.1.0b2** — `[Unreleased]` now carries 2 batches (18 issues).
- **Router epic #181 remaining**: read_screen/send_input intents, warm/EDID
  strategies (the #187 flip should become a router strategy), MCP routed-exec tool.
- **#184** (blocked on WHITESKELETON auth), **#185** (needs fast BMC), **#192**
  (standing approvals, new), **#100/#101** (telemetry pipeline remainder — #100
  partially superseded by #189/#190, worth re-scoping), **#96** epic remainder.
- Other open: #183 (password-only targets in router interfaces), #157 (EDID
  control gap), #152 (per-KVM-type skill), #148 (.mcpb bundle), #172/#171 (docs),
  #163 (ProxyJump RFC), #123-#117 (Reflexes, post-GA), #64/#62/#29/#28 (drivers),
  #21-#13 (test infra: ipmi_sim, QEMU vision harness — all hardware-free builds).

## Device state left non-default (this tool mutates real hardware)
*(unchanged since 2026-07-08 — no hardware touched on 07-11 or 07-12)*
- **ed25519 key installed on two connected hosts** (operator-approved):
  `dtrapani@10.0.1.165` (RHEL) + `dtrapani@10.0.1.18` (server11); comment `kvmbench`.
- **All three connected hosts at the login/lock screen** (rebooted 07-08).
- **.20 host (RHEL @ .165): in-band DEAD** — re-run
  `sudo ip route replace 10.0.1.0/24 dev wlp0s20f3 src 10.0.1.165` after reboot.
- **WHITESKELETON (10.0.1.19):** Windows lock screen; OpenSSH on; key NOT installed.
- **.11/.20/.39 KVMs: keep-awake / jiggler ON.** Scorecards in
  `~/.config/kvm-pilot/scorecards/`. **.39 = V1.9.1.**

## Fleet facts (connected systems)
- **KVM `.11` → `server11`** (Fedora 44), in-band `10.0.1.18` `dtrapani` (key ok).
- **KVM `.20` → RHEL 10.2 host**, in-band `10.0.1.165` `dtrapani` (key ok; the
  DOWN `br0` steals the /24 route after every reboot — re-apply the fix).
- **KVM `.39` → WHITESKELETON** (Win11), in-band `10.0.1.19` `dusti` (no key).
- ATX unwired fleet-wide → reboots must be OS-initiated; no remote recovery of a
  hung host.

_Standing rules (issue-per-finding · direct commits to `main` · stdlib-only at
core import · `pip install` ships every surface · docs↔shipped parity · run
`healthcheck` first · never hand-edit the auto-generated wiki): see
[CLAUDE.md](CLAUDE.md)._
