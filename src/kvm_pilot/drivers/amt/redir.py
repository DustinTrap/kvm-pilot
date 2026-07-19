"""Intel AMT redirection-session transport (SOL / KVM / IDE-R share this).

AMT's storage- and serial-redirection features ride a proprietary *binary*
channel on port 16994 (plaintext) / 16995 (TLS) — distinct from the WS-Man
management plane (:mod:`.wsman`) and from the RFB/KVM port 5900 (:mod:`.rfb`).
Every redirection connection opens with the same three-step handshake:

    1. ``StartRedirectionSession`` (0x10) + a 4-char protocol tag → reply 0x11
    2. ``AuthenticateSession`` (0x13) query → reply 0x14 lists auth types
    3. HTTP-Digest auth (0x13 again) over the admin credentials → reply 0x14

After a success reply the socket carries raw protocol traffic (SOL data, or the
IDE-R command stream in :mod:`.ider`). This module implements only the shared
handshake; the protocol tag selects the feature.

Wire layout and the digest construction are ported from Intel's redirection
spec and MeshCommander's ``amt-redir-duk.js`` (the maintained reference for
AMT 11–16 — the legacy ``amtider`` tool speaks an older revision AMT 14 rejects,
which is why it fails, #213). stdlib-only: raw ``socket`` + ``hashlib``.
"""

from __future__ import annotations

import hashlib
import os
import socket
import struct

from ...errors import AuthError, ConnectionError, ProtocolError
from ...http import _build_ssl_context

# Redirection command bytes (client<->ME) for the session/auth phase.
_START_SESSION = 0x10
_START_SESSION_REPLY = 0x11
_AUTHENTICATE = 0x13
_AUTHENTICATE_REPLY = 0x14

# 4-byte protocol tags that follow StartRedirectionSession (0x10 rr rr rr ....).
START_SOL = bytes([0x10, 0x00, 0x00, 0x00]) + b"SOL "
START_KVM = bytes([0x10, 0x01, 0x00, 0x00]) + b"KVMR"
START_IDER = bytes([0x10, 0x00, 0x00, 0x00]) + b"IDER"

_AUTHURI = "/RedirectionService"  # the digest "uri" AMT authenticates against


def _md5_hex(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()  # nosec B324 - AMT digest uses MD5 by spec


class RedirectionChannel:
    """A connected, authenticated AMT redirection socket ready for feature traffic.

    Construct, then call :meth:`open` with a protocol tag (``START_IDER`` etc.);
    on return the handshake is complete and :meth:`send` / :meth:`recv` carry the
    feature's own protocol. Not thread-safe; the caller owns the read loop.
    """

    def __init__(
        self, host: str, user: str, passwd: str, *, port: int = 16994,
        tls: bool = False, verify_ssl: bool = False, ssl_ca_file: str | None = None,
        timeout: float = 30.0,
    ):
        self.host = host
        self._user = user
        self._passwd = passwd
        self._port = port
        self._tls = tls
        self._verify_ssl = verify_ssl
        self._ssl_ca_file = ssl_ca_file
        self._timeout = timeout
        self._sock: socket.socket | None = None
        self._buf = b""

    # -- transport ------------------------------------------------------

    def _connect(self) -> None:
        try:
            sock = socket.create_connection((self.host, self._port), timeout=self._timeout)
        except OSError as e:
            raise ConnectionError(
                f"AMT redirection connect to {self.host}:{self._port} failed: {e} "
                "(is the redirection listener enabled — `kvm-pilot amt enable-sol`?)"
            ) from None
        if self._tls:
            ctx = _build_ssl_context(self._verify_ssl, self._ssl_ca_file)
            sock = ctx.wrap_socket(sock, server_hostname=self.host)
        sock.settimeout(self._timeout)
        self._sock = sock

    def send(self, data: bytes) -> None:
        assert self._sock is not None
        self._sock.sendall(data)

    def _recv_at_least(self, n: int) -> None:
        """Block until the internal buffer holds >= n bytes (or the peer closes)."""
        assert self._sock is not None
        while len(self._buf) < n:
            try:
                chunk = self._sock.recv(65536)
            except TimeoutError as e:
                raise ProtocolError(
                    f"AMT redirection read from {self.host} timed out after {self._timeout}s"
                ) from e
            if not chunk:
                raise ProtocolError(f"AMT redirection connection to {self.host} closed mid-handshake")
            self._buf += chunk

    def recv(self, bufsize: int = 65536) -> bytes:
        """Return buffered/socket bytes for the feature protocol (post-handshake)."""
        assert self._sock is not None
        if self._buf:
            out, self._buf = self._buf, b""
            return out
        return self._sock.recv(bufsize)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    # -- handshake ------------------------------------------------------

    def open(self, protocol_start: bytes) -> RedirectionChannel:
        """Run the full session-start + digest-auth handshake; returns self."""
        self._connect()
        self.send(protocol_start)
        self._start_session_reply()
        self._authenticate()
        return self

    def _start_session_reply(self) -> None:
        # 0x11: [type][status][2 reserved]... [byte12]=oem-data-len [oem...]
        self._recv_at_least(13)
        if self._buf[0] != _START_SESSION_REPLY:
            raise ProtocolError(
                f"AMT redirection: expected StartRedirectionSessionReply (0x11) "
                f"from {self.host}, got 0x{self._buf[0]:02x}"
            )
        if self._buf[1] != 0:
            raise ProtocolError(
                f"AMT redirection session rejected by {self.host} (status {self._buf[1]})"
            )
        oem_len = self._buf[12]
        self._recv_at_least(13 + oem_len)
        self._buf = self._buf[13 + oem_len:]

    def _authenticate(self) -> None:
        # Step 1: query supported auth types — 0x13, 3 reserved, authType=0, len=0.
        self.send(bytes([_AUTHENTICATE, 0, 0, 0, 0, 0, 0, 0, 0]))
        status, auth_type, data = self._auth_reply()
        if auth_type == 0:  # query reply: data is the list of supported type bytes
            if 4 not in data:
                raise AuthError(
                    f"AMT {self.host} redirection does not offer digest auth "
                    f"(types offered: {list(data)})"
                )
            # Step 2: request a digest challenge (type 4, cnonce-based).
            self.send(self._auth_request(4))
            status, auth_type, data = self._auth_reply()

        if auth_type in (3, 4) and status == 1:
            self.send(self._digest_response(auth_type, data))
            status, auth_type, data = self._auth_reply()

        if status != 0:
            raise AuthError(
                f"AMT redirection auth failed for {self.host} (status {status}) — "
                "check the AMT admin credentials.",
                status,
            )

    def _auth_reply(self) -> tuple[int, int, bytes]:
        # 0x14: [type][status][2 reserved][authType@4][dataLen LE32 @5][data@9..]
        self._recv_at_least(9)
        if self._buf[0] != _AUTHENTICATE_REPLY:
            raise ProtocolError(
                f"AMT redirection: expected AuthenticateSessionReply (0x14) from "
                f"{self.host}, got 0x{self._buf[0]:02x}"
            )
        status = self._buf[1]
        auth_type = self._buf[4]
        data_len = struct.unpack_from("<I", self._buf, 5)[0]
        self._recv_at_least(9 + data_len)
        data = self._buf[9:9 + data_len]
        self._buf = self._buf[9 + data_len:]
        return status, auth_type, data

    def _auth_request(self, auth_type: int) -> bytes:
        # Request a challenge: username + authuri, empty realm/nonce placeholders.
        user = self._user.encode()
        uri = _AUTHURI.encode()
        body = (bytes([len(user)]) + user + b"\x00\x00"
                + bytes([len(uri)]) + uri + b"\x00\x00\x00\x00")
        return bytes([_AUTHENTICATE, 0, 0, 0, auth_type]) + struct.pack("<I", len(body)) + body

    def _digest_response(self, auth_type: int, data: bytes) -> bytes:
        realm, p = _lp_str(data, 0)
        nonce, p = _lp_str(data, p)
        qop = ""
        if auth_type == 4:
            qop, p = _lp_str(data, p)
        cnonce = os.urandom(16).hex()
        nc = "00000002"
        extra = f"{nc}:{cnonce}:{qop}:" if auth_type == 4 else ""
        ha1 = _md5_hex(f"{self._user}:{realm}:{self._passwd}")
        ha2 = _md5_hex(f"POST:{_AUTHURI}")
        digest = _md5_hex(f"{ha1}:{nonce}:{extra}{ha2}")
        fields = [self._user, realm, nonce, _AUTHURI, cnonce, nc, digest]
        if auth_type == 4:
            fields.append(qop)
        body = b"".join(bytes([len(f)]) + f.encode() for f in fields)
        return bytes([_AUTHENTICATE, 0, 0, 0, auth_type]) + struct.pack("<H", len(body)) + b"\x00\x00" + body


def _lp_str(data: bytes, pos: int) -> tuple[str, int]:
    """Read a 1-byte-length-prefixed string at ``pos``; return (value, next-pos)."""
    n = data[pos]
    return data[pos + 1:pos + 1 + n].decode("utf-8", "replace"), pos + 1 + n
