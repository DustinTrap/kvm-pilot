"""Remote PowerShell — the WinRM / PS-Remoting interface (#181).

Runs PowerShell on a Windows (or PowerShell-7) target and returns structured
text. This is the fast, in-band alternative to driving the console over the KVM
for anything the OS can answer (inventory, service state, config) — one call of
clean text instead of typing into a video console and OCR-ing frames back.

"Remote PowerShell" has two common transports:

* **WS-Man / WinRM** (ports 5985/5986) — the classic PSRemoting
  (`Enter-PSSession` / `Invoke-Command`). A native client needs a third-party
  library (e.g. ``pypsrp``); that would live behind an optional ``winrm`` extra
  and import lazily, per the stdlib-only-at-import rule. **Not shipped yet.**
* **PowerShell over SSH** — PowerShell 7's own remoting transport. Reuses the
  ``SSHChannel`` this package already has (system ``ssh``, no new dependency).
  **This is what ships here.**

Both satisfy the same seam (``reachable`` / ``run_ps``), so the router treats
them as one ``winrm`` interface and the transport is an implementation detail.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

from .ssh import SSHChannel

if TYPE_CHECKING:
    from .config import HostConfig


def encode_command(script: str) -> str:
    """PowerShell ``-EncodedCommand`` payload: base64 of the UTF-16LE script.

    This is the quoting-safe way to pass an arbitrary script through argv and an
    SSH command line — no shell metacharacter or newline can break out of it.
    """
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


class RemotePowerShell:
    """Run PowerShell on the managed host, over SSH (dependency-free PSRemoting).

    ``shell`` is the remote interpreter: ``"powershell"`` (Windows PowerShell
    5.1) or ``"pwsh"`` (PowerShell 7+). Built from a profile with
    :meth:`from_config`, which requires ``ssh_host`` to be set (the target's own
    address — a different machine from the KVM appliance).
    """

    def __init__(self, ssh: SSHChannel, *, shell: str = "powershell"):
        self.ssh = ssh
        self.shell = shell

    @classmethod
    def from_config(
        cls,
        cfg: HostConfig,
        *,
        confirm=None,
        dry_run: bool = False,
        shell: str = "powershell",
    ) -> RemotePowerShell:
        return cls(
            SSHChannel.from_config(cfg, confirm=confirm, dry_run=dry_run),
            shell=shell,
        )

    def reachable(self) -> bool:
        """True if the SSH transport to the target accepts a connection. Never raises."""
        return self.ssh.ssh_reachable()

    def run_ps(self, script: str, *, timeout: float | None = None) -> dict:
        """Run ``script`` under the remote PowerShell; returns the ``ssh_exec`` dict.

        The script is passed via ``-EncodedCommand`` so quoting is never a hazard.
        ``-NoProfile -NonInteractive`` keep it fast and script-safe. Gated the
        same as any ``ssh.exec`` (a remote command can change state).
        """
        cmd = (
            f"{self.shell} -NoProfile -NonInteractive "
            f"-EncodedCommand {encode_command(script)}"
        )
        return self.ssh.ssh_exec(cmd, timeout=timeout)
