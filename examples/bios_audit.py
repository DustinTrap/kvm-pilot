#!/usr/bin/env python3
"""
bios_audit.py — drive a host into its firmware setup, then capture and OCR the
visible screen so you have a text record of BIOS/UEFI state across a fleet.

This does NOT change any firmware settings — it powers on, enters setup, and
reads what's on screen. Pair with a runbook if you want to assert specific
values.

Usage:
    export KVM_PILOT_HOST=192.168.8.1 KVM_PILOT_PASSWD=secret ANTHROPIC_API_KEY=...
    python bios_audit.py --bios-key F2 --out bios_audit.txt
"""

from __future__ import annotations

import argparse
import sys

from kvm_pilot import KVMClient, resolve_host
from kvm_pilot.safety import allow_all
from kvm_pilot.vision import ScreenAnalyzer, make_backend


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bios-key", default="F2", help="Key to enter setup (F2, Del, F10, ...)")
    ap.add_argument("--out", default="bios_audit.txt")
    ap.add_argument("--local", nargs=2, metavar=("URL", "MODEL"))
    ap.add_argument("--timeout", type=float, default=180.0)
    args = ap.parse_args()

    cfg = resolve_host()
    kvm = KVMClient.from_config(cfg, confirm=allow_all)

    backend = (make_backend("local", base_url=args.local[0], model=args.local[1])
               if args.local else make_backend("anthropic"))
    analyzer = ScreenAnalyzer(kvm, backend)

    print(f"Entering firmware setup with {args.bios_key}...")
    kvm.enter_bios(key=args.bios_key)

    try:
        state = analyzer.wait_for_any_state(
            ["bios_menu", "uefi_shell"], timeout=args.timeout,
            hint=f"We pressed {args.bios_key} during POST to enter setup.",
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Did not reach a BIOS/UEFI menu: {exc}", file=sys.stderr)
        return 1

    # Built-in OCR on the device gives a verbatim text dump of the panel.
    ocr_text = kvm.snapshot_ocr()
    with open(args.out, "w") as fh:
        fh.write(f"# BIOS audit for {cfg.host}\n")
        fh.write(f"# classifier phase: {state.phase} (confidence {state.confidence:.2f})\n")
        fh.write(f"# classifier note: {state.description}\n\n")
        fh.write(ocr_text)

    print(f"Wrote {args.out} ({len(ocr_text)} chars of OCR text)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
