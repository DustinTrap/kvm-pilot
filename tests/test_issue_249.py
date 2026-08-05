"""#249 — reports must not name components the device does not have.

Found during a live fleet sweep: an Intel AMT machine's access-path report
called its primary plane `kvmd-rest` and `firmware-check` printed `(kvmd None)`.
Neither is fatal, but both send an operator to reason about a daemon that is not
running — the same class of error as #245's invented causes, just quieter.
"""

from __future__ import annotations

import io
import sys

import pytest

from kvm_pilot.health import _PRIMARY_PATH, access_paths
from kvm_pilot.safety import SafetyPolicy, interactive_confirm


def _primary(driver):
    return next(p for p in access_paths(driver)["paths"] if p["kind"] == "primary")


# -- 1. the primary access path is named for the driver --------------------


def test_amt_primary_path_is_wsman_not_kvmd():
    """The finding as reported, on the device that produced it."""
    from kvm_pilot.drivers import make_driver

    p = _primary(make_driver("amt", host="h"))
    assert p["path"] == "amt-wsman"
    assert "kvmd" not in p["path"] and "kvmd" not in p["detail"]


@pytest.mark.parametrize(
    "kind,expected",
    [("redfish", "redfish-api"), ("ipmi", "ipmi-rmcp"), ("amt", "amt-wsman"),
     ("ssh", "os-plane-ssh"), ("fake", "in-process")],
)
def test_no_non_kvmd_driver_claims_kvmd(kind, expected):
    from kvm_pilot.drivers import make_driver

    p = _primary(make_driver(kind, host="h"))
    assert p["path"] == expected
    assert "kvmd" not in p["detail"].lower()


def test_the_pikvm_family_still_says_kvmd():
    """These devices DO run kvmd — the fix is precision, not scrubbing a word."""
    from kvm_pilot.drivers import make_driver

    for kind in ("pikvm", "glkvm", "blikvm"):
        p = _primary(make_driver(kind, host="h", user="u", passwd="p"))
        assert p["path"] == "kvmd-rest", kind
        assert "kvmd" in p["detail"]


def test_every_driver_kind_has_a_primary_path():
    """A new driver must not silently inherit the PiKVM label — which is exactly
    how AMT came to report kvmd.

    Checked against `_DRIVER_KINDS`, not the driver registry: the registry is
    global mutable state that other tests extend via `register_driver`, so
    asserting on it made this test pass alone and fail in a full run.
    """
    from kvm_pilot.health import _DRIVER_KINDS

    assert set(_PRIMARY_PATH) == set(_DRIVER_KINDS)


# -- 2 & 3. firmware-check's kvmd parenthetical and vendor key -------------


def test_firmware_check_omits_kvmd_when_there_is_none(capsys, monkeypatch):
    out = _run_firmware_check(monkeypatch, capsys,
                              {"vendor": "Dell Inc.", "product": "Latitude 5411",
                               "version": "14.1.79", "kvmd_version": None})
    assert "kvmd" not in out
    assert "14.1.79" in out


def test_firmware_check_keeps_kvmd_when_there_is_one(capsys, monkeypatch):
    out = _run_firmware_check(monkeypatch, capsys,
                              {"vendor": "gl.inet", "product": "RM1PE",
                               "version": "V1.9.1", "kvmd_version": "4.82"})
    assert "kvmd 4.82" in out


def test_firmware_check_shows_the_registry_key_when_it_differs(capsys, monkeypatch):
    """#243 was a day lost to 'Dell Inc.' failing to join 'dell'. Printing the
    raw vendor beside a version invites that confusion again; show both."""
    out = _run_firmware_check(monkeypatch, capsys,
                              {"vendor": "Dell Inc.", "product": "Latitude 5411",
                               "version": "14.1.79", "kvmd_version": None})
    assert "Dell Inc." in out and "[dell]" in out


def _run_firmware_check(monkeypatch, capsys, fw):
    """Drive the real `firmware-check` command with a canned driver."""
    from kvm_pilot import cli

    class _Drv:
        VIDEO_SCOPE = None

        def get_firmware_info(self):
            return dict(fw)

        def check_firmware_update(self):
            return None

    monkeypatch.setattr(cli, "_build_client", lambda *a, **k: _Drv())
    rc = cli.main(["firmware-check", "--host", "h", "--no-file-report"])
    assert rc == 0
    return capsys.readouterr().out


# -- 4. a gated command with no terminal fails fast ------------------------


def test_no_tty_declines_immediately_instead_of_blocking(monkeypatch, capsys):
    """`input()` on an open-but-silent stdin never returns and never raises EOF.
    Under a pipe or a consumed heredoc that hung forever with the prompt buried
    in captured output — indistinguishable from a wedged device. During the sweep
    that cost a near-miss bug report against working hardware.
    """
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))   # not a tty

    def _boom(*_a, **_k):
        raise AssertionError("input() must not be called without a terminal")

    monkeypatch.setattr("builtins.input", _boom)
    assert interactive_confirm("hid.press_key", "Press 'shift'") is False
    err = capsys.readouterr().err
    assert "--yes" in err and "no terminal" in err


def test_the_decline_surfaces_as_a_normal_safety_error(monkeypatch):
    """It must still be a refusal, not a silent skip — dry-run is the skip."""
    from kvm_pilot.errors import SafetyError

    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    pol = SafetyPolicy(confirm=interactive_confirm)
    with pytest.raises(SafetyError):
        pol.guard("hid.press_key", "Press 'shift'")


def test_a_real_terminal_still_prompts(monkeypatch):
    """Don't break the interactive path this exists to serve."""
    class _Tty(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", _Tty(""))
    monkeypatch.setattr("builtins.input", lambda *_a: "y")
    assert interactive_confirm("hid.press_key", "Press 'shift'") is True
