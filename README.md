# kvm-pilot

**AI-driven bare-metal control for PiKVM and the GL.iNet GLKVM fork (GL-RM1 / GL-RM1PE).**

`kvm-pilot` is a stdlib-only Python client for the PiKVM REST API, a safety layer
that gates destructive power/media operations, and a pluggable vision subsystem
that reads a KVM screenshot and tells you what boot phase the machine is in —
`bios_menu`, `grub_menu`, `installer_progress`, `login_prompt`, `crash_screen`,
and so on. That last part is the point: it lets you drive a headless box through
POST, firmware, bootloader, and OS install **with no agent on the target**,
because the classifier works at the pixel level where there is no OS to cooperate.

Vision runs on Claude **or** any local OpenAI-compatible VLM (LM Studio, Ollama,
vLLM, llama.cpp). Point it at a model on your own GPU and the screenshots never
leave your network and cost nothing per frame.

> **Status:** v0.1.0a1 — **early alpha.** This code has **not been run against
> real hardware** — not even the GL-RM1PE it targets. It is unit-tested only
> (mocked HTTP and vision responses), so treat every feature as unverified,
> expect bugs and breaking API changes before 1.0, and don't point it at a
> machine you can't afford to have power-cycled unexpectedly. Hardware reports —
> success *or* failure — are exactly what this release is asking for; see
> [Compatibility](#compatibility).

---

## ⚠️ GLKVM users: enable the PiKVM API first

On GL.iNet firmware the PiKVM REST API is **disabled by default**. Until you
enable it, every `/api/*` call returns 404 and `kvm-pilot` cannot talk to the
device. To enable it, SSH into the unit (or use the app's terminal) and
uncomment the relevant block in:

```
/etc/kvmd/nginx-kvmd.conf
```

then restart the service (or reboot the unit). Note that a **firmware upgrade can
revert this**, so you may need to redo it after updates. This is a GL firmware
behavior, not a `kvm-pilot` setting. Stock PiKVM devices expose the API by
default and need no change.

---

## Install

```bash
pip install --pre kvm-pilot           # core, zero runtime dependencies
pip install --pre "kvm-pilot[totp]"   # + 2FA / TOTP support (pyotp)
pip install --pre "kvm-pilot[ws]"     # + WebSocket event streaming
```

`--pre` is required: `0.1.0a1` is an **alpha pre-release**, so a plain
`pip install kvm-pilot` will not select it. The core has **no third-party
runtime dependencies** — it is pure standard library. Extras are opt-in.

## Quickstart

```python
from kvm_pilot import KVMClient
from kvm_pilot.vision import ScreenAnalyzer, make_backend

kvm = KVMClient("192.168.8.1", "admin", "secret")

# Classify the current screen with Claude (model auto-resolved at runtime)
analyzer = ScreenAnalyzer(kvm, make_backend("anthropic"))
print(analyzer.classify().phase)

# Or run entirely on a local VLM — nothing leaves your network
local = make_backend("local", base_url="http://127.0.0.1:1234/v1", model="qwen2.5-vl-7b")
analyzer = ScreenAnalyzer(kvm, local)

# Block until the box reaches the GRUB menu, then pick the first entry
analyzer.wait_for_state("grub_menu", timeout=120)
kvm.press_key("Enter")
```

### CLI

```bash
kvm-pilot info     --host 192.168.8.1 --user admin --passwd secret
kvm-pilot snapshot screen.jpg --profile homelab
kvm-pilot power-cycle --profile homelab --dry-run        # log, don't send
kvm-pilot watch grub_menu --profile homelab \
    --backend local --vision-url http://127.0.0.1:1234/v1 --vision-model qwen2.5-vl-7b
```

The CLI prompts for confirmation before any destructive action. Use `--yes` to
skip prompts in automation, or `--dry-run` to log intended actions without
sending them.

## Safety model

Power-offs, hard resets, virtual-media connect/disconnect, GPIO, and Redfish
resets are classified as **destructive** and pass through a safety layer:

- **dry-run** logs the intended call and skips it entirely.
- **confirmation** — a callback that can veto any destructive call. The library
  default allows everything (so plain scripts work); the CLI installs an
  interactive `y/N` prompt unless you pass `--yes`.

The destructive set is defined explicitly in `kvm_pilot.safety.DESTRUCTIVE_OPS`
so it is auditable rather than guessed. A vision classification can never
trigger a destructive action on its own — you wire that yourself, and the
safety layer still applies.

This software controls real hardware and can power-cycle or interrupt a running
machine. Read [SECURITY.md](SECURITY.md) before exposing a KVM to the internet.

## No hard-coded model version

There is no model version string anywhere in the code. The Anthropic backend
resolves the newest vision-capable model at runtime via the Models API and
caches it; set `KVM_PILOT_VISION_MODEL` or pass `model=` to pin one. The local
backend uses whatever model you loaded on your server. Bring your own backend,
endpoint, and model.

## How this differs from `pikvm-lib`

[`pikvm-lib`](https://github.com/guanana/pikvm-lib) is a fine general-purpose
PiKVM client. `kvm-pilot` is aimed at a different job:

- **Vision-based boot-phase detection** — classify BIOS/GRUB/installer/crash
  states from screenshots, with blocking `wait_for_state` loops. This is the
  core feature and `pikvm-lib` has no equivalent.
- **Pluggable local or cloud VLM** — run inference on your own GPU at zero
  per-frame cost, or on Claude.
- **A safety layer** around destructive operations (dry-run + confirmation).
- **GLKVM-fork awareness** — documents the API-enable prerequisite and GL
  hardware quirks that bite GL-RM1PE users.
- **Zero runtime dependencies** in the core.

If you just want to script power and HID against a stock PiKVM and don't need
the vision layer, `pikvm-lib` may be the simpler choice.

## Compatibility

| Device | Status |
|--------|--------|
| GL-RM1PE (Comet PoE) | Primary development target — **not yet hardware-tested** |
| GL-RM1 (Comet) | Expected to work (same firmware family); untested |
| PiKVM v3 / v4 | Expected to work (upstream API); untested |
| BliKVM | Expected to work (PiKVM-compatible API); untested |

**Nothing in this table has been verified on real hardware yet** — the entire
matrix is "expected to work" pending validation. The ATX power features also
require the optional ATX add-on board; without it, ATX calls will return errors
from the device. Reports of success or failure on *any* hardware are exactly
what this alpha needs — please open an issue.

## License

Apache License 2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE). `kvm-pilot` is
independent and not affiliated with or endorsed by the PiKVM project, GL.iNet,
or Anthropic; those names are used only for compatibility description.
