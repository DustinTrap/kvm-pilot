"""In-band SSH channel to the managed host's OS (issue #81).

This targets the **host behind the KVM**, not the KVM appliance — a separate
machine with its own address and login (the profile's ``ssh_*`` fields). It lets
an agent probe whether the target OS is network-reachable and run recovery
commands once it is, so remote recovery can be preferred over asking a user to
physically intervene.

Dependency-free: reachability uses the stdlib ``socket``; command execution shells
out to the system ``ssh`` binary in ``BatchMode`` (no interactive prompts) — no
third-party SSH library. State-changing execs route through ``SafetyPolicy``
(``ssh.exec``); the reachability probe is read-only and ungated.
"""

from __future__ import annotations

import logging
import shutil
import socket
import subprocess

from .config import HostConfig
from .errors import CapabilityError, TimeoutError
from .safety import SafetyPolicy

logger = logging.getLogger("kvm_pilot.ssh")

DEFAULT_SSH_PORT = 22
_REACHABLE_TIMEOUT = 5.0  # a liveness probe should be quick, regardless of cfg.timeout


class SSHChannel:
    """SSH reachability + command execution against the managed host's OS.

    Build one with :meth:`from_config` (which enforces that SSH is configured for
    the profile). Implements the ``RemoteShell`` capability seam
    (``ssh_reachable`` / ``ssh_exec``) — see ``drivers/base.py``.
    """

    def __init__(
        self,
        host: str,
        *,
        user: str | None = None,
        port: int = DEFAULT_SSH_PORT,
        key: str | None = None,
        timeout: float = 30.0,
        safety: SafetyPolicy | None = None,
    ):
        self.host = host
        self.user = user
        self.port = port
        self.key = key
        self.timeout = timeout
        self.safety = safety or SafetyPolicy()

    @classmethod
    def from_config(
        cls,
        cfg: HostConfig,
        *,
        safety: SafetyPolicy | None = None,
        confirm=None,
        dry_run: bool = False,
    ) -> SSHChannel:
        """Build a channel from a resolved ``HostConfig``.

        Raises ``CapabilityError`` if the profile has no ``ssh_host`` — SSH-to-target
        is opt-in and never inferred from the KVM appliance's address.
        """
        if not cfg.ssh_host:
            raise CapabilityError(
                "SSH-to-target is not configured for this profile. Set ssh_host "
                "(or KVM_PILOT_SSH_HOST) to the managed host's own IP/hostname — "
                "it is a different machine from the KVM appliance."
            )
        if safety is None:
            safety = SafetyPolicy(dry_run=dry_run, confirm=confirm)
        return cls(
            cfg.ssh_host,
            user=cfg.ssh_user,
            port=cfg.ssh_port,
            key=cfg.ssh_key,
            timeout=cfg.timeout,
            safety=safety,
        )

    # -- capability seam (RemoteShell) --------------------------------------

    def ssh_reachable(self) -> bool:
        """True if the target's SSH port accepts a TCP connection. Never raises."""
        timeout = min(self.timeout, _REACHABLE_TIMEOUT)
        try:
            with socket.create_connection((self.host, self.port), timeout=timeout):
                return True
        except OSError:
            return False

    def ssh_exec(self, command: str, *, timeout: float | None = None) -> dict:
        """Run ``command`` on the target over SSH. Gated as ``ssh.exec``.

        Returns ``{command, returncode, stdout, stderr, ok, dry_run}``. Under
        dry-run the command is logged and skipped (``dry_run=True``, ``ok=False``).
        Raises ``SafetyError`` if a confirm callback denies it, ``CapabilityError``
        if the system ``ssh`` binary is missing, and ``TimeoutError`` on timeout.
        """
        desc = f"ssh {self.target}: {command}"
        if not self.safety.guard("ssh.exec", desc):
            return _result(command, returncode=None, stdout="", stderr="", dry_run=True)
        if shutil.which("ssh") is None:
            raise CapabilityError(
                "The system 'ssh' binary was not found on PATH; kvm-pilot's SSH "
                "channel shells out to it."
            )
        # command is a single post-destination arg → ssh runs it via the remote
        # shell; args after the destination are never parsed as ssh options.
        argv = self._ssh_argv() + [command]
        try:
            proc = subprocess.run(  # noqa: S603 - argv is built from config, shell=False
                argv,
                capture_output=True,
                text=True,
                timeout=timeout or self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(f"ssh exec timed out after {timeout or self.timeout}s") from exc
        return _result(
            command,
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            dry_run=False,
        )

    # -- helpers ------------------------------------------------------------

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host

    def _ssh_argv(self) -> list[str]:
        argv = [
            "ssh",
            "-o", "BatchMode=yes",            # never block on a password/passphrase prompt
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"ConnectTimeout={int(min(self.timeout, _REACHABLE_TIMEOUT))}",
            "-p", str(self.port),
        ]
        if self.key:
            argv += ["-i", self.key]
        argv.append(self.target)
        return argv


def _result(command, *, returncode, stdout, stderr, dry_run) -> dict:
    return {
        "command": command,
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
        "ok": returncode == 0,
        "dry_run": dry_run,
    }


__all__ = ["SSHChannel", "DEFAULT_SSH_PORT"]
