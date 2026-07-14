"""
A pure-stdlib fake DMTF Redfish service for hardware-free driver tests.

Serves a coherent, hyperlinked tree with **non-trivial member ids** (e.g.
``/redfish/v1/Systems/Self.1``, not ``1``) so the driver's discovery is genuinely
exercised — a client that assumes ``1`` will not find anything. Power/boot
state, virtual-media insertion, sensor shape, async-vs-sync reset, and a
PasswordChangeRequired flow are all driven by a mutable ``RedfishState``.

No Docker, no third-party deps — a ThreadingHTTPServer on 127.0.0.1.
"""

from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SYS = "/redfish/v1/Systems/Self.1"
CHAS = "/redfish/v1/Chassis/Chas.1"
MGR = "/redfish/v1/Managers/BMC.1"
# Decoy members listed FIRST in their collections when multi_node is set, so
# index-0 selection picks the wrong node and only Links-based resolution is right.
DECOY_CHAS = "/redfish/v1/Chassis/Enclosure.0"
DECOY_MGR = "/redfish/v1/Managers/Enclosure.0"
RESET = f"{SYS}/Actions/ComputerSystem.Reset"
VM_COLL = f"{MGR}/VirtualMedia"
VM_CD = f"{VM_COLL}/CD"
VM_INSERT = f"{VM_CD}/Actions/VirtualMedia.InsertMedia"
VM_EJECT = f"{VM_CD}/Actions/VirtualMedia.EjectMedia"
# Deliberately NOT "{MGR}/LogServices" — forces the driver to follow the
# advertised LogServices @odata.id link rather than fabricate the path.
LOGS = f"{MGR}/LogServiceColl"
LOG_LCLOG = f"{LOGS}/Lclog"
LOG_ENTRIES = f"{LOG_LCLOG}/Entries"
SESSIONS = "/redfish/v1/SessionService/Sessions"
TASK = "/redfish/v1/TaskService/Tasks/1"

# PushPowerButton is a state toggle (DSP0268), NOT unconditionally off — a
# spec-accurate emulator is what catches the intent-inversion regression.
_OFF_TYPES = {"ForceOff", "GracefulShutdown"}
_ON_TYPES = {"On", "ForceOn"}


class RedfishState:
    """Mutable knobs + captured requests, shared across handler instances."""

    def __init__(self) -> None:
        self.power_state = "Off"
        self.boot_progress = "OSRunning"
        self.inserted = False
        self.last_image: str | None = None
        self.sensors_mode = "thermal"  # or "unified"
        self.reset_async = False
        self.password_change_required = False
        self.session_send_location = True  # if False, only the body @odata.id carries the URI
        self.task_state = "Completed"
        self.task_status = "OK"
        self.task_gc = False  # if True, GET on the task monitor 404s (iDRAC/iLO GC)
        self.reset_allowable = ["On", "ForceOff", "GracefulShutdown",
                                "GracefulRestart", "ForceRestart"]
        self.fail_status: int | None = None
        self.fail_times = 0
        # If set, a reset POST applies the state transition (as if a concurrent
        # actor beat us to it) but returns this status — models a BMC that 400/409s
        # a reset already in the requested state (iLO InvalidOperationForSystemState).
        self.reset_reject_status: int | None = None
        self.calls: list[tuple[str, str]] = []
        self.posts: list[tuple[str, dict]] = []
        self.last_headers: dict[str, str] = {}
        self.session_deleted = False
        # Session-token state: each Sessions POST issues a fresh token, and a
        # token-bearing request is validated against it (real BMCs do; the old
        # emulator never checked). expire_token_once rejects the next
        # token-bearing request with 401, modelling a DSP0266 idle timeout.
        self.valid_token: str | None = None
        self.token_seq = 0
        self.expire_token_once = False
        # A real BMC rejects a session create (or Basic request) with the wrong
        # credentials — validated so a dropped/garbled-credential regression fails.
        self.expected_user = "admin"
        self.expected_passwd = "secret"
        # VirtualMedia strictness knobs (real BMCs vary): reject the optional
        # Inserted/WriteProtected params (Supermicro), or require
        # TransferProtocolType (sushy bug #2072805).
        self.vm_reject_optional_params = False
        self.vm_require_transfer_protocol = False
        # Accept InsertMedia (2xx) but never set Inserted — a silent media no-op,
        # the #169 verify-path failure mode.
        self.vm_insert_noop = False
        # Sessions POST returns a token but neither a Location header nor a body
        # @odata.id — the session URI is unknowable, logout can't DELETE (#169).
        self.session_no_uri = False
        # When set, the Chassis/Managers collections list a decoy member first;
        # only ComputerSystem.Links.Chassis/ManagedBy point at the real node.
        self.multi_node = False
        # Override the LogService entries (to test time-based seek). None = default.
        self.log_entries: list[dict] | None = None
        # If True, ServiceRoot advertises $expand and the Sensors collection
        # serves its members inline for a ?$expand request (one GET, no fan-out).
        self.sensors_expandable = False
        # ComputerSystem.Boot (BootSourceOverride) state + quirk knobs.
        self.boot_override_enabled = "Disabled"   # Disabled | Once | Continuous
        self.boot_override_target = "None"        # None|Pxe|Cd|Hdd|Usb|BiosSetup|Diags
        self.boot_override_mode = "UEFI"          # UEFI | Legacy
        self.boot_allowable = ["None", "Pxe", "Cd", "Hdd", "Usb", "BiosSetup", "Diags"]
        # boot_expose_mode=False models older iLO4/iDRAC7 with no
        # BootSourceOverrideMode field; boot_patch_rejects_mode models a BMC that
        # 400s a PATCH that includes a mode it won't accept (driver must retry
        # without it). boot_patch_status: 200 (return resource) | 204 | 202 async.
        self.boot_expose_mode = True
        self.boot_patch_rejects_mode = False
        self.boot_patch_status = 200
        # boot_patch_fail_status: a boot PATCH returns this status for ANY body
        # (a non-mode failure — 500, or a 400 unrelated to the mode property — so
        # the driver must NOT swallow it as the mode-retry case). boot_absent: the
        # ComputerSystem exposes no Boot object at all (a minimal BMC).
        self.boot_patch_fail_status: int | None = None
        self.boot_absent = False
        self.patches: list[tuple[str, dict]] = []


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: object) -> None:  # silence test output
        pass

    @property
    def _state(self) -> RedfishState:
        return self.server.state  # type: ignore[attr-defined]

    def _send(self, body: dict | None, status: int = 200, headers: dict | None = None) -> None:
        data = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _path(self) -> str:
        return self.path.split("?", 1)[0].rstrip("/") or "/redfish/v1"

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except json.JSONDecodeError:
            return {}

    def _pre(self) -> bool:
        st = self._state
        st.last_headers = {k.lower(): v for k, v in self.headers.items()}
        st.calls.append((self.command, self._path()))
        if st.fail_times > 0 and st.fail_status:
            st.fail_times -= 1
            self._send({"error": {"message": "transient"}}, status=st.fail_status)
            return True
        # Session-auth enforcement. A protected resource requires either a valid
        # X-Auth-Token or HTTP Basic; the public login surface (service root +
        # the Sessions collection) needs neither. Real BMCs enforce this; the old
        # emulator validated nothing, so session expiry/re-login was untestable.
        path = self._path()
        if path not in ("/redfish/v1", SESSIONS):
            token = self.headers.get("X-Auth-Token")
            has_basic = any(k.lower() == "authorization" for k in self.headers)
            if token is not None and st.expire_token_once:
                st.expire_token_once = False
                st.valid_token = None  # the BMC forgot this session
                self._send({"error": {"message": "session expired"}}, status=401)
                return True
            if token is not None:
                if token != st.valid_token:
                    self._send({"error": {"message": "invalid or expired token"}}, status=401)
                    return True
            elif has_basic:
                if not self._basic_ok():
                    self._send({"error": {"message": "invalid credentials"}}, status=401)
                    return True
            else:
                # Session mode, protected resource, no credentials at all
                # (e.g. a token cleared by close()): unauthenticated.
                self._send({"error": {"message": "no session token"}}, status=401)
                return True
        return False

    def _basic_ok(self) -> bool:
        st = self._state
        raw = self.headers.get("Authorization", "")
        if not raw.startswith("Basic "):
            return False
        try:
            user, _, passwd = base64.b64decode(raw[6:]).decode().partition(":")
        except (ValueError, UnicodeDecodeError):
            return False
        return user == st.expected_user and passwd == st.expected_passwd

    # -- GET -------------------------------------------------------------

    def do_GET(self) -> None:
        if self._pre():
            return
        path = self._path()
        # $expand on the Sensors collection: serve members inline (one GET).
        if "$expand" in self.path and path == f"{CHAS}/Sensors" and self._state.sensors_expandable:
            self._send({"Members": [self._doc(f"{CHAS}/Sensors/CPUTemp"),
                                    self._doc(f"{CHAS}/Sensors/Fan1")]})
            return
        doc = self._doc(path)
        if doc is None:
            self._send({"error": {"message": "not found"}}, status=404)
        else:
            self._send(doc)

    def _doc(self, path: str) -> dict | None:
        st = self._state
        if path in ("/redfish/v1", "/redfish/v1/"):
            root = {
                "@odata.type": "#ServiceRoot.v1_15_0.ServiceRoot",
                "RedfishVersion": "1.15.1",
                "Systems": {"@odata.id": "/redfish/v1/Systems"},
                "Chassis": {"@odata.id": "/redfish/v1/Chassis"},
                "Managers": {"@odata.id": "/redfish/v1/Managers"},
                "SessionService": {"@odata.id": "/redfish/v1/SessionService"},
                "Links": {"Sessions": {"@odata.id": SESSIONS}},
            }
            if st.sensors_expandable:
                root["ProtocolFeaturesSupported"] = {
                    "ExpandQuery": {"ExpandAll": True, "Levels": True, "MaxLevels": 6}
                }
            return root
        if path == "/redfish/v1/Systems":
            return self._collection([SYS])
        if path == SYS:
            return self._computer_system()
        if path == "/redfish/v1/Chassis":
            return self._collection([DECOY_CHAS, CHAS] if st.multi_node else [CHAS])
        if path == DECOY_CHAS:
            return {"@odata.id": DECOY_CHAS, "ChassisType": "Enclosure"}  # no Thermal/Power/Sensors
        if path == CHAS:
            return self._chassis()
        if path == f"{CHAS}/Thermal":
            return {
                "Temperatures": [{"Name": "CPU", "ReadingCelsius": 42,
                                  "Status": {"Health": "OK"}}],
                "Fans": [{"Name": "Fan1", "Reading": 4200, "ReadingUnits": "RPM",
                          "Status": {"Health": "OK"}}],
            }
        if path == f"{CHAS}/Power":
            return {
                "Voltages": [{"Name": "VCC", "ReadingVolts": 12.1, "Status": {"Health": "OK"}}],
                "PowerControl": [{"Name": "Sys", "PowerConsumedWatts": 210,
                                  "Status": {"Health": "OK"}}],
            }
        if path == f"{CHAS}/Sensors":
            return self._collection([f"{CHAS}/Sensors/CPUTemp", f"{CHAS}/Sensors/Fan1"])
        if path == f"{CHAS}/Sensors/CPUTemp":
            return {"Name": "CPU Temp", "Reading": 42, "ReadingUnits": "Cel",
                    "ReadingType": "Temperature", "Status": {"Health": "OK"}}
        if path == f"{CHAS}/Sensors/Fan1":
            return {"Name": "Fan1", "Reading": 4200, "ReadingUnits": "RPM",
                    "ReadingType": "Rotational", "Status": {"Health": "OK"}}
        if path == "/redfish/v1/Managers":
            return self._collection([DECOY_MGR, MGR] if st.multi_node else [MGR])
        if path == DECOY_MGR:
            return {"@odata.id": DECOY_MGR}  # no VirtualMedia/LogServices
        if path == MGR:
            return {"@odata.id": MGR, "VirtualMedia": {"@odata.id": VM_COLL},
                    "LogServices": {"@odata.id": LOGS}}
        if path == VM_COLL:
            return self._collection([VM_CD])
        if path == VM_CD:
            return {
                "@odata.id": VM_CD, "MediaTypes": ["CD", "DVD"],
                "Inserted": st.inserted, "Image": st.last_image,
                "Actions": {
                    "#VirtualMedia.InsertMedia": {"target": VM_INSERT},
                    "#VirtualMedia.EjectMedia": {"target": VM_EJECT},
                },
            }
        if path == LOGS:
            return self._collection([LOG_LCLOG])
        if path == LOG_LCLOG:
            return {"@odata.id": LOG_LCLOG, "Entries": {"@odata.id": LOG_ENTRIES}}
        if path == LOG_ENTRIES:
            if st.log_entries is not None:
                return {"Members": st.log_entries}
            return {
                "Members": [
                    {"Created": "2026-06-27T00:00:00Z", "Severity": "OK",
                     "MessageId": "Base.1.0.Test", "Message": "system booted"},
                    {"Created": "2026-06-27T00:01:00Z", "MessageSeverity": "Warning",
                     "MessageId": "Base.1.0.Warn", "Message": "fan slow"},
                ],
            }
        if path == TASK:
            if st.task_gc:
                return None  # 404 — finished task garbage-collected
            return {"TaskState": st.task_state, "TaskStatus": st.task_status}
        return None

    def _collection(self, members: list[str]) -> dict:
        return {"Members@odata.count": len(members),
                "Members": [{"@odata.id": m} for m in members]}

    def _computer_system(self) -> dict:
        st = self._state
        reset: dict = {"target": RESET, "ResetType@Redfish.AllowableValues": st.reset_allowable}
        boot: dict = {
            "BootSourceOverrideEnabled": st.boot_override_enabled,
            "BootSourceOverrideTarget": st.boot_override_target,
            "BootSourceOverrideTarget@Redfish.AllowableValues": st.boot_allowable,
        }
        if st.boot_expose_mode:
            boot["BootSourceOverrideMode"] = st.boot_override_mode
        doc = {
            "@odata.id": SYS,
            "@odata.type": "#ComputerSystem.v1_20_0.ComputerSystem",
            "Manufacturer": "ACME", "Model": "Server 9000", "SerialNumber": "SN-1",
            "UUID": "00000000-0000-0000-0000-000000000001", "BiosVersion": "2.1.0",
            "PowerState": st.power_state,
            "Status": {"Health": "OK", "State": "Enabled"},
            "BootProgress": {"LastState": st.boot_progress},
            "Boot": boot,
            # DSP0268 associations — the driver resolves chassis/manager from
            # these, not by indexing the global collections.
            "Links": {"Chassis": [{"@odata.id": CHAS}], "ManagedBy": [{"@odata.id": MGR}]},
            "VirtualMedia": {"@odata.id": VM_COLL},
            # No system-side LogServices link here — logs live under the Manager
            # (Dell shape); the driver must scan both and follow the link.
            "Actions": {"#ComputerSystem.Reset": reset},
        }
        if st.boot_absent:
            doc.pop("Boot")
        return doc

    def _chassis(self) -> dict:
        doc = {"@odata.id": CHAS}
        if self._state.sensors_mode == "unified":
            doc["Sensors"] = {"@odata.id": f"{CHAS}/Sensors"}
        else:
            doc["Thermal"] = {"@odata.id": f"{CHAS}/Thermal"}
            doc["Power"] = {"@odata.id": f"{CHAS}/Power"}
        return doc

    # -- POST / DELETE ---------------------------------------------------

    def do_POST(self) -> None:
        if self._pre():
            return
        path = self._path()
        body = self._read_body()
        st = self._state
        st.posts.append((path, body))
        if path == SESSIONS:
            if body.get("UserName") != st.expected_user or body.get("Password") != st.expected_passwd:
                self._send({"error": {"message": "invalid credentials"}}, status=401)
                return
            st.token_seq += 1
            token = f"tok-redfish-{st.token_seq}"
            st.valid_token = token
            resp: dict = {"UserName": "admin", "@odata.id": f"{SESSIONS}/1"}
            if st.session_no_uri:
                del resp["@odata.id"]
            if st.password_change_required:
                resp["@Message.ExtendedInfo"] = [
                    {"MessageId": "Base.1.0.PasswordChangeRequired",
                     "Message": "change your password"}]
            headers = {"X-Auth-Token": token}
            if st.session_send_location and not st.session_no_uri:
                headers["Location"] = f"{SESSIONS}/1"
            self._send(resp, status=201, headers=headers)
            return
        if path == RESET:
            rt = body.get("ResetType", "")
            if rt in _ON_TYPES:
                st.power_state = "On"
            elif rt in _OFF_TYPES:
                st.power_state = "Off"
            elif rt == "PushPowerButton":
                # Pulse the power button: toggle the current state.
                st.power_state = "Off" if st.power_state == "On" else "On"
            else:  # restarts
                st.power_state = "On"
            if st.reset_reject_status:
                self._send({"error": {"message": "already in requested state"}},
                           status=st.reset_reject_status)
                return
            if st.reset_async:
                self._send(None, status=202, headers={"Location": TASK})
            else:
                self._send(None, status=204)
            return
        if path == VM_INSERT:
            if st.vm_reject_optional_params and ("Inserted" in body or "WriteProtected" in body):
                self._send({"error": {"@Message.ExtendedInfo": [
                    {"MessageId": "Base.1.8.ActionParameterNotSupported",
                     "Message": "Inserted is read-only on this system",
                     "MessageArgs": ["InsertMedia", "Inserted"]}]}}, status=400)
                return
            if st.vm_require_transfer_protocol and "TransferProtocolType" not in body:
                self._send({"error": {"@Message.ExtendedInfo": [
                    {"MessageId": "Base.1.8.ActionParameterMissing",
                     "Message": "TransferProtocolType is required",
                     "MessageArgs": ["InsertMedia", "TransferProtocolType"]}]}}, status=400)
                return
            if not st.vm_insert_noop:
                st.inserted = True
            st.last_image = body.get("Image")
            self._send(None, status=204)
            return
        if path == VM_EJECT:
            st.inserted = False
            st.last_image = None
            self._send(None, status=204)
            return
        # A real BMC 404s an unknown action target (a typo'd @odata.id/target).
        self._send({"error": {"message": "not found"}}, status=404)

    def do_PATCH(self) -> None:
        if self._pre():
            return
        path = self._path()
        body = self._read_body()
        st = self._state
        st.patches.append((path, body))
        if path != SYS:
            self._send({"error": {"message": "not found"}}, status=404)
            return
        boot = body.get("Boot")
        if not isinstance(boot, dict):
            self._send({"error": {"message": "unsupported PATCH target"}}, status=400)
            return
        if st.boot_patch_fail_status:
            self._send({"error": {"@Message.ExtendedInfo": [
                {"MessageId": "Base.1.0.InternalError",
                 "Message": "the BMC could not apply the boot override"}]}},
                status=st.boot_patch_fail_status)
            return
        target = boot.get("BootSourceOverrideTarget")
        if target is not None and target not in st.boot_allowable:
            self._send({"error": {"@Message.ExtendedInfo": [
                {"MessageId": "Base.1.0.PropertyValueNotInList",
                 "Message": f"{target} is not one of the allowable values",
                 "MessageArgs": [str(target), "BootSourceOverrideTarget"]}]}}, status=400)
            return
        if "BootSourceOverrideMode" in boot and st.boot_patch_rejects_mode:
            self._send({"error": {"@Message.ExtendedInfo": [
                {"MessageId": "Base.1.0.PropertyNotWritable",
                 "Message": "BootSourceOverrideMode is not writable on this system",
                 "MessageArgs": ["BootSourceOverrideMode"]}]}}, status=400)
            return
        # Apply the write.
        if "BootSourceOverrideEnabled" in boot:
            st.boot_override_enabled = boot["BootSourceOverrideEnabled"]
        if target is not None:
            st.boot_override_target = target
        if "BootSourceOverrideMode" in boot and st.boot_expose_mode:
            st.boot_override_mode = boot["BootSourceOverrideMode"]
        if st.boot_patch_status == 202:
            self._send(None, status=202, headers={"Location": TASK})
        elif st.boot_patch_status == 204:
            self._send(None, status=204)
        else:
            self._send(self._computer_system(), status=200)

    def do_DELETE(self) -> None:
        if self._pre():
            return
        if self._path() == f"{SESSIONS}/1":
            self._state.session_deleted = True
        self._send(None, status=204)


class RedfishEmulator:
    """Context manager running the fake Redfish service on an ephemeral port."""

    def __init__(self) -> None:
        self.state = RedfishState()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.state = self.state  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def host(self) -> str:
        return self._httpd.server_address[0]

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    def __enter__(self) -> RedfishEmulator:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=2)
