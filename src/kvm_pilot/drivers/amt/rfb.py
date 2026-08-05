"""AMT KVM Redirection over RFB/VNC — firmware-level screenshot + HID.

This is the capability that makes AMT worth having: because the ME renders the
platform's real framebuffer *below* the OS, an RFB snapshot captures **BIOS,
POST, and the bootloader** — exactly what an HDMI-capture KVM on a laptop cannot
see. It also carries keyboard/mouse, so the whole pre-boot surface is drivable.

Scope: standard-port KVM redirection (TCP 5900) with **VNC Authentication**
(RFB security type 2). That needs single-block DES, which the stdlib lacks, so a
compact, FIPS-vector-tested DES lives here — keeping the driver dependency-free.

AMT's KVM server is Intel's **RFB 4.0** (it announces ``RFB 004.000``), which is
3.8-compatible for framebuffer *only if the client cooperates* — the hard-won
lessons, matching MeshCommander's decoder, are baked in here:

  * **Reply ``RFB 003.008``** (downgrade) — do NOT echo 004.000.
  * The framebuffer is **16-bpp RGB565**; the client must **not** send a
    ``SetPixelFormat`` (AMT resets on 32-bpp) — we keep the native format.
  * ``SetEncodings`` must **explicitly list RAW** (AMT doesn't assume it) plus
    the DesktopSize pseudo-encoding; the screen arrives as ≤64×64 RAW tiles.

Prerequisites on the target: KVM Redirection enabled, standard-port (5900) on, an
**exactly-8-char** RFB password set, and (for unattended capture) user-consent
off. The driver's ``enable_kvm()`` sets all of these over WS-Man. Note Intel
dropped 5900 at AMT ≥12 on some SKUs; where it isn't served, use SOL instead.

**Live-validated** against a Dell Latitude 5411 (AMT 14.1.67): a full
1920×1080 BIOS/POST screenshot decoded correctly. One honest caveat learned
there — AMT captures *graphical* framebuffers (BIOS / POST / GRUB / a GUI) but
**not legacy VGA text mode**: it resets right after the framebuffer request
rather than delivering a frame. A reset at that exact point means "unsupported
display mode," not a bug here.
"""

from __future__ import annotations

import socket
import struct
import time
import zlib
from collections.abc import Callable, Sequence
from typing import Any

from ...errors import AuthError, ConnectionError, KVMPilotError, ProtocolError

# --------------------------------------------------------------------------- #
# DES (single 64-bit block, ECB) — only what VNC authentication needs.        #
# Tables are the FIPS 46-3 standard; verified against the published test      #
# vector in tests (key 0123456789ABCDEF, pt 4E6F772069732074 -> 3FA40E8A984D4815). #
# --------------------------------------------------------------------------- #

_IP = [58,50,42,34,26,18,10,2,60,52,44,36,28,20,12,4,62,54,46,38,30,22,14,6,64,56,48,40,32,24,16,8,
       57,49,41,33,25,17,9,1,59,51,43,35,27,19,11,3,61,53,45,37,29,21,13,5,63,55,47,39,31,23,15,7]
_FP = [40,8,48,16,56,24,64,32,39,7,47,15,55,23,63,31,38,6,46,14,54,22,62,30,37,5,45,13,53,21,61,29,
       36,4,44,12,52,20,60,28,35,3,43,11,51,19,59,27,34,2,42,10,50,18,58,26,33,1,41,9,49,17,57,25]
_E = [32,1,2,3,4,5,4,5,6,7,8,9,8,9,10,11,12,13,12,13,14,15,16,17,16,17,18,19,20,21,20,21,
      22,23,24,25,24,25,26,27,28,29,28,29,30,31,32,1]
_P = [16,7,20,21,29,12,28,17,1,15,23,26,5,18,31,10,2,8,24,14,32,27,3,9,19,13,30,6,22,11,4,25]
_PC1 = [57,49,41,33,25,17,9,1,58,50,42,34,26,18,10,2,59,51,43,35,27,19,11,3,60,52,44,36,
        63,55,47,39,31,23,15,7,62,54,46,38,30,22,14,6,61,53,45,37,29,21,13,5,28,20,12,4]
_PC2 = [14,17,11,24,1,5,3,28,15,6,21,10,23,19,12,4,26,8,16,7,27,20,13,2,
        41,52,31,37,47,55,30,40,51,45,33,48,44,49,39,56,34,53,46,42,50,36,29,32]
_SHIFTS = [1,1,2,2,2,2,2,2,1,2,2,2,2,2,2,1]
_SBOX = [
 [14,4,13,1,2,15,11,8,3,10,6,12,5,9,0,7,0,15,7,4,14,2,13,1,10,6,12,11,9,5,3,8,
  4,1,14,8,13,6,2,11,15,12,9,7,3,10,5,0,15,12,8,2,4,9,1,7,5,11,3,14,10,0,6,13],
 [15,1,8,14,6,11,3,4,9,7,2,13,12,0,5,10,3,13,4,7,15,2,8,14,12,0,1,10,6,9,11,5,
  0,14,7,11,10,4,13,1,5,8,12,6,9,3,2,15,13,8,10,1,3,15,4,2,11,6,7,12,0,5,14,9],
 [10,0,9,14,6,3,15,5,1,13,12,7,11,4,2,8,13,7,0,9,3,4,6,10,2,8,5,14,12,11,15,1,
  13,6,4,9,8,15,3,0,11,1,2,12,5,10,14,7,1,10,13,0,6,9,8,7,4,15,14,3,11,5,2,12],
 [7,13,14,3,0,6,9,10,1,2,8,5,11,12,4,15,13,8,11,5,6,15,0,3,4,7,2,12,1,10,14,9,
  10,6,9,0,12,11,7,13,15,1,3,14,5,2,8,4,3,15,0,6,10,1,13,8,9,4,5,11,12,7,2,14],
 [2,12,4,1,7,10,11,6,8,5,3,15,13,0,14,9,14,11,2,12,4,7,13,1,5,0,15,10,3,9,8,6,
  4,2,1,11,10,13,7,8,15,9,12,5,6,3,0,14,11,8,12,7,1,14,2,13,6,15,0,9,10,4,5,3],
 [12,1,10,15,9,2,6,8,0,13,3,4,14,7,5,11,10,15,4,2,7,12,9,5,6,1,13,14,0,11,3,8,
  9,14,15,5,2,8,12,3,7,0,4,10,1,13,11,6,4,3,2,12,9,5,15,10,11,14,1,7,6,0,8,13],
 [4,11,2,14,15,0,8,13,3,12,9,7,5,10,6,1,13,0,11,7,4,9,1,10,14,3,5,12,2,15,8,6,
  1,4,11,13,12,3,7,14,10,15,6,8,0,5,9,2,6,11,13,8,1,4,10,7,9,5,0,15,14,2,3,12],
 [13,2,8,4,6,15,11,1,10,9,3,14,5,0,12,7,1,15,13,8,10,3,7,4,12,5,6,11,0,14,9,2,
  7,11,4,1,9,12,14,2,0,6,10,13,15,3,5,8,2,1,14,7,4,10,8,13,15,12,9,0,3,5,6,11],
]


def _bits(data: bytes) -> list[int]:
    out: list[int] = []
    for byte in data:
        out.extend((byte >> (7 - i)) & 1 for i in range(8))
    return out


def _tobytes(bits: list[int]) -> bytes:
    return bytes(
        sum(bits[i + j] << (7 - j) for j in range(8)) for i in range(0, len(bits), 8)
    )


def _perm(bits: list[int], table: list[int]) -> list[int]:
    return [bits[i - 1] for i in table]


def _keys(key8: bytes) -> list[list[int]]:
    k = _perm(_bits(key8), _PC1)
    c, d = k[:28], k[28:]
    subs = []
    for s in _SHIFTS:
        c = c[s:] + c[:s]
        d = d[s:] + d[:s]
        subs.append(_perm(c + d, _PC2))
    return subs


def _f(r: list[int], k: list[int]) -> list[int]:
    x = [a ^ b for a, b in zip(_perm(r, _E), k, strict=False)]
    out: list[int] = []
    for i in range(8):
        b = x[i * 6:i * 6 + 6]
        row = (b[0] << 1) | b[5]
        col = (b[1] << 3) | (b[2] << 2) | (b[3] << 1) | b[4]
        val = _SBOX[i][row * 16 + col]
        out.extend((val >> (3 - j)) & 1 for j in range(4))
    return _perm(out, _P)


def des_encrypt_block(key8: bytes, block8: bytes) -> bytes:
    """DES-ECB encrypt one 8-byte block under an 8-byte key."""
    subs = _keys(key8)
    bits = _perm(_bits(block8), _IP)
    left, right = bits[:32], bits[32:]
    for k in subs:
        left, right = right, [a ^ b for a, b in zip(left, _f(right, k), strict=False)]
    return _tobytes(_perm(right + left, _FP))


def vnc_auth_response(password: str, challenge: bytes) -> bytes:
    """The 16-byte VNC-auth response: DES-encrypt the 16-byte challenge (two ECB
    blocks) with the password as key — VNC mirrors each key byte's bits (LSB<->MSB),
    truncated/zero-padded to 8 bytes."""
    raw = password.encode("latin-1", "replace")[:8].ljust(8, b"\x00")
    key = bytes(int(f"{b:08b}"[::-1], 2) for b in raw)
    return des_encrypt_block(key, challenge[:8]) + des_encrypt_block(key, challenge[8:16])


# --------------------------------------------------------------------------- #
# Minimal PNG writer (RGBA/8, no external deps).                              #
# --------------------------------------------------------------------------- #


def encode_png(width: int, height: int, rgba: bytes) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    raw = bytearray()
    stride = width * 4
    for y in range(height):
        raw.append(0)  # filter: none
        raw.extend(rgba[y * stride:(y + 1) * stride])
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + chunk(b"IEND", b"")
    )


# --------------------------------------------------------------------------- #
# HID keysyms (X11) for type_text / press_key / send_shortcut.                #
# --------------------------------------------------------------------------- #

_KEYSYM = {
    "enter": 0xFF0D, "return": 0xFF0D, "escape": 0xFF1B, "esc": 0xFF1B,
    "tab": 0xFF09, "backspace": 0xFF08, "delete": 0xFFFF, "space": 0x0020,
    "up": 0xFF52, "down": 0xFF54, "left": 0xFF51, "right": 0xFF53,
    "home": 0xFF50, "end": 0xFF57, "pageup": 0xFF55, "pagedown": 0xFF56,
    "insert": 0xFF63,
    "controlleft": 0xFFE3, "controlright": 0xFFE4, "control": 0xFFE3, "ctrl": 0xFFE3,
    "altleft": 0xFFE9, "altright": 0xFFEA, "alt": 0xFFE9,
    "shiftleft": 0xFFE1, "shiftright": 0xFFE2, "shift": 0xFFE1,
    "metaleft": 0xFFEB, "metaright": 0xFFEC, "meta": 0xFFEB, "super": 0xFFEB,
    **{f"f{n}": 0xFFBD + n for n in range(1, 13)},  # F1..F12 = 0xFFBE..0xFFC9
}


def key_to_keysym(name: str) -> int:
    """Map a kvmd-style key name (``Enter``, ``F2``, ``KeyA``, ``Digit1``) or a
    single character to an X11 keysym."""
    n = name.strip()
    low = n.casefold()
    if low in _KEYSYM:
        return _KEYSYM[low]
    if low.startswith("key") and len(n) == 4:  # KeyA..KeyZ
        return ord(n[3].lower())
    if low.startswith("digit") and len(n) == 6:  # Digit0..Digit9
        return ord(n[5])
    if len(n) == 1:  # a literal character
        return ord(n)
    raise KVMPilotError(f"AMT RFB: unknown key {name!r}")


# --------------------------------------------------------------------------- #
# RFB client.                                                                 #
# --------------------------------------------------------------------------- #

# AMT KVM encodings: RAW pixels, RLE(16) (a ZRLE-style zlib+tile scheme), and the
# DesktopSize pseudo-encoding. We advertise RLE because integrated/hybrid-GPU
# platforms refuse RAW and reset unless RLE is offered (MeshCentral's "try RLE8"
# case) — but RAW is decoded too, for SKUs that do send it.
_ENC_RAW = 0
_ENC_RLE = 16

# Redirection-session message types, shared with IDE-R (see ider.py): the KVM
# tunnel opens with the same OPEN/OPEN_REPLY pair before RFB takes over (#245).
_REDIR_OPEN = 0x40
_REDIR_OPEN_REPLY = 0x41
_ENC_DESKTOP_SIZE = -223  # 0xFFFFFF21


def _build_rgb565_lut() -> bytes:
    """RGB565 little-endian uint16 -> RGB888, precomputed once. The 5/6/5 bits are
    bit-replicated into the low bits so full-scale maps to 255 (not 248/252)."""
    lut = bytearray(65536 * 3)
    for v in range(65536):
        r = (v >> 8) & 0xF8
        g = (v >> 3) & 0xFC
        b = (v & 0x1F) << 3
        lut[v * 3] = r | (r >> 5)
        lut[v * 3 + 1] = g | (g >> 6)
        lut[v * 3 + 2] = b | (b >> 5)
    return bytes(lut)


_RGB565_LUT = _build_rgb565_lut()


def _decode_zrle_tile(u: bytes, w: int, h: int) -> list[int]:
    """Decode one AMT RLE(16) tile (already zlib-inflated bytes ``u``) into ``w*h``
    RGB565 pixel values. The first byte is a ZRLE sub-encoding:

      0        RAW — w*h little-endian RGB565 pixels
      1        solid — one pixel fills the tile
      2..16    packed palette — N palette entries then packed indices (1/2/4 bpp,
               each row byte-aligned)
      128      plain RLE — [pixel][run-length bytes, 255-terminated], run = 1+Σ
      130..255 palette RLE — (sub-128) palette entries, then index bytes; a set
               high bit means a run-length follows
    """
    n = w * h
    sub = u[0]

    def pixel(off: int) -> int:  # little-endian RGB565 at u[off:off+2]
        return u[off] | (u[off + 1] << 8)

    if sub == 0:  # RAW
        return [pixel(1 + i * 2) for i in range(n)]
    if sub == 1:  # solid
        return [pixel(1)] * n
    if 2 <= sub <= 16:  # packed palette
        palette = [pixel(1 + i * 2) for i in range(sub)]
        p = 1 + sub * 2
        bpp = 1 if sub == 2 else (2 if sub <= 4 else 4)
        mask = (1 << bpp) - 1
        out: list[int] = []
        for _row in range(h):
            cur = have = 0  # each row starts on a fresh byte (byte-aligned)
            for _col in range(w):
                if have == 0:
                    cur, have, p = u[p], 8, p + 1
                have -= bpp
                out.append(palette[(cur >> have) & mask])
        return out
    if sub == 128:  # plain RLE
        p, out = 1, []
        while len(out) < n:
            px, p = pixel(p), p + 2
            run = 1
            while True:
                b, p = u[p], p + 1
                run += b
                if b != 255:
                    break
            out.extend([px] * run)
        return out[:n]
    if sub >= 130:  # palette RLE
        size = sub - 128
        palette = [pixel(1 + i * 2) for i in range(size)]
        p, out = 1 + size * 2, []
        while len(out) < n:
            idx, p = u[p], p + 1
            run = 1
            if idx & 0x80:
                while True:
                    b, p = u[p], p + 1
                    run += b
                    if b != 255:
                        break
            out.extend([palette[idx & 0x7F]] * run)
        return out[:n]
    raise ProtocolError(f"AMT RFB: unknown RLE sub-encoding {sub}")


class RedirKvmStream:
    """RFB byte-stream carried by an authenticated AMT redirection session (16994).

    AMT offers KVM two ways: the legacy plain-RFB listener on the standard VNC
    port 5900, and the same pixel protocol tunnelled inside a redirection
    session on 16994 — the port SOL and IDE-R already use. **Newer ME firmware
    disables the 5900 path and keeps only this one** (measured on a Latitude
    5411 at 14.1.79: ``Is5900PortEnabled`` false and un-settable, while a KVMR
    session on 16994 served pixels), so this is not an alternative transport so
    much as the surviving one (#245).

    Wire sequence, established against that device — the ME says nothing until
    the client opens the session, which is why a naive "connect and read"
    just times out::

        StartRedirectionSession("KVMR") + digest auth   (RedirectionChannel)
        client -> 0x40 OPEN_SESSION   rx/tx/heartbeat LE16 + version LE32
        ME     -> 0x41 OPEN_REPLY     byte[1] == 0 on success
        ME     -> "RFB 004.000\n"     ... the normal AMT RFB handshake follows

    Exposes the ``recv``/``sendall``/``close`` shape :class:`Rfb` needs, so the
    RFB state machine is identical on both transports.
    """

    def __init__(self, host: str, user: str, passwd: str, *,
                 port: int = 16994, timeout: float = 15.0):
        self.host, self.port = host, port
        self._user, self._passwd, self._timeout = user, passwd, timeout
        self._chan: Any = None
        # Own buffer, because RedirectionChannel.recv() hands back everything it
        # has buffered regardless of the size asked for. The ME packs OPEN_REPLY
        # and the "RFB 004.000" greeting into one segment, so an exact-8 read of
        # the reply would swallow the greeting and drop it — after which the RFB
        # handshake waits forever for bytes already consumed and the ME resets.
        self._pending = b""

    def connect(self) -> RedirKvmStream:
        from .redir import START_KVM, RedirectionChannel

        self._chan = RedirectionChannel(
            self.host, self._user, self._passwd, port=self.port, timeout=self._timeout
        )
        self._chan.open(START_KVM)
        # EXACTLY eight bytes: 0x40 then seven zeros. This shares a command byte
        # with IDE-R's OPEN_SESSION but NOT its body — IDE-R appends rx/tx/heartbeat
        # timeouts and a version (14 bytes total), and sending those here is what
        # broke #245 for a day. The ME takes the first 8 as the KVM start, replies
        # 0x41, and then reads the six leftover bytes as the beginning of our RFB
        # version string — so the greeting is answered with `<junk>RFB 003.` and the
        # ME rejects it with "Client requested an invalid RFB protocol version",
        # whatever version you actually meant. It looks exactly like a version
        # incompatibility and is nothing of the kind.
        self._chan.send(bytes([_REDIR_OPEN, 0, 0, 0, 0, 0, 0, 0]))
        reply = self._recv_exact(8)
        if reply[0] != _REDIR_OPEN_REPLY:
            raise ProtocolError(
                f"AMT KVM redirection: expected OPEN_REPLY (0x41) from {self.host}, "
                f"got 0x{reply[0]:02x}"
            )
        if reply[1] != 0:
            raise ProtocolError(
                f"AMT KVM redirection session refused by {self.host} (status {reply[1]})"
            )
        return self

    def _fill(self) -> None:
        chunk = self._chan.recv(65536)
        if not chunk:
            raise ConnectionError(f"AMT KVM redirection to {self.host} closed mid-stream")
        self._pending += chunk

    def _recv_exact(self, n: int) -> bytes:
        while len(self._pending) < n:
            self._fill()
        out, self._pending = self._pending[:n], self._pending[n:]
        return out

    def recv(self, n: int) -> bytes:
        """Up to ``n`` bytes, never more — the framing the RFB reader assumes."""
        if not self._pending:
            self._fill()
        out, self._pending = self._pending[:n], self._pending[n:]
        return out

    def sendall(self, data: bytes) -> None:
        self._chan.send(data)

    def close(self) -> None:
        if self._chan is not None:
            self._chan.close()
            self._chan = None


class Rfb:
    """One RFB session to an AMT KVM endpoint (connect → auth → ServerInit).
    Short-lived: opened per snapshot / HID burst, then closed.

    ``stream_factory`` supplies the byte transport; the default dials the
    standard VNC port directly. Pass :class:`RedirKvmStream` to run the very
    same protocol over a redirection session instead (#245).
    """

    def __init__(self, host: str, port: int, password: str, *, timeout: float = 15.0,
                 stream_factory: Callable[[], Any] | None = None):
        self.host, self.port, self._passwd, self._timeout = host, port, password, timeout
        self._stream_factory = stream_factory
        self.width = self.height = 0
        self._sock: socket.socket | None = None
        self._last_xy = (0, 0)
        # AMT's RLE(16) is a ZRLE-style scheme over ONE zlib stream spanning the
        # whole session — never reset per rect. AMT 14 firmware uses the *standard*
        # zlib format (a 0x78 0x9c header on the first tile), not raw deflate, so
        # wbits defaults to 15 (verified live against the wire).
        self._zlib = zlib.decompressobj()

    def __enter__(self) -> Rfb:
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- transport ------------------------------------------------------

    def _recv(self, n: int) -> bytes:
        assert self._sock is not None
        buf = bytearray()
        while len(buf) < n:
            try:
                chunk = self._sock.recv(n - len(buf))
            except OSError as e:  # reset/timeout — AMT dropped us mid-message
                raise ConnectionError(f"AMT RFB {self.host}:{self.port} dropped: {e}") from e
            if not chunk:
                raise ConnectionError(f"AMT RFB {self.host}:{self.port} closed mid-message")
            buf.extend(chunk)
        return bytes(buf)

    def _send(self, data: bytes) -> None:
        assert self._sock is not None
        try:
            self._sock.sendall(data)
        except OSError as e:  # broken pipe — AMT closed the connection (e.g. stuck session)
            raise ConnectionError(f"AMT RFB {self.host}:{self.port} dropped on send: {e}") from e

    def connect(self) -> None:
        # Idempotent: the driver hands out already-connected sessions (it picks the
        # transport by attempting one), and `with session as r:` would otherwise
        # dial a second time and strand the first.
        if self._sock is not None:
            return
        if self._stream_factory is not None:
            self._sock = self._stream_factory()
            self._handshake()
            return
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=self._timeout)
        except OSError as e:
            raise ConnectionError(
                f"AMT RFB connect to {self.host}:{self.port} failed: {e}. Newer ME builds "
                "harden off this legacy 5900 listener and serve KVM only through a "
                "redirection session on 16994; kvm-pilot tries that automatically, so if "
                "you are seeing this the redirection port did not answer either. Check that "
                "the listener is on (`kvm-pilot amt enable-sol`) and that 16994 is reachable "
                "before concluding KVM is disabled or touching MEBx (#245)."
            ) from e
        self._handshake()

    def _handshake(self) -> None:
        server_ver = self._recv(12)
        if not server_ver.startswith(b"RFB "):
            raise ProtocolError(f"AMT RFB: not an RFB server (got {server_ver!r})")
        self._send(b"RFB 003.008\n")
        n = self._recv(1)[0]
        if n == 0:
            # Spec says a reason string follows; AMT often just drops the socket.
            try:
                reason = self._recv(struct.unpack(">I", self._recv(4))[0])
                detail = reason.decode("latin-1", "replace")
            except (ConnectionError, struct.error):
                detail = "(no reason sent — the ME closed the connection)"
            raise AuthError(
                f"AMT RFB refused the connection: {detail}. The server offered no security "
                "types, so the KVM service declined the session rather than rejecting a "
                "credential. Read the reason above literally — it is the ME's own words. "
                "In particular an 'invalid RFB protocol version' here has been OUR framing "
                "bug before: stray bytes left in the stream shift the version string, and "
                "every version then looks invalid (#245)."
            )
        sectypes = set(self._recv(n))
        # 1 = None, 2 = VNC Authentication. Over a redirection session the digest
        # auth already happened at the transport layer, and the ME offers None
        # (measured: types [1, 128] on a 5411 at 14.1.79) — re-authenticating is
        # neither expected nor possible there, since the RFB password governs only
        # the 5900 listener. On plain 5900 the ME offers VNC auth instead. Prefer
        # None when offered: it means "already authenticated", not "unauthenticated".
        if 1 in sectypes:
            self._send(bytes([1]))
        elif 2 in sectypes:
            self._send(bytes([2]))
            challenge = self._recv(16)
            self._send(vnc_auth_response(self._passwd, challenge))
        else:
            raise AuthError(
                f"AMT RFB: server offers no security type we speak (offered {sorted(sectypes)}; "
                "we handle 1=None and 2=VNC-auth)."
            )
        if struct.unpack(">I", self._recv(4))[0] != 0:  # SecurityResult
            raise AuthError(
                f"AMT RFB auth rejected by {self.host} — check the KVM/RFB password."
            )
        self._send(bytes([1]))  # ClientInit: shared
        init = self._recv(24)
        self.width, self.height = struct.unpack(">HH", init[:4])
        bpp = init[4]
        self._recv(struct.unpack(">I", init[20:24])[0])  # desktop name
        if bpp != 16:  # AMT KVM is always RGB565; anything else we can't decode
            raise ProtocolError(
                f"AMT RFB: expected a 16-bpp RGB565 framebuffer, got {bpp}-bpp — "
                "unexpected AMT KVM format."
            )
        # Crucially: send NO SetPixelFormat — AMT resets on a 32-bpp request; we
        # keep its native RGB565. SetEncodings MUST list RAW explicitly (AMT does
        # not assume it); RLE(16) is offered so hybrid-GPU platforms (which won't
        # send RAW) still deliver frames; DesktopSize(-223) handles res changes.
        self._send(struct.pack(">BBHiii", 2, 0, 3, _ENC_RLE, _ENC_RAW, _ENC_DESKTOP_SIZE))

    # -- Video ----------------------------------------------------------

    def framebuffer_png(self) -> bytes:
        """Capture one full framebuffer as PNG. AMT sends the screen as ≤64×64 RAW
        RGB565 tiles (possibly spread across several update messages); we assemble
        them into a canvas and re-encode with ``zlib``. A DesktopSize change
        restarts the capture at the new dimensions."""
        for _ in range(3):
            w, h = self.width, self.height
            if not w or not h:
                raise ProtocolError("AMT RFB: server reported a 0-sized framebuffer")
            canvas = bytearray(w * h * 4)
            self._send(struct.pack(">BBHHHH", 3, 0, 0, 0, w, h))  # FBUR: full, non-incremental
            if self._collect_frame(canvas, w, h):
                return encode_png(w, h, bytes(canvas))
        raise ProtocolError("AMT RFB: framebuffer size kept changing during capture")

    def _collect_frame(self, canvas: bytearray, w: int, h: int) -> bool:
        """Read update messages until the whole canvas is covered. Returns False if
        a DesktopSize rectangle means we must restart at the new size."""
        covered = 0
        deadline = time.monotonic() + self._timeout
        while covered < w * h:
            if time.monotonic() > deadline:
                raise ProtocolError("AMT RFB: timed out assembling the framebuffer")
            msg = self._recv(1)[0]
            if msg == 2:  # Bell — ignore
                continue
            if msg == 3:  # ServerCutText: 3 pad + u32 length + text
                self._recv(3)
                self._recv(struct.unpack(">I", self._recv(4))[0])
                continue
            if msg != 0:  # 0 = FramebufferUpdate
                raise ProtocolError(f"AMT RFB: unexpected server message {msg}")
            self._recv(1)  # pad
            for _ in range(struct.unpack(">H", self._recv(2))[0]):
                rx, ry, rw, rh, enc = struct.unpack(">HHHHi", self._recv(12))
                if enc == _ENC_DESKTOP_SIZE:
                    self.width, self.height = rw, rh
                    return False  # caller restarts at the new size
                if enc == _ENC_RAW:
                    vals: Sequence[int] = struct.unpack(f"<{rw * rh}H", self._recv(rw * rh * 2))
                elif enc == _ENC_RLE:
                    dlen = struct.unpack(">I", self._recv(4))[0]
                    vals = _decode_zrle_tile(self._zlib.decompress(self._recv(dlen)), rw, rh)
                else:
                    raise ProtocolError(f"AMT RFB: unsupported encoding {enc}")
                self._blit_pixels(canvas, w, rx, ry, rw, rh, vals)
                covered += rw * rh
        return True

    def _blit_pixels(
        self, canvas: bytearray, cw: int, x: int, y: int, w: int, h: int, vals: Sequence[int]
    ) -> None:
        """Write w*h RGB565 pixel values into the RGBA canvas at (x, y) via the LUT."""
        lut = _RGB565_LUT
        i = 0
        for row in range(h):
            o = ((y + row) * cw + x) * 4
            for _col in range(w):
                v3 = vals[i] * 3
                canvas[o] = lut[v3]
                canvas[o + 1] = lut[v3 + 1]
                canvas[o + 2] = lut[v3 + 2]
                canvas[o + 3] = 255
                o += 4
                i += 1

    # -- HID ------------------------------------------------------------

    def key(self, keysym: int, down: bool) -> None:
        self._send(struct.pack(">BBHI", 4, 1 if down else 0, 0, keysym))

    def tap(self, keysym: int) -> None:
        self.key(keysym, True)
        self.key(keysym, False)

    def pointer(self, x: int, y: int, mask: int = 0) -> None:
        self._last_xy = (x, y)
        self._send(struct.pack(">BBHH", 5, mask & 0xFF, x, y))

    def click(self, button: int = 1) -> None:
        x, y = self._last_xy
        bit = {1: 0x01, 2: 0x02, 3: 0x04}.get(button, 0x01)  # left/middle/right
        self.pointer(x, y, bit)
        self.pointer(x, y, 0)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None


__all__ = [
    "Rfb", "des_encrypt_block", "vnc_auth_response", "encode_png",
    "key_to_keysym",
]
