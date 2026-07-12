# Hardware reliability test plan (fleet sweep)

A **reusable, written procedure** for exercising every `kvm-pilot` function
against real hardware, multiple times, in multiple device states, cross-checking
each result against **independent ground truth** to catch *false reports*. Run
the same procedure against any new device — other GL models, PiKVM, BliKVM,
Redfish BMCs, JetKVM — with minimal re-planning, and feed the result into the
support matrix.

> Tracked by [#172](https://github.com/DustinTrap/kvm-pilot/issues/172), under
> the support-matrix epic [#96](https://github.com/DustinTrap/kvm-pilot/issues/96).
> The output of a run feeds the driver-features page
> ([#171](https://github.com/DustinTrap/kvm-pilot/issues/171)). Related test
> infrastructure: [#16](https://github.com/DustinTrap/kvm-pilot/issues/16)
> (PiKVM kvmd testenv), [#17](https://github.com/DustinTrap/kvm-pilot/issues/17)
> (Redfish emulator), [#21](https://github.com/DustinTrap/kvm-pilot/issues/21)
> (emulator stack), [#29](https://github.com/DustinTrap/kvm-pilot/issues/29)
> (a real BMC).

> ⚠️ **Early alpha.** This plan describes how to *find out* what works — it is
> not a claim that anything does. Most device+capability combos are mock-only
> until a run recorded here promotes them. Be honest in every note: record what
> you saw, not what you expected.

---

## 0. Why a false-report hunt, not a smoke test

A function can **lie**. The failure mode this plan exists to catch is not "the
command errored" — it is "the command returned a confident answer that does not
match physical reality." Two proven examples on the current fleet:

- **`snapshot` 503s (or returns a tiny H.264 frame) while the video is genuinely
  fine.** Root cause is not the video pipeline — it is the display being
  **DPMS-asleep**, or the JPEG snapshot path flipping to H.264 at the panel's
  native high resolution (snapshot format = *resolution × encoder mode × cache
  state*). A caller that trusts the 503 concludes "no video" and escalates to a
  human when the box is perfectly visible over the WebRTC stream.
- **GLKVM ATX always reports `power = off`** even when the host is booted and
  running. So `power on` / `off` / `power-cycle` **cannot be confirmed via ATX**
  — the state read is a constant, not an observation.

The rule that falls out: **every function result is cross-checked against an
independent ground-truth channel** (§3). A result that agrees with ground truth
is evidence; a result that disagrees is a *finding* (file an issue). A result
that varies across identical repeats is also a finding (§6).

---

## 1. Scope & inventory — organize by capability

Test the whole product surface: **the CLI (29 subcommands)** and **the MCP server
(~24 tools)**. Organize the plan by **capability**, not by command name, so the
same plan maps onto any driver — a Redfish BMC has no `Video`, a GL unit has no
`Sensors`, and the capability grouping tells you which rows to skip vs. must-test
for the device in front of you. Capabilities are the `Capability` protocols in
`src/kvm_pilot/drivers/base.py`; the CLI names come from `cli.py`, the MCP names
from `src/kvm_pilot/mcp/README.md`.

| Capability | CLI subcommands | MCP tools | Notes / known landmines |
|---|---|---|---|
| **SystemInfo** | `info`, `capabilities` | `info`, `capabilities`, `support_matrix` | `capabilities` is offline/structural — no device call. |
| **Power** | `power` ⚡, `power-cycle` ⚡ | `power_state`, `power` ⚡ | **ATX read is unreliable on GLKVM (always off)** — verify state independently. |
| **HID** | `type` ⚡, `key` ⚡, `mouse-move` ⚡, `click` ⚡, `recover-hid`, `keep-awake` | `type_text` ⚡, `press_key` ⚡, `send_shortcut` ⚡, `ctrl_alt_delete` ⚡, `mouse` ⚡ | Keystrokes land on a live console. Mouse *click* must carry a fresh `observed_frame_ref`. `send_shortcut`/`ctrl_alt_delete` are gated **by effect** (reboot chord ⇒ power gate). |
| **Video** | `snapshot`, `classify`, `watch` | `snapshot`, `classify_screen`, `wait_for_state` | **The false-report epicenter.** `snapshot` 503/H.264 while video is fine; check `signal.hdmi_signal` + `unchanged_since_last_snapshot`. |
| **VirtualMedia** | `media-list`, `mount` ⚡, `eject` ⚡ | `list_virtual_media`, `mount_iso` ⚡, `eject` ⚡ | `mount` verifies the media actually reports online (#77); look for `host_visible_as` (#78) — its absence means not truly inserted. |
| **Sensors** | `sensors` | — | BMC drivers only; skip on GL/PiKVM. |
| **Logs** | `logs` | `logs` | The text channel that names a fault a screenshot can't (e.g. stuck encoder behind a 503). |
| **BootProgress** | `boot-progress` | — | BMC BootProgress; part of the vision/sensing hierarchy for phase. |
| **FirmwareUpdate** | `firmware-check`, `firmware-update` ⚡ | — (CLI only) | **Highest brick risk** — see §7. `firmware-update` plans by default; `--execute` flashes. Validate #94/#95 no-op detection. |
| **Events** | `events` | — (CLI only) | WebSocket stream; `websocket-client` is a base dep (bundled). |
| **RemoteShell / SSH** | `ssh-check`, `ssh-exec` ⚡, `ssh-discover`, `ssh-bootstrap` ⚡ | `ssh_reachable`, `ssh_exec` ⚡, `ssh_discover` | In-band to the **managed host** (behind the KVM), not the appliance. `ssh-discover` is an active scan — opt-in, your networks only. |
| **Appliance-SSH** (recovery) | `appliance` (loadavg/reboot ⚡), `paths` | `appliance_status`, `access_paths`, `appliance_reboot` ⚡ | SSH to the **KVM's own OS** (`root@<kvm-ip>`). **loadavg is NOT a health signal on RV1126** (self-inflates to ~10 idle). `appliance_reboot` is never automated. |
| **Health / intake** | `healthcheck` | `healthcheck` | The intake gate (§4, #80). Run **first, on first contact**, always. |

⚡ = destructive (gated). The CLI marks these with `⚡` in
[cli.md](cli.md); the MCP tools carry `destructiveHint` and an `ALLOW_*` gate.

---

## 2. The core principle — hunt for false reports

For every function, ask two questions, not one:

1. **Did it do what it said?** (success reported → the effect actually happened).
2. **Did it fail to do what it could?** (failure/empty reported → the thing
   actually works by another measure).

Both directions are bugs. A `snapshot` that 503s while WebRTC shows a clean
desktop is a **false negative**; a `power on` that returns success while the host
never POSTs is a **false positive**. Neither is visible without an independent
check. Record the verdict in the per-function table (§8) as one of:

- **TRUE** — result matched ground truth.
- **FALSE-POS** — reported success, reality was failure/no-op.
- **FALSE-NEG** — reported failure/empty, reality worked.
- **FLAKY** — varied across identical repeats (§6).
- **N/A** — capability not supported by this driver (expected clean error).

Every non-TRUE verdict gets a GitHub issue (issue-per-finding doctrine).

---

## 3. Ground-truth channels

An independent channel is one that does **not** share the failure mode of the
function under test. Pick the cheapest channel that is genuinely independent.

| Channel | How | Sees what the KVM API can't | Independence caveat |
|---|---|---|---|
| **Native REST cross-check** | The device's own API, hit directly (curl the vendor endpoint, e.g. GLKVM `/api/streamer/snapshot`, `/api/hid`, kvmd `/api/info`) — bypassing kvm-pilot. | Whether kvm-pilot is misreading a field the device reports correctly. | Rides the **same appliance**; dies with it. Confirms *kvm-pilot's* honesty, not the device's. |
| **Appliance-SSH** (`root@<kvm-ip>`) | `kvm-pilot appliance loadavg`, or raw SSH: `ps -eo stat,comm`, `uptime`, `dmesg`, `ss -tlnp`, framebuffer/encoder state, current resolution. | The **RV1126 encoder wedge** REST cannot see; process/D-state, listening ports, real resolution. | **loadavg is NOT a health signal here** — it self-inflates to ~10 idle (video kernel threads park in D-state). Key on *function* (does the frame decode?), never on load. Independent daemon on :22 — survives a wedged encoder. |
| **In-band SSH to the managed host OS** | `kvm-pilot ssh-check` / `ssh-exec` to the machine *behind* the KVM (its own `ssh_host`). `uptime`, `who`, `journalctl`, `cat /sys/class/drm/*/status`. | Ground truth for **power** (host is up ⇒ ATX "off" is a lie) and for boot/login phase independent of video. | Only exists once the host OS is up and networked — useless mid-POST or pre-install. |
| **Visual / vision** | The screenshot itself, read by a human or a vision model; or the live **WebRTC/Janus stream / vendor web UI** when `snapshot` fails. | Whether "no video" is real or a snapshot-path artifact (503/H.264-at-native-res). | The vision model can itself be wrong on ambiguous frames — pair with `signal.hdmi_signal` and multi-frame consensus (#166). |

**Rule of thumb per capability:**

- **Power** → in-band SSH to the host (up/down is unambiguous) **and** visual
  (POST/boot screen). Never trust ATX read alone on GL.
- **Video** → visual/WebRTC + `signal.hdmi_signal` + appliance-SSH resolution.
- **HID** → visual confirmation the keystroke/click landed (screen changed as
  expected), plus in-band SSH (`last`, a file the command created).
- **VirtualMedia** → `media-list` `host_visible_as` + the target's boot menu (visual).
- **Firmware** → `firmware-check` before/after + version read via native REST +
  the device actually rebooting into the new image (§7).
- **Appliance/recovery** → `paths` cross-checked against physically pulling a
  cable / observing the box.

---

## 4. Intake gate — `healthcheck` first, always

Bringing a device into the sweep runs through **`healthcheck` first**
([#80](https://github.com/DustinTrap/kvm-pilot/issues/80); see
[KVM intake doctrine]). It is the readiness/security/firmware audit, and its
`CRITICAL` findings **gate the destructive phase** below. `health.py` pillars:

- **Readiness** — `api-reachable`, `driver-identity`, `ssh-reachable`,
  `recovery-path`, `video-signal`, `hid-reachable`, `encoder-wedge`,
  `msd-online`, `capability-profile`, `support-evidence`.
- **Security** — `tls-posture`, `default-creds`, `exposed-services`.
- **Firmware** — `firmware-report`, `firmware-quirks`, `firmware-currency`.

Severity ladder: `OK < INFO < WARNING < CRITICAL`. A `CRITICAL` (classically
**no out-of-band recovery path** — `recovery-path`) means a hung guest cannot be
recovered remotely; acknowledge it before any destructive op, or don't run them.

Also capture the **lockout-exposure view** up front:

```bash
kvm-pilot paths --profile <name>          # which independent recovery domains are live
```

`access_paths` labels each path by failure **domain** (`kvmd-rest`,
`appliance-ssh`, `target-ssh`, `oob-power`, `console-hid`). The only truly
hardware-independent domain is **out-of-band power** — every in-band path dies
with the appliance. If `out_of_band_live` is `NONE`, the whole sweep — especially
firmware — is running without a net; note it loudly.

> ⚠️ The MCP server does **not** auto-run `healthcheck` on connect, and only the
> destructive CLI subcommands auto-gate on it (`cli.py` `_preflight_gate`). So
> when testing the MCP surface you must call `healthcheck` explicitly as step 1.

---

## 5. The state matrix — test each function in varied states

Most false reports are **state-dependent**. Testing a function once, on an idle
desktop, hides exactly the bugs this plan targets. Exercise each function across
these axes (skip rows the device can't reach):

| Axis | States to cover | Why it exposes false reports |
|---|---|---|
| **Host power** | on · off · mid-boot (POST · boot menu · bootloader/GRUB · OS login · desktop) | ATX read vs. reality; boot-phase classification; whether HID/media behave differently pre-OS. |
| **Display power** | awake · **DPMS-asleep** | The #126/#142 root cause: snapshot 503 while video is fine. Test snapshot with the panel asleep *and* awake. |
| **Resolution** | low/default · **native high-res** (where snapshot flips JPEG→H.264) · after a resolution change | Snapshot format = resolution × encoder × cache; native res is where the 503/tiny-frame appears. |
| **Session activity** | idle · active · jiggler/`keep-awake` ON vs OFF | `keep-awake` on prevents DPMS sleep — confirm it actually changes the snapshot outcome. |
| **Cache state** | fresh frame · repeated snapshot (check `unchanged_since_last_snapshot`) | Byte-identical pixels across an expected change = stale/cached, a false "current" frame. |
| **Media state** | no media · ISO mounted · ISO ejected | `list_virtual_media` `host_visible_as` presence/absence; boot-menu visibility. |

Cross the capability list (§1) with the relevant axes. Not every function needs
every axis — `info` is state-flat; `snapshot`/`classify`/`power`/HID are the
state-sensitive ones and deserve the full grid.

---

## 6. Repetition & reproduction methodology

- **N ≥ 3 per (function × state).** A single call proves nothing about
  reliability. Run each at least three times in the same state.
- **Variance is a finding.** If three identical calls give different answers
  (503, then JPEG, then 503), that inconsistency **is** the bug — record it as
  **FLAKY**, capture the varying outputs, and note what differed (timing? cache?
  a background jiggle?). The reliability damper (#164) and video-honesty work
  (#165) exist because of exactly this.
- **Reproduce before filing a fix.** Once you have a false report, find the
  minimal state that reproduces it (e.g. "always 503 within 10s of DPMS sleep at
  1920×1080"). A reproduction recipe belongs in the issue.
- **Record timing.** Note latency; a function that "works" but takes 40s is a
  different reliability story than one that returns in 1s (Redfish-latent
  #167–#170 are shaped like this).
- **Log the raw output**, not your summary. For `snapshot`, save the file and the
  JSON `signal` block; for `logs`, keep the text; for HID, keep the before/after
  screenshots.

---

## 7. Destructive & brick-aware safety

### 7.1 Gating (know what protects you)

- **CLI**: destructive subcommands (⚡) auto-run the preflight `healthcheck`,
  then prompt for confirmation. `--dry-run` logs instead of sending; `--yes`
  skips the prompt on a real run. The gated op set is `DESTRUCTIVE_OPS` in
  `safety.py` (power, media, HID input, `firmware.flash`, `ssh.exec`,
  `appliance.reboot`).
- **MCP**: each destructive tool needs (1) the operator to set the matching
  **effect-class env flag** in the *server's own* environment, and (2) a
  per-invocation approval (elicitation) or `confirm=true`:
  - `KVM_PILOT_MCP_ALLOW_POWER` → `power`, `ctrl_alt_delete`, reboot/SysRq chords
  - `KVM_PILOT_MCP_ALLOW_HID` → `type_text`, `press_key`, `mouse`, ordinary `send_shortcut`
  - `KVM_PILOT_MCP_ALLOW_MEDIA` → `mount_iso`, `eject`
  - `KVM_PILOT_MCP_ALLOW_SSH` → `ssh_exec`
  - `KVM_PILOT_MCP_ALLOW_APPLIANCE` → `appliance_reboot`
  - `KVM_PILOT_MCP_DRY_RUN=1` builds every driver with `dry_run=True`.

  Gating is **by effect, not transport**: Ctrl+Alt+Del is `power_soft` even over
  the HID keyboard, so it needs the *power* gate — an actuator can't launder a
  reboot through the weaker HID gate. Verify this holds while testing HID.

- **Always verify destructive RESULTS independently** (§3). Because ATX lies on
  GL, a `power on` that "succeeds" means nothing until in-band SSH or the screen
  confirms the host is actually up.

### 7.2 Firmware flash — the highest brick risk

`firmware-update` reboots the device into a new image, dropping this control
channel; a failed flash may need physical recovery, and **these units have no
out-of-band recovery path** (`recovery-path` CRITICAL). Procedure, safest first:

1. **Assess / dry-run.** `firmware-check` first, then `firmware-update` with no
   `--execute` — it plans only. Read the plan; confirm the target version and
   that the device reports its own current version.
2. **Confirm the recovery path exists** before flashing: the U-Boot failsafe /
   vendor recovery mode, and that you have physical access
   (`--i-have-physical-access` documents you accept the brick risk).
3. **Exercise the path with least risk first.** Flash-to-staged / same-version
   (idempotent) to walk the code path without changing firmware, and **validate
   the #94/#95 no-op detection** — the RM1PE remote flash is a known live no-op;
   confirm kvm-pilot reports "device did not enter an upgrade state" rather than
   a false success.
4. **Riskiest device last, one at a time.** Never flash two devices in parallel;
   never flash the device you'd need to recover the others.

> `firmware-update` / `firmware-check` and `events` are **CLI-only** — there is
> no MCP tool. Test them from the CLI.

---

## 8. Per-device procedure (run in this order)

Phase order is by ascending blast radius: observe everything before you touch
anything, and confirm recovery paths before the destructive phase.

### Phase A — Intake
1. `healthcheck` (`--json`) → record pillars, worst severity, any CRITICAL.
2. `paths` → record live recovery domains; flag `out_of_band_live == NONE`.
3. `capabilities` (offline) → the row list you will and won't test on this driver.
4. `firmware-check` → current vs. registry.

### Phase B — Read-only sweep (no state change)
5. `info`, `logs`, `power_state`, `sensors`/`boot-progress` (if supported),
   `media-list`, `ssh-check`, `appliance loadavg`.
6. `snapshot` / `classify` across the **full state grid** (§5) — this is the
   biggest false-report surface. Save frames + `signal` blocks; N ≥ 3 each.
7. Cross-check each against ground truth (§3); fill the table (§8 template below).

### Phase C — HID-safe actuation (low blast radius, reversible)
8. `mouse-move` (no click) — resolution-proof `percent` space; confirm the cursor
   moved on screen. `keep-awake on` then re-run snapshot in the DPMS-asleep case.
9. `type` / `key` into a **safe sink** (a text editor, a login field you'll
   clear) — confirm each keystroke landed visually and via in-band SSH. Then
   `recover-hid` if HID looked wedged.

### Phase D — Destructive (gated; verify results independently)
10. `mount` an ISO → verify `host_visible_as` + boot-menu visibility → `eject`.
11. `power` off/on/reset and `power-cycle` → **verify with in-band SSH + screen**,
    not ATX read. Cover the mid-boot states.
12. `ssh-exec` a harmless command (`uname -a`) on the managed host.
13. `appliance reboot` **only if** you can tolerate ~60s of lost KVM control and
    have confirmed recovery — never in a loop.

### Phase E — Firmware (§7) — last, one device at a time.

### Phase F — MCP parity
14. Re-run the equivalent MCP tools for every capability and confirm **parity**:
    the MCP result should match the CLI result for the same action in the same
    state. Test that gates behave (a destructive tool with its `ALLOW_*` flag
    unset is refused cleanly), that read-only tools run under deny-all confirm,
    and that a capability the driver lacks returns a clean tool error (not an
    `AttributeError`). Confirm every result names the `host`/`driver` it acted on.

### Phase G — Ledger
15. Record the run into the support-matrix ledger (§9).

---

## 8-bis. Per-function record template

Fill one row per **(function × state)** tested. This is the raw evidence a run
produces; the operator completes it live.

| Function | Capability | States tested | Reps | Ground-truth channel(s) | Result (raw) | Verdict | Notes / issue # |
|---|---|---|---|---|---|---|---|
| `snapshot` | Video | desktop, DPMS-asleep, native-res | 3+3+3 | WebRTC stream, `signal.hdmi_signal`, appliance-ssh res | JPEG / 503 / 503 | FLAKY | 503 only when asleep at native res → #NNN |
| `power on` | Power | host-off | 3 | in-band SSH (host up), screen (POST) | "success", ATX still `off` | FALSE-POS | ATX read constant on GL; #NNN |
| `info` | SystemInfo | any | 3 | native REST `/api/info` | matches | TRUE | |
| `mount <iso>` | VirtualMedia | no-media | 3 | `host_visible_as`, boot menu | online + visible | TRUE | |
| ... | ... | ... | ... | ... | ... | ... | ... |

Verdict ∈ {TRUE, FALSE-POS, FALSE-NEG, FLAKY, N/A} (§2). Every non-TRUE gets an
issue.

---

## 9. Per-device intake → support-matrix ledger

A device that passes intake gets recorded per **device + firmware + capability**
into the run ledger (`src/kvm_pilot/data/test_runs.jsonl`), which is the same
data behind the wiki
[Hardware-Compatibility list](https://github.com/DustinTrap/kvm-pilot/wiki/Hardware-Compatibility)
and the shipped support matrix
([#96](https://github.com/DustinTrap/kvm-pilot/issues/96) /
[#98](https://github.com/DustinTrap/kvm-pilot/issues/98) /
[#102](https://github.com/DustinTrap/kvm-pilot/issues/102)).

- **Only `source == "real"` runs count.** A synthetic/emulator run exercises the
  code path but proves nothing about the device — it never contributes a pass, a
  fail, or an "exercised" mark, and never promotes maturity.
- Each ledger record carries `run_id`, `vendor`, `product`, `firmware_version`,
  `driver`, `utc_date`, `source`, and a `capabilities: [{capability, passed,
  outcome}]` list. Record the observed reliability per capability from your table
  (§8), including the **false reports** (a false-pos on `power` is a `passed:
  false` on `power` with the outcome noted).
- A capability entry MAY additionally carry the **conditions it was observed
  under** ([#156](https://github.com/DustinTrap/kvm-pilot/issues/156)):
  `"conditions": {"resolution": "2560x1440", "encoder_format": "h264",
  "snapshot_cached": false, "jpeg_sink_clients": false}` — sourced from
  `video_signal_info()` + streamer state at test time. **Always record these on
  `snapshot` rows** (pass AND fail): the snapshot outcome on GL hardware is a
  function of resolution × encoder mode × cache/sink state, so a bare boolean
  makes two honest reports at different operating points read as a
  contradiction (the [#180](https://github.com/DustinTrap/kvm-pilot/issues/180)
  false-confidence incident). The field is optional and pre-#156 rows stay
  valid; maturity derivation ignores it.
- **Maturity is derived, never hand-set** (`maturity.py`): `alpha` (no live
  passes) → `beta` (≥1) → `rc` (≥3 across ≥2 dates) → `ga` (≥5 over ≥14 days, all
  after the last failure). A new failure resets the `ga` window. CI re-derives
  and fails on drift — so honest failures in the ledger correctly hold a device
  back from claiming maturity it hasn't earned.
- Anything in `never_exercised` (or a combo with no row) is **unverified** — keep
  saying so in docs and MCP results.

---

## 10. New-device onboarding checklist (generalize beyond GL)

The sections above are written around the current GL fleet, but the *method* is
device-agnostic. To point this plan at a new device kind — another GL model,
PiKVM, BliKVM, a Redfish BMC (iDRAC/iLO/XCC/OpenBMC), JetKVM — walk this list:

1. **Pick the driver.** `--driver pikvm | glkvm | blikvm | redfish | fake`. If
   none fits, a new device = a new driver implementing the relevant capability
   protocols (`drivers/base.py`); GL-specific quirks live in `glkvm.py`.
2. **Establish the capability set.** Run `capabilities` — this tells you which
   §1 rows apply. Redfish has `power`/`logs`/`sensors`/`boot-progress` but **no
   `video`** (so no `snapshot`/`classify`/`watch`); GL/PiKVM have video + HID +
   media but no `sensors`. Skip N/A rows; don't record a false FAIL for an
   unsupported capability — expect a clean capability error.
3. **Identify this device's ground-truth channels (§3).** What is its **native
   REST/API** (for a BMC: the Redfish tree itself; for GL: `/api/*`; for PiKVM:
   kvmd `/api/*`)? Does it expose **appliance-SSH**? Is **in-band SSH** to the
   managed host configured? Which visual channel (WebRTC, vendor UI, BMC HTML5
   console)?
4. **Re-derive this device's false-report suspects.** Don't assume GL's bugs.
   Ask: which reads are *constants* or *cached* (GL's ATX-always-off)? Which
   status endpoints are slow/latent (Redfish tends to be — see #167–#170)? Does
   its snapshot path change with resolution/encoder? Write the device's suspect
   list before sweeping.
5. **Re-check the health-signal assumptions.** `loadavg` is meaningless on the
   RV1126 — is it meaningful on *this* SoC? Does this device have a real
   out-of-band recovery path (a BMC often does; a GL unit does not)? Update the
   `recovery-path` expectation.
6. **Confirm the gating still holds.** Verify `DESTRUCTIVE_OPS` covers this
   driver's destructive ops (Redfish uses `redfish.power_*` /
   `redfish.virtual_media_*` ids) and that the MCP `ALLOW_*` gates refuse when
   unset.
7. **Run Phases A–G (§8)** across the state matrix (§5), N ≥ 3 (§6).
8. **Ledger + maturity (§9).** Record real runs; let maturity derive.
9. **Update the driver-features page** ([#171](https://github.com/DustinTrap/kvm-pilot/issues/171))
   with what this device+firmware actually did.

> **Redfish/BMC note:** drivers are built per call and closed afterwards because
> BMCs cap concurrent sessions device-side — a leaked session can lock operators
> out. Keep sweeps serial per BMC; watch for session exhaustion as its own
> failure mode. A real BMC to run this against is
> [#29](https://github.com/DustinTrap/kvm-pilot/issues/29); the emulators
> ([#17](https://github.com/DustinTrap/kvm-pilot/issues/17),
> [#21](https://github.com/DustinTrap/kvm-pilot/issues/21)) exercise the code
> path but are **not** real-hardware evidence.

---

## 11. Lessons learned (from live runs)

Append-only notes from actually running this sweep, so the next operator doesn't
re-learn them the hard way. First captured during the **2026-07-07 GL fleet
sweep** (`.11` RM1PE, `.20`, `.39`).

### Running the harness

- **Never run a daemon-invoking command in a ground-truth SSH probe.** `kvmd
  --version` **blocks indefinitely** on the RV1126 (kvmd is a daemon; `--version`
  does not fast-exit) and silently stalls the entire sweep — the log freezes
  mid-`gt` with no error. Read the version from files instead
  (`/etc/kvmd/version`, `/proc/gl-hw-info/model`). Keep every remote probe a
  fixed, fast, read-only command; when in doubt, test the exact `gt` block
  standalone with a hard timeout before wiring it into the loop.
- **Appliance SSH password auth needs `-o NumberOfPasswordPrompts=1`** (with
  `-o PreferredAuthentications=password -o PubkeyAuthentication=no -o
  ConnectTimeout=6`). Without it, when the server offers `publickey,password` and
  the key isn't accepted, the client hangs re-prompting against a closed stdin.
- **Wrap every per-device sweep in a watchdog** — macOS has no `timeout(1)`.
  `bash sweep.sh … & p=$!; (sleep 200; kill -9 $p)&`. A single hung remote command
  otherwise stalls a `wait`-joined parallel sweep forever, producing zero output
  (looks identical to "still running").
- **Parallelize across devices, never within one.** Two concurrent
  snapshots/healthchecks on the *same* unit perturb the very state you're
  measuring. One agent (or shell) owns a device end-to-end.
- **Test the code you think you're testing.** An editable install (`.venv`) runs
  current repo code but `pip show` reports a **stale metadata version** — trust
  `kvm-pilot --version` (reads `__about__.py`), not `pip show`. A separately
  installed CLI (`~/.local/bin`, pipx, PyPI) can lag the repo by several releases
  and expose a **different, smaller command set** (seen live: `.local/bin` a8 with
  22 subcommands vs repo a12 with 29). Pin the binary path explicitly in the sweep.
- **The appliance-SSH *feature* is key-only by design** (a separate trust domain
  from the kvmd REST credential), so exercising `appliance`/`access_paths`
  appliance-ssh liveness needs a one-time `ssh-copy-id root@<kvm-ip>`. An agent
  auto-mode classifier will (correctly) block that key install as unauthorized
  persistence — get explicit operator authorization or have the operator run it.
  For *ground-truth* verification (not the feature) a one-off password `sshpass`
  login installs nothing and is fine.

### RV1126 / GL ground-truth specifics

- **`loadavg` is not a health signal** — re-confirmed live: idle units with **0
  logged-in users** sit at **8–10** (video kernel threads park in D-state). Key on
  function (does a frame decode?), never on load. The `appliance loadavg` / MCP
  `appliance_status` output says this in its own note — believe it.
- **GL runs no `ustreamer` process.** Video is a custom Rockchip encoder
  (`mpp_rkvenc2` in `dmesg`) plus **janus** for WebRTC (`:7771`). "Is ustreamer
  alive?" is the wrong ground-truth question here — check janus/kvmd are up and
  `dmesg | grep -iE 'venc|rkvenc'` (normal churn includes
  `kmpp_venc_chan_put_frm: frame list is full` / `venc_release`), plus the kvmd
  streamer state via kvm-pilot's own `signal` block.
- **Appliance service map:** nginx (`:80`/`:443`) fronts kvmd (python3, `:8081`)
  and janus (`:7771`); SSH is **dropbear** (`:22`). A raw `curl localhost/api/…`
  301-redirects to https — hit the real path or go through kvm-pilot with auth.

### Classifying results

- **Separate "the function lied" from "the capability is absent."** On GL,
  `sensors` and `boot-progress` cleanly `CapabilityError` (both are BMC/Redfish
  capabilities), and `ssh-check` errors when the profile has no `ssh_host` — these
  are expected **N/A**, not failures. Confirm the capability set with
  `capabilities` first (§8 Phase A) so you don't file a false FAIL.
- **Docs don't auto-publish to the wiki.** `.github/scripts/build_wiki.py` uses an
  explicit `PAGES` allowlist, not a `docs/*.md` glob — a new page silently never
  syncs until it's registered there. (Both this plan and the driver-features page
  hit this.)

---

## 12. Performance benchmarking (where the time goes)

Reliability's sibling: **measure where time actually goes before optimizing.** The
bottleneck differs by interface *and* by device, so a guess is usually wrong — the
data below already refuted one "obvious" optimization.

### Repeatable harnesses
- **Interface scorecard** — `kvm-pilot benchmark --profile <p>` measures per-command
  latency + capability across every interface (library / ssh / winrm); `--save`
  persists it, `--select <cmd>` prints the router's pick, `route`/`host-exec` use it.
  This is the primary, first-class perf tool (#181).
- **Startup / import cost** — `python -X importtime -c "import kvm_pilot.cli"` (heaviest
  sub-imports) and `time kvm-pilot --help` (the cold-start floor).
- **Per-op profile** — `python -m cProfile -s cumtime -m kvm_pilot info --profile <p>`.
- **Connection-reuse A/B** — urllib (new connection per call, what the driver uses)
  vs a kept-open `http.client.HTTPSConnection`, to size the TLS-handshake share.

### Findings (2026-07-08 — GL fleet + real Linux hosts)
- **Interface ladder** (median /op): library ≈ MCP (~0.15–0.18 s) ≪ CLI-default
  (~1.28 s — the per-op preflight) ≪≪ Claude-in-Chrome (seconds + a full-frame JPEG).
- **Per-(device, command) variance is large** — `get_logs` 67 ms on `.11` vs **593 ms**
  on `.20`. Profile per device; don't assume uniform costs.
- **SSH is *setup*-bound → persistence is a 10× win.** `ssh_exec` 263 ms fresh → **26 ms**
  over a reused ControlMaster. **Implemented** (`SSHChannel(persist=True)`).
- **HTTP / the KVM API is *device*-bound, NOT handshake-bound.** Connection keep-alive
  is only **~1.1×** (167 → 152 ms) because the GL device's response (~150 ms) dwarfs the
  ~15 ms TLS handshake. So HTTP connection pooling is **not worth a transport rewrite**
  on these devices — *re-measure on a fast BMC (Redfish) before revisiting.* (This is the
  optimization the profiling refuted.)
- **CLI cold-start floor ~57 ms** — dominated by the stdlib HTTP import chain
  (urllib / ssl / cookiejar); acceptable, and amortized away by the persistent MCP server.
- **Per-op preflight (#80) adds ~1 s** — intentional intake for `info`/`snapshot`; skipped
  on the hot-path `benchmark`/`route`/`host-exec`.

### Optimization status
| Optimization | Status | Note |
|---|---|---|
| Persistent SSH (ControlMaster) | ✅ done | the real 10× win |
| Skip preflight on hot-path commands | ✅ done | reserve the ~1 s intake for first contact |
| Interface selection (router) | ✅ done | pick the cheapest *capable* interface per device/state |
| Prefer MCP / library over repeated CLI | ✅ doctrine | avoids ~57 ms startup + preflight per call |
| HTTP keep-alive / pooling | ⏸ deferred | 1.1× on GL (device-bound); re-measure on a fast BMC |
| CLI startup lazy-imports | ⏸ marginal | ~57 ms floor is mostly stdlib HTTP |

---

## Related

- [CLI reference](cli.md) · [MCP server](../src/kvm_pilot/mcp/README.md) —
  the exact command/tool surfaces and flags.
- [Architecture](architecture.md) — the capability-protocol design this plan
  organizes around.
- [Firmware registry](firmware-registry.md) · [Remote firmware update](firmware-update.md)
  — the firmware-currency model and the gated flash path (§7).
- [Design decisions](decisions.md) — the "looks wrong but is intentional" record
  behind several landmines above.

[KVM intake doctrine]: https://github.com/DustinTrap/kvm-pilot/issues/80
