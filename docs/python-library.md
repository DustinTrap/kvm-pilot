# Python library reference

`kvm-pilot` is also a plain Python library: the CLI and the MCP server are thin
layers over the same objects. This page is the map of that public surface —
what to import, what each piece returns, and what it raises. It is assembled
from the module docstrings; when they disagree, the code wins and this page
has a bug. The agent-facing *how to drive it safely* playbook is the bundled
skill's [Python library](../src/kvm_pilot/skill/references/library.md) page;
how to add a driver is [Writing a first-class driver](plugin-development.md).

**What is public:** everything in `kvm_pilot.__all__` plus the subpackage
names below. Anything with a leading underscore is private and may change in
any release.

```python
from kvm_pilot import (
    KVMClient, make_driver, resolve_host,          # build a driver
    SafetyPolicy, deny_all, interactive_confirm,   # gate destructive ops
    run_healthcheck, preflight, HealthReport, HealthGateError, Severity,
    KVMPilotError, SafetyError, CapabilityError,   # (the full hierarchy below)
    Capability, KVMDriver,
)
from kvm_pilot.vision import ScreenAnalyzer, make_backend, ScreenState, ALL_PHASES
```

The library core (`client.py`, `drivers/`, `http.py`, `vision/`) imports only the
standard library; third-party needs (`mcp`, `pyotp`, `websocket-client`,
`Pillow`) are imported lazily inside the feature that uses them.

## 1. Building a driver

Three ways, from most to least explicit:

```python
from kvm_pilot import KVMClient, make_driver, resolve_host
from kvm_pilot.drivers import make_driver_from_config

kvm = KVMClient("192.168.8.1", "admin", "secret")              # PiKVM family, direct
bmc = make_driver("redfish", host="idrac.lan", user="root", passwd="…")  # by kind
cfg = resolve_host("homelab")                                  # profile > env > file
drv = make_driver_from_config(cfg, dry_run=True)               # honours cfg.driver, incl. "auto"
```

`resolve_host(profile=None, **overrides)` merges explicit arguments over
`KVM_PILOT_*` environment variables over the `[hosts.NAME]` profile in the config
file (`~/.config/kvm-pilot/config.toml`, or `KVM_PILOT_CONFIG`) and returns a
`HostConfig` dataclass — every key is listed in [Configuration](configuration.md).
Naming a profile that does not exist raises `KeyError`, never a silent fallback.

`make_driver_from_config` is what the CLI and MCP server use. When
`cfg.driver == "auto"` (the default) it probes the host and resolves a concrete
kind, memoised per process — see [§9](#9-driver-auto-detection-auto) — or raises
with the probe inventory when nothing matches.

| kind | class | constructor essentials | notes |
|---|---|---|---|
| `pikvm` | `KVMClient` (= `PiKVMDriver`) | `host, user="admin", passwd="admin", *, port=443, scheme="https", verify_ssl=False, ssl_ca_file=None, timeout=30.0, totp_secret=None, dry_run=False, confirm=None, max_retries=3` | Stock PiKVM and any kvmd-API-compatible device. `verify_ssl` defaults off because these ship self-signed certs; pin with `ssl_ca_file` instead. |
| `glkvm` | `GLKVMDriver` | same as `pikvm` | GL.iNet fork. Turns a 404 across `/api/*` into `ApiDisabledError`; carries the GL quirk list. |
| `blikvm` | `BliKVMDriver` | same as `pikvm` | BliKVM fork. |
| `redfish` | `RedfishDriver` | `host, user="root", passwd="", *, port=443, scheme="https", verify_ssl=False, ssl_ca_file=None, timeout=30.0, auth="session", system_index=0, power_wait_timeout=60.0, async_timeout=120.0, dry_run=False, confirm=None, max_retries=3` | Dell iDRAC, HPE iLO, Supermicro, Lenovo XCC, OpenBMC. `auth="basic"` for BMCs/emulators with no SessionService. `system_index` picks the ComputerSystem on multi-node gear (programmatic only). |
| `ipmi` | `IpmiDriver` | `host, user="ADMIN", passwd="", *, port=623, interface="lanplus", cipher=None, ipmitool="ipmitool", timeout=30.0, dry_run=False, confirm=None` | Pre-Redfish BMCs (iDRAC6, older iLO) through the system `ipmitool` binary. |
| `amt` | `AmtDriver` | `host, user="admin", passwd="", *, port=16992, tls=False, verify_ssl=False, ssl_ca_file=None, sol_port=16994, kvm_port=5900, kvm_password=None, amtterm="amtterm", timeout=30.0, dry_run=False, confirm=None` | Intel AMT / vPro: WS-Man power/boot, SOL, and RFB KVM redirection (firmware-level video). `kvm_password` is the separate 8-character RFB password. |
| `ssh` | `SshDriver` | `host, *, ssh_channel=None` (`from_config` needs `ssh_host`) | The OS plane: a host with no KVM/BMC at all (#248). Explicit only — `auto` never selects it. |
| `fake` | `FakeDriver` | `host="fake", *, powered=False, phase="power_off", video_signal=True, hid_connected=True, ocr_text="", logs="", events=None, image=None, dry_run=False, confirm=None` | In-memory, scriptable, no network. Destructive ops gate exactly as on hardware — the right target for tests and for a dry rehearsal. |

Every driver class also has `from_config(cfg, *, confirm=None, dry_run=False)`
(the shape-aware constructor the factory uses), and `make_driver` raises
`ValueError` naming the known kinds for an unknown one. A third-party driver
plugs in with `kvm_pilot.drivers.register_driver(kind, factory)`.

## 2. Capabilities: ask before you call

Drivers are heterogeneous: a Redfish BMC has no screenshot, a GL unit has no
sensors. The `Capability` enum and the mixin every driver carries let you ask
instead of catching:

```python
from kvm_pilot import Capability

drv.capabilities()                     # set[Capability] this driver implements
drv.supports(Capability.VIDEO)         # or supports("video")
drv.video_scope()                      # VideoScope: FIRMWARE | CAPTURE | NONE
drv.close()                            # release sessions/sockets (idempotent)
```

| `Capability` | protocol (`kvm_pilot.drivers.base`) | methods |
|---|---|---|
| `system_info` | `SystemInfo` | `get_info(fields=None) -> dict` |
| `power` | `Power` | `power_on(wait=True)`, `power_off(wait=True)`, `power_off_hard(wait=True)`, `reset_hard(wait=True)`, `is_powered_on() -> bool`; plus `hard_cycle(off_delay=None, on_delay=None)` from `PowerMixin` |
| `hid` | `HID` | `type_text(text)`, `press_key(key)`, `send_shortcut(keys)`, `mouse_move(x, y)`, `mouse_click(button="left")` |
| `video` | `Video` | `snapshot() -> bytes` (JPEG), `snapshot_base64() -> str` |
| `virtual_media` | `VirtualMedia` | `mount_iso(source, image_name=None, cdrom=True) -> str`, `msd_connect()`, `msd_disconnect()`. Key off `get_msd_state()` for truth, not the return value (#243). |
| `boot_config` | `BootConfig` | `get_boot_options() -> dict`, `set_boot_device(device, *, once=True, uefi=True) -> dict` |
| `gpio` | `GPIO` | `gpio_switch(channel, state)`, `gpio_pulse(channel)` |
| `events` | `Events` | `watch_events(on_event=None, stream=True, timeout=None)` — a generator of event dicts |
| `logs` | `Logs` | `get_logs(seek=0, follow=False) -> str` |
| `boot_progress` | `BootProgress` | `get_boot_progress() -> str \| None` (a phase token, from a BMC's structured BootProgress) |
| `sensors` | `Sensors` | `read_sensors() -> dict` |
| `serial_console` | `SerialConsole` | `serial_read(timeout=1.0) -> str`, `serial_write(data)` |
| `watchdog` | `Watchdog` | `watchdog_status() -> dict`, `watchdog_arm(timeout_s)`, `watchdog_pet()` |
| `firmware_update` | `FirmwareUpdate` | `get_upgrade_status() -> dict`, `apply_firmware_update(*, image=None, dry_run=True) -> dict` |
| `ssh` | `RemoteShell` | `ssh_reachable() -> bool`, `ssh_exec(command, *, timeout=None) -> dict` — a per-profile channel to the managed host's OS, not something the KVM implements ([§8](#8-the-ssh-channels)) |

`VideoScope` answers the question `VIDEO` cannot: *will BIOS/POST/GRUB be in
the screenshot?* `FIRMWARE` (AMT KVM, a BMC's own KVM) renders the host's
framebuffer, so firmware screens are visible. `CAPTURE` (PiKVM/GLKVM/BliKVM
over HDMI/DP) sees only what the host routes to that output — on laptops that
is nothing until an OS framebuffer starts (#210). `NONE` (IPMI, a Redfish BMC
without KVM) has no video; SOL may still give text.

The protocols are `runtime_checkable`, so `isinstance(drv, Power)` works, but
`supports()` is the intended check: it reflects what the driver *declares*, and
a declared capability on a device whose combo the support matrix has never
exercised live is still unverified — see [Driver features](driver-features.md).

## 3. The PiKVM-family client (`KVMClient`)

The richest driver, grouped. All drivers share the capability methods above;
these are the extras `KVMClient` adds. Destructive ones are marked ⚡ (gated
by [§4](#4-safety)).

- **Session**: `login() -> str`, `check_auth() -> bool`, `logout()`,
  `check_api_enabled() -> dict` (raises `ApiDisabledError` on GL with the API off),
  `get_firmware_info() -> dict` (the normalised `vendor`/`product`/`version` the
  firmware registry joins on).
- **Read**: `get_info(fields=None)`, `get_logs(seek=0, follow=False)`, `get_metrics() -> str`,
  `get_streamer_state() -> dict`, `video_signal_info() -> dict`, `has_video_signal() -> bool`,
  `get_hid_state()`, `get_atx_state()`, `get_msd_state()`, `get_gpio_state()`.
- **Video**: `snapshot() -> bytes`, `snapshot_save(path) -> Path`, `snapshot_base64()`,
  `snapshot_ocr(lang="eng", region=None) -> str` (kvmd's server-side OCR),
  `display_awake()` — a context manager that holds the target display awake.
  A snapshot can fail *while video is fine* (GL headless/H.264 cases); the
  typed `SnapshotFormatError` and `UnavailableError` tell the two apart, and
  [Troubleshooting](troubleshooting.md#snapshot-fails-or-lies) has the map.
- **HID** ⚡: `type_text(text, keymap="en-us", slow=False, delay=0.0)`,
  `press_key(key, hold_ms=50)`, `key_event(key, state)`, `send_shortcut(keys)`,
  `ctrl_alt_delete()`, `send_password(passwd, keymap="en-us")`,
  `enter_bios(key="F2", wait_s=3.0)`; mouse: `mouse_move(x, y)` (absolute
  kvmd units), `mouse_move_pixels(...)`, `mouse_move_percent(x_pct, y_pct)`,
  `mouse_move_rel(dx, dy)`, `mouse_click(button="left", hold_ms=50, double=False)`,
  `mouse_scroll(delta_x=0, delta_y=-3)`; `set_hid_params(...)`.
  Recovery: `reset_hid()`, `recover_hid(timeout=5.0) -> bool`, `set_jiggler(active) -> dict`.
- **Power** ⚡: `power_on`, `power_off`, `power_off_hard`, `reset_hard` (all
  `wait=True`), `atx_click(button="power", wait=True)`, `hard_cycle()`,
  `is_powered_on()`, `wait_for_power_state(target, timeout=60, poll=2.0)`.
  GL units report ATX state unreliably — verify with the OS or the screen.
- **Virtual media** ⚡: `mount_iso(source, image_name=None, cdrom=True) -> str`
  (local path or URL; verifies the medium reports online, else
  `MediaOfflineError`), `msd_upload_file(...)`, `msd_upload_url(...)`,
  `msd_set_params(...)`, `msd_connect()`, `msd_disconnect()`,
  `msd_remove_image(name)`, `msd_reset()`.
- **GPIO** ⚡: `gpio_switch(channel, state, wait=True)`, `gpio_pulse(channel, delay=0.1, wait=True)`.
- **Events**: `watch_events(on_event=None, stream=True, timeout=None)` over the
  kvmd WebSocket (`websocket-client` is a base dependency).

`GLKVMDriver` and `BliKVMDriver` subclass it and override only the deltas; GL
specifics (quirks, the on-demand encoder wake-up before `snapshot`, the
firmware-update surface) live in `kvm_pilot.drivers.glkvm`.

## 4. Safety

Every driver carries a `SafetyPolicy` at `drv.safety`, built from the
`dry_run` / `confirm` constructor arguments. Any operation that can change the
target's running state is in the explicit, auditable set
`kvm_pilot.safety.DESTRUCTIVE_OPS` (`atx.*`, `wol.wake`, `msd.*`, `gpio.*`,
`hid.*`, `redfish.*`, `ipmi.*`, `amt.*`, `ssh.exec`, …) and routes through
`self.safety.guard(op, description)` before anything is sent. Two independent
guards, evaluated in this order:

1. **`dry_run=True`** — the call is logged and skipped; the confirm callback is
   never invoked, so dry runs never prompt or block. Read-only calls always run.
2. **`confirm(op, description) -> bool`** — consulted only for a call that would
   really be sent. `False` blocks it with `SafetyError`.

```python
from kvm_pilot import SafetyPolicy, deny_all, interactive_confirm, SafetyError
from kvm_pilot.safety import allow_all, DESTRUCTIVE_OPS, effect_of, EffectClass

drv = make_driver("fake", confirm=deny_all)
try:
    drv.power_on()
except SafetyError:
    ...                                   # blocked, nothing sent
drv.safety.dry_run = True                 # flip at runtime; dry-run now wins
drv.power_on()                            # logged, skipped, no prompt
effect_of("atx.reset_hard") is EffectClass.POWER_HARD   # True
```

**The library default is `allow_all`** so plain scripts work; the CLI installs
`interactive_confirm` (a y/N prompt that *fails closed* without a TTY) and the
MCP server installs its own per-call approval flow. Omitting `confirm` therefore
means *unattended*; pass `deny_all`, `interactive_confirm`, or a callback that
relays the question to a human.

`EffectClass` / `effect_of(op)` are the additive layer the MCP server uses to
pick the operator enable-flag by *effect* rather than transport (Ctrl+Alt+Del
is `POWER_SOFT` even though it travels over the keyboard); `shortcut_effect(keys)`
classifies a chord. `DESTRUCTIVE_OPS` stays the single source of truth for
"is this gated at all".

## 5. Health: the intake gate

```python
from kvm_pilot import run_healthcheck, preflight, HealthGateError, Severity

report = run_healthcheck(drv)             # every applicable check, never raises
report.worst                              # Severity.OK | INFO | WARNING | CRITICAL
report.criticals                          # the CRITICAL CheckResults
report.to_dict()                          # JSON-safe (what `healthcheck --json` prints)
for r in report.results:
    r.id, r.pillar, r.severity, r.title, r.detail, r.remediation, r.auto_fix

preflight(drv, confirm=None, skip=False, cache=None, enforce=True)
```

`run_healthcheck` audits readiness (API/recovery path/HID/video), security
posture (TLS, default credentials, exposed services) and firmware (currency
against the bundled registry, capability profile, live evidence). Checks that
raise are reported, not propagated. `preflight` is the gate the destructive CLI
commands run first: stable checks come from `cache` when fresh (a firmware
change invalidates them and the report carries a `firmware-delta`), volatile
checks always run live, and an unacknowledged `CRITICAL` raises
`HealthGateError` unless `enforce=False` or `skip=True`. `preflight_once`
memoises per process. A `CheckResult.auto_fix`, when present, is an opt-in
remediation (`AutoFix.apply(driver)`; `safe_reversible` says whether it may
be applied with per-item consent) — the audit itself never applies one.

## 6. Errors

All library errors derive from `KVMPilotError` (`.status_code` carries the HTTP
status when there was one). Catch the base for "the device said no", the
subclasses to react differently:

| exception | raised when |
|---|---|
| `AuthError` | 401/403 — credentials, or a 2FA device without `totp_secret` |
| `ConnectionError` | could not reach the device at all (DNS, refused, TLS) — shadows the builtin inside the package namespace on purpose |
| `TimeoutError` | an operation or wait loop exceeded its deadline (also shadows the builtin) |
| `BusyError` | 409 / `result.busy` — the subsystem is mid-operation; retry later |
| `UnavailableError` | 503 — streamer down, ATX board absent, encoder asleep |
| `ApiDisabledError` | every `/api/*` 404s (GL firmware ships the REST API off) |
| `SnapshotFormatError` | the snapshot endpoint returned non-JPEG bytes (GL raw H.264 at native resolution, #107) |
| `MediaOfflineError` | virtual media attached but the device reports it offline — the host sees nothing (#77) |
| `ProtocolError` | a non-JSON body where JSON was expected (truncated response); deliberately not retried |
| `CapabilityError` | the driver does not implement what you asked for (also: a required system binary such as `ipmitool`/`ssh` is missing) |
| `SafetyError` | the confirm callback denied a destructive op |
| `VisionError` | the vision backend failed or returned nothing usable; `.retry_after` carries a 429's `Retry-After` |
| `HealthGateError` | `preflight` blocked on an unacknowledged CRITICAL |

The transport retries transient failures (busy/unavailable/network) up to
`max_retries` with backoff, and redacts secrets from logged URLs and bodies.

## 7. Vision

```python
from kvm_pilot.vision import ScreenAnalyzer, make_backend, ALL_PHASES

backend = make_backend("anthropic")                      # model auto-resolved at runtime
backend = make_backend("local", base_url="http://127.0.0.1:1234/v1", model="qwen2.5-vl-7b")

analyzer = ScreenAnalyzer(
    drv, backend,
    default_poll_interval=3.0, min_confidence=0.70, on_state_change=None,
    gate_on_power_signal=True, gate_on_boot_progress=True,
    skip_unchanged_frames=True, ocr_rules=None,
)
state = analyzer.classify(hint="")                       # -> ScreenState
state.phase, state.description, state.confidence, state.raw_text, state.timestamp
analyzer.wait_for_state("grub_menu", timeout=120)        # -> ScreenState, or TimeoutError
analyzer.wait_for_any_state(["login_prompt", "desktop"], timeout=600,
                            poll_interval=None, hint="", min_confidence=None,
                            consensus=2, on_poll=None)
analyzer.vlm_calls, analyzer.cheap_resolves              # how often the gates avoided a model call
```

- **Backends** — `make_backend("anthropic" | "claude")` builds `AnthropicBackend`
  (`api_key`/`model` optional; the newest vision-capable model is resolved at
  runtime, never hard-coded); `make_backend("local" | "openai" | "lmstudio" |
  "ollama" | "vllm", base_url=…, model=…)` builds `OpenAICompatBackend` for any
  OpenAI-compatible server, so frames never leave your network. A backend is
  anything implementing `VisionBackend`: `classify(image_b64, hint="") ->
  ScreenState` and a `model` property.
- **Phases** — `ALL_PHASES` is the closed vocabulary (`no_signal`, `power_off`,
  `post_screen`, `bios_menu`, `uefi_shell`, `grub_menu`, `booting`,
  `login_prompt`, `desktop`, `os_running`, `installer_welcome`,
  `installer_partitioning`, `installer_progress`, `installer_complete`,
  `crash_screen`, `unknown`); `parse_classification` turns a model reply into a
  `ScreenState`.
- **Sensing hierarchy** — `classify()` tries the cheap signals first and only
  then the model: the driver's power signal (`is_powered_on` /
  `has_video_signal` → `power_off` / `no_signal`), a BMC's structured
  `get_boot_progress()`, then `snapshot_ocr` against `ocr_rules`
  (`DEFAULT_OCR_RULES` maps "gnu grub" → `grub_menu`, "kernel panic" →
  `crash_screen`, "uefi interactive shell" → `uefi_shell`). An unchanged frame
  reuses the last *actionable* result. Pass `image_b64=` to classify a specific
  frame verbatim with the gates off.
- **Consensus** — `wait_for_any_state` requires the target phase on
  `consensus` consecutive polls (default 2) so one transient black frame or
  model misread cannot end the wait (#166). A `VisionError` 429 honours
  `retry_after`.

A vision classification is a *sensor reading*. It must never trigger a
destructive action on its own — the gate in [§4](#4-safety) exists so that
the decision stays with the caller.

## 8. The SSH channels

Two different machines, two channels, both in `kvm_pilot.ssh`, both driven by
the system `ssh` binary (no library dependency):

```python
from kvm_pilot.ssh import SSHChannel, ApplianceChannel

ch = SSHChannel.from_config(cfg, confirm=None, dry_run=False)   # the MANAGED HOST (cfg.ssh_host)
ch.ssh_reachable()                        # bool, read-only
ch.ssh_exec("uptime", timeout=30)         # {command, returncode, stdout, stderr, ok, dry_run}; gated as ssh.exec

ap = ApplianceChannel.from_config(cfg)    # the KVM APPLIANCE's own OS (root@<kvm-ip>, opt-in)
ap.loadavg(); ap.d_state_video_threads(); ap.reboot()           # reboot is gated
```

`make_driver_from_config` attaches the host channel to the driver as
`drv.ssh_channel` when the profile has `ssh_host`, which is what makes
`Capability.SSH` appear. `discover_ssh_hosts(cidr, ...)` is the opt-in subnet
sweep behind `kvm-pilot ssh-discover`.

## 9. Driver auto-detection (`auto`)

With no driver pinned anywhere, `make_driver_from_config` runs cheap read-only
probes in verdict-priority order (`kvm_pilot.detect.resolve_auto`); the first
marker wins and the rest are skipped:

| verdict | probe | marker |
|---|---|---|
| `pikvm` | `GET /api/info` on the API port | kvmd-style JSON (`ok` key) |
| `glkvm` | `GET /api/upgrade/version` (authenticated) | GL's proprietary endpoint (GL self-reports as a Raspberry Pi PiKVM, so this is the only tell) |
| `redfish` | `GET /redfish/v1/` | `RedfishVersion` / odata |
| `amt` | `GET /` on 16992 (16993 with TLS) | Intel AMT `Server` banner |
| `ipmi` | RMCP/ASF presence ping (UDP 623) | ASF pong |

Only when nothing matches is the full inventory assembled — including an SSH
banner probe on :22 — so the failure can say "this host answers SSH only, not
a KVM/BMC" instead of a misleading CRITICAL (#235). SSH never wins the verdict.
Pin `driver=` in the profile to skip the probe entirely.

## 10. Evidence data: registry, ledger, maturity

Read-only helpers over the data shipped in the wheel, for tools that want to
reason about what has been *verified* rather than what is *declared*:

```python
from kvm_pilot.firmware_registry import load_registry      # bundled registry (+ cache/override precedence)
from kvm_pilot.support_matrix import load_ledger, rollup   # live run ledger -> per-combo evidence rows
from kvm_pilot.maturity import compute_maturity            # alpha/beta/rc/ga from a combo's records

rows = rollup(vendor="gl.inet", product="RM1PE")
```

The model, the promotion ladder and the ingestion pipeline are in
[Firmware registry](firmware-registry.md); the per-driver view is
[Driver features](driver-features.md); the community page it all renders to is
the [Hardware-Compatibility](https://github.com/DustinTrap/kvm-pilot/wiki/Hardware-Compatibility)
wiki list.

## 11. A complete, safe script

```python
from kvm_pilot import make_driver, run_healthcheck, Severity, interactive_confirm
from kvm_pilot.vision import ScreenAnalyzer, make_backend

kvm = make_driver("glkvm", host="192.168.8.1", user="admin", passwd="secret",
                  confirm=interactive_confirm)        # ask before every destructive step

report = run_healthcheck(kvm)                         # intake gate first, always
if report.worst is Severity.CRITICAL:
    raise SystemExit("\n".join(r.title for r in report.criticals))

analyzer = ScreenAnalyzer(kvm, make_backend("anthropic"))
kvm.mount_iso("https://example.com/distro.iso")       # prompts
kvm.hard_cycle()                                      # prompts
analyzer.wait_for_state("grub_menu", timeout=120)
kvm.press_key("Enter")                                # prompts
analyzer.wait_for_state("installer_complete", timeout=1800)
kvm.close()
```

Rehearse it first with `dry_run=True` on the real profile, or run it unchanged
against `make_driver("fake", powered=True, phase="grub_menu")` — the fake reports
that phase as structured boot progress, so the analyzer resolves it without a model call.
