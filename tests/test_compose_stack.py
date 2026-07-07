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

# Match the version only where it is actually INSTALLED (a `pip install ...`
# line), not anywhere it is mentioned — a stale doc/comment version must not be
# what the pins are compared on.
_SUSHY_INSTALL = re.compile(r'pip install[^\n]*?"?sushy-tools==([\d.]+)')


def test_compose_pins_same_sushy_version_as_ci():
    compose_pins = _SUSHY_INSTALL.findall(_COMPOSE)
    ci_pins = _SUSHY_INSTALL.findall(_CI)
    assert compose_pins, "compose.yaml must `pip install sushy-tools==X.Y.Z`"
    assert ci_pins, "ci.yml must `pip install sushy-tools==X.Y.Z`"
    # Every install site (both files) must agree on one version.
    assert set(compose_pins) | set(ci_pins) == {compose_pins[0]}, (
        "the compose Redfish leg and the CI pip leg must install the same "
        f"sushy-tools version (compose={compose_pins}, ci={ci_pins})"
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
    # Match every `ports:` short-syntax entry — quoted or not, two-part
    # ("8000:8000", which binds ALL interfaces) or three-part
    # ("127.0.0.1:8000:8000") — so a LAN-exposing mapping can't slip past.
    mappings = re.findall(
        r'^\s*-\s*"?((?:[\w.]+:)?\d+:\d+)"?\s*(?:#.*)?$', _COMPOSE, re.MULTILINE
    )
    assert mappings, "compose.yaml declares no port mappings"
    # Loopback-only means an explicit 127.0.0.1: host-IP prefix; a bare
    # "host:container" (no IP) publishes on 0.0.0.0.
    non_loopback = [m for m in mappings if not m.startswith("127.0.0.1:")]
    assert not non_loopback, f"port mappings must bind 127.0.0.1 only: {non_loopback}"
