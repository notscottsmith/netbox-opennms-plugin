# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Navigation menu items."""

from netbox.plugins import PluginMenu, PluginMenuButton, PluginMenuItem

menu = PluginMenu(
    label="OpenNMS",
    icon_class="mdi mdi-router",  # Material Design Icon class
    groups=(
        (
            "OpenNMS",
            (
                PluginMenuItem(
                    link_text="Servers",
                    link="plugins:netbox_opennms:opennmsserver_list",
                    buttons=(
                        PluginMenuButton(
                            link_text="Servers",
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
                    link="plugins:netbox_opennms:discoveryscan_list",
                    link_text="Discovery",
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
            ),
        ),
        (
            "Monitoring",
            (
                PluginMenuItem(
                    link="plugins:netbox_opennms:monitoringoverride_list",
                    link_text="Overrides",
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
                    link_text="Exclusions",
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
