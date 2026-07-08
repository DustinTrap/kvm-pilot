"""Remote PowerShell (WinRM / PSRemoting over SSH) — the winrm interface (#181)."""

from __future__ import annotations

import base64

from kvm_pilot.remote_ps import RemotePowerShell, encode_command


class FakeSSH:
    """Stand-in SSHChannel that records the command instead of shelling out."""

    def __init__(self, reachable: bool = True, returncode: int = 0):
        self._reachable = reachable
        self._rc = returncode
        self.last_command: str | None = None

    def ssh_reachable(self) -> bool:
        return self._reachable

    def ssh_exec(self, command: str, *, timeout=None) -> dict:
        self.last_command = command
        return {"command": command, "returncode": self._rc, "stdout": "5", "stderr": "", "ok": self._rc == 0}


def test_encode_command_is_base64_utf16le():
    script = 'Get-ComputerInfo | Select-Object "OsName","OsVersion"'
    assert base64.b64decode(encode_command(script)).decode("utf-16-le") == script


def test_run_ps_wraps_script_in_encodedcommand():
    ssh = FakeSSH()
    rp = RemotePowerShell(ssh, shell="pwsh")
    result = rp.run_ps("$PSVersionTable.PSVersion.Major")

    assert result["returncode"] == 0
    assert ssh.last_command.startswith("pwsh -NoProfile -NonInteractive -EncodedCommand ")
    encoded = ssh.last_command.rsplit(" ", 1)[1]
    assert base64.b64decode(encoded).decode("utf-16-le") == "$PSVersionTable.PSVersion.Major"


def test_default_shell_is_windows_powershell():
    ssh = FakeSSH()
    RemotePowerShell(ssh).run_ps("whoami")
    assert ssh.last_command.startswith("powershell -NoProfile -NonInteractive ")


def test_reachable_delegates_to_transport():
    assert RemotePowerShell(FakeSSH(reachable=True)).reachable() is True
    assert RemotePowerShell(FakeSSH(reachable=False)).reachable() is False
