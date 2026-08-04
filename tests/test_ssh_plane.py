"""OS-plane targets: SSH-only machines with no device beneath them (#248).

The point of this driver is not what it can do — ``ssh-exec`` could already run
commands. It is that ``healthcheck`` and the router can *see* an SSH-only target
at all, and say the true and useful thing about it: **there is no out-of-band
recovery here.** These tests pin that, and pin the two things that must NOT
change with it.
"""

from __future__ import annotations

import pytest

from kvm_pilot.config import HostConfig, resolve_host
from kvm_pilot.drivers import make_driver, make_driver_from_config
from kvm_pilot.drivers.base import Capability, VideoScope
from kvm_pilot.drivers.ssh_plane import SshDriver
from kvm_pilot.errors import CapabilityError


def _cfg(**kw) -> HostConfig:
    kw.setdefault("driver", "ssh")
    kw.setdefault("ssh_host", "10.0.0.9")
    kw.setdefault("host", kw["ssh_host"])
    return HostConfig(**kw)


# -- what it is ------------------------------------------------------------


def test_it_advertises_no_device_capabilities():
    """There is no device here. An empty set is the honest answer, and it is
    what stops every capability-gated subcommand from reaching a handler."""
    d = make_driver("ssh", host="h")
    assert d.capabilities() == set()
    for cap in (Capability.POWER, Capability.HID, Capability.VIDEO,
                Capability.VIRTUAL_MEDIA, Capability.BOOT_CONFIG):
        assert not d.supports(cap), cap


def test_it_reports_no_video_at_all():
    assert make_driver("ssh", host="h").video_scope() is VideoScope.NONE


def test_it_requires_an_ssh_host_rather_than_falling_back_to_host():
    """`host` means "the appliance" for every other driver. There is no appliance
    here, so demanding ssh_host keeps that distinction unambiguous."""
    with pytest.raises(CapabilityError, match="ssh_host"):
        SshDriver.from_config(HostConfig(host="10.0.0.9", driver="ssh"))


def test_the_os_plane_target_addresses_itself():
    """With no appliance, the managed host's own address IS the identity every
    report, cache key and audit line keys on."""
    cfg = resolve_host(driver="ssh", ssh_host="10.0.0.9", config_path=None)
    assert cfg.host == "10.0.0.9"
    assert make_driver_from_config(cfg).host == "10.0.0.9"


def test_it_is_labelled_ssh_not_pikvm():
    """`_driver_kind` maps by class name with a pikvm fallback — an unlisted
    driver would silently report as a PiKVM in every health report."""
    from kvm_pilot.health import _driver_kind

    assert _driver_kind(make_driver("ssh", host="h")) == "ssh"


# -- the finding that justifies the whole thing ----------------------------


def test_healthcheck_reports_the_missing_out_of_band_recovery_as_CRITICAL():
    """The single most consequential fact about an SSH-only target, and one its
    operator could not hear at all before this. Previously the check returned
    None and vanished from the report — which reads as "fine"."""
    from kvm_pilot.health import Severity, check_recovery_path

    res = check_recovery_path(make_driver("ssh", host="h"))
    assert res is not None
    assert res.severity is Severity.CRITICAL
    assert "no power control" in res.detail
    # It must name the fix, not just the fault.
    assert "BMC" in res.remediation or "KVM appliance" in res.remediation


def test_a_device_with_power_still_reports_a_healthy_recovery_path():
    """The CRITICAL is about *absence of power control*, not about being new —
    a BMC must not start failing this check."""
    from kvm_pilot.drivers.fake import FakeDriver
    from kvm_pilot.health import Severity, check_recovery_path

    res = check_recovery_path(FakeDriver("h"))
    assert res is not None and res.severity is Severity.OK


def test_full_healthcheck_runs_and_is_gated_by_that_critical(monkeypatch, tmp_path):
    from kvm_pilot.health import Severity, run_healthcheck

    class _Chan:
        target, port, host = "root@10.0.0.9", 22, "10.0.0.9"

        def ssh_reachable(self):
            return True

    drv = make_driver("ssh", host="10.0.0.9")
    drv.ssh_channel = _Chan()
    report = run_healthcheck(drv)
    ids = {r.id for r in report.results}
    assert "recovery-path" in ids and "ssh-reachable" in ids
    assert report.worst is Severity.CRITICAL
    # The in-band channel is reported as available — it is a real recovery lever,
    # just not an out-of-band one.
    ssh = next(r for r in report.results if r.id == "ssh-reachable")
    assert ssh.severity is Severity.OK


# -- what must NOT change --------------------------------------------------


def test_auto_detection_never_returns_the_os_plane():
    """#235 refuses to *guess* on a host that only answers SSH, and that stays
    true: an SSH banner identifies a reachable OS, not a device to manage.
    This driver is only ever chosen explicitly."""
    from kvm_pilot import detect
    from kvm_pilot.drivers import _DRIVER_FACTORIES
    from kvm_pilot.errors import KVMPilotError

    # Selectable by name...
    assert "ssh" in _DRIVER_FACTORIES

    # ...but a host that answers ONLY SSH still refuses, rather than quietly
    # resolving to the OS plane. Staged through the memo so the assertion is
    # about the decision, not about the network.
    detect._MEMO.clear()
    cfg = HostConfig(host="10.0.0.9", driver="auto")
    key = (cfg.host, cfg.port, cfg.scheme, cfg.amt_port, cfg.amt_tls, cfg.ipmi_port)
    detect._MEMO[key] = detect.Detection(
        driver=None, evidence=("ssh :22 -> SSH-2.0-OpenSSH_9.9",), interfaces=("ssh",)
    )
    with pytest.raises(KVMPilotError) as ei:
        detect.resolve_auto(cfg)
    assert "refusing to guess" in str(ei.value)
    # And it points at the OS-plane tooling rather than inventing a driver.
    assert "ssh-check" in str(ei.value)
    detect._MEMO.clear()


def test_os_plane_firmware_info_can_never_enter_the_registry():
    """The run ledger joins on a DEVICE's (vendor, product, firmware_version).
    An OS version is a different kind of thing; letting one in would corrupt the
    join the whole maturity ladder rests on (the #243 lesson)."""
    fw = make_driver("ssh", host="h").get_firmware_info()
    assert fw["vendor"] is None and fw["product"] is None and fw["version"] is None
    assert fw["os_plane"] is True


def test_firmware_check_explains_the_category_difference(capsys):
    """Not the generic probe dump — that reads as "failed to detect a device"
    when the truth is "this is not that kind of target"."""
    from kvm_pilot.cli import main

    rc = main(["firmware-check", "--driver", "ssh", "--ssh-host", "10.0.0.9"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "OS-plane target" in err
    assert "firmware registry" in err


def test_capability_gated_commands_refuse_cleanly(capsys):
    """A power command on an OS-plane target must fail on the capability gate
    with a readable message, never AttributeError deep in a handler."""
    from kvm_pilot.cli import main

    rc = main(["power", "off", "--driver", "ssh", "--ssh-host", "10.0.0.9", "--yes"])
    assert rc == 1
    assert "power" in capsys.readouterr().err.lower()
