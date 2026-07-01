#!/usr/bin/env python3
"""
power_cycle_verify.py — HARD power-cycle a host (forced power-off, then on) and
verify it boots back to a login prompt (or desktop), using vision to confirm
rather than guessing on a timer.

The power cycle is a *forced* power-off: if the host is running, unsaved state
is lost. By default this script is a DRY RUN — the destructive calls are logged
and skipped, nothing is sent. Pass --commit to really send them; each
destructive step then asks for y/N confirmation unless you also pass --yes.

Usage:
    export KVM_PILOT_HOST=192.168.8.1 KVM_PILOT_PASSWD=secret ANTHROPIC_API_KEY=...
    python power_cycle_verify.py                  # dry run: log, don't send
    python power_cycle_verify.py --commit         # real run, prompts y/N
    python power_cycle_verify.py --commit --yes   # real run, unattended
    python power_cycle_verify.py --commit --local http://127.0.0.1:1234/v1 qwen2.5-vl-7b
"""

from __future__ import annotations

import argparse
import sys

from kvm_pilot import KVMClient, resolve_host
from kvm_pilot.errors import SafetyError
from kvm_pilot.safety import allow_all, interactive_confirm
from kvm_pilot.vision import ScreenAnalyzer, make_backend


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--commit", action="store_true",
                    help="Really send the power calls (default: dry run — log only)")
    ap.add_argument("--yes", action="store_true",
                    help="With --commit: skip the per-operation y/N prompts")
    ap.add_argument("--local", nargs=2, metavar=("URL", "MODEL"),
                    help="Use a local OpenAI-compatible VLM instead of Claude")
    ap.add_argument("--timeout", type=float, default=300.0)
    args = ap.parse_args()

    cfg = resolve_host()
    confirm = allow_all if args.yes else interactive_confirm
    kvm = KVMClient.from_config(cfg, confirm=confirm, dry_run=not args.commit)

    if args.local:
        backend = make_backend("local", base_url=args.local[0], model=args.local[1])
    else:
        backend = make_backend("anthropic")
    analyzer = ScreenAnalyzer(kvm, backend)

    print(f"Power state before: {'on' if kvm.is_powered_on() else 'off'}")
    print("Hard power cycling (forced power-off, then on)...")
    try:
        kvm.hard_cycle()
    except SafetyError as exc:
        print(f"Aborted: {exc}", file=sys.stderr)
        return 3

    if not args.commit:
        print("Dry run: the power calls were logged, not sent. Re-run with --commit to execute.")
        return 0

    def show(state, elapsed):
        print(f"  [{elapsed:6.1f}s] {state.phase} ({state.confidence:.2f})")

    try:
        final = analyzer.wait_for_any_state(
            ["login_prompt", "desktop"], timeout=args.timeout, on_poll=show
        )
    except Exception as exc:  # noqa: BLE001
        print(f"FAILED: host did not reach a login/desktop state: {exc}", file=sys.stderr)
        return 1

    print(f"\nOK: host returned to {final.phase}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
