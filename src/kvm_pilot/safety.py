"""
Safety layer for destructive KVM operations.

Two independent guards, both off by default in the *permissive* sense that a
plain library call still works — but the CLI turns confirmation on, and any
caller can opt into dry-run.

  * dry_run:  when True, destructive calls are logged and skipped, never sent
              to the device. Read-only calls always execute. Dry-run is checked
              FIRST: a skipped call never invokes the confirm callback, so
              --dry-run works unattended.
  * confirm:  a callback invoked before each destructive call that would really
              be sent. If it returns False, the call is blocked with
              SafetyError. The default callback allows everything (library
              default); the CLI installs an interactive y/N prompt, and --yes
              installs an allow-all.

A "destructive" operation is any one that can power-cycle, reset, wipe boot
media state, or otherwise change the target's running state in a way that is
not trivially reversible. The set is defined explicitly in DESTRUCTIVE_OPS so
it is auditable rather than guessed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from enum import StrEnum

from .errors import SafetyError

logger = logging.getLogger("kvm_pilot.safety")

# Explicit, auditable list of operation identifiers considered destructive.
DESTRUCTIVE_OPS: set[str] = {
    "atx.power_off",
    "atx.power_off_hard",
    "atx.reset_hard",
    "atx.power_on",  # included: powering a box on is a state change worth gating
    "atx.click",
    # Wake-on-LAN: sending a magic packet powers a sleeping/off host ON — a state
    # change, gated like any power-on (the WoL fallback when there's no ATX, #199).
    "wol.wake",
    "msd.set_params",
    "msd.connect",
    "msd.disconnect",
    "msd.remove_image",
    "msd.reset",
    "msd.write",
    "msd.write_remote",
    "gpio.switch",
    "gpio.pulse",
    "redfish.power_action",
    # Redfish (BMC) driver — per-action granularity, mirroring the atx.*/msd.*
    # style. ``redfish.power_action`` (above) stays for the legacy KVMClient
    # Redfish helper; the RedfishDriver uses the specific ids below.
    "redfish.power_on",
    "redfish.power_off",
    "redfish.power_off_hard",
    "redfish.reset_hard",
    "redfish.virtual_media_insert",
    "redfish.virtual_media_eject",
    # Changing the boot device (Redfish BootSourceOverride, or an in-band
    # efibootmgr BootNext) alters what the host boots on its next reset — a
    # pre-reboot state change worth gating, though not itself a power action.
    "redfish.set_boot_device",
    "ssh.set_boot_next",
    # HID input changes target state too: keystrokes and clicks land on a live
    # console (rm -rf is one type_text away). Mouse *moves* stay ungated.
    "hid.ctrl_alt_delete",
    "hid.type_text",
    "hid.press_key",
    "hid.send_shortcut",
    "hid.key_event",
    "hid.mouse_click",
    # Flashing the KVM/BMC's own firmware — the most destructive op we expose: the
    # device reboots into a new image (dropping this control channel) and a failed
    # flash may need physical recovery. See FirmwareUpdate in drivers/base.py.
    "firmware.flash",
    # A command run over SSH on the managed host's OS can change anything (rm -rf,
    # reboot, service stop). An arbitrary command can't be statically classified,
    # so every exec is gated. Reachability probes stay ungated (read-only).
    "ssh.exec",
    # Rebooting the KVM appliance itself (over appliance-SSH) to clear a wedged
    # encoder: drops all KVM control for ~60s. Target power is untouched, but a
    # reboot that fails to rejoin the network strands the operator (no OOB power
    # to the appliance), so it is always gated. Appliance-SSH read-only
    # diagnostics (loadavg, D-state) stay ungated. See ssh.ApplianceChannel.
    "appliance.reboot",
}

class EffectClass(StrEnum):
    """What *kind* of effect an operation has, classified by effect not transport.

    This is an additive layer over ``DESTRUCTIVE_OPS`` (which stays the single
    source of truth for "does this need a confirm/dry-run guard at all"). The
    MCP server reads the effect class to pick the operator enable-flag and to
    label the result receipt, so an actuator can't launder a power effect by
    choosing a different tool — e.g. Ctrl+Alt+Del is ``power_soft`` even though
    it is delivered over the HID keyboard.
    """

    OBSERVE = "observe"
    HID_INPUT = "hid_input"
    HID_CONTROL = "hid_control"
    MEDIA = "media"
    POWER_SOFT = "power_soft"
    POWER_HARD = "power_hard"
    CONFIG_MUTATION = "config_mutation"
    # Resets the KVM APPLIANCE, not the target — distinct from POWER_* (which
    # touch guest power) so an actuator can't misreport an appliance reboot as a
    # guest power action, nor launder it as a config mutation.
    APPLIANCE_RESET = "appliance_reset"
    # Writes to a system OUTSIDE the managed target (e.g. filing a GitHub issue,
    # #190). Not destructive to the device, but a publication/spam surface, so
    # it gets its own operator gate rather than borrowing a device gate.
    EXTERNAL_WRITE = "external_write"


# Effect class for each guarded op id. Every id in DESTRUCTIVE_OPS must appear
# here (a test enforces it). Ops not in this map are OBSERVE (reads) — mirroring
# guard()'s "not in DESTRUCTIVE_OPS -> ungated" default.
OP_EFFECT: dict[str, EffectClass] = {
    # ATX / power
    "atx.power_on": EffectClass.POWER_SOFT,
    "wol.wake": EffectClass.POWER_SOFT,   # WoL magic packet powers a host on
    "atx.power_off": EffectClass.POWER_SOFT,
    "atx.power_off_hard": EffectClass.POWER_HARD,
    "atx.reset_hard": EffectClass.POWER_HARD,
    "atx.click": EffectClass.POWER_HARD,  # low-level button; may be long-press/reset
    # Virtual media
    "msd.set_params": EffectClass.MEDIA,
    "msd.connect": EffectClass.MEDIA,
    "msd.disconnect": EffectClass.MEDIA,
    "msd.remove_image": EffectClass.MEDIA,
    "msd.reset": EffectClass.MEDIA,
    "msd.write": EffectClass.MEDIA,
    "msd.write_remote": EffectClass.MEDIA,
    # GPIO drives relays / power lines
    "gpio.switch": EffectClass.POWER_HARD,
    "gpio.pulse": EffectClass.POWER_HARD,
    # Redfish (BMC)
    "redfish.power_action": EffectClass.POWER_HARD,  # generic legacy helper; conservative
    "redfish.power_on": EffectClass.POWER_SOFT,
    "redfish.power_off": EffectClass.POWER_SOFT,
    "redfish.power_off_hard": EffectClass.POWER_HARD,
    "redfish.reset_hard": EffectClass.POWER_HARD,
    "redfish.virtual_media_insert": EffectClass.MEDIA,
    "redfish.virtual_media_eject": EffectClass.MEDIA,
    # Boot-device override (Redfish BootSourceOverride / in-band efibootmgr
    # BootNext): changes what the host boots next reset — a config mutation, not
    # a power/media action, so an actuator can't launder it as either.
    "redfish.set_boot_device": EffectClass.CONFIG_MUTATION,
    "ssh.set_boot_next": EffectClass.CONFIG_MUTATION,
    # HID input
    "hid.type_text": EffectClass.HID_INPUT,
    "hid.press_key": EffectClass.HID_INPUT,
    "hid.key_event": EffectClass.HID_INPUT,
    "hid.mouse_click": EffectClass.HID_INPUT,
    # ctrl_alt_delete is a reboot delivered over the keyboard -> power_soft.
    "hid.ctrl_alt_delete": EffectClass.POWER_SOFT,
    # send_shortcut is a generic actuator: this is the default for the op id, but
    # the shortcut tool computes the real class from the chord via shortcut_effect.
    "hid.send_shortcut": EffectClass.HID_CONTROL,
    # Firmware flash reboots the device into a new image — treat as hard power.
    "firmware.flash": EffectClass.POWER_HARD,
    # Arbitrary in-band command: can do anything, keeps its own ALLOW_SSH gate.
    "ssh.exec": EffectClass.HID_CONTROL,
    "appliance.reboot": EffectClass.APPLIANCE_RESET,
    # Files the firmware-registry report as a GitHub issue (#189/#190). Not in
    # DESTRUCTIVE_OPS (it never touches the device); listed for the MCP gate.
    "report.file_firmware": EffectClass.EXTERNAL_WRITE,
}


def effect_of(op: str) -> EffectClass:
    """Effect class for a guarded op id; OBSERVE for anything unmapped (reads)."""
    return OP_EFFECT.get(op, EffectClass.OBSERVE)


_CTRL_KEYS = {"controlleft", "controlright"}
_ALT_KEYS = {"altleft", "altright"}
_SYSRQ_KEYS = {"printscreen", "sysrq"}


def _normalize_chord(keys: str) -> set[str]:
    """Split a comma-separated kvmd shortcut into a casefolded token set."""
    return {tok.strip().casefold() for tok in keys.split(",") if tok.strip()}


def shortcut_effect(keys: str) -> EffectClass:
    """Classify a ``send_shortcut`` chord by *effect*, covering power-chord families.

    ``send_shortcut`` is a generic actuator, so the classifier is the whole gate:
    a reboot chord must not slip through as ordinary ``hid_control``. Recognizes
    Ctrl+Alt+Del and Magic SysRq (Alt+SysRq/PrintScreen + command key). An
    unrecognized chord falls back to ``hid_control`` — the literal chord is still
    surfaced for human/policy sign-off, so it is not a silent bypass.
    """
    toks = _normalize_chord(keys)
    has_ctrl = bool(toks & _CTRL_KEYS)
    has_alt = bool(toks & _ALT_KEYS)
    # Ctrl+Alt+Del — soft reboot (any L/R modifier variant).
    if has_ctrl and has_alt and "delete" in toks:
        return EffectClass.POWER_SOFT
    # Magic SysRq: Alt + SysRq/PrintScreen + a command letter.
    if has_alt and (toks & _SYSRQ_KEYS):
        cmd = toks - _ALT_KEYS - _SYSRQ_KEYS
        if cmd & {"keyb", "keyo"}:  # reboot / poweroff — immediate, unclean
            return EffectClass.POWER_HARD
        if cmd & {"keyr", "keye", "keyi", "keys", "keyu"}:  # SAK/SIGTERM/SIGKILL/sync/remount-ro
            return EffectClass.POWER_SOFT
    return EffectClass.HID_CONTROL


# Callback signature: (op_name, human_description) -> bool
ConfirmCallback = Callable[[str, str], bool]


def allow_all(_op: str, _desc: str) -> bool:
    """Default confirm callback: permit every operation."""
    return True


def deny_all(_op: str, _desc: str) -> bool:
    """Block every destructive operation (useful in tests / safe demos)."""
    return False


def interactive_confirm(op: str, desc: str) -> bool:
    """Prompt the operator on stdin. Returns True only on an explicit 'y'."""
    try:
        answer = input(f"[kvm-pilot] {desc}\n  Proceed? [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


class SafetyPolicy:
    """Holds dry-run state and the confirmation callback for a client."""

    def __init__(
        self,
        dry_run: bool = False,
        confirm: ConfirmCallback | None = None,
    ):
        self.dry_run = dry_run
        self.confirm: ConfirmCallback = confirm or allow_all

    def guard(self, op: str, description: str) -> bool:
        """Evaluate guards for a destructive op.

        Returns True if the underlying call should proceed, False if it was
        intercepted by dry-run (caller should no-op). Raises SafetyError if the
        confirmation callback denied the operation.

        Dry-run wins: a dry-run call is logged and skipped without consulting
        the confirm callback, so ``--dry-run`` never prompts and works in
        non-interactive automation.
        """
        if op not in DESTRUCTIVE_OPS:
            return True  # non-destructive ops are never gated

        if self.dry_run:
            logger.warning("DRY-RUN: skipping destructive op '%s' (%s)", op, description)
            return False

        if not self.confirm(op, description):
            raise SafetyError(f"Operation '{op}' was not confirmed: {description}")

        logger.info("Executing destructive op '%s' (%s)", op, description)
        return True


__all__ = [
    "SafetyPolicy",
    "DESTRUCTIVE_OPS",
    "EffectClass",
    "OP_EFFECT",
    "effect_of",
    "shortcut_effect",
    "allow_all",
    "deny_all",
    "interactive_confirm",
    "ConfirmCallback",
]
