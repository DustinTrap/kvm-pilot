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

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SYS = "/redfish/v1/Systems/Self.1"
CHAS = "/redfish/v1/Chassis/Chas.1"
MGR = "/redfish/v1/Managers/BMC.1"
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

_OFF_TYPES = {"ForceOff", "GracefulShutdown", "PushPowerButton"}
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
        self.calls: list[tuple[str, str]] = []
        self.posts: list[tuple[str, dict]] = []
        self.last_headers: dict[str, str] = {}
        self.session_deleted = False


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
        return False

    # -- GET -------------------------------------------------------------

    def do_GET(self) -> None:
        if self._pre():
            return
        doc = self._doc(self._path())
        if doc is None:
            self._send({"error": {"message": "not found"}}, status=404)
        else:
            self._send(doc)

    def _doc(self, path: str) -> dict | None:
        st = self._state
        if path in ("/redfish/v1", "/redfish/v1/"):
            return {
                "@odata.type": "#ServiceRoot.v1_15_0.ServiceRoot",
                "RedfishVersion": "1.15.1",
                "Systems": {"@odata.id": "/redfish/v1/Systems"},
                "Chassis": {"@odata.id": "/redfish/v1/Chassis"},
                "Managers": {"@odata.id": "/redfish/v1/Managers"},
                "SessionService": {"@odata.id": "/redfish/v1/SessionService"},
                "Links": {"Sessions": {"@odata.id": SESSIONS}},
            }
        if path == "/redfish/v1/Systems":
            return self._collection([SYS])
        if path == SYS:
            return self._computer_system()
        if path == "/redfish/v1/Chassis":
            return self._collection([CHAS])
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
            return self._collection([MGR])
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
        return {
            "@odata.id": SYS,
            "@odata.type": "#ComputerSystem.v1_20_0.ComputerSystem",
            "Manufacturer": "ACME", "Model": "Server 9000", "SerialNumber": "SN-1",
            "UUID": "00000000-0000-0000-0000-000000000001", "BiosVersion": "2.1.0",
            "PowerState": st.power_state,
            "Status": {"Health": "OK", "State": "Enabled"},
            "BootProgress": {"LastState": st.boot_progress},
            "VirtualMedia": {"@odata.id": VM_COLL},
            # No system-side LogServices link here — logs live under the Manager
            # (Dell shape); the driver must scan both and follow the link.
            "Actions": {"#ComputerSystem.Reset": reset},
        }

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
            resp: dict = {"UserName": "admin", "@odata.id": f"{SESSIONS}/1"}
            if st.password_change_required:
                resp["@Message.ExtendedInfo"] = [
                    {"MessageId": "Base.1.0.PasswordChangeRequired",
                     "Message": "change your password"}]
            headers = {"X-Auth-Token": "tok-redfish-123"}
            if st.session_send_location:
                headers["Location"] = f"{SESSIONS}/1"
            self._send(resp, status=201, headers=headers)
            return
        if path == RESET:
            rt = body.get("ResetType", "")
            if rt in _ON_TYPES:
                st.power_state = "On"
            elif rt in _OFF_TYPES:
                st.power_state = "Off"
            else:  # restarts
                st.power_state = "On"
            if st.reset_async:
                self._send(None, status=202, headers={"Location": TASK})
            else:
                self._send(None, status=204)
            return
        if path == VM_INSERT:
            st.inserted = True
            st.last_image = body.get("Image")
            self._send(None, status=204)
            return
        if path == VM_EJECT:
            st.inserted = False
            st.last_image = None
            self._send(None, status=204)
            return
        self._send(None, status=204)

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
