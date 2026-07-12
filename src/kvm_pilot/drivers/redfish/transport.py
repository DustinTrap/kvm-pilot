"""
Stdlib-only HTTP transport for the DMTF Redfish REST API.

Unlike :mod:`kvm_pilot.http` (which is PiKVM-specific — ``X-KVMD-*`` auth, the
``ok``/``result`` envelope — and discards status/headers), Redfish correctness
depends on the HTTP status (``202`` async, ``412``/``428`` preconditions) and on
response headers (``X-Auth-Token``, ``Location``, ``ETag``). So this transport
returns status + headers + parsed body.

Auth follows DSP0266: **session-first** (POST to the SessionService Sessions
collection, then send the returned ``X-Auth-Token`` on every request, and
``DELETE`` the session on logout), with optional HTTP Basic. TLS is mandatory in
Redfish and BMCs ship self-signed certs, so ``verify_ssl`` is honored exactly as
in the PiKVM transport. Passwords and tokens are redacted from any raised error.

No third-party runtime dependencies.
"""

from __future__ import annotations

import base64
import builtins
import http.client
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

from ...errors import (
    AuthError,
    BusyError,
    ConnectionError,
    KVMPilotError,
    TimeoutError,
    UnavailableError,
)
from ...http import _build_opener, _build_ssl_context

logger = logging.getLogger("kvm_pilot.redfish")

_REDACTION = "***REDACTED***"
_SERVICE_ROOT = "/redfish/v1/"
_DEFAULT_SESSIONS = "/redfish/v1/SessionService/Sessions"


class RedfishResponse:
    """A parsed Redfish HTTP response: status code, headers, and JSON body."""

    __slots__ = ("status", "headers", "body")

    def __init__(self, status: int, headers: dict[str, str], body: dict | None):
        self.status = status
        self.headers = headers  # lower-cased keys
        self.body = body

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


class RedfishHTTP:
    """Session/Basic auth + JSON transport for a Redfish service."""

    def __init__(
        self,
        host: str,
        user: str,
        passwd: str,
        *,
        port: int = 443,
        scheme: str = "https",
        verify_ssl: bool = False,
        timeout: float = 30.0,
        auth: str = "session",
        max_retries: int = 3,
        backoff_base: float = 0.5,
        ssl_ca_file: str | None = None,
    ):
        if auth not in ("session", "basic"):
            raise ValueError(f"auth must be 'session' or 'basic', got {auth!r}")
        # Bare IPv6 literals need brackets in URLs; _same_origin compares against
        # the unbracketed form (urlsplit strips brackets from .hostname).
        host = host.strip("[]")
        host_url = f"[{host}]" if ":" in host else host
        self._base = f"{scheme}://{host_url}:{port}"
        self._scheme = scheme
        self._host = host
        self._port = port
        self._user = user
        self._passwd = passwd
        self._timeout = timeout
        self._auth = auth
        self._max_retries = max(0, max_retries)
        self._backoff_base = backoff_base
        self._verify_ssl = verify_ssl
        self._ssl_ctx = _build_ssl_context(verify_ssl, ssl_ca_file)
        # Refuse redirects: _same_origin guards the URLs we construct, but the
        # stdlib default opener would forward X-Auth-Token / Basic credentials
        # to whatever host a 3xx Location names, with no origin check.
        self._opener = _build_opener(self._ssl_ctx)
        self._token: str | None = None
        self._session_uri: str | None = None

    # -- secret redaction ------------------------------------------------

    def _redact(self, text: str) -> str:
        out = text
        for secret in (self._passwd, self._token):
            if secret:
                out = out.replace(secret, _REDACTION)
        return out

    # -- url + headers ---------------------------------------------------

    def _url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        if not path.startswith("/"):
            path = "/" + path
        return self._base + path

    def _same_origin(self, url: str) -> bool:
        """True if ``url`` targets the configured BMC origin.

        Redfish ``@odata.id`` / ``Location`` / ``nextLink`` values are usually
        relative (resolved against our base), but a misconfigured or hostile BMC
        can return an *absolute* URL pointing elsewhere. We must not attach the
        session token / Basic credentials to an off-origin host.
        """
        if not (url.startswith("http://") or url.startswith("https://")):
            return True  # relative -> resolved against self._base
        p = urllib.parse.urlsplit(url)
        host = (p.hostname or "").lower()
        port = p.port or (443 if p.scheme == "https" else 80)
        return p.scheme == self._scheme and host == self._host.lower() and port == self._port

    def _headers(self, *, authed: bool, json_body: bool) -> dict[str, str]:
        h = {"OData-Version": "4.0", "Accept": "application/json"}
        if json_body:
            h["Content-Type"] = "application/json"
        if authed:
            if self._token:
                h["X-Auth-Token"] = self._token
            elif self._auth == "basic":
                raw = f"{self._user}:{self._passwd}".encode()
                h["Authorization"] = "Basic " + base64.b64encode(raw).decode()
        return h

    # -- error mapping ---------------------------------------------------

    @staticmethod
    def _extract_message(body: dict | None) -> str:
        """Pull the human-facing text out of a Redfish extended-error body."""
        if not isinstance(body, dict):
            return ""
        err = body.get("error")
        if not isinstance(err, dict):
            return ""
        parts: list[str] = []
        if err.get("message"):
            parts.append(str(err["message"]))
        for info in err.get("@Message.ExtendedInfo", []) or []:
            if not isinstance(info, dict):
                continue
            mid = info.get("MessageId", "")
            msg = info.get("Message", "")
            if mid or msg:
                parts.append(f"{mid}: {msg}".strip(": "))
        return " | ".join(parts)

    @staticmethod
    def _password_change_required(body: dict | None) -> bool:
        if not isinstance(body, dict):
            return False
        err = body.get("error", {})
        infos = err.get("@Message.ExtendedInfo", []) if isinstance(err, dict) else []
        infos = infos or body.get("@Message.ExtendedInfo", []) or []
        return any(
            "PasswordChangeRequired" in str(i.get("MessageId", ""))
            for i in infos
            if isinstance(i, dict)
        )

    @staticmethod
    def _extended_info(body: dict | None) -> list[dict]:
        """The Redfish @Message.ExtendedInfo entries from an error body, if any."""
        if not isinstance(body, dict):
            return []
        err = body.get("error")
        infos = err.get("@Message.ExtendedInfo", []) if isinstance(err, dict) else []
        infos = infos or body.get("@Message.ExtendedInfo", []) or []
        return [i for i in infos if isinstance(i, dict)]

    def _raise(self, status: int, body: dict | None, raw: str) -> None:
        detail = self._redact(self._extract_message(body) or raw[:400])
        err = self._build_error(status, body, detail)
        # Attach the structured ExtendedInfo so callers can react to specific
        # MessageIds (e.g. InsertMedia's ActionParameterMissing) without string
        # scraping the human-facing detail.
        err.extended_info = self._extended_info(body)  # type: ignore[attr-defined]
        raise err

    def _build_error(self, status: int, body: dict | None, detail: str) -> KVMPilotError:
        if status in (401, 403):
            if self._password_change_required(body):
                err = AuthError(
                    "Account requires a password change before API use "
                    f"(Redfish PasswordChangeRequired): {detail}",
                    status,
                )
                # Re-login cannot fix this and would leak a session slot.
                err.password_change_required = True  # type: ignore[attr-defined]
                return err
            return AuthError(f"Authentication failed (HTTP {status}): {detail}", status)
        if status == 409:
            return BusyError(f"Resource busy (HTTP {status}): {detail}", status)
        if status == 503:
            return UnavailableError(f"Service unavailable (HTTP {status}): {detail}", status)
        return KVMPilotError(f"Redfish HTTP {status}: {detail}", status)

    @staticmethod
    def _is_retryable(exc: Exception, method: str) -> bool:
        # 409/503 are retried for READS only (#167): a 409/503 after a
        # state-changing POST is not proof the action wasn't applied — a BMC
        # whose management plane is perturbed by the ComputerSystem.Reset it
        # just accepted answers 503, and a transport-level re-POST would reset
        # the host twice. Surfacing the typed error lets the driver's own
        # reconciliation decide (RedfishDriver._reset re-reads PowerState).
        # A ConnectionError is only safe when the request never reached the
        # BMC or the method is read-only.
        idempotent = method in ("GET", "HEAD")
        if isinstance(exc, (BusyError, UnavailableError)):
            return idempotent
        if isinstance(exc, ConnectionError):
            return idempotent or not getattr(exc, "request_sent", False)
        return False

    # -- core request with retry ----------------------------------------

    def _is_session_path(self, path: str) -> bool:
        return "SessionService/Sessions" in path or (
            self._session_uri is not None and path == self._session_uri
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        authed: bool = True,
        retry: bool = True,
    ) -> RedfishResponse:
        try:
            return self._request_with_retries(method, path, json_body=json_body,
                                              authed=authed, retry=retry)
        except AuthError as exc:
            # One-shot session re-auth. Real BMCs terminate idle sessions
            # (DSP0266 SessionService inactivity timeout, ~30 min default) and
            # drop all tokens on reboot; the token can also be absent after a
            # driver close() whose discovery caches survived. Re-login once and
            # retry — safe even for POST actions, since a 401 is rejected before
            # the action runs, and login() issues its own requests unauthed so
            # this cannot recurse. Skipped for 403 (a privilege failure re-login
            # can't fix), PasswordChangeRequired, basic auth, and the Sessions
            # collection itself.
            if (
                authed
                and self._auth == "session"
                and exc.status_code == 401
                and not getattr(exc, "password_change_required", False)
                and not self._is_session_path(path)
            ):
                logger.info("Redfish session rejected (401); re-authenticating once")
                # Best-effort DELETE of the old session before re-login (#169): a
                # SPURIOUS 401 abandons a still-live server-side session, and
                # session-capped BMCs run out of slots -> operator lockout.
                # logout() never raises and nulls token+uri; the DELETE itself
                # 401-ing is fine (the session really was dead).
                self.logout()
                self.login()
                return self._request_with_retries(method, path, json_body=json_body,
                                                  authed=authed, retry=retry)
            raise

    def _request_with_retries(
        self, method: str, path: str, *, json_body: dict | None, authed: bool, retry: bool
    ) -> RedfishResponse:
        attempts = self._max_retries + 1 if retry else 1
        last_exc: Exception | None = None
        for attempt in range(attempts):
            try:
                return self._request_once(method, path, json_body=json_body, authed=authed)
            except Exception as exc:  # noqa: BLE001 - re-raised below
                last_exc = exc
                if attempt < attempts - 1 and self._is_retryable(exc, method):
                    time.sleep(self._backoff_base * (2**attempt))
                    continue
                raise
        assert last_exc is not None  # pragma: no cover
        raise last_exc

    def _request_once(
        self, method: str, path: str, *, json_body: dict | None, authed: bool
    ) -> RedfishResponse:
        data = json.dumps(json_body).encode() if json_body is not None else None
        url = self._url(path)
        # Never send credentials to an off-origin URL the BMC handed us.
        authed_eff = authed and self._same_origin(url)
        if authed and not authed_eff:
            logger.warning("Refusing to send Redfish credentials to off-origin URL")
        req = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers=self._headers(authed=authed_eff, json_body=data is not None),
        )
        try:
            with self._opener.open(req, timeout=self._timeout) as resp:
                raw = resp.read()
                headers = {k.lower(): v for k, v in resp.headers.items()}
                status = resp.status
        except urllib.error.HTTPError as e:
            if 300 <= e.code < 400:
                # Refused by _NoRedirect — the BMC pointed us off-origin.
                raise ConnectionError(
                    f"Refused to follow HTTP {e.code} redirect to "
                    f"{e.headers.get('Location')!r} — kvm-pilot never forwards "
                    "credentials to a redirect target"
                ) from e
            raw = e.read()
            body = self._parse(raw)
            self._raise(e.code, body, raw.decode(errors="replace"))
            raise  # pragma: no cover - _raise always raises
        except urllib.error.URLError as e:
            # Connect/send phase: the request did not complete a round trip.
            if isinstance(e.reason, builtins.TimeoutError):
                raise TimeoutError(
                    f"Redfish request timed out after {self._timeout}s"
                ) from e
            raise ConnectionError(f"Connection error: {e.reason}") from e
        except builtins.TimeoutError as e:
            # Read phase: the BMC got the request but the response never
            # arrived — deliberately not retryable.
            raise TimeoutError(
                f"Reading the Redfish response timed out after {self._timeout}s"
            ) from e
        except (OSError, http.client.HTTPException) as e:
            # Read phase (reset, remote disconnect, incomplete read): ambiguous —
            # the action may have been executed. Marked so retry skips it for
            # non-idempotent methods.
            err = ConnectionError(f"Connection failed mid-request: {e!r}")
            err.request_sent = True  # type: ignore[attr-defined]
            raise err from e
        return RedfishResponse(status, headers, self._parse(raw))

    @staticmethod
    def _parse(raw: bytes) -> dict | None:
        if not raw:
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else {"_value": parsed}

    # -- convenience -----------------------------------------------------

    def get_json(self, path: str) -> dict:
        resp = self.request("GET", path)
        return resp.body or {}

    # -- session lifecycle ----------------------------------------------

    def login(self) -> None:
        """Create a Redfish session (no-op in basic-auth mode)."""
        if self._auth == "basic":
            return
        root = self.request("GET", _SERVICE_ROOT, authed=False).body or {}
        sessions_uri = (
            (root.get("Links", {}) or {}).get("Sessions", {}).get("@odata.id")
            or _DEFAULT_SESSIONS
        )
        resp = self.request(
            "POST",
            sessions_uri,
            json_body={"UserName": self._user, "Password": self._passwd},
            authed=False,
        )
        token = resp.header("x-auth-token")
        if not token:
            raise AuthError("Redfish session created but no X-Auth-Token was returned")
        if self._password_change_required(resp.body):
            raise AuthError(
                "Account requires a password change before API use "
                "(Redfish PasswordChangeRequired on session create)"
            )
        self._token = token
        # Prefer the (spec-required) Location header; fall back to the session
        # resource's self-link in the body for non-compliant firmware that omits
        # Location — otherwise logout() can't DELETE and the session leaks.
        self._session_uri = resp.header("location") or (resp.body or {}).get("@odata.id")
        if not self._session_uri:
            logger.warning(
                "Redfish session created but the BMC returned no session URI (no "
                "Location header or body @odata.id) — the session cannot be DELETEd "
                "on logout and will occupy a slot until the BMC expires it (#169)"
            )

    def logout(self) -> None:
        """Delete the session token (best-effort; never raises)."""
        if self._auth == "basic" or not self._session_uri:
            self._token = None
            return
        try:
            self.request("DELETE", self._session_uri, retry=False)
        except Exception:  # noqa: BLE001 - logout must never raise
            logger.debug("Redfish session DELETE failed (ignored)", exc_info=True)
        finally:
            self._token = None
            self._session_uri = None


__all__ = ["RedfishHTTP", "RedfishResponse"]
