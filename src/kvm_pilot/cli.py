"""
kvm-pilot command-line interface (stdlib argparse, no third-party deps).

The CLI defaults to *interactive confirmation* on destructive operations; pass
--yes to skip prompts (for automation) or --dry-run to log intended actions
without sending them. Credentials resolve through kvm_pilot.config (flags > env
> config-file profile).

Examples:
    kvm-pilot info --host 192.168.8.1 --user admin --passwd secret
    kvm-pilot snapshot out.jpg --profile homelab
    kvm-pilot power-cycle --profile homelab --dry-run
    kvm-pilot watch grub_menu --profile homelab --backend local \\
        --vision-url http://127.0.0.1:1234/v1 --vision-model qwen2.5-vl-7b
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from .__about__ import __version__
from .client import KVMClient
from .config import resolve_host
from .errors import KVMPilotError, SafetyError
from .safety import allow_all, interactive_confirm


def _build_client(args) -> KVMClient:
    cfg = resolve_host(
        getattr(args, "profile", None),
        host=getattr(args, "host", None),
        user=getattr(args, "user", None),
        passwd=getattr(args, "passwd", None),
        port=getattr(args, "port", None),
        totp_secret=getattr(args, "totp_secret", None),
        verify_ssl=getattr(args, "verify_ssl", None),
    )
    confirm = allow_all if getattr(args, "yes", False) else interactive_confirm
    return KVMClient(
        cfg.host,
        cfg.user,
        cfg.passwd,
        port=cfg.port,
        scheme=cfg.scheme,
        verify_ssl=cfg.verify_ssl,
        timeout=cfg.timeout,
        totp_secret=cfg.totp_secret,
        dry_run=getattr(args, "dry_run", False),
        confirm=confirm,
    )


def _make_analyzer(kvm: KVMClient, args):
    from .vision import ScreenAnalyzer, make_backend

    if args.backend in ("local", "openai"):
        backend = make_backend(
            "local", base_url=args.vision_url, model=args.vision_model
        )
    else:
        backend = make_backend(
            "anthropic", model=getattr(args, "vision_model", None) or None
        )
    return ScreenAnalyzer(kvm, backend)


# -- subcommand handlers ---------------------------------------------------

def cmd_info(args) -> int:
    kvm = _build_client(args)
    print(json.dumps(kvm.get_info(), indent=2, default=str))
    return 0


def cmd_snapshot(args) -> int:
    kvm = _build_client(args)
    out = kvm.snapshot_save(args.output)
    print(f"Saved {out}")
    return 0


def cmd_power(args) -> int:
    kvm = _build_client(args)
    action = {
        "on": kvm.power_on,
        "off": kvm.power_off,
        "off-hard": kvm.power_off_hard,
        "reset": kvm.reset_hard,
    }[args.action]
    action()
    print(f"power {args.action}: requested")
    return 0


def cmd_power_cycle(args) -> int:
    kvm = _build_client(args)
    kvm.hard_cycle()
    print("hard power cycle: requested")
    return 0


def cmd_type(args) -> int:
    kvm = _build_client(args)
    kvm.type_text(args.text, slow=args.slow)
    return 0


def cmd_key(args) -> int:
    kvm = _build_client(args)
    kvm.press_key(args.key)
    return 0


def cmd_mount(args) -> int:
    kvm = _build_client(args)
    name = kvm.mount_iso(args.source, image_name=args.name, cdrom=not args.usb)
    print(f"mounted: {name}")
    return 0


def cmd_classify(args) -> int:
    kvm = _build_client(args)
    analyzer = _make_analyzer(kvm, args)
    state = analyzer.classify(hint=args.hint or "")
    print(json.dumps(state.to_dict(), indent=2, default=str))
    return 0


def cmd_watch(args) -> int:
    kvm = _build_client(args)
    analyzer = _make_analyzer(kvm, args)

    def _progress(state, elapsed):
        print(f"  [{elapsed:6.1f}s] {state.phase} ({state.confidence:.2f}): {state.description[:70]}")

    try:
        final = analyzer.wait_for_state(
            args.phase, timeout=args.timeout, hint=args.hint or "", on_poll=_progress
        )
    except KVMPilotError as exc:
        print(f"watch failed: {exc}", file=sys.stderr)
        return 2
    print(f"\nreached: {final.phase}")
    print(json.dumps(final.to_dict(), indent=2, default=str))
    return 0


# -- parser ----------------------------------------------------------------

def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--host")
    p.add_argument("--user")
    p.add_argument("--passwd")
    p.add_argument("--port", type=int)
    p.add_argument("--profile", help="Named host profile from the config file")
    p.add_argument("--totp-secret", dest="totp_secret")
    p.add_argument("--verify-ssl", dest="verify_ssl", action="store_true", default=None)
    p.add_argument("--dry-run", dest="dry_run", action="store_true",
                   help="Log destructive actions without sending them")
    p.add_argument("--yes", "-y", action="store_true",
                   help="Skip interactive confirmation on destructive actions")


def _add_vision(p: argparse.ArgumentParser) -> None:
    p.add_argument("--backend", choices=["anthropic", "local", "openai"], default="anthropic")
    p.add_argument("--vision-url", dest="vision_url",
                   help="Base URL for a local OpenAI-compatible VLM (e.g. http://127.0.0.1:1234/v1)")
    p.add_argument("--vision-model", dest="vision_model",
                   help="Model name. Local: required. Anthropic: optional override (auto-resolved if omitted)")
    p.add_argument("--hint", help="Optional context hint for the classifier")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kvm-pilot", description=__doc__)
    parser.add_argument("--version", action="version", version=f"kvm-pilot {__version__}")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("info", help="Print device info as JSON")
    _add_common(p)
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("snapshot", help="Save a screenshot")
    p.add_argument("output")
    _add_common(p)
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("power", help="Power action")
    p.add_argument("action", choices=["on", "off", "off-hard", "reset"])
    _add_common(p)
    p.set_defaults(func=cmd_power)

    p = sub.add_parser("power-cycle", help="Hard power cycle (off-hard -> on)")
    _add_common(p)
    p.set_defaults(func=cmd_power_cycle)

    p = sub.add_parser("type", help="Type text on the host")
    p.add_argument("text")
    p.add_argument("--slow", action="store_true")
    _add_common(p)
    p.set_defaults(func=cmd_type)

    p = sub.add_parser("key", help="Press a single key")
    p.add_argument("key")
    _add_common(p)
    p.set_defaults(func=cmd_key)

    p = sub.add_parser("mount", help="Mount an ISO (local path or URL)")
    p.add_argument("source")
    p.add_argument("--name")
    p.add_argument("--usb", action="store_true")
    _add_common(p)
    p.set_defaults(func=cmd_mount)

    p = sub.add_parser("classify", help="Classify the current screen once")
    _add_common(p)
    _add_vision(p)
    p.set_defaults(func=cmd_classify)

    p = sub.add_parser("watch", help="Wait until the screen reaches a phase")
    p.add_argument("phase")
    p.add_argument("--timeout", type=float, default=300.0)
    _add_common(p)
    _add_vision(p)
    p.set_defaults(func=cmd_watch)

    return parser


def main(argv: list | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    try:
        return args.func(args)
    except SafetyError as exc:
        print(f"blocked: {exc}", file=sys.stderr)
        return 3
    except KVMPilotError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
