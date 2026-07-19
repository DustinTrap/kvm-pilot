"""Intel AMT IDE-R (IDE Redirect) virtual media — boot a host from a local ISO.

IDE-R presents a local disk image to a managed vPro host as a virtual ATAPI
CD-ROM, so the host can boot from it over the network with no physical media.
It runs on the redirection channel (:mod:`.redir`, port 16994/16995) after the
shared session/auth handshake.

The ME drives the conversation: after we open an IDE-R session (0x40) and enable
the CD device (0x48), the host's BIOS issues ATAPI/SCSI commands (0x50) that we
answer — TEST UNIT READY, READ CAPACITY, READ(6/10/12), MODE SENSE, GET
CONFIGURATION, READ TOC, GET EVENT STATUS — serving 2048-byte sectors from the
ISO. A background thread holds the session open (answering keep-alive pings)
while the host boots.

The framing, command codes, and the ATAPI response templates are ported from
MeshCommander's ``amt-ider.js`` (the maintained reference for AMT 11–16; the
legacy ``amtider`` tool speaks an older revision AMT 14 rejects — #213).
stdlib-only: ``socket`` + ``struct`` + ``threading``.

NOTE (maturity): emulator-tested only. Live boot-from-ISO on real AMT hardware
is unverified pending #217 recovery — treat as experimental.
"""

from __future__ import annotations

import logging
import os
import struct
import threading
from typing import BinaryIO

from ...errors import CapabilityError, KVMPilotError
from .redir import START_IDER, RedirectionChannel

logger = logging.getLogger("kvm_pilot.drivers.amt")

# IDE-R command bytes (both directions) over the redirection channel.
_OPEN = 0x40
_OPEN_REPLY = 0x41
_CLOSE = 0x43
_PING = 0x44
_PONG = 0x45
_RESET = 0x46
_RESET_REPLY = 0x47
_ENABLE_FEATURES = 0x48
_STATUS_DATA = 0x49
_ERROR = 0x4A
_HEARTBEAT = 0x4B
_COMMAND = 0x50          # host issued a SCSI command (CDB)
_SENSE = 0x51            # our CommandEndResponse
_GET_DATA = 0x52         # request write-data from host (floppy only; unused)
_DATA_FROM_HOST = 0x53
_DATA_TO_HOST = 0x54     # our read-data reply

_DEV_CD = 0xB0           # ATAPI device selector for the virtual CD-ROM
_CD_BLOCK = 2048         # CD-ROM logical block size

# ATAPI response templates (verbatim from meshcmd amt-ider.js).
_CFG_HEADER = bytes([0x00, 0x00, 0x00, 0x28, 0x00, 0x00, 0x00, 0x08])
_CFG_PROFILE_LIST = bytes([0x00, 0x00, 0x03, 0x04, 0x00, 0x08, 0x01, 0x00])
_CFG_CORE = bytes([0x00, 0x01, 0x03, 0x04, 0x00, 0x00, 0x00, 0x02])
_CFG_MORPHING = bytes([0x00, 0x02, 0x03, 0x04, 0x00, 0x00, 0x00, 0x00])
_CFG_REMOVABLE = bytes([0x00, 0x03, 0x03, 0x04, 0x29, 0x00, 0x00, 0x02])
_CFG_RANDOM = bytes([0x00, 0x10, 0x01, 0x08, 0x00, 0x00, 0x08, 0x00, 0x00, 0x01, 0x00, 0x00])
_CFG_READ = bytes([0x00, 0x1E, 0x03, 0x00])
_CFG_POWER = bytes([0x01, 0x00, 0x03, 0x00])
_CFG_TIMEOUT = bytes([0x01, 0x05, 0x03, 0x00])
_MODESENSE_3F_CD = bytes([
    0x00, 0x28, 0x01, 0x80, 0x00, 0x00, 0x00, 0x00, 0x01, 0x06, 0x00, 0xff, 0x00, 0x00,
    0x00, 0x00, 0x2a, 0x18, 0x00, 0x00, 0x00, 0x00, 0x02, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x80, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
_MODESENSE_CD_ERR_RECOVERY = bytes([
    0x00, 0x0E, 0x01, 0x80, 0x00, 0x00, 0x00, 0x00, 0x01, 0x06, 0x00, 0xFF, 0x00, 0x00, 0x00, 0x00])


def _u16be(buf: bytes, p: int) -> int:
    return (buf[p] << 8) | buf[p + 1]


def _u32be(buf: bytes, p: int) -> int:
    return (buf[p] << 24) | (buf[p + 1] << 16) | (buf[p + 2] << 8) | buf[p + 3]


class IderSession:
    """A live IDE-R session serving one read-only ISO as a virtual CD-ROM.

    :meth:`start` runs the handshake, opens the IDE-R session, enables the CD,
    and spins a daemon thread that answers the host's ATAPI commands until
    :meth:`stop`. The image is attached so the host boots from it on its next
    reset (pair with ``boot-device cd`` + a power cycle).
    """

    def __init__(
        self, host: str, user: str, passwd: str, iso_path: str, *,
        port: int = 16994, tls: bool = False, verify_ssl: bool = False,
        ssl_ca_file: str | None = None, timeout: float = 30.0,
    ):
        if not os.path.isfile(iso_path):
            raise CapabilityError(f"IDE-R image not found: {iso_path}")
        self.host = host
        self.iso_path = iso_path
        self._iso_size = os.path.getsize(iso_path)
        self._blocks = self._iso_size // _CD_BLOCK          # 2048-byte CD blocks
        self._chan = RedirectionChannel(
            host, user, passwd, port=port, tls=tls, verify_ssl=verify_ssl,
            ssl_ca_file=ssl_ca_file, timeout=timeout)
        self._iso: BinaryIO | None = None  # opened lazily on start
        self._out_seq = 0
        self._in_seq = 0
        self._acc = b""
        self._readbfr = 8192
        self._cd_ready = False
        self._enabled = threading.Event()
        self._stop = threading.Event()
        self._error: BaseException | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle ------------------------------------------------------

    def start(self, ready_timeout: float = 20.0) -> None:
        """Open the session and start serving; block until the CD is enabled."""
        self._iso = open(self.iso_path, "rb")  # noqa: SIM115 - held for the session lifetime
        self._chan.open(START_IDER)
        # OPEN_SESSION: rx/tx/heartbeat timeouts (LE16) + version (LE32).
        self._send(_OPEN, struct.pack("<HHHI", 30000, 0, 20000, 1))
        self._thread = threading.Thread(target=self._loop, name=f"ider-{self.host}", daemon=True)
        self._thread.start()
        if not self._enabled.wait(ready_timeout):
            self.stop()
            raise KVMPilotError(
                f"IDE-R session to {self.host} did not enable within {ready_timeout}s"
                + (f" ({self._error})" if self._error else ""))
        if self._error:
            self.stop()
            raise self._error

    def stop(self) -> None:
        self._stop.set()
        self._chan.close()
        if self._iso is not None:
            try:
                self._iso.close()
            finally:
                self._iso = None

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive() and not self._stop.is_set()

    # -- serving loop ---------------------------------------------------

    def _loop(self) -> None:
        try:
            while not self._stop.is_set():
                data = self._chan.recv()
                if not data:
                    break
                self._acc += data
                while self._acc:
                    consumed = self._dispatch()
                    if consumed == 0:
                        break  # need more bytes
                    if self._in_seq != struct.unpack_from("<I", self._acc, 4)[0]:
                        raise KVMPilotError(f"IDE-R out-of-sequence from {self.host}")
                    self._in_seq += 1
                    self._acc = self._acc[consumed:]
        except BaseException as e:  # noqa: BLE001 - surface to start()/callers via _error
            if not self._stop.is_set():
                self._error = e
                logger.debug("IDE-R loop error for %s: %s", self.host, e)
        finally:
            self._stop.set()

    def _dispatch(self) -> int:
        """Parse one IDE-R message from the head of ``_acc``; 0 = need more bytes."""
        if len(self._acc) < 8:
            return 0
        cmd = self._acc[0]
        if cmd == _OPEN_REPLY:
            if len(self._acc) < 30:
                return 0
            extra = self._acc[29]
            if len(self._acc) < 30 + extra:
                return 0
            self._readbfr = min(_u16le(self._acc, 16), 8192)
            self._send_enable_cd()
            return 30 + extra
        if cmd == _CLOSE:
            self._stop.set()
            return 8
        if cmd == _PING:
            self._send(_PONG)
            return 8
        if cmd in (_PONG, _HEARTBEAT):
            return 8
        if cmd == _RESET:
            if len(self._acc) < 9:
                return 0
            self._send(_RESET_REPLY)
            return 9
        if cmd == _STATUS_DATA:
            if len(self._acc) < 13:
                return 0
            stype = self._acc[8]
            value = _u32le(self._acc, 9)
            if stype == 1 and (value & 1):      # REGS_AVAIL — (re)enable
                self._send_enable_cd()
            elif stype == 2:                     # REGS_STATUS — enabled bit
                if value & 2:
                    self._enabled.set()
            return 13
        if cmd == _ERROR:
            if len(self._acc) < 11:
                return 0
            return 11
        if cmd == _COMMAND:
            if len(self._acc) < 28:
                return 0
            device_flags = self._acc[14]
            feature = self._acc[9]
            cdb = self._acc[16:28]
            self._handle_scsi(cdb, feature, device_flags)
            return 28
        if cmd == _DATA_FROM_HOST:
            if len(self._acc) < 14:
                return 0
            length = _u16le(self._acc, 9)
            if len(self._acc) < 14 + length:
                return 0
            # Read-only media: reject writes with a write-protect sense.
            self._send(_SENSE, bytes([0] * 12 + [0x87, 0x70, 0x03, 0, 0, 0, 0xA0, 0x51, 0x07, 0x27, 0x00]),
                       completed=True)
            return 14 + length
        raise KVMPilotError(f"IDE-R: unknown command 0x{cmd:02x} from {self.host}")

    # -- outbound framing ----------------------------------------------

    def _send(self, cmd: int, data: bytes = b"", *, completed: bool = False, dma: bool = False) -> None:
        attrs = (0x02 if (cmd > 50 and completed) else 0) | (0x01 if dma else 0)
        header = bytes([cmd, 0, 0, attrs]) + struct.pack("<I", self._out_seq)
        self._out_seq += 1
        self._chan.send(header + data)

    def _send_enable_cd(self) -> None:
        # DisableEnableFeatures type 3 (REGS_TOGGLE), value 0x01|0x08 = CD, on-reboot.
        self._send(_ENABLE_FEATURES, bytes([3]) + struct.pack("<I", 0x01 + 0x08))

    def _end_response(self, error: int, sense: int, device: int, asc: int = 0, asq: int = 0) -> None:
        if error:
            body = bytes([0] * 12 + [0xc5, 0, 3, 0, 0, 0, device, 0x50, 0, 0, 0])
        else:
            body = bytes([0] * 12 + [0x87, (sense << 4) & 0xff, 3, 0, 0, 0, device, 0x51, sense, asc, asq])
        self._send(_SENSE, body, completed=True)

    def _data_to_host(self, device: int, data: bytes, dma: int) -> None:
        dmalen = 0 if dma else len(data)
        head = bytes([0, len(data) & 0xff, (len(data) >> 8) & 0xff, 0, 0xb4 if dma else 0xb5, 0, 2, 0,
                      dmalen & 0xff, (dmalen >> 8) & 0xff, device, 0x58, 0x85, 0, 3, 0, 0, 0, device,
                      0x50, 0, 0, 0, 0, 0, 0])
        self._send(_DATA_TO_HOST, head + data, completed=True, dma=bool(dma))

    # -- ATAPI command handling (CD-ROM only) --------------------------

    def _handle_scsi(self, cdb: bytes, feature: int, device_flags: int) -> None:
        dev = _DEV_CD
        dma = feature & 1
        op = cdb[0]
        if op == 0x00:                                   # TEST_UNIT_READY
            if not self._cd_ready:
                self._cd_ready = True
                self._end_response(1, 0x06, dev, 0x28, 0x00)  # unit attention: media changed
                return
            self._end_response(1, 0x00, dev, 0x00, 0x00)
        elif op in (0x08, 0x28, 0xa8):                   # READ_6 / READ_10 / READ_12
            if op == 0x08:
                lba = ((cdb[1] & 0x1f) << 16) + (cdb[2] << 8) + cdb[3]
                length = cdb[4] or 256
            elif op == 0x28:
                lba, length = _u32be(cdb, 2), _u16be(cdb, 7)
            else:
                lba, length = _u32be(cdb, 2), _u32be(cdb, 6)
            self._send_disk_data(dev, lba, length, dma)
        elif op == 0x1a:                                 # MODE_SENSE_6
            if cdb[2] == 0x3f and cdb[3] == 0x00:
                self._data_to_host(dev, bytes([0, 0x05, 0x80, 0]), dma)
            else:
                self._end_response(1, 0x05, dev, 0x24, 0x00)
        elif op == 0x1b:                                 # START_STOP_UNIT (eject)
            self._end_response(1, 0, dev)
        elif op == 0x1e:                                 # ALLOW_MEDIUM_REMOVAL
            self._end_response(1, 0x00, dev, 0x00, 0x00)
        elif op == 0x23:                                 # READ_FORMAT_CAPACITIES
            self._data_to_host(dev, struct.pack(">I", 8) + bytes([0, 0, 0x0b, 0x40, 0x02, 0, 0x02, 0]), dma)
        elif op == 0x25:                                 # READ_CAPACITY
            last = max(self._blocks - 1, 0)
            self._data_to_host(device_flags, struct.pack(">I", last) + bytes([0, 0, 0x08, 0]), dma)
        elif op == 0x43:                                 # READ_TOC
            msf = cdb[1] & 0x02
            fmt = cdb[2] & 0x07 or (cdb[9] >> 6)
            if fmt == 1:
                self._data_to_host(dev, bytes([0, 0x0a, 0x01, 0x01, 0, 0x14, 0x01, 0, 0, 0, 0, 0]), dma)
            elif fmt == 0:
                tail = [0x02, 0, 0, 0x14, 0xaa, 0, 0, 0, 0x34, 0x13] if msf else \
                       [0x00, 0, 0, 0x14, 0xaa, 0, 0, 0, 0x00, 0x00]
                self._data_to_host(dev, bytes([0, 0x12, 0x01, 0x01, 0, 0x14, 0x01, 0, 0, 0] + tail), dma)
        elif op == 0x46:                                 # GET_CONFIGURATION
            self._get_configuration(cdb, dev, dma)
        elif op == 0x4a:                                 # GET_EVENT_STATUS_NOTIFICATION
            if cdb[1] != 0x01 and cdb[4] != 0x10:
                self._end_response(1, 0x05, dev, 0x26, 0x01)
            else:
                self._data_to_host(dev, bytes([0x00, 0x02, 0x80, 0x00]), dma)  # media present
        elif op == 0x51:                                 # READ_DISC_INFORMATION
            self._end_response(0, 0x05, dev, 0x20, 0x00)
        elif op == 0x5a:                                 # MODE_SENSE_10
            page = cdb[2] & 0x3f
            if page in (0x01, 0x3f):
                r = _MODESENSE_CD_ERR_RECOVERY if page == 0x01 else _MODESENSE_3F_CD
                self._data_to_host(dev, r, dma)
            else:
                self._end_response(1, 0x05, dev, 0x24, 0x00)
        else:                                            # unsupported → illegal request
            self._end_response(0, 0x05, dev, 0x20, 0x00)

    def _get_configuration(self, cdb: bytes, dev: int, dma: int) -> None:
        send_all = cdb[1] != 2
        first = _u16be(cdb, 2)
        buflen = _u16be(cdb, 7)
        if buflen == 0:
            self._data_to_host(dev, struct.pack(">I", 0x003c) + struct.pack(">I", 0x0008), dma)
            return
        r = None
        for code, feat in ((0x000, _CFG_PROFILE_LIST), (0x001, _CFG_CORE), (0x002, _CFG_MORPHING),
                           (0x003, _CFG_REMOVABLE), (0x010, _CFG_RANDOM), (0x01E, _CFG_READ),
                           (0x100, _CFG_POWER), (0x105, _CFG_TIMEOUT)):
            if first == code or (send_all and first < code):
                r = feat
        if r is None:
            payload = struct.pack(">I", 0x0008) + struct.pack(">I", 4)
        else:
            payload = struct.pack(">I", 0x0008) + struct.pack(">I", len(r) + 4) + r
        self._data_to_host(dev, payload[:buflen], dma)

    def _send_disk_data(self, dev: int, lba: int, length: int, dma: int) -> None:
        if length < 0 or lba + length > self._blocks:
            self._end_response(1, 0x05, dev, 0x21, 0x00)   # LBA out of range
            return
        if length == 0:
            self._end_response(1, 0x00, dev, 0x00, 0x00)
            return
        assert self._iso is not None
        offset = lba * _CD_BLOCK
        remaining = length * _CD_BLOCK
        self._iso.seek(offset)
        while remaining > 0:
            chunk = self._iso.read(min(remaining, self._readbfr))
            if not chunk:
                break
            remaining -= len(chunk)
            self._data_to_host(dev, chunk, dma)


def _u16le(buf: bytes, p: int) -> int:
    return struct.unpack_from("<H", buf, p)[0]


def _u32le(buf: bytes, p: int) -> int:
    return struct.unpack_from("<I", buf, p)[0]
