#!/usr/bin/env python3
"""
unattended_install.py — the flagship workflow: mount an install ISO and drive a
bare-metal OS install by *watching the screen*, not by sleeping on a timer.

The point of doing this with vision: there is no agent on the target during
install. kvm-pilot mounts the ISO, boots it, and advances through phases
(GRUB -> installer -> progress -> complete) by classifying what's actually on
the console, so the script adapts to a slow disk or a stalled mirror instead of
racing a fixed sleep.

This is a TEMPLATE. The keystrokes between phases depend on your installer and
(ideally) your kickstart/preseed/autoinstall file. Wire those in for your
distro; the phase-watching skeleton is the reusable part.

Usage:
    export KVM_PILOT_HOST=192.168.8.1 KVM_PILOT_PASSWD=secret ANTHROPIC_API_KEY=...
    python unattended_install.py https://example.com/distro.iso
    python unattended_install.py /srv/iso/distro.iso --local http://127.0.0.1:1234/v1 qwen2.5-vl-7b
"""

from __future__ import annotations

import argparse
import sys

from kvm_pilot import KVMClient, resolve_host
from kvm_pilot.safety import interactive_confirm
from kvm_pilot.vision import ScreenAnalyzer, make_backend


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("iso", help="ISO path or URL")
    ap.add_argument("--local", nargs=2, metavar=("URL", "MODEL"))
    ap.add_argument("--yes", action="store_true", help="Skip confirmation prompts")
    ap.add_argument("--timeout", type=float, default=1800.0, help="Per-phase timeout (s)")
    args = ap.parse_args()

    cfg = resolve_host()
    confirm = (lambda op, desc: True) if args.yes else interactive_confirm
    kvm = KVMClient.from_config(cfg, confirm=confirm)

    backend = (make_backend("local", base_url=args.local[0], model=args.local[1])
               if args.local else make_backend("anthropic"))
    analyzer = ScreenAnalyzer(kvm, backend)

    def watch(phase, hint=""):
        def show(state, elapsed):
            print(f"  [{elapsed:6.1f}s] {state.phase} ({state.confidence:.2f}): {state.description[:60]}")
        print(f"Waiting for: {phase}")
        return analyzer.wait_for_state(phase, timeout=args.timeout, hint=hint, on_poll=show)

    # 1. Mount the ISO as a virtual CD-ROM and boot from it.
    print(f"Mounting {args.iso}...")
    kvm.mount_iso(args.iso, cdrom=True)
    kvm.hard_cycle()

    # 2. Bootloader of the install media.
    watch("grub_menu", hint="This is the boot menu of the install ISO.")
    kvm.press_key("Enter")   # take the default install entry

    # 3. Installer reaches its first interactive screen.
    watch("installer_welcome",
          hint="The OS installer has started and is showing its first screen.")
    # --- distro-specific: select language/keyboard, point at preseed, etc. ---
    # e.g. kvm.type_text("\n")  /  kvm.send_shortcut("...")

    # 4. Partitioning (the step most likely to vary and to need confirmation).
    watch("installer_partitioning",
          hint="The installer is at the disk/partitioning step.")
    # --- distro-specific: accept guided partitioning or apply your recipe ---

    # 5. Copying files / installing packages — just wait it out, adaptively.
    watch("installer_progress", hint="Installation is in progress.")

    # 6. Done. Most installers prompt to reboot here.
    final = watch("installer_complete",
                  hint="Installation finished; it may ask to reboot/remove media.")

    print(f"\nInstall reported complete: {final.description}")
    print("Detaching virtual media before reboot...")
    kvm.msd_disconnect()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        raise SystemExit(130) from None
