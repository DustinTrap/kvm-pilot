# Using the Python library

> Part of the bundled kvm-pilot skill. Read this when driving the library
> directly (scripts, MSD mode switching, anything the CLI/MCP don't cover).
> Also served at runtime by the MCP `doctrine` tool (topic "library").

## Use the library, not raw HTTP

**First contact: rehearse with `dry_run=True`.** Dry-run short-circuits before
anything else — destructive calls are logged and skipped (the confirm callback
is never invoked), so the whole flow can be validated without changing the
machine's state:

```python
from kvm_pilot import KVMClient

kvm = KVMClient("192.168.8.1", "admin", "secret", dry_run=True)
kvm.mount_iso("https://example.com/distro.iso")   # logged, not sent
kvm.hard_cycle()                                  # logged, not sent
```

**Real run: gate every destructive step on explicit approval.**
`interactive_confirm` prompts on stdin and *fails closed* (denies) when there
is no TTY. In an agent context, ask the user in chat before each destructive
step and wire their answer into the callback:

```python
from kvm_pilot import KVMClient
from kvm_pilot.safety import interactive_confirm
from kvm_pilot.vision import ScreenAnalyzer, make_backend

kvm = KVMClient("192.168.8.1", "admin", "secret", confirm=interactive_confirm)
analyzer = ScreenAnalyzer(kvm, make_backend("anthropic"))   # or "local"

kvm.mount_iso("https://example.com/distro.iso")   # gate: asks before mounting
kvm.hard_cycle()                                  # gate: asks before power off/on
analyzer.wait_for_state("grub_menu", timeout=120)
kvm.press_key("Enter")                            # keystroke injection is gated too
analyzer.wait_for_state("installer_complete", timeout=1800)
```

**Never pass an allow-all confirm callback** (e.g. `lambda op, d: True`) unless
the user has explicitly approved unattended destructive operation in this
session. And note that **omitting `confirm` is also unattended** — the library
default allows everything so plain scripts work — so actively pass
`interactive_confirm` (or a callback that relays the question to the user);
the ask-first duty sits with you, not the library.

## Safety

Destructive operations — power off/reset, virtual-media connect/disconnect and
image uploads, keyboard/mouse injection (`type_text`, `press_key`, shortcuts,
clicks), GPIO, Redfish resets — are gated by `SafetyPolicy`
(`kvm_pilot.safety.DESTRUCTIVE_OPS` is the explicit, auditable set):

- `dry_run=True` short-circuits **first**: the call is logged and skipped and
  the confirm callback is never invoked, so dry runs never prompt or block.
- The `confirm` callback runs only for calls that would really be sent;
  returning `False` blocks the call with `SafetyError`.

When acting on a user's real hardware, remember most device+capability combos
are still unverified (check the `support_matrix` MCP tool or the
Hardware-Compatibility wiki page) — confirm each destructive step with the user
first unless they have explicitly said otherwise.

## Worked examples

In the repository (the `examples/` directory is **not shipped inside the pip
package**):

- [`examples/unattended_install.py`](https://github.com/DustinTrap/kvm-pilot/blob/main/examples/unattended_install.py) — mount an ISO and drive an OS install by watching the screen.
- [`examples/bios_audit.py`](https://github.com/DustinTrap/kvm-pilot/blob/main/examples/bios_audit.py) — hard-cycle into firmware setup and OCR what's on screen.
- [`examples/power_cycle_verify.py`](https://github.com/DustinTrap/kvm-pilot/blob/main/examples/power_cycle_verify.py) — hard power-cycle and verify the host comes back.

All three default to the safe path (dry run and/or interactive confirmation);
copy that pattern, not an allow-all one.
