"""AMT IDE-R virtual-media tests, driven against a pure-stdlib fake ME.

Exercises the whole redirection stack end-to-end over a real loopback socket:
the StartRedirectionSession + HTTP-Digest handshake (:mod:`redir`), the IDE-R
session/framing (:mod:`ider`), and the ATAPI CD-ROM emulation — READ CAPACITY
and READ(10) served from a real ISO file. The fake ME *verifies the digest* the
client computes, so a wrong auth construction fails the test.

This is the emulator-level confidence behind the driver; live boot-from-ISO on
real AMT hardware is still unverified (#213/#217).
"""

from __future__ import annotations

import hashlib
import socket
import struct
import threading

import pytest

from kvm_pilot.drivers.amt import AmtDriver
from kvm_pilot.drivers.base import Capability

_USER, _PASS = "admin", "Secr3t!!"
_REALM, _NONCE, _QOP = "Digest:ABCDEF", "deadbeefcafe", "auth"


def _frame(cmd: int, seq: int, data: bytes = b"", *, completed: bool = False) -> bytes:
    attrs = 0x02 if (cmd > 50 and completed) else 0
    return bytes([cmd, 0, 0, attrs]) + struct.pack("<I", seq) + data


class _FakeAmtIder:
    """A one-connection fake ME: runs the handshake, enables the CD, then issues
    READ CAPACITY + READ(10) + a keep-alive ping and records the client's replies."""

    def __init__(self) -> None:
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(1)
        self.port = self._srv.getsockname()[1]
        self._seq = 0
        self.digest_ok = False
        self.capacity: bytes | None = None
        self.sector: bytes | None = None
        self.ponged = False
        self.done = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _send(self, conn, cmd, data=b"", *, completed=False):
        conn.sendall(_frame(cmd, self._seq, data, completed=completed))
        self._seq += 1

    def _recv_msg(self, conn) -> tuple[int, bytes]:
        """Read one length-known IDE-R message (cmd byte tells us its size)."""
        hdr = _recvn(conn, 8)
        cmd = hdr[0]
        if cmd == 0x40:            # OPEN_SESSION: 8 header + 10 payload
            return cmd, hdr + _recvn(conn, 10)
        if cmd == 0x48:            # ENABLE_FEATURES: 8 + 1 type + 4 value
            return cmd, hdr + _recvn(conn, 5)
        if cmd == 0x45:            # PONG
            return cmd, hdr
        if cmd == 0x54:            # DATA_TO_HOST: 8 hdr + 26 sub-header + payload
            sub = _recvn(conn, 26)
            dlen = sub[1] | (sub[2] << 8)
            return cmd, hdr + sub + _recvn(conn, dlen)
        if cmd == 0x51:            # SENSE / CommandEndResponse: 8 + 23
            return cmd, hdr + _recvn(conn, 23)
        raise AssertionError(f"unexpected client cmd 0x{cmd:02x}")

    def _run(self) -> None:
        try:
            conn, _ = self._srv.accept()
            conn.settimeout(10)
            self._handshake(conn)
            self._open_and_enable(conn)
            self._drive_scsi(conn)
            self.done.set()
        except Exception:  # noqa: BLE001 - test thread; failure shows as an unset event
            pass

    def _handshake(self, conn) -> None:
        assert _recvn(conn, 8) == b"\x10\x00\x00\x00IDER"                 # StartRedirectionSession
        conn.sendall(bytes([0x11, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]))  # reply, oem-len 0
        assert _recvn(conn, 9)[0] == 0x13                                 # auth query
        conn.sendall(bytes([0x14, 0, 0, 0, 0]) + struct.pack("<I", 3) + bytes([1, 3, 4]))  # offers digest
        # Client requests a challenge (type 4); we don't need its body, just its length.
        req = _recv_auth(conn)
        assert req[4] == 4
        chal = (bytes([len(_REALM)]) + _REALM.encode()
                + bytes([len(_NONCE)]) + _NONCE.encode()
                + bytes([len(_QOP)]) + _QOP.encode())
        conn.sendall(bytes([0x14, 1, 0, 0, 4]) + struct.pack("<I", len(chal)) + chal)  # status 1 = challenge
        resp = _recv_auth(conn)
        self._verify_digest(resp)
        conn.sendall(bytes([0x14, 0, 0, 0, 4]) + struct.pack("<I", 0))    # status 0 = success

    def _verify_digest(self, resp: bytes) -> None:
        # resp: [0x13,0,0,0,type][len LE16][00 00] then length-prefixed fields:
        # user, realm, nonce, uri, cnonce, nc, digest, qop
        fields, p = [], 9
        for _ in range(8):
            n = resp[p]
            fields.append(resp[p + 1:p + 1 + n].decode())
            p += 1 + n
        user, realm, nonce, uri, cnonce, nc, digest, qop = fields
        ha1 = hashlib.md5(f"{user}:{realm}:{_PASS}".encode()).hexdigest()  # nosec B324
        ha2 = hashlib.md5(f"POST:{uri}".encode()).hexdigest()             # nosec B324
        expect = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}".encode()).hexdigest()  # nosec B324
        self.digest_ok = (digest == expect and realm == _REALM and nonce == _NONCE)

    def _open_and_enable(self, conn) -> None:
        assert self._recv_msg(conn)[0] == 0x40                           # OPEN_SESSION
        reply = bytearray(30)
        reply[0] = 0x41
        struct.pack_into("<I", reply, 4, self._seq)
        self._seq += 1
        struct.pack_into("<H", reply, 16, 8192)                          # readbfr
        conn.sendall(bytes(reply))
        assert self._recv_msg(conn)[0] == 0x48                           # ENABLE_FEATURES
        self._send(conn, 0x49, bytes([2]) + struct.pack("<I", 0x02))     # REGS_STATUS: enabled

    def _drive_scsi(self, conn) -> None:
        # READ_CAPACITY (0x25): expect a DATA_TO_HOST with the last-block number.
        self._send(conn, 0x50, _scsi_cdb(bytes([0x25] + [0] * 11)))
        _cmd, msg = self._recv_msg(conn)
        self.capacity = msg[34:38]                                       # 4-byte BE last-block
        # READ_10 (0x28), LBA 0, 1 block: expect the ISO's first 2048-byte sector.
        cdb = bytes([0x28, 0]) + struct.pack(">I", 0) + bytes([0]) + struct.pack(">H", 1) + bytes([0])
        self._send(conn, 0x50, _scsi_cdb(cdb))
        _cmd, msg = self._recv_msg(conn)
        self.sector = msg[34:34 + 2048]
        # Keep-alive ping -> pong.
        self._send(conn, 0x44)
        cmd, _ = self._recv_msg(conn)
        self.ponged = (cmd == 0x45)

    def close(self) -> None:
        try:
            self._srv.close()
        except OSError:
            pass


def _scsi_cdb(cdb: bytes) -> bytes:
    """A 0x50 COMMAND_WRITTEN body: device-flags 0x10 (CD) at [14], cdb at [16:28]."""
    body = bytearray(20)
    body[6] = 0x10                # device_flags -> CD (byte 14 of the whole message = body[6])
    return bytes(body[:8]) + cdb.ljust(12, b"\x00")


def _recvn(conn, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = conn.recv(n - len(buf))
        if not chunk:
            raise AssertionError("connection closed early")
        buf += chunk
    return buf


def _recv_auth(conn) -> bytes:
    hdr = _recvn(conn, 4)                       # 0x13, 0, 0, 0
    rest = _recvn(conn, 5)                      # authType + LE32 length
    length = struct.unpack_from("<I", rest, 1)[0]
    return hdr + rest + _recvn(conn, length)


@pytest.fixture
def fake_ider():
    srv = _FakeAmtIder()
    yield srv
    srv.close()


def test_ider_capability_is_detected():
    drv = AmtDriver("127.0.0.1", _USER, _PASS, confirm=lambda *_: True)
    assert Capability.VIRTUAL_MEDIA in drv.capabilities()
    assert drv.get_msd_state()["connected"] is False


def test_ider_boots_a_cd_end_to_end(fake_ider, tmp_path):
    # A 4-sector ISO whose first sector is recognizable.
    iso = tmp_path / "boot.iso"
    first = bytes(range(256)) * 8            # 2048 distinctive bytes
    iso.write_bytes(first + b"\x5a" * (2048 * 3))

    drv = AmtDriver("127.0.0.1", _USER, _PASS, sol_port=fake_ider.port,
                    confirm=lambda *_: True, timeout=10)
    name = drv.mount_iso(str(iso))           # runs handshake + open + enable, then returns
    assert name == str(iso)
    assert drv.get_msd_state()["connected"] is True

    assert fake_ider.done.wait(10), "fake ME did not finish the SCSI script"
    assert fake_ider.digest_ok, "client's HTTP-Digest auth did not verify"
    # READ_CAPACITY returns last-block = blocks-1 = 3 (a 4-sector image).
    assert struct.unpack(">I", fake_ider.capacity)[0] == 3
    # READ_10 of LBA 0 returns the ISO's real first sector.
    assert fake_ider.sector == first
    assert fake_ider.ponged, "client did not answer the keep-alive ping"

    drv.msd_disconnect()
    assert drv.get_msd_state()["connected"] is False


def test_mount_iso_rejects_non_cdrom():
    drv = AmtDriver("127.0.0.1", _USER, _PASS, confirm=lambda *_: True)
    with pytest.raises(Exception, match="USB-R"):
        drv.mount_iso("/tmp/x.img", cdrom=False)  # nosec B108 - path never opened (rejected first)


def test_mount_iso_dry_run_does_not_connect():
    drv = AmtDriver("127.0.0.1", _USER, _PASS, dry_run=True)
    assert drv.mount_iso("/nonexistent.iso") == "/nonexistent.iso"  # gated out before any socket
    assert drv.get_msd_state()["connected"] is False


# -- ATAPI command handling (unit, no socket) -----------------------------

from kvm_pilot.drivers.amt.ider import IderSession  # noqa: E402


class _CaptureChannel:
    """A stand-in redirection channel that records outbound IDE-R frames."""

    def __init__(self):
        self.sent: list[bytes] = []

    def send(self, data: bytes) -> None:
        self.sent.append(data)

    def close(self) -> None:
        pass


def _capture_session(tmp_path, blocks: int = 4) -> IderSession:
    iso = tmp_path / "u.iso"
    iso.write_bytes(b"\xa5" * (2048 * blocks))
    s = IderSession("127.0.0.1", _USER, _PASS, str(iso))
    s._chan = _CaptureChannel()          # type: ignore[assignment]
    s._iso = open(iso, "rb")             # noqa: SIM115 - closed by the test's session
    s._cd_ready = True
    return s


@pytest.mark.parametrize("cdb, expect", [
    (bytes([0x00] + [0] * 11), 0x51),                                          # TEST_UNIT_READY
    (bytes([0x1a, 0, 0x3f, 0] + [0] * 8), 0x54),                               # MODE_SENSE_6 all
    (bytes([0x1a, 0, 0x01, 0] + [0] * 8), 0x51),                               # MODE_SENSE_6 other
    (bytes([0x1b] + [0] * 11), 0x51),                                          # START_STOP
    (bytes([0x1e] + [0] * 11), 0x51),                                          # ALLOW_MEDIUM_REMOVAL
    (bytes([0x23, 0, 0, 0, 0, 0, 0, 0, 0xfc, 0, 0, 0]), 0x54),                 # READ_FORMAT_CAPACITIES
    (bytes([0x25] + [0] * 11), 0x54),                                          # READ_CAPACITY
    (bytes([0x43, 0, 0, 0, 0, 0, 0, 0, 0xff, 0, 0, 0]), 0x54),                 # READ_TOC fmt0
    (bytes([0x43, 0x02, 0, 0, 0, 0, 0, 0, 0xff, 0, 0, 0]), 0x54),             # READ_TOC msf
    (bytes([0x43, 0, 0x01, 0, 0, 0, 0, 0, 0xff, 0, 0, 0]), 0x54),             # READ_TOC fmt1
    (bytes([0x46, 0, 0, 0, 0, 0, 0, 0, 0xff, 0, 0, 0]), 0x54),                 # GET_CONFIGURATION
    (bytes([0x46, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]), 0x54),                    # GET_CONFIGURATION buflen0
    (bytes([0x4a, 1, 0, 0, 0x10, 0, 0, 0, 0x08, 0, 0, 0]), 0x54),             # GET_EVENT_STATUS ok
    (bytes([0x4a, 0, 0, 0, 0, 0, 0, 0, 0x08, 0, 0, 0]), 0x51),                # GET_EVENT_STATUS bad
    (bytes([0x51] + [0] * 11), 0x51),                                          # READ_DISC_INFO
    (bytes([0x5a, 0, 0x3f, 0, 0, 0, 0, 0, 0xff, 0, 0, 0]), 0x54),             # MODE_SENSE_10 all
    (bytes([0x5a, 0, 0x01, 0, 0, 0, 0, 0, 0xff, 0, 0, 0]), 0x54),             # MODE_SENSE_10 err-recov
    (bytes([0x5a, 0, 0x08, 0, 0, 0, 0, 0, 0xff, 0, 0, 0]), 0x51),             # MODE_SENSE_10 other
    (bytes([0xff] + [0] * 11), 0x51),                                          # unsupported -> illegal
])
def test_atapi_command_responses(tmp_path, cdb, expect):
    s = _capture_session(tmp_path)
    try:
        s._handle_scsi(cdb, feature=0, device_flags=0x10)
        assert s._chan.sent, "no response frame"
        assert s._chan.sent[-1][0] == expect
    finally:
        s._iso.close()


def test_read10_serves_data_and_rejects_out_of_range(tmp_path):
    s = _capture_session(tmp_path, blocks=4)
    try:
        # In range: READ_10 LBA 0, 2 blocks -> DATA_TO_HOST frame(s).
        s._handle_scsi(bytes([0x28, 0]) + struct.pack(">I", 0) + bytes([0])
                       + struct.pack(">H", 2) + bytes([0]), 0, 0x10)
        assert s._chan.sent[-1][0] == 0x54
        # READ_6 also serves data.
        s._chan.sent.clear()
        s._handle_scsi(bytes([0x08, 0, 0, 0, 1, 0]) + bytes(6), 0, 0x10)
        assert s._chan.sent[-1][0] == 0x54
        # Past the end -> illegal-block sense, no data.
        s._chan.sent.clear()
        s._handle_scsi(bytes([0x28, 0]) + struct.pack(">I", 100) + bytes([0])
                       + struct.pack(">H", 1) + bytes([0]), 0, 0x10)
        assert s._chan.sent[-1][0] == 0x51
    finally:
        s._iso.close()


def test_dispatch_control_messages(tmp_path):
    s = _capture_session(tmp_path)
    try:
        s._acc = _frame(0x44, 0)                       # PING
        assert s._dispatch() == 8
        assert s._chan.sent[-1][0] == 0x45             # -> PONG
        s._acc = _frame(0x46, 0, b"\x00")              # RESET_OCCURRED
        assert s._dispatch() == 9
        assert s._chan.sent[-1][0] == 0x47             # -> RESET_REPLY
        s._acc = _frame(0x4b, 0)                       # HEARTBEAT (no reply)
        assert s._dispatch() == 8
        s._acc = _frame(0x49, 0, bytes([2]) + struct.pack("<I", 0x02))  # REGS_STATUS enabled
        assert s._dispatch() == 13
        assert s._enabled.is_set()
        s._acc = _frame(0x43, 0)                       # CLOSE
        assert s._dispatch() == 8
        assert s._stop.is_set()
    finally:
        s._iso.close()
