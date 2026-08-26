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

from dataclasses import dataclass, field

from dcim.models import Device
from virtualization.models import VirtualMachine

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
        nb = netbox_index.get(foreign_id) if foreign_id else None
        if nb is None:
            results.append(
                NodeMatch(node_id, label, foreign_source, foreign_id, location, "red")
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
