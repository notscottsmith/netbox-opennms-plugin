# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Discovered neighbor-link DTOs + defensive JSON parsing (Node Links, #15).

``GET /api/v2/enlinkd/{id}`` returns an ``EnlinkdDTO`` — one array per discovery
protocol (LLDP/CDP/bridge/OSPF/IS-IS), each entry describing one neighbor link
of the node. Field names are confirmed against the OpenNMS source
(``org.opennms.web.rest.model.v2`` DTOs, Horizon 36) since this endpoint isn't
documented in the OpenNMS REST API docs; parsed leniently regardless, since a
missing/empty array per protocol is normal (a node with only LLDP neighbors has
no ``cdpLinkNodes`` key at all).

Each protocol also carries a ``*Url`` field for its remote endpoint (e.g.
``lldpRemChassisIdUrl``), a web-UI link OpenNMS builds via a shared
``getNodeUrl(nodeid)``/``getSnmpInterfaceUrl(nodeid, ifindex)`` helper
(``EnLinkdElementFactory``, Horizon 36) that always embeds the remote node's
OpenNMS node ID as a ``node=<id>`` query parameter — confirmed by reading that
helper's source, since (like the rest of this endpoint) it isn't documented.
That id is how issue #16 resolves a link's remote endpoint to a NetBox object.
"""

import re
from dataclasses import dataclass

_NODE_ID_RE = re.compile(r"[?&]node=(\d+)")


def _remote_node_id(url):
    """The remote OpenNMS node id embedded in a link's ``*Url`` field, if any."""
    if not url:
        return None
    match = _NODE_ID_RE.search(url)
    return int(match.group(1)) if match else None


@dataclass(frozen=True)
class DiscoveredLink:
    """One discovered neighbor link, normalized across discovery protocols."""

    protocol: str  # "LLDP" / "CDP" / "Bridge" / "OSPF" / "IS-IS"
    local_port: str
    remote_device: str
    remote_port: str
    remote_node_id: int | None = None


def _entries(payload, key):
    """Coerce *payload[key]* into a list of dicts (absent/null/bare-object safe)."""
    if not isinstance(payload, dict):
        return []
    node = payload.get(key)
    if node is None:
        return []
    if isinstance(node, dict):
        node = [node]
    if not isinstance(node, list):
        return []
    return [entry for entry in node if isinstance(entry, dict)]


def _link(
    protocol, raw, local_port_key, remote_device_key, remote_port_key, remote_url_key
):
    return DiscoveredLink(
        protocol=protocol,
        local_port=raw.get(local_port_key) or "",
        remote_device=raw.get(remote_device_key) or "",
        remote_port=raw.get(remote_port_key) or "",
        remote_node_id=_remote_node_id(raw.get(remote_url_key)),
    )


def parse_node_links(payload):
    """Parse an ``EnlinkdDTO`` payload into a flat list of ``DiscoveredLink``.

    A bridge link is one-to-many (``BridgeLinkRemoteNodes``), so it expands into
    one ``DiscoveredLink`` per remote. ``payload`` may be ``None`` (no node
    found) — returns an empty list.
    """
    links = []
    for raw in _entries(payload, "lldpLinkNodes"):
        links.append(
            _link(
                "LLDP",
                raw,
                "lldpLocalPort",
                "lldpRemChassisId",
                "ldpRemPort",
                "lldpRemChassisIdUrl",
            )
        )
    for raw in _entries(payload, "cdpLinkNodes"):
        links.append(
            _link(
                "CDP",
                raw,
                "cdpLocalPort",
                "cdpCacheDevice",
                "cdpCacheDevicePort",
                "cdpCacheDeviceUrl",
            )
        )
    for raw in _entries(payload, "ospfLinkNodes"):
        links.append(
            _link(
                "OSPF",
                raw,
                "ospfLocalPort",
                "ospfRemRouterId",
                "ospfRemPort",
                "ospfRemRouterUrl",
            )
        )
    for raw in _entries(payload, "isisLinkNodes"):
        links.append(
            _link(
                "IS-IS",
                raw,
                "isisCircIfIndex",
                "isisISAdjNeighSysID",
                "isisISAdjNeighPort",
                "isisISAdjUrl",
            )
        )
    for raw in _entries(payload, "bridgeLinkNodes"):
        local_port = raw.get("bridgeLocalPort") or ""
        for remote in _entries(raw, "BridgeLinkRemoteNodes"):
            links.append(
                DiscoveredLink(
                    protocol="Bridge",
                    local_port=local_port,
                    remote_device=remote.get("bridgeRemote") or "",
                    remote_port=remote.get("bridgeRemotePort") or "",
                    remote_node_id=_remote_node_id(remote.get("bridgeRemoteUrl")),
                )
            )
    return links
