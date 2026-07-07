"""The dev emulator stack (compose.yaml + Makefile) stays wired to the suite (#21).

Pure-stdlib text pins (precedent: ``tests/test_workflows_pinned.py``): the compose
Redfish leg, the Makefile glue, and the CI ``redfish-integration`` job must keep
pointing at the same emulator version and the same localhost port, or the
one-command dev stack silently drifts from what CI actually validates.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_COMPOSE = (_ROOT / "compose.yaml").read_text()
_MAKEFILE = (_ROOT / "Makefile").read_text()
_CI = (_ROOT / ".github" / "workflows" / "ci.yml").read_text()

_SUSHY_PIN = re.compile(r"sushy-tools==([\d.]+)")


def test_compose_pins_same_sushy_version_as_ci():
    compose_pin = _SUSHY_PIN.search(_COMPOSE)
    ci_pin = _SUSHY_PIN.search(_CI)
    assert compose_pin, "compose.yaml must pin sushy-tools==X.Y.Z"
    assert ci_pin, "ci.yml must pin sushy-tools==X.Y.Z"
    assert compose_pin.group(1) == ci_pin.group(1), (
        "the compose Redfish leg and the CI pip leg must install the same "
        f"sushy-tools version (compose={compose_pin.group(1)}, ci={ci_pin.group(1)})"
    )


def test_makefile_integration_target_points_at_compose_port():
    assert "REDFISH_URL ?= http://127.0.0.1:8000" in _MAKEFILE
    assert "KVM_PILOT_REDFISH_URL=$(REDFISH_URL)" in _MAKEFILE, (
        "make integration must export KVM_PILOT_REDFISH_URL so the "
        "tests/integration conftest targets the running stack"
    )
    assert '"127.0.0.1:8000:8000"' in _COMPOSE, (
        "compose.yaml must publish the Redfish emulator on the port "
        "`make integration` points the tests at"
    )


def test_compose_publishes_loopback_only():
    # An emulator that answers to fake credentials must never listen on the LAN.
    mappings = re.findall(r'-\s*"([^"]+:\d+:\d+)"', _COMPOSE)
    assert mappings, "compose.yaml declares no port mappings"
    non_loopback = [m for m in mappings if not m.startswith("127.0.0.1:")]
    assert not non_loopback, f"port mappings must bind 127.0.0.1 only: {non_loopback}"
