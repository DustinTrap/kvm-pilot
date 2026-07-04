"""Unit tests for the in-band SSH-to-target channel (issue #81).

No real network or ssh binary: socket and subprocess are mocked.
"""

from __future__ import annotations

import subprocess
from unittest import mock

import pytest

from kvm_pilot.config import HostConfig, resolve_host
from kvm_pilot.drivers.base import CAPABILITY_PROTOCOLS, Capability, RemoteShell
from kvm_pilot.errors import CapabilityError, SafetyError, TimeoutError
from kvm_pilot.safety import DESTRUCTIVE_OPS, SafetyPolicy, deny_all
from kvm_pilot.ssh import MAX_SWEEP_HOSTS, SSHChannel, discover_ssh_hosts


def _cfg(**kw) -> HostConfig:
    base = {"host": "10.0.0.1", "ssh_host": "10.0.0.2", "ssh_user": "root"}
    base.update(kw)
    return HostConfig(**base)


# -- capability seam ---------------------------------------------------------

def test_ssh_is_a_registered_capability():
    assert CAPABILITY_PROTOCOLS[Capability.SSH] is RemoteShell


def test_channel_satisfies_remote_shell_protocol():
    assert isinstance(SSHChannel("h"), RemoteShell)


def test_ssh_exec_is_gated_as_destructive():
    assert "ssh.exec" in DESTRUCTIVE_OPS


# -- construction ------------------------------------------------------------

def test_from_config_requires_ssh_host():
    with pytest.raises(CapabilityError, match="not configured"):
        SSHChannel.from_config(HostConfig(host="10.0.0.1"))  # no ssh_host


def test_from_config_carries_target_fields():
    ch = SSHChannel.from_config(_cfg(ssh_port=2222))
    assert ch.host == "10.0.0.2"
    assert ch.user == "root"
    assert ch.port == 2222
    assert ch.target == "root@10.0.0.2"


# -- reachability ------------------------------------------------------------

def test_reachable_true_when_socket_connects():
    with mock.patch("kvm_pilot.ssh.socket.create_connection") as conn:
        conn.return_value.__enter__.return_value = object()
        assert SSHChannel("h", port=22).ssh_reachable() is True
        conn.assert_called_once()


def test_reachable_false_and_never_raises_on_oserror():
    with mock.patch("kvm_pilot.ssh.socket.create_connection", side_effect=OSError("refused")):
        assert SSHChannel("h").ssh_reachable() is False


# -- exec --------------------------------------------------------------------

def test_exec_runs_and_reports_result():
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="ok\n", stderr="")
    with mock.patch("kvm_pilot.ssh.shutil.which", return_value="/usr/bin/ssh"), \
         mock.patch("kvm_pilot.ssh.subprocess.run", return_value=completed) as run:
        res = SSHChannel("h").ssh_exec("uname -a")
    assert res == {
        "command": "uname -a", "returncode": 0, "stdout": "ok\n",
        "stderr": "", "ok": True, "dry_run": False,
    }
    # the command is the final argv element (a single post-destination arg)
    assert run.call_args.args[0][-1] == "uname -a"


def test_exec_dry_run_skips_subprocess():
    ch = SSHChannel("h", safety=SafetyPolicy(dry_run=True))
    with mock.patch("kvm_pilot.ssh.subprocess.run") as run:
        res = ch.ssh_exec("rm -rf /")
    run.assert_not_called()
    assert res["dry_run"] is True and res["ok"] is False


def test_exec_denied_by_confirm_raises_safety_error():
    ch = SSHChannel("h", safety=SafetyPolicy(confirm=deny_all))
    with mock.patch("kvm_pilot.ssh.subprocess.run") as run, pytest.raises(SafetyError):
        ch.ssh_exec("reboot")
    run.assert_not_called()


def test_exec_missing_ssh_binary_raises_capability_error():
    with mock.patch("kvm_pilot.ssh.shutil.which", return_value=None), \
         pytest.raises(CapabilityError, match="ssh"):
        SSHChannel("h").ssh_exec("true")


def test_exec_timeout_maps_to_kvm_pilot_timeout():
    with mock.patch("kvm_pilot.ssh.shutil.which", return_value="/usr/bin/ssh"), \
         mock.patch("kvm_pilot.ssh.subprocess.run",
                    side_effect=subprocess.TimeoutExpired("ssh", 5)), \
         pytest.raises(TimeoutError):
        SSHChannel("h", timeout=5).ssh_exec("sleep 99")


# -- config resolution -------------------------------------------------------

def test_resolve_host_reads_ssh_env(monkeypatch, tmp_path):
    monkeypatch.setenv("KVM_PILOT_SSH_HOST", "192.168.1.50")
    monkeypatch.setenv("KVM_PILOT_SSH_USER", "admin")
    monkeypatch.setenv("KVM_PILOT_SSH_PORT", "2200")
    cfg = resolve_host(host="10.0.0.1", config_path=tmp_path / "none.toml")
    assert cfg.ssh_host == "192.168.1.50"
    assert cfg.ssh_user == "admin"
    assert cfg.ssh_port == 2200


def test_resolve_host_ssh_port_defaults_to_22(tmp_path):
    cfg = resolve_host(host="10.0.0.1", config_path=tmp_path / "none.toml")
    assert cfg.ssh_host is None
    assert cfg.ssh_port == 22


# -- network sweep (opt-in, risky) -------------------------------------------

def test_discover_returns_only_open_hosts():
    open_hosts = {"10.0.0.3"}
    with mock.patch("kvm_pilot.ssh._port_open", side_effect=lambda h, p, t: h in open_hosts):
        found = discover_ssh_hosts("10.0.0.0/29", port=22)
    assert found == [{"host": "10.0.0.3", "port": 22}]


def test_discover_rejects_over_broad_range():
    with pytest.raises(ValueError, match="narrow the range"):
        discover_ssh_hosts(f"10.0.0.0/{32 - (MAX_SWEEP_HOSTS.bit_length())}")  # > MAX_SWEEP_HOSTS


def test_discover_rejects_malformed_cidr():
    with pytest.raises(ValueError):
        discover_ssh_hosts("not-a-cidr")
