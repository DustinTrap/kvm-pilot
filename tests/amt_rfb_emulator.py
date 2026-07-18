"""A pure-stdlib RFB/VNC *server* emulator for the AMT KVM-redirection tests.

Speaks the RFB 3.8 server side on 127.0.0.1: ProtocolVersion -> VNC-auth
challenge -> ServerInit, then serves a RAW framebuffer on request and records
KeyEvent / PointerEvent messages. Knobs (``reject_auth``, ``width``/``height``/
``pixels``) live on the instance. Handles connections sequentially so a test can
snapshot then drive HID.
"""

from __future__ import annotations

import socket
import struct
import threading


class AmtRfbEmulator:
    def __init__(self, width: int = 2, height: int = 2, pixels=None, reject_auth: bool = False):
        self.width, self.height = width, height
        # Row-major (r, g, b); default a 2x2 red / green / blue / white.
        self.pixels = pixels or [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)]
        self.reject_auth = reject_auth
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
        conn.sendall(b"RFB 004.000\n")           # AMT announces RFB 4.0
        self._recv(conn, 12)                     # client ProtocolVersion (it downgrades to 003.008)
        conn.sendall(bytes([1, 2]))              # 1 security type: VNC-auth (2)
        self._recv(conn, 1)                      # client's chosen type
        conn.sendall(b"\x00" * 16)               # 16-byte challenge
        self._recv(conn, 16)                     # DES response (correctness proven elsewhere)
        if self.reject_auth:
            conn.sendall(struct.pack(">I", 1))   # SecurityResult: failed
            return
        conn.sendall(struct.pack(">I", 0))       # SecurityResult: OK
        self._recv(conn, 1)                      # ClientInit
        pf = struct.pack(">BBBBHHHBBB", 16, 16, 0, 1, 31, 63, 31, 11, 5, 0) + b"\x00\x00\x00"  # RGB565
        name = b"AMT-EMU"
        conn.sendall(struct.pack(">HH", self.width, self.height) + pf
                     + struct.pack(">I", len(name)) + name)
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

    def _framebuffer(self) -> bytes:
        data = bytearray()
        for (r, g, b) in self.pixels:
            v = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)  # pack to RGB565
            data += struct.pack("<H", v)                       # little-endian, as AMT sends it
        return (
            struct.pack(">BBH", 0, 0, 1)                              # FramebufferUpdate, 1 rect
            + struct.pack(">HHHHi", 0, 0, self.width, self.height, 0)  # full-screen RAW rect
            + bytes(data)
        )
