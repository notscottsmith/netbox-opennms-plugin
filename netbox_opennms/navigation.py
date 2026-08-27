# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Navigation menu items."""

from netbox.plugins import PluginMenuButton, PluginMenuItem

menu_items = (
    PluginMenuItem(
        link="plugins:netbox_opennms:opennmsserver_list",
        link_text="Servers",
        buttons=(
            PluginMenuButton(
                link="plugins:netbox_opennms:opennmsserver_add",
                title="Add",
                icon_class="mdi mdi-plus-thick",
            ),
        ),
    ),
    PluginMenuItem(
        link="plugins:netbox_opennms:requisition_list",
        link_text="Requisitions",
        buttons=(
            PluginMenuButton(
                link="plugins:netbox_opennms:requisition_add",
                title="Add",
                icon_class="mdi mdi-plus-thick",
            ),
        ),
    ),
    PluginMenuItem(
        link="plugins:netbox_opennms:monitoringoverride_list",
        link_text="Monitoring Overrides",
        buttons=(
            PluginMenuButton(
                link="plugins:netbox_opennms:monitoringoverride_add",
                title="Add",
                icon_class="mdi mdi-plus-thick",
            ),
        ),
    ),
    PluginMenuItem(
        link="plugins:netbox_opennms:monitoringexclusion_list",
        link_text="Monitoring Exclusions",
        buttons=(
            PluginMenuButton(
                link="plugins:netbox_opennms:monitoringexclusion_add",
                title="Add",
                icon_class="mdi mdi-plus-thick",
            ),
        ),
    ),
    PluginMenuItem(
        link="plugins:netbox_opennms:vrfassignment_list",
        link_text="VRF Assignments",
        buttons=(
            PluginMenuButton(
                link="plugins:netbox_opennms:vrfassignment_add",
                title="Add",
                icon_class="mdi mdi-plus-thick",
            ),
        ),
    ),
    PluginMenuItem(
        link="plugins:netbox_opennms:discoveryscan_list",
        link_text="Discovery Scans",
        buttons=(
            PluginMenuButton(
                link="plugins:netbox_opennms:discoveryscan_add",
                title="Add",
                icon_class="mdi mdi-plus-thick",
            ),
        ),
    ),
    PluginMenuItem(
        link="plugins:netbox_opennms:sync_preview",
        link_text="Sync Preview",
    ),
    PluginMenuItem(
        link="plugins:netbox_opennms:discoverednode_list",
        link_text="Discovered Nodes",
    ),
)
