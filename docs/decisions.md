# Design decisions

Short records of non-obvious choices — especially the ones that **look wrong but
are intentional**, so they don't get re-litigated. Newest first.

## Drivers

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
#102's MCP/healthcheck consumers can read maturity via `load_registry()` with
no access to the repo-only ledger; (c) blocking a pure function + CI drift gate
on a breaking data migration it does not use would invert the dependency. When
#97 lands v3 (known_bad into version rows, currency-reader migration,
`additionalProperties` tightening) it ports these rows as-is.

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

## Process

Most structural choices came from adversarial review passes (find → verify →
fix). The *fixes* are in the code; this file preserves the *rejected* findings and
tradeoff rationale. The Redfish driver's spec grounding lives in
[`redfish.md`](redfish.md).
