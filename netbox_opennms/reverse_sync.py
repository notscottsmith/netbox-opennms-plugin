# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""One-Time Sync: pull OpenNMS-gathered node data into NetBox (issue #23).

The reverse of the plugin's usual NetBox -> OpenNMS render/sync direction
(``jobs.SyncForeignSourceJob``): given a ``DiscoveredNode`` already matched
to a NetBox Device/VM (``DiscoveredNode.matched_object``, the same Foreign-ID
join ``scan.py`` uses), pull its SNMP interfaces and discovered neighbor
links and commit them — interfaces are created/updated on the object, and a
neighbor link with both endpoints matched becomes a NetBox cable.

Mirrors ``dryrun.py``/``scan.py``'s split: ``plan_reverse_sync`` builds the
change plan from already-fetched OpenNMS data (no network calls of its own —
it does read NetBox's current Interface/Cable state directly, since that's a
local DB read rather than a network hop worth isolating, unlike
``dryrun.diff``'s stricter no-DB-either purity) and ``run_reverse_sync`` is
the thin I/O wrapper that fetches per node, plans, and applies. #24 (bulk
One-Time Sync) reuses this same engine across every node on a Requisition's
Nodes tab.

The neighbor-link -> cable resolution (``_remote_discovered_node`` /
``_cable_endpoints``) is shared with the Node Links tab's manual
"Create cable" action (issue #16) — moved here from ``views.py`` so both the
one-off manual action and this bulk engine use exactly one join, not two.
"""

from dataclasses import dataclass, field

from dcim.choices import InterfaceTypeChoices
from dcim.models import Cable, Device, Interface
from django.core.exceptions import ValidationError

from .client import OpenNMSClient, OpenNMSError, parse_node_links
from .import_node import KIND_INTERFACE_MODELS


def _remote_discovered_node(local_node, link):
    """The ``DiscoveredNode`` for *link*'s remote endpoint, if any.

    ``link.remote_node_id`` is the OpenNMS node id parsed out of the payload's
    ``*Url`` field (see ``client.node_links._remote_node_id``) — every remote
    endpoint OpenNMS reports lives on the *same* server as the local node, so
    matching is scoped to ``local_node.server``.
    """
    from .models import DiscoveredNode

    if link.remote_node_id is None:
        return None
    return DiscoveredNode.objects.filter(
        server=local_node.server, opennms_node_id=link.remote_node_id
    ).first()


def _cable_endpoints(local_object, local_node, link):
    """Resolve *link* to ``(local_interface, remote_interface)``, or ``(None, reason)``.

    Both endpoints must already be matched/imported NetBox Devices (#8/#9) with
    an Interface named for the port OpenNMS reported, and neither interface may
    already be cabled — anything else is "not-yet-actionable", per #16's
    review-don't-guess principle, not an error.
    """
    if not isinstance(local_object, Device):
        return None, "This object isn't a Device, and can't be cabled."
    remote_node = _remote_discovered_node(local_node, link)
    remote_object = remote_node.matched_object if remote_node else None
    if remote_object is None:
        return (
            None,
            "The remote node for this link hasn't been matched or imported "
            "into NetBox yet.",
        )
    if not isinstance(remote_object, Device):
        return (
            None,
            f"Remote object is a {remote_object._meta.verbose_name}, "
            "which can't be cabled.",
        )
    local_interface = Interface.objects.filter(
        device=local_object, name=link.local_port
    ).first()
    if local_interface is None:
        return None, f"No interface named '{link.local_port}' on {local_object}."
    remote_interface = Interface.objects.filter(
        device=remote_object, name=link.remote_port
    ).first()
    if remote_interface is None:
        return None, f"No interface named '{link.remote_port}' on {remote_object}."
    if local_interface.cable_id or remote_interface.cable_id:
        return None, "One of these interfaces is already connected to a cable."
    return (local_interface, remote_interface), None


@dataclass
class InterfaceChange:
    """One SNMP interface's planned outcome on the target Device/VM."""

    action: str  # "create" | "update" | "unchanged"
    name: str
    description: str = ""
    enabled: bool = True
    changes: list = field(default_factory=list)  # human-readable, for "update"
    existing_pk: int = None  # set for "update"/"unchanged"


@dataclass
class LinkChange:
    """One discovered neighbor link's planned outcome (a cable, or skipped)."""

    link: object  # client.DiscoveredLink
    local_interface: object = None  # dcim.Interface | None
    remote_interface: object = None  # dcim.Interface | None
    blocked_reason: str = ""

    @property
    def actionable(self):
        return self.local_interface is not None and self.remote_interface is not None


@dataclass
class ReverseSyncPlan:
    """The full One-Time Sync change plan for one Device/VM (issue #23)."""

    netbox_object: object
    kind: str  # "device" | "vm"
    interfaces: list = field(default_factory=list)
    links: list = field(default_factory=list)

    @property
    def has_changes(self):
        return any(i.action != "unchanged" for i in self.interfaces) or any(
            row.actionable for row in self.links
        )


@dataclass
class ReverseSyncNodeData:
    """Already-fetched OpenNMS data for one node — the fetch/plan seam."""

    discovered_node: object  # models.DiscoveredNode
    snmp_interfaces: list = field(default_factory=list)
    node_links_payload: object = None


def _snmp_interface_name(raw):
    return raw.get("ifName") or raw.get("ifDescr") or f"if{raw.get('ifIndex', '')}"


def _snmp_interface_enabled(raw):
    status = raw.get("ifAdminStatus")
    if status is None:
        return True
    return str(status).strip().lower() in ("1", "up")


def _existing_interfaces(netbox_object, kind):
    interface_model = KIND_INTERFACE_MODELS[kind]
    filter_kwargs = (
        {"device": netbox_object}
        if kind == "device"
        else {"virtual_machine": netbox_object}
    )
    return {
        iface.name: iface
        for iface in interface_model.objects.filter(**filter_kwargs)
    }


def plan_reverse_sync(node_data, netbox_object):
    """Build the interface/cable change plan for *netbox_object* from *node_data*.

    ``node_data`` is a ``ReverseSyncNodeData`` — the SNMP interfaces and
    node-links payload OpenNMS already returned for
    ``node_data.discovered_node``. ``netbox_object`` MUST be the Device/VM
    ``node_data.discovered_node.matched_object`` resolves to — the caller's
    responsibility, like every other Foreign-ID join in this plugin.
    """
    kind = "device" if isinstance(netbox_object, Device) else "vm"
    existing = _existing_interfaces(netbox_object, kind)

    interfaces = []
    for raw in node_data.snmp_interfaces:
        if not isinstance(raw, dict):
            continue
        name = _snmp_interface_name(raw)
        description = raw.get("ifAlias") or ""
        enabled = _snmp_interface_enabled(raw)
        current = existing.get(name)
        if current is None:
            interfaces.append(
                InterfaceChange(
                    action="create", name=name, description=description, enabled=enabled
                )
            )
            continue
        changes = []
        if current.description != description:
            changes.append(
                f"description {current.description!r} → {description!r}"
            )
        if current.enabled != enabled:
            changes.append(f"enabled {current.enabled} → {enabled}")
        interfaces.append(
            InterfaceChange(
                action="update" if changes else "unchanged",
                name=name,
                description=description,
                enabled=enabled,
                changes=changes,
                existing_pk=current.pk,
            )
        )

    links = []
    if node_data.node_links_payload is not None:
        for link in parse_node_links(node_data.node_links_payload):
            endpoints, reason = _cable_endpoints(
                netbox_object, node_data.discovered_node, link
            )
            links.append(
                LinkChange(
                    link=link,
                    local_interface=endpoints[0] if endpoints else None,
                    remote_interface=endpoints[1] if endpoints else None,
                    blocked_reason=reason or "",
                )
            )

    return ReverseSyncPlan(
        netbox_object=netbox_object, kind=kind, interfaces=interfaces, links=links
    )


def fetch_node_data(client, discovered_node):
    """I/O: fetch this node's SNMP interfaces + neighbor links from OpenNMS."""
    snmp_interfaces = client.list_snmp_interfaces(discovered_node.opennms_node_id)
    node_links_payload = client.get_node_links(discovered_node.opennms_node_id)
    return ReverseSyncNodeData(
        discovered_node=discovered_node,
        snmp_interfaces=snmp_interfaces,
        node_links_payload=node_links_payload,
    )


def apply_reverse_sync_plan(plan):
    """I/O: commit *plan* — create/update Interfaces, create Cables for
    actionable links. Raises ``django.core.exceptions.ValidationError`` if a
    cable fails NetBox's own validation (e.g. a race with a concurrent edit).
    """
    interface_model = KIND_INTERFACE_MODELS[plan.kind]
    created = updated = cabled = 0
    for change in plan.interfaces:
        if change.action == "create":
            kwargs = {
                "name": change.name,
                "description": change.description,
                "enabled": change.enabled,
            }
            if plan.kind == "device":
                interface_model.objects.create(
                    device=plan.netbox_object,
                    type=InterfaceTypeChoices.TYPE_OTHER,
                    **kwargs,
                )
            else:
                interface_model.objects.create(
                    virtual_machine=plan.netbox_object, **kwargs
                )
            created += 1
        elif change.action == "update":
            nic = interface_model.objects.get(pk=change.existing_pk)
            nic.description = change.description
            nic.enabled = change.enabled
            nic.save(update_fields=["description", "enabled"])
            updated += 1
    for row in plan.links:
        if row.actionable:
            cable = Cable(
                a_terminations=[row.local_interface],
                b_terminations=[row.remote_interface],
            )
            cable.full_clean()
            cable.save()
            cabled += 1
    return created, updated, cabled


@dataclass
class ReverseSyncResult:
    """Per-node outcome of a One-Time Sync run (issue #23 — never all-or-nothing)."""

    discovered_node: object
    success: bool
    error: str = ""
    interfaces_created: int = 0
    interfaces_updated: int = 0
    cables_created: int = 0


def run_reverse_sync(server, nodes):
    """Fetch, plan, and apply One-Time Sync for each ``DiscoveredNode`` in *nodes*.

    One client for the whole batch (every node here belongs to *server*).
    Per-node try/except so one node's failure never aborts the rest — #24's
    bulk action needs a report per node, not a silent all-or-nothing result.
    """
    results = []
    with OpenNMSClient.from_server(server) as client:
        for node in nodes:
            netbox_object = node.matched_object
            if netbox_object is None:
                results.append(
                    ReverseSyncResult(
                        node, False, "No matched NetBox object to sync into."
                    )
                )
                continue
            try:
                node_data = fetch_node_data(client, node)
            except OpenNMSError as exc:
                results.append(ReverseSyncResult(node, False, str(exc)))
                continue
            plan = plan_reverse_sync(node_data, netbox_object)
            try:
                created, updated, cabled = apply_reverse_sync_plan(plan)
            except ValidationError as exc:
                results.append(ReverseSyncResult(node, False, str(exc)))
                continue
            results.append(
                ReverseSyncResult(
                    node,
                    True,
                    interfaces_created=created,
                    interfaces_updated=updated,
                    cables_created=cabled,
                )
            )
    return results


@dataclass
class ReverseSyncPreviewRow:
    """One node's preview row for the bulk (Requisition-level) action, #24."""

    discovered_node: object
    plan: object = None  # ReverseSyncPlan | None
    error: str = ""


def preview_reverse_sync(server, nodes):
    """I/O: fetch + plan (no apply) for every matched node in *nodes* — the
    Requisition-level bulk preview (issue #24 AC #2).

    Mirrors ``run_reverse_sync``'s per-node try/except, but stops short of
    ``apply_reverse_sync_plan`` so nothing commits until the operator reviews
    the aggregate plan. Nodes with no ``matched_object`` are skipped outright
    (there's nothing to plan against) rather than reported as an error row —
    unmatched nodes simply aren't part of what a "Pull" over this Requisition
    can act on.
    """
    rows = []
    with OpenNMSClient.from_server(server) as client:
        for node in nodes:
            netbox_object = node.matched_object
            if netbox_object is None:
                continue
            try:
                node_data = fetch_node_data(client, node)
            except OpenNMSError as exc:
                rows.append(ReverseSyncPreviewRow(node, error=str(exc)))
                continue
            plan = plan_reverse_sync(node_data, netbox_object)
            rows.append(ReverseSyncPreviewRow(node, plan=plan))
    return rows
