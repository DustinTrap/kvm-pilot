#!/usr/bin/env python3
"""
power_cycle_verify.py — hard power-cycle a host and verify it boots back to a
login prompt (or desktop), using vision to confirm rather than guessing on a
timer.

Usage:
    export KVM_PILOT_HOST=192.168.8.1 KVM_PILOT_PASSWD=secret ANTHROPIC_API_KEY=...
    python power_cycle_verify.py
    python power_cycle_verify.py --local http://127.0.0.1:1234/v1 qwen2.5-vl-7b
"""

from __future__ import annotations

import argparse
import sys

from kvm_pilot import KVMClient, resolve_host
from kvm_pilot.safety import allow_all
from kvm_pilot.vision import ScreenAnalyzer, make_backend


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", nargs=2, metavar=("URL", "MODEL"),
                    help="Use a local OpenAI-compatible VLM instead of Claude")
    ap.add_argument("--timeout", type=float, default=300.0)
    args = ap.parse_args()

    cfg = resolve_host()
    # allow_all here because this script's whole purpose is the power cycle;
    # in real automation you might pass an interactive or policy-based callback.
    kvm = KVMClient(cfg.host, cfg.user, cfg.passwd, port=cfg.port,
                    verify_ssl=cfg.verify_ssl, totp_secret=cfg.totp_secret,
                    confirm=allow_all)

    if args.local:
        backend = make_backend("local", base_url=args.local[0], model=args.local[1])
    else:
        backend = make_backend("anthropic")
    analyzer = ScreenAnalyzer(kvm, backend)

    print(f"Power state before: {'on' if kvm.is_powered_on() else 'off'}")
    print("Hard power cycling...")
    kvm.hard_cycle()

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
