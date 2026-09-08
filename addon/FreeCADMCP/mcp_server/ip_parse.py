"""Parsing of allowed peer IP/subnet lists.

Extracted from ``rpc_server.ip_filter`` without its XML-RPC server or FreeCAD
imports so the v2 transport and settings modules stay GUI-independent.
"""

import ipaddress
import re

_COMMA_SEP_RE = re.compile(r"^\s*[^,\s]+(\s*,\s*[^,\s]+)*\s*$")


def validate_allowed_ips(allowed_ips_str):
    """Validate a comma-separated string of IP addresses/subnets.

    Returns a ``(valid, errors)`` tuple. ``valid`` is a list of normalized
    entry strings that passed validation; ``errors`` is a list of
    human-readable error messages (empty when the input is fully valid).
    An empty or whitespace-only string is valid and means *open*: any peer
    is accepted (remote mode then relies on the token alone).
    """
    errors = []

    if not allowed_ips_str or not allowed_ips_str.strip():
        return [], []

    if not _COMMA_SEP_RE.match(allowed_ips_str):
        return [], [
            "Malformed list — check for leading/trailing commas, "
            "double commas, or missing separators."
        ]

    valid = []
    for entry in allowed_ips_str.split(","):
        entry = entry.strip()
        try:
            ipaddress.ip_network(entry, strict=False)
            valid.append(entry)
        except ValueError:
            errors.append(f"Invalid IP/subnet: '{entry}'")
    return valid, errors


def parse_allowed_networks(allowed_ips_str):
    """Strictly parse a comma-separated IP/subnet string.

    Returns a list of :class:`ipaddress.ip_network` objects. Raises
    :class:`ValueError` listing every invalid entry — callers must fail
    closed instead of silently weakening peer restrictions. An empty string
    parses to an empty list (open).
    """
    valid, errors = validate_allowed_ips(allowed_ips_str)
    if errors:
        raise ValueError("; ".join(errors))
    return [ipaddress.ip_network(entry, strict=False) for entry in valid]
