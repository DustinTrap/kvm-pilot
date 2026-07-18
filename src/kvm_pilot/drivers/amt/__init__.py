"""Intel AMT / vPro driver package.

The out-of-band channel that fills the gap HDMI-capture KVMs cannot: AMT lives
in the Management Engine, below the OS, so it sees and drives BIOS / POST / the
bootloader. Split by protocol — :mod:`.wsman` (WS-Man: power / boot / info), SOL
via the ``amtterm`` client, and :mod:`.rfb` (KVM-redirection snapshot + HID) —
assembled in :class:`.driver.AmtDriver`. See ``docs/amt.md`` (#211).
"""

from __future__ import annotations

from .driver import AmtDriver

__all__ = ["AmtDriver"]
