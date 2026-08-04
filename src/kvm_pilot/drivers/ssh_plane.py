"""OS-plane target: a machine reachable only over SSH, with no device beneath it (#248).

Every other driver here speaks to a *device* — a KVM appliance or a BMC — that
sits beside the managed machine and works whether or not its OS is up. This one
speaks to the machine itself. It exists because the project's own recovery
ladder already ranks in-band SSH **second**, ahead of KVM-side recovery::

    Wake-on-LAN -> in-band SSH -> KVM-side recovery -> Intel AMT -> physical

and the router already models an ``OS`` plane — but until now you could not
stand on that rung without owning a KVM, which made kvm-pilot unusable on the
half of a mixed fleet that has no BMC.

**Deliberately almost empty.** It implements no device capability protocols: no
power, no video, no HID, no media. It carries the ``SSHChannel`` and reports
honestly about what is absent. The value is not in what it can do — ``ssh-exec``
could already do that — it is in letting ``healthcheck`` and the router *see* an
OS-plane target at all, and say the true and useful thing about it: **there is no
out-of-band recovery here.**

**Never auto-detected.** ``--driver auto`` refuses to guess on a host that only
answers SSH (#235), and that stays true: an SSH banner identifies a reachable
OS, not a device to manage. This driver is only ever selected explicitly, by an
operator who already knows what the target is.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..errors import CapabilityError
from .base import Capability, CapabilityMixin, VideoScope

if TYPE_CHECKING:
    from ..config import HostConfig


class SshDriver(CapabilityMixin):
    """A managed host reachable over SSH, with no out-of-band device."""

    # No capture and no firmware framebuffer — there is no video path at all.
    VIDEO_SCOPE = VideoScope.NONE

    def __init__(self, host: str, *, ssh_channel: Any = None):
        self.host = host
        # Attached by from_config / make_driver_from_config, exactly as it is for
        # the device drivers — SSH stays a per-profile channel, not a capability.
        self.ssh_channel = ssh_channel

    @classmethod
    def from_config(
        cls, cfg: HostConfig, *, confirm=None, dry_run: bool = False
    ) -> SshDriver:
        """Build from a resolved config. Requires ``ssh_host``.

        The device drivers can fall back to ``host``; this one cannot, because
        ``host`` means "the appliance" everywhere else and there is no appliance
        here. Demanding ``ssh_host`` keeps that distinction unambiguous.
        """
        if not cfg.ssh_host:
            raise CapabilityError(
                "the 'ssh' driver targets the managed host's own OS, so it needs "
                "ssh_host (or KVM_PILOT_SSH_HOST / --ssh-host) — the address of the "
                "machine itself. Use a device driver if you meant a KVM or BMC."
            )
        return cls(cfg.ssh_host)

    def capabilities(self) -> set[Capability]:
        """None. There is no device here to have capabilities.

        Stated explicitly rather than inherited, so nobody reads an empty set as
        "detection failed": SSH-to-target is a channel by design (see
        ``base.RemoteShell``), never a driver capability.
        """
        return set()

    def get_firmware_info(self) -> dict[str, Any]:
        """No device firmware exists on this plane.

        Returned rather than raised so the healthcheck's firmware checks skip
        cleanly, and so ``firmware-check`` can tell the operator this is a
        *category* difference rather than a failure to detect anything (#248).
        An OS version is not device firmware and must never reach the firmware
        registry — the run ledger joins on (vendor, product, firmware_version)
        of a device, and feeding it an OS would corrupt that join.
        """
        return {"vendor": None, "product": None, "version": None, "os_plane": True}

    def close(self) -> None:
        channel = getattr(self, "ssh_channel", None)
        if channel is not None:
            channel.close()
