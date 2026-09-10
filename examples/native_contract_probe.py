#!/usr/bin/env python3
"""Record the native FreeCAD contract through the embedded MCP server.

This is a stdlib-only MCP client. It never imports the add-on.

Modes
    (default)   Verification run. Executes every probe except the
                deliberately invalid constraint constructions. On a
                confirmed FreeCAD crash it records the journal entry,
                captures the crash report, prints the reason, and exits
                non-zero. It never restarts FreeCAD and never retries a
                crashed step. tests/native_contract.json is rewritten only
                when no step crashed.
    --dev       Test development. Same probes as the default run, but a
                confirmed crash restarts FreeCAD, recreates the probe
                document, and resumes at the next step. Bounded at 12
                restarts per run.
    --sweep     Implies --dev and adds the deliberately invalid D1
                constraint constructions, one step per candidate, so an
                abort is recorded as an observation and recovered through
                a restart. Merges only probes["constraint.forms"] into
                tests/native_contract.json.

Crash handling
    A transport failure is only a crash candidate. The failure is
    confirmed as a crash when pgrep reports no FreeCAD process within
    5 seconds. When the process is alive the step records outcome
    "error" and is retried once. Crash evidence is taken from
    ~/Library/Logs/DiagnosticReports/freecad-*.ips files newer than the
    process start of this run.

Isolation
    Every run creates one document named MCP_NativeContract_<pid> and
    closes it at the end. No other document is opened, saved, closed, or
    modified. Probe output files are written under .native-contract/tmp/.

Environment
    FREECAD_MCP_URL    Server URL; default http://127.0.0.1:9876/mcp.
    FREECAD_MCP_TOKEN  Bearer token for remote mode. Never printed.
"""

from __future__ import annotations

import argparse
import datetime
import glob
import http.client
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse

PROTOCOL_VERSION = "2026-07-28"
HTTP_TIMEOUT_S = 120.0
RESTART_POLL_S = 180.0
MAX_RESTARTS = 12

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTRACT_DIR = os.path.join(REPO, ".native-contract")
TMP_DIR = os.path.join(CONTRACT_DIR, "tmp")
JOURNAL_PATH = os.path.join(CONTRACT_DIR, "journal.jsonl")
RECORD_PATH = os.path.join(REPO, "tests", "native_contract.json")
DIAG_DIR = os.path.expanduser("~/Library/Logs/DiagnosticReports")

SESSION_ID = "native-contract-probe"
RESULT_PREFIX = "PROBE_RESULT:"

# The plan's "pgrep -f FreeCAD" would also match this probe process,
# because this repository path itself contains "FreeCAD". The app-binary
# pattern is the same check without the self-match.
PGREP_PATTERN = "FreeCAD.app"


class ProtocolFailure(Exception):
    """A protocol-level JSON-RPC error from the server."""


class UnparseableResponse(Exception):
    """The response body could not be parsed; a crash candidate."""


class ToolFailure(Exception):
    """A complete tool result with isError:true."""

    def __init__(self, tool: str, error: dict) -> None:
        self.error = error
        super().__init__(f"{tool}: {error.get('code')}: {error.get('message')}")


class ProbeFatal(Exception):
    """An unrecoverable run-level condition; message is the reason."""


class VerificationCrash(Exception):
    """A confirmed crash during a verification run; terminal failure."""

    def __init__(self, step: str, journal_path: str, report_path: str | None) -> None:
        self.step = step
        self.journal_path = journal_path
        self.report_path = report_path
        super().__init__(step)


TRANSPORT_ERRORS = (
    ConnectionResetError,
    ConnectionRefusedError,
    TimeoutError,
    http.client.RemoteDisconnected,
    UnparseableResponse,
)


# ---------------------------------------------------------------------------
# Client.
# ---------------------------------------------------------------------------


class FreeCadMcpClient:
    """Minimal stdlib MCP client for the embedded FreeCAD server."""

    def __init__(self, url: str, token: str) -> None:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http",) or not parsed.path:
            raise ValueError(f"expected an http URL with a path, got {url!r}")
        self._host = parsed.hostname or "127.0.0.1"
        self._port = parsed.port or 80
        self._path = parsed.path
        self._token = token
        self._next_id = 0

    def _headers(self, method: str, name: str | None) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "Mcp-Method": method,
        }
        if name is not None:
            headers["Mcp-Name"] = name
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _meta(self) -> dict:
        return {
            "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
            "io.modelcontextprotocol/clientInfo": {
                "name": "native-contract-probe",
                "version": "1",
            },
            "io.modelcontextprotocol/clientCapabilities": {
                "elicitation": {"form": {}},
            },
        }

    def _read_response(self, conn: http.client.HTTPConnection, request_id: int) -> dict:
        """Return the full JSON-RPC response for ``request_id``.

        Handles both direct JSON bodies and SSE final events.
        """
        response = conn.getresponse()
        content_type = (response.getheader("Content-Type") or "").lower()
        body = response.read()
        try:
            if "text/event-stream" in content_type:
                events = []
                for raw_line in body.decode("utf-8").splitlines():
                    if raw_line.startswith("data:"):
                        events.append(raw_line[5:].strip())
                if not events:
                    raise UnparseableResponse("SSE response carried no data events")
                message = json.loads(events[-1])
            else:
                message = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise UnparseableResponse(f"{type(exc).__name__}: {exc}") from None
        if message.get("id") != request_id:
            raise ProtocolFailure(
                f"response id {message.get('id')!r} does not match request {request_id}"
            )
        if "error" in message:
            error = message["error"]
            raise ProtocolFailure(
                f"{error.get('code')}: {error.get('message')}"
                + (f" ({error.get('data')!r})" if error.get("data") else "")
            )
        return message["result"]

    def request(
        self,
        method: str,
        params: dict | None = None,
        name: str | None = None,
        timeout: float = HTTP_TIMEOUT_S,
    ) -> dict:
        """One JSON-RPC request; returns the full result object."""
        self._next_id += 1
        request_id = self._next_id
        envelope: dict = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": {"_meta": self._meta(), **(params or {})},
        }
        body = json.dumps(envelope).encode("utf-8")
        conn = http.client.HTTPConnection(self._host, self._port, timeout=timeout)
        try:
            conn.request("POST", self._path, body=body, headers=self._headers(method, name))
            return self._read_response(conn, request_id)
        finally:
            conn.close()

    def call_tool(self, name: str, arguments: dict, timeout: float = HTTP_TIMEOUT_S) -> dict:
        """Call a tool, answering any consent elicitation with accept.

        Returns ``structuredContent`` from a complete result.
        """
        params: dict = {"name": name, "arguments": arguments}
        result = self.request("tools/call", params, name=name, timeout=timeout)
        if result.get("resultType") == "input_required":
            request_state = result.get("requestState")
            if request_state is None:
                raise ProtocolFailure("input_required result without a requestState token")
            params["requestState"] = request_state
            params["inputResponses"] = {
                "confirm": {"action": "accept", "content": {"confirmed": True}}
            }
            result = self.request("tools/call", params, name=name, timeout=timeout)
        if result.get("isError"):
            raise ToolFailure(name, result.get("structuredContent", {}).get("error", {}))
        if result.get("resultType") != "complete":
            raise ProtocolFailure(f"unexpected resultType: {result.get('resultType')!r}")
        return result.get("structuredContent", {})

    def run_script(
        self, code: str, session_id: str = SESSION_ID, timeout: float = HTTP_TIMEOUT_S
    ) -> dict:
        """Run FreeCAD Python code; returns stdout/stderr/executed."""
        return self.call_tool(
            "run_script", {"code": code, "session_id": session_id}, timeout=timeout
        )

    def discover_capabilities(self) -> dict:
        """Return the merged capabilities payload."""
        result = self.call_tool("discover_capabilities", {})
        caps = result.get("capabilities")
        if not isinstance(caps, dict):
            caps = {}
        gui = result.get("gui")
        if isinstance(gui, dict):
            caps["gui"] = gui
        return caps


# ---------------------------------------------------------------------------
# Journal and process helpers.
# ---------------------------------------------------------------------------


def now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="milliseconds")


def redact(value: object, token: str) -> object:
    """Drop any token material from journaled payloads."""
    if isinstance(value, dict):
        clean: dict = {}
        for key, item in value.items():
            if "token" in str(key).lower():
                clean[key] = "<redacted>"
            else:
                clean[key] = redact(item, token)
        return clean
    if isinstance(value, list):
        return [redact(item, token) for item in value]
    if isinstance(value, str) and token and token in value:
        return "<redacted>"
    return value


class Journal:
    """Append-only JSONL journal; every record is flushed and fsynced."""

    def __init__(self, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self.path = path
        self._fh = open(path, "a", encoding="utf-8")

    def record(self, payload: dict) -> None:
        entry = {"ts": now_iso(), **payload}
        self._fh.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def close(self) -> None:
        self._fh.close()


def freecad_pids() -> list[int]:
    out = subprocess.run(
        ["pgrep", "-f", PGREP_PATTERN], capture_output=True, text=True, check=False
    )
    return [int(line) for line in out.stdout.split() if line.strip().isdigit()]


def process_start_epoch(pid: int) -> float | None:
    out = subprocess.run(
        ["ps", "-p", str(pid), "-o", "etimes="], capture_output=True, text=True, check=False
    )
    try:
        return time.time() - float(out.stdout.strip())
    except ValueError:
        return None


def wait_alive_verdict(window_s: float) -> bool:
    """True when FreeCAD is still alive after ``window_s``; False once gone."""
    deadline = time.monotonic() + window_s
    while True:
        if not freecad_pids():
            return False
        if time.monotonic() >= deadline:
            return True
        time.sleep(0.5)


def wait_pgrep_empty(window_s: float) -> bool:
    """True when pgrep reports no FreeCAD process within the window."""
    deadline = time.monotonic() + window_s
    while True:
        if not freecad_pids():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(1.0)


def newest_crash_report(since_epoch: float) -> str | None:
    """Newest freecad-*.ips at or after ``since_epoch``; waits briefly."""
    deadline = time.monotonic() + 10.0
    while True:
        candidates = []
        for path in glob.glob(os.path.join(DIAG_DIR, "freecad-*.ips")):
            try:
                if os.path.getmtime(path) >= since_epoch - 1.0:
                    candidates.append(path)
            except OSError:
                continue
        if candidates:
            return max(candidates, key=os.path.getmtime)
        if time.monotonic() >= deadline:
            return None
        time.sleep(1.0)


def parse_ips(path: str) -> dict:
    """Extract the diagnostic digest from a two-document .ips file."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        meta = json.loads(fh.readline())
        body = json.loads(fh.read())
    frames: list[str] = []
    for thread in body.get("threads", []):
        if thread.get("triggered"):
            for frame in (thread.get("frames") or [])[:25]:
                symbol = frame.get("symbol") or f"image{frame.get('imageIndex')}"
                location = frame.get("symbolLocation", frame.get("imageOffset"))
                frames.append(f"{symbol} + {location}")
            break
    return {
        "captureTime": meta.get("captureTime"),
        "exception": body.get("exception"),
        "termination": body.get("termination"),
        "triggeredThreadFrames": frames,
    }


# ---------------------------------------------------------------------------
# D1 candidate matrix: (type, arguments, datum).
# datum selects the FreeCAD quantity appended after the arguments:
# "none" | "mm" -> Quantity("10 mm") | "deg" -> Quantity("10 deg")
# | "unit" -> Quantity("1.0").
# ---------------------------------------------------------------------------

DOCUMENTED_FORMS: list[tuple[str, tuple, str]] = [
    ("Coincident", (0, 2, 1, 1), "none"),
    ("Coincident", (0, 1, -1, 1), "none"),
    ("Coincident", (0, 1, -1, 0), "none"),  # documented as not working
    ("Horizontal", (0,), "none"),
    ("Vertical", (1,), "none"),
    ("Block", (2,), "none"),
    ("PointOnObject", (4, 1, 0), "none"),
    ("Parallel", (0, 1), "none"),
    ("Perpendicular", (0, 1), "none"),
    ("Equal", (2, 3), "none"),
    ("Tangent", (0, 2), "none"),
    ("Symmetric", (0, 1, 1, 2, -1), "none"),
    ("Symmetric", (0, 1, 1, 2, 0, 2), "none"),
    ("DistanceX", (0, 1, 1, 2), "mm"),
    ("DistanceX", (0, 1, 2), "mm"),
    ("DistanceY", (1, 1, 0, 2), "mm"),
    ("DistanceY", (1, 1, 2), "mm"),
    ("Distance", (2, 0), "mm"),
    ("Distance", (0, 1, 2), "mm"),
    ("Distance", (0, 1, 1, 2), "mm"),
    ("Radius", (2,), "mm"),
    ("Diameter", (2,), "mm"),
    ("Angle", (0, 1), "deg"),
    ("Angle", (0, 1, 0, 2), "deg"),
    ("Weight", (5, 1), "unit"),
]

# D1 crash reproductions: the datum-less four-token forms are what the
# structured path and run_script reach the native constructor with.
KNOWN_BAD_FORMS: list[tuple[str, tuple, str]] = [
    ("DistanceX", (0, 1, 1, 7), "none"),
    ("DistanceX", (0, 1, 1, 7.0), "none"),
]

WRONG_LENGTH_FORMS: list[tuple[str, tuple, str]] = [
    ("Coincident", (0, 2, 1), "none"),
    ("Horizontal", (0, 1), "none"),
    ("Vertical", (0, 1), "none"),
    ("Block", (0, 1), "none"),
    ("PointOnObject", (4, 1), "none"),
    ("Parallel", (0,), "none"),
    ("Perpendicular", (0,), "none"),
    ("Equal", (0,), "none"),
    ("Tangent", (0,), "none"),
    ("Symmetric", (0, 1, 1), "none"),
    ("DistanceX", (0, 1), "none"),
    ("DistanceY", (0, 1), "none"),
    ("Distance", (2,), "none"),
    ("Radius", (2, 0), "none"),
    ("Diameter", (2, 0), "none"),
    ("Angle", (0,), "none"),
    ("Weight", (5,), "none"),
]

CANDIDATE_FORMS = DOCUMENTED_FORMS + KNOWN_BAD_FORMS + WRONG_LENGTH_FORMS
DOCUMENTED_KEYS = {f"{ctype}:{json.dumps(list(args))}" for ctype, args, _ in DOCUMENTED_FORMS}


def candidate_key(candidate: tuple[str, tuple, str]) -> str:
    return f"{candidate[0]}:{json.dumps(list(candidate[1]))}"


SWEEP_FIXTURE = """
_name = "SweepSketch"
if doc.getObject(_name) is not None:
    doc.removeObject(_name)
    doc.recompute()
_sk = doc.addObject("Sketcher::SketchObject", _name)
_sk.addGeometry(Part.LineSegment(Vector(0, 0, 0), Vector(40, 0, 0)), False)
_sk.addGeometry(Part.LineSegment(Vector(40, 0, 0), Vector(40, 30, 0)), False)
_sk.addGeometry(Part.Circle(Vector(5, 5, 0), Vector(0, 0, 1), 3.0), False)
_sk.addGeometry(
    Part.ArcOfCircle(Part.Circle(Vector(0, 0, 0), Vector(0, 0, 1), 5.0), 0.0, 1.0), False
)
_sk.addGeometry(Part.Point(Vector(1, 1, 0)), False)
_bs = Part.BSplineCurve()
_bs.buildFromPolesMultsKnots(
    [Vector(0, 0, 0), Vector(10, 0, 0), Vector(20, 0, 0)],
    [3, 3],
    [0.0, 1.0],
    False,
    2,
    [1.0, 1.0, 1.0],
)
_sk.addGeometry(_bs, False)
doc.recompute()
"""

SWEEP_FIXTURE_IN_LOOP = (
    '    _name = "SweepSketch"\n'
    "    if doc.getObject(_name) is not None:\n"
    "        doc.removeObject(_name)\n"
    "        doc.recompute()\n"
    '    _sk = doc.addObject("Sketcher::SketchObject", _name)\n'
    "    _sk.addGeometry(Part.LineSegment(Vector(0, 0, 0), Vector(40, 0, 0)), False)\n"
    "    _sk.addGeometry(Part.LineSegment(Vector(40, 0, 0), Vector(40, 30, 0)), False)\n"
    "    _sk.addGeometry(Part.Circle(Vector(5, 5, 0), Vector(0, 0, 1), 3.0), False)\n"
    "    _sk.addGeometry(\n"
    "        Part.ArcOfCircle(Part.Circle(Vector(0, 0, 0), Vector(0, 0, 1), 5.0),\n"
    "        0.0, 1.0), False)\n"
    "    _sk.addGeometry(Part.Point(Vector(1, 1, 0)), False)\n"
    "    _bs = Part.BSplineCurve()\n"
    "    _bs.buildFromPolesMultsKnots(\n"
    "        [Vector(0, 0, 0), Vector(10, 0, 0), Vector(20, 0, 0)],\n"
    "        [3, 3],\n"
    "        [0.0, 1.0],\n"
    "        False,\n"
    "        2,\n"
    "        [1.0, 1.0, 1.0],\n"
    "    )\n"
    "    _sk.addGeometry(_bs, False)\n"
    "    doc.recompute()\n"
)

SWEEP_QUANTITIES = (
    "_quantities = {\n"
    '    "none": None,\n'
    '    "mm": App.Units.Quantity("10 mm"),\n'
    '    "deg": App.Units.Quantity("10 deg"),\n'
    '    "unit": App.Units.Quantity("1.0"),\n'
    "}\n"
)

SWEEP_PROGRAM = (
    "import json as _json\n"
    "import Part\n"
    "import Sketcher\n"
    "from FreeCAD import Vector\n"
    "\n"
    'doc = App.getDocument("@DOC@")\n'
    '_candidate = _json.loads(r"""@CANDIDATE@""")\n'
    '_ctype = _candidate["type"]\n'
    '_cargs = tuple(_candidate["arguments"])\n'
    '_cdatum = _candidate["datum"]\n'
    + SWEEP_QUANTITIES
    + "_q = _quantities.get(_cdatum)\n"
    + SWEEP_FIXTURE
    + '_result = {"type": _ctype, "arguments": list(_cargs), "datum": _cdatum}\n'
    "try:\n"
    "    if _q is None:\n"
    "        _c = Sketcher.Constraint(_ctype, *_cargs)\n"
    "    else:\n"
    "        _c = Sketcher.Constraint(_ctype, *(_cargs + (_q,)))\n"
    '    _result["constructor"] = "ok"\n'
    "except Exception as _exc:\n"
    '    _result["constructor"] = type(_exc).__name__ + ": " + str(_exc)\n'
    '_result["outcome"] = "rejected"\n'
    'if _result.get("constructor") == "ok":\n'
    "    try:\n"
    "        _idx = _sk.addConstraint(_c)\n"
    '        _result["addConstraint"] = "ok"\n'
    '        _result["index"] = int(_idx)\n'
    '        _result["outcome"] = "accepted"\n'
    "    except Exception as _exc:\n"
    '        _result["addConstraint"] = type(_exc).__name__ + ": " + str(_exc)\n'
    'print("PROBE_RESULT:" + _json.dumps(_result, sort_keys=True))\n'
)

DEFAULT_FORMS_PROGRAM = (
    "import json as _json\n"
    "import Part\n"
    "import Sketcher\n"
    "from FreeCAD import Vector\n"
    "\n"
    'doc = App.getDocument("@DOC@")\n'
    '_candidates = _json.loads(r"""@CANDIDATES@""")\n' + SWEEP_QUANTITIES + "_forms = {}\n"
    "for _cand in _candidates:\n"
    '    _ctype = _cand["type"]\n'
    '    _cargs = tuple(_cand["arguments"])\n'
    '    _q = _quantities[_cand["datum"]]\n'
    '    _key = _ctype + ":" + _json.dumps(list(_cargs))\n'
    + SWEEP_FIXTURE_IN_LOOP
    + "    _entry = {}\n"
    "    try:\n"
    "        if _q is None:\n"
    "            _c = Sketcher.Constraint(_ctype, *_cargs)\n"
    "        else:\n"
    "            _c = Sketcher.Constraint(_ctype, *(_cargs + (_q,)))\n"
    '        _entry["constructor"] = "ok"\n'
    "    except Exception as _exc:\n"
    '        _entry["constructor"] = type(_exc).__name__ + ": " + str(_exc)\n'
    '    _entry["outcome"] = "rejected"\n'
    '    if _entry.get("constructor") == "ok":\n'
    "        try:\n"
    "            _idx = _sk.addConstraint(_c)\n"
    '            _entry["addConstraint"] = "ok"\n'
    '            _entry["index"] = int(_idx)\n'
    '            _entry["outcome"] = "accepted"\n'
    "        except Exception as _exc:\n"
    '            _entry["addConstraint"] = type(_exc).__name__ + ": " + str(_exc)\n'
    "    _forms[_key] = _entry\n"
    'doc.removeObject("SweepSketch")\n'
    "doc.recompute()\n"
    'print("PROBE_RESULT:" + _json.dumps({"outcome": "ok", "forms": _forms}, sort_keys=True))\n'
)


# ---------------------------------------------------------------------------
# Probe programs. @DOC@ and @TMP@ are substituted by the runner.
# ---------------------------------------------------------------------------

PROGRAM_NULL_SHAPE = """
import json as _json

doc = App.getDocument("@DOC@")
body = doc.addObject("PartDesign::Body", "NullShapeProbe")
result = {}
try:
    shape = body.Shape
    result["shape_access"] = "ok"
except Exception as exc:
    shape = None
    result["shape_access"] = type(exc).__name__ + ": " + str(exc)
if shape is not None:
    # hasattr is not safe here: FreeCAD property getters raise
    # RuntimeError for a null shape, which hasattr does not swallow.
    for name in ("Volume", "Area", "ShapeType", "BoundBox", "Solids"):
        try:
            value = getattr(shape, name)
            if name == "Solids":
                value = len(value)
            result[name] = {"present": True, "result": repr(value)[:120]}
        except Exception as exc:
            result[name] = {
                "present": type(exc).__name__ != "AttributeError",
                "raised": type(exc).__name__ + ": " + str(exc),
            }
    for name in ("isValid", "check", "isNull", "getTolerance"):
        try:
            value = getattr(shape, name)()
            result[name] = {"present": True, "result": repr(value)[:120]}
        except Exception as exc:
            result[name] = {
                "present": type(exc).__name__ != "AttributeError",
                "raised": type(exc).__name__ + ": " + str(exc),
            }
doc.removeObject(body.Name)
doc.recompute()
result["outcome"] = "ok"
print("PROBE_RESULT:" + _json.dumps(result, sort_keys=True))
"""

PROGRAM_ATTACHMENT = """
import json as _json

doc = App.getDocument("@DOC@")
body = doc.addObject("PartDesign::Body", "AttachProbe")
result = {}
for type_id in (
    "Sketcher::SketchObject",
    "PartDesign::Plane",
    "PartDesign::Pad",
    "Part::Box",
):
    obj = None
    try:
        obj = body.newObject(type_id, type_id.split("::")[-1] + "Probe")
        route = "body.newObject"
    except Exception:
        try:
            obj = doc.addObject(type_id, type_id.split("::")[-1] + "Probe")
            route = "doc.addObject"
        except Exception as exc:
            result[type_id] = {
                "outcome": "create_error",
                "error": type(exc).__name__ + ": " + str(exc),
            }
            continue
    entry = {"route": route}
    props = {}
    for prop in ("Support", "AttachmentSupport", "MapMode"):
        props[prop] = hasattr(obj, prop)
    entry["properties"] = props
    entry["outcome"] = "ok"
    result[type_id] = entry
    doc.removeObject(obj.Name)
doc.removeObject(body.Name)
doc.recompute()
result["outcome"] = "ok"
print("PROBE_RESULT:" + _json.dumps({"types": result, "outcome": "ok"}, sort_keys=True))
"""

GEOMETRY_FIXTURE = """
import json as _json
import Part
from FreeCAD import Vector

doc = App.getDocument("@DOC@")
sk = doc.addObject("Sketcher::SketchObject", "GeoProbe")
sk.addGeometry(Part.LineSegment(Vector(0, 0, 0), Vector(10, 0, 0)), False)
sk.addGeometry(Part.Circle(Vector(0, 0, 0), Vector(0, 0, 1), 5.0), False)
sk.addGeometry(
    Part.ArcOfCircle(Part.Circle(Vector(0, 0, 0), Vector(0, 0, 1), 5.0), 0.0, 1.0), False
)
sk.addGeometry(Part.Point(Vector(1, 2, 0)), False)
"""

PROGRAM_GEOMETRY_ATTRS = (
    GEOMETRY_FIXTURE
    + """
names = (
    "StartPoint",
    "EndPoint",
    "Circle",
    "Center",
    "Radius",
    "FirstParameter",
    "LastParameter",
    "x",
    "y",
    "Construction",
)
elements = {}
for i, geo in enumerate(sk.Geometry):
    entry = {"native": type(geo).__name__}
    attrs = {}
    for name in names:
        item = {"present": hasattr(geo, name)}
        if item["present"]:
            try:
                item["result"] = repr(getattr(geo, name))
            except Exception as exc:
                item["raised"] = type(exc).__name__ + ": " + str(exc)
        attrs[name] = item
    entry["attributes"] = attrs
    elements[str(i)] = entry
doc.removeObject(sk.Name)
doc.recompute()
print("PROBE_RESULT:" + _json.dumps({"outcome": "ok", "elements": elements}, sort_keys=True))
"""
)

PROGRAM_GET_CONSTRUCTION = (
    GEOMETRY_FIXTURE
    + """
sk.addGeometry(Part.LineSegment(Vector(0, 5, 0), Vector(10, 5, 0)), True)
result = {"getConstruction": {"present": hasattr(sk, "getConstruction")}}
result["getConstruction"]["callable"] = callable(getattr(sk, "getConstruction", None))
if result["getConstruction"]["callable"]:
    for index in (0, 1):
        try:
            result["getConstruction"]["index%d" % index] = repr(sk.getConstruction(index))
        except Exception as exc:
            result["getConstruction"]["index%d_error" % index] = (
                type(exc).__name__ + ": " + str(exc)
            )
result["elements_have_Construction"] = [hasattr(g, "Construction") for g in sk.Geometry]
result["outcome"] = "ok"
doc.removeObject(sk.Name)
doc.recompute()
print("PROBE_RESULT:" + _json.dumps(result, sort_keys=True))
"""
)

PROGRAM_CONSTRAINT_ATTRS = """
import json as _json
import Part
import Sketcher
from FreeCAD import Vector

doc = App.getDocument("@DOC@")
sk = doc.addObject("Sketcher::SketchObject", "ConAttrProbe")
sk.addGeometry(Part.LineSegment(Vector(0, 0, 0), Vector(10, 0, 0)), False)
sk.addGeometry(Part.LineSegment(Vector(10, 0, 0), Vector(10, 10, 0)), False)
sk.addConstraint(Sketcher.Constraint("Coincident", 0, 2, 1, 1))
c = sk.Constraints[0]
entry = {}
for name in ("Driving", "IsDriving", "IsActive", "Type", "Value", "Name"):
    item = {"present": hasattr(c, name)}
    if item["present"]:
        try:
            value = getattr(c, name)
            item["result"] = repr(value() if callable(value) else value)
        except Exception as exc:
            item["raised"] = type(exc).__name__ + ": " + str(exc)
    entry[name] = item
doc.removeObject(sk.Name)
doc.recompute()
print("PROBE_RESULT:" + _json.dumps({"outcome": "ok", "constraint": entry}, sort_keys=True))
"""

PROGRAM_SOLVER_ATTRS = """
import json as _json
import Part
from FreeCAD import Vector

doc = App.getDocument("@DOC@")
sk = doc.addObject("Sketcher::SketchObject", "SolverProbe")
sk.addGeometry(Part.LineSegment(Vector(0, 0, 0), Vector(10, 0, 0)), False)
doc.recompute()
solve_result = None
solve_error = None
try:
    solve_result = repr(sk.solve())
except Exception as exc:
    solve_error = type(exc).__name__ + ": " + str(exc)
entry = {}
for name in ("DoF", "FullyConstrained"):
    item = {"present": hasattr(sk, name)}
    if item["present"]:
        try:
            item["value"] = repr(getattr(sk, name))
        except Exception as exc:
            item["raised"] = type(exc).__name__ + ": " + str(exc)
    entry[name] = item
for name in ("getSolverDoF", "getSolverMessages"):
    item = {"present": hasattr(sk, name), "callable": callable(getattr(sk, name, None))}
    if item["callable"]:
        try:
            item["result"] = repr(getattr(sk, name)())
        except Exception as exc:
            item["raised"] = type(exc).__name__ + ": " + str(exc)
    entry[name] = item
doc.removeObject(sk.Name)
doc.recompute()
print(
    "PROBE_RESULT:"
    + _json.dumps(
        {
            "outcome": "ok",
            "solve": solve_result,
            "solve_error": solve_error,
            "attributes": entry,
        },
        sort_keys=True,
    )
)
"""


def datum_program(kind: str) -> str:
    """One setDatum probe: fixture constraint plus a string or quantity edit."""
    edit_line = (
        '    sk.setDatum(_idx, "12 mm")\n'
        if kind == "string"
        else '    sk.setDatum(_idx, App.Units.Quantity("12 mm"))\n'
    )
    return (
        "import json as _json\n"
        "import Part\n"
        "import Sketcher\n"
        "from FreeCAD import Vector\n"
        "\n"
        'doc = App.getDocument("@DOC@")\n'
        'sk = doc.addObject("Sketcher::SketchObject", "DatumProbe")\n'
        "sk.addGeometry(Part.LineSegment(Vector(0, 0, 0), Vector(10, 0, 0)), False)\n"
        '_idx = int(sk.addConstraint(Sketcher.Constraint("DistanceX", 0, 1, 0, 2, '
        'App.Units.Quantity("10 mm"))))\n'
        'result = {"constraint_index": _idx}\n'
        "try:\n" + edit_line + '    result["edit"] = "ok"\n'
        "except Exception as exc:\n"
        '    result["edit"] = type(exc).__name__ + ": " + str(exc)\n'
        "try:\n"
        '    result["value_after"] = repr(sk.Constraints[_idx].Value)\n'
        "except Exception as exc:\n"
        '    result["value_after"] = type(exc).__name__ + ": " + str(exc)\n'
        'result["outcome"] = "ok" if result.get("edit") == "ok" else "rejected"\n'
        "doc.removeObject(sk.Name)\n"
        "doc.recompute()\n"
        'print("PROBE_RESULT:" + _json.dumps(result, sort_keys=True))\n'
    )


PROGRAM_PROPERTY_STATUS = """
import json as _json

doc = App.getDocument("@DOC@")
box = doc.addObject("Part::Box", "PropProbe")
result = {}
props = {}
props["PropertiesList"] = {"present": hasattr(box, "PropertiesList")}
if props["PropertiesList"]["present"]:
    props["PropertiesList"]["result"] = list(box.PropertiesList)
props["supportedTypes"] = {"present": hasattr(doc, "supportedTypes")}
if props["supportedTypes"]["present"]:
    try:
        props["supportedTypes"]["result"] = repr(doc.supportedTypes())
    except Exception as exc:
        props["supportedTypes"]["raised"] = type(exc).__name__ + ": " + str(exc)
for name in ("Length", "Width", "Height", "Missing"):
    entry = {}
    if hasattr(box, "getPropertyStatus"):
        try:
            entry["status"] = list(box.getPropertyStatus(name))
        except Exception as exc:
            entry["status_error"] = type(exc).__name__ + ": " + str(exc)
    if hasattr(box, "getTypeIdOfProperty"):
        try:
            entry["typeId"] = repr(box.getTypeIdOfProperty(name))
        except Exception as exc:
            entry["typeId_error"] = type(exc).__name__ + ": " + str(exc)
    props[name] = entry
result["properties"] = props
result["outcome"] = "ok"
try:
    import Import

    try:
        Import.insert()
    except TypeError as exc:
        result["import_insert_signature"] = str(exc)
    except Exception as exc:
        result["import_insert_signature"] = type(exc).__name__ + ": " + str(exc)
except ImportError as exc:
    result["import_insert_signature"] = "Import module unavailable: " + str(exc)
doc.removeObject(box.Name)
doc.recompute()
print("PROBE_RESULT:" + _json.dumps(result, sort_keys=True))
"""

PROGRAM_MESH_PIPELINE = """
import json as _json
import os
import Part
import Mesh
import MeshPart


def _solid(mesh):
    return repr(mesh.isSolid() if callable(mesh.isSolid) else mesh.isSolid)


tmp = "@TMP@"
box = Part.makeBox(10, 10, 10)
result = {}
try:
    MeshPart.meshFromShape()
except TypeError as exc:
    result["signature_error"] = str(exc)
except Exception as exc:
    result["signature_error"] = type(exc).__name__ + ": " + str(exc)
mesh = MeshPart.meshFromShape(Shape=box)
result["defaults"] = {
    "countFacets": int(mesh.CountFacets),
    "isSolid": _solid(mesh),
    "boundBox": repr(mesh.BoundBox),
}
tuned = MeshPart.meshFromShape(
    Shape=box, LinearDeflection=0.03, AngularDeflection=0.12, Relative=False
)
result["addon_kwargs"] = {"accepted": True, "countFacets": int(tuned.CountFacets)}
stl_path = os.path.join(tmp, "probe_mesh.stl")
tuned.write(stl_path)
reread = Mesh.Mesh(stl_path)
result["stl_readback"] = {
    "countFacets": int(reread.CountFacets),
    "isSolid": _solid(reread),
    "boundBox": repr(reread.BoundBox),
}
m3mf_path = os.path.join(tmp, "probe_mesh.3mf")
try:
    tuned.write(m3mf_path)
    result["write_3mf_plain"] = "ok"
except Exception as exc:
    result["write_3mf_plain"] = type(exc).__name__ + ": " + str(exc)
try:
    tuned.write(m3mf_path, Format="3MF")
    result["write_3mf_format_kwarg"] = "ok"
except Exception as exc:
    result["write_3mf_format_kwarg"] = type(exc).__name__ + ": " + str(exc)
reread3 = Mesh.Mesh(m3mf_path)
result["threemf_readback"] = {
    "countFacets": int(reread3.CountFacets),
    "isSolid": _solid(reread3),
}
result["outcome"] = "ok"
print("PROBE_RESULT:" + _json.dumps(result, sort_keys=True))
"""

PROGRAM_PART_READ = """
import json as _json
import os
import Part

tmp = "@TMP@"
box = Part.makeBox(10, 10, 10)
step_path = os.path.join(tmp, "probe_part.step")
result = {}
try:
    Part.write(box, step_path)
    result["part_write"] = "ok"
except Exception as exc:
    result["part_write"] = type(exc).__name__ + ": " + str(exc)
if result["part_write"] != "ok":
    try:
        box.exportStep(step_path)
        result["shape_exportStep"] = "ok"
    except Exception as exc:
        result["shape_exportStep"] = type(exc).__name__ + ": " + str(exc)
shape = Part.read(step_path)
solids = list(shape.Solids)
result["readback"] = {
    "type": type(shape).__name__,
    "volume": repr(sum(solid.Volume for solid in solids)),
    "solidCount": len(solids),
    "isValid": repr(shape.isValid()),
    "boundBox": repr(shape.BoundBox),
}
result["outcome"] = "ok"
print("PROBE_RESULT:" + _json.dumps(result, sort_keys=True))
"""

PROGRAM_GUI_SELECTION = """
import json as _json
import os

doc = App.getDocument("@DOC@")
box = doc.addObject("Part::Box", "SelProbe")
doc.recompute()
result = {}
Gui.Selection.clearSelection()
try:
    Gui.Selection.addSelection(box)
    result["addSelection_obj"] = "ok"
except Exception as exc:
    result["addSelection_obj"] = type(exc).__name__ + ": " + str(exc)
try:
    Gui.Selection.addSelection(doc.Name, box.Name, "Face3", 1.0, 2.0, 3.0)
    result["addSelection_doc_sub_xyz"] = "ok"
except Exception as exc:
    result["addSelection_doc_sub_xyz"] = type(exc).__name__ + ": " + str(exc)
try:
    sel = Gui.Selection.getSelectionEx(doc.Name)
    first = sel[0] if len(sel) else None
    result["getSelectionEx"] = {
        "count": len(sel),
        "first_object": repr(first.ObjectName) if first else None,
        "first_subelements": list(first.SubElementNames) if first else None,
        "first_has_shape": getattr(first, "HasShape", None),
    }
except Exception as exc:
    result["getSelectionEx"] = type(exc).__name__ + ": " + str(exc)
try:
    result["sendMsgToActiveView"] = repr(Gui.SendMsgToActiveView("ViewFit"))
except Exception as exc:
    result["sendMsgToActiveView"] = type(exc).__name__ + ": " + str(exc)
try:
    view = Gui.activeDocument().activeView()
    if view is None:
        result["saveImage"] = "no active view"
    else:
        image_path = os.path.join("@TMP@", "probe_view.png")
        view.saveImage(image_path, 400, 300, "Current")
        result["saveImage"] = {
            "arity": "path, width, height, style",
            "file_exists": os.path.exists(image_path),
        }
except Exception as exc:
    result["saveImage"] = type(exc).__name__ + ": " + str(exc)
doc.removeObject(box.Name)
doc.recompute()
result["outcome"] = "ok"
print("PROBE_RESULT:" + _json.dumps(result, sort_keys=True))
"""

PROGRAM_QT_SCHEDULE = """
import json as _json
from PySide import QtCore

state = globals().setdefault("_probe_state", {})
app = QtCore.QCoreApplication.instance()
state["qt_thread_is_app_thread"] = bool(
    app is not None and QtCore.QThread.currentThread() is app.thread()
)
state["timer_fired"] = False
state["timer_thread_is_app_thread"] = None


def _on_timer(state=state):
    state["timer_fired"] = True
    app2 = QtCore.QCoreApplication.instance()
    state["timer_thread_is_app_thread"] = bool(
        app2 is not None and QtCore.QThread.currentThread() is app2.thread()
    )


QtCore.QTimer.singleShot(0, _on_timer)
print(
    "PROBE_RESULT:"
    + _json.dumps(
        {
            "outcome": "ok",
            "stage": "scheduled",
            "qt_thread_is_app_thread": state["qt_thread_is_app_thread"],
        },
        sort_keys=True,
    )
)
"""

PROGRAM_QT_COLLECT = """
import json as _json

state = globals().get("_probe_state", {})
print(
    "PROBE_RESULT:"
    + _json.dumps(
        {
            "outcome": "ok",
            "stage": "collected",
            "timer_fired": state.get("timer_fired"),
            "timer_thread_is_app_thread": state.get("timer_thread_is_app_thread"),
        },
        sort_keys=True,
    )
)
"""

PROGRAM_FEM_OBJECTS = """
import json as _json
import ObjectsFem

factories = sorted(n for n in dir(ObjectsFem) if n.startswith("make"))
print(
    "PROBE_RESULT:"
    + _json.dumps({"outcome": "ok", "factories": factories}, sort_keys=True)
)
"""

PROGRAM_DOC_LIFECYCLE = """
import json as _json
import os

doc = App.getDocument("@DOC@")
result = {}
result["undo_mode_present"] = hasattr(doc, "UndoMode")
result["undo_mode"] = repr(getattr(doc, "UndoMode", None))
doc.openTransaction("probe")
doc.addObject("Part::Box", "LifecycleBox")
doc.commitTransaction()
result["doc_isTouched_present"] = hasattr(doc, "isTouched")
if result["doc_isTouched_present"]:
    try:
        result["doc_isTouched_after_commit"] = bool(doc.isTouched())
    except Exception as exc:
        result["doc_isTouched_after_commit"] = type(exc).__name__ + ": " + str(exc)
doc.recompute()
if result["doc_isTouched_present"]:
    try:
        result["doc_isTouched_after_recompute"] = bool(doc.isTouched())
    except Exception as exc:
        result["doc_isTouched_after_recompute"] = type(exc).__name__ + ": " + str(exc)
path = os.path.join("@TMP@", "lifecycle.FCStd")
doc.saveAs(path)
result["saved_path_exists"] = os.path.exists(path)
name = doc.Name
App.closeDocument(name)
reopened = App.openDocument(path)
result["reopened"] = {
    "name": reopened.Name,
    "label": reopened.Label,
    "object_count": len(reopened.Objects),
    "file_name": str(reopened.FileName),
}
if hasattr(reopened, "isTouched"):
    try:
        result["reopened_isTouched"] = bool(reopened.isTouched())
    except Exception as exc:
        result["reopened_isTouched"] = type(exc).__name__ + ": " + str(exc)
App.closeDocument(reopened.Name)
result["outcome"] = "ok"
print("PROBE_RESULT:" + _json.dumps(result, sort_keys=True))
"""


# ---------------------------------------------------------------------------
# Runner.
# ---------------------------------------------------------------------------


class Runner:
    """Executes probe steps with journaling, crash policy, and restarts."""

    def __init__(self, client: FreeCadMcpClient, journal: Journal, dev: bool, token: str) -> None:
        self.client = client
        self.journal = journal
        self.dev = dev
        self.token = token
        self.restarts = 0
        self.doc_name: str | None = None
        self.steps: list[tuple[str, str, bool]] = []
        self.process_start = self._record_process_start()

    def _record_process_start(self) -> float:
        starts = [s for s in (process_start_epoch(p) for p in freecad_pids()) if s is not None]
        return min(starts) if starts else time.time()

    def ensure_document(self) -> str:
        if self.doc_name:
            return self.doc_name
        wanted = f"MCP_NativeContract_{os.getpid()}"
        result = self.client.call_tool("new_document", {"name": wanted})
        self.doc_name = str(result["name"])
        return self.doc_name

    def close_document(self) -> None:
        if not self.doc_name:
            return
        name = self.doc_name
        self.doc_name = None
        try:
            self.client.call_tool("close_document", {"document": name})
            self.journal.record({"step": "cleanup.close_document", "outcome": "closed"})
        except (ToolFailure, ProtocolFailure, *TRANSPORT_ERRORS) as exc:
            self.journal.record(
                {"step": "cleanup.close_document", "outcome": "skipped", "error": repr(exc)}
            )

    def substitute(self, program: str) -> str:
        return program.replace("@DOC@", self.ensure_document()).replace("@TMP@", TMP_DIR)

    def run_program(self, program: str) -> dict:
        return self.client.run_script(self.substitute(program))

    @staticmethod
    def extract_payload(result: dict) -> dict:
        for line in reversed((result.get("stdout") or "").splitlines()):
            if line.startswith(RESULT_PREFIX):
                return json.loads(line[len(RESULT_PREFIX) :])
        return {
            "outcome": "script_error",
            "stdoutTail": (result.get("stdout") or "")[-1500:],
            "stderrTail": (result.get("stderr") or "")[-1500:],
        }

    def step(self, name: str, sent: dict, perform) -> tuple[dict, bool]:
        """Journal, run, classify one probe step. Returns (payload, crashed)."""
        self.journal.record({"step": name, "sent": redact(sent, self.token)})
        try:
            payload = perform()
        except ToolFailure as exc:
            self.steps.append((name, "script_error", False))
            return {"outcome": "script_error", "error": repr(exc)}, False
        except TRANSPORT_ERRORS as exc:
            return self._transport_failure(name, exc, perform)
        self.steps.append((name, str(payload.get("outcome", "?")), False))
        return payload, False

    def script_step(self, name: str, program: str) -> tuple[dict, bool]:
        sent = {
            "tool": "run_script",
            "arguments": {"code": program, "session_id": SESSION_ID},
        }
        return self.step(name, sent, lambda: self.extract_payload(self.run_program(program)))

    def _transport_failure(self, name: str, exc: Exception, perform) -> tuple[dict, bool]:
        if wait_alive_verdict(5.0):
            self.journal.record({"step": name, "outcome": "error", "error": repr(exc)})
            try:
                payload = perform()
            except ToolFailure as exc2:
                self.steps.append((name, "script_error", False))
                return {"outcome": "script_error", "error": repr(exc2)}, False
            except TRANSPORT_ERRORS as exc2:
                if wait_alive_verdict(5.0):
                    self.journal.record({"step": name, "outcome": "error", "error": repr(exc2)})
                    self.steps.append((name, "error", False))
                    return {"outcome": "error", "error": repr(exc2)}, False
                exc = exc2
            else:
                self.steps.append((name, str(payload.get("outcome", "?")), False))
                return payload, False
        return self._on_crash(name, exc)

    def _on_crash(self, name: str, exc: Exception) -> tuple[dict, bool]:
        self.journal.record({"step": name, "outcome": "crash", "error": repr(exc)})
        report = newest_crash_report(self.process_start)
        diagnosis = None
        copied_path: str | None = None
        if report:
            diagnosis = parse_ips(report)
            copied_path = os.path.join(CONTRACT_DIR, os.path.basename(report))
            shutil.copy2(report, copied_path)
        self.journal.record({"step": name, "crashReport": copied_path, "diagnosis": diagnosis})
        print(f"CRASH confirmed at step {name!r}; FreeCAD process is gone ({exc!r})")
        if copied_path:
            print(f"  crash report copy: {copied_path}")
        else:
            print("  no crash report newer than this run's process start was found")
        if not self.dev:
            raise VerificationCrash(name, self.journal.path, copied_path)
        self.steps.append((name, "crash:restarted", True))
        self._restart(name)
        return {"outcome": "abort", "error": repr(exc)}, True

    def _restart(self, name: str) -> None:
        if self.restarts >= MAX_RESTARTS:
            raise ProbeFatal(
                f"restart bound of {MAX_RESTARTS} exceeded; journal: {self.journal.path}"
            )
        self.restarts += 1
        print(f"restarting FreeCAD (restart {self.restarts}/{MAX_RESTARTS})")
        subprocess.run(
            ["osascript", "-e", 'quit app "FreeCAD"'],
            capture_output=True,
            check=False,
            timeout=60,
        )
        if not wait_pgrep_empty(20.0):
            raise ProbeFatal(
                f"FreeCAD did not quit within 20 seconds; journal: {self.journal.path}"
            )
        try:
            subprocess.run(
                ["osascript", "-e", 'tell application "FreeCAD" to activate'],
                check=True,
                capture_output=True,
                timeout=60,
            )
        except subprocess.SubprocessError:
            subprocess.run(["open", "-a", "FreeCAD"], capture_output=True, check=False, timeout=60)
        deadline = time.monotonic() + RESTART_POLL_S
        caps_ok = False
        ready = False
        while time.monotonic() < deadline:
            try:
                caps = self.client.discover_capabilities()
                caps_ok = True
                gui = caps.get("gui") or {}
                if gui.get("state") == "healthy" and gui.get("queuedJobs") == 0:
                    self.doc_name = None
                    doc = self.ensure_document()
                    self.client.call_tool("inspect_objects", {"document": doc}, timeout=60.0)
                    ready = True
                    break
            except (ToolFailure, ProtocolFailure, *TRANSPORT_ERRORS):
                pass
            time.sleep(2.0)
        if not ready:
            if caps_ok:
                raise ProbeFatal(
                    "FreeCAD restarted but the MCP server never reported a healthy, "
                    f"idle GUI within {RESTART_POLL_S:.0f}s; check for a startup "
                    f"dialog; journal: {self.journal.path}"
                )
            raise ProbeFatal(
                "FreeCAD restarted but the MCP server did not start; start it "
                f"manually and re-run; journal: {self.journal.path}"
            )
        self.process_start = self._record_process_start()
        self.journal.record({"step": name, "outcome": "restarted", "document": self.doc_name})


# ---------------------------------------------------------------------------
# Probe sequence.
# ---------------------------------------------------------------------------


def server_version_payload(client: FreeCadMcpClient) -> dict:
    caps = client.discover_capabilities()
    freecad = caps.get("freecad") or {}
    version = [str(part) for part in freecad.get("version") or []]
    return {
        "outcome": "ok",
        "version": ".".join(version[:3]),
        "full": [str(part) for part in freecad.get("full") or []],
    }


def run_constraint_forms_default(runner: Runner) -> tuple[dict, bool, dict]:
    """One verification step over the documented candidates."""
    program = DEFAULT_FORMS_PROGRAM.replace(
        "@CANDIDATES@",
        json.dumps(
            [
                {"type": ctype, "arguments": list(args), "datum": datum}
                for ctype, args, datum in DOCUMENTED_FORMS
            ]
        ),
    )
    payload, crashed = runner.script_step("constraint.forms", program)
    forms: dict = {}
    if isinstance(payload.get("forms"), dict):
        forms = dict(payload["forms"])
    stale = load_record().get("probes", {}).get("constraint.forms") or {}
    stale_forms = stale.get("forms") if isinstance(stale, dict) else None
    if isinstance(stale_forms, dict):
        for key, entry in stale_forms.items():
            if key not in DOCUMENTED_KEYS:
                forms[key] = entry
    return {"outcome": payload.get("outcome", "?"), "forms": forms}, crashed, payload


def run_constraint_forms_sweep(runner: Runner, payloads: dict) -> None:
    """One step per candidate; aborts are observations through restarts."""
    matrix: dict[str, dict] = {}
    for index, candidate in enumerate(CANDIDATE_FORMS):
        key = candidate_key(candidate)
        step_name = f"constraint.forms[{index:02d}] {key}"
        program = SWEEP_PROGRAM.replace(
            "@CANDIDATE@",
            json.dumps(
                {"type": candidate[0], "arguments": list(candidate[1]), "datum": candidate[2]}
            ),
        )
        payload, crashed = runner.script_step(step_name, program)
        entry = {
            "type": candidate[0],
            "arguments": list(candidate[1]),
            "datum": candidate[2],
        }
        if crashed:
            entry["outcome"] = "abort"
            entry["error"] = str(payload.get("error", "process abort"))
        else:
            entry.update(payload)
        matrix[key] = entry
        payloads["constraint.forms"] = {"outcome": "ok", "forms": dict(matrix)}


def run_probe_sequence(runner: Runner, payloads: dict, sweep: bool) -> None:
    """Execute every probe into ``payloads`` keyed by probe name."""
    runner.ensure_document()

    payload, _crashed = runner.step(
        "server.version",
        {"tool": "discover_capabilities", "arguments": {}},
        lambda: server_version_payload(runner.client),
    )
    payloads["server.version"] = payload

    script_probes = [
        ("shape.null_attributes", PROGRAM_NULL_SHAPE),
        ("attachment.properties", PROGRAM_ATTACHMENT),
        ("geometry.attributes", PROGRAM_GEOMETRY_ATTRS),
        ("geometry.getConstruction", PROGRAM_GET_CONSTRUCTION),
        ("constraint.attributes", PROGRAM_CONSTRAINT_ATTRS),
        ("solver.attributes", PROGRAM_SOLVER_ATTRS),
        ("setDatum.string", datum_program("string")),
        ("setDatum.quantity", datum_program("quantity")),
        ("object.property_status", PROGRAM_PROPERTY_STATUS),
        ("mesh.pipeline", PROGRAM_MESH_PIPELINE),
        ("part.read", PROGRAM_PART_READ),
        ("gui.selection", PROGRAM_GUI_SELECTION),
        ("fem.objects", PROGRAM_FEM_OBJECTS),
    ]
    for name, program in script_probes:
        payload, _crashed = runner.script_step(name, program)
        payloads[name] = payload

    # qt.signals: two run_script calls through one persistent session.
    qt_sent = {
        "tool": "run_script",
        "arguments": [
            {"code": PROGRAM_QT_SCHEDULE, "session_id": SESSION_ID},
            {"code": PROGRAM_QT_COLLECT, "session_id": SESSION_ID},
        ],
    }

    def qt_perform() -> dict:
        scheduled = runner.extract_payload(runner.run_program(PROGRAM_QT_SCHEDULE))
        if scheduled.get("outcome") != "ok":
            return scheduled
        collected = runner.extract_payload(runner.run_program(PROGRAM_QT_COLLECT))
        merged = {"outcome": collected.get("outcome", "error")}
        merged.update({k: v for k, v in scheduled.items() if k != "outcome"})
        merged.update({k: v for k, v in collected.items() if k != "outcome"})
        return merged

    payloads["qt.signals"] = runner.step("qt.signals", qt_sent, qt_perform)[0]

    # doc.lifecycle must stay last: it closes and reopens the probe document.
    if sweep:
        run_constraint_forms_sweep(runner, payloads)
        payloads["doc.lifecycle"] = runner.script_step("doc.lifecycle", PROGRAM_DOC_LIFECYCLE)[0]
    else:
        payloads["constraint.forms"], _, _ = run_constraint_forms_default(runner)
        payloads["doc.lifecycle"] = runner.script_step("doc.lifecycle", PROGRAM_DOC_LIFECYCLE)[0]


# ---------------------------------------------------------------------------
# Record writing.
# ---------------------------------------------------------------------------


def load_record() -> dict:
    try:
        with open(RECORD_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def write_record(record: dict) -> None:
    with open(RECORD_PATH, "w", encoding="utf-8") as fh:
        json.dump(record, fh, sort_keys=True, indent=2)
        fh.write("\n")


def build_default_record(payloads: dict, version: str) -> dict:
    return {
        "schema": 1,
        "freecad": version,
        "captured": now_iso(),
        "probes": payloads,
    }


def build_sweep_record(payloads: dict, version: str) -> dict:
    existing = load_record()
    if not existing:
        existing = {"schema": 1, "freecad": version, "captured": now_iso(), "probes": {}}
    probes = dict(existing.get("probes") or {})
    forms = payloads.get("constraint.forms")
    if forms is not None:
        probes["constraint.forms"] = forms
    existing["probes"] = probes
    return existing


# ---------------------------------------------------------------------------
# Main.
# ---------------------------------------------------------------------------


def print_summary(steps: list[tuple[str, str, bool]]) -> None:
    print()
    print(f"{'step':<52} {'outcome':<18} crash")
    print("-" * 82)
    for name, outcome, crashed in steps:
        print(f"{name:<52} {outcome:<18} {'YES' if crashed else 'no'}")


def finish(runner: Runner, payloads: dict, sweep: bool) -> int:
    version = "?"
    raw_version = payloads.get("server.version", {}).get("version")
    if isinstance(raw_version, str):
        version = raw_version
    if sweep:
        write_record(build_sweep_record(payloads, version))
    else:
        crashed = any(flag for _, _, flag in runner.steps)
        if crashed:
            print("a step crashed; the record was not rewritten")
        else:
            write_record(build_default_record(payloads, version))
    print_summary(runner.steps)
    print(f"record:  {RECORD_PATH}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--dev", action="store_true", help="restart FreeCAD after a confirmed crash"
    )
    parser.add_argument(
        "--sweep", action="store_true", help="imply --dev and add the invalid D1 constructions"
    )
    args = parser.parse_args()
    sweep = args.sweep
    dev = args.dev or sweep

    token = os.environ.get("FREECAD_MCP_TOKEN") or ""
    url = os.environ.get("FREECAD_MCP_URL") or "http://127.0.0.1:9876/mcp"
    os.makedirs(CONTRACT_DIR, exist_ok=True)
    os.makedirs(TMP_DIR, exist_ok=True)

    journal = Journal(JOURNAL_PATH)
    client = FreeCadMcpClient(url, token)
    runner = Runner(client, journal, dev, token)
    payloads: dict = {}
    print(f"journal: {JOURNAL_PATH}")
    print(f"record:  {RECORD_PATH}")
    try:
        try:
            run_probe_sequence(runner, payloads, sweep)
        except VerificationCrash as exc:
            print()
            print(f"VERIFICATION RUN STOPPED: FreeCAD crashed at step {exc.step!r}.")
            print(f"  journal:      {exc.journal_path}")
            print(f"  crash report: {exc.report_path or 'not found'}")
            print("A crashed verification run is a terminal failure; FreeCAD was not restarted.")
            print_summary(runner.steps)
            return 2
        except ProbeFatal as exc:
            print(f"FATAL: {exc}")
            if sweep and "constraint.forms" in payloads:
                version = str(payloads.get("server.version", {}).get("version") or "?")
                write_record(build_sweep_record(payloads, version))
                print("partial constraint.forms matrix was merged into the record")
            print_summary(runner.steps)
            return 3
        return finish(runner, payloads, sweep)
    finally:
        runner.close_document()
        journal.close()


if __name__ == "__main__":
    sys.exit(main())
