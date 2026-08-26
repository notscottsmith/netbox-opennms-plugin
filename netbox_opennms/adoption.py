# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Label-based adoption of pre-existing OpenNMS nodes (issue #4).

Before rendering a Sync, the caller fetches the Foreign Source's live OpenNMS
state (``client.get_requisition``) and passes it to
``existing_foreign_ids_by_label``, then ``adopt_foreign_ids`` rewrites each
resolved ``NodeSpec``'s foreign_id to reuse an existing node's Foreign ID when
its label matches unambiguously — letting the plugin take over a Foreign
Source that already has real nodes/history, managed by hand or other tooling,
instead of reassigning every node a fresh ``{prefix}-device-{pk}`` identity.
Adoption is unconditional: it does not check the configured
``foreign_id_prefix`` or any prior ``DeployedForeignSource`` record, so it also
grandfathers legacy ``device-{pk}``/``vm-{pk}`` nodes from before that setting
existed.

An ambiguous match — on either side: the label maps to more than one existing
Foreign ID, or more than one desired node in this Requisition shares the
label — is NOT adopted (the freshly-derived id is kept) and is reported as a
non-blocking warning, the same advisory convention as ``NodeSpec.warning``.

Both functions are pure (no writes, no network) given the parsed OpenNMS JSON
and the resolved ``NodeSpec`` list.
"""

from collections import Counter, defaultdict


def _as_list(value):
    """Normalize an OpenNMS JSON collection to a list (see ``dryrun._as_list``:
    the v1 REST serializer unwraps a single-element collection to a bare dict)."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def existing_foreign_ids_by_label(current_requisition):
    """node-label -> sorted distinct Foreign IDs, from a parsed OpenNMS
    requisition JSON (``client.get_requisition``'s return value, or ``None``
    for a never-synced Foreign Source — yields ``{}``, so nothing is adopted).
    """
    result = defaultdict(set)
    if not isinstance(current_requisition, dict):
        return {}
    for node in _as_list(current_requisition.get("node")):
        if not isinstance(node, dict):
            continue
        label = node.get("node-label")
        fid = node.get("foreign-id")
        if not label or not fid:
            continue
        result[label].add(fid)
    return {label: sorted(ids) for label, ids in result.items()}


def adopt_foreign_ids(nodes, existing_by_label):
    """Rewrite each ``NodeSpec.foreign_id`` in place to an unambiguous label
    match found in ``existing_by_label``. Nodes with no match, or an ambiguous
    one, keep the Foreign ID they already carry (from ``foreign_id_for``).

    Returns the list of non-blocking warning strings for any match skipped as
    ambiguous — callers should log these the same way as a resolution warning.
    """
    warnings = []
    if not existing_by_label:
        return warnings
    label_counts = Counter(node.node_label for node in nodes)
    for node in nodes:
        existing_ids = existing_by_label.get(node.node_label)
        if not existing_ids:
            continue
        if label_counts[node.node_label] > 1:
            warnings.append(
                f"{node.node_label}: matches more than one resolved node in this "
                "Requisition — skipping adoption to avoid an ambiguous Foreign ID."
            )
            continue
        if len(existing_ids) > 1:
            warnings.append(
                f"{node.node_label}: matches {len(existing_ids)} existing OpenNMS "
                "nodes sharing the same label — skipping adoption to avoid an "
                "ambiguous Foreign ID."
            )
            continue
        node.foreign_id = existing_ids[0]
    return warnings
