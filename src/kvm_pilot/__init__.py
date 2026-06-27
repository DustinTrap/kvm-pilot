"""
kvm-pilot — AI-driven bare-metal control for PiKVM and the GL.iNet GLKVM fork
(GL-RM1 / GL-RM1PE).

A stdlib-only REST client for the PiKVM API, a safety layer that gates
destructive power/media operations, and a pluggable vision subsystem that
classifies boot/run phases from KVM screenshots using either Claude or a local
OpenAI-compatible VLM.

Quickstart:
    from kvm_pilot import KVMClient
    from kvm_pilot.vision import ScreenAnalyzer, make_backend

    kvm = KVMClient("192.168.8.1", "admin", "secret")
    analyzer = ScreenAnalyzer(kvm, make_backend("anthropic"))
    state = analyzer.wait_for_state("grub_menu", timeout=120)
"""

from __future__ import annotations

from .__about__ import __version__
from .client import KVMClient, PiKVMClient
from .config import HostConfig, resolve_host
from .drivers import make_driver
from .drivers.base import Capability, KVMDriver
from .errors import (
    AuthError,
    BusyError,
    CapabilityError,
    ConnectionError,
    KVMPilotError,
    SafetyError,
    TimeoutError,
    UnavailableError,
    VisionError,
)
from .safety import SafetyPolicy, deny_all, interactive_confirm

__all__ = [
    "__version__",
    "KVMClient",
    "PiKVMClient",
    "HostConfig",
    "resolve_host",
    "SafetyPolicy",
    "deny_all",
    "interactive_confirm",
    "KVMPilotError",
    "AuthError",
    "BusyError",
    "UnavailableError",
    "TimeoutError",
    "ConnectionError",
    "SafetyError",
    "VisionError",
    "CapabilityError",
    "Capability",
    "KVMDriver",
    "make_driver",
]
