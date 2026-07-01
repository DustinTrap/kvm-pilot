# Design decisions

Short records of non-obvious choices — especially the ones that **look wrong but
are intentional**, so they don't get re-litigated. Newest first.

## Drivers

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

It is now the *third* copy of the same `power_off_hard → power_on` composition
(`KVMClient`, `FakeDriver`, `RedfishDriver`). A shared `PowerMixin.hard_cycle`
(composed from the protocol methods, with the settle delays as an overridable class
attribute) is the better home and would make the invariant structural — **deferred on
purpose**: the clean version changes `KVMClient.hard_cycle`'s public `off_delay`/
`on_delay` signature and touches the primary (real-hardware) PiKVM path, which is out
of scope for the CLI-dispatch change. Tracked as follow-up, not churned into #27.

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

## Process

Most structural choices came from adversarial review passes (find → verify →
fix). The *fixes* are in the code; this file preserves the *rejected* findings and
tradeoff rationale. The Redfish driver's spec grounding lives in
[`redfish.md`](redfish.md).
