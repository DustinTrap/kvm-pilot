"""Fixtures for the opt-in external-emulator integration tests.

These validate ``--driver redfish`` against an *independent* reference Redfish
implementation — DMTF-conformant sushy-tools (``sushy-emulator --fake``) — rather
than the project's own in-process emulator, so spec assumptions shared by our
driver and our mock can't hide a bug.

The fixture sources an emulator three ways, in priority order:
  1. ``KVM_PILOT_REDFISH_URL`` — an already-running emulator (e.g. a local
     ``docker run … quay.io/metal3-io/sushy-tools``).
  2. ``sushy-emulator`` on ``PATH`` — started here as a ``--fake`` subprocess on
     an ephemeral port (this is the CI path: ``pip install sushy-tools``).
  3. neither — the tests ``skip`` (so the default suite stays hermetic).

The ``--fake`` driver needs no libvirt/QEMU and no nested KVM, so it runs on a
stock GitHub-hosted runner.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
import urllib.request

import pytest


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_healthy(url: str, proc: subprocess.Popen | None, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc is not None and proc.poll() is not None:
            out = proc.stdout.read() if proc.stdout else ""
            raise RuntimeError(f"sushy-emulator exited early (rc={proc.returncode}):\n{out}")
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310 - localhost http
                if resp.status == 200:
                    return
        except OSError:
            pass
        time.sleep(0.5)  # back off on both a non-200 and a connection error
    raise RuntimeError(f"emulator at {url} did not become healthy within {timeout}s")


@pytest.fixture(scope="session")
def redfish_emulator_url() -> str:
    url = os.environ.get("KVM_PILOT_REDFISH_URL")
    if url:
        _wait_healthy(url.rstrip("/") + "/redfish/v1/", None)
        yield url.rstrip("/")
        return

    exe = shutil.which("sushy-emulator")
    if not exe:
        pytest.skip(
            "external Redfish emulator unavailable: set KVM_PILOT_REDFISH_URL or "
            "`pip install sushy-tools` so sushy-emulator is on PATH"
        )

    port = _free_port()
    proc = subprocess.Popen(  # noqa: S603 - fixed argv, trusted binary from PATH
        [exe, "--fake", "-i", "127.0.0.1", "-p", str(port)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        _wait_healthy(base + "/redfish/v1/", proc)
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture(scope="session")
def ipmi_bmc():
    """Connection params for an external IPMI BMC (OpenIPMI ``ipmi_sim`` or real).

    Env-driven (mirrors ``redfish_emulator_url``): point at an already-running
    ``ipmi_sim`` (or a real BMC) via ``KVM_PILOT_IPMI_HOST`` [+ ``_PORT`` /
    ``_USER`` / ``_PASSWD`` / ``_CIPHER``]. Skips when unset so the default suite
    stays hermetic (macOS has no ipmi_sim build).
    """
    host = os.environ.get("KVM_PILOT_IPMI_HOST")
    if not host:
        pytest.skip(
            "external IPMI BMC unavailable: set KVM_PILOT_IPMI_HOST (+ _PORT/_USER/"
            "_PASSWD/_CIPHER) to an ipmi_sim or real BMC"
        )
    cipher = os.environ.get("KVM_PILOT_IPMI_CIPHER")
    return {
        "host": host,
        "port": int(os.environ.get("KVM_PILOT_IPMI_PORT", "623")),
        "user": os.environ.get("KVM_PILOT_IPMI_USER", "admin"),
        "passwd": os.environ.get("KVM_PILOT_IPMI_PASSWD", "password"),
        "cipher": int(cipher) if cipher else None,
    }
