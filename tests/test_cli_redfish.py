"""CLI ``--driver redfish`` end-to-end over the real transport against a fake BMC.

Complements the offline dispatch tests in ``test_cli.py``: these drive the actual
``main()`` entry point against the pure-stdlib Redfish emulator, so the whole
chain — argparse → ``make_driver_from_config`` → ``RedfishDriver`` → ``RedfishHTTP``
→ HTTP — is exercised for the subcommands a BMC *does* support (info, power,
power-cycle, mount). No Docker, no hardware.

The external best-in-class equivalent (sushy-tools) lives in
``tests/integration/test_redfish_external.py``.
"""

from __future__ import annotations

from kvm_pilot.cli import main
from redfish_emulator import RESET

# The `emu` fixture (a running RedfishEmulator) is shared from tests/conftest.py.


def _argv(emu, *rest: str) -> list[str]:
    return [*rest, "--driver", "redfish", "--host", emu.host,
            "--port", str(emu.port), "--scheme", "http"]


def _reset_types(emu) -> list[str]:
    return [body.get("ResetType") for path, body in emu.state.posts if path == RESET]


def test_cli_info_reports_bmc_identity(emu, capsys):
    emu.state.power_state = "On"
    rc = main(_argv(emu, "info"))
    assert rc == 0
    out = capsys.readouterr().out
    assert "ACME" in out and "Server 9000" in out  # from the emulator's ComputerSystem


def test_cli_power_on_drives_real_state(emu):
    emu.state.power_state = "Off"
    rc = main(_argv(emu, "power", "on", "--yes"))
    assert rc == 0
    assert emu.state.power_state == "On"
    assert _reset_types(emu) == ["On"]


def test_cli_power_cycle_forces_off_then_on(emu):
    # power-cycle works on a BMC via RedfishDriver.hard_cycle (force off -> on),
    # the method added so every POWER driver answers the command uniformly.
    emu.state.power_state = "On"
    rc = main(_argv(emu, "power-cycle", "--yes"))
    assert rc == 0
    assert emu.state.power_state == "On"
    assert _reset_types(emu) == ["ForceOff", "On"]


def test_cli_mount_inserts_virtual_media(emu):
    iso = "http://srv/imgs/ubuntu-24.04.iso"
    rc = main(_argv(emu, "mount", iso, "--yes"))
    assert rc == 0
    assert emu.state.inserted is True
    assert emu.state.last_image == iso


def test_cli_mount_is_gated_under_dry_run(emu):
    # The insert is destructive; --dry-run logs without sending it (--yes so the
    # confirm step, which runs before the dry-run skip, is non-interactive).
    rc = main(_argv(emu, "mount", "http://srv/x.iso", "--yes", "--dry-run"))
    assert rc == 0
    assert emu.state.inserted is False
