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

## Process

Most structural choices came from adversarial review passes (find → verify →
fix). The *fixes* are in the code; this file preserves the *rejected* findings and
tradeoff rationale. The Redfish driver's spec grounding lives in
[`redfish.md`](redfish.md).
