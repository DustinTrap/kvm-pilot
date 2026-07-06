"""Exception hierarchy for kvm-pilot."""

from __future__ import annotations


class KVMPilotError(Exception):
    """Base class for all kvm-pilot errors."""

    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


class AuthError(KVMPilotError):
    """Authentication or authorization failure (HTTP 401/403)."""


class BusyError(KVMPilotError):
    """Device or subsystem is busy (HTTP 409 or result.busy=True)."""


class UnavailableError(KVMPilotError):
    """A subsystem is unavailable (HTTP 503) — e.g. streamer down, ATX board absent."""


class TimeoutError(KVMPilotError):  # noqa: A001 - intentional shadow within package namespace
    """A KVM operation or wait loop exceeded its deadline."""


class ConnectionError(KVMPilotError):  # noqa: A001
    """Could not reach the device at all (DNS, refused, TLS)."""


class SafetyError(KVMPilotError):
    """A destructive operation was blocked by the safety layer."""


class VisionError(KVMPilotError):
    """The vision backend failed or returned an unusable result.

    ``retry_after`` carries a 429's parsed ``Retry-After`` seconds (or ``None``)
    so a wait loop can honor it; ``status_code`` (from the base class) carries
    the HTTP status for retryable-error handling.
    """

    retry_after: float | None = None


class CapabilityError(KVMPilotError):
    """The driver does not support the requested capability."""


class ApiDisabledError(KVMPilotError):
    """The device's REST API appears disabled.

    The hallmark case is GL.iNet (GLKVM) firmware, which ships the PiKVM REST API
    off by default — every ``/api/*`` returns 404 until it is enabled in
    ``/etc/kvmd/nginx-kvmd.conf`` (and it can revert on a firmware upgrade).
    """


class SnapshotFormatError(KVMPilotError):
    """The snapshot endpoint returned bytes that are not a JPEG.

    Observed on GL RM1PE firmware where ``/api/streamer/snapshot`` returns raw
    H.264 with a JPEG content type (#107) — trusting the Content-Type would feed
    garbage to OCR/vision, so callers get this typed error instead.
    """


class MediaOfflineError(KVMPilotError):
    """Virtual media was attached but the device reports it offline.

    Observed on GLKVM: the mount API accepts the request (``enabled/connected:
    true``) while ``online`` stays ``false`` and the host sees no device (#77) —
    usually the GL-side MSD/virtual-media toggle in the device web UI is off.
    """


__all__ = [
    "KVMPilotError",
    "AuthError",
    "BusyError",
    "UnavailableError",
    "TimeoutError",
    "ConnectionError",
    "SafetyError",
    "VisionError",
    "CapabilityError",
    "ApiDisabledError",
    "SnapshotFormatError",
    "MediaOfflineError",
]
