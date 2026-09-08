"""Embedded MCP server for the FreeCAD add-on.

Modules in this package stay GUI-independent (no FreeCAD imports at module
level) until the server is explicitly started by ``mcp_server.server``.

There is deliberately no re-export surface here: import submodules directly
(e.g. ``from mcp_server import protocol``) so every consumer names the module
its wire contract comes from.
"""
