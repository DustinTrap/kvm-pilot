# Writing a first-class driver (plugin)

How to add support for a new device family — a KVM appliance, a BMC, an
out-of-band management technology — and bring it all the way to **first-class**:
every function tested, reliable, documented, benchmarked, and represented in the
feature matrices to the same standard as the mature drivers (`glkvm`, `redfish`,
`ipmi`, `amt`).

> **Why this exists.** A driver that *works* is not the same as one that's
> first-class. A new driver is the first of its shape to exercise a lot of shared
> code (the CLI, the MCP server, the healthcheck, the run ledger), and it will
> silently under-deliver — or actively break shared assumptions — unless it's
> held to the whole bar. This guide is the checklist that keeps the quality
> consistent across the fleet. It's used both by outside contributors and
> internally when we mature a new plugin.

The **worked example throughout is the Intel AMT driver** (`src/kvm_pilot/drivers/amt/`,
[#211](https://github.com/DustinTrap/kvm-pilot/issues/211)) — a good reference
because it spans every plane (power, boot, serial, *and* video+HID) and its
build-out is what this guide was distilled from.

The end state is the **[Definition of Done](#definition-of-done)** at the bottom.
Everything above it explains each line.

---

## 0. First principles (the non-negotiables)

These come from [CLAUDE.md](../CLAUDE.md) and apply to every driver:

- **Stdlib-only at import.** Driver modules import only the standard library; a
  third-party need is imported lazily inside its own function (see how the AMT
  RFB client needs no VNC library, and SOL shells out to `amtterm` instead of
  vendoring a redirection stack).
- **`pip install kvm-pilot` ships everything.** The driver lives under
  `src/kvm_pilot/` so it lands in the wheel. A runtime dependency is a **base**
  dependency, never an opt-in extra.
- **Destructive ops are gated.** Anything that changes a target's running state
  routes through `SafetyPolicy.guard(op, description)` and its op-id lives in
  `DESTRUCTIVE_OPS` + `OP_EFFECT` in `safety.py`.
- **Capabilities, not a monolith.** A driver implements the capability protocols
  its hardware supports (`drivers/base.py`); detection is structural.
- **Issue-per-finding.** Every meaningful change references a GitHub issue.
- **Be honest.** Never claim "tested" or a maturity level beyond what the run
  ledger shows. The support matrix is the source of truth.

---

## 1. The driver

### 1a. Capability protocols
Implement the relevant `@runtime_checkable` Protocols from
[`drivers/base.py`](../src/kvm_pilot/drivers/base.py) — `Power`, `HID`, `Video`,
`VirtualMedia`, `BootConfig`, `SystemInfo`, `SerialConsole`, `Sensors`, `Logs`,
`BootProgress`, `Events`. `detect_capabilities()` derives the set structurally
from the methods present, so you get `capabilities()`/`supports()` for free via
`CapabilityMixin`. Subclass `PowerMixin` for `hard_cycle`.

> **Match the *rich* signatures, not just the Protocol.** The base `HID` protocol
> declares the minimum (`type_text(text)`, `mouse_click(button="left")`), but the
> CLI and MCP call the richer `KVMClient` signatures — `type_text(text, slow=)`,
> `mouse_click(button, double=)`, `mouse_move_percent`/`mouse_move_pixels`. A
> driver that implements only the Protocol will **traceback** through the CLI.
> `tests/test_driver_contract.py` enforces this. (AMT is pixel-native, so its
> `mouse_move_percent` maps onto the real framebuffer — see it for the pattern.)

### 1b. Construction & registry
- `from_config(cls, cfg, *, confirm, dry_run) -> Driver` builds from a resolved
  `HostConfig`; store `self.safety = SafetyPolicy(dry_run, confirm)`.
- Register in [`drivers/__init__.py`](../src/kvm_pilot/drivers/__init__.py): a
  `_make_<kind>` factory, an entry in `_DRIVER_FACTORIES`, a branch in
  `make_driver_from_config`, and the class in `__getattr__`/`__all__`/`TYPE_CHECKING`.
- Add `--driver <kind>` to the CLI `choices` (`cli.py` `_add_common`) **and its
  help string**, and to the `AnyDriver` union.
- Add config fields + env vars to [`config.py`](../src/kvm_pilot/config.py)
  (`resolve_host` signature + construction) and document them (§3).

### 1c. Normalized firmware identity — **required**
Implement `get_firmware_info()` returning **`vendor`, `product`, `version`**
(plus raw `manufacturer`/`model`). This is the key the run ledger and firmware
registry join on — a driver that returns only `{version}` records device identity
as `fake/fake` and never earns a maturity level. Mirror the Redfish/AMT/IPMI
shape. `tests/test_driver_contract.py` enforces the method exists.

### 1d. Safety op-ids
Every state-changing method calls `self.safety.guard("<kind>.<op>", desc)` and
returns a benign value when it returns `False` (dry-run / deny). Add each op-id to
`DESTRUCTIVE_OPS` and give it an `EffectClass` in `OP_EFFECT` (`safety.py`). The
MCP server gates on the effect class (`KVM_PILOT_MCP_ALLOW_*`); a genuinely
sensitive op may warrant a *dedicated* gate (AMT consent-off uses
`KVM_PILOT_MCP_ALLOW_CONSENT_OFF` on top of `ALLOW_CONFIG`).

### 1e. Driver-specific methods → surfaces
A method that isn't a capability (AMT's `enable_sol`/`enable_kvm`/`reset_kvm_session`,
GL's `appliance`/`recover-hid`) still needs surfaces to be first-class: a CLI
subcommand (mirror `cmd_appliance` / `cmd_amt` — `getattr`-guarded so it exits
cleanly on the wrong driver) and, where safe for an agent, an MCP tool.

---

## 2. Tests — the five layers

A first-class driver is tested at **every layer the mature drivers are**, not
just the driver in isolation. Coverage target: **≥90%** for the driver package.

| Layer | What it proves | How |
|---|---|---|
| **Unit** | Each method's logic + every error path | Direct calls; hand-crafted vectors for pure functions (e.g. the AMT DES FIPS-46-3 vector, the ZRLE sub-encoding vectors) |
| **Emulator (real transport)** | The wire protocol is correct | A pure-stdlib fake server on `127.0.0.1` exercised over the *real* transport — `tests/amt_emulator.py` (WS-Man SOAP), `tests/amt_rfb_emulator.py` (RFB), like `emulator.py`/`redfish_emulator.py`. Add knobs (`fault_reason`, `reject_auth`, `http_status`, …) so a test sets state then asserts on captured calls |
| **CLI (through `main()`)** | The command dispatch + capability gating | `test_cli_*.py`: run `main([...])` over the emulator; assert the wrong-driver path exits cleanly (not `AttributeError`) |
| **MCP (through a session)** | The tool gating + surface | `test_mcp_server.py`: `session.call_tool(...)`; assert the `ALLOW_*` floor and per-invocation approval; add the tool to the `EXPECTED_TOOLS` guard |
| **Integration / CI** | Optional, real reference stack | An env-gated `tests/integration/test_*_external.py` (skip-by-default), like the Redfish sushy-tools CI job. If there's no pip-installable reference emulator (AMT), the hermetic path stays the in-process emulator — say so in the file |

**Error-path parity.** The mature drivers test their full error taxonomy — wrong
password → `AuthError`, transport 5xx/timeout/reset, non-zero return code but
HTTP-OK (device refused), password **redaction** in error text, session reuse /
stuck-session recovery, teardown frees the channel. Match it.

**The contract test.** Add your driver to `tests/test_driver_contract.py`
(`OOB_DRIVERS`) — the shared guard that a driver didn't forget `get_firmware_info`
/ `from_config` / the rich HID signatures. New requirements go here so they're
enforced fleet-wide.

**Do not** weaken an assertion to pass, and **do not** gate a commit on
`pytest | tail` (the pipe hides failures — use no pipe or `set -o pipefail`).

---

## 3. Documentation — every surface

AMT's audit found the driver at-bar in the *reference* docs but **invisible** in
the *operator* surfaces. Both matter. Update all of:

- **`docs/<kind>.md`** — the reference (protocol notes, config, safety, live
  bring-up, honest caveats, sources). Model on `docs/redfish.md` / `docs/amt.md`.
  Register it in `.github/scripts/build_wiki.py` `PAGES` or it **never syncs to
  the wiki** (a test enforces this).
- **`docs/<kind>-onboarding.md`** — the **operator onboarding / runbook**
  (distinct from the reference above, which is "how it's built"). This is the
  "what to expect + the critical steps to bring this hardware online, easy for
  the next person or agent" doc. Model on
  [`docs/amt-onboarding.md`](amt-onboarding.md). It MUST cover: what the device
  is and its honest capability/limitation expectations; prerequisites; the
  ordered bring-up steps (config profile → `healthcheck` intake gate → any
  listener/feature enablement → verify capabilities); a symptom→fix
  troubleshooting table seeded with the real failures the driver hit during live
  bring-up; the security posture; and the hazards that need physical hands
  (e.g. AMT's ME-firmware-update wedge needing a G3 power cycle). Register it in
  `PAGES` too. **Every hardware type needs one** — GLKVM, Redfish (iLO), IPMI
  (iDRAC6), PiKVM included; backfilling the drivers that predate this rule is
  tracked in [#220](https://github.com/DustinTrap/kvm-pilot/issues/220).
- **`docs/driver-features.md`** — the per-capability table (reliability + testing
  level). "Where this page and the ledger disagree, the ledger wins" — don't
  claim a maturity level the ledger doesn't back.
- **`docs/architecture.md`** — the driver registry list + the Migration/Step-4
  driver roster.
- **`README.md`** — the intro device list, the by-plane tool table, the
  Compatibility table + the "run live so far" line (once genuinely live), and an
  architecture paragraph.
- **`src/kvm_pilot/skill/SKILL.md`** — the bundled Claude skill: the interface
  matrix, the recovery-order levers, and the exposed-tools list. **This is where
  an agent learns your driver exists** — a `test_skill_tool_list_matches_server_surface`
  guard fails if a new MCP tool is missing here.
- **`src/kvm_pilot/mcp/README.md`** — the tool table, the driver-tool matrix, and
  the `KVM_PILOT_MCP_ALLOW_*` env gates.
- **`docs/decisions.md`** — every non-obvious "looks-wrong-but-intentional" call.
  A driver with a proprietary protocol has many (AMT: the RFB 4.0 downgrade, the
  no-`SetPixelFormat`, standard-zlib not raw-deflate, write-only boot). Record
  them so they aren't "fixed" later.
- **`docs/configuration.md`** (config keys + env vars), **`docs/firmware-registry.md`**
  (how the driver reads its version), **`docs/cli.md`** (every subcommand row).

---

## 4. Reliability, evidence & the matrices

This is what turns "the code runs" into a **maturity level** users can trust.

### 4a. Healthcheck checks + quirks
Add driver-specific checks to [`health.py`](../src/kvm_pilot/health.py) `CHECKS`,
each guarded so it self-skips on other drivers (`getattr(driver, "<marker>", None)`).
Cover the device's real intake failures and security posture — for AMT: transport
TLS (plaintext by default), provisioning/control-mode, the redirection-listener
state, the user-consent posture, the credential rules. If several checks read the
device, **share one memoized read** (`amt_health()`) so the audit doesn't flood a
rate-limiting endpoint.

Add `known_quirks()` returning the shared `Quirk` dataclass — the generic
`firmware-quirks` check renders them. A quirk is a defect + workaround, honestly
sourced (`documented` vs `observed`).

### 4b. Benchmark
The `benchmark`/`route` commands and the interface router work for any driver via
`make_driver_from_config`; a read-only op is charted automatically if the driver
`supports()` its capability. Run `kvm-pilot benchmark --driver <kind> --host <h>
--save` on real hardware for real per-op latency (the emulator only proves the
path).

### 4c. The run ledger → maturity → wiki (the evidence chain)
1. A `source=="real"` row in `src/kvm_pilot/data/test_runs.jsonl` (hand-authored,
   or produced by `kvm-pilot test-report --driver <kind> --host <h>` for the
   read-only + `--include`/`--attest` destructive probes). Record only what you
   actually verified; leave unproven capabilities out.
2. `python -m kvm_pilot.maturity --ledger … --registry … --write` derives the
   level (one run → `beta`; `rc`/`ga` need repeat runs across dates) and folds it
   into `firmware_registry.json`. Re-run with `--check` (CI enforces it).
3. The Hardware-Compatibility **wiki row regenerates automatically** on push
   (`wiki-sync.yml`); it shows `n=1` (insufficient data) until ≥3 runs. You do not
   hand-edit the wiki.

`test-report` note: its probes are format/identity-agnostic only if your driver
plays along — return a real `get_firmware_info` (§1c) and a standard image
(JPEG/PNG) from `snapshot`.

---

## Definition of Done

A driver is **first-class** when all of these are true:

**Driver**
- [ ] Implements the capability protocols it supports; `capabilities()` is accurate.
- [ ] HID (if any) accepts the rich CLI/MCP signatures; pixel-native maps onto the framebuffer.
- [ ] `from_config`; registered in the driver registry, `--driver` choices+help, config fields+env.
- [ ] `get_firmware_info()` returns vendor/product/version.
- [ ] Every destructive op gated + in `DESTRUCTIVE_OPS`/`OP_EFFECT`; sensitive ops get a dedicated gate.
- [ ] Driver-specific methods have CLI (and, where safe, MCP) surfaces.

**Tests (≥90% package coverage, all green)**
- [ ] Unit tests for every method + error path; pure functions have known-answer vectors.
- [ ] A stdlib emulator exercised over the real transport, with failure knobs.
- [ ] CLI-through-`main()` and MCP-through-session tests (incl. capability/gating).
- [ ] Added to `tests/test_driver_contract.py`.
- [ ] Error-path parity with the mature drivers (auth, transport, redaction, refusal, teardown).

**Docs** — reference page (+ wiki `PAGES`), **operator onboarding/runbook page
(`docs/<kind>-onboarding.md`, + wiki `PAGES`)**, driver-features, architecture,
README, SKILL.md, mcp/README.md, decisions.md, configuration.md,
firmware-registry.md, cli.md.

**Reliability & matrices**
- [ ] Healthcheck checks + `known_quirks()`.
- [ ] Benchmarked on real hardware.
- [ ] A `source=real` ledger row → derived maturity → registry (drift-check passes) → wiki row.
- [ ] No claim exceeds the ledger.

**Tracking** — a GitHub issue referenced by the commits; findings posted back.

---

*Living document — refine it as each new plugin teaches us something. See the AMT
build-out ([#211](https://github.com/DustinTrap/kvm-pilot/issues/211)) for the
worked example every section is drawn from.*
