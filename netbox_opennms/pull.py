# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Pull OpenNMS metadata back into NetBox (RD-3 pull-back, the inverse of push).

``MetadataEntry`` pushes NetBox → OpenNMS; this module is the other direction —
discover the ``(context, key)`` pairs actually present on a Requisition's live
nodes, let the operator map each to a safe NetBox destination
(``MetadataPullMapping``), then apply those mappings by writing the observed
values into NetBox. Both steps are explicit, operator-invoked actions, never
part of the normal render/sync job (AD-3's side-effect-free rendering stays
intact; this is a distinct, deliberate write path).
"""

from .client import OpenNMSError
from .membership import resolve


def _meta_data_entries(requisition_node):
    """``(context, key, value)`` triads from one Requisition-node document.

    Mirrors ``views._requisition_entries``'s dict-or-list tolerance for the
    ``meta-data`` list (``{"context", "key", "value"}`` — the
    ``RequisitionMetaData`` shape); an entry missing any required field is
    skipped rather than raised on.
    """
    triads = []
    for entry in requisition_node.get("meta-data") or []:
        if not isinstance(entry, dict):
            continue
        context, key, value = (
            entry.get("context"),
            entry.get("key"),
            entry.get("value"),
        )
        if context and key and value is not None:
            triads.append((context, key, value))
    return triads


def discover_requisition_metadata_keys(requisition, client):
    """Distinct ``(context, key)`` pairs observed across a Requisition's live nodes.

    Fetches ``client.get_requisition_node`` for each currently-matched member
    (via ``membership.resolve``) and collects every metadata key it carries.
    A member with no resolvable node, or whose fetch fails, is skipped — this
    is best-effort discovery, not a hard requirement that every member agree.
    """
    resolution = resolve(requisition.name)
    if resolution is None:
        return []
    seen = set()
    pairs = []
    for node in resolution.nodes:
        try:
            requisition_node = client.get_requisition_node(
                requisition.name, node.foreign_id
            )
        except OpenNMSError:
            continue
        if not requisition_node:
            continue
        for context, key, _value in _meta_data_entries(requisition_node):
            if (context, key) not in seen:
                seen.add((context, key))
                pairs.append((context, key))
    return pairs


def _write_target(obj, netbox_target, value):
    """Write *value* to *obj* at ``netbox_target`` (a safe field or ``cf_<name>``)."""
    if netbox_target.startswith("cf_"):
        obj.custom_field_data[netbox_target[3:]] = value
    else:
        setattr(obj, netbox_target, value)


def apply_pull_mappings(requisition, client):
    """Write each ``MetadataPullMapping``'s observed value into its NetBox target.

    For every currently-matched member, fetch its Requisition-node document
    once and apply every configured mapping whose ``(context, key)`` is
    present on it. Returns the number of objects updated. A member with no
    resolvable node, or whose fetch fails, is skipped (best-effort, like
    discovery) — this is an explicit operator action, not part of Sync.
    """
    resolution = resolve(requisition.name)
    if resolution is None:
        return 0
    mappings = list(requisition.pull_mappings.all())
    if not mappings:
        return 0
    updated = 0
    for node in resolution.nodes:
        if node.netbox_object is None:
            continue
        try:
            requisition_node = client.get_requisition_node(
                requisition.name, node.foreign_id
            )
        except OpenNMSError:
            continue
        if not requisition_node:
            continue
        values = {
            (context, key): value
            for context, key, value in _meta_data_entries(requisition_node)
        }
        changed = False
        for mapping in mappings:
            value = values.get((mapping.context, mapping.key))
            if value is None:
                continue
            _write_target(node.netbox_object, mapping.netbox_target, value)
            changed = True
        if changed:
            node.netbox_object.save()
            updated += 1
    return updated
