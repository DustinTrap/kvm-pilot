---
name: kvm-pilot
description: >-
  AI-driven bare-metal control of PiKVM and GL.iNet GLKVM devices (GL-RM1 /
  GL-RM1PE). Use whenever the user wants to remotely operate a headless server
  or workstation through a KVM — power on/off/cycle, mount an install ISO,
  enter BIOS/UEFI, type at a console, or watch the screen to detect boot phase
  (POST, GRUB, installer, login, crash). Backed by the `kvm-pilot` Python
  package; vision runs on Claude or a local OpenAI-compatible VLM.
---

# kvm-pilot skill

This skill is a thin wrapper over the installable `kvm-pilot` package. The code
lives in the package, not here — install it and import it rather than copying
client logic into a script.

## Setup

```bash
pip install kvm-pilot==0.1.0a1            # core, stdlib-only
pip install "kvm-pilot[totp]==0.1.0a1"    # if the device has 2FA enabled
```

`0.1.0a1` is an early alpha and is **yanked** on PyPI (opt-in only), so pin the
exact version — a bare `pip install kvm-pilot` installs nothing.

Credentials resolve from `KVM_PILOT_HOST` / `KVM_PILOT_USER` / `KVM_PILOT_PASSWD`
(or a `--profile` in `~/.config/kvm-pilot/config.toml`). For Claude vision set
`ANTHROPIC_API_KEY`; for a local VLM, point at its `/v1` URL and model.

**GLKVM devices:** the PiKVM REST API is disabled by default on GL firmware.
The user must enable it in `/etc/kvmd/nginx-kvmd.conf` on the device first, or
every call returns 404. A firmware upgrade can revert it.

## Use the library, not raw HTTP

```python
from kvm_pilot import KVMClient
from kvm_pilot.vision import ScreenAnalyzer, make_backend

kvm = KVMClient("192.168.8.1", "admin", "secret", confirm=lambda op, d: True)
analyzer = ScreenAnalyzer(kvm, make_backend("anthropic"))   # or "local"

kvm.mount_iso("https://example.com/distro.iso")
kvm.hard_cycle()
analyzer.wait_for_state("grub_menu", timeout=120)
kvm.press_key("Enter")
analyzer.wait_for_state("installer_complete", timeout=1800)
```

## Safety

Destructive operations (power off/reset, media connect/disconnect, GPIO,
Redfish resets) are gated. Pass `dry_run=True` to log without sending, or a
`confirm` callback to require approval. When acting on a user's real hardware,
prefer confirming destructive steps unless they've explicitly said otherwise.

## CLI

`kvm-pilot info | snapshot | power | power-cycle | type | key | mount |
classify | watch`. Add `--dry-run` to preview destructive actions, `--yes` to
skip prompts. See `kvm-pilot --help`.

## Worked examples

See the package's `examples/` directory: `unattended_install.py`,
`bios_audit.py`, `power_cycle_verify.py`.
