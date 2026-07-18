"""The interface-router scorecard profiler (#181).

Runs entirely against the in-memory FakeDriver with an injected clock, so it's
deterministic and touches no network — the benchmark logic (capability gating,
warm p50, failure capture, fastest-capable selection) is what's under test, not
real latency.
"""

from __future__ import annotations

import pytest

from kvm_pilot.benchmark import (
    CommandResult,
    Op,
    Scorecard,
    _measure,
    benchmark_driver,
)
from kvm_pilot.drivers.fake import FakeDriver


def make_clock(step: float = 0.001):
    """A fake perf clock: each call advances by ``step`` s, so every measured
    call reads as exactly ``step*1000`` ms — deterministic, no wall-clock."""
    t = [0.0]

    def clock() -> float:
        v = t[0]
        t[0] += step
        return v

    return clock


def _by_command(card: Scorecard) -> dict[str, CommandResult]:
    return {r.command: r for r in card.results}


def test_scorecard_charts_capable_and_unsupported_ops():
    d = FakeDriver(powered=True, phase="grub_menu")
    card = benchmark_driver(
        d, host="fake", driver_kind="fake", samples=6, clock=make_clock()
    )
    by = _by_command(card)

    # Every op the driver structurally supports is measured, capable, warm p50 set.
    for cmd in ("capabilities", "get_info", "get_logs", "is_powered_on",
                "snapshot", "boot_progress", "get_boot_options", "mouse_move"):
        assert by[cmd].capable, f"{cmd} should be capable on the fake driver"
        assert by[cmd].p50_ms == pytest.approx(1.0)  # step=0.001 s -> 1.0 ms per call
        assert by[cmd].samples == 6
        assert by[cmd].interface == "library"

    # A capability the driver lacks is charted (not silently skipped) with no
    # network call: capable False, no samples, an explanatory note.
    sensors = by["read_sensors"]
    assert sensors.capable is False
    assert sensors.p50_ms is None
    assert sensors.samples == 0
    assert "not supported" in sensors.note


def test_hid_probe_can_be_opted_out():
    d = FakeDriver(powered=True)
    card = benchmark_driver(d, host="fake", driver_kind="fake", hid=False, clock=make_clock())
    assert "mouse_move" not in _by_command(card)


def test_scorecard_metadata_and_serialization_round_trip():
    d = FakeDriver(powered=True)
    card = benchmark_driver(
        d, host="fake", driver_kind="fake", firmware="V1.9.1", samples=4, clock=make_clock()
    )
    assert card.host == "fake"
    assert card.driver == "fake"
    assert card.firmware == "V1.9.1"

    payload = card.to_dict()
    assert payload["host"] == "fake"
    assert payload["firmware"] == "V1.9.1"
    info = next(r for r in payload["results"] if r["command"] == "get_info")
    assert info["capable"] is True
    assert info["interface"] == "library"
    assert isinstance(info["p50_ms"], float)


def test_fastest_returns_lowest_latency_capable_row():
    card = Scorecard(
        host="h", driver="fake", firmware=None,
        results=[
            CommandResult("snapshot", "library", True, 50.0, 5),
            CommandResult("snapshot", "mcp", True, 20.0, 5),
            CommandResult("snapshot", "chrome", True, 2000.0, 5),
            CommandResult("get_info", "library", False, None, 0, "boom"),
        ],
    )
    # cheapest *capable* interface wins
    assert card.fastest("snapshot").interface == "mcp"
    # a command with no capable interface has no answer (router must fall back)
    assert card.fastest("get_info") is None
    # an unknown command likewise
    assert card.fastest("nonexistent") is None


def test_measure_records_failure_as_data_not_a_crash():
    def boom(_driver):
        raise RuntimeError("streamer returned H.264")

    result = _measure(Op("snapshot", None, boom), object(), samples=3, clock=make_clock())
    assert result.capable is False
    assert result.p50_ms is None
    assert result.samples == 0
    assert "RuntimeError" in result.note
    assert "H.264" in result.note


# -- OS-plane interfaces: SSH + WinRM (#181) --------------------------------

def _cfg(**over):
    from kvm_pilot.config import resolve_host
    base = dict(host="1.2.3.4", user="a", passwd="x", driver="fake")
    base.update(over)
    return resolve_host(None, **base)


def _stub_channel(monkeypatch, *, reachable=True, returncode=0):
    import kvm_pilot.ssh as sshmod

    class Ch:
        @classmethod
        def from_config(cls, cfg, **kw):
            return cls()

        def ssh_reachable(self):
            return reachable

        def ssh_exec(self, cmd, *, timeout=None):
            return {"returncode": returncode, "stdout": "", "stderr": "denied", "ok": returncode == 0}

        def close(self):
            pass

    monkeypatch.setattr(sshmod, "SSHChannel", Ch)


def test_benchmark_ssh_not_configured_is_charted_not_crashed():
    from kvm_pilot.benchmark import benchmark_ssh

    rows = benchmark_ssh(_cfg(), samples=2)  # no ssh_host
    assert len(rows) == 1
    assert rows[0].interface == "ssh" and rows[0].capable is False
    assert "not configured" in rows[0].note


def test_benchmark_ssh_capable_when_reachable(monkeypatch):
    _stub_channel(monkeypatch, reachable=True, returncode=0)
    from kvm_pilot.benchmark import benchmark_ssh

    rows = benchmark_ssh(_cfg(ssh_host="5.6.7.8"), samples=3, clock=make_clock())
    assert rows[0].capable is True
    assert rows[0].p50_ms == pytest.approx(1.0)


def test_benchmark_ssh_nonzero_exit_is_not_capable(monkeypatch):
    _stub_channel(monkeypatch, reachable=True, returncode=255)
    from kvm_pilot.benchmark import benchmark_ssh

    rows = benchmark_ssh(_cfg(ssh_host="5.6.7.8"), samples=2, clock=make_clock())
    assert rows[0].capable is False
    assert "255" in rows[0].note


def test_benchmark_winrm_not_configured():
    from kvm_pilot.benchmark import benchmark_winrm

    rows = benchmark_winrm(_cfg(), samples=2)
    assert rows[0].interface == "winrm" and rows[0].capable is False


def test_benchmark_all_merges_library_ssh_and_winrm_rows():
    from kvm_pilot.benchmark import benchmark_all

    d = FakeDriver(powered=True)
    card = benchmark_all(d, _cfg(), host="fake", driver_kind="fake", samples=3, clock=make_clock())
    assert {r.interface for r in card.results} >= {"library", "ssh", "winrm"}

    kvm_only = benchmark_all(
        d, _cfg(), host="fake", driver_kind="fake", samples=3, os_plane=False, clock=make_clock()
    )
    assert {r.interface for r in kvm_only.results} == {"library"}
