"""Cantilever FEM over the embedded FreeCAD MCP HTTP server.

Builds a steel cantilever (100 x 10 x 10 mm bar along +X, one face fully
fixed, a 100 N tip load pulling -Z), runs it through the modern
``Fem::SolverCalculiX`` pipeline with ``run_fem``, and compares the loaded
VTK result summary against Euler-Bernoulli beam theory.

The example is a dependency-free standard-library client for the v2 wire
protocol: streamable HTTP on the loopback interface with bearer
authentication, request metadata headers, per-request ``_meta`` and the
consent elicitation round trip (used here only to close the example's own
document at the end).

Prerequisites:
- FreeCAD 1.1.3+ running with the FreeCADMCP add-on installed.
- "Start MCP Server" clicked (or Auto-Start Server enabled).

Configuration:
- ``FREECAD_MCP_TOKEN``: bearer token from the "Show Auth Token" dialog.
  It is read from the environment only and is never printed or logged.
- ``FREECAD_MCP_URL``: optional endpoint override
  (default ``http://127.0.0.1:9876/mcp``).

Run:
    FREECAD_MCP_TOKEN=... python3 examples/cantilever_fem.py

Behavioral guarantees:
- Only the example's own document is created, used and closed; existing
  FreeCAD documents are never touched or closed.
- Geometry and the modern analysis/solver objects are created with the
  structured tools; the FEM model plumbing (material card, constraints,
  direction binding and Gmsh meshing) uses ``run_script`` with FreeCAD's
  native ``ObjectsFem`` factories — mesh objects are explicitly not a
  ``create_object`` type and belong to scripted workflows.

Expected output: the solver reports a nonempty result pipeline and finite
displacement/stress ranges. Von Mises stress concentrates at the fixed
corner and displacement is upper-bounded by beam theory by roughly the
order of the linear-tet mesh (default ~5 mm size), so the comparison prints
ratios instead of asserting closeness — it validates the pipeline
end-to-end, not mesh convergence.
"""

from __future__ import annotations

import http.client
import json
import os
import sys
import urllib.parse

PROTOCOL_VERSION = "2026-07-28"
EXPECTED_TOOLS = 17

DOC = "MCPExampleCantilever"
BEAM = "Beam"
ANALYSIS = "Analysis"
SOLVER = "Solver"

# Geometry: 100 x 10 x 10 mm bar along +X.
L = 100.0
B = 10.0
H = 10.0
# Material: structural steel.
E_GPA = 210.0
NU = 0.3
RHO = "7900 kg/m^3"
# Tip load: 100 N pulling -Z on the +X face.
F_N = 100.0
# Gmsh mesh target sizes (mm).
MESH_MAX = 5.0
MESH_MIN = 1.0

# Generous read timeout: a blocking run_fem call stays open until the
# solver process and native result loading finish.
HTTP_TIMEOUT_S = 900.0


def analytic() -> tuple[float, float]:
    """Closed-form cantilever beam predictions for sanity checking."""
    inertia = B * H**3 / 12.0  # mm^4, second moment of area
    sigma_max_mpa = (F_N * L) * (H / 2) / inertia  # N/mm^2 = MPa
    delta_tip_mm = (F_N * L**3) / (3 * (E_GPA * 1000.0) * inertia)  # mm
    return sigma_max_mpa, delta_tip_mm


class ProtocolFailure(Exception):
    """A protocol-level JSON-RPC error from the server."""


class ToolFailure(Exception):
    """A complete tool result with isError:true."""

    def __init__(self, tool: str, error: dict) -> None:
        self.tool = tool
        self.error = error
        super().__init__(f"{tool}: {error.get('code')}: {error.get('message')}")


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

    # -- wire plumbing ------------------------------------------------------

    def _headers(self, method: str, name: str | None) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOL_VERSION,
            "Mcp-Method": method,
        }
        if name is not None:
            headers["Mcp-Name"] = name
        return headers

    def _meta(self) -> dict:
        return {
            "io.modelcontextprotocol/protocolVersion": PROTOCOL_VERSION,
            "io.modelcontextprotocol/clientInfo": {
                "name": "cantilever-fem-example",
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
        if "text/event-stream" in content_type:
            events = []
            for raw_line in body.decode("utf-8").splitlines():
                if raw_line.startswith("data:"):
                    events.append(raw_line[5:].strip())
            if not events:
                raise ProtocolFailure("SSE response carried no data events")
            message = json.loads(events[-1])
        else:
            message = json.loads(body.decode("utf-8"))
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
        conn = http.client.HTTPConnection(
            self._host, self._port, timeout=HTTP_TIMEOUT_S
        )
        try:
            conn.request(
                "POST", self._path, body=body, headers=self._headers(method, name)
            )
            return self._read_response(conn, request_id)
        finally:
            conn.close()

    # -- tools --------------------------------------------------------------

    def call_tool(self, name: str, arguments: dict) -> dict:
        """Call a tool, answering any consent elicitation with accept.

        Returns ``structuredContent`` from a complete result. Only the
        example's own operations reach the consent path.
        """
        params: dict = {"name": name, "arguments": arguments}
        result = self.request("tools/call", params, name=name)
        if result.get("resultType") == "input_required":
            request_state = result.get("requestState")
            if request_state is None:
                raise ProtocolFailure(
                    "input_required result without a requestState token"
                )
            params["requestState"] = request_state
            params["inputResponses"] = {
                "confirm": {"action": "accept", "content": {"confirmed": True}}
            }
            result = self.request("tools/call", params, name=name)
        if result.get("isError"):
            raise ToolFailure(
                name, result.get("structuredContent", {}).get("error", {})
            )
        if result.get("resultType") != "complete":
            raise ProtocolFailure(
                f"unexpected resultType: {result.get('resultType')!r}"
            )
        return result.get("structuredContent", {})

    def run_script(self, code: str, session_id: str = "cantilever-example") -> dict:
        """Run FreeCAD Python code; returns stdout/stderr/executed."""
        return self.call_tool("run_script", {"code": code, "session_id": session_id})


# ---------------------------------------------------------------------------
# Fixture and analysis.
# ---------------------------------------------------------------------------


def tool_payload(call: str, payload: dict) -> dict:
    print(f"-> {call}")
    print(f"   {json.dumps(payload, ensure_ascii=False)[:400]}")
    return payload


def resolve_tool_result(client: FreeCadMcpClient, name: str, arguments: dict) -> dict:
    """Call a structured tool and return its structuredContent payload."""
    payload = tool_payload(f"{name}{arguments}", client.call_tool(name, arguments))
    return payload


def build_fixture(client: FreeCadMcpClient) -> tuple[str, str, str]:
    """Create the example document, beam, analysis and modern solver."""
    new_doc = resolve_tool_result(
        client,
        "new_document",
        {"name": DOC},
    )
    doc_name = new_doc["name"]

    beam = resolve_tool_result(
        client,
        "create_object",
        {
            "document": doc_name,
            "type": "Part::Box",
            "name": BEAM,
            "properties": {"Length": L, "Width": B, "Height": H},
        },
    )["object"]["name"]

    analysis = resolve_tool_result(
        client,
        "create_object",
        {
            "document": doc_name,
            "type": "Fem::FemAnalysis",
            "name": ANALYSIS,
        },
    )["object"]["name"]

    # The modern solver is created explicitly by name: run_fem selects or
    # creates exactly one Fem::SolverCalculiX and never converts legacy
    # solver objects.
    resolve_tool_result(
        client,
        "create_object",
        {
            "document": doc_name,
            "type": "Fem::SolverCalculiX",
            "name": SOLVER,
        },
    )
    return doc_name, beam, analysis


# The rest of the FEM model — material card, fixed/loaded constraints with
# face resolution by centroid, load direction binding and the native Gmsh
# mesh factory — runs as one run_script block. The code only touches the
# document this example created.
FIXTURE_SCRIPT_TEMPLATE = """
import FreeCAD
import ObjectsFem

doc = FreeCAD.getDocument({doc_name!r})
beam = doc.getObject({beam_name!r})
analysis = doc.getObject({analysis_name!r})


def face_on_x(obj, value, label):
    for index, face in enumerate(obj.Shape.Faces, start=1):
        if abs(face.CenterOfMass.x - value) < 1e-6:
            return "Face" + str(index)
    raise RuntimeError("no face found at x=" + str(value) + " for " + label)


fixed_sub = face_on_x(beam, 0.0, "fixed face")
loaded_sub = face_on_x(beam, {length!r}, "loaded face")
if fixed_sub == loaded_sub:
    raise RuntimeError("fixed and loaded faces resolved to the same face")
print("resolved faces:", fixed_sub, loaded_sub)

material = ObjectsFem.makeMaterialSolid(doc, "Steel")
material.Material = {{
    "Name": "Steel",
    "Density": {rho!r},
    "YoungsModulus": {youngs!r},
    "PoissonRatio": {poisson!r},
}}
fixed = ObjectsFem.makeConstraintFixed(doc, "Fixed")
fixed.References = [(beam, fixed_sub)]

load = ObjectsFem.makeConstraintForce(doc, "Load")
load.References = [(beam, loaded_sub)]
z_edge = next(
    (
        "Edge" + str(index)
        for index, edge in enumerate(beam.Shape.Edges, start=1)
        if abs(edge.tangentAt(0).z) > 0.99
    ),
    None,
)
if z_edge is None:
    raise RuntimeError("no Z-tangent edge found for the load direction")
load.Direction = (beam, [z_edge])
load.Reversed = True
# NOTE: a bare float here means mm*kg/s^2 (100.0 would be 0.1 N).
# Always assign a quantity string such as "100 N".
load.Force = {force!r}
print("load direction:", z_edge, "force:", load.Force)

mesh = ObjectsFem.makeMeshGmsh(doc, "Mesh")
mesh.Shape = beam.Shape
mesh.ElementOrder = "2nd"
mesh.CharacteristicLengthMax = {mesh_max!r}
mesh.CharacteristicLengthMin = {mesh_min!r}

for member in (material, fixed, load, mesh):
    analysis.addObject(member)
doc.recompute()

print("mesh volumes:", mesh.FemMesh.VolumeCount if mesh.FemMesh else 0)
if not mesh.FemMesh or mesh.FemMesh.VolumeCount == 0:
    raise RuntimeError("Gmsh meshing produced no volume elements")
print("FIXTURE_READY")
"""


def prepare_fem_model(
    client: FreeCadMcpClient, doc_name: str, beam_name: str, analysis_name: str
) -> None:
    code = FIXTURE_SCRIPT_TEMPLATE.format(
        doc_name=doc_name,
        beam_name=beam_name,
        analysis_name=analysis_name,
        length=L,
        rho=RHO,
        youngs=f"{E_GPA} GPa",
        poisson=f"{NU:.6g}",
        force=f"{F_N} N",
        mesh_max=MESH_MAX,
        mesh_min=MESH_MIN,
    )
    result = tool_payload("run_script(...)", client.run_script(code))
    if not result.get("executed"):
        raise RuntimeError("run_script reports the code did not execute")
    if "FIXTURE_READY" not in result.get("stdout", ""):
        raise RuntimeError(
            "fixture script did not complete; stdout:\n" + result.get("stdout", "")
        )
    if result.get("stderr"):
        print("   script stderr:", result["stderr"].strip())


def range_lookup(blocks: list[dict], needle: str) -> tuple[str, dict] | None:
    """Find the first scalar or vector range whose key contains ``needle``."""
    for block in blocks:
        for kind in ("vectors", "scalars"):
            for key, value in block.get(kind, {}).items():
                if needle.lower() in key.lower():
                    return f"{kind}:{key}", value
    return None


def main() -> int:
    token = os.environ.get("FREECAD_MCP_TOKEN")
    if not token:
        print("FATAL: set FREECAD_MCP_TOKEN (Show Auth Token dialog).")
        return 2
    url = os.environ.get("FREECAD_MCP_URL", "http://127.0.0.1:9876/mcp")
    client = FreeCadMcpClient(url, token)

    discover = tool_payload("server/discover", client.request("server/discover"))
    listing = tool_payload("tools/list", client.request("tools/list"))
    tool_names = [tool["name"] for tool in listing["tools"]]
    if len(tool_names) != EXPECTED_TOOLS:
        print(
            f"FATAL: expected {EXPECTED_TOOLS} tools, got {len(tool_names)}: "
            f"{tool_names}"
        )
        return 3
    server_info = (discover.get("_meta") or {}).get(
        "io.modelcontextprotocol/serverInfo", {}
    )
    print(
        f"   server {server_info.get('name')} {server_info.get('version')}, "
        f"{len(tool_names)} tools"
    )

    doc_name, beam_name, analysis_name = build_fixture(client)
    prepare_fem_model(client, doc_name, beam_name, analysis_name)

    print("\nRunning FEM analysis (modern CalculiX pipeline) ...")
    result = tool_payload(
        f"run_fem({{'document': {doc_name!r}, 'analysis': {analysis_name!r}}})",
        client.call_tool("run_fem", {"document": doc_name, "analysis": analysis_name}),
    )
    if result.get("cancellation_requested"):
        print("   note: cancellation was requested for this solve")
    print(
        f"   pipeline {result['pipeline']!r}, solver {result['solver']!r}, "
        f"{result['aggregates']['block_count']} result blocks, "
        f"{result['aggregates']['point_count_sum']} points, "
        f"{result['aggregates']['cell_count_sum']} cells"
    )
    print(f"   results: {result['vtk_path']} (+{len(result['vtu_files'])} .vtu)")

    blocks = result["blocks"]
    displacement = range_lookup(blocks, "DISPL")
    stress = range_lookup(blocks, "mises")
    sigma_pred, delta_pred = analytic()
    sigma_fem = stress[1]["max"] if stress else None
    delta_fem = displacement[1]["max"] if displacement else None

    print("\n=== Comparison vs. Euler-Bernoulli beam theory ===")
    print(f"  sigma_max predicted (Mc/I)   : {sigma_pred:8.3f} MPa")
    print(
        f"  sigma_max FEM (von Mises)    : "
        f"{'n/a' if sigma_fem is None else format(sigma_fem, '8.3f')}"
        + ("" if sigma_fem is None else f" MPa  ratio {sigma_fem / sigma_pred:.2f}x")
        + (f"  [{stress[0]}]" if stress else "")
    )
    print(f"  delta_tip predicted (FL^3/3EI): {delta_pred:8.4f} mm")
    print(
        f"  delta_tip FEM (DISPL max)     : "
        f"{'n/a' if delta_fem is None else format(delta_fem, '8.4f')}"
        + ("" if delta_fem is None else f" mm  ratio {delta_fem / delta_pred:.2f}x")
        + (f"  [{displacement[0]}]" if displacement else "")
    )

    if not blocks or sigma_fem is None or delta_fem is None:
        print("\nFAILED: empty result blocks or missing stress/displacement ranges.")
        return 1
    if not (0.0 < sigma_fem < float("inf")) or not (0.0 < delta_fem < float("inf")):
        print("\nFAILED: non-finite FEM result ranges.")
        return 1

    # Close only the example's own document. It is dirty (the solver wrote
    # results into it), so the server requires consent; the client accepts
    # the elicitation round trip. No preexisting document is ever closed.
    tool_payload(
        f"close_document({{'document': {doc_name!r}}})",
        resolve_tool_result(client, "close_document", {"document": doc_name}),
    )
    print("\nDone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
