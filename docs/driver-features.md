# Per-driver feature list: capability, reliability & testing level

> Tracking issue: [#171](https://github.com/DustinTrap/kvm-pilot/issues/171).
> Related: [#96](https://github.com/DustinTrap/kvm-pilot/issues/96) (support-matrix
> epic), [#102](https://github.com/DustinTrap/kvm-pilot/issues/102) /
> [#103](https://github.com/DustinTrap/kvm-pilot/issues/103) (the shipped run
> ledger + its docs), and [#152](https://github.com/DustinTrap/kvm-pilot/issues/152).

This page lists, **for every driver**, the complete set of capabilities it can
expose and — per capability — how reliably it behaves on real hardware and how
far it has actually been tested. It exists so an operator or agent can answer
"can this driver do X, and can I trust the result?" without reading the source.

> **Early alpha — read this before you trust a rating.** *Structural* support
> (the driver implements a protocol) is **not** the same as *verified* support.
> Most device+capability combinations here are mock- or emulator-only. The
> **single source of truth** for what has actually been exercised on real
> hardware is the run ledger (`src/kvm_pilot/data/test_runs.jsonl`, surfaced by
> `kvm-pilot capabilities` and the MCP `support_matrix` tool) and the community
> [Hardware-Compatibility](https://github.com/DustinTrap/kvm-pilot/wiki/Hardware-Compatibility)
> wiki page. Where this page and the ledger disagree, **the ledger wins** — the
> tables below are refreshed by hand and can lag.

## Three axes, kept separate

A feature is described along three independent axes. Conflating them is exactly
the mistake that leads to over-claiming maturity.

1. **Structural capability** — does the driver implement the capability protocol
   in [`drivers/base.py`](../src/kvm_pilot/drivers/base.py)? Detected
   structurally (`isinstance` against a `@runtime_checkable` Protocol), never
   hand-declared. `kvm-pilot capabilities --driver <kind>` prints this set
   offline.
2. **Reliability** — when it *is* implemented, does it work **consistently** on
   real hardware, and — critically — does it ever **report success while lying**?
   Several devices return a value that looks fine but isn't (ATX power says
   "off" on a running host; an old GL snapshot returns H.264 bytes mislabeled
   as JPEG; a firmware flash returns 200 and no-ops). Reliability captures those
   false-report modes, which are more dangerous than an honest error.
3. **Testing level** — how far has the code path actually been exercised? Unit
   test with the transport mocked? Against a stdlib emulator in CI? Or on a real
   device with an entry in the run ledger?

## Legends

### Reliability

| Rating | Meaning |
|---|---|
| **reliable** | Verified to work consistently on real hardware, with no known false-report mode. |
| **conditional** | Works, but only under documented conditions or with a known caveat (a firmware floor, a resolution ceiling, the REST API must be enabled, the display must be awake). |
| **false-report** | The call can *appear* to succeed while the result is untrustworthy — you must confirm out-of-band. The most dangerous rating; each one is spelled out in Notes. |
| **unverified** | Structurally implemented but never confirmed on real hardware (mock/emulator only). No claim either way. |
| **n/a** | Not implemented by this driver. |

### Testing level

| Level | Meaning |
|---|---|
| **mock** | Unit tests only, transport mocked (`tests/conftest.py`). Exercises the code, touches no device. |
| **emulator** | Exercised over the **real transport** against a pure-stdlib fake server in CI: `tests/emulator.py` (kvmd) for the PiKVM family, `tests/redfish_emulator.py` and the external DMTF **sushy-tools** for Redfish. Proves the path is wired correctly; proves nothing about a physical device. |
| **live:`<vendor> <product>@<fw>`** | Confirmed on real hardware, with a row in `test_runs.jsonl`. Annotated with the **derived maturity** (below) where the ledger provides one, e.g. `live:gl.inet RM1PE@V1.9.1 (beta)`. |
| **n/a** | Not applicable (capability not implemented). |

### Derived maturity (issue #98)

On top of live evidence, `kvm_pilot.maturity` promotes each
`(vendor, product, firmware)` × capability along a ladder — **derived from the
ledger, never hand-set** (CI fails on drift):

| Level | Rule (per capability, from its live pass history) |
|---|---|
| `alpha` | 0 live passes (mocks only, or live failures only) |
| `beta` | ≥ 1 live pass |
| `rc` | ≥ 3 live passes across ≥ 2 distinct UTC dates |
| `ga` | ≥ 5 live passes spanning ≥ 14 days, all after the most recent live failure |

See [firmware-registry.md](firmware-registry.md#maturity-derived-from-the-run-ledger-98).

### The capability vocabulary

Every driver is scored against the full `Capability` enum, so "not supported"
is stated explicitly rather than left blank.

| Capability | Protocol | What it is |
|---|---|---|
| `system_info` | `SystemInfo` | Read device/host identity + state. |
| `power` | `Power` | Read and change host power state. |
| `hid` | `HID` | Emulated keyboard + mouse. |
| `video` | `Video` | Still-frame capture (feeds the vision layer). |
| `virtual_media` | `VirtualMedia` | Attach/detach an ISO or USB image. |
| `gpio` | `GPIO` | Drive relays / power buttons / LEDs. |
| `events` | `Events` | Stream async device events. |
| `logs` | `Logs` | Device/host event log (`seek` = seconds of lookback). |
| `boot_progress` | `BootProgress` | Host POST/boot phase as a structured token (no screenshot). |
| `sensors` | `Sensors` | Structured temps / fans / voltages / watts. |
| `serial_console` | `SerialConsole` | Read/write the host serial console as text (SOL). |
| `watchdog` | `Watchdog` | Arm/pet/inspect a hardware watchdog (IPMI). |
| `firmware_update` | `FirmwareUpdate` | Flash the **KVM/BMC's own** firmware over the network. |
| `ssh` | `RemoteShell` | In-band control of the managed **host's** OS over SSH. **Not a driver capability** — a per-profile channel (see note in each table). |

---

## `glkvm` — GL.iNet fork (GL-RM1 "Comet" / GL-RM1PE "Comet PE")

The **primary live-tested target.** GLKVM speaks the kvmd REST API
(`PiKVMDriver` is the base) but diverges enough to own its module
([`drivers/glkvm.py`](../src/kvm_pilot/drivers/glkvm.py), #140). Two GL-RM1PE
firmware lines have real ledger runs: **V1.5.1 release2** (kvmd 4.82) and
**V1.9.1 release1**.

**Driver-wide preconditions and quirks** (from `GLKVM_QUIRKS`; `source` shown):

- **REST API disabled by default** (`api-disabled-by-default`, *documented*):
  every `/api/*` 404s until enabled in `/etc/kvmd/nginx-kvmd.conf` and kvmd is
  restarted — and a firmware update can silently revert it. The driver turns the
  bare 404 into an actionable `ApiDisabledError`. This gates **all** capabilities
  below, so each is at best `conditional` on the API being enabled.
- **Masquerades as a Raspberry Pi PiKVM.** `/api/info` self-reports
  `type: rpi, board: rpi4` even on Rockchip RV1126 hardware; `get_firmware_info`
  reads the real product version from `/api/upgrade/version` instead.
- **ATX sensing is not wired** (`atx-power-state-always-off`, *observed* on kvmd
  4.82): `/api/atx` reports `power=off`, `enabled=false`, LEDs false even on a
  fully booted host. Registry `profile.power_state_trusted = false`.

Structural set: `system_info, power, hid, video, virtual_media, gpio, events, logs, firmware_update`.

| Capability | CLI / MCP surface | Reliability | Testing level | Notes / quirks |
|---|---|---|---|---|
| `system_info` | `info` · MCP `info` | conditional | live:gl.inet RM1PE@V1.5.1 (beta), @V1.9.1 (beta) | Fields present and correct, but the raw identity **lies** (rpi/rpi4 on RV1126); trust `get_firmware_info` (reads `/api/upgrade/version`). Dual version numbers (GL product vs kvmd component). |
| `power` | `power`, `power-cycle` · MCP `power`, `power_state` | **false-report** | read: live (untrusted); actuate: never (ATX unwired) | ATX **always reports off / enabled=false / LEDs false** on RM1PE even when running — `is_powered_on()` and the *result* of on/off/cycle **cannot be confirmed via ATX**. Verify visually (snapshot/vision). On unwired ATX, actuation now fails with a **clear `CapabilityError`** ("power control unavailable — see `kvm-pilot paths`") instead of an opaque `HTTP 500` (#174). Actuation itself has **no ledger run** (`never_exercised: power` — no OOB power on the fleet). |
| `hid` | `type`, `key`, `mouse-move`, `click` · MCP `type_text`, `press_key`, `send_shortcut`, `ctrl_alt_delete`, `mouse` | conditional | emulator; live-observed (not yet in ledger) | Works, but the emulated USB HID gadget can drop to **busy/unplugged** on real RM1PE units (#155), blanking keyboard/mouse until re-enumerated. Mitigations shipped: `recover_hid` (#160), keep-awake jiggler (#159), `display_awake` (#161). Not captured as a formal ledger capability yet — a **sweep gap**. |
| `video` | `snapshot`, `classify`, `watch` · MCP `snapshot`, `classify_screen`, `wait_for_state` | conditional (V1.9.1) / false-report (V1.5.1) | live:gl.inet RM1PE@V1.9.1 (snapshot PASS); @V1.5.1 (snapshot FAIL, H.264) | **Headless snapshot now works (#142):** GL's encoder is on-demand — a snapshot with no video client 503'd forever (`streamer: null`). The driver now registers a stream client over kvmd `/api/ws` to start it, then snapshots, with a `streamer_warm()` keep-alive (~0.1s/frame). **Live-verified 1600×900 JPEG on V1.9.1.** On **V1.5.1** the encoder emits an undecodable H.264 **P-frame** (no SPS/PPS/IDR, #107/#151) — surfaced as `SnapshotFormatError`, not a false success. The 503 detail now names the offline-streamer case honestly (#173) instead of "encoder wedged". |
| `virtual_media` | `media-list`, `mount`, `eject` · MCP `list_virtual_media`, `mount_iso`, `eject` | conditional | **live: RM1PE @V1.9.1 + @V1.5.1 (mount→online→eject PASS, 2026-07-07)** | Now **live-verified on all three units**: `mount_iso` uploaded a test ISO, `/api/msd` reported `online=true`, and `eject` detached it. The historical false-report — GL reports `connected=true` while `online` stays **false** when the MSD toggle is off (#77) — is guarded by `mount_iso` polling for `online` and raising `MediaOfflineError`. Positive tell (#78): host boot menu shows "Glinet Optical Drive" when truly presented. |
| `gpio` | library API (`gpio_switch`/`gpio_pulse`); no CLI/MCP | unverified | emulator/mock | Inherited from the PiKVM base, but **RM1PE exposes no GPIO channels** (`ATX enabled=false, no GPIO`). Present in the API surface, effectively unusable on this hardware. |
| `events` | `events` (`websocket-client`, now a base dep) | unverified | live-connected (not asserted) | WebSocket event stream over kvmd `/api/ws`. Connects live and returns frames (same channel #142 uses to start the streamer); not yet asserted against a specific state change, so kept `unverified`. |
| `logs` | `logs` · MCP `logs` | **reliable** | live:gl.inet RM1PE@V1.5.1 (beta), @V1.9.1 (beta) | kvmd `/api/log`; `seek` = seconds of lookback. Verified on both firmwares — it's how the RV1126 encoder wedge (venc/vpss/vvi in D-state) was diagnosed. `follow`/tail is refused (blocking transport). |
| `boot_progress` | `boot-progress` | n/a | n/a | Not implemented by the PiKVM family — boot phase comes from **vision** (`classify`), not a structured enum. |
| `sensors` | `sensors` | n/a | n/a | `Sensors` protocol not implemented. Raw Prometheus metrics exist (`get_metrics`, `/api/export`) but are not the structured capability. |
| `serial_console` | — | n/a | n/a | Not implemented. |
| `watchdog` | — | n/a | n/a | Not implemented. |
| `firmware_update` | `firmware-update`, `firmware-check` | **false-report** | live:gl.inet RM1PE@V1.5.1 (alpha, **FAILED**) | GL `/api/upgrade/*` (provisional, reverse-engineered). On RM1PE the `start` POST **returns 200 and no-ops** (#94/#95) — the driver now verifies an actual upgrade-state change and reports failure otherwise. Registry `remote_update`: `risk=high, recovery_required=true, self_flash_blind=true` (physical U-Boot recovery only; a flash can re-disable the REST API and can corrupt if media is mounted). **The GL web console is the only known-good upgrade path** (V1.5.1→V1.9.1 live-verified, #177; quirk `firmware-flash-webui-only`). See [firmware-update.md](firmware-update.md). |
| `ssh` | `ssh-check`, `ssh-exec` · MCP `ssh_reachable`, `ssh_exec` | (per-profile) | n/a as a driver cap | Not a GLKVM capability. In-band SSH to the **managed host's** OS is a per-profile channel (`ssh_*` config, #81). Separately, an **appliance-SSH** channel to the KVM's *own* OS (#162) backs `kvm-pilot appliance-*` / `paths` for encoder-wedge recovery REST can't see. |

## `pikvm` — stock PiKVM (canonical base)

`PiKVMDriver` in [`client.py`](../src/kvm_pilot/client.py) — the full kvmd REST
client and the base of the whole family. **No stock-PiKVM device has a run in
the ledger**; the GL fork is what has actually been on hardware. Everything
below is therefore `unverified` on real hardware, though the read/HID/power/MSD
paths are exercised over the real transport against the kvmd emulator in CI.

Structural set: `system_info, power, hid, video, virtual_media, gpio, events, logs`.

| Capability | CLI / MCP surface | Reliability | Testing level | Notes |
|---|---|---|---|---|
| `system_info` | `info` · MCP `info` | unverified | emulator | kvmd `/api/info`. |
| `power` | `power`, `power-cycle` · MCP `power`, `power_state` | unverified | emulator | ATX. `is_powered_on` fail-opens when no ATX board is wired (returns "on" rather than a false "off"); on real stock PiKVM with an ATX board it should be trustworthy, but that is **unverified**. |
| `hid` | `type`, `key`, `mouse-move`, `click` · MCP HID tools | unverified | emulator | Keyboard/mouse over kvmd. |
| `video` | `snapshot`, `classify`, `watch` · MCP `snapshot`, `classify_screen`, `wait_for_state` | unverified | emulator | MJPEG snapshot with JPEG-header validation (#107). |
| `virtual_media` | `media-list`, `mount`, `eject` · MCP MSD tools | unverified | emulator | Upload/select/attach with online-verify. |
| `gpio` | library API; no CLI/MCP | unverified | emulator/mock | `/api/gpio`. |
| `events` | `events` (needs `ws` extra) | unverified | mock | WebSocket. |
| `logs` | `logs` · MCP `logs` | unverified | emulator | kvmd `/api/log`; `follow` refused. |
| `boot_progress` | `boot-progress` | n/a | n/a | Not implemented (vision instead). |
| `sensors` | `sensors` | n/a | n/a | Not implemented (Prometheus metrics only). |
| `serial_console` | — | n/a | n/a | Not implemented. |
| `watchdog` | — | n/a | n/a | Not implemented. |
| `firmware_update` | `firmware-update` | n/a | n/a | **Not implemented** — stock PiKVM has no OS-update REST API (it updates via `pikvm-update` over SSH). |
| `ssh` | `ssh-check`, `ssh-exec` | (per-profile) | n/a as a driver cap | Per-profile in-band channel, as above. |

## `blikvm` — BliKVM

[`BliKVMDriver`](../src/kvm_pilot/drivers/pikvm.py) is a **thin subclass of the
PiKVM base with no known deltas yet** — it exists so BliKVM-specific behavior has
a home. Its capability set, reliability, and testing level are **identical to
`pikvm`** above: structural set `system_info, power, hid, video, virtual_media,
gpio, events, logs`; everything `unverified` on real hardware (no BliKVM ledger
run), read/HID/power/MSD paths `emulator`-tested; `boot_progress`, `sensors`,
`serial_console`, `watchdog`, `firmware_update` not implemented. Any BliKVM
quirk discovered on hardware should be added to that subclass and reflected
here.

## `redfish` — DMTF Redfish BMC (iDRAC / iLO / Supermicro / Lenovo XCC / OpenBMC)

[`RedfishDriver`](../src/kvm_pilot/drivers/redfish/driver.py) — one stdlib client
for every DMTF-conformant BMC, portable by **navigating hypermedia** (follows
`@odata.id`, reads `@Redfish.ActionInfo`/`AllowableValues`; no hard-coded vendor
ids). Its capability set is **complementary** to a PiKVM's: strong on structured
state, **no pixels, no keyboard/mouse**. Per its own docstring it is **"alpha,
mock-tested only — never run against real hardware"**, but the whole set is
validated end-to-end against the external **sushy-tools** emulator in CI, so its
testing level is `emulator`, not bare `mock`. No live ledger rows exist yet, so
every reliability rating is `unverified`.

Structural set: `system_info, power, virtual_media, logs, boot_progress, sensors`.

| Capability | CLI / MCP surface | Reliability | Testing level | Notes |
|---|---|---|---|---|
| `system_info` | `info` · MCP `info` | unverified | emulator (sushy-tools) | ComputerSystem identity + volatile power/health/boot re-read fresh. |
| `power` | `power`, `power-cycle` · MCP `power`, `power_state` | unverified | emulator (sushy-tools) | `ComputerSystem.Reset`, ResetType chosen by intersecting a preference list with the target's advertised `AllowableValues`; blocks on the real `PowerState` transition. In principle **more trustworthy than ATX**, but unverified on hardware. |
| `virtual_media` | `mount`, `eject` · MCP `mount_iso`, `eject` | unverified | emulator (sushy-tools) | `InsertMedia`/`EjectMedia`; adapts to strict BMCs (drops optional body fields, retries with `TransferProtocolType`). No separate connect step. |
| `logs` | `logs` · MCP `logs` | unverified | emulator (sushy-tools) | SEL / lifecycle / IML via LogServices; `seek` = seconds, filtered on `LogEntry.Created`. `follow` refused. |
| `boot_progress` | `boot-progress` | unverified | emulator (sushy-tools) | **The Redfish standout** — structured `BootProgress.LastState` mapped to the project's phase vocabulary; no screenshot needed. |
| `sensors` | `sensors` · CLI `sensors` | unverified | emulator (sushy-tools) | Unified `Sensors` collection with `Thermal`/`Power` fallback; `$expand` fan-out where advertised. |
| `hid` | — | n/a | n/a | A BMC has no keyboard/mouse. Deliberately not implemented. |
| `video` | — | n/a | n/a | No screenshot endpoint. Vision is unavailable on Redfish. |
| `gpio` | — | n/a | n/a | Not applicable. |
| `events` | — | n/a | n/a | Redfish EventService (push/SSE) not implemented in this version. |
| `serial_console` | — | n/a | n/a | Redfish exposes SOL as an SSH/IPMI descriptor, not an HTTP byte stream — not implemented. |
| `watchdog` | — | n/a | n/a | An IPMI primitive; not implemented. |
| `firmware_update` | — | n/a | n/a | Redfish `UpdateService` not yet implemented (a later step). |
| `ssh` | `ssh-check`, `ssh-exec` | (per-profile) | n/a as a driver cap | Per-profile in-band channel, as above. |

## `fake` — in-process test double (no hardware)

[`FakeDriver`](../src/kvm_pilot/drivers/fake.py) implements the capability
protocols over mutable in-memory state: no network, no hardware. It is the
device double for tests, demos, and CI, and the **reference implementer of
`BootProgress`**. Destructive ops still route through the same `SafetyPolicy`
op-ids as a real driver, so it is a faithful stand-in for the safety layer, the
analyzer, and the CLI.

Reliability here means only "does the double behave deterministically" — it
**proves nothing about any physical device.** Testing level is `mock` by
definition (it *is* the mock). Never point it at, or infer anything about, real
hardware.

Structural set: `system_info, power, hid, video, virtual_media, gpio, events, logs, boot_progress`.

| Capability | Reliability | Testing level | Notes |
|---|---|---|---|
| `system_info` | deterministic (test double) | mock | Canned `{host, driver, powered, phase}`. |
| `power` | deterministic (test double) | mock | Mutates in-memory `powered`; gated identically to a real driver. |
| `hid` | deterministic (test double) | mock | Records keystrokes/clicks; scriptable HID-online state (#160/#161 stand-ins). |
| `video` | deterministic (test double) | mock | Returns a stub JPEG; scriptable OCR text and video-signal flag. |
| `virtual_media` | deterministic (test double) | mock | In-memory mount history; kvmd-shaped MSD state. |
| `gpio` | deterministic (test double) | mock | Records switch/pulse. |
| `events` | deterministic (test double) | mock | Replays a queued event list, then stops (unlike the real unbounded stream). |
| `logs` | deterministic (test double) | mock | Returns canned log text. |
| `boot_progress` | deterministic (test double) | mock | First `BootProgress` implementer; returns the scripted phase (or `None` while powered off). |
| `sensors` / `serial_console` / `watchdog` / `firmware_update` / `ssh` | n/a | n/a | Not implemented. |

---

## Live-fleet sweep evidence (2026-07-07)

> **Status: sweep complete.** Full findings + reliability matrix:
> [#176](https://github.com/DustinTrap/kvm-pilot/issues/176). The runs are in
> `src/kvm_pilot/data/test_runs.jsonl`; `kvm_pilot.maturity` derived the levels
> below (drift-checked in CI).

A full-fleet reliability sweep of **`.11`** (`homelab`), **`.20`** (`homelab2`),
and **`.39`** (`bench`) — all GL-RM1PE — ran every CLI subcommand + MCP tool
multiple times, cross-checked against appliance-SSH + native-REST ground truth
per the [test plan](test-plan.md). Headline results that changed the `glkvm`
ratings above:

- **`video` / `snapshot` — fixed for the headless case ([#142](https://github.com/DustinTrap/kvm-pilot/issues/142)).**
  The RCA: GL's encoder is on-demand and runs only while a video client is
  connected, so a headless `snapshot` 503'd forever (`streamer: null`) — the
  100% failure the sweep found on all three idle units. The driver now registers
  a stream client over kvmd's `/api/ws` to start the encoder, then snapshots
  (`streamer_warm()` keeps it warm, ~0.1s/frame). **Verified live returning a
  valid 1600×900 JPEG on V1.9.1 (`.11`/`.20`)** → `snapshot` is now a ledger PASS
  on V1.9.1. On V1.5.1 (`.39`) the streamer starts but emits an undecodable H.264
  P-frame ([#107](https://github.com/DustinTrap/kvm-pilot/issues/107)/[#151](https://github.com/DustinTrap/kvm-pilot/issues/151)) — an honest fail, not a false success.
- **`virtual_media` — promoted from `never_exercised` to live-verified** on all
  three (mount → `online=true` → eject).
- **`power` — the opaque `HTTP 500` on unwired ATX is fixed**
  ([#174](https://github.com/DustinTrap/kvm-pilot/issues/174)) to a clear
  "power control unavailable" `CapabilityError`. Actuation is still
  `never_exercised` (ATX unwired on the whole fleet — no OOB power).

Ledger-derived maturity after the sweep (confirm any time with
`python -c "from kvm_pilot.support_matrix import rollup; import json; print(json.dumps(rollup(), indent=2, default=str))"`):

| Unit(s) | Model | Firmware | Maturity | Live PASS | Live FAIL / never |
|---|---|---|---|---|---|
| `.11`, `.20` | GL-RM1PE | V1.9.1 release1 | **beta** | info, snapshot, healthcheck, logs, power_state, virtual_media | never: power (ATX unwired), firmware_update |
| `.39` | GL-RM1PE | V1.5.1 release2 | **alpha** | info, healthcheck, logs, power_state, virtual_media | FAIL: snapshot (H.264, #107/#151), firmware_update (no-op #94/#95); never: power |

Not exercised this sweep (honest gaps): `hid` **delivery** (no video/in-band SSH to
confirm a keystroke landed — command path only), `gpio` (unwired), `events`
(connects, but not asserted against a state change), `classify`/`watch` vision
(skipped), and appliance-reboot recovery (not triggered on a healthy box).

## See also

- [Hardware-Compatibility (wiki)](https://github.com/DustinTrap/kvm-pilot/wiki/Hardware-Compatibility) — the community source of truth for real-hardware runs.
- [Hardware reliability test plan](test-plan.md) — the reusable fleet-sweep procedure that produces the ledger evidence this page rates from ([#172](https://github.com/DustinTrap/kvm-pilot/issues/172)).
- [Architecture](architecture.md) — the capability-protocol design and the cross-device capability matrix.
- [Firmware registry](firmware-registry.md) — currency, capability profiles, and the derived-maturity ladder.
- [Remote firmware update](firmware-update.md) — the GL `/api/upgrade/*` surface and its high-risk reliability model.
- [CLI reference](cli.md) — every subcommand and the capability it needs.
