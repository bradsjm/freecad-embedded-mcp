"""Tool modules for the embedded MCP v2 server.

Every module here exports ``TOOL_DEFINITIONS`` (wire schemas) and
``HANDLERS`` (``name -> callable(ctx, arguments)``); ``mcp_server.server``
registers them. Tool modules import shared contracts relatively
(``..protocol``, ``..object_validation``) and sibling tool modules lazily
inside handlers.
"""
