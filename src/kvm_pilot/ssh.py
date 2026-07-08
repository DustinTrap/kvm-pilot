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

import ipaddress
import logging
import os
import shutil
import socket
import subprocess  # nosec B404 - intentional: shells out to the system `ssh` (no SSH lib)
import tempfile
from concurrent.futures import ThreadPoolExecutor

from .config import HostConfig
from .errors import CapabilityError, TimeoutError
from .safety import SafetyPolicy

logger = logging.getLogger("kvm_pilot.ssh")

DEFAULT_SSH_PORT = 22
_REACHABLE_TIMEOUT = 5.0  # a liveness probe should be quick, regardless of cfg.timeout
MAX_SWEEP_HOSTS = 1024  # refuse an over-broad scan (~/22); ask for a smaller range


def _port_open(host: str, port: int, timeout: float) -> bool:
    """True if a TCP connection to ``host:port`` succeeds. Never raises."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def discover_ssh_hosts(
    cidr: str,
    *,
    port: int = DEFAULT_SSH_PORT,
    timeout: float = 0.5,
    max_hosts: int = MAX_SWEEP_HOSTS,
) -> list[dict]:
    """Scan a CIDR for hosts with an open SSH port.

    **RISKY / opt-in.** This is an active network scan — noisy, and only acceptable
    on networks you own or are authorized to probe. Never run it by default; use it
    only to help a user find a target whose address they don't know, after they
    confirm the range. Returns ``[{"host", "port"}]`` for reachable hosts.

    Raises ``ValueError`` for a malformed CIDR or a range larger than ``max_hosts``.
    """
    net = ipaddress.ip_network(cidr, strict=False)  # raises ValueError on bad input
    hosts = list(net.hosts()) or [net.network_address]
    if len(hosts) > max_hosts:
        raise ValueError(
            f"{cidr} covers {len(hosts)} addresses (> {max_hosts}); narrow the range."
        )
    with ThreadPoolExecutor(max_workers=min(64, len(hosts))) as pool:
        checked = pool.map(lambda ip: (str(ip), _port_open(str(ip), port, timeout)), hosts)
    return [{"host": h, "port": port} for h, is_open in checked if is_open]


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
        persist: bool = False,
        password: str | None = None,
    ):
        # A host beginning with '-' can be misparsed by the ssh binary (and by
        # socket tooling) as an option flag — e.g. '-oProxyCommand=…'. Reject it
        # rather than pass an attacker-influenced runtime override into argv.
        if host.startswith("-"):
            raise CapabilityError(
                f"Refusing SSH host {host!r}: a leading '-' can be misparsed as an "
                "ssh option. Provide a hostname or IP address."
            )
        self.host = host
        self.user = user
        self.port = port
        self.key = key
        self.timeout = timeout
        self.safety = safety or SafetyPolicy()
        # Persistent connection: an OpenSSH ControlMaster socket reused by later
        # calls, so only the first exec pays the TCP+crypto+auth handshake.
        # Measured 10x on a LAN host (~263ms fresh -> ~26ms warm). A short /tmp
        # path keeps the socket under macOS's ~104-char sun_path limit; ssh
        # expands the %h/%p/%r tokens. ControlMaster is a no-op on Windows ssh.
        self.persist = persist
        _tmp = "/tmp" if os.path.isdir("/tmp") else tempfile.gettempdir()  # noqa: S108  # nosec B108 - a ControlMaster socket under a short /tmp path (macOS sun_path limit); user-owned
        self._control_path = os.path.join(_tmp, "kvm-pilot-cm-%h-%p-%r")
        # Opt-in password auth for the *target* channel (distinct from the
        # key-only appliance channel, #183). Dep-free: the password is fed via
        # SSH_ASKPASS — a fixed helper script (no secret on disk) that echoes an
        # env var — so no `sshpass` binary and no third-party library are needed.
        self.password = password
        self._askpass_path: str | None = None

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
            password=cfg.ssh_password,
        )

    # -- capability seam (RemoteShell) --------------------------------------

    def ssh_reachable(self) -> bool:
        """True if the target's SSH port accepts a TCP connection. Never raises."""
        return _port_open(self.host, self.port, min(self.timeout, _REACHABLE_TIMEOUT))

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
        env, extra = self._auth_run_kwargs()
        try:
            # shell=False and argv is built from config (no untrusted shell string);
            # the single command runs via the remote shell over ssh.
            proc = subprocess.run(  # nosec B603
                argv,
                capture_output=True,
                text=True,
                timeout=timeout or self.timeout,
                env=env,
                **extra,
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

    # The askpass helper reads the password from this env var at exec time, so
    # the secret is never written to the helper file or passed on argv.
    _ASKPASS_ENV = "KVM_PILOT_SSH_ASKPASS_PW"

    def _ssh_argv(self) -> list[str]:
        argv = ["ssh"]
        if self.password:
            # Password auth via SSH_ASKPASS: must NOT set BatchMode=yes (it
            # disables askpass). Force password so a stale key never silently wins.
            argv += ["-o", "PubkeyAuthentication=no", "-o", "PreferredAuthentications=password"]
        else:
            argv += ["-o", "BatchMode=yes"]  # never block on a password/passphrase prompt
        argv += [
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", f"ConnectTimeout={int(min(self.timeout, _REACHABLE_TIMEOUT))}",
            "-p", str(self.port),
        ]
        if self.persist:
            argv += [
                "-o", "ControlMaster=auto",
                "-o", f"ControlPath={self._control_path}",
                "-o", "ControlPersist=30",
            ]
        if self.key and not self.password:
            argv += ["-i", self.key]
        argv.append(self.target)
        return argv

    def _askpass_helper(self) -> str:
        """Path to a fixed askpass script (created once, mode 0700).

        Contains no secret — it echoes ``$KVM_PILOT_SSH_ASKPASS_PW`` from the
        environment, which is set only for the ssh subprocess.
        """
        if self._askpass_path and os.path.exists(self._askpass_path):
            return self._askpass_path
        fd, path = tempfile.mkstemp(prefix="kvm-pilot-askpass-", suffix=".sh")
        with os.fdopen(fd, "w") as fh:
            fh.write("#!/bin/sh\nprintf '%s\\n' \"$" + self._ASKPASS_ENV + "\"\n")
        os.chmod(path, 0o700)  # nosec B103 - owner-only exec, no secret in the file
        self._askpass_path = path
        return path

    def _auth_run_kwargs(self) -> tuple[dict | None, dict]:
        """``(env, extra subprocess kwargs)`` for the current auth mode.

        Key mode inherits the environment untouched. Password mode wires up
        SSH_ASKPASS and detaches stdin + session so ssh takes the password from
        the helper rather than a tty prompt (and so never hangs).
        """
        if not self.password:
            return None, {}
        env = dict(os.environ)
        env["SSH_ASKPASS"] = self._askpass_helper()
        env["SSH_ASKPASS_REQUIRE"] = "force"   # OpenSSH >=8.4: use askpass even with a tty
        env.setdefault("DISPLAY", ":0")          # some builds still gate askpass on DISPLAY
        env[self._ASKPASS_ENV] = self.password
        return env, {"stdin": subprocess.DEVNULL, "start_new_session": True}

    def close(self) -> None:
        """Tear down a persistent ControlMaster and the askpass helper (best-effort).

        Safe to call always: a no-op when nothing was started.
        """
        if self._askpass_path:
            try:
                os.unlink(self._askpass_path)
            except OSError:
                pass
            self._askpass_path = None
        if not self.persist or shutil.which("ssh") is None:
            return
        try:
            subprocess.run(  # nosec B603 B607 - fixed args and no shell; system ssh from PATH is intentional
                ["ssh", "-O", "exit", "-o", f"ControlPath={self._control_path}", self.target],
                capture_output=True,
                timeout=5,
            )
        except (subprocess.SubprocessError, OSError):
            pass


class ApplianceChannel(SSHChannel):
    """SSH to the KVM appliance's OWN OS (root@<kvm-ip>), distinct from the target
    ``SSHChannel``. Adds read-only wedge diagnostics and a gated appliance reboot —
    the only path to observe/recover the RV1126 encoder wedge that the kvmd REST
    API cannot see (#162).

    Opt-in and key-based (no password / no sshpass — keeps kvmd-REST and
    appliance-root as separate trust domains). The host is the appliance itself
    (``cfg.host`` — the REST box), so no second address is configured. Recovery is
    operator-gated and must NEVER run in an autonomous loop: the wedge recurs
    within minutes and there is no out-of-band power to the appliance, so a reboot
    that fails to rejoin the network strands the operator with zero access.
    """

    # RV1126 hardware video-pipeline kernel threads. When the encoder wedges these
    # sit in D-state — but on GL firmware they park in D even when perfectly idle
    # (measured 2026-07-07: load self-inflates to ~= their count with zero
    # interaction), so their presence is CONTEXT, not a wedge tell on its own.
    _VIDEO_THREADS = frozenset(
        {"venc", "vpss", "vrga_0", "vvi_thread", "valloc", "ivs", "vsys", "vrgn", "vlog"}
    )

    @classmethod
    def from_config(
        cls,
        cfg: HostConfig,
        *,
        safety: SafetyPolicy | None = None,
        confirm=None,
        dry_run: bool = False,
    ) -> ApplianceChannel:
        """Build from a resolved ``HostConfig``; raises ``CapabilityError`` unless
        ``appliance_ssh`` is enabled. The host is inferred from ``cfg.host``."""
        if not cfg.appliance_ssh:
            raise CapabilityError(
                "Appliance-SSH is not enabled for this profile. Set appliance_ssh=true "
                "(or KVM_PILOT_APPLIANCE_SSH=1) and provide appliance_ssh_key — it SSHes "
                "to the KVM appliance's OWN OS (root@<kvm-ip>), key-based only."
            )
        if safety is None:
            safety = SafetyPolicy(dry_run=dry_run, confirm=confirm)
        return cls(
            cfg.host,  # the appliance IS the REST box — no separate address
            user=cfg.appliance_ssh_user,
            port=cfg.appliance_ssh_port,
            key=cfg.appliance_ssh_key,
            timeout=cfg.timeout,
            safety=safety,
        )

    # -- read-only diagnostics (ungated; FIXED constant commands only) ------

    def _readonly(self, command: str) -> str:
        """Run a FIXED read-only command, ungated (like ``ssh_reachable``). Returns
        stdout, or '' on any failure. NEVER pass user input here — the safety of
        skipping the guard rests on the command being a module constant."""
        if shutil.which("ssh") is None:
            return ""
        try:
            proc = subprocess.run(  # nosec B603 - fixed constant command, no shell
                self._ssh_argv() + [command],
                capture_output=True,
                text=True,
                timeout=min(self.timeout, _REACHABLE_TIMEOUT),
            )
        except (subprocess.SubprocessError, OSError):
            return ""
        return proc.stdout if proc.returncode == 0 else ""

    def loadavg(self) -> float | None:
        """1-minute load average, or None if unreadable. **Context only** — on
        these units it sits at ~(D-state thread count) even when perfectly idle,
        so it is NOT a standalone health signal (measured 2026-07-07)."""
        out = self._readonly("cat /proc/loadavg").split()
        try:
            return float(out[0]) if out else None
        except ValueError:
            return None

    def d_state_video_threads(self) -> list[str]:
        """RV1126 video-pipeline kernel threads currently in D-state. Context: on
        GL firmware these park in D even when healthy, so presence alone is not a
        wedge — pair with a functional signal (does the snapshot decode?)."""
        found = []
        for line in self._readonly("ps -eo stat,comm").splitlines():
            parts = line.split(None, 1)
            if len(parts) == 2 and parts[0].startswith("D") and parts[1].strip() in self._VIDEO_THREADS:
                found.append(parts[1].strip())
        return found

    # -- gated recovery -----------------------------------------------------

    def reboot(self) -> dict:
        """Reboot the KVM APPLIANCE (not the target) — the only recovery for the
        RV1126 encoder wedge (the wedged threads are unkillable kernel threads).
        Gated as ``appliance.reboot``. Target power is untouched (the HID/MSD
        gadget is bus-powered). Operator-gated; never autonomous."""
        if not self.safety.guard("appliance.reboot", f"Reboot the KVM appliance {self.host}"):
            return _result("reboot", returncode=None, stdout="", stderr="", dry_run=True)
        if shutil.which("ssh") is None:
            raise CapabilityError("The system 'ssh' binary was not found on PATH.")
        # Fire-and-forget: the reboot tears down our SSH session, so a normal exit
        # never returns — detach it and treat the dropped connection as success.
        try:
            subprocess.run(  # nosec B603 - fixed constant command, no shell
                self._ssh_argv() + ["nohup /sbin/reboot >/dev/null 2>&1 &"],
                capture_output=True,
                text=True,
                timeout=min(self.timeout, _REACHABLE_TIMEOUT),
            )
        except subprocess.TimeoutExpired:
            pass  # session torn down by the reboot — expected
        return _result("reboot", returncode=0, stdout="rebooting", stderr="", dry_run=False)


def _result(command, *, returncode, stdout, stderr, dry_run) -> dict:
    return {
        "command": command,
        "returncode": returncode,
        "stdout": stdout,
        "stderr": stderr,
        "ok": returncode == 0,
        "dry_run": dry_run,
    }


__all__ = [
    "SSHChannel",
    "ApplianceChannel",
    "DEFAULT_SSH_PORT",
    "MAX_SWEEP_HOSTS",
    "discover_ssh_hosts",
]
