# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Intel AMT / vPro driver** (`make_driver("amt")`, `--driver amt`,
  [`drivers/amt/`](src/kvm_pilot/drivers/amt/), #211) — the most fully-featured
  driver yet, managing AMT laptops/desktops out-of-band over three native
  protocols, all **pure-stdlib**:
  - **WS-Man** (SOAP over HTTP Digest, 16992/16993) — `Power` (CIM
    `RequestPowerStateChange`: on / soft-off / hard-off / reset), `SystemInfo`
    (manufacturer, model, serial, UUID, AMT version, provisioning state), and
    single-use **`BootConfig`** (PXE / HDD / CD / BIOS-setup).
  - **SOL** serial console (`SerialConsole`, port 16994) via `amtterm` —
    `kvm-pilot console` works at BIOS/GRUB when serial-redirect is on; password
    passed via `AMT_PASSWORD` env, never argv.
  - **RFB / KVM-redirection** (`Video` + `HID`, standard-port 5900) — a
    from-scratch stdlib VNC client (inline DES for the VNC challenge) that
    captures the **platform framebuffer**: a genuine BIOS/POST/GRUB screenshot
    on a machine whose HDMI a capture-KVM never sees boot, plus keyboard/mouse
    injection. The first non-PiKVM driver to implement `Video`/`HID`.
  - Destructive AMT ops (`amt.power_*`, `amt.reset_hard`, `amt.set_boot_device`,
    `amt.serial_console`, and the shared `hid.*`) are gated through
    `SafetyPolicy`; the KVM-redirection (RFB) password is a separate MEBx
    credential (`amt_kvm_password` / `KVM_PILOT_AMT_KVM_PASSWORD`), falling back
    to the WS-Man password when unset. Covered by 41 unit tests against
    pure-stdlib WS-Man and RFB emulators (including a DES FIPS-46-3 known-answer
    vector). **Not yet live-validated** — activation of AMT network access in
    MEBx is pending on the test unit.

## [0.1.0b8] — 2026-07-14

**Beta 8 — the docs, visuals & brand overhaul (#209).** No runtime code
changes; this release exists so the refreshed README (the PyPI page), the
corrected claims, and the new brand reach every published surface.

### Added
- **Brand assets** (`docs/assets/`): the `>_ kvm-pilot` terminal-prompt
  wordmark (SVG, dark-mode aware), favicon, and a 1280×640 social card, all in
  the docs' house palette.
- **Live demo GIF** in the README — a real, unedited GLKVM run (honest
  preflight, headless snapshot waking the on-demand encoder, gated dry-run
  power-cycle, and the captured boot console), recorded with the committed
  `docs/assets/demo.tape`.
- **Five house-style diagrams** for the hardest concepts: the MCP trust
  ladder, the two-machines (appliance vs managed host) picture, the snapshot
  pipeline and its failure exits, the evidence→maturity ledger, and the
  approval/signed-receipt lifecycle — embedded in getting-started,
  driver-features, and the MCP README.
- **`docs/troubleshooting.md`** — the canonical symptom-first page (GLKVM API
  404, snapshot failures, `approval cancel`, dark-host WoL-first recovery,
  mouse calibration, iDRAC6 SOL-on-COM2, `--pre` install) + FAQ; on the wiki
  sidebar.
- **Root `llms.txt`** (llmstxt.org) — an agent-discoverable doc map.
- **Docs guard tests** (`tests/test_docs_guards.py`): README version literals
  must match `__about__.py` (the guard regex handles the `v` prefix that let
  "v0.1.0b2" ship stale on the b5 PyPI page), and SKILL.md's tool list must
  two-way match the registered MCP tool surface.

### Changed
- **One tagline everywhere** — README, PyPI description, `server.json`, and
  the GitHub About now all say: *Smart hands for your AI agents — write-capable,
  multi-plane (KVM + BMC + SSH) MCP server: gated, verified, audited.*
- **README restructured**: logo + badge row, quickstart with the copy-paste
  `claude mcp add` (READ_ONLY first rung) above the fold, a
  tool-surface-by-plane table, a version-agnostic status block, and the GLKVM
  API warning demoted to a setup note pointing at troubleshooting.
- **SKILL.md tool list** now covers all 29 MCP tools (was missing
  `wait_for_state`, `boot_options`, `appliance_status`, `access_paths`,
  `calibrate_mouse`, `set_boot_device`, `wake`, `appliance_reboot`); the
  "nothing in kvm-pilot reboots the appliance" claims were corrected in
  SKILL.md and the MCP README (stale since `appliance_reboot` shipped).
- **MCP README** gains the missing `boot_options` / `set_boot_device` / `wake`
  rows, the `KVM_PILOT_MCP_ALLOW_CONFIG` env entry, and drops "(experimental)"
  from its title.
- **architecture.md** rewritten to the present tense (the driver split, the
  `BootConfig` protocol, and the Redfish/IPMI drivers have long landed); the
  capability matrix gains a boot-device row.
- The wiki sidebar groups the internal analysis narratives under their own
  heading.

### Fixed
- **Honesty fix:** the README compatibility table claimed `snapshot` was
  verified on RM1PE firmware V1.5.1 — it fails there with an undecodable
  H.264 frame (#107/#151) and is verified on V1.9.1 only. Same correction in
  CLAUDE.md; a live-verified Dell iDRAC6 (IPMI) row was added.
- The registry-name casing in this changelog's b3 entry
  (`io.github.dustintrap` → `io.github.DustinTrap`, matching the live
  listing).

### Removed
- `RESUME.md` is no longer tracked in the repo (local-only, gitignored) — the
  committed copy published internal fleet details (#209). The checkpoint
  skill's contract was updated to match.
- The orphaned `docs/mcp-tools.svg`.

## [0.1.0b7] — 2026-07-14

**Beta 7 — IPMI.** A full IPMI 2.0 driver for pre-Redfish BMCs (Dell iDRAC6, older
HPE iLO, Supermicro), validated end-to-end against a live Dell iDRAC6 (R710).

### Added
- **IPMI driver (#62).** `--driver ipmi` shells out to `ipmitool -I lanplus`
  (password via the `IPMI_PASSWORD` env, never argv). Implements Power /
  SystemInfo / BootConfig / Sensors / Logs, so `power` / `boot-device` /
  `sensors` / `logs` work with no new CLI/MCP code — the pre-Redfish BMC path
  (e.g. Dell iDRAC6 / R710). 34 hardware-free tests. IPMI completeness is now
  roughly Redfish minus VirtualMedia.
- **IPMI serial console / SOL (#208).** `SerialConsole` for `IpmiDriver`
  (`serial_read`/`serial_write` over a PTY-backed `ipmitool sol activate`) plus a
  `kvm-pilot console` command for an interactive text console (exit with `~.`).
  Gated as `ipmi.serial_console` (HID_INPUT — it can inject keystrokes). Text-only;
  drives Linux/ESXi serial installers, not the Windows GUI installer.

### Fixed
- **IPMI `get_info` model (#62).** Dell iDRAC6 fills FRU `Product Name` with the
  BMC hostname (e.g. `localhost`); the model now comes from `Board Product` with
  vendor placeholders skipped, so a real R710 reports `PowerEdge R710` — not
  `localhost`. (Found on real hardware.)
- **Healthcheck driver label (#62).** `_driver_kind` omitted `ipmi`, so the IPMI
  driver was mislabeled `pikvm@<host>` in health reports; it now reads `ipmi@…`.

### Testing
- IPMI driver cross-checked against an independent OpenIPMI `ipmi_sim` BMC
  (`tests/integration/test_ipmi_external.py`, env-gated `ipmi_bmc` fixture) —
  the IPMI analogue of the sushy-tools Redfish cross-check. Stock-sim limits
  documented in `docs/decisions.md`.
- IPMI driver + SOL live-validated end-to-end against a real Dell iDRAC6 (R710):
  power/boot/sensors/SEL/info (14/0), SOL boot capture, and F11 keystroke drive
  into the BIOS Boot Manager.

## [0.1.0b6] — 2026-07-14

**Beta 6 — remote boot-control + Wake-on-LAN.** A cohesive set for driving what a
host boots and powering it on out-of-band, matured against simulators
(the in-repo Redfish emulator + independent sushy-tools) ahead of real
BMC validation. See the [iLO/iDRAC hardware test plan](docs/hardware-test-plan-ilo-idrac.md).

### Added
- **Wake-on-LAN (#199).** `wol.py` magic-packet core; `kvm-pilot wake`
  (`--mac`/`--broadcast`) + a `wake` MCP tool; `HostConfig.mac` / `wol_broadcast`.
  **`power on` now falls back to WoL** when the KVM has no wired ATX/GPIO power
  path (GL units report `ATX enabled=false`) and the profile carries a `mac` —
  the magic packet reaches the NIC directly, bypassing the missing power channel.
  *Hardware-validated:* woke a real suspended RHEL host in 52 s.
- **Redfish boot-device control (#28/#201).** New `BOOT_CONFIG` capability +
  `BootConfig` protocol; `kvm-pilot boot-device <pxe|cd|hdd|usb|bios|diag|none>
  [--once|--persistent] [--legacy] [--show]` plus `boot_options` / `set_boot_device`
  MCP tools. Feature-detects the BMC's advertised targets and `BootSourceOverrideMode`,
  and auto-retries without the mode property on BMCs (iLO4/iDRAC7) that reject it.
- **In-band boot control (#150).** `boot-device --via ssh` sets a one-time UEFI
  `BootNext` via `efibootmgr` over the SSH channel (single-gated as `ssh.set_boot_next`).

### Changed
- Boot-device and WoL are gated: `redfish.set_boot_device` / `ssh.set_boot_next`
  (`CONFIG_MUTATION`) and `wol.wake` (`POWER_SOFT`) join `DESTRUCTIVE_OPS` /
  `OP_EFFECT`; the MCP tools require the matching operator enable-flag + `confirm`.

### Testing
- The Redfish emulator gained a `Boot` object, `PATCH`, and iLO4/iDRAC7 quirk
  knobs; boot-device is also validated against **sushy-tools** (an independent
  Redfish), which surfaced a real behavior difference (its `--fake` pins
  `BootSourceOverrideEnabled`). ~75 new tests across wol/redfish-boot/efiboot/CLI/MCP.

### Not yet validated
- Real-hardware BMC runs on HPE iLO / Dell iDRAC are **pending (#29)** — the code
  is exercised on simulators only. iDRAC6-era boxes (e.g. R710) have no Redfish;
  IPMI is tracked separately (#62).

## [0.1.0b5] — 2026-07-13

**Beta 5 — calibration fixes from the first live fleet run.** b4's mouse
auto-calibration went to real hardware (GL-RM1PE ×2, V1.9.1) hours after
release; the live run validated the algorithm (residual **0.0106** of the
screen ≈ 17px at 1600×900 on a RHEL/GDM host, visually verified) and found two
functional bugs in the surrounding plumbing — both fixed here. If you
installed b4, upgrade: calibration on PiKVM-family devices recorded no
resolution and, on GL's on-demand streamer, was never applied via the CLI path.

### Fixed
- `current_resolution()` called a nonexistent method (`signal_state` instead
  of `video_signal_info`), so every stored calibration said
  `resolution: "unknown"` — silently weakening the resolution-staleness
  protection on exactly the hardware that reports its mode.
- The staleness guard refused to apply a stored calibration whenever the
  current mode was *unreadable* — which on GL's on-demand streamer is
  whenever nothing is watching, i.e. most of the time. Staleness is now
  judged on evidence: an **observed** different mode still refuses; an
  unobservable one applies best-effort (the snapshot-anchored click flow
  wakes the streamer and confirms the mode exactly where certainty matters).

### Live-run evidence (first #128 hardware validation)
- Positive: 10.0.1.20 (RHEL GDM login) — scale ≈1.002/1.004, a real 1.4%
  y-offset found, held-out residual 0.0106, correction applied and visually
  confirmed at dead center.
- Negative: 10.0.1.11 (Fedora text console, no mouse cursor) — failed
  actionably ("cursor not found"), nothing stored; the blinking text cursor
  produced blobs in some frames and the later defenses caught it (static-gate
  multi-sampling filed as a follow-up).

## [0.1.0b4] — 2026-07-13

**Beta 4 — mouse auto-calibration.** Fixes the oldest live-hardware complaint
(#128, "the agent clicks where the button should be and misses"): kvm-pilot now
measures each host's real commanded→observed mouse mapping and corrects for it
transparently.

### Added
- **`calibrate-mouse` CLI / `calibrate_mouse` MCP tool** (#128) — parks the
  cursor, commands a 5-point grid, locates the observed cursor by frame
  differencing (the baseline cursor's departure mark is *identified from the
  data* as the blob recurring across every frame — never assumed from the
  commanded corner), least-squares fits per-axis scale+offset, and verifies a
  **held-out** point within `--tolerance` (default 0.02 of the screen). The
  correction is stored per (host, capture resolution) and goes stale on a
  resolution change — it is then never applied. Failures are actionable
  errors, never partial stores: non-static screens, invisible cursors,
  implausible fits, and non-linear pointer acceleration are each named with a
  remedy. Pointer moves only, but gated like HID input (one approval covers
  the run); under dry-run the tool short-circuits explicitly, since pointer
  moves are not `DESTRUCTIVE_OPS` and the driver guard wouldn't intercept
  them. Snapshot decoding needs Pillow — the new `calibrate` extra
  (`pip install 'kvm-pilot[calibrate]'`); the detection/fit math is stdlib and
  fully tested with synthetic frames.
- Percent-space `mouse` moves/clicks (MCP and CLI) apply the stored
  calibration transparently and report `calibrated: true/false` per the
  honest-sensor doctrine. Pixel/raw coordinates mean "exactly here" and are
  never adjusted.

### Fixed
- `docs/cli.md` again satisfies its own "every command must appear here"
  contract: added the missing `keep-awake`, `recover-hid`, `appliance`, and
  `paths` rows (found by the #194 docs review) alongside the new
  `calibrate-mouse`.
- MCP registry publish: the release job's `mcp-publisher` download 404'd
  (assets use Go arch names — `amd64`, not `x86_64`); mapped and
  version-pinned. Registry validation learnings applied: description ≤ 100
  chars; the namespace and the PyPI `mcp-name:` marker are case-sensitive
  (`io.github.DustinTrap/kvm-pilot`) — this release's README carries the
  corrected marker, so the listing goes live with b4 (#197).

## [0.1.0b3] — 2026-07-13

**Beta 3 — the externally visible contracts release.** The safety internals
(effect gates, signed receipts, typed outcomes, audit trail) were already ahead
of the MCP ecosystem; this release makes them *legible from the outside*:
precise per-tool annotations that clients can build policy on, a true
least-privilege read-only launch mode enforced below the tool layer, and an
official MCP registry listing. Distilled from the 2026-07-13 competitive gap
analysis (#194).

### Added
- **`KVM_PILOT_MCP_READ_ONLY` launch mode** (#196) — the least-privilege
  posture for first contact with a fleet: only read-only tools are registered
  (minus `ssh_discover`, an active scan), every effect gate is force-closed
  regardless of `ALLOW_*`, and every driver is built with a deny-all confirm,
  so even a registration bypass fails closed instead of mutating (tool
  filtering alone is not enforcement). Results carry `read_only: true`. The
  documented trust ladder is now READ_ONLY → DRY_RUN → per-effect `ALLOW_*`,
  and the getting-started guide starts new users on the first rung.
- **Official MCP registry listing** (#197) — `server.json` at the repo root
  names the server `io.github.DustinTrap/kvm-pilot`; the release workflow
  gains a best-effort `publish-mcp-registry` job (GitHub OIDC, runs after the
  PyPI publish, version-synced to the release tag) and the README carries the
  `mcp-name:` ownership marker the registry validates against PyPI. The
  listing goes live with the first release that ships this README.

### Changed
- **Per-tool MCP annotations** (#195) — every tool now declares all four MCP
  hints explicitly (`readOnly`/`destructive`/`idempotent`/`openWorld`), and a
  regression test fails CI if a tool ships without them (clients build
  approval/parallelism policy from these bits, and the spec defaults treat an
  unannotated tool as maximally dangerous and internet-reaching). Notable
  precision changes: `mount_iso`/`eject`/`file_firmware_report` are now
  annotated as reversible (non-destructive) writes — their MEDIA /
  EXTERNAL_WRITE gates and approvals are unchanged; `classify_screen` /
  `wait_for_state` are flagged open-world because the *server-side vision
  backend* may be a cloud VLM (a local backend never leaves the network); all
  LAN-only tools now say so (`openWorldHint=false`).

### Fixed
- The README (PyPI long description) used relative links for the
  getting-started guide, docs pages, LICENSE/NOTICE, and the architecture SVG
  images — they 404 (and the images don't render) on pypi.org. All README
  links are now absolute, a guard test keeps them that way, and the PyPI
  sidebar gains a `Documentation` project URL pointing at the wiki. Takes
  effect on the PyPI page at the next release (#193).

## [0.1.0b2] — 2026-07-12

**Beta 2 — the reliability + evidence-harness release.** Two hardening
batches in one: the transports can no longer double-fire a destructive action,
MCP approvals became signed single-use receipts with an audit trail, GLKVM
snapshots work headless at native resolution, evidence learned to carry the
conditions it was observed under — and `kvm-pilot test-report` turns the whole
per-device intake into one command. Everything in this release is
unit/emulator-verified; the first live fleet run of the new paths is the next
milestone (the harness exists precisely to record it honestly). 18 issues
closed.

### Added
- **`kvm-pilot test-report`** (#99) — the live-test harness: probes the
  device's capabilities and appends one evidence row to the run ledger
  (`--ledger` > `$KVM_PILOT_TEST_LEDGER` > `~/.config/kvm-pilot/test_runs.jsonl`;
  never the installed package data). Read-only probes (info, snapshot —
  stamped with the #156 conditions on pass AND fail — healthcheck, logs,
  power_state) run every invocation; destructive probes run only via
  `--include virtual_media,power,firmware_update` with a recorded `--attest`
  operator statement, still routed through the normal safety gates. Pass =
  assertion + **observed effect** (power must flip the read-back and restore;
  media must report online and the eject must land; a flash passes only via
  the driver's #94 verified-state contract) — an unobserved effect records an
  honest FAIL. Fake-driver runs (and `--synthetic`) record
  `source="synthetic"`, which never promotes maturity; there is no flag to
  force `real`.
- The generated wiki Hardware-Compatibility page now carries a **Maturity
  column** — the #98-derived level per (device, firmware) read from the shipped
  registry (`—` when no live-derived rating exists), completing #103.
- New GLKVM quirk **`firmware-flash-webui-only`** (#177): the GL web console is
  the only known-good RM1PE upgrade path (V1.5.1→V1.9.1 live-verified); the
  `/api/upgrade/*` flash was observed to no-op (#94/#95) and no API-driven
  flash has ever been verified on any release. `firmware-update` output now
  steers to the web console on affected devices (it never blocks — the driver
  already reports a no-op flash honestly), and
  firmware-update/driver-features/firmware-registry docs carry the guidance.

- **GLKVM headless JPEG snapshot at native resolution** (#187): when the
  snapshot bytes fail the JPEG guard (H.264 at native/high res, #107) and the
  firmware exposes `params.video_format` (V1.9.1+), the driver flips the
  encoder to MJPEG (`POST /api/streamer/set_params?video_format=1`), retries,
  and restores the prior format — no EDID change, no H.264 decoder, no
  browser. Inside `streamer_warm()` the flip is held for the whole block and
  restored once at exit. Composes with the #142 offline-streamer recovery
  (offline → warm → H.264 → flip). V1.5.1 doesn't expose the switch and keeps
  the honest `SnapshotFormatError` (upgrade via the web UI, #177). Mechanism
  live-proven on V1.9.1; the auto-path is emulator-verified and awaits its
  first live ledger row. New quirk `snapshot-h264-at-native-res`.
- A firmware change now **invalidates the device assessment**: `preflight`
  remembers the firmware each device was last assessed at, forces the stable
  checks to re-run live when it differs (out-of-band web-UI flashes included),
  and emits a `firmware-delta` finding diffing what cleared / regressed /
  stayed open (#180).
- Run-ledger capability rows can now record the **conditions a result was
  observed under** (`conditions: {resolution, encoder_format, snapshot_cached,
  jpeg_sink_clients}`) — the axes that actually decide a GL snapshot outcome —
  and the support-matrix rollup surfaces them as `pass_conditions` /
  `fail_conditions`, so field reports at different operating points reconcile
  from data instead of reading as contradictions. Optional; pre-existing rows
  and derived maturity are unaffected (#156).
- CI docs-parity guard: `build_wiki.py --check` fails when a `docs/*.md` page is
  not registered in the wiki `PAGES` allowlist, so a new page can no longer
  silently go unpublished; the MCP README tool table gained the missing
  `access_paths` row (#175).

### Added (MCP)
- **Signed, expiring, single-use approval receipts + audit trail** for the
  destructive act tools (#72): every approval mints an HMAC-signed receipt
  bound to the exact invocation (host, tool, effect, args-hash, dry-run,
  approver), re-verified immediately before dispatch and consumed on use —
  a bound field changing after approval, an expired receipt
  (`KVM_PILOT_MCP_RECEIPT_TTL`, default 60 s), or a replay fails closed as a
  denial-shaped result. Approved results carry `receipt: {id, state}` and a
  real `approval.expires`. Every destructive-invocation terminal (approved,
  denied, consumed, expired, mismatched, replayed, dispatch-exception) emits
  one JSON audit record on the `kvm_pilot.mcp.audit` logger.

- New `file_firmware_report` tool — the MCP twin of CLI `firmware-check`'s
  auto-filing (#189), completing the registry telemetry loop for agent
  sessions. Filing a GitHub issue is a new **external-write effect class**
  with its own operator gate (`KVM_PILOT_MCP_ALLOW_EXTERNAL_WRITE`, off by
  default) plus the usual per-invocation approval; the shared helper moved to
  `firmware_registry.file_firmware_report` (#190).

### Changed
- The healthcheck's support-evidence finding now labels ledger history as
  **recorded** (vs this run's own tested-now probes), renders the #156
  conditions each result was observed under, and flags condition-blind
  snapshot passes ("verified only under unknown resolution/encoder") instead
  of overclaiming a bare `verified (n=N)` — the honesty regression that gave
  #180 its false confidence (#180).

### Changed (MCP)
- Act results now carry a typed **`outcome`** field (`approved` / `denied` /
  `cancelled` / `not_confirmed` / `gate_closed` / `invalidated`) so agents
  branch on data instead of matching the human-facing `denied_reason` strings —
  a cancelled elicitation (benign, retryable) is now typed apart from an
  explicit denial. The `KVM_PILOT_MCP_ELICIT=off` escape-hatch hint moved from
  every client-side denial to a **one-time hint after ≥2 consecutive**
  client-side approval kills on the same host — a security trade-off shouldn't
  be advertised on a one-off mis-click (#149).
- The `power` tool now returns a structured result instead of a sentence: it
  **bumps the frame generation** (a stale mouse click can no longer anchor to a
  pre-reboot snapshot within the reboot window) and **verifies the effect** via
  the driver's trustworthy signal — Redfish PowerState or a wired ATX LED —
  reporting `verified: true/false` with the observed state, or an honest
  `verified: null` + the reason and remedy when no trustworthy signal exists
  (GL units: ATX sensing lies; verify visually) (#168).

### Fixed
- The transports no longer auto-retry a **409/503 that answers a state-changing
  request** — re-firing a POST whose 409/503 arrived after the device began
  acting (e.g. a BMC perturbed by the `ComputerSystem.Reset` it just accepted)
  could double-fire a destructive action. Reads (GET/HEAD) keep the bounded
  retry; the #164 breaker semantics are unchanged; the Redfish driver's own
  at-target reconciliation now sees the surfaced error instead of being
  pre-empted by a transport re-POST (#167).
- A JSON endpoint answering with a **non-JSON body** (e.g. truncated at the
  content boundary) now raises a typed `ProtocolError` with a redacted preview
  instead of leaking raw bytes into dict-expecting callers as an opaque
  `AttributeError`; an empty 2xx body returns `None` (#170).
- Redfish `mount_iso` now **verifies the medium actually landed** (polls the
  VirtualMedia slot for `Inserted=true`, raising `MediaOfflineError` on a
  silent no-op — the #78 trap, Redfish edition; `verify=False` opts out), and
  a mid-flight 401 re-auth **DELETEs the old session before re-login** so a
  spurious 401 can't strand a live session slot on session-capped BMCs; a
  login that yields no session URI now logs the future leak (#169).

- The MCP effect gate now **fails closed** for an effect class with no
  registered enable-flag instead of silently borrowing the CONFIG gate;
  `APPLIANCE_RESET` is now explicitly mapped to
  `KVM_PILOT_MCP_ALLOW_APPLIANCE` (#190).

## [0.1.0b1] — 2026-07-11

**First beta.** The telemetry loop now closes itself: devices in the field
contribute registry updates automatically, and the ingest side can no longer
spam on malformed input. The beta label follows the support matrix — the
GL-RM1PE on V1.9.1 has its core capabilities at **beta** maturity in the run
ledger; most other device+capability combos remain unverified, and the matrix
stays the source of truth.

### Added
- `firmware-check` now **auto-files** the "Latest known release" report as a
  `firmware-report` issue (via the `gh` CLI) when the registry SSoT is behind
  the device-reported latest — the emission side of the registry telemetry loop
  was fully manual and produced almost no reports (#189). On by default;
  `--no-file-report` opts out, `--dry-run` previews the exact issue body,
  `--source`/`--date`/`--repo` override the details. Deduplicated against
  existing reports and validated with the same rules the ingest applies.
- `firmware_registry.render_issue_form()` — the inverse of `parse_issue_form`,
  so auto-filed bodies round-trip through the ingest parser (#189).
- `reconcile()` carries the registry entry's `source` (release channel URL)
  into the suggested submission, so known devices auto-file with no extra input.

### Changed
- The hourly firmware-ingest workflow **parks invalid submissions**: one ❌
  comment + the `ingest-error` label (remove the label after fixing the body to
  re-queue). Previously a malformed `firmware-report` issue was re-commented
  every hour forever — #177/#180 accumulated ~59 bot comments each over 3 days
  (#188).

## [0.1.0a14] — 2026-07-08

The **interface-router** release. There are many ways to reach a managed machine —
in-process driver calls, the MCP server, the browser console, and now in-band
**SSH** and **WinRM / remote-PowerShell** — and they differ by orders of magnitude
in latency and cost, *and* in whether they even work right now (a GL snapshot is a
JPEG or an undecodable H.264 frame depending on resolution/streamer state; SSH works
only while the OS is up). This release adds an **adaptive interface router** that
picks the fastest interface that will actually produce the result, per device,
benchmarked and self-tuning from real use — split across a KVM **control plane**
(works at any OS state) and an in-band **OS plane** (fast structured text). Grounded
in a live benchmark across the GL fleet plus real Linux hosts (#179/#181). Per the
support matrix this line stays **alpha**.

### Added
- `kvm-pilot benchmark` — per-command latency + capability profiler that emits a
  per-device scorecard (`--json` or table); `--select COMMAND` prints the
  interface the adaptive router would pick, `--no-os-plane` skips the in-band
  probes. Foundation for the interface router (#181, benchmarked in #179).
- **Adaptive interface router** (`router.py`): KVM-control vs OS-in-band
  **planes**, `select_interface()` (cheapest *capable* interface), and
  state-change invalidation (`is_stale`/`stale_rows`) (#181).
- **SSH** and **WinRM / remote-PowerShell** as first-class, benchmarked
  interfaces. WinRM ships dependency-free as PowerShell-over-SSH
  (`remote_ps.py`), with a seam for native WS-Man later (#181).
- **Persistent SSH** via OpenSSH ControlMaster (`SSHChannel(persist=True)`) —
  ~10× fewer per-call handshakes on a LAN host (#181, #182).
- Opt-in **password auth** for the *target* SSH channel via SSH_ASKPASS
  (dependency-free; the appliance channel stays key-only), configured with
  `ssh_password` / `KVM_PILOT_SSH_PASSWORD` (#183).
- **Router wired into the CLI** — `kvm-pilot route <command>` prints the interface
  the router picks (from a cached per-device scorecard, re-benchmarking the KVM
  plane if the firmware changed); `kvm-pilot host-exec <cmd>` runs a command on
  the managed host's OS via the fastest capable **in-band** interface (ssh/winrm),
  auto-selected; `benchmark --save` persists the scorecard (#181).
- **Online learning** — `Scorecard.record()` folds each real call's latency +
  outcome back into the scorecard (EWMA) so the router self-tunes with use (#181).

## [0.1.0a13] — 2026-07-08

The headless-vision release. GL's video encoder is on-demand — it runs only while
a video client is connected — so a headless `snapshot`/`classify`/`watch` (the
flagship AI-agent case) 503'd forever on an idle unit. This release makes it work:
the driver starts the on-demand encoder itself over kvmd's WebSocket and keeps it
warm, plus honesty fixes for the snapshot-503 and power-on-unwired-ATX messages —
all grounded in a full live reliability sweep of the GL-RM1PE fleet (`.11`/`.20`
V1.9.1, `.39` V1.5.1; findings in #176). Per the support matrix this line stays
**alpha**: only GL-RM1PE is live-validated (V1.9.1 reaches beta-grade there), while
the other drivers remain emulator-only.

### Added
- **Headless `snapshot` now works on idle GL units — auto-recovers the on-demand
  streamer** (#142): GL runs the video encoder only while a video client is
  connected, so a headless `snapshot`/`classify`/`watch` 503'd forever (the
  flagship AI-agent case). On a `streamer: null` 503, the GLKVM driver now registers
  a stream client over kvmd's `/api/ws`, waits for the encoder (~1.5s), and retries —
  returning a real frame. Verified live: `snapshot` recovers a valid JPEG on RM1PE
  V1.9.1 (.11/.20); on V1.5.1 (.39) the streamer starts but emits H.264, correctly
  surfaced as the honest #107 error, not a false success.
- **`GLKVMDriver.streamer_warm()` keep-alive** (#142): a context manager that holds
  the stream client open in a background thread so the encoder stays warm for the
  whole block — every `snapshot` inside is instant (measured ~0.1s vs ~5s cold),
  no vision/LLM needed. `watch` and the MCP `wait_for_state` loop now wrap their
  polling in it, so vision-driven flows never 503 on a cold streamer mid-poll.
- **`websocket-client` promoted to a base dependency** (was the opt-in `ws` extra):
  headless GL snapshot is a core capability that needs it (#142). `kvm-pilot[ws]`
  stays as a no-op alias for back-compat. The `events` command now works out of the box.
- **GLKVM quirk `snapshot-needs-video-client`** (observed, #142/#173): documents
  that the video encoder is on-demand and headless `snapshot`/`classify`/`watch`
  are unavailable with no active video client — surfaced by `healthcheck`.
- **Driver-features reference** (`docs/driver-features.md`, #171) and a reusable
  **hardware reliability test plan** (`docs/test-plan.md`, #172), both published to
  the wiki (registered in `build_wiki.py` `PAGES`). The 2026-07-07 sweep evidence is
  recorded in the run ledger (maturity re-derived).

### Fixed
- **`snapshot` 503 no longer misdiagnoses an idle on-demand streamer as a wedged
  encoder** (#173): with `streamer: null` (on-demand, no video client) the
  `_snapshot_unavailable_detail` explanation fell through to "HDMI present and
  capturing … encoder wedged (#142)", sending the operator to an appliance reboot
  that isn't the fix. It now leads with an honest `streamer_offline` branch — "the
  on-demand streamer is not running; `/api/streamer/snapshot` does not start it on
  GL — open the WebRTC stream/web console or use trigger-then-wait (#142)." The
  `video-signal` healthcheck remediation was corrected the same way (it previously
  claimed "a snapshot will start it", false on GL). Reproduced + verified live on
  the .11/.20 (V1.9.1) and .39 (V1.5.1) fleet.
- **`power`/`power-cycle` on an unwired ATX now fail with a clear message instead
  of an opaque `HTTP 500`** (#174): GL units report `/api/atx` `enabled=false`
  (no power cable to the host header), and a power POST then 500s with "Server got
  itself in trouble". The driver now preflights `atx.enabled` (only on a call it is
  really about to send — dry-run/denied calls still touch nothing) and raises a
  clear `CapabilityError` pointing at `kvm-pilot paths`. Verified live on all three
  units.

## [0.1.0a12] — 2026-07-07

The resilient-access release. An independent, opt-in appliance-SSH transport
that can observe and recover the RV1126 encoder wedge the REST API can't see;
self-healing healthcheck AutoFixes and auto keep-awake for the common access
failures; a lockout-exposure map; and reliability hardening — fast-fail on a
wedged device, honest video-signal reporting, and multi-frame vision consensus.
Every fix was grounded in, and validated on, a live GL-RM1PE fleet — and the
reliability work was reprioritized by testing each assumption on real hardware
(the scariest "landmines" turned out to be Redfish-shaped and unreproducible on
GL, so they're tracked, not shipped blind).

### Fixed
- **`video-signal` healthcheck no longer reports a false "live" on an idle
  streamer** (#165): when the on-demand streamer has no subscriber, `/api/streamer`
  returns `streamer: null` and `has_video_signal()` fails open to True, so the
  check reported "Video signal: live" on a possibly-dark target. `video_signal_info()`
  now exposes `streamer_offline`, and the check reports an honest INFO "capture
  subsystem idle — signal unconfirmed; a snapshot will start it." Reproduced +
  verified live on the bench. (`has_video_signal()` keeps its fail-open True — it's
  the correct *gate* to attempt a snapshot, which reveals the truth.)

### Added
- **Multi-frame consensus in `wait_for_state`/`watch` — don't act on one transient
  frame** (#166): the wait loop returned on the *first* qualifying poll, and that
  frame flowed out as `frame_ref` into a positional `mouse` act — so a single
  transient frame (the live-seen black/near-black mid-boot screen, or a one-off
  model misread) could end a wait and drive a click. It now requires the target
  phase on `consensus` (default 2) consecutive **model-classified** polls; a
  deterministic device-state cheap gate (`power_off`/`no_signal`, no frame
  classified) stays authoritative and returns immediately. `consensus=1` opts out.
- **Consecutive-failure retry damper — fast-fail a wedged device** (#164): a
  no-signal/wedged unit made every HTTP call burn `max_retries+1` attempts ×
  backoff (**3.8s measured live**), and an agent poll loop repeated that with no
  inter-call damping. `HTTP` now counts consecutive transport-down failures
  (503/timeout) and, past `breaker_threshold` (default 3), drops to a single
  attempt until the device responds — any 2xx (or definitive 4xx) resets it.
  Verified live: a poll loop on a down unit dropped from 3.8s/call to 0.07s/call
  once the damper opened. Not a distributed/half-open breaker — a per-host counter.
- **Access-path map — the lockout-exposure view** (#162): `kvm-pilot paths`
  (and MCP `access_paths`) rolls up which independent recovery paths are live per
  device — REST, appliance-SSH, target-SSH, out-of-band power, console-HID — each
  labeled by its failure *domain* so redundancy isn't oversold: several live paths
  that all ride the same appliance count as **one** independent domain.
  `out_of_band_live=false` means every path shares the appliance's fate (verified
  live: a unit showing "2 paths live" is really 1 domain with no OOB power).
- **Auto keep-awake for sustained vision/wait sessions** (#161): a
  `client.display_awake()` context manager wraps the CLI `watch` and MCP
  `wait_for_state` poll loops so the target display can't DPMS-sleep and blind
  the loop mid-wait; it restores the prior jiggler state on exit (even on error).
- **MCP appliance tools** (#162): `appliance_status` (read-only load/D-state
  diagnostics) and `appliance_reboot` (gated behind `KVM_PILOT_MCP_ALLOW_APPLIANCE`
  + `confirm=true`) surface the appliance-SSH channel to agents.
- **Appliance-SSH channel — an independent transport that can see/recover the
  encoder wedge REST can't** (#162): an opt-in, **key-based** SSH channel to the
  KVM appliance's OWN OS (root@`<kvm-ip>`, `appliance_ssh`/`appliance_ssh_key`),
  distinct from `ssh_host` (the target). A new REST-based `encoder-wedge`
  healthcheck flags the GL RV1126 hard-loop (kvmd repeatedly failing to init the
  encoder → snapshot 503s) from the kvmd log — keyed on **function, not
  loadavg**, which sits at ~10 on these units even when perfectly idle (measured
  live: load self-inflates to the D-state-thread count with zero interaction).
  `kvm-pilot appliance loadavg|reboot` exposes read-only diagnostics and a
  gated `appliance.reboot` recovery (new `EffectClass.APPLIANCE_RESET`) — the
  only way to clear the wedge (the stuck threads are unkillable kernel threads).
  Recovery is operator-gated and never autonomous (the wedge recurs and there is
  no out-of-band power to the appliance). Key-based only — no sshpass, no
  reusing the kvmd password over SSH.
- **Self-healing healthcheck AutoFixes for the access failure modes** (#160, #161):
  `check_hid-reachable`'s offline branch now offers a reversible
  `recover_hid()` AutoFix (reset + re-enumerate the USB HID gadget, via
  `kvm-pilot recover-hid`) — the recoverable half of the #155 write-select
  fault. `check_video_signal` now distinguishes **"Display asleep (target on)"**
  (no signal but the HID gadget is attached, so the target is powered and its
  display DPMS-slept — the real root of #126/#142) from a plain no-signal, and
  offers a `keep-awake` AutoFix to wake it. Both apply under `healthcheck --fix`.
- **`keep-awake` — toggle the target-display jiggler** (#159): `kvm-pilot
  keep-awake on|off` (and `client.set_jiggler(active)`) drives kvmd's mouse
  jiggler so the target's display doesn't DPMS-sleep out from under a
  vision/snapshot session. An asleep display reports `hdmi.signal=false` and the
  snapshot path fails — the actual root cause of the #126/#142 "snapshot 503s
  even though video works" reports, confirmed live: with the display asleep
  snapshot returns H.264/503; with the jiggler holding it awake at 1600x900,
  snapshot returns a clean JPEG. Benign HID movement (no click/key), so ungated.

## [0.1.0a11] — 2026-07-07

The honest-sensor release: kvm-pilot now correctly diagnoses real GLKVM
hardware states — video signal, capture geometry, frame staleness, and HID
reachability — instead of confusing itself. Every fix was found and verified
live on real GL-RM1PE units (V1.5.1 and V1.9.1).

### Fixed
- **Video liveness reads `streamer.hdmi.signal`, not `source.online`** (#154):
  on GL firmware `source.online` stays True with no picture, so kvm-pilot
  reported "video signal live" when there was none and the vision loop 503'd on
  a snapshot instead of cheaply concluding `no_signal`. `has_video_signal()` now
  prefers `hdmi.signal`; `video_signal_info()` exposes `hdmi_signal` +
  `streamer_idle`; the snapshot-503 diagnostic names the real cause (no signal /
  idle on-demand streamer / wedged encoder). Found and verified live on a bench
  GL-RM1PE. Also nulls the **stale** resolution/fps `video_signal_info()`
  returned when there is no signal (the `resolution` dict keeps the
  last-negotiated mode; `captured_fps` spins on no-signal) — honoring V1.9.1's
  authoritative `source.real_resolution == "no_signal"`; verified live on a
  V1.9.1 unit (#158).

### Added
- **Mouse click refuses a stale-by-age or non-server-issued `observed_frame_ref`**
  (#141): frame generation only bumps on power/media, so an observation could go
  stale while the screen changed on its own (boot progressing, a placeholder
  frame persisting) and still be carried into a click. The `mouse` tool now also
  refuses an observation older than `KVM_PILOT_MCP_FRAME_MAX_AGE` (60s default)
  or any `frame_ref` this server did not issue.
- **Healthcheck `hid-reachable` check** (#155): reads `/api/hid` and warns when
  the KVM's emulated keyboard/mouse is not reaching the target (`connected:
  false`) — a USB OTG/HID cable or port-role fault where keystrokes and clicks
  are generated but silently dropped, which reads as "the target ignores input"
  or is misread as "target powered off." Names the cable/port remediation.
  Pairs with `video-signal` to disambiguate target-off vs display-asleep vs
  HID-unreachable. Diagnosed and verified live on a bench GL-RM1PE.

## [0.1.0a10] — 2026-07-07

Support-matrix subsystem (what's actually been proven on real hardware), a
server-side screen-phase wait, and field-report fixes — all built and
adversarially reviewed by a multi-agent workflow.

### Added
- **MCP act denials explain client-side approval failures** (#149): when an
  elicitation approval is cancelled (`denied_reason: "approval cancel"` — a
  new chat message dismisses the pending prompt), declined, or denied by the
  approver (a mis-click does this), the act result now carries a
  `remediation` field saying what happened — the approval died client-side,
  the action never reached the device, read-only tools are unaffected — and
  naming the operator's escape hatch: `KVM_PILOT_MCP_ELICIT=off` (the
  `ALLOW_*` effect gate + per-call `confirm=true` become the standing
  authorization; trade-off: no per-call human approval). Matching
  troubleshooting entry in the MCP README and agent guidance in the bundled
  skill. No gate is weakened; the repeat-failure hint and typed
  cancelled-vs-denied outcomes remain open on #149/#72.
- **Unattended Linux installs guide** (#129): `docs/unattended-install.md` —
  prefer text mode + SSH over driving a graphical installer via KVM HID, with
  the per-distro boot-arg matrix (Anaconda `inst.sshd`/`inst.text`, d-i
  network-console, Subiquity autoinstall, linuxrc `ssh=1`, Arch/Alpine live
  shell) and the SSH handoff — plus the bundled skill's compact install-time
  rule. The detect-installer automation half is tracked in the Reflexes
  `unattended-install` playbook (#122).
- **MCP `support_matrix` tool + `support-evidence` healthcheck finding**
  (#102, part of #96): a read-only, offline lookup of what has actually been
  exercised on real hardware, per `(vendor, product, firmware_version)` ×
  capability — pass/fail counts, last outcomes, a `never_exercised` list, and
  the #98-derived maturity level joined from the shipped registry. The
  `capabilities` tool now carries a driver-granular `live_evidence`
  annotation, and `healthcheck` gains a `support-evidence` finding naming what
  has (and has NOT) been live-verified on the exact device+firmware (WARNING
  when a recorded live attempt failed, e.g. RM1PE V1.5.1 `firmware_update`,
  #94/#95). The run ledger now ships in the wheel
  (`src/kvm_pilot/data/test_runs.jsonl`, moved from `data/`), so the evidence
  travels with `pip install kvm-pilot`.
- **Support-matrix maturity, derived from the run ledger** (#98, part of #96):
  `kvm_pilot.maturity` computes per-capability and overall levels
  (alpha/beta/rc/ga) for every `(vendor, product, firmware_version)` combo
  from live runs in `src/kvm_pilot/data/test_runs.jsonl` and folds them into
  the shipped registry's new additive `versions[].maturity` rows (schema
  stays v2). Levels are derived, never hand-set — a CI test re-derives them
  from the ledger and fails on drift.
- **MCP `wait_for_state` tool** (#147 — the last #61 acceptance criterion):
  block (bounded, 300 s server-side cap) until the screen reaches a named
  boot/run phase, the MCP twin of CLI `watch`. Validates the phase token up
  front, emits MCP progress per poll, returns the final `frame_ref` for a
  follow-up `mouse` click, and returns a same-path `reached: false` result on
  timeout. With no server-side vision key it fails fast pointing at
  `classify_screen` polling instead of burning the timeout; cheap-gate phases
  (power_off/no_signal/boot-progress) still wait keylessly.
- **One-command dev emulator stack** (#21): `compose.yaml` + `Makefile`
  (`make emulators` / `make integration` / `make emulators-down`) stand up the
  Redfish reference emulator on loopback, and a new CI `emulator-stack` job
  runs the integration tests through it so the dev stack can't drift from what
  CI validates.

### Fixed
- **Support-matrix evidence now counts real-hardware runs only** — a synthetic
  / emulator run no longer contributes a pass, a fail, or an "exercised" mark
  to the `support_matrix` tool, the `capabilities` `live_evidence`, or the
  `support-evidence` healthcheck (matching what `maturity` promotes on). The
  healthcheck matches the exact device+firmware, so it can no longer report a
  sibling model's evidence (an `RM1` row under an `RM1PE` device). Found by the
  batch's adversarial review, along with a keyless `wait_for_state` gating gap,
  a NaN-timeout hole, a CI-drift-gate crash on a malformed ledger date, and a
  registry fold that dropped hand-authored version rows — all fixed with tests.

## [0.1.0a9] — 2026-07-06

Post-a8 sweep release: field-report fixes from the first Comet sessions, a
driver split that gives the GL fork its own home, and observability so
vision-driven agents reason from data instead of guessing.

### Fixed — trust what the device tells you
- **GLKVM quirk auto-detection matched the GL product firmware but never the
  kvmd component version** (#139) — on real hardware the observed
  `atx-power-state-always-off` quirk silently stopped applying, and the
  healthcheck lost its ATX-untrustworthy warning. Quirks now match against
  every version the device reports.
- **`firmware-update` no longer reports success on a no-op flash** (#94): after
  `POST /api/upgrade/start` the device must visibly enter an upgrade state
  (status transition or channel drop) within a bounded window, or the call
  returns `sent: false` with the raw start response.
- **`snapshot()` validates the JPEG magic bytes** (#107) instead of trusting
  the Content-Type — RM1PE firmware has returned raw H.264 labeled as JPEG;
  that now raises a typed `SnapshotFormatError`.
- **`mount_iso()` verifies the media actually goes online** (#77): GLKVM
  accepts the mount calls while the host sees no device; a mount that never
  reports `online` now raises `MediaOfflineError` naming the GL-side toggle.

### Added — observability + act-surface completion
- **MCP `snapshot` payload carries the live `signal` state**
  (online/resolution/fps/format) **and `unchanged_since_last_snapshot`**
  (#141/#143): a byte-identical frame across an expected screen change is the
  staleness tell the generation mechanism can't catch. Snapshot 503s are
  re-raised with the streamer state attached, separating "no video signal"
  from "signal fine, JPEG path failing" (#142).
- **Healthcheck `driver-identity` check** (#145): GL firmware self-reports as
  a stock Raspberry Pi PiKVM, so a GL unit on a `driver = "pikvm"` profile
  silently loses every GL protection — the healthcheck now fingerprints GL's
  proprietary `/api/upgrade/version` and warns with a set-`glkvm` remediation.
- **MCP `list_virtual_media` tool + CLI `media-list`** (#127): inventory the
  KVM's MSD storage before asking anyone to download or upload an ISO that may
  already be on the device.
- **CLI `mouse-move` / `click`** (#124, percent coordinates by default — the
  #128 field report's fix) and **CLI `key` chord support**
  (`ControlLeft+AltLeft+F2`, #112), completing the CLI half of the a8 act layer.

### Changed — GL fork separation + CI gates
- **`GLKVMDriver` moved to its own `drivers/glkvm.py`** (#140) with a module
  docstring enumerating how the GL fork diverges from stock PiKVM;
  `drivers/pikvm.py` keeps the no-delta forks (BliKVM) and re-exports the
  moved symbols for this release. Public import paths are unchanged.
- **CI now gates on 75% coverage**, caches pip, and smoke-builds the wiki on
  PRs (#56).
- Docs: new canonical [CLI reference](docs/cli.md); the configuration
  reference and `.env.example` now document every a7/a8 env var (MCP effect
  gates, profile allowlist, vision backend, SSH channel); stale
  "act tools not exposed yet" / "never run on real hardware" claims corrected
  across SECURITY.md, the skill, and module docstrings (#146).

## [0.1.0a8] — 2026-07-06

### Added — MCP act layer: drive the box, not just observe it (2026-07-06, #61, #112)
- **New MCP tools** `type_text`, `press_key`, `send_shortcut`, `ctrl_alt_delete`,
  `mouse`, `mount_iso`, `eject` — an agent can now *drive* HID/media over MCP, not
  only observe. Authorized by **two guarantees**: an operator effect-gate env flag
  (classified by *effect, not transport*, so Ctrl+Alt+Del and reboot chords need
  `KVM_PILOT_MCP_ALLOW_POWER`, ordinary HID needs `KVM_PILOT_MCP_ALLOW_HID`, media
  needs `KVM_PILOT_MCP_ALLOW_MEDIA`) **and** a per-invocation approval — MCP
  elicitation when the client supports it, else `confirm=true` under a standing
  policy (the unattended path). Fail-closed `KVM_PILOT_MCP_PROFILES` allowlist.
  Denials return through the same call path; each result carries a stable
  `invocation_id` + transport + effect. Effect taxonomy is additive over
  `DESTRUCTIVE_OPS` (`EffectClass` in `safety.py`); see `mcp/act.py`.

### Added — mouse with a generation-keyed staleness gate (2026-07-06, #124)
- **`mouse`** — absolute move + optional click (`percent` coords by default,
  resolution-independent). A click must carry the `observed_frame_ref` from a prior
  `snapshot`; it is refused if the host rebooted or swapped media since (the frame
  *generation* changed), so a click can't land on a stale screen. `snapshot` now
  returns a `frame_ref`.

### Added — keyless `classify_screen` (2026-07-06, #125)
- `classify_screen` falls back to **caller-side** classification (returns the
  screenshot + prompt/schema) when the server has no vision key.

### Added — SSH install-time hand-off (2026-07-06, #81)
- CLI `--ssh-host` / MCP `host=` runtime override (use a target's DHCP IP without
  editing config); a volatile `ssh-reachable` healthcheck; and a guided
  `kvm-pilot ssh-bootstrap` command that reads the target IP off an installer
  console and starts `sshd` (plans by default; the IP probe doubles as a console
  canary and aborts before any command if it doesn't echo; success requires an
  `ssh_exec` auth-probe).

## [0.1.0a7] — 2026-07-04

### Added — first-run onboarding + agent recovery guidance (2026-07-04, #111)
- **`docs/getting-started.md`** — an agent-first first-run guide: install `--pre`,
  enabling the `kvm-pilot-mcp` server in your agent (including the exit/relaunch-your-
  session + activation-prompt steps), in-agent vs profile credentials, the
  KVM-appliance-vs-connected-server distinction, a sample-prompt library, a "what a
  good first run looks like" example, and a Tips & tricks section.
- **Skill recovery guidance** (`SKILL.md`): prefer **remote recovery before physical
  intervention** (SSH into the target OS — ask the user for its IP/host/FQDN; offer a
  risky opt-in sweep as a fallback); how to read a black screen while `powered_on`
  reads True on an untrusted-power device; and proactively offering tips to a
  likely-new user.

### Added — in-band SSH-to-target channel (2026-07-04, #81)
- **SSH to the managed host's OS** — a new per-profile channel (targets the host
  *behind* the KVM, not the appliance) so an agent can probe reachability and run
  recovery commands once the target OS is on the network, and prefer remote
  recovery over asking a user to physically intervene (surfaced by the #111
  first-run experience). CLI `ssh-check` (read-only) / `ssh-exec` (destructive,
  gated); MCP `ssh_reachable` (read-only) / `ssh_exec` (destructive, disabled
  unless the operator sets `KVM_PILOT_MCP_ALLOW_SSH`).
- Config: `ssh_host` / `ssh_user` / `ssh_port` / `ssh_key` profile keys +
  `KVM_PILOT_SSH_*` env (separate from the KVM appliance creds). Zero new
  dependencies — reachability is a stdlib `socket` probe, exec shells out to the
  system `ssh`; every exec is gated via the new `ssh.exec` destructive op. Modeled
  as a `Capability.SSH` / `RemoteShell` seam, not a KVM-driver capability (see
  `docs/decisions.md`).
- **Opt-in SSH host discovery** — CLI `ssh-discover <CIDR>` / MCP `ssh_discover`
  scan a user-provided range for an open SSH port to help find a target whose
  address the user doesn't know. **Risky/opt-in by design**: never a default,
  bounded to ≤1024 hosts, prints a warning, and the MCP tool requires `confirm=true`.

## [0.1.0a6] — 2026-07-03

### Changed
- **CI now runs a clean-room `--pre` install smoke test** (#110): a fresh venv
  `pip install --pre`s the built wheel and asserts the bundled MCP server imports
  and the skill + both console scripts ship — the check the editable dev installs
  couldn't do. It also runs as a **pre-publish gate in the release workflow**
  against the exact artifact, so a broken `--pre` dependency resolution can't reach
  PyPI. No library changes in this release.
- Updated the release-workflow guard test to allow the new `smoke-install` gate in
  `publish.needs` (it still requires `build` + `test`).

## [0.1.0a5] — 2026-07-03

### Fixed
- **MCP server import broke on a fresh `pip install --pre kvm-pilot`** (#110). The
  uncaught `mcp>=1.10` let `--pre` pull the `mcp` 2.x **beta** into a clean env,
  and mcp 2.x relocated `mcp.server.fastmcp` (`FastMCP`/`Image`) — so the bundled
  server raised `ModuleNotFoundError` out of the box. Capped `mcp>=1.10,<2` (a4's
  editable dev/CI installs missed it because they don't pass `--pre`). Caught by a
  clean-room install of the published wheel.

### Changed
- **Skill clarity for agents:** the bundled `SKILL.md` now enumerates the 8 MCP
  tools explicitly (with the vision-backend and `power`-gate caveats), and its
  front-matter description drops the stale "never validated on real hardware."

## [0.1.0a4] — 2026-07-03

Batteries-included packaging: `pip install kvm-pilot` now installs the whole
product — CLI, the bundled Claude skill, and the MCP server (#109, part of #7).

### Added
- **`kvm-pilot-mcp` console script** — the MCP server now ships in the wheel and
  launches via `kvm-pilot-mcp` (or `python -m kvm_pilot.mcp.server`); no separate
  clone or `requirements.txt` step.
- The **bundled Claude skill** (`SKILL.md`) ships as package data under the
  installed package.

### Changed
- **New packaging rule:** `pip install kvm-pilot` installs every user-facing
  surface. The MCP server moved to `src/kvm_pilot/mcp/`, the skill to
  `src/kvm_pilot/skill/`, and **`mcp>=1.10` is now a base dependency** (the
  client/driver code still imports only the stdlib — `mcp` is imported only in the
  server subpackage). `totp` / `ws` remain opt-in extras. The prior "core = zero
  runtime dependencies" framing is updated accordingly across the docs.
- Removed `mcp_server/requirements.txt` (its deps are declared in `pyproject`; it
  also still pinned the yanked `0.1.0a1`).

## [0.1.0a3] — 2026-07-03

Docs-only release: corrects stale honesty claims now that the project has touched
real hardware, and refreshes the PyPI long description (a2's page was immutable).

### Changed
- **Corrected the "never run on real hardware" claim** across `CLAUDE.md`,
  `skill/SKILL.md`, and `mcp_server/README.md` — now stating the honest posture
  (largely mock-only; a few combos exercised live on the RM1PE; remote flash a
  known no-op #94/#95) with the
  [Hardware-Compatibility list](https://github.com/DustinTrap/kvm-pilot/wiki/Hardware-Compatibility)
  as the source of truth (docs half of #103).
- **Refreshed `README.md`** (PyPI's long description), which still described a1:
  updated status to a2's real-hardware reality, `--pre` install instructions,
  Redfish/BMC in the tagline, and a Compatibility table reflecting the live
  RM1PE runs (#107) instead of "nothing verified."

## [0.1.0a2] — 2026-07-03

Second **alpha**, and the **first build exercised against real hardware** (a
GL-RM1PE). Still alpha, still loud about it — the flash path is a proven no-op on
that unit and much of the driver surface remains mock-only.

### Added — first real-hardware validation + community hardware-compatibility list (2026-07-03, #105/#106/#107)
- **First real-hardware runs** against a GL-RM1PE (10.0.1.20). Read paths (`info`,
  `snapshot`, `healthcheck`, `logs`, `power_state`) verified live on firmware
  V1.5.1 release2 and V1.9.1 release1; `firmware-update`'s remote flash confirmed a
  **live no-op** on this model (start POST accepted, nothing flashed —
  [#94](https://github.com/DustinTrap/kvm-pilot/issues/94)/[#95](https://github.com/DustinTrap/kvm-pilot/issues/95)).
  This retires the repo's blanket "never run on real hardware" caveat for the
  glkvm read/snapshot surface.
- **Community Hardware-Compatibility list.** A git-native run ledger
  (`data/test_runs.jsonl`) feeds an auto-generated
  [Hardware-Compatibility](https://github.com/DustinTrap/kvm-pilot/wiki/Hardware-Compatibility)
  wiki page — pass-rate × sample-count per (vendor, product, firmware) × capability,
  gated at n≥3 before a cell shows a verdict.
- **Field finding [#107](https://github.com/DustinTrap/kvm-pilot/issues/107):** on
  RM1PE the `snapshot` endpoint returned an H.264 frame (not JPEG) above 1080p on
  V1.5.1 — **fixed by the V1.9.1 firmware** (now serves a cached JPEG). The RV1126
  encoder still wedges (D-state, load ~10) above 1080p regardless; keep RM1PE
  guests at a true 1080p.

### Added — MCP interface parity + skill interface-selection guidance (2026-07-03, #93)
- **`logs` MCP tool** (read-only, `Capability.LOGS`) — exposes the device/host
  event log over MCP with a `seek` lookback (tail-follow omitted; it blocks over
  the synchronous transport). This is the text diagnostic behind a `snapshot`
  503 (e.g. a stuck encoder) that the image tools can't give — previously it was
  reachable only from the CLI.
- **`capabilities` MCP tool** (read-only, **structural/offline** — no network,
  no preflight) — lists what the target driver supports so an agent can pick the
  right interface up front. The server's `_driver()` helper gains
  `capability=None` (skip the gate for meta tools every driver serves) and
  `preflight=False` (skip the on-connect audit for offline tools).
- **Skill reframed from "MCP-preferred / CLI-fallback" to best-interface-per-
  action.** `skill/SKILL.md` now carries a per-action **interface matrix**, the
  **host-vs-appliance** distinction (rebooting the KVM box is out-of-band SSH,
  not `power`), **`snapshot`-failure reads** (503 → `logs` → appliance reboot;
  tiny-frame-with-signal → H.264-at-native-res → stream), a **multitasking**
  section (run read-only interfaces in parallel; never parallelize destructive
  ops), and a fix for the MCP `-s local` scope gotcha. Mirrored into
  `mcp_server/README.md`. This is the operator-interface complement to the
  sensing (#13) and actuation (#81) hierarchies.

### Added — gated remote firmware update (2026-07-03, #92)
- **`kvm-pilot firmware-update`** — assesses (and, with `--execute`, performs) a
  remote flash of the KVM's own firmware. Read-only by default: prints
  installed→latest, a per-model reliability assessment, and the planned
  `/api/upgrade/*` steps, sending nothing. Prefers a local image (`--image` →
  `POST /api/upgrade/upload`) over the online path, ejects virtual media first
  (`gl-inet/glkvm#120`), and **refuses to execute on a device with no out-of-band
  recovery path** unless `--i-have-physical-access` is given.
- **`FirmwareUpdate` capability** (`drivers/base.py`) implemented by `GLKVMDriver`
  (`get_upgrade_status`, `apply_firmware_update`), routed through the new
  `firmware.flash` destructive op; defaults to `dry_run`. GL's `/api/upgrade/*`
  shapes are reverse-engineered (no vendor spec) and the execute path is
  **unverified on hardware**.
- **Healthcheck now offers the update.** When firmware is stale and the registry
  `profile.remote_update.supported` is set, the "Firmware update available" finding's
  remediation names `kvm-pilot firmware-update` and states the risk (still a WARNING;
  the healthcheck never flashes). Per-model risk is data in the registry profile
  (`remote_update`), not code. See [`docs/firmware-update.md`](docs/firmware-update.md).

### Added — preflight on first connection + firmware registry (2026-07-02 batch, #80)
- **Preflight healthcheck runs on first connection**, not only ahead of a
  destructive op. Read-only intake (CLI reads, MCP read tools) audits the device
  and surfaces findings without blocking; destructive paths still enforce the
  gate (`health.preflight_once`). The operating docs (`skill/SKILL.md`,
  `mcp_server/README.md`, `CLAUDE.md`) now require it as the first-contact step.
- **Firmware registry** (`src/kvm_pilot/data/firmware_registry.json`, schema v2)
  — the single source of truth for firmware currency and a device's capability /
  UX profile, keyed by `(vendor, product)` from each driver's normalized
  `get_firmware_info()`. Generic across PiKVM/GLKVM/Redfish (iDRAC/iLO/XCC) and
  future IPMI/AMT. New checks `check_firmware_currency` (ordered `_vercmp`:
  update-available / known-bad ranges / quiet when current) and
  `check_capability_profile` (mouse / vmedia / power-trust / video).
- **GLKVM reports the GL product firmware** the UI shows (`V1.9.1 release1
  (RM1PE)` via `/api/upgrade/version`), not just the kvmd component version.
  `get_firmware_info()` now returns `{vendor, product, version}` for the PiKVM
  family and Redfish; fixes the GLKVM `system.platform` path that returned
  `model=null`.
- **Reconcile loop** — `GLKVMDriver.get_available_update()` (GL's
  `/api/upgrade/compare`) + `firmware_registry.reconcile()` diff a device-reported
  latest against the SSoT; `kvm-pilot firmware-check` prints the currency verdict
  and a ready-to-contribute report. The fleet keeps the registry current.
- **Automated ingestion** — a `firmware-report` GitHub Issue Form and an
  hourly-capped workflow that no-ops cheaply when idle, dedups via the `ingested`
  label + idempotent merge, validates untrusted issue bodies as data, and opens
  one batched PR (degrading gracefully when Actions can't open PRs).
- **Distribution** — the registry ships bundled (offline default); loader
  precedence `KVM_PILOT_FIRMWARE_DB` > user cache > bundled for an opt-in refresh.
- New CLI subcommands: `healthcheck`, `firmware-check`.

### Changed — safety & transport (2026-07-01 deep-review batch)
- **Breaking:** `snapshot()` / `snapshot_save()` / `snapshot_base64()` lost the
  `quality` parameter — kvmd silently ignored `preview_quality` without
  `preview=1`, so it was a no-op lie (see `docs/decisions.md`).
- **Breaking (behavior):** HID input (`type_text`, `press_key`,
  `send_shortcut`, `key_event`, `mouse_click`) and MSD uploads
  (`msd_upload_file`, `msd_upload_url`) are now gated destructive ops
  (`hid.*`, `msd.write`, `msd.write_remote`). `--dry-run` really skips them;
  the CLI prompts for them without `--yes`.
- **Breaking (behavior):** `SafetyPolicy.guard` evaluates dry-run BEFORE the
  confirm callback — `--dry-run` never prompts and works unattended.
- Transports map read-phase socket failures (timeouts, resets,
  RemoteDisconnected, IncompleteRead) into `kvm_pilot.errors`
  (`TimeoutError`/`ConnectionError`) instead of leaking raw builtins, and
  never auto-retry a non-idempotent request after a failure that may already
  have reached the device (a lost response can't power-cycle a box twice).
- **Release safety:** the PyPI publish path now runs the full test suite
  (ruff/mypy/pytest) and verifies the built artifact version matches the release
  tag before publishing — a release cut from a red commit, or a tag that
  disagrees with `__about__.py`, fails instead of silently shipping
  ([#57](https://github.com/DustinTrap/kvm-pilot/issues/57)).
- **Supply chain:** every GitHub Action is pinned by full commit SHA (with a
  version comment) — including the OIDC-privileged `pypa/gh-action-pypi-publish`
  on the release path — so an upstream tag compromise can't run arbitrary code
  with PyPI publish rights; added `.github/dependabot.yml` to keep the pins
  fresh, least-privilege `permissions:` defaults, and `persist-credentials: false`
  on checkouts ([#58](https://github.com/DustinTrap/kvm-pilot/issues/58)).
- **Security:** neither HTTP transport follows redirects any more. The stdlib
  default opener would forward auth headers (`X-KVMD-Passwd`, `X-Auth-Token`,
  `Authorization: Basic`, session cookie) to whatever host a 3xx `Location`
  named — defeating the Redfish same-origin guard and exposing the PiKVM
  transport outright. A 3xx is now surfaced as a `ConnectionError`
  ([#37](https://github.com/DustinTrap/kvm-pilot/issues/37)).
- `is_powered_on()` fails open when kvmd reports the ATX subsystem disabled
  (no ATX board): vision classification proceeds instead of reporting
  `power_off` for a running machine.
- **Redfish:** power methods read `PowerState` before issuing a reset — a host
  already at the target gets no reset, and `PushPowerButton` (a state toggle)
  is chosen only when the pulse moves toward the intent, so `power_off` on an
  already-off iDRAC8 no longer powers it back on
  ([#42](https://github.com/DustinTrap/kvm-pilot/issues/42)).
- Redfish: transitional `PowerState` values (`PoweringOn`/`PoweringOff`/
  `Paused`) map to `unknown`, not `power_off`.
- `KVMClient.get_logs(follow=True)` raises `CapabilityError` instead of blocking
  to the timeout and crashing — the blocking transport can't serve tail-follow
  (mirrors the Redfish driver); the kvmd emulator gained an `/api/log` handler so
  the non-follow path is finally covered
  ([#48](https://github.com/DustinTrap/kvm-pilot/issues/48)).
- Vision wait loops back off on repeated errors and honor a 429's `Retry-After`
  instead of re-uploading the image at a fixed 3 s cadence against a
  rate-limited API; `VisionError` now carries `status_code` and `retry_after`
  ([#51](https://github.com/DustinTrap/kvm-pilot/issues/51)).
- Vision: a truncated model response (Anthropic `stop_reason=max_tokens` /
  OpenAI-compat `finish_reason=length`) now raises a specific "truncated"
  `VisionError` instead of a misleading "did not return valid JSON"; the default
  `max_tokens` is 1024 and the prompt bounds `raw_text` to ~500 chars so
  text-dense boot screens stop overflowing it
  ([#49](https://github.com/DustinTrap/kvm-pilot/issues/49)). Anthropic model
  auto-resolution now skips entries whose `capabilities.image_input` is
  explicitly unsupported, instead of blindly taking the first id
  ([#50](https://github.com/DustinTrap/kvm-pilot/issues/50)).
- Vision: `VisionError` is honored on every failure path (non-JSON 200s,
  non-object JSON, raw socket errors); model confidence is clamped/normalized
  (percent-scale answers no longer defeat `min_confidence`); the
  unchanged-frame gate reuses only actionable results, so a static screen
  can't pin a wait loop to a cached `unknown`.
- Config: unknown profile keys warn loudly instead of silently falling back to
  `admin`/`admin`; `KVM_PILOT_PROFILE` is honored everywhere; `--scheme http`
  defaults the port to 80; IPv6 literal hosts work.
- **Platform envelope:** Python 3.14 added to the CI matrix and PyPI
  classifiers, and the default config path is now platform-correct — `%APPDATA%`
  on Windows, `$XDG_CONFIG_HOME` then `~/.config` elsewhere — instead of forcing
  a Unix path while claiming "OS Independent"
  ([#65](https://github.com/DustinTrap/kvm-pilot/issues/65)).
- MSD uploads stream the file instead of reading it all into RAM: `mount_iso`
  / `msd_upload_file` on a multi-GB ISO no longer needs the whole image resident
  (urllib streams it in 8 KiB blocks with a pinned Content-Length), so a small
  jump host or container won't OOM ([#47](https://github.com/DustinTrap/kvm-pilot/issues/47)).
- **Redfish:** `read_sensors()` uses a single `?$expand` request where the
  service advertises it (`ProtocolFeaturesSupported.ExpandQuery`) instead of one
  GET per sensor — real BMCs expose 100+ sensors, so the fan-out was 10s of
  seconds to minutes. Falls back to per-member fetches when unsupported
  ([#45](https://github.com/DustinTrap/kvm-pilot/issues/45)).
- **Redfish/cross-driver:** `Logs.seek` is now uniformly "seconds of lookback"
  (kvmd's semantics). The Redfish driver was treating it as an entry-skip index,
  so `get_logs(seek=3600)` returned different data per driver; it now filters
  `LogEntry.Created`, keeping stampless and unset-RTC entries
  ([#46](https://github.com/DustinTrap/kvm-pilot/issues/46)).
- **Redfish:** chassis and manager are resolved from the ComputerSystem's
  `Links.Chassis`/`Links.ManagedBy`, not by indexing the global collections, so
  sensors/logs/virtual-media can't come from a different node than power ops on
  multi-node gear; an out-of-range `system_index` is now a hard error, and the
  reset confirm prompt names the target system
  ([#44](https://github.com/DustinTrap/kvm-pilot/issues/44)).
- **Redfish:** InsertMedia now sends only `Image` — the optional
  `Inserted`/`WriteProtected` params that strict BMCs (Supermicro) reject are
  gone — and retries once with `TransferProtocolType` for BMCs that require it
  ([#43](https://github.com/DustinTrap/kvm-pilot/issues/43)).
- **Redfish:** a session-mode `401` now triggers a one-shot re-login and retry,
  so an expired/evicted session (idle timeout, BMC reboot) or a token cleared by
  `close()` recovers transparently instead of failing every subsequent request
  ([#41](https://github.com/DustinTrap/kvm-pilot/issues/41)).
- **Redfish:** the CLI now closes its driver on exit (success, handled error, or
  capability gate), so a BMC session is DELETEd instead of leaked — repeated
  invocations no longer exhaust the device's session pool and lock operators
  out. All drivers gained a uniform no-op `close()` + context-manager protocol
  on the base ([#40](https://github.com/DustinTrap/kvm-pilot/issues/40)).
- MCP server: capability-aware per-call drivers (closed after every call — no
  leaked BMC sessions), real image snapshots, tool annotations, an
  operator-side `KVM_PILOT_MCP_ALLOW_POWER` gate on the power tool,
  `KVM_PILOT_MCP_DRY_RUN`, and local-VLM support via `KVM_PILOT_VISION_*`.

### Changed (internal)
- `hard_cycle` is now a single `PowerMixin.hard_cycle` (in `drivers/base.py`)
  shared by all three drivers instead of a copy each; per-driver settle delays
  are class attributes (PiKVM 5.0/3.0, Redfish/Fake 0.0). The public
  `hard_cycle(off_delay=, on_delay=)` defaults are now `None` (= "use the
  driver's class attribute"); passing explicit values is unchanged
  ([#63](https://github.com/DustinTrap/kvm-pilot/issues/63)).

### Added (credential hygiene)
- **`--passwd-file` / `--ask-passwd`** (and `--totp-secret-file`) so secrets
  needn't go on argv, where they're visible in `ps` and shell history; the
  `--passwd`/`--totp-secret` help text now says so, and the docs lead with
  env/profile. The config loader **warns** when a file holding a password or
  TOTP secret is group/other-readable (POSIX), matching the ssh/pgpass 0600 bar
  ([#59](https://github.com/DustinTrap/kvm-pilot/issues/59)).

### Added (structured sensing reachable)
- **CLI `sensors` / `logs` / `boot-progress`** — the `Sensors`/`Logs`/
  `BootProgress` capabilities were implemented in drivers but reachable from no
  entry point; they now have capability-gated subcommands (`sensors` is a
  BMC/Redfish capability). `ScreenAnalyzer` also gained a structured-`BootProgress`
  gate that resolves the phase with no snapshot/model call when the driver
  reports it ([#60](https://github.com/DustinTrap/kvm-pilot/issues/60)).

### Added (deep-review batch)
- **TLS pinning** (`#38` decision): `ssl_ca_file=` / `--ssl-ca-file` /
  `KVM_PILOT_SSL_CA_FILE` pins verification to a CA bundle or the device's own
  self-signed cert on every transport (HTTP, Redfish, WebSocket), overriding
  `verify_ssl`. Unverified TLS remains the default (devices ship self-signed
  certs) but now logs a one-time warning naming the alternatives.
- **CLI `eject`** — the inverse of `mount` (gated `msd_disconnect`).
- `mouse_move_pixels(x, y)` — pixel coordinates mapped edge-exactly into
  kvmd's centered −32768…32767 space; `mouse_move` documents that contract.
- `docs/configuration.md` — full config-file and `KVM_PILOT_*` env reference.
- `.github/ISSUE_TEMPLATE/hardware-report.yml` — structured hardware
  success/failure reports.
- `py.typed` ships in the wheel; PyPI metadata covers the Redfish/BMC side.

### Added
- **Sensing model.** A `docs/sensing-hierarchy.svg` diagram and a "Sensing
  model" section documenting why structured/text signals are preferred over
  vision (answer from the cheapest signal the device exposes; escalate to OCR
  and a vision model only as a last resort).
- Forward-looking capability protocols in `drivers/base.py`: `Logs`,
  `BootProgress`, `Sensors`, `SerialConsole`, `Watchdog`. The PiKVM client now
  reports the `Logs` capability (`/api/log`); the rest are the seam for the
  Redfish and IPMI drivers, where boot phase is a structured enum and the
  console is a serial text stream.
- `KVMClient.has_video_signal()` — a cheap "is there a screen?" probe over
  `/api/streamer`, parsed defensively (only `False` on a positive offline
  report).
- `ScreenAnalyzer` gates: `gate_on_power_signal`, `skip_unchanged_frames` (both
  default on) and opt-in `ocr_rules` (with `DEFAULT_OCR_RULES`), plus
  `vlm_calls` / `cheap_resolves` counters.
- **CLI `capabilities`** — print the capabilities the active driver supports
  (offline; no network call).
- **CLI `events`** — stream device events over the WebSocket (`--duration`,
  `--count`, `--no-stream`); requires the `ws` extra.
- **Global `--timeout`** flag (HTTP per-request timeout) plus the matching
  `KVM_PILOT_TIMEOUT` env var; `scheme` now also resolves through the full
  args > env > file precedence with a `--scheme` flag / `KVM_PILOT_SCHEME`.
- `KVMClient.from_config(cfg)` — one constructor for the field-by-field build
  the CLI, MCP server, and examples each previously repeated.
- **Driver registry.** `make_driver(kind, **conf)` (mirroring `make_backend`)
  plus `register_driver()` for third-party kinds; built-in kinds `pikvm` /
  `glkvm` / `blikvm` (the `KVMClient`) and `fake`. A `--driver` CLI flag selects
  among them.
- **`FakeDriver`** (`kvm_pilot.drivers.FakeDriver`) — an in-process,
  hardware-free driver implementing the capability protocols over scriptable
  in-memory state, with destructive ops still routed through `SafetyPolicy`. It
  is the first real implementer of a sensing protocol (`BootProgress`), so the
  capability seam and the safety layer can be exercised end-to-end with no
  hardware. `kvm-pilot capabilities --driver fake` runs fully offline.
- **`RedfishDriver`** (`kvm_pilot.drivers.RedfishDriver`, `make_driver("redfish")`)
  — a stdlib-only DMTF Redfish client for server BMCs (Dell iDRAC, HPE iLO,
  Supermicro, Lenovo XCC, OpenBMC). It advertises a BMC's *complementary*
  capability set — `SystemInfo`, `Power`, `BootProgress`, `Sensors`, `Logs`,
  `VirtualMedia` (no `HID`/`Video`/`GPIO`) — and is **portable by navigating
  hypermedia**: it follows `@odata.id` and reads `@Redfish.ActionInfo` /
  `AllowableValues` rather than hard-coding vendor ids, mapping power intents to a
  target's actual `ResetType` set. Session-auth-first (`X-Auth-Token`, `DELETE`
  on logout) with HTTP Basic optional; handles async `202`/Task responses,
  `PasswordChangeRequired`, the legacy `Thermal`/`Power` vs unified `Sensors`
  models, and structured `BootProgress` → the phase vocabulary. Reset and
  virtual-media insert/eject route through `SafetyPolicy` (new `redfish.*` ops).
  Wired into the CLI via **capability-aware `--driver` dispatch** (#27, PR #34):
  `--driver redfish` works on every applicable subcommand, and a subcommand
  needing a capability the BMC lacks (e.g. `type`, `snapshot`, `events`) exits 1
  with a clean `CapabilityError` message instead of crashing. A new
  `--redfish-auth session|basic` flag (+ `KVM_PILOT_REDFISH_AUTH` env /
  `redfish_auth` profile key) selects the auth mode for endpoints without a
  SessionService (emulators, or BMCs with session auth disabled). Wired and
  unit/emulator-tested only — still not validated against a real BMC.
- New phase token **`os_running`** (`vision.base`) for an OS that has handed off
  but whose specific on-screen state isn't distinguishable — emitted by the
  vision backend and mapped to from a BMC's `BootProgress=OSRunning`.
- **PiKVM driver family.** `KVMClient` was split into a canonical **`PiKVMDriver`**
  base with thin **`GLKVMDriver`** / **`BliKVMDriver`** subclasses (the
  API-compatible forks); `KVMClient` and `PiKVMClient` remain aliases of
  `PiKVMDriver`. The registry maps `glkvm`/`blikvm` to the subclasses.
- **GLKVM (GL-RM1PE) first-contact support.** `GLKVMDriver` detects the GL "API
  disabled by default" condition — a 404 across `/api/*` now raises an actionable
  `ApiDisabledError` pointing at `/etc/kvmd/nginx-kvmd.conf` instead of a bare
  HTTP 404 — plus a `check_api_enabled()` preflight, `get_firmware_info()`, and a
  `known_quirks()` registry (seeded honestly with the one documented quirk; grows
  as real hardware testing reveals release-specific behavior). New
  `errors.ApiDisabledError`.
- **`HostConfig.driver`** field (+ `KVM_PILOT_DRIVER` env / config-file key) so a
  profile can pin its driver; the CLI `--driver` flag overrides it.

### Changed
- **Docs consolidated under `docs/`.** `CONTRIBUTING.md` and `SECURITY.md` moved
  into `docs/` (still recognized by GitHub there), joined by a `docs/README.md`
  index; `skill/SKILL.md` and `mcp_server/README.md` stay next to their code but
  are linked from the index. The GitHub wiki is now an auto-generated, formatted
  mirror of `docs/`, published on every push to `main` by a `wiki-sync` workflow
  (`.github/scripts/build_wiki.py`) — edit the docs, never the wiki.
- `ScreenAnalyzer.classify()` now resolves from cheap signals before calling the
  vision backend: a `power_off` / `no_signal` short-circuit (no snapshot, no
  model), an unchanged-frame skip (reuse the last result), and optional
  OCR-assist for text screens. In a typical boot-watch this avoids most model
  calls; set the flags to `False` to restore unconditional classification.
- Internal simplifications (no public behaviour change): a single
  `vision.base.request_json()` helper backs both vision backends; the classifier
  system prompt interpolates `ALL_PHASES` so its token list can no longer drift
  from the parser's; `scheme`/`timeout` no longer bypass config precedence.
- `AnthropicBackend` validates its API key **lazily** (at first network use)
  rather than at construction, so analyzer paths resolved by a cheap gate (e.g.
  `power_off`) run with no key — `kvm-pilot classify --driver fake` works fully
  offline. A `make_backend` misconfiguration now raises `VisionError` (a clean
  CLI error) instead of an uncaught `ValueError` traceback.

### Removed
- Unused surface: `HTTP.delete()`, the no-op `KVMClient`/`ScreenAnalyzer`
  context managers, the `detect_state` alias, and the `ctrl_c`/`ctrl_z` HID
  shortcuts (use `send_shortcut(...)`).

## [0.1.0a1] — 2026-06-26

First public **alpha** pre-release, published to solicit hardware testing and
user feedback. **Not validated on real hardware** — see Notes.

### Added
- `KVMClient`: full PiKVM / GLKVM REST client covering auth (incl. TOTP/2FA),
  keyboard + mouse HID, snapshots/OCR, ATX power, Mass Storage Device, GPIO,
  Redfish, WebSocket events, and system info/logs/metrics.
- Safety layer (`SafetyPolicy`): `dry_run` mode and a confirmation callback
  gating an explicit, auditable set of destructive operations.
- Pluggable vision subsystem with two backends:
  - `AnthropicBackend` — resolves the newest vision-capable model at runtime
    via the Models API; no hard-coded version. Override with
    `KVM_PILOT_VISION_MODEL` or `model=`.
  - `OpenAICompatBackend` — any OpenAI-compatible endpoint (LM Studio, Ollama,
    vLLM, llama.cpp) for zero-cost on-prem inference.
- `ScreenAnalyzer`: backend-agnostic single-shot classification plus blocking
  `wait_for_state` / `wait_for_any_state` loops with confidence thresholds and
  bounded backoff.
- `kvm-pilot` CLI with `info`, `snapshot`, `power`, `power-cycle`, `type`,
  `key`, `mount`, `classify`, and `watch` subcommands; interactive confirmation
  by default, `--yes` and `--dry-run` flags.
- Config resolution (`resolve_host`) with args > env > TOML-profile precedence.
- HTTP transport with bounded retry/backoff on transient errors (409/503/
  network) and password/token redaction in error text.

### Notes
- **Not tested on real hardware.** Every feature is covered only by unit tests
  with mocked HTTP and vision responses; no device — including the GL-RM1PE this
  project targets — has been exercised end to end. Hardware validation and user
  feedback are the explicit goals of this alpha. Reports welcome in the issue
  tracker.

[Unreleased]: https://github.com/DustinTrap/kvm-pilot/compare/v0.1.0a12...HEAD
[0.1.0a12]: https://github.com/DustinTrap/kvm-pilot/releases/tag/v0.1.0a12
[0.1.0a11]: https://github.com/DustinTrap/kvm-pilot/releases/tag/v0.1.0a11
[0.1.0a10]: https://github.com/DustinTrap/kvm-pilot/releases/tag/v0.1.0a10
[0.1.0a9]: https://github.com/DustinTrap/kvm-pilot/releases/tag/v0.1.0a9
[0.1.0a8]: https://github.com/DustinTrap/kvm-pilot/releases/tag/v0.1.0a8
[0.1.0a7]: https://github.com/DustinTrap/kvm-pilot/releases/tag/v0.1.0a7
[0.1.0a6]: https://github.com/DustinTrap/kvm-pilot/releases/tag/v0.1.0a6
[0.1.0a5]: https://github.com/DustinTrap/kvm-pilot/releases/tag/v0.1.0a5
[0.1.0a4]: https://github.com/DustinTrap/kvm-pilot/releases/tag/v0.1.0a4
[0.1.0a3]: https://github.com/DustinTrap/kvm-pilot/releases/tag/v0.1.0a3
[0.1.0a2]: https://github.com/DustinTrap/kvm-pilot/releases/tag/v0.1.0a2
[0.1.0a1]: https://github.com/DustinTrap/kvm-pilot/releases/tag/v0.1.0a1
