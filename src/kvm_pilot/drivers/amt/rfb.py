"""AMT KVM Redirection over RFB/VNC — firmware-level screenshot + HID.

This is the capability that makes AMT worth having: because the ME renders the
platform's real framebuffer *below* the OS, an RFB snapshot captures **BIOS,
POST, and the bootloader** — exactly what an HDMI-capture KVM on a laptop cannot
see. It also carries keyboard/mouse, so the whole pre-boot surface is drivable.

Scope: standard-port KVM redirection (TCP 5900) with **VNC Authentication**
(RFB security type 2). That needs single-block DES, which the stdlib lacks, so a
compact, FIPS-vector-tested DES lives here — keeping the driver dependency-free.
RAW-encoded framebuffers are decoded and re-encoded to PNG with ``zlib``.

Prerequisites on the target (MEBx / WS-Man): KVM Redirection enabled,
standard-port (5900) redirection on, an RFB password set, and (for unattended
capture) user-consent off. **Status: mock-tested only — no live AMT KVM has
validated this path yet** (the support matrix is the source of truth, per
CLAUDE.md); the emulator exercises the handshake, auth, RAW decode, and event
encoding, but real-firmware quirks are unproven.
"""

from __future__ import annotations

import socket
import struct
import zlib

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


class Rfb:
    """One RFB session to an AMT KVM-redirection endpoint (connect → auth →
    ServerInit). Short-lived: opened per snapshot / HID burst, then closed."""

    def __init__(self, host: str, port: int, password: str, *, timeout: float = 15.0):
        self.host, self.port, self._passwd, self._timeout = host, port, password, timeout
        self.width = self.height = 0
        self._sock: socket.socket | None = None
        self._last_xy = (0, 0)

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
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError(f"AMT RFB {self.host}:{self.port} closed mid-message")
            buf.extend(chunk)
        return bytes(buf)

    def _send(self, data: bytes) -> None:
        assert self._sock is not None
        self._sock.sendall(data)

    def connect(self) -> None:
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=self._timeout)
        except OSError as e:
            raise ConnectionError(
                f"AMT RFB connect to {self.host}:{self.port} failed: {e} "
                "(is KVM redirection + standard-port 5900 enabled in MEBx?)"
            ) from e
        self._handshake()

    def _handshake(self) -> None:
        server_ver = self._recv(12)
        if not server_ver.startswith(b"RFB "):
            raise ProtocolError(f"AMT RFB: not an RFB server (got {server_ver!r})")
        self._send(b"RFB 003.008\n")
        n = self._recv(1)[0]
        if n == 0:  # server sent a reason string then dropped us
            reason = self._recv(struct.unpack(">I", self._recv(4))[0])
            raise AuthError(f"AMT RFB refused the connection: {reason.decode('latin-1', 'replace')}")
        sectypes = set(self._recv(n))
        if 2 not in sectypes:  # 2 = VNC Authentication
            raise AuthError(f"AMT RFB: server offers no VNC-auth (types={sorted(sectypes)})")
        self._send(bytes([2]))
        challenge = self._recv(16)
        self._send(vnc_auth_response(self._passwd, challenge))
        if struct.unpack(">I", self._recv(4))[0] != 0:  # SecurityResult
            raise AuthError(
                f"AMT RFB auth rejected by {self.host} — check the KVM/RFB password."
            )
        self._send(bytes([1]))  # ClientInit: shared
        init = self._recv(24)
        self.width, self.height = struct.unpack(">HH", init[:4])
        self._recv(struct.unpack(">I", init[20:24])[0])  # desktop name
        # Ask for a fixed 32-bpp true-colour BGRA format so RAW decodes uniformly.
        self._send(
            b"\x00\x00\x00\x00" + struct.pack(
                ">BBBBHHHBBB", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0
            ) + b"\x00\x00\x00"
        )
        self._send(b"\x02\x00\x00\x01\x00\x00\x00\x00")  # SetEncodings: RAW(0) only

    # -- Video ----------------------------------------------------------

    def framebuffer_png(self) -> bytes:
        """Request one full RAW framebuffer update and return it as PNG bytes."""
        w, h = self.width, self.height
        if not w or not h:
            raise ProtocolError("AMT RFB: server reported a 0-sized framebuffer")
        self._send(struct.pack(">BBHHHH", 3, 0, 0, 0, w, h))  # FramebufferUpdateRequest
        rgba = self._read_update(w, h)
        return encode_png(w, h, rgba)

    def _read_update(self, w: int, h: int) -> bytes:
        hdr = self._recv(4)
        if hdr[0] != 0:
            raise ProtocolError(f"AMT RFB: unexpected server message {hdr[0]}")
        canvas = bytearray(w * h * 4)
        for _ in range(struct.unpack(">H", hdr[2:4])[0]):
            rx, ry, rw, rh, enc = struct.unpack(">HHHHi", self._recv(12))
            if enc != 0:
                raise ProtocolError(f"AMT RFB: only RAW encoding is handled (got {enc})")
            data = self._recv(rw * rh * 4)  # BGRA per the pixel format we set
            for row in range(rh):
                src = row * rw * 4
                for col in range(rw):
                    p = src + col * 4
                    b, g, r = data[p], data[p + 1], data[p + 2]
                    d = ((ry + row) * w + (rx + col)) * 4
                    canvas[d], canvas[d + 1], canvas[d + 2], canvas[d + 3] = r, g, b, 255
        return bytes(canvas)

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
