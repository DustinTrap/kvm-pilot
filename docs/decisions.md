# Design decisions

Short records of non-obvious choices — especially the ones that **look wrong but
are intentional**, so they don't get re-litigated. Newest first.

## Drivers

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

### `RedfishDriver` is library/registry-only, not on the CLI `--driver` list
A BMC is capability-partial (no HID/Video; no `hard_cycle`/`watch_events`), so CLI
subcommands like `type`/`snapshot`/`events` would `AttributeError`. It's exposed via
`make_driver("redfish")`; the CLI entry waits on capability-aware dispatch
([#27](https://github.com/DustinTrap/kvm-pilot/issues/27)).

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
