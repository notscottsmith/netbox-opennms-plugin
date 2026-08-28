# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Central UI label constants (issue #43).

NetBox has no plugin-level glossary/label-registry mechanism (checked against
``netbox.plugins.PluginConfig`` and the ``PluginMenu``/``PluginMenuItem`` API,
NetBox 4.6) — this module is the plugin-local substitute, so a UI string used
in more than one place (navigation, table columns, page titles) is defined
once and referenced everywhere, instead of drifting across separately-typed
literals.
"""

# --- Main-menu navigation (navigation.py) -----------------------------------

NAV_SERVERS = "Servers"
NAV_REQUISITIONS = "Requisitions"
NAV_DISCOVERY = "Discovery"
NAV_DISCOVERED_NODES = "Discovered Nodes"
# Removed once Sync Preview is absorbed into Requisitions (issue #46).
NAV_SYNC_PREVIEW = "Sync Preview"
NAV_OVERRIDES = "Overrides"
NAV_EXCLUSIONS = "Exclusions"

# --- Requisition table columns (tables.py) ----------------------------------

MONITORING_LOCATION = "Monitoring Location"
