# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Detail-page extensions for monitored objects (Story 4.2 observability).

Adds the OpenNMS last-sync panel to a Device's / VirtualMachine's detail page so
an operator sees provisioning status without opening the Monitoring Profile.
NetBox auto-discovers ``template_extensions`` — no PluginConfig change.
"""

from django.urls import reverse
from netbox.plugins import PluginTemplateExtension

from .jobs import sync_status_for
from .models import DiscoveredNode

PANEL = "netbox_opennms/inc/sync_status_panel.html"


class _SyncStatusPanel(PluginTemplateExtension):
    """Render the last-sync panel for a monitored object, or nothing if unmonitored."""

    def right_page(self):
        # Self-guard: an observability panel must never break the host object's
        # detail page, so degrade to nothing on any unexpected error.
        try:
            obj = self.context["object"]
            status = sync_status_for(obj)
            discovered_node = DiscoveredNode.for_object(obj)
            # Show the panel when the object is monitored, has sync history, is
            # CONFLICTED (the conflict must be visible where the operator looks —
            # C1), or has an OpenNMS Discovery match to pull from (#23); an
            # otherwise-unmonitored object with none of the above gets nothing.
            if status is None or not (
                status["governed"]
                or status["job"]
                or status["conflicts"]
                or discovered_node is not None
            ):
                return ""
            can_pull = (
                discovered_node is not None and discovered_node.server.is_healthy
            )
            pull_url = None
            if discovered_node is not None:
                url_name = f"{obj._meta.app_label}:{obj._meta.model_name}_opennms_pull"
                pull_url = reverse(url_name, args=[obj.pk])
            return self.render(
                PANEL,
                extra_context={
                    "sync_status": status,
                    "discovered_node": discovered_node,
                    "can_pull": can_pull,
                    "pull_url": pull_url,
                },
            )
        except Exception:
            return ""


class DeviceSyncStatusPanel(_SyncStatusPanel):
    models = ["dcim.device"]


class VirtualMachineSyncStatusPanel(_SyncStatusPanel):
    models = ["virtualization.virtualmachine"]


template_extensions = [DeviceSyncStatusPanel, VirtualMachineSyncStatusPanel]
