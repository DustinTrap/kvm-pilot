"""The PiKVM driver family split: subclass relations and base-vs-fork
behavior. GL-specific tests live in test_glkvm.py (#140)."""

from __future__ import annotations

import pytest

from emulator import EmulatorServer
from kvm_pilot import ApiDisabledError, BliKVMDriver, GLKVMDriver, KVMClient, PiKVMDriver
from kvm_pilot.errors import KVMPilotError


@pytest.fixture
def emu():
    with EmulatorServer() as server:
        yield server


def test_family_are_pikvmdriver_subclasses():
    assert issubclass(GLKVMDriver, PiKVMDriver)
    assert issubclass(BliKVMDriver, PiKVMDriver)
    # KVMClient / PiKVMClient remain aliases of the canonical base.
    assert KVMClient is PiKVMDriver


def test_stock_pikvm_404_is_a_plain_error_not_apidisabled(emu):
    # The base driver has no GL hint, so a bare 404 from get_info stays generic
    # (only the explicit check_api_enabled() preflight upgrades it).
    emu.state.api_disabled = True
    base = PiKVMDriver("127.0.0.1", "admin", "s3cr3t", port=emu.port, scheme="http")
    with pytest.raises(KVMPilotError) as ei:
        base.get_info()
    assert not isinstance(ei.value, ApiDisabledError)
    assert ei.value.status_code == 404
