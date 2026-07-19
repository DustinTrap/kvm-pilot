"""Minimal WS-Management (DMTF WS-Man, SOAP 1.2) client for Intel AMT.

Intel AMT's management plane — power, boot configuration, inventory — speaks
WS-Man over HTTP (port 16992) or HTTPS/TLS (16993), authenticated with HTTP
Digest. Rather than depend on a WS-Man library (``pywsman`` needs a C
extension, which the "stdlib-only at core import" rule forbids), this is a small
hand-rolled client: it builds SOAP 1.2 envelopes with WS-Addressing + WS-Man
headers from templates, POSTs them with ``urllib`` (whose
``HTTPDigestAuthHandler`` performs the 401->Authorization digest handshake and
caches the nonce), and parses responses with ``xml.etree.ElementTree``.

Only the verbs AMT needs are implemented: ``get`` (one instance), ``enumerate``
(a class -> instances, via Enumerate+Pull), ``invoke`` (call a method), and
``put`` (update an instance). SOAP faults and transport failures map into the
kvm-pilot error taxonomy; the password is redacted from any raised text.
"""

from __future__ import annotations

import logging
import urllib.error
import urllib.request
import uuid
from xml.etree import ElementTree as ET

from ...errors import AuthError, ConnectionError, KVMPilotError, ProtocolError
from ...errors import TimeoutError as KpTimeoutError
from ...http import _build_ssl_context, _NoRedirect

logger = logging.getLogger("kvm_pilot.drivers.amt")

# XML namespaces
_S = "http://www.w3.org/2003/05/soap-envelope"
_WSA = "http://schemas.xmlsoap.org/ws/2004/08/addressing"
_WSMAN = "http://schemas.dmtf.org/wbem/wsman/1/wsman.xsd"
_WSEN = "http://schemas.xmlsoap.org/ws/2004/09/enumeration"
_ANON = "http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous"

# Resource-URI bases: CIM_* classes hang off the DMTF CIM schema; AMT_*/IPS_*
# classes off Intel's. A resource URI is ``<base><ClassName>``.
CIM = "http://schemas.dmtf.org/wbem/wscim/1/cim-schema/2/"
AMT = "http://intel.com/wbem/wscim/1/amt-schema/1/"
IPS = "http://intel.com/wbem/wscim/1/ips-schema/1/"

# WS-Transfer / WS-Enumeration action URIs
_GET = "http://schemas.xmlsoap.org/ws/2004/09/transfer/Get"
_PUT = "http://schemas.xmlsoap.org/ws/2004/09/transfer/Put"
_ENUMERATE = "http://schemas.xmlsoap.org/ws/2004/09/enumeration/Enumerate"
_PULL = "http://schemas.xmlsoap.org/ws/2004/09/enumeration/Pull"

_REDACTION = "***REDACTED***"


def cim(cls: str) -> str:
    """Resource URI for a ``CIM_`` class."""
    return CIM + cls


def amt(cls: str) -> str:
    """Resource URI for an ``AMT_`` class."""
    return AMT + cls


class WsmanError(KVMPilotError):
    """A SOAP fault or WS-Man protocol error returned by the AMT device."""


class Wsman:
    """A WS-Man client bound to one AMT endpoint (digest auth over HTTP/TLS)."""

    def __init__(
        self,
        host: str,
        user: str,
        passwd: str,
        *,
        port: int = 16992,
        tls: bool = False,
        verify_ssl: bool = False,
        ssl_ca_file: str | None = None,
        timeout: float = 30.0,
    ):
        self.host = host
        self._user = user
        self._passwd = passwd
        self._timeout = timeout
        scheme = "https" if tls else "http"
        self._url = f"{scheme}://{_bracket(host)}:{port}/wsman"
        # urllib's digest handler performs the 401 -> Authorization dance and
        # caches the nonce across calls on this opener; _NoRedirect keeps the
        # credential from following a 3xx Location (see http._NoRedirect).
        mgr = urllib.request.HTTPPasswordMgrWithDefaultRealm()
        mgr.add_password(None, self._url, user, passwd)
        handlers: list[urllib.request.BaseHandler] = [
            urllib.request.HTTPDigestAuthHandler(mgr),
            _NoRedirect(),
        ]
        if tls:
            handlers.append(
                urllib.request.HTTPSHandler(context=_build_ssl_context(verify_ssl, ssl_ca_file))
            )
        self._opener = urllib.request.build_opener(*handlers)

    # -- transport ------------------------------------------------------

    def _post(self, envelope: str) -> ET.Element:
        req = urllib.request.Request(
            self._url,
            data=envelope.encode("utf-8"),
            headers={"Content-Type": "application/soap+xml;charset=UTF-8"},
            method="POST",
        )
        try:
            with self._opener.open(req, timeout=self._timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise AuthError(
                    f"AMT WS-Man auth failed (HTTP {e.code}) for {self.host} — check "
                    "the AMT/MEBx admin credentials (AMT digest realm).",
                    e.code,
                ) from None
            raise WsmanError(
                f"AMT WS-Man HTTP {e.code} from {self.host}: {self._http_error_detail(e.read())}",
                e.code,
            ) from None
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", e)
            if isinstance(reason, TimeoutError):
                raise KpTimeoutError(
                    f"AMT WS-Man request to {self.host} timed out after {self._timeout}s"
                ) from None
            raise ConnectionError(f"AMT WS-Man connection to {self.host} failed: {reason}") from None
        except TimeoutError:  # read-phase timeout (urllib does not wrap these)
            raise KpTimeoutError(
                f"AMT WS-Man read from {self.host} timed out after {self._timeout}s"
            ) from None
        try:
            root = ET.fromstring(body)  # nosec B314 - AMT is a trusted management endpoint
        except ET.ParseError as e:
            raise ProtocolError(
                f"AMT WS-Man returned non-XML from {self.host}: {self._redact(_text(body))[:200]!r}"
            ) from e
        self._check_fault(root)
        return root

    def _check_fault(self, root: ET.Element) -> None:
        detail = self._fault_detail(root)
        if detail is not None:
            raise WsmanError(f"AMT WS-Man fault from {self.host}: {self._redact(detail)}")

    def _http_error_detail(self, body: bytes) -> str:
        """Human-readable detail for an HTTP 4xx/5xx: the SOAP fault's Reason +
        Subcode when the body is a parseable fault (AMT answers 400/500 with a
        full fault envelope), else the raw text truncated. Password-redacted.

        AMT's fault envelope opens with ~300 chars of namespace declarations, so
        the old ``body[:300]`` slice discarded the actual reason (#216)."""
        try:
            detail = self._fault_detail(ET.fromstring(body))  # nosec B314 - trusted mgmt endpoint
        except ET.ParseError:
            detail = None
        if detail is None:
            detail = _text(body).strip()[:300] or "(empty body)"
        return self._redact(detail)

    def _fault_detail(self, root: ET.Element) -> str | None:
        """``Reason (subcode X)`` from a SOAP fault anywhere under ``root``, or
        ``None`` if there is no fault. Matches on local names so it survives the
        prefix/namespace drift seen across AMT firmware versions. Prefers the
        specific ``Code/Subcode/Value`` (e.g. ``e:TimedOut``) over the top-level
        ``Sender``/``Receiver`` class."""
        fault = next((el for el in root.iter() if _local(el.tag) == "Fault"), None)
        if fault is None:
            return None
        reason = next(((el.text or "").strip() for el in fault.iter()
                       if _local(el.tag) == "Text" and (el.text or "").strip()), None)
        subcode_el = next((el for el in fault.iter() if _local(el.tag) == "Subcode"), None)
        subcode = None
        if subcode_el is not None:
            subcode = next(((el.text or "").strip() for el in subcode_el.iter()
                            if _local(el.tag) == "Value" and (el.text or "").strip()), None)
        if reason and subcode:
            return f"{reason} (subcode {subcode})"
        return reason or subcode or "unknown SOAP fault"

    def _redact(self, text: str) -> str:
        return text.replace(self._passwd, _REDACTION) if self._passwd else text

    # -- envelope builder -----------------------------------------------

    def _envelope(
        self, action: str, resource_uri: str, *, selectors: dict[str, str] | None = None,
        body_inner: str = "",
    ) -> str:
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<s:Envelope xmlns:s="{_S}" xmlns:wsa="{_WSA}" xmlns:wsman="{_WSMAN}">'
            "<s:Header>"
            f'<wsa:Action s:mustUnderstand="true">{action}</wsa:Action>'
            f'<wsa:To s:mustUnderstand="true">{self._url}</wsa:To>'
            f'<wsman:ResourceURI s:mustUnderstand="true">{resource_uri}</wsman:ResourceURI>'
            f"<wsa:MessageID>uuid:{uuid.uuid4()}</wsa:MessageID>"
            f"<wsa:ReplyTo><wsa:Address>{_ANON}</wsa:Address></wsa:ReplyTo>"
            f"<wsman:OperationTimeout>PT{int(self._timeout)}S</wsman:OperationTimeout>"
            '<wsman:MaxEnvelopeSize s:mustUnderstand="true">153600</wsman:MaxEnvelopeSize>'
            f"{_selector_xml(selectors)}"
            "</s:Header>"
            f"<s:Body>{body_inner}</s:Body>"
            "</s:Envelope>"
        )

    # -- verbs ----------------------------------------------------------

    def get(self, resource_uri: str, selectors: dict[str, str] | None = None) -> ET.Element:
        """Get one instance; returns the instance element (first ``<Body>`` child)."""
        return _body_child(self._post(self._envelope(_GET, resource_uri, selectors=selectors)))

    def enumerate(self, resource_uri: str) -> list[ET.Element]:
        """Enumerate a class into a list of instance elements (Enumerate then Pull)."""
        root = self._post(
            self._envelope(_ENUMERATE, resource_uri, body_inner=f'<wsen:Enumerate xmlns:wsen="{_WSEN}"/>')
        )
        ctx = root.find(f".//{{{_WSEN}}}EnumerationContext")
        context = ctx.text if ctx is not None else None
        items: list[ET.Element] = []
        for _ in range(64):  # AMT enumerations are small; bound the pull loop defensively
            if not context:
                break
            pull = (
                f'<wsen:Pull xmlns:wsen="{_WSEN}">'
                f"<wsen:EnumerationContext>{context}</wsen:EnumerationContext>"
                "<wsen:MaxElements>32</wsen:MaxElements></wsen:Pull>"
            )
            root = self._post(self._envelope(_PULL, resource_uri, body_inner=pull))
            for items_el in root.iter(f"{{{_WSEN}}}Items"):
                items.extend(list(items_el))
            if root.find(f".//{{{_WSEN}}}EndOfSequence") is not None:
                break
            nxt = root.find(f".//{{{_WSEN}}}EnumerationContext")
            context = nxt.text if nxt is not None else None
        return items

    def invoke(
        self, resource_uri: str, method: str, params_inner: str,
        selectors: dict[str, str] | None = None,
    ) -> ET.Element:
        """Invoke ``method`` on the instance; returns the ``*_OUTPUT`` element.

        ``params_inner`` is the fully-formed ``<x:method_INPUT xmlns:x=...>...``
        body element — kept caller-built so this stays class-agnostic.
        """
        action = f"{resource_uri}/{method}"
        return _body_child(
            self._post(self._envelope(action, resource_uri, selectors=selectors, body_inner=params_inner))
        )

    def put(
        self, resource_uri: str, body_inner: str, selectors: dict[str, str] | None = None
    ) -> ET.Element:
        """Replace an instance (WS-Transfer Put); returns the updated instance."""
        return _body_child(
            self._post(self._envelope(_PUT, resource_uri, selectors=selectors, body_inner=body_inner))
        )


# -- module helpers -------------------------------------------------------


def _selector_xml(selectors: dict[str, str] | None) -> str:
    if not selectors:
        return ""
    items = "".join(
        f'<wsman:Selector Name="{escape(name)}">{escape(value)}</wsman:Selector>'
        for name, value in selectors.items()
    )
    return f"<wsman:SelectorSet>{items}</wsman:SelectorSet>"


def escape(text: object) -> str:
    """Minimal XML text/attribute escaping for values placed into a template."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _bracket(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def _text(body: bytes) -> str:
    return body.decode("utf-8", "replace")


def _body_child(root: ET.Element) -> ET.Element:
    """First child of the SOAP ``<Body>`` (the returned instance / ``*_OUTPUT``).

    An empty Body (a legal Put/Invoke response with no payload) yields the Body
    element itself, so callers that only read fields via :func:`findtext` get an
    empty search space rather than an ``IndexError``.
    """
    body = root.find(f"{{{_S}}}Body")
    if body is None:
        return root
    return body[0] if len(body) else body


def findtext(el: ET.Element, tag: str) -> str | None:
    """Text of the first descendant whose *local* name equals ``tag`` (ns-agnostic).

    AMT responses carry several namespaces; matching on the local name keeps the
    readers robust to prefix/namespace drift across firmware versions.
    """
    for child in el.iter():
        if _local(child.tag) == tag:
            return child.text
    return None


def findall_local(el: ET.Element, tag: str) -> list[ET.Element]:
    """All descendants whose local name equals ``tag``."""
    return [c for c in el.iter() if _local(c.tag) == tag]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


__all__ = [
    "Wsman", "WsmanError", "cim", "amt", "escape", "findtext", "findall_local",
    "CIM", "AMT", "IPS",
]
