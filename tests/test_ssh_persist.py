"""Persistent SSH connections via OpenSSH ControlMaster (#181).

The router wants the in-band OS plane to be cheap; measured ~10x on a LAN host
(~263ms fresh vs ~26ms over a reused ControlMaster socket). These check the argv
seam and that teardown is a safe no-op — no network.
"""

from __future__ import annotations

from kvm_pilot.ssh import SSHChannel


def test_persist_injects_controlmaster_options():
    argv = SSHChannel("host", user="u", persist=True)._ssh_argv()
    joined = " ".join(argv)
    assert "ControlMaster=auto" in joined
    assert "ControlPersist=30" in joined
    assert any(a.startswith("ControlPath=") for a in argv)
    # tokens are left for ssh to expand (keeps the socket path per-target)
    assert any("%h-%p-%r" in a for a in argv)


def test_without_persist_there_is_no_controlmaster():
    argv = SSHChannel("host", user="u", persist=False)._ssh_argv()
    assert "ControlMaster=auto" not in " ".join(argv)


def test_close_is_a_safe_noop_without_persist():
    # Must not shell out or raise when there's no master to tear down.
    SSHChannel("host", persist=False).close()
