"""
PiKVM-API-compatible fork drivers (currently BliKVM).

``PiKVMDriver`` (in :mod:`kvm_pilot.client`) is the canonical base — the full
PiKVM REST client. This module hosts thin subclasses for forks that are
API-compatible with stock PiKVM and have **no substantial deltas**.

The GL.iNet GLKVM fork does NOT live here: it diverges enough (API disabled by
default, proprietary ``/api/upgrade/*`` flash layer, dual version numbers,
streamer/ATX quirks) that it has its own module — :mod:`kvm_pilot.drivers.glkvm`
(#140). Put GL-specific behavior there.
"""

from __future__ import annotations

from ..client import PiKVMDriver

# Moved to .glkvm in #140; re-exported here for one release so existing
# `from kvm_pilot.drivers.pikvm import ...` importers keep working.
from .glkvm import GLKVM_QUIRKS, GLKVMDriver, Quirk  # noqa: F401


class BliKVMDriver(PiKVMDriver):
    """BliKVM — PiKVM-API-compatible hardware.

    No deltas from the base client are known yet; this subclass exists so any
    BliKVM-specific behavior or quirks have a home.
    """

    _vendor = "blikvm"


__all__ = ["BliKVMDriver"]
