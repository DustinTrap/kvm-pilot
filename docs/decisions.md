# Design decisions

Short records of non-obvious choices — especially the ones that **look wrong but
are intentional**, so they don't get re-litigated. Newest first.

## Drivers

### AMT KVM speaks Intel's RFB 4.0 — every "wrong-looking" client choice is required ([#211](https://github.com/DustinTrap/kvm-pilot/issues/211))
The AMT KVM video path (`drivers/amt/rfb.py`) makes several calls that look like bugs
against stock VNC but are exactly what Intel's firmware requires — all verified live on a
Dell Latitude 5411 (AMT 14.1.67), matching MeshCommander's decoder:
- **Reply `RFB 003.008` to AMT's `RFB 004.000`.** AMT announces its proprietary RFB 4.0;
  echoing 004.000 gets the connection dropped. The 3.8 downgrade runs a standard, working
  framebuffer session.
- **Send NO `SetPixelFormat`; keep the native 16-bpp RGB565.** AMT only produces RGB565 and
  **resets** on a 32-bpp request (what most VNC libraries send by default). Looks like a
  missing step; it's a deliberate omission.
- **`SetEncodings` must list RAW *explicitly*** (RFC 6143 says RAW is implicit, so clients
  omit it — AMT doesn't assume it) plus RLE(16). Integrated/hybrid-GPU platforms refuse RAW
  and only deliver **RLE(16)**.
- **RLE(16) is decoded over a *standard-zlib* stream (`0x78 0x9c` header), not raw deflate.**
  The reference research said `inflateInit(-15)` (raw); AMT 14 firmware uses standard zlib —
  the wire was authoritative over the docs, so `zlib.decompressobj()` uses the default wbits.
- **A reset right after the framebuffer request is "unsupported display mode", not a fault.**
  AMT captures graphical screens (BIOS/POST/GRUB/GUI) but not legacy VGA text mode, so
  `snapshot()` does not retry-forever on that reset.
- **KVM is single-session; `snapshot()` cycles the SAP and retries.** A dropped session can
  wedge port 5900, so `SessionTimeout` is set non-zero and a wedged session is cleared with
  `reset_kvm_session()` (`kvm-pilot amt reset-kvm`).
- **The RFB password must be EXACTLY 8 chars** (upper+lower+digit+special) — a separate
  credential (`amt_kvm_password`) from the WS-Man password, validated up front because AMT
  otherwise rejects it with an opaque `InvalidRepresentation`.

### AMT boot override is write-only; `get_boot_options` says so honestly ([#211](https://github.com/DustinTrap/kvm-pilot/issues/211))
Real AMT `CIM_BootConfigSetting` returns only its keys — no `BootOrder` — so the pending
single-use *source* override (pxe/hdd/cd) **cannot be read back**. `get_boot_options()`
therefore reports `override_readable: false` and `target: null` rather than a misleading
`"none"` (which would look like "no override is set" when we simply can't tell). `BIOSSetup`
*is* readable. This looks like an incomplete read implementation; it's the honest ceiling of
what the ME discloses. Confirm an override by observing the next boot (SOL/KVM), not by reading it.

### AMT SOL/KVM are enabled over WS-Man with a full-object Put; consent-off needs ACM ([#211](https://github.com/DustinTrap/kvm-pilot/issues/211))
MEBx *provisions* SOL and KVM, but their network listeners are toggled remotely over WS-Man
(`enable_sol`/`enable_kvm`) — no physical MEBx trip, matching MeshCommander/rpc-go. Two
non-obvious calls: AMT's WS-Transfer Put is strict, so a partial or reordered body is rejected
as `InvalidRepresentation` — the driver **GET-modifies-PUTs the whole instance** (`_rmw_put`),
which looks wasteful but is required. And disabling the on-screen user-consent prompt
(`OptInRequired=0`) only works in **Admin Control Mode**; the ME forces consent in Client
Control Mode, so `enable_kvm(require_consent=False)` is rejected there rather than silently
ignored. Rapid WS-Man bursts trip AMT's flood protection (HTTP 401), so the AMT healthcheck
checks share one memoized `amt_health()` read.

### Host-visible virtual-media device names are per-driver data, seeded GL-only ([#78](https://github.com/DustinTrap/kvm-pilot/issues/78))
The device name a target host shows for a brand's MSD gadget (GLKVM: the observed
`UEFI: Glinet Optical Drive 1.00` boot-menu entry) is declared as a driver class
attribute, `virtual_media_host_pattern`. Not a `Quirk` — a quirk is a defect plus
workaround, and this is a positive expected signal. Not a firmware-registry `profile`
field — the gadget name is brand/driver identity (same across GL products and firmware),
not per-(vendor, product, version) data, and it must be available offline like
`capabilities()`, whereas registry matching needs a live `get_firmware_info()` probe.
The value is a **substring**, not a regex (matching `Quirk.firmware` semantics) — the
trailing "1.00" is a USB revision that may vary. PiKVM/BliKVM/Redfish deliberately stay
`None` until observed on real hardware (don't invent device data). Consumers today:
the `msd-online` healthcheck detail and the MCP `list_virtual_media` `host_visible_as`
field. Automated boot-entry matching is future work that will read this same attribute.

### SSH-to-target is a per-profile channel, not a KVM-driver capability ([#81](https://github.com/DustinTrap/kvm-pilot/issues/81))
The in-band SSH channel targets the **managed host's OS** — a different machine from
the KVM appliance, with its own address (`ssh_host`) and login. It could have been a
driver capability, but capability detection is **structural** (`detect_capabilities`
is `hasattr`-based via `runtime_checkable` protocols) and therefore config-independent:
a driver that merely *had* `ssh_reachable`/`ssh_exec` would report SSH support even with
no target configured. So SSH is a standalone `SSHChannel` (`src/kvm_pilot/ssh.py`) built
from the profile's `ssh_*` fields and **gated on "is `ssh_host` set?"** (raising
`CapabilityError` otherwise), never inferred from the KVM's address. `Capability.SSH` +
the `RemoteShell` protocol exist as a **seam** (like `SerialConsole`), implemented by the
channel rather than by KVM drivers. Dependency-free by design: reachability is a stdlib
`socket` probe and exec shells out to the **system `ssh`** (`BatchMode`), so no
`paramiko` — matching the stdlib-only-at-core convention. Every exec routes through
`safety.guard("ssh.exec", …)` (an arbitrary command can't be statically classified);
the reachability probe is read-only and ungated. SSH is deliberately **not** folded into
the `recovery-path` healthcheck: that check answers "can a *hung* host be reset?", and a
hung host won't answer SSH — so SSH reachability is a complementary in-band lever, not an
out-of-band reset path.

### Remote firmware flash is its own gated command, not a healthcheck auto-fix ([#92](https://github.com/DustinTrap/kvm-pilot/issues/92))
The healthcheck already carries an `AutoFix` mechanism (applied, with per-item consent,
by `healthcheck --fix`), so attaching the firmware update there looks natural — but
`AutoFix` is deliberately restricted to `safe_reversible` fixes that "never perturb a
running guest," and a firmware flash is the exact opposite: it reboots the KVM into a
new image (dropping the control channel) and can brick onboard storage with no remote
recovery on the GL RM1 family. So the healthcheck only makes the finding *actionable*
(its remediation names `kvm-pilot firmware-update` and states the risk), and the flash
lives behind its own explicit command, a new `FirmwareUpdate` capability, and the
`firmware.flash` destructive op. Per-model reliability (`risk`, `recovery_required`,
`self_flash_blind`) is **data** in the registry `profile.remote_update`, not hard-coded
in `health.py` — same rule as the rest of the capability profile.

The command defaults to a **dry-run plan** and, on a device whose healthcheck reports no
out-of-band recovery path (CRITICAL `recovery-path`), **refuses to execute** unless
`--i-have-physical-access` is passed — an informed override, per the maintainer's call
that a present-and-informed operator may still choose to flash. It also ejects virtual
media first (`gl-inet/glkvm#120`). The GL `/api/upgrade/*` request shapes are
reverse-engineered (no vendor spec) and the execute path is **unverified on hardware**;
it is feature-detected via `/api/upgrade/status` and documented as provisional in
[`firmware-update.md`](firmware-update.md).

### Capability-aware CLI dispatch: gate on `supports()`, then cast to the rich driver union ([#27](https://github.com/DustinTrap/kvm-pilot/issues/27))
Each subcommand declares the capability it needs; `_client(args, cap)` builds the
driver and rejects it with a clean message + exit 1 (not an `AttributeError`) when it
lacks the capability, deriving the command name from `args.command`. `_rich_client`
wraps it and `cast`s to `KVMClient | FakeDriver` (the `RichDriver` alias) — which looks
like a type lie. It's sound: `RedfishDriver` is the only capability-partial driver and
it lacks exactly HID/Video/Events, so gating on one of those excludes it, leaving the
PiKVM-family/Fake surface that carries the convenience kwargs (`slow=`, `quality=`,
`stream=`) the minimal capability protocols don't declare. A future rich-but-partial
driver would need its own Protocol rather than this cast.

### `RedfishDriver.hard_cycle` exists even though `hard_cycle` is not a capability
`power-cycle` gates on `POWER`, but `hard_cycle` isn't part of the `Power` protocol —
`KVMClient`/`FakeDriver` carried it as a convenience, `RedfishDriver` didn't, so
`power-cycle --driver redfish` would `AttributeError` despite a BMC plainly having
power. Added `hard_cycle` (force-off → on) so the invariant "advertises `POWER` ⇒ the
CLI `power-cycle` works" holds for every power driver. Its `off_delay`/`on_delay`
default to `0.0` (unlike `KVMClient`'s ATX settle delays) because the two gated power
ops already block on the real `PowerState` transition.

It was, for a while, the *third* copy of the same `power_off_hard → power_on`
composition (`KVMClient`, `FakeDriver`, `RedfishDriver`). That was consolidated
in #63 into `PowerMixin.hard_cycle` (in `drivers/base.py`), composed from the
`Power` protocol methods with the settle delays as overridable class attributes
(`_hard_cycle_off_delay`/`_hard_cycle_on_delay`): the PiKVM ATX path keeps 5.0/3.0
because its power ops don't block on the state change, while Redfish (which blocks
on the `PowerState` transition) and Fake keep 0.0. `hard_cycle(off_delay=, on_delay=)`
still overrides per call — the public defaults are now `None` (meaning "use the
driver's class attribute"), a small alpha-era signature refinement.

### `--redfish-auth` selector, defaulting to `session`
Session auth is the BMC norm (and what real iDRAC/iLO recommend), so it stays the
default. But sushy-tools' `--fake` emulator exposes no `SessionService`, and a BMC can
administratively disable session or basic auth (cf.
[#29](https://github.com/DustinTrap/kvm-pilot/issues/29)) — an "unlocked" `--driver
redfish` that could *only* do session auth couldn't authenticate to either. The
`basic` opt-in keeps the unlock honest. It's redfish-only and ignored by the PiKVM
family.

### Two-layer Redfish testing: in-process mock for the CLI path, external sushy-tools for independence
The pure-stdlib `tests/redfish_emulator.py` validates the full CLI → driver → HTTP
path in the default hermetic suite (`test_cli_redfish.py`). The opt-in `integration`
job (`tests/integration/`) runs the same surface against DMTF-conformant **sushy-tools**
`sushy-emulator --fake` — an *independently authored* implementation, so a spec
assumption shared by our driver and our own mock can't hide. sushy's fake driver has
no `SessionService`, hence the basic-auth path; it applies power transitions with a
short simulated delay that the driver's wait loop absorbs. The `--fake` backend needs
no libvirt/QEMU and no nested KVM, so it runs on a stock GitHub runner (pip-install +
self-started subprocess — no Docker, no `services:` container). The fixture also honors
`KVM_PILOT_REDFISH_URL` so the same tests can run against an already-running emulator —
the local-Docker fallback (`quay.io/metal3-io/sushy-tools`) when sushy-tools isn't on
PATH.

### Kept the 4 unimplemented sensing protocols (`BootProgress`, `Sensors`, `SerialConsole`, `Watchdog`)
They landed with zero implementers, which reads like dead code. Kept on purpose:
they are the documented seam for BMC drivers, and they're no longer speculative —
`FakeDriver` and `RedfishDriver` implement `BootProgress`, `RedfishDriver` implements
`Sensors`. `SerialConsole`/`Watchdog` are the IPMI/SOL seam. Don't delete them as
"dead code."

### `PiKVMDriver` is a base class with `GLKVMDriver`/`BliKVMDriver` subclasses (inheritance, not composition)
The GL/Bli devices are *API-compatible forks* — a subclass that overrides only the
deltas is the natural shape, and there are ≥2 real subclasses. (General guidance
still favors composition; this is the case where inheritance genuinely fits.)
`KVMClient`/`PiKVMClient` stay as aliases of `PiKVMDriver` so no public API breaks.

### `GLKVMDriver` maps **every** `/api/*` 404 → `ApiDisabledError`
Looks over-broad. It's intentional: GL firmware disables the **whole** `/api/*`
surface by default, so a 404 on any endpoint is overwhelmingly "API disabled," and
that's the dominant first-contact failure on a GL-RM1PE. The stock `PiKVMDriver`
sets no hint, so its 404s stay generic (see `test_stock_pikvm_404_is_a_plain_error`).

### Quirk registry holds only documented/observed facts
`GLKVM_QUIRKS` is seeded with the single documented quirk and grows from real
testing (`source="observed"`). Never invent firmware-version-specific data — the
project's honesty rule (alpha, untested on hardware) applies to the quirk DB too.

### Redfish action POSTs keep the default retry (on transient errors)
Reviewers flagged retrying non-idempotent POSTs (reset/insert). Kept: those ops are
effectively idempotent (resetting twice is still reset; inserting twice is still
inserted), retry only fires on transient `409`/`503`/network, and it matches the
existing `KVMClient` behavior.

### One `make_driver_from_config()` shared by the CLI and MCP server
The driver-from-`HostConfig` dispatch is shape-aware (fake takes no credentials;
the PiKVM family builds via `from_config`). It lives in one place so `cfg.driver`
is honored identically by both consumers (used in ≥2 places → justified helper).

### A dedicated `RedfishHTTP` transport instead of reusing `http.py`
`http.py` is PiKVM-specific (`X-KVMD-*` auth, the `ok`/`result` envelope) and
discards status/headers, which Redfish needs (`202`, `X-Auth-Token`, `Location`,
`ETag`). Generalizing `http.py` is the separate Step 2 ([#6](https://github.com/DustinTrap/kvm-pilot/issues/6)).

## Vision

### Added an `os_running` phase token
For `BootProgress=OSRunning`, which means "OS handed off, running" — no existing
token fit, and the alternative (returning `None`) wrongly signals "can't report."
Cheap to add now that `SYSTEM_PROMPT` interpolates `ALL_PHASES`.

### `AnthropicBackend` validates its API key lazily (at first network use)
So analyzer paths resolved by a cheap gate (e.g. `power_off`) run offline with no
key — `classify --driver fake` needs no credentials. Mirrors the lazy model
resolution.

## Safety

### Guard fail-open is covered by a systematic contract test
`SafetyPolicy.guard` returns True for any op id NOT in `DESTRUCTIVE_OPS` (so
adding a driver method doesn't accidentally gate a read) — but that means a
typo'd op id or a dropped `guard()` call silently un-gates a destructive method.
Because gating is the tool's one safety mechanism (and it's exposed to LLM
agents), a table-driven contract test exercises every gated method under
deny/dry-run/recording, and a source-scan invariant asserts every literal op id
passed to `.guard()` is registered. A dropped guard now fails CI (verified by
mutation); previously the suite stayed green.

### Dry-run short-circuits BEFORE the confirm callback
`SafetyPolicy.guard` checks `dry_run` first and never invokes `confirm` for a
skipped call. The old order (confirm first) made `--dry-run` prompt — and in
non-interactive automation *block with exit 3* — for calls that were never going
to be sent. Consequence: a denying confirm callback is not exercised in dry-run;
tests that want to see the callback fire must run live (`dry_run=False`).

### HID input is destructive
`type_text`/`press_key`/`send_shortcut`/`key_event`/`mouse_click` guard through
`hid.*` ops: keystrokes land on a live console (`rm -rf` is one `type_text`
away), so "changes target state" clearly applies. Mouse *moves* stay ungated
(cursor position alone changes nothing). Known cost: under the CLI's interactive
confirm, key-spam helpers like `enter_bios` prompt per keystroke — `--yes` or a
session-scoped confirm is the escape hatch; refining that UX is follow-up work.

### `is_powered_on()` fails open when ATX sensing is absent
With no ATX board, kvmd reports `enabled: false` and the power LED is
meaningless. Reporting "off" there made the vision layer short-circuit every
classification to `power_off` on ATX-less devices, suppressing all snapshots.
Same fail-open rationale as `has_video_signal`. Caller-visible change:
`wait_for_power_state(False)` on an ATX-less device now times out (the device
cannot sense power) instead of returning instantly with a false success.

### Neither transport follows HTTP redirects
The stdlib's default opener copies every request header — including our
credentials (`X-KVMD-Passwd`, `X-Auth-Token`, `Authorization: Basic`, the
session `Cookie`) — to whatever host a 3xx `Location` names, with no
same-origin check and even across an https->http downgrade. That defeated the
Redfish `_same_origin` guard (which only covers URLs *we* construct, not
server-issued redirects) and left the PiKVM transport, which has no such guard,
fully exposed: a hostile or MITM'd device could exfiltrate the admin password.
Both transports now build their opener with a `_NoRedirect` handler that
refuses 3xx and surfaces it as a `ConnectionError`. Neither kvmd nor Redfish
needs transparent redirect following (Redfish async/`Location` is read
explicitly), so this costs nothing.

### Ambiguous transport failures are never auto-retried for POSTs
A read-phase reset/timeout means the device may have already executed the
request; re-firing a power/HID/MSD POST could run it twice. Connect-phase
failures (nothing was sent) stay retryable for every method. Retrying 409/503
stays safe for all methods: those are definitive "rejected" responses.

### The analyzer consults structured BootProgress before snapshotting
The sensing hierarchy prefers structured signals over pixels. `ScreenAnalyzer`
now has a `BootProgress` gate (after the power/no-signal probe, before the
snapshot): if the driver exposes `get_boot_progress()` and returns an actionable
phase token, that resolves the classification at 0.99 with no snapshot and no
model call. Devices without the capability (the PiKVM family) skip it and fall
through to the pixel path. This makes a BMC classifiable with zero pixels and
gives #13's roadmap its first structured-tier consumer.

### Redfish read_sensors() uses $expand where advertised
Real BMCs expose 100-400 Sensor resources; one GET per member (each a fresh
TCP+TLS handshake) is 10s of seconds to minutes, and the sensing hierarchy
polls it. `read_sensors()` now probes `ServiceRoot.ProtocolFeaturesSupported.
ExpandQuery` and, when the service advertises it, fetches the Sensors collection
with `?$expand=*($levels=1)` (or `.` for a Levels-only service) — one request
instead of N. It falls back to the per-member loop when $expand isn't advertised,
and remembers a `501` (the DSP0266 response for an unsupported $-query) so it
doesn't retry expansion per call. Deferred: HTTP keep-alive in the transport
(every request currently pays a fresh handshake) — a cross-cutting change to
both transports, tracked separately.

### Logs.seek is uniformly "seconds of lookback"
kvmd's `/api/log?seek=N` interprets N as seconds of history, so the shared
`Logs` protocol standardizes on that; the Redfish driver was interpreting `seek`
as an entry-skip index, so `get_logs(seek=3600)` returned "the last hour" on
PiKVM and "everything after entry 3600" (usually empty) on Redfish. Redfish now
filters `LogEntry.Created` to entries within the lookback, with three field
caveats: entries with a missing/unparseable timestamp are kept; unset-RTC epoch
stamps (~1970, common on fresh OpenBMC) are kept; and index skipping is never
used as a fallback, because LogEntry ordering varies by vendor (iDRAC
newest-first, OpenBMC oldest-first). `seek=0` returns everything.

### Redfish resolves chassis/manager from the System's Links, not a global index
The Chassis and Managers collections have no defined ordering correspondence to
the Systems collection (DSP0266), so indexing all three with one `system_index`
could read sensors/logs/virtual-media from a different node than the one being
power-cycled on multi-node gear (blades, Supermicro twins). The driver now
resolves them from the chosen ComputerSystem's `Links.Chassis` /
`Links.ManagedBy` (DSP0268), falling back to the collection only when the System
advertises no such link. An out-of-range `system_index` is a hard `CapabilityError`
(never a silent fall-back to member 0 — that would target the wrong node with a
destructive op), the reset confirm prompt names the resolved system URI, and
`system_index` stays programmatic-only (not plumbed through config) until a
multi-node config surface is actually needed.

### Redfish InsertMedia sends only `Image`
`Inserted`/`WriteProtected` are optional (DSP2046) and merely restate the insert
defaults, but strict firmware (Supermicro X11/X12, some Lenovo/older iDRAC)
*rejects* an InsertMedia body that carries them (the fix OpenStack sushy adopted
for Supermicro). So the driver POSTs `{"Image": source}` alone. For the inverse
quirk — a BMC that *requires* `TransferProtocolType` (sushy bug #2072805,
reported as 400 `ActionParameterMissing`) — it retries once with the type
derived from the URL scheme. Full `@Redfish.ActionInfo`-driven parameter
negotiation (as `_reset_info` does for Reset) was deliberately not built: the
omit-plus-targeted-retry pair covers the field-known cases with far less code
(CLAUDE.md: smallest change that works, no speculative generality).

### Vision wait loops back off on repeated errors and honor Retry-After
`request_json` now carries the HTTP status on the `VisionError` (`status_code`)
and parses a 429's `Retry-After` seconds into `retry_after`. `wait_for_any_state`
keeps a separate error counter and, on each failed poll, sleeps
`max(bounded_backoff, retry_after)` (clamped to the remaining deadline) instead
of hammering a rate-limited API at the fixed interval — each failed poll
re-uploads the whole image, so this matters. The retryable set is effectively
{429, 500, 503, 529}: the loop already retries *every* VisionError to the
deadline, so no explicit gate is needed there; the single-shot classify/CLI path
keeps its one-attempt behavior (a jittered in-request retry was left as optional
future work).

### Redfish re-authenticates once on a mid-flight 401
Real BMCs terminate idle sessions (DSP0266 SessionService inactivity timeout,
~30 min default) and drop every token on reboot, and a token cleared by
`close()` leaves the (cached) discovery URIs valid but credential-less. The
transport now catches a session-mode `401`, clears the token, calls `login()`
once, and retries the request a single time (a per-call guard, not a loop).
Safe even for destructive POSTs — a `401` is rejected before the action runs —
and non-recursive, because `login()` issues its own requests unauthenticated.
Skipped for `403` (a privilege failure re-login can't fix), PasswordChangeRequired
(re-login would leak a session slot), basic auth, and the Sessions collection
itself. This lives in the transport, not the driver, because the driver's
`_root_doc`/`_system_doc` caches mean `close()` + `_ensure_login` cannot recover
a live object on their own.

### Redfish reads PowerState before every reset
`PushPowerButton` pulses the power button (DSP0268) — a *toggle*, not an
absolute state — so choosing it from `AllowableValues` alone can invert a
safety-gated intent: on iDRAC8-class firmware (off set `[ForceOff,
PushPowerButton]`, no `GracefulShutdown`) `power_off` on an already-off host
powered it back *on*, then timed out. Both power methods now read the current
`PowerState` first: a host already at the target gets no reset at all, and
`PushPowerButton` is selected only when the pulse moves toward the target
(otherwise the preference falls through to `ForceOff`). A `400`/`409` that
nonetheless lands while the host is observed at target is treated as success —
a refinement of, not a change to, the "resetting twice is still reset" retry
rationale below (which holds only for absolute ResetTypes, not toggles).

### Redfish transitional `PowerState` maps to `unknown`, not `power_off`
`PoweringOn`/`PoweringOff`/`Paused` (DSP0268) are mid-transition; only a literal
`Off` becomes `power_off`. Conservative choice: a wait loop must not conclude a
host is down while it is coming up.

### `snapshot()` lost its `quality` parameter
kvmd silently ignores `preview_quality` without `preview=1`, and the preview
path downscales to ~1/5 resolution (which would break OCR/vision) — there is no
full-resolution re-encode-at-quality endpoint. The parameter was a no-op lie;
deleted rather than deprecated while the API is alpha.

### GLKVM owns `drivers/glkvm.py`; `pikvm.py` keeps only no-delta forks (#140)
The GL fork repeatedly confused agents while it lived in `pikvm.py`: GL-only
behavior (API disabled by default, the `/api/upgrade/*` flash layer, dual
version numbers, streamer/ATX quirks) sat in a file whose name promised stock
PiKVM, and field sessions kept applying stock-PiKVM assumptions to GL units
(#126/#128 were filed from exactly that gap). GLKVM now has its own module
whose docstring enumerates the divergences — including that GL firmware
self-reports as `rpi/rpi4/v3`, so nothing in `/api/info` reveals the fork.
`pikvm.py` re-exports the moved symbols for one release (the package is on
PyPI); the public `from kvm_pilot import GLKVMDriver` path never changed.

### Derived maturity lives in the shipped registry under schema v2, not behind #97's v3 (#98)
The maturity levels `kvm_pilot.maturity` derives from the run ledger are written
into the *bundled* `firmware_registry.json` as an **additive** optional
`versions[]` key while `schema_version` stays 2 — deliberately not gated on
#97's v3 restructure. Why: (a) deployed clients' hand-rolled validators ignore
unknown entry keys, so a cache-refreshed registry bearing `versions[]` still
validates on every released install; (b) the registry ships in the wheel, so
#102's MCP/healthcheck consumers can read maturity via `load_registry()`
without re-deriving from the ledger (which was repo-only when this was decided;
#102 later shipped the ledger in the wheel too); (c) blocking a pure function +
CI drift gate on a breaking data migration it does not use would invert the
dependency. When #97 lands v3 (known_bad into version rows, currency-reader
migration, `additionalProperties` tightening) it ports these rows as-is.

### support_matrix reports ledger evidence; maturity stays #98's derived output (#102)
The MCP `support_matrix` tool and the `support-evidence` healthcheck finding
report the raw run-ledger evidence — per-combo pass/fail counts, last outcomes,
a `never_exercised` list — and *join* the maturity level from the shipped
registry's #98-derived `versions[].maturity` rows. `kvm_pilot.support_matrix`
never computes a level itself: hand-approximating (or re-deriving) levels in a
second place would create exactly the derived-vs-committed drift #98's CI check
exists to prevent. Two consequences: the ledger moved into the wheel
(`src/kvm_pilot/data/test_runs.jsonl`) so the evidence answers offline from
`pip install kvm-pilot` (batteries-included rule), and the `capabilities` tool's
`live_evidence` annotation is **driver-granular**, not device+firmware — that
tool is contractually offline (no network call, tested as such), so it cannot
know which firmware it is facing; the device+firmware-granular annotation lives
in `healthcheck` (`support-evidence`), which already probes firmware identity.
The finding is INFO by default (no live evidence is the near-universal state
for an alpha and must not inflate reports or feed the destructive gate) and
WARNING only when a recorded live attempt actually failed.

## Orchestration — "Reflexes" edge-autonomy release (planned)

These two records are **forward-looking**: the feature (an on-demand playbook
runner over `ScreenAnalyzer`) is not yet in the code. They are recorded here now,
per the "capture it so it isn't re-litigated" rule, ahead of the build. Builds on
the sensing-hierarchy efficiency roadmap ([#13](https://github.com/DustinTrap/kvm-pilot/issues/13));
full design in the [Reflexes epic #117](https://github.com/DustinTrap/kvm-pilot/issues/117)
and the [Reflexes RFC](reflexes.md).

### Playbooks are Ansible-*style* YAML on our own runner, not Ansible-the-engine
Playbooks read like Ansible tasks (named steps, `wait_for`, `when`/`register`)
because that YAML is what humans find easiest to author, read, and enhance — but
they are executed by a small stdlib runner, **not** by `ansible-playbook`.
Adopting Ansible-the-engine was rejected: it fights the stdlib-only-core +
`pip`-ships-everything thesis (it becomes a heavy shell-out extra, not
"included"); its execution model *converges idempotent tasks to a desired state*,
which does not model our **reactive** watch → act → **escalate-to-agent-on-unknown**
loop; and its host/connection model is wrong for a managed host that has **no
agent** and is driven through the KVM's REST API (everything would be
`connection: local`). The same step model also loads from JSON (stdlib) for the
agent-emitted path — one internal model, two loaders. YAML pulls in **PyYAML as a
base dependency** (a user-facing surface, so base not an extra, per the
batteries-included rule); the core library import stays stdlib via a lazy import.
A real, opt-in Ansible collection may still come later as an ecosystem
integration — it is just not the core format.

### Destructive playbook steps: pre-authorize the whole run, then verify each precondition
A playbook may contain destructive steps (power, reset, virtual media). To run
unattended without a per-step human round-trip *and* honor the invariant that "a
vision classification must never trigger a destructive action on its own", the
operator **pre-authorizes the whole run** (a run-scoped allow-list), moving the
safety decision to authoring/launch time — the classifier still never
*authorizes*, the human did, in advance. Pre-authorization is deliberately **not
blanket**: before firing each destructive step the runner re-verifies, via the
cheap sensing gates, that the device is actually in that step's expected
`precondition` phase. A precondition mismatch does **not** fire the step — it
escalates. This is what stops a surprise state from triggering the wrong
destructive action, which is exactly the risk the invariant guards against. All
destructive steps keep their `DESTRUCTIVE_OPS` / `safety.guard()` routing, and the
health preflight gate still runs before the run.

### MCP act tools: classify by effect, gate by effect, approve per invocation (#61)
The MCP act tools (`type_text`/`press_key`/`send_shortcut`/`ctrl_alt_delete`) are
authorized by an **effect class** (`EffectClass` in `safety.py`), not by tool name
or transport. The class layer is **additive over `DESTRUCTIVE_OPS`** — the set and
`SafetyPolicy.guard` are unchanged, so the driver stays stdlib-only and the client
transport guard is untouched; `OP_EFFECT`/`effect_of`/`shortcut_effect` are a
read-only lookup consumed only by the MCP layer (`mcp/act.py`).
- **Ctrl+Alt+Del is `power_soft`, not HID.** It is a reboot delivered over the
  keyboard, so it is gated by `KVM_PILOT_MCP_ALLOW_POWER`, and `send_shortcut`
  computes its class from the chord (CAD, Magic SysRq `b`/`o`) so a reboot can't
  slip through the weaker HID gate by choosing a different actuator. The result
  records **both** transport and effect for the same reason.
- **Two guarantees, two postures.** (a) *allowed* — operator env flag per effect +
  a fail-closed `KVM_PILOT_MCP_PROFILES` allowlist; (b) *approved at run time* —
  MCP elicitation when the client supports it (*interactive*), else an explicit
  `confirm=true` under a standing policy (*pre-authorized*). The pre-authorized
  posture is **intentional, not a fallback hack**: an unattended install loop has
  no human to answer an elicitation, so forcing elicitation-only would break the
  product's headline use case. Denials return through the same call path
  (`approved:false` + reason) so the agent recovers instead of hanging.
- **Deferred to #72:** the signed/expiring consent receipt. The MVP result already
  carries a stable `invocation_id` + effect class so that layer can build on it.
### SSH bootstrap during install: guided, not blind full-auto (#81)
The "expensive HID phase sets up the cheap phase" — reading the DHCP IP off the
installer console and starting `sshd` over KVM HID so the rest of the install runs
in-band over SSH (`kvm_pilot/bootstrap.py`, CLI `ssh-bootstrap`). It is deliberately
conservative rather than blind full-auto, because the failure modes are severe:
- **Plan by default.** `execute=False` sends nothing; it returns the plan. This is
  the CLI default (like `firmware-update`), with one top-level confirmation before
  the first keystroke (not a prompt per keystroke).
- **The IP probe doubles as a console canary.** A marker-wrapped `echo` is typed and
  OCR'd back; if the marker never echoes, the keystrokes were not consumed by a
  shell (silently-failed VT-switch, or a graphical/Windows installer), so it
  **aborts before typing any `sshd` command** — a dropped command must never land in
  the installer's partitioner. "Marker present but no IP" vs "marker absent"
  distinguishes retry-with-`--ssh-host` from hard-abort.
- **Reachability is necessary but not sufficient.** Success requires a trivial
  `ssh_exec` to actually authenticate — a reachable port is not a working channel.
  The default bootstrap commands only start `sshd`; the operator adds auth (a key or
  password) via `--command`, and the auth probe reports if it's still unusable.
- **Not an MCP tool in v1.** Agents should orchestrate the same flow with
  `snapshot`/`classify`/`type_text` + `ssh_reachable(host=…)` so a human stays in the
  loop; a single ungated auto-bootstrap MCP tool is deferred.

## Appliance-SSH channel & the RV1126 encoder wedge (#162)

The GL RV1126 hardware video encoder wedges (kvmd hard-loops on `init rv1126
encoder failed`; the JPEG snapshot path 503s), and it is the dominant, recurring,
fleet-wide fault. Several non-obvious choices came out of measuring it live:

- **loadavg is NOT the wedge signal.** Measured 2026-07-07: a freshly-rebooted,
  zero-interaction unit self-inflates to load ~10 within 90s and holds there while
  CPU falls to ~4%. The RV1126 video-pipeline **kernel** threads (`venc`, `vpss`,
  `vrga`, `vvi_thread`, …) park in uninterruptible **D-state** as their normal idle
  behavior, and Linux counts D-state toward loadavg — so load ≈ (D-thread count) ≈
  10 whether healthy or wedged. The `encoder-wedge` check therefore keys on
  **function** (the encoder-init failure pattern in the kvmd log, which is
  REST-visible and survives the wedge), and treats loadavg/D-state only as context.
- **Detection is REST-based; the appliance channel is for recovery.** The kvmd log
  is available over the REST `/api/log`, so the wedge is *detectable* without
  appliance SSH. The appliance-SSH channel exists for the *independent transport*
  and the *reboot recovery* — a reboot is the only fix, because the wedged threads
  are kernel threads (unkillable; a userspace service restart cannot clear them).
- **The finding is WARNING, never CRITICAL.** A CRITICAL gates every destructive op
  (the health gate), and gating everything on a recurring transient would make the
  tool unusable. WARNING surfaces it without blocking.
- **Recovery is operator-gated and never autonomous.** The wedge recurs within
  minutes of a reboot, and there is **no out-of-band power to the appliance itself**,
  so an auto-reboot reflex would thrash the management plane and a reboot that fails
  to rejoin the network strands the operator with zero access. `appliance.reboot`
  stays a gated, human-initiated op; it must not be wired into the Reflexes loop.
- **Key-based auth only; separate trust domain.** The channel reuses `SSHChannel`'s
  deliberate BatchMode/no-sshpass stance. The credential that works today is
  `root@<kvm>` with the shared fleet password (== the kvmd admin password); it is
  **not** reused — onboard a key once (`ssh-copy-id root@<kvm>`) so kvmd-REST and
  appliance-root stay distinct trust domains and no password is persisted. The
  shared fleet root password across units is itself a blast-radius hazard;
  per-device keys are the recommendation.

## Adaptive interface router (#181) & target SSH password auth (#183)

- **Interfaces split into two planes, and capability is state-dependent.** KVM
  (out-of-band: library/mcp/chrome — works at any OS state) vs OS (in-band:
  ssh/winrm — needs the OS up). The router picks the cheapest *capable* interface
  from a per-device scorecard. Capability is **not** static — a GL snapshot is
  JPEG or undecodable H.264 depending on resolution/streamer state (proved live:
  the same host flipped within 25 min), so a scorecard is only valid until state
  changes; `is_stale`/`stale_rows` re-benchmark exactly the affected rows.
- **Persistent connections are the OS-plane lever.** SSH per-call cost is
  dominated by the handshake (measured ~263ms fresh → ~26ms over a reused
  ControlMaster, ~10×). The engine should hold persistent connections for every
  interface — HTTP keep-alive, SSH ControlMaster, MCP stdio.
- **Target SSH gains opt-in password auth; the appliance channel does NOT.** The
  *target* `SSHChannel` (the managed host) can use `ssh_password` via SSH_ASKPASS
  — dependency-free (a 0700 helper echoes an env var; no `sshpass`, no library,
  the secret never hits disk/argv). The **appliance** channel stays deliberately
  key-only (see above) — the two are different trust domains on purpose. The
  agent must not plant its own key on a target to gain access (standing
  persistence); the operator installs the key or supplies a password.
- **WinRM ships as PowerShell-over-SSH first.** "Remote PowerShell" is served
  dependency-free by reusing `SSHChannel` (`powershell -EncodedCommand`), leaving
  native WS-Man (which needs a third-party client) as an optional extra later.

## Evidence honesty after a firmware change (#180 / #156)

- **A firmware delta invalidates the assessment, detected at the next
  connection — not per call.** `preflight` persists the last-assessed firmware
  (`assessed:{driver}@{host}` in the health cache) and, when the live version
  differs, re-runs the stable checks and emits a `firmware-delta` diff
  (cleared / new-regressed / still-open). The in-process `_SESSION_AUDITED`
  guard stays host-keyed on purpose: probing firmware on every MCP tool call
  would add a network round-trip per call to catch only a mid-process
  out-of-band flash, which the next process/connection catches anyway.
- **Condition-blind ledger evidence surfaces a caveat; the maturity ladder is
  unchanged.** Old snapshot rows without #156 `conditions` genuinely were
  observed — just under unrecorded resolution/encoder state — so `maturity.py`
  keeps counting them (changing the ladder would churn the committed registry
  through the CI drift gate and rewrite honest history). The healthcheck
  instead labels the evidence "recorded, conditions unrecorded — may still
  fail at native res", and every *new* snapshot row should carry `conditions`.
- **The firmware-delta diff compares stable (cacheable) findings only.** The
  cache never stored the old run's volatile results, so including the new
  run's volatile findings would report every live warning as a false
  "regression" after each upgrade.

## IPMI driver cross-checked against OpenIPMI `ipmi_sim` (#62 / #28)

Like the Redfish driver against sushy-tools, the IPMI driver
(`tests/test_ipmi.py`, fake-ipmitool) is corroborated against a reference BMC we
didn't write — OpenIPMI's `ipmi_sim` — in `tests/integration/test_ipmi_external.py`
(marked `integration`, env-gated via the `ipmi_bmc` fixture; run live on the
homelab OpenShift VM against Fedora's stock `/etc/ipmi/ipmisim1.emu`, since macOS
has no ipmi_sim build). Independence is the point: the sim answers as *MontaVista*
(mfr `0x1291`, fw `9.08`), not the Dell shapes our fixtures were captured from, so
a `mc info`/chassis-status parser overfit to Dell would surface here. It didn't —
6/6 green.

- **Assert only what the reference sim models; document the rest.** Stock
  `ipmi_sim` has known gaps (characterised live), so the integration test asserts
  execution + parsing + feature-detect, not full state round-trips (which the
  fake-ipmitool unit tests and real iLO/iDRAC hardware, #29, cover):
  - **Power never toggles.** `chassis power on/off` is accepted but the reported
    state is stuck at "off" — the sim binds chassis power to an external QEMU VM
    (`startcmd`, `startnow false`) that isn't running. Consequently the
    state-dependent verbs `reset`/`soft` are *rejected* ("Invalid data field") on
    an off chassis, so only `power_off_hard`/`power_on` are exercised live.
  - **No device SDRs** (`no-device-sdrs`, empty SDR repo) → `read_sensors()`
    returns `count == 0`, so the test asserts the shape is readable, not a count.
  - **Boot parameter 5 GET is unimplemented** → `get_boot_options()` degrades to
    `target=None`/`enabled='Unknown'` yet still reports the static `allowable`
    set and `mode_settable`, which is what the test checks (plus fast client-side
    rejection of `usb`, which has no IPMI bootdev selector).
- **The cosmetic "Unable to Get Channel Cipher Suites" line is not an error.**
  `ipmi_sim` doesn't answer Get-Channel-Cipher-Suites, but the RMCP+ session still
  establishes on the default cipher, so the driver's commands succeed. Tests parse
  the real output lines and ignore that warning.

## Process

Most structural choices came from adversarial review passes (find → verify →
fix). The *fixes* are in the code; this file preserves the *rejected* findings and
tradeoff rationale. The Redfish driver's spec grounding lives in
[`redfish.md`](redfish.md).
