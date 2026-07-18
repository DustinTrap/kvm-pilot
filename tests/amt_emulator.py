"""A pure-stdlib Intel AMT WS-Man emulator for the AmtDriver tests.

Mirrors ``redfish_emulator.py``: a ``ThreadingHTTPServer`` on 127.0.0.1 that
speaks just enough WS-Man (SOAP 1.2) for the driver — Get / Enumerate+Pull /
Invoke / Put — plus an optional HTTP Digest challenge so the auth round-trip and
the ``AuthError`` path are exercisable. All state and captured requests live on
``AmtState`` so a test sets a knob before acting and asserts on ``state.calls``
after. No Docker, no third-party deps.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.etree import ElementTree as ET

_S = "http://www.w3.org/2003/05/soap-envelope"
_WSEN = "http://schemas.xmlsoap.org/ws/2004/09/enumeration"
_G = "http://intel.com/wbem/wscim/1/amt-schema/1/emu"  # response instance namespace (any)


class AmtState:
    """Mutable knobs + captured requests shared with the request handler."""

    def __init__(self) -> None:
        self.power_state = "8"          # CIM PowerState: 2=on, 8=off-soft
        self.last_power_request: str | None = None
        self.boot_order = ""            # substring compared against the source id
        self.bios_setup = "false"
        self.provisioning_state = "2"   # 2 = post/provisioned
        self.amt_version = "16.1.25"
        self.manufacturer = "Dell Inc."
        self.model = "Latitude 5411"
        self.serial = "JXXD6D3"
        self.platform_guid = "4c4c4544-0058-1234-5678-abcdef012345"
        self.require_auth = False       # issue an HTTP Digest challenge first
        self.reject_auth = False        # 401 even after the client authenticates
        self.fault_reason: str | None = None  # return a SOAP fault for every op
        self.calls: list[tuple[str, str]] = []  # (action_tail, resource_tail)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:  # silence test output
        pass

    @property
    def _state(self) -> AmtState:
        return self.server.state  # type: ignore[attr-defined]

    def _send(self, body: str, status: int = 200, headers: dict | None = None) -> None:
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/soap+xml;charset=UTF-8")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        st = self._state
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)  # always drain the body (keep-alive safety)
        authz = self.headers.get("Authorization")
        if st.require_auth and authz is None:
            # Issue a Digest challenge; urllib computes the response and retries.
            self._send(
                "", 401,
                {"WWW-Authenticate": 'Digest realm="Digest:AMT", nonce="deadbeef", qop="auth"'},
            )
            return
        if st.reject_auth:
            self._send("", 401, {"WWW-Authenticate": 'Digest realm="Digest:AMT", nonce="x", qop="auth"'})
            return

        req = ET.fromstring(raw)  # nosec B314 - test input
        action = _text(req, "Action") or ""
        resource = _text(req, "ResourceURI") or ""
        st.calls.append((action.rsplit("/", 1)[-1], resource.rsplit("/", 1)[-1]))

        if st.fault_reason is not None:
            self._send(_fault(st.fault_reason))
            return

        if action.endswith("/Enumerate"):
            self._send(_enum_ctx(resource))
        elif action.endswith("/Pull"):
            self._send(_pull(resource, self._instances(resource)))
        elif action.endswith("/transfer/Get"):
            self._send(_body(self._instance_xml(resource)))
        elif action.endswith("/transfer/Put"):
            self._record_put(resource, req)
            self._send(_body(self._instance_xml(resource)))
        else:  # a custom method invocation (…/<Method>)
            self._send(self._invoke(action, resource, req))

    # -- resource content ------------------------------------------------

    def _instances(self, resource: str) -> list[str]:
        st = self._state
        cls = resource.rsplit("/", 1)[-1]
        if cls == "CIM_AssociatedPowerManagementService":
            return [_inst("CIM_AssociatedPowerManagementService", {"PowerState": st.power_state})]
        if cls == "CIM_Chassis":
            return [_inst("CIM_Chassis", {
                "Manufacturer": st.manufacturer, "Model": st.model, "SerialNumber": st.serial})]
        if cls == "CIM_ComputerSystemPackage":
            return [_inst("CIM_ComputerSystemPackage", {"PlatformGUID": st.platform_guid})]
        if cls == "CIM_SoftwareIdentity":
            return [_inst("CIM_SoftwareIdentity", {"InstanceID": "AMT", "VersionString": st.amt_version})]
        return []

    def _instance_xml(self, resource: str) -> str:
        st = self._state
        cls = resource.rsplit("/", 1)[-1]
        if cls == "AMT_SetupAndConfigurationService":
            return _inst("AMT_SetupAndConfigurationService", {
                "ProvisioningState": st.provisioning_state, "CoreVersion": st.amt_version})
        if cls == "AMT_BootSettingData":
            return _inst("AMT_BootSettingData", {
                "BIOSSetup": st.bios_setup, "BIOSPause": "false", "BootMediaIndex": "0",
                "UseSOL": "false"})
        if cls == "CIM_BootConfigSetting":
            return _inst("CIM_BootConfigSetting", {
                "InstanceID": "Intel(r) AMT: Boot Configuration 0", "BootOrder": st.boot_order})
        return _inst(cls, {})

    def _record_put(self, resource: str, req: ET.Element) -> None:
        if resource.rsplit("/", 1)[-1] == "AMT_BootSettingData":
            self._state.bios_setup = _text(req, "BIOSSetup") or self._state.bios_setup

    def _invoke(self, action: str, resource: str, req: ET.Element) -> str:
        st = self._state
        method = action.rsplit("/", 1)[-1]
        if method == "RequestPowerStateChange":
            st.last_power_request = _text(req, "PowerState")
            # Reflect the new state so a follow-up is_powered_on() sees it.
            st.power_state = "2" if st.last_power_request == "2" else "8"
        elif method == "ChangeBootOrder":
            # The forced source is the Selector inside the <Source> EPR (a "Force…"
            # InstanceID). The header SelectorSet names the boot-config instance, so
            # pick the source selector specifically (empty for none/bios).
            sels = [c.text for c in req.iter() if c.tag.rsplit("}", 1)[-1] == "Selector"]
            st.boot_order = next((s for s in sels if s and "Force" in s), "")
        return _body(_inst(f"{method}_OUTPUT", {"ReturnValue": "0"}))


# -- SOAP builders --------------------------------------------------------


def _body(inner: str) -> str:
    return f'<s:Envelope xmlns:s="{_S}"><s:Body>{inner}</s:Body></s:Envelope>'


def _fault(reason: str) -> str:
    return _body(
        f'<s:Fault xmlns:s="{_S}"><s:Code><s:Value>s:Sender</s:Value></s:Code>'
        f"<s:Reason><s:Text>{reason}</s:Text></s:Reason></s:Fault>"
    )


def _enum_ctx(resource: str) -> str:
    return _body(
        f'<wsen:EnumerateResponse xmlns:wsen="{_WSEN}">'
        f"<wsen:EnumerationContext>ctx-{resource.rsplit('/', 1)[-1]}</wsen:EnumerationContext>"
        "</wsen:EnumerateResponse>"
    )


def _pull(resource: str, instances: list[str]) -> str:
    return _body(
        f'<wsen:PullResponse xmlns:wsen="{_WSEN}"><wsen:Items>{"".join(instances)}</wsen:Items>'
        "<wsen:EndOfSequence/></wsen:PullResponse>"
    )


def _inst(cls: str, fields: dict[str, str]) -> str:
    body = "".join(f"<g:{k}>{v}</g:{k}>" for k, v in fields.items())
    return f'<g:{cls} xmlns:g="{_G}">{body}</g:{cls}>'


def _text(el: ET.Element, local: str) -> str | None:
    for c in el.iter():
        if c.tag.rsplit("}", 1)[-1] == local:
            return c.text
    return None


class AmtEmulator:
    """Context-manager WS-Man emulator on an ephemeral loopback port."""

    def __init__(self) -> None:
        self.state = AmtState()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.state = self.state  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def host(self) -> str:
        return self._httpd.server_address[0]

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    def __enter__(self) -> AmtEmulator:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=2)
