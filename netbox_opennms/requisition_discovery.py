# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Unmirrored Foreign Source discovery (issue #11).

Read-only: which of an OpenNMS Server's Foreign Sources/Requisitions have no
corresponding NetBox ``Requisition`` row. No import story exists for
Requisitions themselves (unlike Nodes/IP Ranges/VLANs) — visibility only, so
unlike ``scan.py`` (#7) nothing here is persisted to a model.
"""


def unmirrored_requisitions(opennms_names, netbox_names):
    """Names present in *opennms_names* but not in *netbox_names* (sorted).

    ``Requisition.name`` IS the Foreign Source name (models.py), so this is a
    plain set difference — no separate join key needed the way node matching
    needs the Foreign ID (``scan.reconcile``).
    """
    netbox_set = set(netbox_names)
    return sorted({name for name in opennms_names if name not in netbox_set})


def list_unmirrored(server):
    """Fetch *server*'s live Foreign Source names and diff against NetBox.

    The thin I/O wrapper around ``unmirrored_requisitions`` (mirrors
    ``scan.scan_server``). Raises ``OpenNMSError`` on a client failure —
    callers degrade per their own convention (AD-16).
    """
    from .client import OpenNMSClient
    from .models import Requisition

    with OpenNMSClient.from_server(server) as client:
        names = client.list_requisition_names()
    return unmirrored_requisitions(
        names, Requisition.objects.values_list("name", flat=True)
    )
