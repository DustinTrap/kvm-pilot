"""
Stdlib-only HTTP transport for the PiKVM / GLKVM REST API.

Adds, over a naive urllib wrapper:
  * Optional TOTP/2FA: the 6-digit code is appended to the password with no
    separator, per PiKVM's auth scheme.
  * Bounded retry with backoff on transient failures (409 busy, 503, network).
  * Password/secret redaction in any raised error text.

No third-party runtime dependencies. ``pyotp`` is imported lazily and only if
a TOTP secret is supplied; it is an optional extra, not a hard requirement.
"""

from __future__ import annotations

import http.cookiejar
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .errors import (
    AuthError,
    BusyError,
    ConnectionError,
    KVMPilotError,
    UnavailableError,
)

_REDACTION = "***REDACTED***"


def _totp_now(secret: str) -> str:
    """Return the current 6-digit TOTP code for ``secret``.

    Imported lazily so pyotp is only needed when 2FA is actually used.
    """
    try:
        import pyotp  # type: ignore
    except ImportError as exc:  # pragma: no cover - exercised only without extra
        raise KVMPilotError(
            "A TOTP secret was provided but pyotp is not installed. "
            "Install the 2FA extra:  pip install 'kvm-pilot[totp]'"
        ) from exc
    return pyotp.TOTP(secret).now()


class HTTP:
    """Thin urllib wrapper handling PiKVM auth, JSON parsing, and retries."""

    def __init__(
        self,
        host: str,
        user: str,
        passwd: str,
        *,
        verify_ssl: bool = False,
        timeout: float = 30.0,
        port: int = 443,
        scheme: str = "https",
        totp_secret: str | None = None,
        max_retries: int = 3,
        backoff_base: float = 0.5,
    ):
        self._base = f"{scheme}://{host}:{port}"
        self._user = user
        self._passwd = passwd
        self._timeout = timeout
        self._totp_secret = totp_secret
        self._max_retries = max(0, max_retries)
        self._backoff_base = backoff_base
        self._verify_ssl = verify_ssl
        self._ssl_ctx = ssl.create_default_context()
        if not verify_ssl:
            self._ssl_ctx.check_hostname = False
            self._ssl_ctx.verify_mode = ssl.CERT_NONE
        self._auth_token: str | None = None

    # -- auth helpers ----------------------------------------------------

    def _effective_passwd(self) -> str:
        if self._totp_secret:
            return self._passwd + _totp_now(self._totp_secret)
        return self._passwd

    def _headers(self, extra: dict | None = None) -> dict[str, str]:
        h: dict[str, str] = {
            "X-KVMD-User": self._user,
            "X-KVMD-Passwd": self._effective_passwd(),
        }
        if self._auth_token:
            h["Cookie"] = f"auth_token={self._auth_token}"
        if extra:
            h.update(extra)
        return h

    def _redact(self, text: str) -> str:
        out = text
        # Redact the full transmitted credential first: with TOTP enabled the
        # value on the wire is password+code, not just the base password.
        if self._totp_secret:
            try:
                out = out.replace(self._effective_passwd(), _REDACTION)
            except Exception:  # noqa: BLE001 - redaction must never raise
                pass
        if self._passwd:
            out = out.replace(self._passwd, _REDACTION)
        if self._auth_token:
            out = out.replace(self._auth_token, _REDACTION)
        return out

    def _url(self, path: str, params: dict | None = None) -> str:
        url = self._base + path
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url += "?" + urllib.parse.urlencode(clean)
        return url

    # -- error mapping ---------------------------------------------------

    def _raise(self, status: int, body: bytes) -> None:
        text = self._redact(body.decode(errors="replace"))
        if status in (401, 403):
            raise AuthError(f"Authentication failed (HTTP {status}): {text}", status)
        if status == 409:
            raise BusyError(f"Device busy (HTTP {status}): {text}", status)
        if status == 503:
            raise UnavailableError(f"Subsystem unavailable (HTTP {status}): {text}", status)
        raise KVMPilotError(f"HTTP {status}: {text}", status)

    @staticmethod
    def _is_retryable(exc: Exception) -> bool:
        if isinstance(exc, (BusyError, UnavailableError, ConnectionError)):
            return True
        return False

    # -- core request with retry ----------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        data: bytes | None = None,
        content_type: str = "application/x-www-form-urlencoded",
        raw_response: bool = False,
        extra_headers: dict | None = None,
        long_timeout: float | None = None,
        retry: bool = True,
    ) -> Any:
        attempts = self._max_retries + 1 if retry else 1
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                return self._request_once(
                    method,
                    path,
                    params=params,
                    data=data,
                    content_type=content_type,
                    raw_response=raw_response,
                    extra_headers=extra_headers,
                    long_timeout=long_timeout,
                )
            except Exception as exc:  # noqa: BLE001 - re-raised below
                last_exc = exc
                if attempt < attempts - 1 and self._is_retryable(exc):
                    time.sleep(self._backoff_base * (2 ** attempt))
                    continue
                raise
        assert last_exc is not None  # pragma: no cover
        raise last_exc

    def _request_once(
        self,
        method: str,
        path: str,
        *,
        params: dict | None,
        data: bytes | None,
        content_type: str,
        raw_response: bool,
        extra_headers: dict | None,
        long_timeout: float | None,
    ) -> Any:
        url = self._url(path, params)
        hdrs = self._headers(extra_headers)
        if data is not None:
            hdrs["Content-Type"] = content_type
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        timeout = long_timeout if long_timeout else self._timeout
        try:
            with urllib.request.urlopen(req, context=self._ssl_ctx, timeout=timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as e:
            self._raise(e.code, e.read())
            return None  # unreachable, _raise always raises
        except urllib.error.URLError as e:
            raise ConnectionError(f"Connection error: {e.reason}") from e

        if raw_response:
            return body
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            return body
        if isinstance(parsed, dict) and not parsed.get("ok", True):
            raise KVMPilotError(f"API returned ok=false: {self._redact(str(parsed))}", 200)
        return parsed.get("result", parsed) if isinstance(parsed, dict) else parsed

    # -- convenience verbs ----------------------------------------------

    def get(self, path: str, params: dict | None = None, **kw) -> Any:
        return self.request("GET", path, params=params, **kw)

    def post(
        self,
        path: str,
        params: dict | None = None,
        body: bytes | None = None,
        content_type: str = "application/x-www-form-urlencoded",
        **kw,
    ) -> Any:
        return self.request(
            "POST", path, params=params, data=body, content_type=content_type, **kw
        )

    def delete(self, path: str, **kw) -> Any:
        return self.request("DELETE", path, **kw)

    # -- session login (captures Set-Cookie) ----------------------------

    def login(self) -> str:
        cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(cj),
            urllib.request.HTTPSHandler(context=self._ssl_ctx),
        )
        data = urllib.parse.urlencode(
            {"user": self._user, "passwd": self._effective_passwd()}
        ).encode()
        req = urllib.request.Request(self._url("/api/auth/login"), data=data, method="POST")
        try:
            with opener.open(req, timeout=self._timeout) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            self._raise(e.code, e.read())
        except urllib.error.URLError as e:
            raise ConnectionError(f"Connection error: {e.reason}") from e
        for cookie in cj:
            if cookie.name == "auth_token":
                self._auth_token = str(cookie.value)
                return self._auth_token
        raise AuthError("Login succeeded but no auth_token cookie was returned")


__all__ = ["HTTP"]
