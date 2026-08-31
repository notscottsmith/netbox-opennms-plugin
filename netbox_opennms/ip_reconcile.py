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

from dcim.choices import InterfaceTypeChoices
from dcim.models import Device
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Q
from ipam.models import IPAddress, IPRange, Prefix

from .import_node import KIND_INTERFACE_MODELS, parse_discovery_payload
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
    # net_host is a django-netfields transform that only supports direct
    # equality (or net_host_contained) -- it can't be chained with __in, so
    # each address needs its own Q().
    query = Q()
    for ip in ip_addresses:
        query |= Q(address__net_host=ip)
    index = {}
    for row in IPAddress.objects.filter(query).select_related("vrf"):
        index[str(row.address.ip)] = row
    return index


def reconcile_interfaces(interfaces, matched_object, ip_index, *, vrf=None, scope=None):
    """Pure per-IP reconciliation (issue #30): walked interfaces -> verdicts.

    *interfaces* is the ``import_node.InterfaceProposal`` list for one node
    (``netmask`` included, issue #30). *matched_object* is the
    ``DiscoveredNode``'s own matched Device/VM, or ``None`` -- with no
    matched object there's nothing to check "correctly assigned to" against,
    so assignment is never checked (issue #31: confirming an IP on an
    unmatched node must still be able to read green). *ip_index* is
    ``netbox_ip_index()``'s result. *vrf*/*scope* are already resolved
    (``scope.resolve_vrf``/``scope.requisition_scope_site_and_location``) --
    this function does no DB reads of its own, mirroring ``scan.reconcile``'s
    pure/IO split.

    Green: a NetBox ``IPAddress`` exists at this address, assigned to one of
    *matched_object*'s own interfaces (when *matched_object* is set -- not
    checked at all when it isn't), and its ``vrf`` matches *vrf* (or *vrf* is
    ``None`` -- nothing to disagree with). Orange: the address exists but
    something differs (wrong VRF, or -- only when *matched_object* is set --
    unassigned or assigned to a different object). Red: no NetBox
    ``IPAddress`` exists at this address at all.
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
        if matched_object is not None:
            parent = _assigned_parent(row)
            if parent is None:
                diffs.append("unassigned")
            elif parent != matched_object:
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


class ConfirmRejected(Exception):
    """Raised when confirming an IP interface (issue #31) doesn't apply."""


def required_confirm_permissions(node, ip_address):
    """Permissions needed to confirm *ip_address* on *node* (issue #31).

    Computed from the same current verdict ``confirm_ip_interface`` itself
    re-derives, so a caller's permission check and the actual write can
    never drift apart into checking for one thing and creating another.
    Always includes ``ipam.add_ipaddress``; adds ``ipam.add_prefix`` or
    ``ipam.add_iprange`` depending on the proposal type, and --
    when *node* has a matched Device/VM -- ``dcim.add_interface`` or
    ``virtualization.add_vminterface``, since confirming also creates an
    interface on it in that case. Returns ``()`` for an unknown IP or one
    that isn't "red" -- nothing would be created, so nothing to permission-gate.
    """
    verdicts = {v.ip_address: v for v in reconcile_node_interfaces(node)}
    verdict = verdicts.get(ip_address)
    if verdict is None or verdict.verdict != "red":
        return ()
    perms = ["ipam.add_ipaddress"]
    if isinstance(verdict.proposal, PrefixProposal):
        perms.append("ipam.add_prefix")
    elif isinstance(verdict.proposal, IPRangeProposal):
        perms.append("ipam.add_iprange")
    if node.matched_object is not None:
        kind = "device" if isinstance(node.matched_object, Device) else "vm"
        perms.append(
            "dcim.add_interface"
            if kind == "device"
            else "virtualization.add_vminterface"
        )
    return tuple(perms)


def confirm_ip_interface(node, ip_address):
    """Create the reviewed Prefix/IPRange and IPAddress for one IP (issue #31).

    Re-derives *node*'s current verdicts via ``reconcile_node_interfaces`` --
    never trusts client-submitted proposal data -- and applies the matching
    verdict's proposal exactly as last reviewed; nothing is applied silently.
    Only a "red" verdict has anything to create (no NetBox ``IPAddress``
    exists yet); raises ``ConfirmRejected`` for an unknown IP or a
    green/orange one, since an *existing* address is a correction, not a
    creation, and out of this issue's scope.

    Available whether or not *node* has a Device/VM match: when it does, the
    created ``IPAddress`` is assigned to a new interface on it, mirroring
    ``import_node._create_interfaces_and_ips``; when it doesn't, there's
    nothing to assign it to, so it's created unassigned -- either way the
    next reconciliation reads this IP as green.
    """
    verdicts = {v.ip_address: v for v in reconcile_node_interfaces(node)}
    verdict = verdicts.get(ip_address)
    if verdict is None:
        raise ConfirmRejected(
            f"{ip_address!r} is not one of this node's IP interfaces."
        )
    if verdict.verdict != "red":
        raise ConfirmRejected(f"{ip_address} already exists in NetBox.")

    proposal = verdict.proposal
    with transaction.atomic():
        if isinstance(proposal, PrefixProposal):
            network = ipaddress.ip_network(proposal.prefix)
            defaults = {}
            if proposal.scope is not None:
                defaults["scope_type"] = ContentType.objects.get_for_model(
                    proposal.scope
                )
                defaults["scope_id"] = proposal.scope.pk
            Prefix.objects.get_or_create(
                prefix=proposal.prefix, vrf=proposal.vrf, defaults=defaults
            )
            prefixlen = network.prefixlen
        else:
            # Recomputed from *ip_address* rather than trusting
            # proposal.start_address/end_address's display-only string
            # format (no CIDR suffix) -- this is exactly the network
            # ``_propose`` sized the proposal from in the first place.
            network = classful_network(ip_address)
            IPRange.objects.get_or_create(
                start_address=f"{network[0]}/{network.prefixlen}",
                end_address=f"{network[-1]}/{network.prefixlen}",
                vrf=proposal.vrf,
            )
            prefixlen = 128 if network.version == 6 else 32

        matched = node.matched_object
        nic = None
        if matched is not None:
            kind = "device" if isinstance(matched, Device) else "vm"
            interface_model = KIND_INTERFACE_MODELS[kind]
            if kind == "device":
                nic = interface_model.objects.create(
                    device=matched,
                    name=f"opennms-{ip_address}",
                    type=InterfaceTypeChoices.TYPE_OTHER,
                )
            else:
                nic = interface_model.objects.create(
                    virtual_machine=matched, name=f"opennms-{ip_address}"
                )

        address = IPAddress(
            address=f"{ip_address}/{prefixlen}",
            vrf=proposal.vrf,
            assigned_object=nic,
        )
        address.full_clean()
        address.save()
    return address
