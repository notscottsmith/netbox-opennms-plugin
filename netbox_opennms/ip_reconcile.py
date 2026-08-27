# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Per-IP reconciliation of a Discovered Node's walked interfaces against
NetBox (issue #30, ADR 0008/0009).

Mirrors ``scan.py``'s ``reconcile()``/``netbox_index()`` split:
``reconcile_interfaces`` is pure (already-parsed ``InterfaceProposal``s + an
already-built NetBox IP index + an already-resolved VRF/scope, no client or
extra DB access of its own), ``reconcile_node_interfaces`` is the thin
DB-only wrapper a caller uses to get all three built for one
``DiscoveredNode``. Unlike node-level reconciliation this never touches
``OpenNMSClient`` at all -- it reconciles a node's already-walked (issue
#28), already-persisted ``ip_interfaces``/``services_by_ip`` against NetBox.
"""

import ipaddress
from dataclasses import dataclass, field

from ipam.models import IPAddress

from .import_node import parse_discovery_payload
from .scope import requisition_scope_site_and_location, resolve_vrf

# RFC1918 classful sizing (ADR 0008): the private space an address falls in
# decides how coarse a proposed IP Range is when OpenNMS gave no netmask to
# compute a real prefix from.
_RFC1918_CLASSFUL = (
    (ipaddress.ip_network("10.0.0.0/8"), 8),
    (ipaddress.ip_network("172.16.0.0/12"), 16),
    (ipaddress.ip_network("192.168.0.0/16"), 24),
)


@dataclass
class PrefixProposal:
    """A ``Prefix`` to propose (never auto-created) for a known-netmask IP."""

    prefix: str
    vrf: object = None
    scope: object = None  # a Site or Location, or None


@dataclass
class IPRangeProposal:
    """An ``IPRange`` to propose for an IP with no usable netmask, sized per
    RFC1918 classful convention (ADR 0008)."""

    start_address: str
    end_address: str
    vrf: object = None


@dataclass
class InterfaceVerdict:
    """One walked IP interface's reconciled state against NetBox (issue #30)."""

    ip_address: str
    netmask: str = ""
    verdict: str = ""  # "green" | "orange" | "red"
    diff_detail: list = field(default_factory=list)
    proposal: object = None  # PrefixProposal | IPRangeProposal | None


def classful_network(ip_address):
    """The RFC1918-classful network *ip_address* falls in (ADR 0008): /8 for
    10.0.0.0/8, /16 for 172.16.0.0/12, /24 for 192.168.0.0/16; anything else,
    including IPv6, sizes to a flat /24 or /64.
    """
    addr = ipaddress.ip_address(ip_address)
    if addr.version == 6:
        return ipaddress.ip_network(f"{addr}/64", strict=False)
    for network, prefixlen in _RFC1918_CLASSFUL:
        if addr in network:
            return ipaddress.ip_network(f"{addr}/{prefixlen}", strict=False)
    return ipaddress.ip_network(f"{addr}/24", strict=False)


def _parse_netmask_network(ip_address, netmask):
    """*ip_address*/*netmask* as an ``ip_network``, or ``None`` if unparseable
    -- OpenNMS's ``netMask`` is only ever populated when SNMP was reachable
    (ADR 0008), and even then may not parse cleanly (e.g. a malformed or
    IPv6-incompatible value), in which case this is treated the same as "no
    netmask" rather than raising.
    """
    if not netmask:
        return None
    try:
        return ipaddress.ip_network(f"{ip_address}/{netmask}", strict=False)
    except ValueError:
        return None


def _propose(iface, vrf, scope):
    network = _parse_netmask_network(iface.ip_address, iface.netmask)
    if network is not None:
        return PrefixProposal(prefix=str(network), vrf=vrf, scope=scope)
    network = classful_network(iface.ip_address)
    return IPRangeProposal(
        start_address=str(network[0]), end_address=str(network[-1]), vrf=vrf
    )


def _assigned_parent(ip_address_row):
    """The Device/VM an ``ipam.IPAddress`` row's assignment belongs to, or
    ``None`` if unassigned -- ``assigned_object`` is an Interface/VMInterface
    (or another assignable type this plugin never proposes onto), so this
    walks one level further to the object a ``DiscoveredNode.matched_object``
    would actually be.
    """
    assigned = ip_address_row.assigned_object
    if assigned is None:
        return None
    return getattr(assigned, "device", None) or getattr(
        assigned, "virtual_machine", None
    )


def netbox_ip_index(ip_addresses):
    """ip string -> NetBox ``IPAddress`` row, for every address in *ip_addresses*.

    Scoped to just the addresses a node's walk actually returned, unlike
    ``scan.netbox_index()`` (which must cover every Device/VM, since node
    identity has no narrower key to filter by) -- a node's own IP set is a
    small, known list, so there's no need to index the whole ``ipam.IPAddress``
    table.
    """
    if not ip_addresses:
        return {}
    index = {}
    for row in IPAddress.objects.filter(
        address__net_host__in=ip_addresses
    ).select_related("vrf"):
        index[str(row.address.ip)] = row
    return index


def reconcile_interfaces(interfaces, matched_object, ip_index, *, vrf=None, scope=None):
    """Pure per-IP reconciliation (issue #30): walked interfaces -> verdicts.

    *interfaces* is the ``import_node.InterfaceProposal`` list for one node
    (``netmask`` included, issue #30). *matched_object* is the
    ``DiscoveredNode``'s own matched Device/VM, or ``None`` -- with no
    matched object there's nothing to check "correctly assigned to" against,
    so an existing address can only ever read orange (assigned elsewhere or
    unassigned) or green never applies via assignment, only via a bare VRF
    match. *ip_index* is ``netbox_ip_index()``'s result. *vrf*/*scope* are
    already resolved (``scope.resolve_vrf``/
    ``scope.requisition_scope_site_and_location``) -- this function does no
    DB reads of its own, mirroring ``scan.reconcile``'s pure/IO split.

    Green: a NetBox ``IPAddress`` exists at this address, assigned to one of
    *matched_object*'s own interfaces (when *matched_object* is set), and its
    ``vrf`` matches *vrf* (or *vrf* is ``None`` -- nothing to disagree with).
    Orange: the address exists but something differs (wrong VRF, unassigned,
    or assigned to a different object). Red: no NetBox ``IPAddress`` exists at
    this address at all.
    """
    results = []
    for iface in interfaces:
        row = ip_index.get(iface.ip_address)
        if row is None:
            results.append(
                InterfaceVerdict(
                    iface.ip_address,
                    iface.netmask,
                    "red",
                    proposal=_propose(iface, vrf, scope),
                )
            )
            continue
        diffs = []
        parent = _assigned_parent(row)
        if parent is None:
            diffs.append("unassigned")
        elif matched_object is None or parent != matched_object:
            diffs.append(f"assigned to {parent!r}, expected {matched_object!r}")
        if vrf is not None and row.vrf_id != vrf.pk:
            diffs.append(f"VRF: NetBox={row.vrf!r} expected={vrf!r}")
        verdict = "orange" if diffs else "green"
        results.append(
            InterfaceVerdict(
                iface.ip_address,
                iface.netmask,
                verdict,
                diffs,
                proposal=_propose(iface, vrf, scope) if verdict == "orange" else None,
            )
        )
    return results


def reconcile_node_interfaces(node):
    """The full per-IP reconciliation for one walked ``DiscoveredNode`` (issue #30).

    DB-only wrapper around ``reconcile_interfaces``: parses the node's
    already-persisted ``ip_interfaces`` (issue #28's ``scan.walk_node``),
    builds the NetBox IP index, and resolves VRF/scope through the node's
    Discovery Scan's Requisition (ADR 0009). Returns ``[]`` for an unwalked
    node (``walked_at`` unset, or no ``discovery_scan``/``requisition`` to
    resolve scope from) -- nothing stored yet to reconcile against.
    """
    if not node.walked_at:
        return []
    interfaces, _ = parse_discovery_payload(node.ip_interfaces, node.services_by_ip)
    ip_index = netbox_ip_index([iface.ip_address for iface in interfaces])
    vrf = scope = None
    scan = node.discovery_scan
    if scan is not None and scan.requisition_id:
        site, location = requisition_scope_site_and_location(scan.requisition)
        vrf = resolve_vrf(site=site, location=location)
        scope = location or site
    return reconcile_interfaces(
        interfaces, node.matched_object, ip_index, vrf=vrf, scope=scope
    )
