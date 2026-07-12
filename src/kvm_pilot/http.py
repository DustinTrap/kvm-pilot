"""
Stdlib-only HTTP transport for the PiKVM / GLKVM REST API.

Adds, over a naive urllib wrapper:
  * Optional TOTP/2FA: the 6-digit code is appended to the password with no
    separator, per PiKVM's auth scheme.
  * Bounded retry with backoff on transient failures (409 busy, 503, network) —
    but a non-idempotent request (POST) is never re-fired after a failure that
    may have already reached the device (read-phase reset/timeout), so a lost
    response can't power-cycle a box twice.
  * Timeouts and mid-request failures map into the kvm-pilot error taxonomy
    (TimeoutError / ConnectionError), never raw socket exceptions.
  * Password/secret redaction in any raised error text.

No third-party runtime dependencies. ``pyotp`` is imported lazily and only if
a TOTP secret is supplied; it is an optional extra, not a hard requirement.
"""

from __future__ import annotations

import builtins
import http.client
import http.cookiejar
import json
import logging
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import IO, Any

from .errors import (
    ApiDisabledError,
    AuthError,
    BusyError,
    ConnectionError,
    KVMPilotError,
    ProtocolError,
    TimeoutError,
    UnavailableError,
)

logger = logging.getLogger("kvm_pilot.http")

_REDACTION = "***REDACTED***"

_unverified_warned = False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow HTTP redirects.

    The stdlib's default redirect handler copies every request header —
    including our credentials (``X-KVMD-Passwd``, ``X-Auth-Token``,
    ``Authorization: Basic``, the session ``Cookie``) — to whatever host a 3xx
    ``Location`` names, with no same-origin check and even across an
    https->http downgrade. A hostile or misconfigured device could use that to
    exfiltrate the admin password. Neither the kvmd API nor Redfish needs
    transparent redirect following (Redfish async/Location is handled
    explicitly), so we refuse: returning ``None`` here makes urllib surface the
    3xx as an ``HTTPError`` to the caller instead of chasing it.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


def _build_opener(ssl_ctx: ssl.SSLContext, *extra: urllib.request.BaseHandler) -> urllib.request.OpenerDirector:
    """An opener that uses our TLS context and never follows redirects."""
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl_ctx), _NoRedirect(), *extra
    )


def _build_ssl_context(verify_ssl: bool, ssl_ca_file: str | None) -> ssl.SSLContext:
    """TLS context for a device transport (shared with the Redfish transport).

    ``ssl_ca_file`` pins verification to a specific CA — or the device's own
    self-signed certificate — and always wins. Otherwise ``verify_ssl`` selects
    the system trust store, or (the default, because PiKVM/GLKVM/BMC devices
    ship self-signed certs) no verification at all — a deliberate, *visible*
    choice: the first unverified context per process logs a warning, since the
    admin credentials ride this channel on every request.
    """
    if ssl_ca_file:
        return ssl.create_default_context(cafile=ssl_ca_file)
    ctx = ssl.create_default_context()
    if not verify_ssl:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        global _unverified_warned
        if not _unverified_warned:
            _unverified_warned = True
            logger.warning(
                "TLS verification is DISABLED (the default — PiKVM/BMC devices ship "
                "self-signed certs), so credentials travel over an unauthenticated "
                "channel. Pass verify_ssl=True, or pin the device cert with "
                "ssl_ca_file= / --ssl-ca-file / KVM_PILOT_SSL_CA_FILE."
            )
    return ctx


def _bracket_ipv6(host: str) -> str:
    """Wrap a bare IPv6 literal in brackets so it survives URL construction."""
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


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
        breaker_threshold: int = 3,
        not_found_hint: str | None = None,
        ssl_ca_file: str | None = None,
    ):
        self._base = f"{scheme}://{_bracket_ipv6(host)}:{port}"
        self._user = user
        self._passwd = passwd
        self._timeout = timeout
        self._totp_secret = totp_secret
        self._max_retries = max(0, max_retries)
        self._backoff_base = backoff_base
        # Consecutive-failure damper (#164): a wedged device makes every call burn
        # max_retries+1 attempts × backoff (~3.5s measured live). After this many
        # calls fail terminally in a row, drop to a single attempt so a poll loop
        # fast-fails instead of hammering a down box; any device response resets it.
        self._breaker_threshold = max(0, breaker_threshold)
        self._consecutive_failures = 0
        # Appended to a 404's error (and promotes it to ApiDisabledError) — set by
        # the GLKVM driver, whose firmware 404s every /api/* until the API is enabled.
        self._not_found_hint = not_found_hint
        self._verify_ssl = verify_ssl
        self._ssl_ca_file = ssl_ca_file
        self._ssl_ctx = _build_ssl_context(verify_ssl, ssl_ca_file)
        # A redirect-refusing opener (see _NoRedirect): credentials must never be
        # forwarded to a host a 3xx Location names.
        self._opener = _build_opener(self._ssl_ctx)
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
        if self._totp_secret:
            out = out.replace(self._totp_secret, _REDACTION)
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
        if status == 404 and self._not_found_hint:
            raise ApiDisabledError(f"HTTP 404: {text}\n{self._not_found_hint}", status)
        raise KVMPilotError(f"HTTP {status}: {text}", status)

    @staticmethod
    def _is_retryable(exc: Exception, method: str) -> bool:
        # 409/503 are retried (bounded, with backoff) for READS only. A 409/503
        # after a state-changing POST is NOT proof the op wasn't applied: a BMC
        # whose management plane is perturbed by the reset it just accepted
        # answers 503, and a transport-level re-POST would fire the destructive
        # action twice (#167) — surfacing the typed error lets the caller's own
        # reconciliation decide (e.g. RedfishDriver._reset re-reads PowerState).
        # A ConnectionError is only safe when the request never reached the
        # device (connect-phase failure) or the method is read-only: re-firing a
        # power/HID/MSD POST whose response was lost mid-read could execute the
        # destructive action twice.
        idempotent = method in ("GET", "HEAD")
        if isinstance(exc, (BusyError, UnavailableError)):
            return idempotent
        if isinstance(exc, ConnectionError):
            return idempotent or not getattr(exc, "request_sent", False)
        return False

    # -- core request with retry ----------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        data: bytes | IO[bytes] | None = None,
        content_type: str = "application/x-www-form-urlencoded",
        raw_response: bool = False,
        extra_headers: dict | None = None,
        long_timeout: float | None = None,
        retry: bool = True,
    ) -> Any:
        # A file-like body cannot be re-sent after a partial read, so never retry
        # a streaming upload (the caller also passes retry=False for it).
        streaming = data is not None and not isinstance(data, (bytes, bytearray))
        # When the damper is open (device confirmed down), make a single attempt so
        # a poll loop fast-fails instead of burning the full backoff every call (#164).
        retrying = retry and not streaming and not self.breaker_open
        attempts = self._max_retries + 1 if retrying else 1
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                result = self._request_once(
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
                if attempt < attempts - 1 and self._is_retryable(exc, method):
                    time.sleep(self._backoff_base * (2 ** attempt))
                    continue
                # Terminal: a transport-down failure (503/timeout/conn) advances the
                # damper; any definitive device response (2xx here, or a 4xx/auth
                # error below) means the box is up, so it resets. 409/503 count as
                # down regardless of method — #167's retry gate must not change
                # the #164 breaker semantics (a 503-on-POST still looks wedged).
                down = self._is_retryable(exc, method) or isinstance(
                    exc, (BusyError, UnavailableError)
                )
                self._consecutive_failures = self._consecutive_failures + 1 if down else 0
                raise
            self._consecutive_failures = 0
            return result
        assert last_exc is not None  # pragma: no cover
        raise last_exc

    @property
    def breaker_open(self) -> bool:
        """True when consecutive transport-down failures have crossed the threshold
        (#164): the device looks wedged, so calls fast-fail (one attempt) until it
        responds again. A threshold of 0 disables the damper."""
        return (
            self._breaker_threshold > 0
            and self._consecutive_failures >= self._breaker_threshold
        )

    def _request_once(
        self,
        method: str,
        path: str,
        *,
        params: dict | None,
        data: bytes | IO[bytes] | None,
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
            with self._opener.open(req, timeout=timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as e:
            if 300 <= e.code < 400:
                # Refused by _NoRedirect. Surfacing the Location is safe (it is
                # where the attacker wanted us to go, not a secret) and helps
                # debugging; the credentials are what we withheld.
                raise ConnectionError(
                    f"Refused to follow HTTP {e.code} redirect to "
                    f"{e.headers.get('Location')!r} — kvm-pilot never forwards "
                    "credentials to a redirect target"
                ) from e
            self._raise(e.code, e.read())
            return None  # unreachable, _raise always raises
        except urllib.error.URLError as e:
            # Connect/send phase: the request did not complete a round trip.
            if isinstance(e.reason, builtins.TimeoutError):
                raise TimeoutError(f"Request to {path} timed out after {timeout}s") from e
            raise ConnectionError(f"Connection error: {e.reason}") from e
        except builtins.TimeoutError as e:
            # Read phase (urllib does not wrap these): the device got the request
            # but the response never arrived — deliberately not retryable.
            raise TimeoutError(
                f"Reading the response to {path} timed out after {timeout}s"
            ) from e
        except (OSError, http.client.HTTPException) as e:
            # Read phase (reset, remote disconnect, incomplete read): ambiguous —
            # the request may have been executed. Marked so retry skips it for
            # non-idempotent methods.
            err = ConnectionError(f"Connection failed mid-request: {e!r}")
            err.request_sent = True  # type: ignore[attr-defined]
            raise err from e

        if raw_response:
            return body
        if not body:
            return None  # an empty 2xx body (204-style) is not a protocol violation
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            # Truncated/garbage body on a JSON endpoint: previously the raw bytes
            # leaked to dict-expecting callers and crashed as AttributeError (#170).
            preview = self._redact(body.decode(errors="replace"))[:200]
            raise ProtocolError(
                f"Expected JSON from {path} but got {len(body)} bytes of non-JSON "
                f"(truncated preview): {preview!r}",
                200,
            ) from None
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
        body: bytes | IO[bytes] | None = None,
        content_type: str = "application/x-www-form-urlencoded",
        **kw,
    ) -> Any:
        return self.request(
            "POST", path, params=params, data=body, content_type=content_type, **kw
        )

    # -- session login (captures Set-Cookie) ----------------------------

    def login(self) -> str:
        cj = http.cookiejar.CookieJar()
        # Same no-redirect policy as the main path. Login credentials ride the
        # POST body (which the stdlib drops on redirect), so the leak risk is
        # lower here, but a silent redirect otherwise yields a confusing
        # "no auth_token cookie" error — refuse it outright.
        opener = _build_opener(self._ssl_ctx, urllib.request.HTTPCookieProcessor(cj))
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
            if isinstance(e.reason, builtins.TimeoutError):
                raise TimeoutError(f"Login timed out after {self._timeout}s") from e
            raise ConnectionError(f"Connection error: {e.reason}") from e
        except builtins.TimeoutError as e:
            raise TimeoutError(f"Login timed out after {self._timeout}s") from e
        except (OSError, http.client.HTTPException) as e:
            raise ConnectionError(f"Connection failed mid-request: {e!r}") from e
        for cookie in cj:
            if cookie.name == "auth_token":
                self._auth_token = str(cookie.value)
                return self._auth_token
        raise AuthError("Login succeeded but no auth_token cookie was returned")


__all__ = ["HTTP"]
