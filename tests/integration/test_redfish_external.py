"""End-to-end ``--driver redfish`` against an external reference emulator.

Marked ``integration`` and skipped unless an emulator is available (see
``conftest.py``). The point of running against sushy-tools rather than the
in-process ``redfish_emulator`` is *independence*: an externally-authored,
DMTF-conformant implementation can expose assumptions our driver and our own mock
happen to share (``@odata`` shapes, UUID member ids, action target URLs, the
async ``TaskService``/``202`` flow).

sushy-tools' ``--fake`` driver has no SessionService, so we authenticate with
HTTP Basic (``--redfish-auth basic``) — the same path a BMC with session auth
disabled would need. It applies power transitions with a short simulated delay,
which the driver's wait loop absorbs; that delay is exactly why state assertions
go through ``power_on()``/``power_off_hard()`` (which block on the real GET) and
not a fire-and-forget call.
"""

from __future__ import annotations

import json
import urllib.parse

import pytest

from kvm_pilot.cli import main
from kvm_pilot.drivers.redfish import RedfishDriver
from kvm_pilot.safety import allow_all

pytestmark = pytest.mark.integration


def _parts(url: str) -> tuple[str, int, str]:
    u = urllib.parse.urlparse(url)
    return u.hostname or "127.0.0.1", u.port or (443 if u.scheme == "https" else 80), u.scheme


def _cli(url: str, *rest: str) -> list[str]:
    host, port, scheme = _parts(url)
    return [*rest, "--driver", "redfish", "--redfish-auth", "basic",
            "--host", host, "--port", str(port), "--scheme", scheme]


def _driver(url: str) -> RedfishDriver:
    host, port, scheme = _parts(url)
    return RedfishDriver(host, "admin", "password", port=port, scheme=scheme,
                         auth="basic", confirm=allow_all)


def test_cli_info_talks_to_external_reference(redfish_emulator_url, capsys):
    rc = main(_cli(redfish_emulator_url, "info"))
    assert rc == 0
    info = json.loads(capsys.readouterr().out)
    # An independent Redfish service answered through the whole CLI -> driver ->
    # HTTP -> hypermedia-discovery chain.
    assert info["manufacturer"]
    assert info["redfish_version"]


def test_cli_capabilities_match_the_bmc_set(redfish_emulator_url, capsys):
    rc = main(_cli(redfish_emulator_url, "capabilities"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "power" in out and "system_info" in out
    assert "hid" not in out and "video" not in out  # a BMC has neither


def test_cli_capability_gate_fires_against_external_reference(redfish_emulator_url, capsys):
    # #27's core guarantee, proven against an independent BMC: a HID command on a
    # capability-partial driver exits 1 cleanly (the gate runs before any network).
    rc = main(_cli(redfish_emulator_url, "type", "hello"))
    assert rc == 1
    err = capsys.readouterr().err
    assert "hid" in err and "redfish" in err


def test_power_transitions_are_reflected_on_get(redfish_emulator_url):
    # The independent corroboration our own mock can't give: a real reset POST
    # drives PowerState, observed back via a fresh GET.
    driver = _driver(redfish_emulator_url)
    driver.power_on()
    assert driver.is_powered_on() is True
    driver.power_off_hard()
    assert driver.is_powered_on() is False
