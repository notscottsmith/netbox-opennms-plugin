# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""OpenNMS Discovery scan reconciliation (issue #7).

Mirrors ``dryrun.py``'s ``diff()``: a single pure function (``reconcile``)
compares already-fetched OpenNMS node inventory against an already-built
NetBox foreign-id index and returns a green/orange/red match verdict per
node, with field-level diff detail for orange. No network access inside it —
``scan_server`` is the thin I/O wrapper that fetches both sides and calls it.

Node identity is the Foreign ID (AD-8/AD-14), the same join key ``adoption``
uses — a node is a match regardless of which OpenNMS Server a NetBox object
happens to be Scope-bound to, so ``netbox_index`` covers every Device/VM.
"""

import datetime
from dataclasses import dataclass, field

from dcim.models import Device, Site
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from virtualization.models import VirtualMachine

from . import import_node
from .derivation import foreign_id_for
from .membership import resolve_all

# Maps the "kind" tag in a netbox_index() entry back to its model, so a
# caller that wants to attach a real ContentType/pk (e.g. DiscoveredNode)
# doesn't need to re-derive it. reconcile() itself never imports ContentType.
KIND_MODELS = {"device": Device, "vm": VirtualMachine}


@dataclass
class NodeMatch:
    """One OpenNMS node's reconciled state against NetBox."""

    opennms_node_id: int
    label: str
    foreign_source: str
    foreign_id: str
    location: str
    verdict: str  # "green" | "orange" | "red"
    diff_detail: list = field(default_factory=list)
    matched_kind: str = ""  # "device" | "vm" | "" (red = no match)
    matched_pk: int = None
    created: object = None  # aware datetime | None (settle detection, issue #27)


def _parse_node_created(node):
    """Parse an OpenNMS node's ``createTime`` into an aware ``datetime``, or
    ``None`` if absent/unparseable (issue #27's settle-detection signal — a
    scan has "gone quiet" once no new node has appeared for a while).

    A naive result (no offset in the source string) is treated as UTC, since
    comparing a naive and an aware datetime raises rather than telling us
    anything useful about "how long ago".
    """
    raw = node.get("createTime")
    if not raw or not isinstance(raw, str):
        return None
    parsed = parse_datetime(raw)
    if parsed is None:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed, datetime.UTC)
    return parsed


def _netbox_node(obj, kind, locations):
    return {
        "kind": kind,
        "pk": obj.pk,
        "label": obj.name or "",
        "location": locations.get(foreign_id_for(obj), ""),
    }


def netbox_index():
    """foreign_id -> normalized NetBox node data, across every Device and VM.

    ``location`` is the OpenNMS Monitoring Location the renderer would actually
    emit for a monitored object — Requisition/override precedence
    (``membership.resolve_node``), NOT the object's NetBox Site (an unrelated
    namespace) — sourced from one ``resolve_all()`` pass. An object matching no
    Requisition (unmonitored, excluded, or conflicted) has no expected
    location and compares against blank.
    """
    locations = {
        node.foreign_id: node.location
        for resolution in resolve_all()
        for node in resolution.nodes
    }
    index = {}
    for obj in Device.objects.select_related("site"):
        index[foreign_id_for(obj)] = _netbox_node(obj, "device", locations)
    for obj in VirtualMachine.objects.select_related("site", "cluster"):
        index[foreign_id_for(obj)] = _netbox_node(obj, "vm", locations)
    return index


def reconcile(opennms_nodes, netbox_index):
    """Pure reconciliation: OpenNMS node dicts + a foreign-id index -> verdicts.

    ``opennms_nodes`` is the list ``OpenNMSClient.list_nodes()`` returns (or an
    equivalent hand-built list in tests). ``netbox_index`` is the dict
    ``netbox_index()`` builds (or an equivalent hand-built dict in tests).

    Green: the OpenNMS node's Foreign ID resolves to a NetBox object and its
    label/location agree. Orange: it resolves but at least one of those
    differs (``diff_detail`` explains which). Red: the Foreign ID does not
    resolve to any NetBox Device/VM at all — a candidate for later import.
    """
    results = []
    for node in opennms_nodes:
        if not isinstance(node, dict):
            continue
        node_id = node.get("id")
        label = node.get("label") or ""
        foreign_source = node.get("foreignSource") or ""
        foreign_id = node.get("foreignId") or ""
        location = node.get("location") or ""
        created = _parse_node_created(node)
        nb = netbox_index.get(foreign_id) if foreign_id else None
        if nb is None:
            results.append(
                NodeMatch(
                    node_id,
                    label,
                    foreign_source,
                    foreign_id,
                    location,
                    "red",
                    created=created,
                )
            )
            continue
        diffs = []
        if nb["label"] != label:
            diffs.append(f"label: OpenNMS={label!r} NetBox={nb['label']!r}")
        if nb["location"] != location:
            diffs.append(f"location: OpenNMS={location!r} NetBox={nb['location']!r}")
        verdict = "orange" if diffs else "green"
        results.append(
            NodeMatch(
                node_id,
                label,
                foreign_source,
                foreign_id,
                location,
                verdict,
                diffs,
                nb["kind"],
                nb["pk"],
                created=created,
            )
        )
    return results


def scan_server(server):
    """Fetch *server*'s live node inventory and reconcile it against NetBox.

    The thin I/O wrapper around ``reconcile`` (mirrors ``dryrun.dry_run``):
    one client round-trip, then the pure function. Raises ``OpenNMSError`` on
    a client failure — callers degrade per their own convention (AD-16).
    """
    from .client import OpenNMSClient

    with OpenNMSClient.from_server(server) as client:
        nodes = client.list_nodes()
    return reconcile(nodes, netbox_index())


def scan_discovery(discovery_scan):
    """Fetch one Discovery Scan's own live nodes and reconcile them against
    NetBox — the polling counterpart to ``scan_server`` (issue #27).

    Scoped to *discovery_scan*'s own throwaway Foreign Source
    (``list_nodes(foreign_source=...)``), so a scan's poll never sees nodes
    belonging to another scan or Requisition on the same Server. Raises
    ``OpenNMSError`` on a client failure, like ``scan_server``.
    """
    from .client import OpenNMSClient

    with OpenNMSClient.from_server(discovery_scan.server) as client:
        nodes = client.list_nodes(foreign_source=discovery_scan.foreign_source)
    return reconcile(nodes, netbox_index())


def walk_node(client, node, overrides):
    """Fetch *node*'s live OpenNMS detail/interfaces/services and persist it
    onto the ``DiscoveredNode`` row (issue #28, ADR 0007).

    The I/O wrapper around ``import_node.build_proposal``/
    ``compute_completeness_gaps`` — mirrors ``scan_server``'s split from
    ``reconcile``. Called once per newly-upserted Discovery Scan node
    (``PollDiscoveryScansJob``); a node already walked is never re-fetched by
    the caller (gated on ``walked_at``), so this itself always (re)writes.
    """
    detail = client.get_node(node.opennms_node_id) or {}
    ip_interfaces = client.list_ip_interfaces(node.opennms_node_id)
    services_by_ip = {}
    for iface in ip_interfaces:
        ip = iface.get("ipAddress") if isinstance(iface, dict) else None
        if ip:
            services_by_ip[ip] = client.list_services(node.opennms_node_id, ip)

    proposal = import_node.build_proposal(
        node, detail, ip_interfaces, services_by_ip, overrides, Site
    )
    node.node_detail = detail
    node.ip_interfaces = ip_interfaces
    node.services_by_ip = services_by_ip
    node.completeness_gaps = import_node.compute_completeness_gaps(
        proposal, proposal.interfaces
    )
    node.walked_at = timezone.now()
    node.save(
        update_fields=[
            "node_detail",
            "ip_interfaces",
            "services_by_ip",
            "completeness_gaps",
            "walked_at",
        ]
    )


def upsert_discovered_nodes(server, matches, *, discovery_scan=None):
    """Upsert *matches* as ``DiscoveredNode`` rows for *server*, keyed on
    ``(server, opennms_node_id)`` (issue #7), and delete any row for a node no
    longer present. Returns the list of ``opennms_node_id`` values seen.

    Shared by the per-Server scan view (issue #7) and the per-Discovery-Scan
    poll (issue #27). When *discovery_scan* is given, each upserted row is
    stamped with it AND stale-row cleanup is scoped to that scan's own rows
    only — a scan's poll (filtered to its own Foreign Source) must never
    delete rows belonging to a different scan or to a general per-Server scan
    on the same Server.
    """
    from django.contrib.contenttypes.models import ContentType

    from .models import DiscoveredNode

    linked_ids = set(
        server.discovered_nodes.filter(resolution="linked").values_list(
            "opennms_node_id", flat=True
        )
    )
    seen_ids = []
    for match in matches:
        seen_ids.append(match.opennms_node_id)
        defaults = {
            "label": match.label,
            "foreign_source": match.foreign_source,
            "foreign_id": match.foreign_id,
            "location": match.location,
        }
        if discovery_scan is not None:
            defaults["discovery_scan"] = discovery_scan
        # A manually-linked row's match came from the operator, not this
        # scan's Foreign-ID reconciliation — never overwrite it (issue #8).
        if match.opennms_node_id not in linked_ids:
            matched_object_type = None
            if match.matched_kind:
                matched_object_type = ContentType.objects.get_for_model(
                    KIND_MODELS[match.matched_kind]
                )
            defaults.update(
                {
                    "verdict": match.verdict,
                    "diff_detail": match.diff_detail,
                    "matched_object_type": matched_object_type,
                    "matched_object_id": match.matched_pk,
                }
            )
        DiscoveredNode.objects.update_or_create(
            server=server,
            opennms_node_id=match.opennms_node_id,
            defaults=defaults,
        )
    stale = server.discovered_nodes.exclude(opennms_node_id__in=seen_ids)
    if discovery_scan is not None:
        stale = stale.filter(discovery_scan=discovery_scan)
    stale.delete()
    return seen_ids
