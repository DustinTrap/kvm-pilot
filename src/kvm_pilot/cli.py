"""
kvm-pilot command-line interface (stdlib argparse, no third-party deps).

The CLI defaults to *interactive confirmation* on destructive operations; pass
--yes to skip prompts (for automation) or --dry-run to log intended actions
without sending them. Credentials resolve through kvm_pilot.config (flags > env
> config-file profile).

Prefer env/profile credentials over ``--passwd``/``--totp-secret`` on the
command line: argv is visible to any local user via ``ps`` and is persisted in
shell history. Use ``KVM_PILOT_PASSWD`` / a config profile, ``--passwd-file``,
or ``--ask-passwd`` (interactive, no echo).

Examples:
    kvm-pilot info --host 192.168.8.1 --user admin --ask-passwd
    KVM_PILOT_PASSWD=secret kvm-pilot info --host 192.168.8.1 --user admin
    kvm-pilot capabilities --profile homelab        # what this driver supports
    kvm-pilot snapshot out.jpg --profile homelab
    kvm-pilot --timeout 60 power-cycle --profile homelab --dry-run
    kvm-pilot events --profile homelab --count 5    # needs the 'ws' extra
    kvm-pilot watch grub_menu --profile homelab --backend local \\
        --vision-url http://127.0.0.1:1234/v1 --vision-model qwen2.5-vl-7b

``--timeout`` (HTTP per-request timeout) is a global flag and must precede the
subcommand; ``watch`` keeps its own ``--timeout`` for the vision wait deadline.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, cast

from .__about__ import __version__
from .client import KVMClient
from .config import resolve_host
from .drivers import make_driver_from_config
from .drivers.base import Capability
from .errors import CapabilityError, KVMPilotError, SafetyError
from .safety import allow_all, interactive_confirm

if TYPE_CHECKING:
    # Drivers the CLI can construct. KVMClient (PiKVM family) and FakeDriver expose
    # the full HID/Video/Events surface the subcommands call; RedfishDriver is
    # capability-partial (a BMC — strong on structured state, but no keyboard or
    # screen), so capability-specific subcommands gate on supports() before
    # dispatch instead of AttributeError-ing deep in a handler.
    from .drivers.base import BootProgress, Logs, Sensors
    from .drivers.fake import FakeDriver
    from .drivers.redfish import RedfishDriver

    AnyDriver = KVMClient | FakeDriver | RedfishDriver
    RichDriver = KVMClient | FakeDriver


def _first_line(path: str) -> str:
    """First line of a secret file (newline stripped), or '' if empty."""
    lines = Path(path).read_text().splitlines()
    return lines[0] if lines else ""


def _resolve_secret(direct, file_path, ask: bool, prompt: str) -> str | None:
    """A secret from --x (argv), --x-file, or an interactive --ask prompt.

    Explicit flags only — nothing prompts implicitly, so `--driver fake` and
    existing scripts relying on the admin/admin default are unaffected.
    """
    if direct is not None:
        return direct
    if file_path:
        return _first_line(file_path)
    if ask:
        import getpass
        return getpass.getpass(prompt)
    return None


def _build_client(args) -> AnyDriver:
    confirm = allow_all if getattr(args, "yes", False) else interactive_confirm
    dry_run = getattr(args, "dry_run", False)
    passwd = _resolve_secret(
        getattr(args, "passwd", None), getattr(args, "passwd_file", None),
        getattr(args, "ask_passwd", False), "Password: ",
    )
    totp_secret = _resolve_secret(
        getattr(args, "totp_secret", None), getattr(args, "totp_secret_file", None),
        False, "",
    )
    cfg = resolve_host(
        getattr(args, "profile", None),
        host=getattr(args, "host", None),
        user=getattr(args, "user", None),
        passwd=passwd,
        port=getattr(args, "port", None),
        scheme=getattr(args, "scheme", None),
        timeout=getattr(args, "http_timeout", None),
        totp_secret=totp_secret,
        verify_ssl=getattr(args, "verify_ssl", None),
        ssl_ca_file=getattr(args, "ssl_ca_file", None),
        driver=getattr(args, "driver", None),
        redfish_auth=getattr(args, "redfish_auth", None),
    )
    # Shared with the MCP server so cfg.driver is honored the same way everywhere.
    kvm = make_driver_from_config(cfg, confirm=confirm, dry_run=dry_run)
    # Stash it so main() can close() it on the way out — a RedfishDriver holds a
    # BMC session that must be DELETEd (BMCs cap sessions; a leak locks the
    # operator out). Every command routes through here exactly once.
    args._driver = kvm
    return kvm


def _skip_healthcheck(args) -> bool:
    if getattr(args, "skip_healthcheck", False):
        return True
    return os.environ.get("KVM_PILOT_SKIP_HEALTHCHECK", "").lower() in ("1", "true", "yes")


def _preflight_gate(kvm, confirm, *, skip: bool) -> None:
    from .health import HealthCache, preflight

    preflight(kvm, confirm=confirm, cache=HealthCache(), skip=skip)


def _inform_on_connect(kvm, *, skip: bool) -> None:
    """Audit a device once on first connection and print any findings (#80).

    Non-blocking: read-only intake informs and proceeds (the destructive gate is
    the blocker). Runs at most once per device per process; findings go to stderr
    so JSON on stdout stays clean.
    """
    from .health import HealthCache, Severity, preflight_once

    try:
        report = preflight_once(kvm, cache=HealthCache(), enforce=False, skip=skip)
    except Exception:  # noqa: BLE001 - an informational audit must never break the read
        return
    if report is None:
        return
    notable = [r for r in report.results if r.severity >= Severity.WARNING]
    if not notable:
        return
    print(
        f"preflight {report.driver_kind}@{report.host}: worst {report.worst}, "
        f"{len(notable)} finding(s) — run `kvm-pilot healthcheck` for detail:",
        file=sys.stderr,
    )
    for r in notable:
        print(f"  [{r.severity}] {r.pillar}: {r.title} — {r.detail}", file=sys.stderr)


def _make_analyzer(kvm: KVMClient | FakeDriver, args):
    from .vision import ScreenAnalyzer, make_backend

    if args.backend in ("local", "openai"):
        backend = make_backend(
            "local", base_url=args.vision_url, model=args.vision_model
        )
    else:
        backend = make_backend("anthropic", model=args.vision_model)
    return ScreenAnalyzer(kvm, backend)


# -- capability dispatch ---------------------------------------------------
#
# Drivers advertise capabilities structurally (drivers.base), so each subcommand
# declares the one it needs and the dispatcher fails cleanly when the active
# driver lacks it — the seam that lets a capability-partial driver (e.g.
# --driver redfish, a BMC with no HID/Video) coexist with the full-featured
# PiKVM family.


def _driver_label(kvm) -> str:
    # "RedfishDriver" -> "redfish": the registry kind, derived from the class so
    # the kind names aren't copied into a second map to keep in sync.
    return type(kvm).__name__.removesuffix("Driver").lower()


def _client(args, capability: Capability) -> AnyDriver:
    """Build the driver and fail cleanly if it can't serve this subcommand.

    The required capability is checked up front — a structural, network-free probe
    (``supports()``) — so a command a device lacks exits 1 with a clear message
    instead of ``AttributeError``-ing deep in the handler. The subcommand name for
    the message comes from ``args.command`` (the argparse ``dest``).
    """
    kvm = _build_client(args)
    if not kvm.supports(capability):
        raise CapabilityError(
            f"'{args.command}' needs the {capability.value} capability, which the "
            f"{_driver_label(kvm)} driver does not provide"
        )
    # Preflight healthcheck (#80), run AFTER the capability check so a command the
    # driver cannot serve still fails cleanly without any network probe. Dry-run
    # and --skip-healthcheck bypass both paths.
    if not getattr(args, "dry_run", False):
        if getattr(args, "_preflight", False):
            # Destructive subcommands: enforce the gate. --yes means the operator
            # pre-approved, so a critical informs-and-proceeds rather than blocking.
            confirm = allow_all if getattr(args, "yes", False) else interactive_confirm
            _preflight_gate(kvm, confirm, skip=_skip_healthcheck(args))
        else:
            # Read-only intake: audit the device on first connection and surface
            # findings, but never block — a standing CRITICAL (e.g. no out-of-band
            # recovery path) must not make a plain `info`/`snapshot` impossible.
            _inform_on_connect(kvm, skip=_skip_healthcheck(args))
    return kvm


def _rich_client(args, capability: Capability) -> RichDriver:
    """``_client`` for subcommands that use the full HID/Video/Events surface.

    Gating on HID/Video/Events excludes RedfishDriver — the only capability-partial
    driver, and the only one lacking those — leaving the PiKVM-family/Fake surface
    that carries the convenience kwargs (``slow=``, ``quality=``, ``stream=``) the
    minimal capability protocols don't declare. The cast records that narrowing for
    the type checker (see docs/decisions.md).
    """
    return cast("RichDriver", _client(args, capability))


# -- subcommand handlers ---------------------------------------------------

def cmd_info(args) -> int:
    kvm = _client(args, Capability.SYSTEM_INFO)
    print(json.dumps(kvm.get_info(), indent=2, default=str))
    return 0


def cmd_sensors(args) -> int:
    kvm = _client(args, Capability.SENSORS)
    print(json.dumps(cast("Sensors", kvm).read_sensors(), indent=2, default=str))
    return 0


def cmd_logs(args) -> int:
    kvm = _client(args, Capability.LOGS)
    text = cast("Logs", kvm).get_logs(seek=args.seek)
    print(text, end="" if text.endswith("\n") else "\n")
    return 0


def cmd_boot_progress(args) -> int:
    kvm = _client(args, Capability.BOOT_PROGRESS)
    phase = cast("BootProgress", kvm).get_boot_progress()
    # None = the device can't report yet (e.g. powered off with no BootProgress).
    print(phase if phase is not None else "unknown")
    return 0


def cmd_snapshot(args) -> int:
    kvm = _rich_client(args, Capability.VIDEO)
    out = kvm.snapshot_save(args.output)
    print(f"Saved {out}")
    return 0


def cmd_power(args) -> int:
    kvm = _client(args, Capability.POWER)
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
    kvm = _client(args, Capability.POWER)
    kvm.hard_cycle()
    print("hard power cycle: requested")
    return 0


def cmd_type(args) -> int:
    kvm = _rich_client(args, Capability.HID)
    kvm.type_text(args.text, slow=args.slow)
    return 0


def cmd_key(args) -> int:
    kvm = _rich_client(args, Capability.HID)
    kvm.press_key(args.key)
    return 0


def cmd_mount(args) -> int:
    kvm = _client(args, Capability.VIRTUAL_MEDIA)
    name = kvm.mount_iso(args.source, image_name=args.name, cdrom=not args.usb)
    print(f"mounted: {name}")
    return 0


def cmd_eject(args) -> int:
    # The inverse of mount: without it, detaching an ISO required writing Python.
    kvm = _client(args, Capability.VIRTUAL_MEDIA)
    kvm.msd_disconnect()
    print("ejected: virtual media detached")
    return 0


def cmd_classify(args) -> int:
    kvm = _rich_client(args, Capability.VIDEO)
    analyzer = _make_analyzer(kvm, args)
    state = analyzer.classify(hint=args.hint or "")
    print(json.dumps(state.to_dict(), indent=2, default=str))
    return 0


def cmd_watch(args) -> int:
    from .vision.base import ALL_PHASES

    if args.phase not in ALL_PHASES:
        # A typo'd phase can never match — without this it would silently burn
        # the whole timeout in paid model calls before failing.
        print(
            f"error: unknown phase {args.phase!r}. Valid phases: {', '.join(ALL_PHASES)}",
            file=sys.stderr,
        )
        return 1
    kvm = _rich_client(args, Capability.VIDEO)
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


def cmd_healthcheck(args) -> int:
    from .health import Severity, run_healthcheck

    kvm = _build_client(args)
    report = run_healthcheck(kvm)
    if getattr(args, "fix", False):
        _apply_auto_fixes(kvm, report, args)
        report = run_healthcheck(kvm)  # re-audit after fixes
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, default=str))
    else:
        print(f"Healthcheck: {report.driver_kind}@{report.host} "
              f"(firmware={report.firmware or '?'}) — worst: {report.worst}")
        for r in report.results:
            line = f"  [{str(r.severity):<8}] {r.pillar}: {r.title} — {r.detail}"
            print(line)
            if r.severity >= Severity.WARNING and r.remediation:
                print(f"             ↳ {r.remediation}")
    # Exit code reflects the worst finding: CRITICAL=2, WARNING=1, else 0.
    if report.worst is Severity.CRITICAL:
        return 2
    if report.worst is Severity.WARNING:
        return 1
    return 0


def _apply_auto_fixes(kvm, report, args) -> None:
    confirm = allow_all if getattr(args, "yes", False) else interactive_confirm
    for r in report.results:
        if r.auto_fix and r.auto_fix.safe_reversible:
            if confirm("health.fix", f"Apply fix for {r.id}: {r.auto_fix.description}"):
                r.auto_fix.apply(kvm)


def cmd_capabilities(args) -> int:
    kvm = _build_client(args)
    caps = kvm.capabilities()  # structural; makes no network call
    # Print in the capability enum's declaration order for stable output.
    ordered = [c.value for c in Capability if c in caps]
    if args.json:
        print(json.dumps(ordered))
    else:
        print(", ".join(ordered) if ordered else "(none)")
    return 0


def cmd_events(args) -> int:
    kvm = _rich_client(args, Capability.EVENTS)
    try:
        seen = 0
        for evt in kvm.watch_events(stream=not args.no_stream, timeout=args.duration):
            print(json.dumps(
                {"event_type": evt.get("event_type"), "event": evt.get("event", {})},
                default=str,
            ))
            seen += 1
            if args.count and seen >= args.count:
                break
    except ImportError as exc:
        # The 'ws' extra (websocket-client) is not installed.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pass
    return 0


# -- parser ----------------------------------------------------------------

def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--driver", choices=["pikvm", "glkvm", "blikvm", "redfish", "fake"],
                   help="Device driver (overrides KVM_PILOT_DRIVER / config profile; "
                        "default pikvm; 'glkvm' = GL.iNet GLKVM fork, 'redfish' = a DMTF "
                        "Redfish BMC (no HID/Video — capability-partial), 'fake' = no hardware)")
    p.add_argument("--host")
    p.add_argument("--user")
    p.add_argument("--passwd",
                   help="Password (VISIBLE in `ps` and shell history — prefer "
                        "KVM_PILOT_PASSWD, a config profile, --passwd-file, or --ask-passwd)")
    p.add_argument("--passwd-file", dest="passwd_file",
                   help="Read the password from the first line of PATH (avoids argv exposure)")
    p.add_argument("--ask-passwd", dest="ask_passwd", action="store_true",
                   help="Prompt for the password on the terminal (no echo)")
    p.add_argument("--port", type=int)
    p.add_argument("--scheme", choices=["http", "https"])
    p.add_argument("--profile", help="Named host profile from the config file")
    p.add_argument("--totp-secret", dest="totp_secret",
                   help="TOTP/2FA seed (VISIBLE in `ps` — prefer KVM_PILOT_TOTP_SECRET "
                        "or --totp-secret-file)")
    p.add_argument("--totp-secret-file", dest="totp_secret_file",
                   help="Read the TOTP seed from the first line of PATH")
    p.add_argument("--redfish-auth", dest="redfish_auth", choices=["session", "basic"],
                   help="Redfish HTTP auth mode (default session; use 'basic' for a BMC or "
                        "emulator without a SessionService). Ignored by non-redfish drivers.")
    p.add_argument("--verify-ssl", dest="verify_ssl", action="store_true", default=None)
    p.add_argument("--ssl-ca-file", dest="ssl_ca_file",
                   help="Pin TLS verification to a CA bundle or the device's own "
                        "self-signed cert (PEM). Overrides --verify-ssl")
    p.add_argument("--dry-run", dest="dry_run", action="store_true",
                   help="Log destructive actions without sending them")
    p.add_argument("--yes", "-y", action="store_true",
                   help="Skip interactive confirmation on destructive actions")
    p.add_argument("--skip-healthcheck", dest="skip_healthcheck", action="store_true",
                   help="Skip the device preflight healthcheck gate before a "
                        "destructive action (KVM_PILOT_SKIP_HEALTHCHECK=1 also works)")


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
    parser.add_argument(
        "--timeout", dest="http_timeout", type=float, metavar="SECONDS",
        help="HTTP per-request timeout in seconds (global; must precede the subcommand)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("info", help="Print device info as JSON")
    _add_common(p)
    p.set_defaults(func=cmd_info)

    p = sub.add_parser("capabilities", help="List the capabilities this driver supports (offline)")
    p.add_argument("--json", action="store_true", help="Emit a JSON array instead of a comma list")
    _add_common(p)
    p.set_defaults(func=cmd_capabilities)

    p = sub.add_parser("healthcheck",
                       help="Audit device readiness/security/firmware (#80)")
    p.add_argument("--json", action="store_true", help="Emit the report as JSON")
    p.add_argument("--fix", action="store_true",
                   help="Offer to apply safe, reversible auto-fixes (with confirmation)")
    _add_common(p)
    p.set_defaults(func=cmd_healthcheck)

    p = sub.add_parser("snapshot", help="Save a screenshot")
    p.add_argument("output")
    _add_common(p)
    p.set_defaults(func=cmd_snapshot)

    p = sub.add_parser("sensors", help="Read structured sensors (temps/fans/power/voltages) — BMC")
    _add_common(p)
    p.set_defaults(func=cmd_sensors)

    p = sub.add_parser("logs", help="Read the device/host event log")
    p.add_argument("--seek", type=int, default=0,
                   help="Seconds of lookback (0 = everything available)")
    _add_common(p)
    p.set_defaults(func=cmd_logs)

    p = sub.add_parser("boot-progress", help="Structured boot phase (BMC BootProgress)")
    _add_common(p)
    p.set_defaults(func=cmd_boot_progress)

    p = sub.add_parser("power", help="Power action")
    p.add_argument("action", choices=["on", "off", "off-hard", "reset"])
    _add_common(p)
    p.set_defaults(func=cmd_power, _preflight=True)

    p = sub.add_parser("power-cycle", help="Hard power cycle (off-hard -> on)")
    _add_common(p)
    p.set_defaults(func=cmd_power_cycle, _preflight=True)

    p = sub.add_parser("type", help="Type text on the host")
    p.add_argument("text")
    p.add_argument("--slow", action="store_true")
    _add_common(p)
    p.set_defaults(func=cmd_type, _preflight=True)

    p = sub.add_parser("key", help="Press a single key")
    p.add_argument("key")
    _add_common(p)
    p.set_defaults(func=cmd_key, _preflight=True)

    p = sub.add_parser("mount", help="Mount an ISO (local path or URL)")
    p.add_argument("source")
    p.add_argument("--name")
    p.add_argument("--usb", action="store_true")
    _add_common(p)
    p.set_defaults(func=cmd_mount, _preflight=True)

    p = sub.add_parser("eject", help="Detach virtual media (the inverse of mount)")
    _add_common(p)
    p.set_defaults(func=cmd_eject, _preflight=True)

    p = sub.add_parser("classify", help="Classify the current screen once")
    _add_common(p)
    _add_vision(p)
    p.set_defaults(func=cmd_classify)

    p = sub.add_parser("watch", help="Wait until the screen reaches a phase")
    p.add_argument("phase")
    p.add_argument("--timeout", type=float, default=300.0,
                   help="Vision wait-loop deadline in seconds (distinct from the global --timeout)")
    _add_common(p)
    _add_vision(p)
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("events", help="Stream device events (requires the 'ws' extra)")
    p.add_argument("--duration", type=float,
                   help="Stop after N seconds (default: until interrupted)")
    p.add_argument("--count", type=int, help="Stop after N events")
    p.add_argument("--no-stream", dest="no_stream", action="store_true",
                   help="Request a single state snapshot instead of a live stream")
    _add_common(p)
    p.set_defaults(func=cmd_events)

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
    except (ValueError, KeyError) as exc:
        # Config/host resolution errors (missing host, unknown profile) — present
        # cleanly instead of a traceback.
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        # Release device-side resources (notably a RedfishDriver's BMC session)
        # however the command exits — success, handled error, or capability gate.
        kvm = getattr(args, "_driver", None)
        if kvm is not None:
            try:
                kvm.close()
            except Exception:  # noqa: BLE001 - teardown must never mask the result
                logging.getLogger("kvm_pilot.cli").debug("driver close failed", exc_info=True)


if __name__ == "__main__":
    raise SystemExit(main())
