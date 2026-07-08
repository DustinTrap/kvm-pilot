"""Dep-free password auth for the target SSH channel via SSH_ASKPASS (#183).

All offline: they check the argv/env construction and that the password never
lands on disk or on argv — no network, no real ssh.
"""

from __future__ import annotations

import os
import subprocess

from kvm_pilot.config import resolve_host
from kvm_pilot.ssh import SSHChannel


def test_password_mode_forces_password_and_drops_batchmode():
    argv = SSHChannel("h", user="u", key="/some/key", password="pw")._ssh_argv()
    joined = " ".join(argv)
    assert "PreferredAuthentications=password" in joined
    assert "PubkeyAuthentication=no" in joined
    assert "BatchMode=yes" not in joined   # BatchMode would disable the askpass helper
    assert "-i" not in argv                 # the key is ignored in password mode


def test_key_mode_is_unchanged_and_needs_no_special_env():
    ch = SSHChannel("h", user="u", key="/some/key")
    assert "BatchMode=yes" in ch._ssh_argv()
    env, extra = ch._auth_run_kwargs()
    assert env is None and extra == {}


def test_askpass_wires_password_without_writing_it_to_disk_or_argv():
    ch = SSHChannel("h", user="u", password="s3cret-pw")
    env, extra = ch._auth_run_kwargs()

    assert env is not None
    helper = env["SSH_ASKPASS"]
    assert os.path.exists(helper)
    assert env["SSH_ASKPASS_REQUIRE"] == "force"
    assert env["KVM_PILOT_SSH_ASKPASS_PW"] == "s3cret-pw"   # secret only in env
    assert extra["stdin"] == subprocess.DEVNULL
    assert extra["start_new_session"] is True

    script = open(helper).read()
    assert "s3cret-pw" not in script                        # not on disk
    assert "KVM_PILOT_SSH_ASKPASS_PW" in script             # reads it from env at exec
    assert oct(os.stat(helper).st_mode)[-3:] == "700"       # owner-only

    ch.close()
    assert not os.path.exists(helper)                       # cleaned up on close


def test_ssh_password_resolves_from_env(monkeypatch):
    monkeypatch.setenv("KVM_PILOT_SSH_PASSWORD", "envpw")
    cfg = resolve_host(None, host="1.2.3.4", user="a", passwd="x", ssh_host="5.6.7.8", driver="fake")
    assert cfg.ssh_password == "envpw"
