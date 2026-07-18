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
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.etree import ElementTree as ET

_S = "http://www.w3.org/2003/05/soap-envelope"
_WSEN = "http://schemas.xmlsoap.org/ws/2004/09/enumeration"
_G = "http://intel.com/wbem/wscim/1/amt-schema/1/emu"  # response instance namespace (any)


class AmtState:
    """Mutable knobs + captured requests shared with the request handler."""

    def __init__(self) -> None:
        self.power_state = "8"          # CIM PowerState: 2=on, 8=off-soft
        self.power_state_missing = False  # power instance omits PowerState (item: is_powered_on raise)
        self.last_power_request: str | None = None
        self.boot_order = ""            # substring compared against the source id
        self.boot_order_readable = True  # real AMT omits BootOrder (write-only) — flip to test honesty
        self.bios_setup = "false"
        # redirection / KVM enablement knobs
        self.redir_listener = "false"   # AMT_RedirectionService.ListenerEnabled
        self.redir_state = "32771"      # EnabledState 32771 = IDER+SOL both
        self.kvm_5900 = "false"         # IPS_KVMRedirectionSettingData.Is5900PortEnabled
        self.kvm_optin_policy = "true"
        self.kvm_session_timeout = "3"
        self.kvm_rfb_password: str | None = None
        self.kvm_sap_state = "3"        # CIM_KVMRedirectionSAP.EnabledState: 3=disabled, 6=enabled
        self.kvm_sap_requested: str | None = None
        self.kvm_sap_requests: list[str] = []  # every RequestedState, in order (proves a 3->2 cycle)
        self.optin_required = "1"       # IPS_OptInService: 0=none, 1=KVM, 4294967295=all
        self.control_mode = "2"         # IPS_HostBasedSetupService: 1=CCM, 2=ACM
        self.provisioning_state = "2"   # 2 = post/provisioned
        self.amt_version = "16.1.25"
        self.manufacturer = "Dell Inc."
        self.model = "Latitude 5411"
        self.serial = "REDACTED"
        self.platform_guid = "4c4c4544-0058-1234-5678-abcdef012345"
        self.require_auth = False       # issue an HTTP Digest challenge first
        self.reject_auth = False        # 401 even after the client authenticates
        self.fault_reason: str | None = None  # return a SOAP fault for every op
        # -- transport-fault taxonomy (WS-Man error mapping) ----------------
        self.http_status: int | None = None   # answer with this HTTP status (e.g. 500)
        self.error_body: str | None = None     # body for http_status (defaults to a fault of fault_reason)
        self.garbage_body = False               # 200 with a non-XML body -> ProtocolError
        self.delay = 0.0                         # sleep before responding (drives a read timeout)
        # -- method / enumeration behaviour ---------------------------------
        self.nonzero_methods: set[str] = set()  # Invoke methods that return ReturnValue != 0
        self.enum_no_context = False             # Enumerate omits the EnumerationContext
        self.enum_pages = False                  # paginate Pull (continuation, then EndOfSequence)
        self.pull_count = 0                      # Pull requests seen (pagination assertions)
        self.suppress_classes: set[str] = set()  # enumerate returns [] for these classes
        self.amt_setup_no_version = False        # AMT_SetupAndConfigurationService omits the version
        self.swid_no_amt = False                 # CIM_SoftwareIdentity carries no AMT identity
        self.calls: list[tuple[str, str]] = []  # (action_tail, resource_tail)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:  # silence test output
        pass

    @property
    def _state(self) -> AmtState:
        return self.server.state  # type: ignore[attr-defined]

    def _send(self, body: str, status: int = 200, headers: dict | None = None) -> None:
        raw = body.encode("utf-8")
        try:  # the client may have already timed out and gone (see the `delay` knob)
            self.send_response(status)
            self.send_header("Content-Type", "application/soap+xml;charset=UTF-8")
            self.send_header("Content-Length", str(len(raw)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw)
        except OSError:
            pass

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

        # Transport-layer faults short-circuit before any SOAP is parsed — the
        # driver must map each onto the right kvm-pilot error type.
        if st.delay:
            time.sleep(st.delay)  # client read-times-out; _send swallows the dead-socket write
        if st.http_status is not None:
            body = st.error_body if st.error_body is not None else _fault(st.fault_reason or "server error")
            self._send(body, st.http_status)
            return
        if st.garbage_body:
            self._send("this is not XML at all >>>")
            return

        req = ET.fromstring(raw)  # nosec B314 - test input
        action = _text(req, "Action") or ""
        resource = _text(req, "ResourceURI") or ""
        st.calls.append((action.rsplit("/", 1)[-1], resource.rsplit("/", 1)[-1]))

        if st.fault_reason is not None:
            self._send(_fault(st.fault_reason))
            return

        if action.endswith("/Enumerate"):
            self._send(_enum_no_ctx() if st.enum_no_context else _enum_ctx(resource))
        elif action.endswith("/Pull"):
            self._send(self._pull_response(resource))
        elif action.endswith("/transfer/Get"):
            self._send(_body(self._instance_xml(resource)))
        elif action.endswith("/transfer/Put"):
            self._record_put(resource, req)
            self._send(_body(self._instance_xml(resource)))
        else:  # a custom method invocation (…/<Method>)
            self._send(self._invoke(action, resource, req))

    # -- resource content ------------------------------------------------

    def _pull_response(self, resource: str) -> str:
        """Body for a Pull. With ``enum_pages``, the first Pull returns the items
        plus a *continuation* context (no EndOfSequence) so the driver pulls again."""
        st = self._state
        insts = self._instances(resource)
        if st.enum_pages:
            st.pull_count += 1
            if st.pull_count == 1:
                return _pull_page(resource, insts)  # items + continuation, no EndOfSequence
            return _pull(resource, [])              # EndOfSequence, nothing more
        return _pull(resource, insts)

    def _instances(self, resource: str) -> list[str]:
        st = self._state
        cls = resource.rsplit("/", 1)[-1]
        if cls in st.suppress_classes:
            return []  # model a firmware that simply omits the class
        if cls == "CIM_AssociatedPowerManagementService":
            fields = {} if st.power_state_missing else {"PowerState": st.power_state}
            return [_inst("CIM_AssociatedPowerManagementService", fields)]
        if cls == "CIM_Chassis":
            return [_inst("CIM_Chassis", {
                "Manufacturer": st.manufacturer, "Model": st.model, "SerialNumber": st.serial})]
        if cls == "CIM_ComputerSystemPackage":
            return [_inst("CIM_ComputerSystemPackage", {"PlatformGUID": st.platform_guid})]
        if cls == "CIM_SoftwareIdentity":
            if st.swid_no_amt:  # no AMT-shaped identity -> _amt_version can't recover a version
                return [_inst("CIM_SoftwareIdentity", {"InstanceID": "BIOS", "VersionString": "P89"})]
            return [_inst("CIM_SoftwareIdentity", {"InstanceID": "AMT", "VersionString": st.amt_version})]
        return []

    def _instance_xml(self, resource: str) -> str:
        st = self._state
        cls = resource.rsplit("/", 1)[-1]
        if cls == "AMT_SetupAndConfigurationService":
            fields = {"ProvisioningState": st.provisioning_state}
            if not st.amt_setup_no_version:  # omit -> _amt_version falls back to CIM_SoftwareIdentity
                fields["CoreVersion"] = st.amt_version
            return _inst("AMT_SetupAndConfigurationService", fields)
        if cls == "AMT_BootSettingData":
            return _inst("AMT_BootSettingData", {
                "BIOSSetup": st.bios_setup, "BIOSPause": "false", "BootMediaIndex": "0",
                "UseSOL": "false"})
        if cls == "CIM_BootConfigSetting":
            fields = {"InstanceID": "Intel(r) AMT: Boot Configuration 0"}
            if st.boot_order_readable:  # real AMT omits BootOrder — the write-only reality
                fields["BootOrder"] = st.boot_order
            return _inst("CIM_BootConfigSetting", fields)
        if cls == "AMT_RedirectionService":
            return _inst("AMT_RedirectionService", {
                "CreationClassName": "AMT_RedirectionService", "Name": "Intel(r) AMT Redirection Service",
                "SystemName": "Intel(r) AMT", "SystemCreationClassName": "CIM_ComputerSystem",
                "EnabledState": st.redir_state, "ListenerEnabled": st.redir_listener})
        if cls == "IPS_KVMRedirectionSettingData":
            return _inst("IPS_KVMRedirectionSettingData", {
                "InstanceID": "Intel(r) KVM Redirection Settings",
                "ElementName": "Intel(r) KVM Redirection Settings", "EnabledByMEBx": "true",
                "Is5900PortEnabled": st.kvm_5900, "OptInPolicy": st.kvm_optin_policy,
                "SessionTimeout": st.kvm_session_timeout, "RFBPassword": st.kvm_rfb_password or "",
                "DefaultScreen": "0"})
        if cls == "CIM_KVMRedirectionSAP":
            return _inst("CIM_KVMRedirectionSAP", {
                "Name": "KVM Redirection Service Access Point", "SystemName": "ManagedSystem",
                "SystemCreationClassName": "CIM_ComputerSystem", "CreationClassName": "CIM_KVMRedirectionSAP",
                "EnabledState": st.kvm_sap_state})
        if cls == "IPS_OptInService":
            return _inst("IPS_OptInService", {
                "Name": "Intel(r) AMT OptIn Service", "CreationClassName": "IPS_OptInService",
                "SystemName": "Intel(r) AMT", "SystemCreationClassName": "CIM_ComputerSystem",
                "OptInRequired": st.optin_required, "OptInState": "0", "CanModifyOptInPolicy": "1"})
        if cls == "IPS_HostBasedSetupService":
            return _inst("IPS_HostBasedSetupService", {
                "CurrentControlMode": st.control_mode,
                "ElementName": "Intel(r) AMT Host Based Setup Service"})
        return _inst(cls, {})

    def _record_put(self, resource: str, req: ET.Element) -> None:
        st = self._state
        cls = resource.rsplit("/", 1)[-1]
        if cls == "AMT_BootSettingData":
            st.bios_setup = _text(req, "BIOSSetup") or st.bios_setup
        elif cls == "AMT_RedirectionService":
            st.redir_listener = _text(req, "ListenerEnabled") or st.redir_listener
            st.redir_state = _text(req, "EnabledState") or st.redir_state
        elif cls == "IPS_KVMRedirectionSettingData":
            st.kvm_5900 = _text(req, "Is5900PortEnabled") or st.kvm_5900
            st.kvm_optin_policy = _text(req, "OptInPolicy") or st.kvm_optin_policy
            st.kvm_session_timeout = _text(req, "SessionTimeout") or st.kvm_session_timeout
            st.kvm_rfb_password = _text(req, "RFBPassword")
        elif cls == "IPS_OptInService":
            st.optin_required = _text(req, "OptInRequired") or st.optin_required

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
        elif method == "RequestStateChange" and resource.rsplit("/", 1)[-1] == "CIM_KVMRedirectionSAP":
            st.kvm_sap_requested = _text(req, "RequestedState")
            if st.kvm_sap_requested is not None:
                st.kvm_sap_requests.append(st.kvm_sap_requested)
            st.kvm_sap_state = "6" if st.kvm_sap_requested == "2" else "3"
        # A non-zero ReturnValue on an HTTP-200 = the ME accepted the SOAP but
        # refused the op (bad power package, unprovisioned, etc.).
        rv = "1" if method in st.nonzero_methods else "0"
        return _body(_inst(f"{method}_OUTPUT", {"ReturnValue": rv}))


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


def _enum_no_ctx() -> str:
    # An Enumerate with no context at all — the driver's pull loop breaks immediately.
    return _body(f'<wsen:EnumerateResponse xmlns:wsen="{_WSEN}"/>')


def _pull(resource: str, instances: list[str]) -> str:
    return _body(
        f'<wsen:PullResponse xmlns:wsen="{_WSEN}"><wsen:Items>{"".join(instances)}</wsen:Items>'
        "<wsen:EndOfSequence/></wsen:PullResponse>"
    )


def _pull_page(resource: str, instances: list[str]) -> str:
    # A non-final page: items plus a fresh continuation context, and NO
    # EndOfSequence — the driver must Pull again to finish the enumeration.
    return _body(
        f'<wsen:PullResponse xmlns:wsen="{_WSEN}"><wsen:Items>{"".join(instances)}</wsen:Items>'
        f"<wsen:EnumerationContext>more-{resource.rsplit('/', 1)[-1]}</wsen:EnumerationContext>"
        "</wsen:PullResponse>"
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
