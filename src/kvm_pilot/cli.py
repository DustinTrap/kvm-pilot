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
from .firmware_registry import UPSTREAM_REPO
from .safety import allow_all, interactive_confirm

if TYPE_CHECKING:
    # Drivers the CLI can construct. KVMClient (PiKVM family) and FakeDriver expose
    # the full HID/Video/Events surface the subcommands call; RedfishDriver is
    # capability-partial (a BMC — strong on structured state, but no keyboard or
    # screen), so capability-specific subcommands gate on supports() before
    # dispatch instead of AttributeError-ing deep in a handler.
    from .drivers.base import BootProgress, FirmwareUpdate, Logs, Sensors
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


def _resolve_cfg(args):
    """Resolve a HostConfig from CLI args (no driver built). Shared by the driver
    path and the SSH-channel commands, which target the OS, not the KVM."""
    passwd = _resolve_secret(
        getattr(args, "passwd", None), getattr(args, "passwd_file", None),
        getattr(args, "ask_passwd", False), "Password: ",
    )
    totp_secret = _resolve_secret(
        getattr(args, "totp_secret", None), getattr(args, "totp_secret_file", None),
        False, "",
    )
    return resolve_host(
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
        ssh_host=getattr(args, "ssh_host", None),
        ssh_user=getattr(args, "ssh_user", None),
        ssh_port=getattr(args, "ssh_port", None),
        ssh_key=getattr(args, "ssh_key", None),
    )


def _build_client(args) -> AnyDriver:
    confirm = allow_all if getattr(args, "yes", False) else interactive_confirm
    dry_run = getattr(args, "dry_run", False)
    cfg = _resolve_cfg(args)
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


def cmd_ssh_check(args) -> int:
    """Probe whether the managed host's OS is reachable over SSH (read-only)."""
    from .ssh import SSHChannel

    ch = SSHChannel.from_config(_resolve_cfg(args))  # CapabilityError if not configured
    reachable = ch.ssh_reachable()
    print(json.dumps({"target": ch.target, "port": ch.port, "reachable": reachable}, indent=2))
    return 0 if reachable else 1


def cmd_ssh_exec(args) -> int:
    """Run a command on the managed host's OS over SSH (destructive — gated)."""
    from .ssh import SSHChannel

    confirm = allow_all if getattr(args, "yes", False) else interactive_confirm
    ch = SSHChannel.from_config(
        _resolve_cfg(args), confirm=confirm, dry_run=getattr(args, "dry_run", False)
    )
    result = ch.ssh_exec(args.command)
    print(json.dumps(result, indent=2, default=str))
    return 0 if (result["dry_run"] or result["ok"]) else int(result["returncode"] or 1)


def cmd_ssh_discover(args) -> int:
    """Scan a CIDR for hosts with an open SSH port (RISKY — opt-in, your networks only)."""
    from .ssh import discover_ssh_hosts

    print(
        f"WARNING: scanning {args.cidr} for open SSH — only do this on networks you own.",
        file=sys.stderr,
    )
    found = discover_ssh_hosts(args.cidr, port=args.ssh_port)
    print(json.dumps({"cidr": args.cidr, "port": args.ssh_port, "candidates": found}, indent=2))
    return 0 if found else 1


def cmd_ssh_bootstrap(args) -> int:
    """Bootstrap SSH on an installer host over KVM HID, then hand off (issue #81)."""
    from .bootstrap import DEFAULT_BOOTSTRAP_COMMANDS, ssh_bootstrap

    kvm = _rich_client(args, Capability.HID)
    if args.execute and not getattr(args, "yes", False):
        # One top-level confirmation for the whole composite, not per keystroke.
        print(
            "SSH bootstrap will switch the host to a text console and TYPE commands on it "
            "over KVM HID. Only do this against an installer/console you expect.",
            file=sys.stderr,
        )
        if not interactive_confirm("bootstrap.run", "Proceed with SSH bootstrap?"):
            print("aborted", file=sys.stderr)
            return 1
        kvm.safety.confirm = allow_all  # consented to the run; don't re-prompt per keystroke
    cfg = _resolve_cfg(args)
    analyzer = _make_analyzer(kvm, args)
    commands = args.bootstrap_command or list(DEFAULT_BOOTSTRAP_COMMANDS)
    result = ssh_bootstrap(
        kvm, cfg, analyzer=analyzer, execute=args.execute, vt_shortcut=args.vt,
        ip_region=args.ip_region, commands=commands,
        require_installer=not args.no_installer_check,
    )
    print(json.dumps(result.to_dict(), indent=2))
    return 0 if result.ok else 1


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
    # A chord (Ctrl+Alt+F2) routes to send_shortcut; a bare key is pressed (#112).
    keys = args.key.replace("+", ",")
    if "," in keys:
        kvm.send_shortcut(keys)
    else:
        kvm.press_key(args.key)
    return 0


def _mouse_to(kvm, x: float, y: float, space: str) -> None:
    if space == "percent":
        kvm.mouse_move_percent(x, y)
    elif space == "pixel":
        kvm.mouse_move_pixels(int(x), int(y))
    else:  # raw kvmd -32768..32767, (0, 0) at screen center
        kvm.mouse_move(int(x), int(y))


def cmd_mouse_move(args) -> int:
    kvm = _rich_client(args, Capability.HID)
    _mouse_to(kvm, args.x, args.y, args.space)
    print(f"mouse: moved to ({args.x}, {args.y}) [{args.space}]")
    return 0


def cmd_click(args) -> int:
    kvm = _rich_client(args, Capability.HID)
    if args.at:
        _mouse_to(kvm, args.at[0], args.at[1], args.space)
    kvm.mouse_click(args.button, double=args.double)
    where = f" at ({args.at[0]}, {args.at[1]}) [{args.space}]" if args.at else ""
    print(f"mouse: {args.button} {'double-click' if args.double else 'click'}{where}")
    return 0


def cmd_media_list(args) -> int:
    # Check this before telling anyone to download/upload an ISO — the image
    # may already be on the device from an earlier job (#127).
    kvm = _client(args, Capability.VIRTUAL_MEDIA)
    if not hasattr(kvm, "get_msd_state"):
        print("this driver does not expose MSD storage inventory", file=sys.stderr)
        return 1
    print(json.dumps(kvm.get_msd_state(), indent=2, default=str))
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


def cmd_keep_awake(args) -> int:
    # Toggle kvmd's jiggler so the target display doesn't DPMS-sleep out from
    # under a vision/snapshot session — the root of the "snapshot 503s though
    # video works" reports (#126/#142/#159).
    kvm = _rich_client(args, Capability.HID)
    jiggler = kvm.set_jiggler(args.state == "on")
    if jiggler.get("active"):
        interval = jiggler.get("interval")
        print("keep-awake: ON" + (f" (jiggle every {interval}s)" if interval else ""))
    else:
        print("keep-awake: off")
    return 0


def cmd_recover_hid(args) -> int:
    # Re-enumerate the USB HID gadget when it's not reaching the target (#160) —
    # the recoverable half of the #155 write-select fault (a physical cable/port
    # fault won't clear this way).
    kvm = _rich_client(args, Capability.HID)
    if kvm.recover_hid():
        print("recover-hid: HID gadget reattached — keyboard/mouse reach the target")
        return 0
    print("recover-hid: still not reachable — check the USB OTG cable is data-capable "
          "and in a host port on the target")
    return 1


def cmd_appliance(args) -> int:
    # Read-only diagnostics + gated reboot on the KVM APPLIANCE's own OS (#162) —
    # the only path to observe/recover the RV1126 encoder wedge REST can't see.
    kvm = _build_client(args)
    chan = getattr(kvm, "appliance_channel", None)
    if chan is None:
        print("appliance-SSH is not configured for this profile. Set appliance_ssh=true "
              "(or KVM_PILOT_APPLIANCE_SSH=1) and appliance_ssh_key.", file=sys.stderr)
        return 2
    if args.action == "loadavg":
        la = chan.loadavg()
        threads = chan.d_state_video_threads()
        print(f"appliance load (1m): {la if la is not None else 'unreadable'}")
        print(f"D-state video threads: {', '.join(threads) if threads else 'none'}")
        print("(note: these park in D even when healthy — loadavg ≈ their count and is "
              "NOT a health signal on these units)")
        return 0
    res = chan.reboot()  # action == "reboot"; gated as appliance.reboot
    if res.get("dry_run"):
        print("appliance reboot: dry-run — not sent")
    elif res.get("ok"):
        print(f"appliance reboot: issued to {chan.host} — KVM control drops for ~60s; "
              "target power is untouched")
    else:
        print("appliance reboot: failed", file=sys.stderr)
        return 1
    return 0


def cmd_paths(args) -> int:
    # The lockout-exposure view: which independent recovery paths are live (#162).
    from .health import access_paths
    kvm = _build_client(args)
    result = access_paths(kvm)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return 0
    print(f"Access paths for {_driver_label(kvm)}@{getattr(kvm, 'host', '?')}:")
    for p in result["paths"]:
        status = "live" if p["live"] else ("DOWN" if p["configured"] else "not configured")
        print(f"  [{status:15}] {p['path']:15} ({p['kind']})")
    s = result["summary"]
    print(f"\n{s['live_count']} path(s) live across {s['independent_domains']} independent "
          f"failure domain(s); out-of-band power: {'YES' if s['out_of_band_live'] else 'NONE'}")
    if not s["out_of_band_live"]:
        print("  WARNING: no out-of-band power — a fully hung appliance/target cannot be "
              "recovered remotely; every live path shares the appliance's fate.")
    return 0


def cmd_classify(args) -> int:
    kvm = _rich_client(args, Capability.VIDEO)
    analyzer = _make_analyzer(kvm, args)
    state = analyzer.classify(hint=args.hint or "")
    print(json.dumps(state.to_dict(), indent=2, default=str))
    return 0


def cmd_watch(args) -> int:
    from contextlib import nullcontext

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

    # Hold the display awake AND (on GL) keep the on-demand encoder warm for the
    # whole wait, so the loop can't DPMS-sleep (#161) or 503 on a cold streamer
    # (#142) mid-poll; drivers lacking either no-op via nullctx.
    awake = getattr(kvm, "display_awake", nullcontext)
    warm = getattr(kvm, "streamer_warm", nullcontext)
    try:
        with warm(), awake():
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


def _file_firmware_report(submission: dict, args) -> dict:
    """CLI shim over :func:`kvm_pilot.firmware_registry.file_firmware_report` (#189).

    On by default whenever the registry is behind (``--no-file-report`` opts out).
    The shared helper (also behind the MCP ``file_firmware_report`` tool, #190)
    never raises — the outcome dict says what happened and why.
    """
    from .firmware_registry import file_firmware_report

    return file_firmware_report(
        submission, repo=args.repo, source=args.source, date=args.date,
        dry_run=args.dry_run,
    )


def cmd_firmware_check(args) -> int:
    """Detect the device's firmware currency and reconcile it against the registry SSoT.

    Prints the installed version, the latest the device knows about (if it self-reports,
    e.g. GL's /api/upgrade/compare), and — when the bundled/loaded registry is behind or
    missing this device — auto-files the "Latest known release" report upstream (#189;
    ``--no-file-report`` to opt out, ``--dry-run`` to preview).
    """
    from .firmware_registry import check_currency

    kvm = _build_client(args)
    fw, upd, submission = check_currency(kvm)
    vendor = (fw.get("vendor") or "").strip()
    product = fw.get("product") or ""

    if args.json:
        out: dict = {"vendor": vendor, "product": product, "installed": fw.get("version"),
                     "kvmd": fw.get("kvmd_version")}
        if upd:
            out.update(latest_available=upd["latest"], update_available=upd["update_available"],
                       beta=upd.get("beta"))
        out["registry_behind"] = submission is not None
        if submission:
            out["submission"] = submission
            if not args.no_file_report:
                out["report"] = _file_firmware_report(submission, args)
        print(json.dumps(out, indent=2, default=str))
        return 0

    print(f"{vendor} {product}: installed {fw.get('version')} (kvmd {fw.get('kvmd_version')})")
    if upd:
        verdict = "UPDATE AVAILABLE" if upd["update_available"] else "up to date"
        print(f"  vendor's latest: {upd['latest']} — {verdict}"
              + (f" (beta {upd['beta']})" if upd.get("beta") else ""))
    else:
        print("  device does not self-report an available-update check")
    if submission:
        print("\nRegistry SSoT is behind for this device — contributing this to keep it current:")
        print(f"  Latest known release: vendor={vendor} product={product} latest={submission['latest']}")
        if args.no_file_report:
            print("  File it via the 'Firmware report' issue form (the hourly workflow ingests it).")
        else:
            r = _file_firmware_report(submission, args)
            if r.get("filed"):
                print(f"  Auto-filed: {r['url']}" + (f"\n  Note: {r['note']}" if r.get("note") else ""))
            elif r.get("dry_run"):
                print(f"  Would file to {args.repo}: {r['title']}\n")
                print(r["body"])
            else:
                print(f"  Not auto-filed: {r['reason']}")
                print("  File it via the 'Firmware report' issue form (the hourly workflow ingests it).")
    elif upd:
        print("\nRegistry SSoT already reflects this latest — nothing to contribute.")
    return 0


_GL_UBOOT_RECOVERY = (
    "Physical recovery if a flash fails (GL RM1/RM1PE U-Boot failsafe): hold Reset "
    "while powering on (blue LED flashes 5x), set your NIC to static 192.168.1.2/24, "
    "browse to http://192.168.1.1, and upload the firmware. There is NO remote recovery."
)


def _print_firmware_assessment(ru: dict, no_recovery: bool) -> None:
    """Print the reliability assessment that lets the operator make the final call."""
    risk = (ru.get("risk") or "unknown").upper()
    print(f"\nReliability: RISK {risk}. Vendor guidance is to update with physical access.")
    if ru.get("self_flash_blind"):
        print("  - The KVM flashes its own firmware and reboots; this control channel "
              "drops mid-flash (you are blind across the reboot).")
    if ru.get("recovery_required"):
        print("  - A failed flash needs physical access to recover.")
    if no_recovery:
        print("  - THIS UNIT has no out-of-band recovery path wired (healthcheck CRITICAL) "
              "— a bad flash strands it until someone is physically present.")
    if ru.get("notes"):
        print(f"  - {ru['notes']}")
    print(f"  - {_GL_UBOOT_RECOVERY}")
    print("  - Post-flash: a GL update can revert /etc/kvmd/nginx-kvmd.conf and disable the "
          "REST API. If calls 404 afterward, re-enable it and re-run `kvm-pilot healthcheck`.")


def _eject_before_flash(kvm) -> None:
    """Detach virtual media before flashing — gl-inet/glkvm#120: a flash started with
    media mounted risks corrupt writes / interrupted installs. Best-effort."""
    if not kvm.supports(Capability.VIRTUAL_MEDIA):
        return
    try:
        state = kvm.get_msd_state() or {}
        drive = state.get("drive") or {}
        connected = bool(drive.get("connected") or state.get("connected"))
    except Exception:  # noqa: BLE001 - never let the check itself block a decided flash
        print("Could not verify virtual-media state — ensure media is ejected before flashing "
              "(gl-inet/glkvm#120).", file=sys.stderr)
        return
    if connected:
        print("Ejecting virtual media before flashing (gl-inet/glkvm#120).")
        kvm.msd_disconnect()


def cmd_test_report(args) -> int:
    """Probe the device's capabilities and append the evidence to the run ledger (#99).

    Read-only probes always run; destructive ones only via ``--include`` + an
    operator ``--attest`` string, still routed through the normal safety gates.
    Failures are data — the exit code is 0 whenever a row was recorded.
    """
    from . import test_report as tr

    include = frozenset(
        s.strip() for s in (args.include or "").split(",") if s.strip()
    )
    unknown = include - tr.DESTRUCTIVE_CAPS
    if unknown:
        print(
            f"error: --include accepts only destructive capabilities "
            f"({', '.join(sorted(tr.DESTRUCTIVE_CAPS))}); got: {', '.join(sorted(unknown))}",
            file=sys.stderr,
        )
        return 2
    if include and not args.attest:
        print(
            "error: destructive probes need --attest \"<operator statement>\" — it is "
            "recorded on the ledger row (e.g. 'jdoe: lab unit, physical access, ok "
            "to power-cycle')",
            file=sys.stderr,
        )
        return 2
    if "virtual_media" in include and not args.iso:
        print("error: --include virtual_media needs --iso <path-or-url>", file=sys.stderr)
        return 2

    plan = tr.READ_ONLY_CAPS + sorted(include)
    if getattr(args, "dry_run", False):
        print("DRY RUN — nothing probed, nothing recorded. Would probe: "
              + ", ".join(plan))
        return 0

    kvm = _build_client(args)
    if include:
        # Destructive probes go through the same intake gate as every other
        # destructive subcommand (#80); the healthcheck probe itself re-audits.
        confirm = allow_all if getattr(args, "yes", False) else interactive_confirm
        _preflight_gate(kvm, confirm, skip=_skip_healthcheck(args))

    source = "synthetic" if (_driver_label(kvm) == "fake" or args.synthetic) else "real"
    caps, skipped = tr.run_probes(kvm, include=include, iso=args.iso, image=args.image)
    row = tr.build_row(kvm, caps, source=source,
                       operator=args.attest if include else None)
    ledger = Path(args.ledger).expanduser() if args.ledger else tr.default_ledger_path()
    tr.append_row(ledger, row)

    hint = (
        f"recorded run {row['run_id']} -> {ledger}\n"
        "To contribute: append the row to src/kvm_pilot/data/test_runs.jsonl in a "
        "PR, then regenerate the derived maturity (see kvm_pilot.maturity)."
    )
    if args.json:
        print(json.dumps({"row": row, "ledger": str(ledger), "skipped": skipped},
                         indent=2, default=str))
        print(hint, file=sys.stderr)
        return 0
    for cap in caps:
        mark = "PASS" if cap["passed"] else "FAIL"
        print(f"  {mark}  {cap['capability']:16s} {cap['outcome']}")
    for name in skipped:
        print(f"  skip  {name}")
    print(hint)
    return 0


def cmd_firmware_update(args) -> int:
    """Offer / perform a gated remote firmware flash of the KVM itself.

    Read-only by default: prints current->latest, the reliability assessment, and the
    planned `/api/upgrade/*` steps, sending nothing. `--execute` performs the flash —
    routed through the driver's `firmware.flash` safety gate — and refuses when the
    device has no out-of-band recovery path unless `--i-have-physical-access` is given.
    See docs/firmware-update.md.
    """
    from .firmware_registry import load_registry
    from .health import Severity, _match_firmware, run_healthcheck

    kvm = _build_client(args)
    if not kvm.supports(Capability.FIRMWARE_UPDATE):
        raise CapabilityError(
            f"'firmware-update' needs the firmware_update capability, which the "
            f"{_driver_label(kvm)} driver does not provide"
        )
    fwu = cast("FirmwareUpdate", kvm)

    status = fwu.get_upgrade_status()
    if not status.get("enabled"):
        print("Remote firmware update is not available on this device (the upgrade "
              "subsystem is disabled or absent). Flash via the vendor UI.", file=sys.stderr)
        return 1

    info_fn = getattr(kvm, "get_firmware_info", None)
    fw = info_fn() if info_fn is not None else {}
    entry = _match_firmware(
        load_registry().get("firmware", []),
        (fw.get("vendor") or "").strip().lower(), fw.get("product") or "",
    ) or {}
    ru = (entry.get("profile") or {}).get("remote_update") or {}
    current = status.get("current") or fw.get("version")
    latest = entry.get("latest")

    line = f"Firmware: installed {current}"
    if latest:
        line += f", latest known {latest}"
    if status.get("image_size"):
        line += f", staged image {status['image_size'] // (1024 * 1024)} MB"
    print(line)

    # Steer to the known-good path when the device's quirks say the API flash
    # is unreliable (#177) — printed on both the plan and --execute paths, but
    # never blocking: the driver reports a no-op honestly (#94).
    for q in getattr(kvm, "known_quirks", lambda: [])():
        if q.id == "firmware-flash-webui-only":
            print(f"\nNOTE: {q.summary}\n  -> {q.workaround}")
            break

    # Recovery posture drives both the printed assessment and the execute gate.
    report = run_healthcheck(kvm)
    no_recovery = any(
        r.id == "recovery-path" and r.severity is Severity.CRITICAL for r in report.results
    )
    _print_firmware_assessment(ru, no_recovery)

    execute = getattr(args, "execute", False) and not getattr(args, "dry_run", False)
    if not execute:
        result = fwu.apply_firmware_update(image=args.image, dry_run=True)
        print("\nDRY RUN — nothing was sent. Planned steps:")
        for s in result["plan"]:
            print(f"  {s['method']} {s['path']} — {s['note']}")
        print("Re-run with --execute to flash (add --yes to skip the confirmation prompt).")
        return 0

    if no_recovery and not getattr(args, "i_have_physical_access", False):
        print("\nRefusing to flash: this unit has no out-of-band recovery path, so a failed "
              "flash needs a physical trip. Re-run with --i-have-physical-access to override, "
              "or wire an ATX/GPIO reset first.", file=sys.stderr)
        return 1

    _eject_before_flash(kvm)
    result = fwu.apply_firmware_update(image=args.image, dry_run=False)
    if not result.get("sent"):
        if result.get("error"):
            print(f"Flash FAILED: {result['error']}", file=sys.stderr)
        else:
            print("Flash not sent (declined at the safety prompt or dry-run policy).",
                  file=sys.stderr)
        return 1
    print("Firmware flash started. The device will reboot and may be unreachable for several "
          "minutes — do NOT interrupt power.")
    print("When it returns: if the REST API 404s, re-enable it in /etc/kvmd/nginx-kvmd.conf, "
          "then run `kvm-pilot healthcheck`.")
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


def _print_scorecard(card) -> None:
    print(f"benchmark {card.driver}@{card.host}  firmware={card.firmware or 'n/a'}")
    print(f"  {'command':14} {'interface':9} {'capable':7} {'p50_ms':>8}  {'n':>2}  note")
    for r in card.results:
        p50 = f"{r.p50_ms:.1f}" if r.p50_ms is not None else "-"
        cap = "yes" if r.capable else "NO"
        print(f"  {r.command:14} {r.interface:9} {cap:7} {p50:>8}  {r.samples:>2}  {r.note}")


def _firmware_of(kvm) -> str | None:
    """Best-effort firmware version (drivers exposing get_firmware_info); else None."""
    fw = getattr(kvm, "get_firmware_info", None)
    if not callable(fw):
        return None
    try:
        return (fw() or {}).get("version")
    except Exception:  # noqa: BLE001 - firmware is best-effort metadata, never fatal
        return None


def cmd_benchmark(args) -> int:
    """Profile per-command latency + capability for this device (feeds the router #181).

    Builds the driver directly (no preflight — the ~1s intake audit would skew the
    p50) and, unless --no-hid, sends one harmless absolute mouse-move as the HID
    probe. Output is the scorecard the interface router consumes; ``--save``
    persists it to the per-device cache the router reuses.
    """
    from . import benchmark as bench

    cfg = _resolve_cfg(args)
    kvm = make_driver_from_config(cfg, confirm=allow_all, dry_run=False)
    args._driver = kvm  # main() closes it on the way out
    firmware = _firmware_of(kvm)
    card = bench.benchmark_all(
        kvm,
        cfg,
        host=cfg.host,
        driver_kind=_driver_label(kvm),
        firmware=firmware,
        samples=args.samples,
        hid=not args.no_hid,
        os_plane=not args.no_os_plane,
    )
    if args.json:
        print(json.dumps(card.to_dict(), indent=2))
    else:
        _print_scorecard(card)
    if getattr(args, "select", None):
        from .router import select_interface

        pick = select_interface(card, args.select)
        if pick is not None:
            lat = f"{pick.p50_ms:.1f} ms" if pick.p50_ms is not None else "unmeasured"
            print(f"\nrouter → {args.select!r}: {pick.interface} ({lat})")
        else:
            print(f"\nrouter → {args.select!r}: no capable interface (escalate / fall back)")
    if getattr(args, "save", False):
        from .router import save_scorecard

        print(f"scorecard saved: {save_scorecard(card)}", file=sys.stderr)
    return 0


def cmd_route(args) -> int:
    """Show the interface the router picks for a command — the seamless-selection surface.

    Uses the cached per-device scorecard (re-benchmarking KVM-plane rows if the
    firmware changed); benchmarks fresh when there's no cache, the command is
    unknown, or ``--fresh``. The engine picks; you see which interface and why.
    """
    from . import benchmark as bench
    from .router import load_for, plane_of, save_scorecard, select_interface

    cfg = _resolve_cfg(args)
    kvm = make_driver_from_config(cfg, confirm=allow_all, dry_run=False)
    args._driver = kvm
    firmware = _firmware_of(kvm)
    card = None if args.fresh else load_for(cfg.host, firmware=firmware)
    fresh = card is None or not any(r.command == args.command for r in card.results)
    if fresh:
        card = bench.benchmark_all(
            kvm, cfg, host=cfg.host, driver_kind=_driver_label(kvm), firmware=firmware,
            samples=args.samples, hid=not args.no_hid, os_plane=not args.no_os_plane,
        )
        save_scorecard(card)
    assert card is not None  # fresh is True whenever the loaded card is None, so it was rebuilt
    pick = select_interface(card, args.command)
    if pick is None:
        print(f"route {args.command!r}: no capable interface (escalate / fall back)", file=sys.stderr)
        return 1
    source = "benchmarked" if fresh else "cached"
    if args.json:
        print(json.dumps({
            "command": args.command, "interface": pick.interface,
            "plane": plane_of(pick.interface).value, "p50_ms": pick.p50_ms, "source": source,
        }))
    else:
        lat = f"{pick.p50_ms:.1f} ms" if pick.p50_ms is not None else "unmeasured"
        print(f"route {args.command!r} → {pick.interface} "
              f"({plane_of(pick.interface).value} plane, {lat}, {source})")
    return 0


def cmd_host_exec(args) -> int:
    """Run a command on the managed host's OS via the fastest capable in-band
    interface (ssh or winrm), auto-selected by the router and self-tuned from the
    outcome (online learning). Needs ``ssh_host`` configured for the profile.
    """
    import time

    from . import benchmark as bench
    from .router import Plane, load_for, save_scorecard, select_interface

    cfg = _resolve_cfg(args)
    kvm = make_driver_from_config(cfg, confirm=allow_all, dry_run=False)
    args._driver = kvm
    command_name = "ps_exec" if args.powershell else "exec"

    def _pick(card):
        return None if card is None else select_interface(card, command_name, allow_planes={Plane.OS})

    firmware = _firmware_of(kvm)
    card = load_for(cfg.host, firmware=firmware)
    if card is None or _pick(card) is None:
        card = bench.benchmark_all(
            kvm, cfg, host=cfg.host, driver_kind=_driver_label(kvm),
            firmware=firmware, samples=args.samples, os_plane=True,
        )
        save_scorecard(card)
    pick = _pick(card)
    if pick is None:
        print("host-exec: no capable in-band interface (ssh/winrm) — set ssh_host and "
              "ensure the target is reachable and credentialed", file=sys.stderr)
        return 1

    start = time.perf_counter()
    try:
        if pick.interface == "winrm":
            from .remote_ps import RemotePowerShell

            rp = RemotePowerShell.from_config(cfg, confirm=allow_all, shell=args.shell)  # nosec B604 - 'shell' is a PowerShell interpreter name, not subprocess shell=True
            result = rp.run_ps(args.cmd)
            rp.ssh.close()
        else:
            from .ssh import SSHChannel

            channel = SSHChannel.from_config(cfg, confirm=allow_all)
            channel.persist = True
            result = channel.ssh_exec(args.cmd)
            channel.close()
    except Exception as exc:  # noqa: BLE001 - a failed exec updates the scorecard, not a crash
        card.record(command_name, pick.interface, None, ok=False)
        save_scorecard(card)
        print(f"host-exec via {pick.interface} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    elapsed_ms = (time.perf_counter() - start) * 1000
    rc = result.get("returncode") if isinstance(result, dict) else None
    ok = rc in (0, None)
    card.record(command_name, pick.interface, elapsed_ms, ok)  # online learning
    save_scorecard(card)
    if isinstance(result, dict):
        sys.stdout.write(result.get("stdout", ""))
        if result.get("stderr"):
            sys.stderr.write(result["stderr"])
    return 0 if ok else (rc or 1)


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


def _add_ssh_target(p: argparse.ArgumentParser) -> None:
    """Target selection for the ssh-* commands (the managed host's OS, not the KVM).

    Lets a caller pick a profile and/or override the SSH address at runtime — e.g.
    an install-time DHCP IP the profile can't know until the target boots. These
    beat the profile/``KVM_PILOT_SSH_*`` env via the usual resolve_host precedence.
    """
    p.add_argument("--profile", help="Named host profile from the config file")
    p.add_argument("--ssh-host", dest="ssh_host",
                   help="Managed host's SSH address (overrides the profile/env ssh_host)")
    p.add_argument("--ssh-user", dest="ssh_user", help="SSH username for the managed host")
    p.add_argument("--ssh-port", dest="ssh_port", type=int, help="SSH port (default 22)")
    p.add_argument("--ssh-key", dest="ssh_key", help="Path to an SSH private key")


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

    p = sub.add_parser(
        "benchmark",
        help="Profile per-command latency + capability for the interface router (#181)",
    )
    p.add_argument("--samples", type=int, default=6,
                   help="Samples per command (the first, cold, sample is dropped from the p50)")
    p.add_argument("--no-hid", dest="no_hid", action="store_true",
                   help="Skip the harmless HID mouse-move probe (it moves the target cursor)")
    p.add_argument("--no-os-plane", dest="no_os_plane", action="store_true",
                   help="Skip the in-band SSH / WinRM (remote-PowerShell) probes; KVM plane only")
    p.add_argument("--select", metavar="COMMAND",
                   help="After benchmarking, print the interface the router would pick for COMMAND")
    p.add_argument("--save", action="store_true",
                   help="Persist the scorecard to the per-device cache the router reuses")
    p.add_argument("--json", action="store_true", help="Emit the scorecard as JSON")
    _add_common(p)
    p.set_defaults(func=cmd_benchmark)

    p = sub.add_parser(
        "route",
        help="Show the interface the router picks for a command (uses/refreshes the cached scorecard) (#181)",
    )
    p.add_argument("command", help="Command to route (e.g. snapshot, exec, get_info)")
    p.add_argument("--fresh", action="store_true", help="Re-benchmark instead of using the cache")
    p.add_argument("--samples", type=int, default=6)
    p.add_argument("--no-hid", dest="no_hid", action="store_true")
    p.add_argument("--no-os-plane", dest="no_os_plane", action="store_true")
    p.add_argument("--json", action="store_true")
    _add_common(p)
    p.set_defaults(func=cmd_route)

    p = sub.add_parser(
        "host-exec",
        help="Run a command on the managed host's OS via the fastest capable in-band "
             "interface (ssh/winrm), auto-selected by the router (#181)",
    )
    p.add_argument("cmd", help="Command to run on the managed host's OS")
    p.add_argument("--powershell", action="store_true",
                   help="Route via WinRM / remote-PowerShell instead of ssh")
    p.add_argument("--shell", default="powershell",
                   help="Remote PowerShell interpreter for --powershell (powershell | pwsh)")
    p.add_argument("--samples", type=int, default=6)
    _add_common(p)
    p.set_defaults(func=cmd_host_exec)

    p = sub.add_parser("healthcheck",
                       help="Audit device readiness/security/firmware (#80)")
    p.add_argument("--json", action="store_true", help="Emit the report as JSON")
    p.add_argument("--fix", action="store_true",
                   help="Offer to apply safe, reversible auto-fixes (with confirmation)")
    _add_common(p)
    p.set_defaults(func=cmd_healthcheck)

    p = sub.add_parser("firmware-check",
                       help="Detect firmware currency and reconcile it against the registry "
                            "(auto-files the report when the registry is behind, #189)")
    p.add_argument("--json", action="store_true", help="Emit the result as JSON")
    p.add_argument("--no-file-report", dest="no_file_report", action="store_true",
                   help="Only report; don't auto-file the latest-known report when the "
                        "registry is behind")
    p.add_argument("--source", help="Release-channel URL for the auto-filed report "
                                    "(default: the registry entry's existing source)")
    p.add_argument("--date", help="Release date (YYYY-MM-DD) for the auto-filed report "
                                  "(default: today, i.e. 'observed on')")
    p.add_argument("--repo", default=UPSTREAM_REPO,
                   help=f"GitHub repo the report is filed to (default {UPSTREAM_REPO})")
    _add_common(p)
    p.set_defaults(func=cmd_firmware_check)

    p = sub.add_parser("test-report",
                       help="Probe the device's capabilities and append the evidence "
                            "row to the run ledger (#99; read-only probes always run, "
                            "destructive only via --include + --attest)")
    p.add_argument("--include",
                   help="Comma-separated DESTRUCTIVE capabilities to also exercise "
                        "(virtual_media,power,firmware_update); requires --attest and "
                        "routes through the normal safety gates")
    p.add_argument("--attest",
                   help="Operator attestation recorded on the ledger row (e.g. "
                        "'jdoe: lab unit, physical access, ok to power-cycle')")
    p.add_argument("--iso", help="Image path/URL for the virtual_media probe")
    p.add_argument("--image",
                   help="Local firmware image for the firmware_update probe (omit to "
                        "flash the device's staged image)")
    p.add_argument("--ledger",
                   help="Ledger JSONL to append to (default $KVM_PILOT_TEST_LEDGER, "
                        "else ~/.config/kvm-pilot/test_runs.jsonl)")
    p.add_argument("--synthetic", action="store_true",
                   help="Force source=synthetic (emulator/bench target — synthetic "
                        "runs never promote maturity); the fake driver is always "
                        "synthetic. There is no flag to force source=real")
    p.add_argument("--json", action="store_true", help="Emit the row as JSON")
    _add_common(p)
    p.set_defaults(func=cmd_test_report)

    p = sub.add_parser("firmware-update",
                       help="Assess (and optionally perform) a gated remote firmware flash")
    p.add_argument("--image", help="Local firmware image to upload before flashing "
                                   "(omit to flash the image the device has staged)")
    p.add_argument("--execute", action="store_true",
                   help="Actually flash (default is a dry-run plan that sends nothing)")
    p.add_argument("--i-have-physical-access", dest="i_have_physical_access",
                   action="store_true",
                   help="Override the no-out-of-band-recovery refusal — acknowledges a "
                        "failed flash needs physical access to this device")
    _add_common(p)
    p.set_defaults(func=cmd_firmware_update)

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

    p = sub.add_parser("ssh-check",
                       help="Is the managed host's OS reachable over SSH? (read-only)")
    _add_ssh_target(p)
    p.set_defaults(func=cmd_ssh_check)

    p = sub.add_parser("ssh-exec",
                       help="Run a command on the managed host's OS over SSH (in-band; gated)")
    p.add_argument("command", help="The command to run on the target host")
    _add_ssh_target(p)
    p.add_argument("--dry-run", dest="dry_run", action="store_true",
                   help="Log the command without sending it")
    p.add_argument("--yes", "-y", action="store_true",
                   help="Skip interactive confirmation")
    p.set_defaults(func=cmd_ssh_exec)

    p = sub.add_parser("ssh-discover",
                       help="Scan a CIDR for open SSH (RISKY, opt-in — your networks only)")
    p.add_argument("cidr", help="CIDR to scan, e.g. 10.0.1.0/24")
    p.add_argument("--ssh-port", type=int, default=22, help="SSH port to probe (default 22)")
    p.set_defaults(func=cmd_ssh_discover)

    p = sub.add_parser(
        "ssh-bootstrap",
        help="Bootstrap SSH on an installer host over KVM HID, then hand off (#81)")
    p.add_argument("--execute", action="store_true",
                   help="Actually perform it (default: print the plan and send nothing)")
    p.add_argument("--vt", default="ControlLeft,AltLeft,F2",
                   help="VT-switch shortcut to reach a text console (default Ctrl+Alt+F2)")
    p.add_argument("--ip-region", dest="ip_region", nargs=4, type=int,
                   metavar=("L", "T", "R", "B"),
                   help="OCR bounding box for reading the IP (optional; default full screen)")
    p.add_argument("--command", action="append", dest="bootstrap_command",
                   help="A bootstrap command to type (repeatable; overrides the defaults). "
                        "Add one that installs a key or sets a password for a usable channel.")
    p.add_argument("--no-installer-check", dest="no_installer_check", action="store_true",
                   help="Skip the 'is an installer on screen?' precondition (risky)")
    _add_common(p)
    _add_vision(p)
    p.set_defaults(func=cmd_ssh_bootstrap, _preflight=True)

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

    p = sub.add_parser("key", help="Press a key, or send a chord of kvmd key codes")
    p.add_argument("key",
                   help="A kvmd key code (e.g. Enter, F2) or a +/,-separated chord "
                        "(e.g. ControlLeft+AltLeft+F2)")
    _add_common(p)
    p.set_defaults(func=cmd_key, _preflight=True)

    p = sub.add_parser("mouse-move",
                       help="Move the mouse (absolute) — percent of screen by default")
    p.add_argument("x", type=float)
    p.add_argument("y", type=float)
    p.add_argument("--space", choices=["percent", "pixel", "raw"], default="percent",
                   help="Coordinate space: percent 0.0-1.0 (default, resolution-proof), "
                        "screen pixels, or raw kvmd -32768..32767")
    _add_common(p)
    p.set_defaults(func=cmd_mouse_move, _preflight=True)

    p = sub.add_parser("click", help="Click the mouse (move first with --at X Y)")
    p.add_argument("button", nargs="?", default="left",
                   choices=["left", "right", "middle"])
    p.add_argument("--at", nargs=2, type=float, metavar=("X", "Y"),
                   help="Move here first (in --space coordinates)")
    p.add_argument("--space", choices=["percent", "pixel", "raw"], default="percent",
                   help="Coordinate space for --at (default percent 0.0-1.0)")
    p.add_argument("--double", action="store_true", help="Double-click")
    _add_common(p)
    p.set_defaults(func=cmd_click, _preflight=True)

    p = sub.add_parser("media-list",
                       help="List images already on the KVM's virtual-media storage (read-only)")
    _add_common(p)
    p.set_defaults(func=cmd_media_list)

    p = sub.add_parser("mount", help="Mount an ISO (local path or URL)")
    p.add_argument("source")
    p.add_argument("--name")
    p.add_argument("--usb", action="store_true")
    _add_common(p)
    p.set_defaults(func=cmd_mount, _preflight=True)

    p = sub.add_parser("eject", help="Detach virtual media (the inverse of mount)")
    _add_common(p)
    p.set_defaults(func=cmd_eject, _preflight=True)

    p = sub.add_parser(
        "keep-awake",
        help="Toggle the mouse jiggler so the target display doesn't sleep (#159)")
    p.add_argument("state", choices=["on", "off"], help="Turn keep-awake on or off")
    _add_common(p)
    p.set_defaults(func=cmd_keep_awake)

    p = sub.add_parser(
        "recover-hid",
        help="Reset/re-enumerate the USB HID gadget when it isn't reaching the target (#160)")
    _add_common(p)
    p.set_defaults(func=cmd_recover_hid)

    p = sub.add_parser(
        "appliance",
        help="Diagnose/recover the KVM appliance's OWN OS over SSH — encoder wedge (#162)")
    p.add_argument("action", choices=["loadavg", "reboot"],
                   help="loadavg (read-only diagnostics) or reboot (gated recovery)")
    _add_common(p)
    p.set_defaults(func=cmd_appliance)

    p = sub.add_parser(
        "paths",
        help="Show which independent recovery paths are live (lockout exposure) (#162)")
    p.add_argument("--json", action="store_true", help="Emit the raw path map as JSON")
    _add_common(p)
    p.set_defaults(func=cmd_paths)

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
