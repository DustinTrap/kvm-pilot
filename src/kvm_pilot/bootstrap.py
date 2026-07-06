"""Guided SSH bootstrap during an OS install (issue #81).

The "expensive HID phase sets up the cheap phase": once an installer is running we
can, over KVM HID + vision, switch to a text console, read the target's DHCP IP,
start ``sshd``, point the profile's SSH channel at the discovered address, and hand
off to the fast, deterministic in-band SSH channel for the rest of the install.

Deliberately conservative — OCR IP-reads, distro-specific ``sshd``, VT-switch
assumptions, and one-way HID (no exit codes) make blind automation risky:

* **Plan by default.** With ``execute=False`` it sends nothing — it returns the
  plan. Only ``execute=True`` touches the device.
* **The IP probe doubles as a console canary.** After the VT-switch it types a
  marker-wrapped ``echo`` and OCRs the result. If the marker never echoes back the
  keystrokes were *not* consumed by a shell (a silently-failed VT-switch, or a
  graphical/Windows installer), so it **aborts before typing any sshd command** —
  a dropped command must never land in the installer's partitioner.
* **Reachability + an auth probe are the only trusted success signals.** A reachable
  port is not a working channel, so success requires a trivial ``ssh_exec`` to
  actually authenticate.

Every step escalates with full context on failure rather than pushing on. This is a
CLI/library helper; it is intentionally **not** an MCP tool in v1 (agents should
orchestrate the same flow with ``snapshot``/``classify``/``type_text`` +
``ssh_reachable(host=…)`` so a human stays in the loop).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

# A marker-wrapped probe so OCR can locate the value unambiguously; the marker
# also proves a shell consumed the keystrokes (the canary). The fallback covers
# minimal environments where `ip` is absent but `hostname -I` works.
_IP_PROBE = (
    'echo "KVMIP=$(ip -4 -o addr show scope global 2>/dev/null | '
    "awk 'NR==1{split($4,a,\"/\");print a[1]}')\""
)
_IP_FALLBACK_PROBE = 'echo "KVMIP=$(hostname -I 2>/dev/null | awk \'{print $1}\')"'
_MARKER = "KVMIP="
_IP_RE = re.compile(r"KVMIP=(\d{1,3}(?:\.\d{1,3}){3})")

# Best-effort, distro-dependent defaults: generate host keys and start sshd. They
# deliberately do NOT set up auth — install a key or password via `commands` (the
# CLI's --command); the auth probe reports if the channel is reachable but unusable.
DEFAULT_BOOTSTRAP_COMMANDS: tuple[str, ...] = (
    "ssh-keygen -A",
    "systemctl start sshd 2>/dev/null || /usr/sbin/sshd",
)

DEFAULT_VT_SHORTCUT = "ControlLeft,AltLeft,F2"


@dataclass
class BootstrapStep:
    name: str
    ok: bool
    detail: str


@dataclass
class BootstrapResult:
    ok: bool
    stage: str
    discovered_host: str | None = None
    reachable: bool = False
    steps: list[BootstrapStep] = field(default_factory=list)
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "stage": self.stage,
            "discovered_host": self.discovered_host,
            "reachable": self.reachable,
            "message": self.message,
            "steps": [{"name": s.name, "ok": s.ok, "detail": s.detail} for s in self.steps],
        }


def _valid_ip(ip: str) -> bool:
    try:
        octets = [int(o) for o in ip.split(".")]
    except ValueError:
        return False
    if len(octets) != 4 or any(o < 0 or o > 255 for o in octets):
        return False
    # Reject loopback / unspecified / link-local — never a usable target address.
    return octets[0] not in (0, 127) and octets[:2] != [169, 254]


def _read_target_ip(kvm: Any, ip_region: Any) -> tuple[str | None, bool]:
    """Type the marker probe(s), OCR the console, return ``(ip, saw_marker)``.

    ``ip`` is the discovered address or None; ``saw_marker`` distinguishes "shell
    reached but IP unreadable" (marker present, no valid IP) from "console not
    reached at all" (marker absent) — the abort-vs-retry decision.
    """
    saw_marker = False
    for probe in (_IP_PROBE, _IP_FALLBACK_PROBE):
        kvm.type_text(probe + "\n", slow=True)
        text = kvm.snapshot_ocr(region=ip_region) or ""
        if _MARKER in text:
            saw_marker = True
        match = _IP_RE.search(text)
        if match and _valid_ip(match.group(1)):
            return match.group(1), True
    return None, saw_marker


def ssh_bootstrap(
    kvm: Any,
    cfg: Any,
    *,
    analyzer: Any,
    execute: bool = False,
    vt_shortcut: str = DEFAULT_VT_SHORTCUT,
    ip_region: Any = None,
    commands: tuple[str, ...] | list[str] = DEFAULT_BOOTSTRAP_COMMANDS,
    require_installer: bool = True,
    reachable_timeout: float = 120.0,
    poll_interval: float = 3.0,
    channel_factory: Callable[[Any], Any] | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> BootstrapResult:
    """Bootstrap SSH on an installer host over KVM HID, then hand off (issue #81).

    With ``execute=False`` (default) returns the plan without touching the device.
    """
    commands = list(commands)
    if not execute:
        planned = [
            BootstrapStep("plan", True, d)
            for d in (
                f"verify an installer is on screen (require_installer={require_installer})",
                f"switch to a text console: send_shortcut({vt_shortcut})",
                "read the target IP via a marker-wrapped echo (aborts if it doesn't echo)",
                *(f"type: {c}" for c in commands),
                "set the channel host to the discovered IP; poll ssh_reachable + ssh_exec 'true'",
            )
        ]
        return BootstrapResult(
            ok=True, stage="plan", steps=planned,
            message="dry-run plan; re-run with execute=True to perform it",
        )

    steps: list[BootstrapStep] = []

    def escalate(stage: str, message: str, **kw: Any) -> BootstrapResult:
        steps.append(BootstrapStep(stage, False, message))
        return BootstrapResult(ok=False, stage=stage, steps=steps, message=message, **kw)

    # 1. Precondition: don't type into an unknown screen.
    if require_installer:
        try:
            state = analyzer.classify()
        except Exception as exc:  # noqa: BLE001 - surface a classify failure as escalation
            return escalate("detect-installer", f"could not classify the screen: {exc}")
        if not state.phase.startswith("installer"):
            return escalate(
                "detect-installer",
                f"screen phase is {state.phase!r}, not an installer — refusing to type",
            )
        steps.append(BootstrapStep("detect-installer", True, f"installer detected ({state.phase})"))

    # 2. Switch to a text console.
    kvm.send_shortcut(vt_shortcut)
    steps.append(BootstrapStep("vt-switch", True, f"sent {vt_shortcut}"))

    # 3. IP probe / console canary — abort before any sshd command if it fails.
    ip, saw_marker = _read_target_ip(kvm, ip_region)
    if ip is None:
        if saw_marker:
            return escalate(
                "read-ip",
                "console reached but the target IP was unreadable — pass --ssh-host manually",
            )
        return escalate(
            "read-ip",
            "the IP marker never echoed back — the console was not reached (the VT-switch may "
            "have failed, or this isn't a Linux text console); aborting before any sshd command",
        )
    steps.append(BootstrapStep("read-ip", True, f"target IP {ip}"))

    # 4. Bootstrap sshd (operator-reviewed, distro-dependent).
    for command in commands:
        kvm.type_text(command + "\n", slow=True)
        steps.append(BootstrapStep("bootstrap-cmd", True, f"typed: {command}"))

    # 5. Point the channel at the discovered IP and confirm the hand-off.
    cfg.ssh_host = ip
    if channel_factory is None:
        from .ssh import SSHChannel

        channel_factory = SSHChannel.from_config
    channel = channel_factory(cfg)

    deadline = time.time() + reachable_timeout
    reachable = False
    while True:
        if channel.ssh_reachable():
            reachable = True
            break
        if time.time() >= deadline:
            break
        sleep(poll_interval)
    if not reachable:
        return escalate(
            "reachability",
            f"{ip} did not accept SSH within {reachable_timeout:.0f}s",
            discovered_host=ip,
        )
    steps.append(BootstrapStep("reachability", True, f"{ip} accepts SSH"))

    # 6. A reachable port is not a working channel — prove auth actually works.
    result = channel.ssh_exec("true")
    if not result.get("ok"):
        return escalate(
            "auth",
            f"{ip} is reachable but SSH auth failed — add a key/password to your bootstrap "
            "commands (e.g. install an authorized_keys entry)",
            discovered_host=ip, reachable=True,
        )
    steps.append(BootstrapStep("auth", True, "SSH authenticated"))
    return BootstrapResult(
        ok=True, stage="done", discovered_host=ip, reachable=True, steps=steps,
        message=f"SSH is ready at {ip}; hand off in-band from here",
    )


__all__ = [
    "ssh_bootstrap",
    "BootstrapResult",
    "BootstrapStep",
    "DEFAULT_BOOTSTRAP_COMMANDS",
    "DEFAULT_VT_SHORTCUT",
]
