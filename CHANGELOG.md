# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims
to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/DustinTrap/kvm-pilot/compare/v0.1.0a8...HEAD
[0.1.0a8]: https://github.com/DustinTrap/kvm-pilot/releases/tag/v0.1.0a8
[0.1.0a7]: https://github.com/DustinTrap/kvm-pilot/releases/tag/v0.1.0a7
[0.1.0a6]: https://github.com/DustinTrap/kvm-pilot/releases/tag/v0.1.0a6
[0.1.0a5]: https://github.com/DustinTrap/kvm-pilot/releases/tag/v0.1.0a5
[0.1.0a4]: https://github.com/DustinTrap/kvm-pilot/releases/tag/v0.1.0a4
[0.1.0a3]: https://github.com/DustinTrap/kvm-pilot/releases/tag/v0.1.0a3
[0.1.0a2]: https://github.com/DustinTrap/kvm-pilot/releases/tag/v0.1.0a2
[0.1.0a1]: https://github.com/DustinTrap/kvm-pilot/releases/tag/v0.1.0a1
