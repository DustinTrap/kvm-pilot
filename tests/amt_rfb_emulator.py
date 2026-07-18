"""A pure-stdlib RFB/VNC *server* emulator for the AMT KVM-redirection tests.

Speaks the RFB 3.8 server side on 127.0.0.1: ProtocolVersion -> VNC-auth
challenge -> ServerInit, then serves a framebuffer on request and records
KeyEvent / PointerEvent messages. Handles connections sequentially so a test can
snapshot then drive HID.

Knobs on the instance model the corners the driver has to survive:
  * framebuffer shape — ``reject_auth``; a single RAW rect (default), the screen
    split into many ≤64×64 RAW tiles across several update messages
    (``tile_mode``), a single **RLE(16)** rect through a real standard-zlib
    stream (``rle_mode``), or a **DesktopSize(-223)** restart (``resize_first_to``
    / ``always_resize``);
  * injected noise — a Bell + ServerCutText before the frame (``inject_control``),
    an unsupported rect encoding (``bad_encoding``) or an unknown server message
    (``bad_message``);
  * handshake failures — not-an-RFB banner (``bad_protocol``), a reason string
    then a drop (``reason_drop``), no VNC-auth on offer (``no_vnc_auth``), a
    non-16-bpp ServerInit (``bad_bpp``);
  * transport drops — close the first N connections outright (``drop_first``).
"""

from __future__ import annotations

import socket
import struct
import threading
import zlib

_ENC_RAW = 0
_ENC_RLE = 16
_ENC_DESKTOP_SIZE = -223


def _p565(r: int, g: int, b: int) -> int:
    """Pack an (r, g, b) triple to a 16-bit RGB565 value (as AMT sends it)."""
    return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)


class AmtRfbEmulator:
    def __init__(self, width: int = 2, height: int = 2, pixels=None, reject_auth: bool = False):
        self.width, self.height = width, height
        # Row-major (r, g, b); default a 2x2 red / green / blue / white.
        self.pixels = pixels if pixels is not None else [
            (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)]
        self.reject_auth = reject_auth
        # framebuffer shape / injected noise
        self.tile_mode = False          # split the screen into 1x1 RAW tiles over many messages
        self.rle_mode = False           # send one RLE(16) rect through a real zlib stream
        self.resize_first_to: tuple[int, int] | None = None  # first update is a DesktopSize restart
        self.always_resize = False      # every update is a DesktopSize rect (never a real frame)
        self.inject_control = False     # prepend a Bell + ServerCutText before the frame
        self.bad_encoding = False       # a rect with an unsupported encoding id
        self.bad_message = False        # an unknown server-to-client message type
        # handshake failures
        self.bad_protocol = False       # server banner is not "RFB "
        self.reason_drop = False        # security-type count 0 -> reason string, then drop
        self.no_vnc_auth = False        # offer security types without VNC-auth (2)
        self.bad_bpp = False            # ServerInit reports a non-16-bpp framebuffer
        # transport drops
        self.drop_first = 0             # close this many connections immediately after accept
        self._resized = False
        self.keys: list[tuple[int, int]] = []          # (down_flag, keysym)
        self.pointers: list[tuple[int, int, int]] = []  # (button_mask, x, y)
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(2)
        self._thread = threading.Thread(target=self._serve, daemon=True)

    @property
    def host(self) -> str:
        return self._srv.getsockname()[0]

    @property
    def port(self) -> int:
        return self._srv.getsockname()[1]

    def __enter__(self) -> AmtRfbEmulator:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            self._srv.close()
        except OSError:
            pass
        self._thread.join(timeout=2)

    # -- server ---------------------------------------------------------

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return  # listener closed on __exit__
            if self.drop_first > 0:
                # A stuck single AMT session drops us before we get anywhere.
                self.drop_first -= 1
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            try:
                self._session(conn)
            except (OSError, struct.error):
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    @staticmethod
    def _recv(conn: socket.socket, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            c = conn.recv(n - len(buf))
            if not c:
                raise OSError("client closed")
            buf.extend(c)
        return bytes(buf)

    def _session(self, conn: socket.socket) -> None:
        # One zlib stream per session, matching the driver's per-session decompressor.
        self._rle_zlib = zlib.compressobj()
        if self.bad_protocol:
            conn.sendall(b"NOTaBANNER\r\n")   # 12 bytes, not an RFB ProtocolVersion
            return
        conn.sendall(b"RFB 004.000\n")        # AMT announces RFB 4.0
        self._recv(conn, 12)                  # client ProtocolVersion (it downgrades to 003.008)
        if self.reason_drop:
            reason = b"no redirection sessions available"
            conn.sendall(struct.pack(">B", 0) + struct.pack(">I", len(reason)) + reason)
            return                            # count 0 => reason string, then the server drops us
        if self.no_vnc_auth:
            conn.sendall(bytes([1, 1]))       # 1 type: None(1) — deliberately NOT VNC-auth(2)
            self._recv(conn, 1)
            return
        conn.sendall(bytes([1, 2]))           # 1 security type: VNC-auth (2)
        self._recv(conn, 1)                   # client's chosen type
        conn.sendall(b"\x00" * 16)            # 16-byte challenge
        self._recv(conn, 16)                  # DES response (correctness proven elsewhere)
        if self.reject_auth:
            conn.sendall(struct.pack(">I", 1))   # SecurityResult: failed
            return
        conn.sendall(struct.pack(">I", 0))       # SecurityResult: OK
        self._recv(conn, 1)                      # ClientInit
        bpp = 8 if self.bad_bpp else 16
        pf = struct.pack(">BBBBHHHBBB", bpp, 16, 0, 1, 31, 63, 31, 11, 5, 0) + b"\x00\x00\x00"  # RGB565
        name = b"AMT-EMU"
        conn.sendall(struct.pack(">HH", self.width, self.height) + pf
                     + struct.pack(">I", len(name)) + name)
        if self.bad_bpp:
            return  # the driver rejects the pixel format before requesting a frame
        self._loop(conn)

    def _loop(self, conn: socket.socket) -> None:
        while True:
            t = self._recv(conn, 1)[0]
            if t == 0:      # SetPixelFormat (3 pad + 16 format)
                self._recv(conn, 19)
            elif t == 2:    # SetEncodings
                self._recv(conn, 1)
                n = struct.unpack(">H", self._recv(conn, 2))[0]
                self._recv(conn, 4 * n)
            elif t == 3:    # FramebufferUpdateRequest
                self._recv(conn, 9)
                conn.sendall(self._framebuffer())
            elif t == 4:    # KeyEvent
                d = self._recv(conn, 7)
                self.keys.append((d[0], struct.unpack(">I", d[3:7])[0]))
            elif t == 5:    # PointerEvent
                d = self._recv(conn, 5)
                self.pointers.append((d[0], struct.unpack(">H", d[1:3])[0],
                                      struct.unpack(">H", d[3:5])[0]))
            else:
                raise OSError(f"unknown RFB client message type {t}")

    # -- framebuffer responses ------------------------------------------

    def _framebuffer(self) -> bytes:
        if self.bad_message:
            return struct.pack(">B", 9)  # not a 0/2/3 server message -> driver rejects it
        if self.always_resize:
            return self._desktop_size_rect(self.width, self.height)  # never a real frame
        if self.resize_first_to is not None and not self._resized:
            self._resized = True
            w, h = self.resize_first_to
            self.width, self.height = w, h
            self.pixels = [self.pixels[0]] * (w * h)  # solid, resized to the new geometry
            return self._desktop_size_rect(w, h)
        prefix = self._control_prefix() if self.inject_control else b""
        if self.bad_encoding:
            return prefix + self._bad_encoding_rect()
        if self.rle_mode:
            return prefix + self._rle_frame()
        if self.tile_mode:
            return prefix + self._tiled_frame()
        return prefix + self._raw_full_frame()

    def _raw_full_frame(self) -> bytes:
        data = bytearray()
        for (r, g, b) in self.pixels:
            data += struct.pack("<H", _p565(r, g, b))  # little-endian, as AMT sends it
        return (
            struct.pack(">BBH", 0, 0, 1)                               # FramebufferUpdate, 1 rect
            + struct.pack(">HHHHi", 0, 0, self.width, self.height, _ENC_RAW)  # full-screen RAW
            + bytes(data)
        )

    def _tiled_frame(self) -> bytes:
        # One FramebufferUpdate per row, each carrying W 1x1 RAW tiles — the client
        # must assemble several tiles spread across several update messages.
        out = bytearray()
        for y in range(self.height):
            out += struct.pack(">BBH", 0, 0, self.width)
            for x in range(self.width):
                r, g, b = self.pixels[y * self.width + x]
                out += struct.pack(">HHHHi", x, y, 1, 1, _ENC_RAW) + struct.pack("<H", _p565(r, g, b))
        return bytes(out)

    def _rle_frame(self) -> bytes:
        # One RLE(16) rect covering the whole framebuffer, compressed through a real
        # standard-zlib stream (0x78 0x9c) exactly as AMT 14 firmware sends it. The
        # inflated tile uses ZRLE sub-encoding 0 (RAW): the w*h RGB565 pixels.
        tile = bytearray([0])
        for (r, g, b) in self.pixels:
            tile += struct.pack("<H", _p565(r, g, b))
        comp = self._rle_zlib.compress(bytes(tile)) + self._rle_zlib.flush(zlib.Z_SYNC_FLUSH)
        return (
            struct.pack(">BBH", 0, 0, 1)
            + struct.pack(">HHHHi", 0, 0, self.width, self.height, _ENC_RLE)
            + struct.pack(">I", len(comp)) + comp
        )

    def _desktop_size_rect(self, w: int, h: int) -> bytes:
        return (struct.pack(">BBH", 0, 0, 1)
                + struct.pack(">HHHHi", 0, 0, w, h, _ENC_DESKTOP_SIZE))

    def _bad_encoding_rect(self) -> bytes:
        return (struct.pack(">BBH", 0, 0, 1)
                + struct.pack(">HHHHi", 0, 0, self.width, self.height, 99))  # 99 = unsupported

    def _control_prefix(self) -> bytes:
        text = b"clipboard"
        return (struct.pack(">B", 2)                              # Bell
                + struct.pack(">B", 3) + b"\x00\x00\x00"          # ServerCutText: type + 3 pad
                + struct.pack(">I", len(text)) + text)
