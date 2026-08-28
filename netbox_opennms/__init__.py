# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""NetBox plugin that provisions nodes into OpenNMS via the REST provisioning API."""

from netbox.plugins import PluginConfig

# Single source of truth for the version: pyproject reads this via
# [tool.setuptools.dynamic] (a top-level literal, AST-read at build time without
# importing NetBox). PluginConfig.version also derives from it.
__version__ = "0.1.9"


class NetBoxOpenNMSConfig(PluginConfig):
    """Plugin configuration for netbox-opennms-plugin.

    Declares NetBox compatibility and the connection configuration surface
    (``PLUGINS_CONFIG``). Per-server connection settings (URL, credentials,
    default location) now live on ``OpenNMSServer`` rows, encrypted at rest
    (ADR 0005, superseding the prior AD-13); ``opennms_secret_key`` is the
    Fernet key that protects them and is required to start.
    """

    name = "netbox_opennms"
    verbose_name = "OpenNMS"
    description = (
        "Provision NetBox devices and virtual machines into OpenNMS "
        "via the REST provisioning API."
    )
    version = __version__
    author = "Ronny Trommer"
    author_email = "ronny@no42.org"
    base_url = "opennms"

    # NetBox 4.6 introduced Python 3.12+ and Django 6.0; pin to the 4.6.x line.
    # 4.6.1 minimum: the no-worker warning (Story 1.8) uses
    # utilities.rqworker.any_workers_for_queue, added in 4.6.1.
    min_version = "4.6.1"
    # max_version intentionally unset — pinned against a tested 4.6.x patch at
    # release (Story 4.4). Do not pin Django independently; NetBox bundles it.

    # opennms_secret_key must be set in NetBox's PLUGINS_CONFIG: the Fernet key
    # protecting OpenNMSServer credentials/headers at rest (ADR 0005). NetBox
    # refuses to start without it.
    required_settings = ["opennms_secret_key"]

    # Plugin-wide settings that remain global (per-server settings — URL,
    # credentials, default location — moved to OpenNMSServer, ADR 0002).
    default_settings = {
        # rescanExisting value used by the import step (Story 1.7).
        "import_mode": "false",
        # Periodic drift reconciler: clear OpenNMS netbox.* Foreign Sources that
        # NetBox no longer governs (last member left / moved / unassigned). "true"
        # / "false". Touches only the plugin's own namespace.
        "reconcile_orphans": "true",
        # Prefix applied to every Foreign ID this plugin derives (AD-8). Change
        # with care: it is part of node identity, not just a label (issue #3).
        "foreign_id_prefix": "netbox",
        # How long (minutes) a Discovery Scan must see no new OpenNMS node
        # before PollDiscoveryScansJob infers it has settled (issue #27,
        # ADR 0006 — OpenNMS gives no job-status endpoint to ask directly).
        "discovery_settle_idle_minutes": "5",
        # How long (minutes) after a Discovery Scan settles before
        # CleanupDiscoveryScansJob deletes its OpenNMS-side throwaway
        # requisition (issue #29, ADR 0006). Default 1440 (24h) — long enough
        # for an operator to review the scan's DiscoveredNode rows against
        # OpenNMS's own data before it's removed there.
        "discovery_retention_minutes": "1440",
        # Which Scope levels (see scope.SCOPE_FIELDS, singularized) feed a
        # Scope-picked Requisition's auto-derived name, in this order, when
        # its Name field is left blank (issue #20).
        "requisition_naming_template": ["tenant", "site", "location"],
        # Separator joining requisition_naming_template's levels into the
        # derived name (issue #20). "-" or "_" -- both OpenNMS-safe (H7).
        "requisition_naming_separator": "-",
    }

    def ready(self):
        super().ready()
        from . import signals  # noqa: F401  (registers post_delete handlers)


config = NetBoxOpenNMSConfig
