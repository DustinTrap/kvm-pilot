"""IpmiDriver against an independent IPMI simulator (OpenIPMI ``ipmi_sim``) — #62/#28.

Marked ``integration`` and skipped unless an ``ipmi_sim`` BMC is available (see
conftest ``ipmi_bmc``). The point, as with the sushy-tools Redfish tests, is
*independence*: driving a reference implementation we didn't write (a different
vendor identity — MontaVista, not the Dell shapes our fake-ipmitool fixtures were
captured from) so a spec assumption shared by our driver and our own mocks can't
hide a bug. Runs on Linux (CI / the homelab OpenShift VM); macOS has no ipmi_sim.

Reference-sim limits, characterised live against Fedora's stock
``/etc/ipmi/ipmisim1.emu`` (documented so assertions stay honest, exactly like the
sushy ``--fake`` "Continuous" note in ``test_redfish_external.py``):

* **Power does not toggle.** ``chassis power on/off`` is accepted ("Chassis Power
  Control: Up/On") but the reported state never changes — ipmi_sim binds chassis
  power to an external QEMU VM (``startcmd``, ``startnow false``) that isn't
  running. So we assert the power verbs *execute* against a live BMC, not a flip.
  And because the modelled state is stuck at "off", the state-dependent verbs
  ``reset``/``soft`` are rejected ("Invalid data field") — you can't reset a
  powered-off chassis — so only ``power_off_hard``/``power_on`` are exercised live.
* **No device SDRs.** The MC is ``no-device-sdrs`` with no SDR repository records,
  so ``sdr elist`` is empty → ``read_sensors()["count"]`` is 0.
* **Boot parameter 5 GET is unimplemented** ("Invalid data field in request"), so
  ``get_boot_options()`` degrades to ``target=None``/``enabled='Unknown'`` while
  still feature-detecting the static ``allowable`` set.

State round-trips (power flip, boot target read-back, populated sensors) are
covered by the fake-ipmitool unit tests (``tests/test_ipmi.py``) and by real
iLO/iDRAC hardware (#29); here we prove the wire protocol and parsers against an
independent BMC.
"""

from __future__ import annotations

import pytest

from kvm_pilot.drivers.base import BootConfig, Logs, Power, Sensors, SystemInfo
from kvm_pilot.drivers.ipmi import IpmiDriver
from kvm_pilot.errors import KVMPilotError
from kvm_pilot.safety import allow_all

pytestmark = pytest.mark.integration


def _driver(bmc: dict) -> IpmiDriver:
    return IpmiDriver(
        bmc["host"], bmc["user"], bmc["passwd"],
        port=bmc["port"], cipher=bmc.get("cipher"), confirm=allow_all,
    )


def test_capabilities_match_the_ipmi_set(ipmi_bmc):
    # Structural capability set, corroborated against a live BMC session.
    d = _driver(ipmi_bmc)
    assert isinstance(d, Power | SystemInfo | Sensors | Logs | BootConfig)


def test_info_talks_to_ipmi_sim(ipmi_bmc):
    info = _driver(ipmi_bmc).get_info()
    # An independent, non-Dell BMC answered through IpmiDriver -> ipmitool ->
    # RMCP+/lanplus, and our `mc info` parser read a real identity back.
    assert info["power_state"] in ("on", "off")
    assert info["manufacturer"] is not None or info["bmc_version"] is not None


def test_power_verbs_execute_against_live_bmc(ipmi_bmc):
    # Each verb frames a real `chassis power <verb>` the BMC accepts (no non-zero
    # exit). We do NOT assert a state flip: stock ipmi_sim doesn't model chassis
    # power (see module docstring). reset/soft are state-dependent and rejected
    # while the modelled state is off, so only off/on are exercised here; all four
    # verbs' framing is covered by the fake-ipmitool unit tests + real hardware.
    d = _driver(ipmi_bmc)
    d.power_off_hard()
    d.power_on()
    assert isinstance(d.is_powered_on(), bool)


def test_boot_device_commands_execute_and_feature_detect(ipmi_bmc):
    # set_boot_device frames a real `chassis bootdev` the BMC accepts; get_boot_options
    # degrades gracefully where the sim omits boot-param-5 GET, yet still reports the
    # static IPMI allowable set + that mode is settable.
    d = _driver(ipmi_bmc)
    d.set_boot_device("pxe", once=True)
    d.set_boot_device("hdd", once=False)
    opts = d.get_boot_options()
    assert "pxe" in opts["allowable"] and "hdd" in opts["allowable"]
    assert opts["mode_settable"] is True


def test_usb_boot_rejected_before_send(ipmi_bmc):
    # Client-side feature-detect proven against a live BMC: IPMI has no usb bootdev
    # selector, so a usb request fails fast rather than sending a doomed command.
    d = _driver(ipmi_bmc)
    assert "usb" not in d.get_boot_options()["allowable"]
    with pytest.raises(KVMPilotError):
        d.set_boot_device("usb")


def test_sensors_and_sel_readable(ipmi_bmc):
    d = _driver(ipmi_bmc)
    sensors = d.read_sensors()
    assert isinstance(sensors["count"], int)   # 0 on stock ipmi_sim (no device SDRs)
    assert isinstance(sensors["sensors"], list)
    assert isinstance(d.get_logs(), str)        # SEL, may be empty
