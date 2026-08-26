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
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class DiscoveredLink:
    """One discovered neighbor link, normalized across discovery protocols."""

    protocol: str  # "LLDP" / "CDP" / "Bridge" / "OSPF" / "IS-IS"
    local_port: str
    remote_device: str
    remote_port: str


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


def _link(protocol, raw, local_port_key, remote_device_key, remote_port_key):
    return DiscoveredLink(
        protocol=protocol,
        local_port=raw.get(local_port_key) or "",
        remote_device=raw.get(remote_device_key) or "",
        remote_port=raw.get(remote_port_key) or "",
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
            _link("LLDP", raw, "lldpLocalPort", "lldpRemChassisId", "ldpRemPort")
        )
    for raw in _entries(payload, "cdpLinkNodes"):
        links.append(
            _link("CDP", raw, "cdpLocalPort", "cdpCacheDevice", "cdpCacheDevicePort")
        )
    for raw in _entries(payload, "ospfLinkNodes"):
        links.append(
            _link("OSPF", raw, "ospfLocalPort", "ospfRemRouterId", "ospfRemPort")
        )
    for raw in _entries(payload, "isisLinkNodes"):
        links.append(
            _link(
                "IS-IS",
                raw,
                "isisCircIfIndex",
                "isisISAdjNeighSysID",
                "isisISAdjNeighPort",
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
                )
            )
    return links
