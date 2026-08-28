# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Navigation menu items."""

from netbox.plugins import PluginMenu, PluginMenuButton, PluginMenuItem

from . import labels

menu = PluginMenu(
    label="OpenNMS",
    icon_class="mdi mdi-router",  # Material Design Icon class
    groups=(
        (
            "OpenNMS",
            (
                PluginMenuItem(
                    link_text=labels.NAV_SERVERS,
                    link="plugins:netbox_opennms:opennmsserver_list",
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
                    link_text=labels.NAV_REQUISITIONS,
                    buttons=(
                        PluginMenuButton(
                            link="plugins:netbox_opennms:requisition_add",
                            title="Add",
                            icon_class="mdi mdi-plus-thick",
                        ),
                    ),
                ),
                PluginMenuItem(
                    link="plugins:netbox_opennms:discoveryscan_list",
                    link_text=labels.NAV_DISCOVERY,
                    buttons=(
                        PluginMenuButton(
                            link="plugins:netbox_opennms:discoveryscan_add",
                            title="Add",
                            icon_class="mdi mdi-plus-thick",
                        ),
                    ),
                ),
                # No native nested-submenu support under "Discovery" — NetBox's
                # PluginMenu/PluginMenuItem API (4.6) only groups items into
                # flat (group-label -> items) tuples, so this is a sibling
                # item within the same "OpenNMS" group rather than a child of
                # Discovery (issue #43).
                PluginMenuItem(
                    link="plugins:netbox_opennms:discoverednode_list",
                    link_text=labels.NAV_DISCOVERED_NODES,
                ),
            ),
        ),
        (
            "Monitoring",
            (
                PluginMenuItem(
                    link="plugins:netbox_opennms:monitoringoverride_list",
                    link_text=labels.NAV_OVERRIDES,
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
                    link_text=labels.NAV_EXCLUSIONS,
                    buttons=(
                        PluginMenuButton(
                            link="plugins:netbox_opennms:monitoringexclusion_add",
                            title="Add",
                            icon_class="mdi mdi-plus-thick",
                        ),
                    ),
                ),
            ),
        ),
    ),
)
