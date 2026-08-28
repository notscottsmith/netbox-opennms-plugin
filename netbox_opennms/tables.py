# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Tables for plugin list views (Requisition redesign)."""

import django_tables2 as tables
from netbox.tables import NetBoxTable, columns

from .models import (
    AssetMapping,
    DiscoveredNode,
    DiscoveryScan,
    MetadataEntry,
    MonitoredInterface,
    MonitoredService,
    MonitoringDetector,
    MonitoringExclusion,
    MonitoringOverride,
    MonitoringPolicy,
    OpenNMSServer,
    Requisition,
)


class OpenNMSServerTable(NetBoxTable):
    name = tables.Column(linkify=True)
    is_default = columns.BooleanColumn()
    last_check_status = tables.TemplateColumn(
        template_code="""
            {% if record.last_check_status == "ok" %}
              <span class="badge text-bg-green">OK</span>
            {% elif record.last_check_status == "failed" %}
              <span class="badge text-bg-red" title="{{ record.last_check_message }}">Failed</span>
            {% else %}
              <span class="badge text-bg-secondary">Untested</span>
            {% endif %}
        """,
        verbose_name="Status",
    )
    # NetBox's list view wraps the whole table in one bulk-action <form>; a
    # nested per-row <form> here is invalid HTML that browsers silently hoist
    # out, breaking the click into a 405 (#32). formaction/formmethod submit
    # this row's action through the outer form instead, without nesting one.
    test_action = tables.TemplateColumn(
        template_code="""
            <button type="submit"
                    formaction="{% url 'plugins:netbox_opennms:opennmsserver_test'
                                        record.pk %}"
                    formmethod="post"
                    class="btn btn-sm btn-outline-primary">
              Test
            </button>
        """,
        verbose_name="",
        orderable=False,
    )
    scan_action = tables.TemplateColumn(
        template_code="""
            <button type="submit"
                    formaction="{% url 'plugins:netbox_opennms:opennmsserver_scan'
                                        record.pk %}"
                    formmethod="post"
                    class="btn btn-sm btn-outline-secondary">
              Scan
            </button>
        """,
        verbose_name="",
        orderable=False,
    )

    class Meta(NetBoxTable.Meta):
        model = OpenNMSServer
        fields = (
            "pk",
            "id",
            "name",
            "url",
            "default_location",
            "is_default",
            "last_check_status",
            "test_action",
            "scan_action",
            "created",
            "last_updated",
            "actions",
        )
        default_columns = (
            "name",
            "url",
            "default_location",
            "is_default",
            "last_check_status",
            "test_action",
            "scan_action",
        )


class MonitoringExclusionTable(NetBoxTable):
    description = tables.Column(linkify=True)

    class Meta(NetBoxTable.Meta):
        model = MonitoringExclusion
        fields = (
            "pk",
            "id",
            "description",
            "created",
            "last_updated",
            "actions",
        )
        default_columns = ("description",)


class DiscoveryScanTable(NetBoxTable):
    foreign_source = tables.Column(linkify=True, verbose_name="Discovery Scan")
    server = tables.Column(linkify=True)
    requisition = tables.Column(linkify=True)
    last_triggered = columns.DateTimeColumn()
    # See OpenNMSServerTable.test_action above (#32) — formaction/formmethod
    # avoids nesting a <form> inside the list view's outer bulk-action form.
    trigger_action = tables.TemplateColumn(
        template_code="""
            <button type="submit"
                    formaction="{% url 'plugins:netbox_opennms:discoveryscan_trigger'
                                        record.pk %}"
                    formmethod="post"
                    class="btn btn-sm btn-outline-secondary">
              Trigger
            </button>
        """,
        verbose_name="",
        orderable=False,
    )

    class Meta(NetBoxTable.Meta):
        model = DiscoveryScan
        fields = (
            "pk",
            "id",
            "foreign_source",
            "server",
            "requisition",
            "location",
            "ip_range_begin",
            "ip_range_end",
            "retries",
            "timeout",
            "last_triggered",
            "trigger_action",
            "created",
            "last_updated",
            "actions",
        )
        default_columns = (
            "foreign_source",
            "server",
            "requisition",
            "location",
            "ip_range_begin",
            "ip_range_end",
            "last_triggered",
            "trigger_action",
        )


class DiscoveredNodeTable(NetBoxTable):
    label = tables.Column(linkify=True)
    server = tables.Column(linkify=True)
    verdict = tables.TemplateColumn(
        template_code="""
            {% if record.verdict == "green" %}
              <span class="badge text-bg-green">Matches</span>
            {% elif record.verdict == "orange" %}
              <span class="badge text-bg-orange"
                    title="{{ record.diff_detail|join:'; ' }}">Differs</span>
            {% else %}
              <span class="badge text-bg-red">Missing from NetBox</span>
            {% endif %}
        """,
    )
    matched_object = tables.Column(linkify=True, verbose_name="Matched object")
    resolution = tables.Column(verbose_name="Resolution")
    completeness_gaps = tables.TemplateColumn(
        verbose_name="Completeness",
        template_code="""
            {% if not record.walked_at %}
              <span class="text-muted">Not walked</span>
            {% elif record.completeness_gaps %}
              <span class="badge text-bg-orange"
                    title="{{ record.completeness_gaps|join:'; ' }}">
                {{ record.completeness_gaps|length }}
                gap{{ record.completeness_gaps|length|pluralize }}
              </span>
            {% else %}
              <span class="badge text-bg-green">Complete</span>
            {% endif %}
        """,
    )

    class Meta(NetBoxTable.Meta):
        model = DiscoveredNode
        fields = (
            "pk",
            "id",
            "server",
            "label",
            "verdict",
            "resolution",
            "foreign_source",
            "foreign_id",
            "location",
            "matched_object",
            "completeness_gaps",
            "walked_at",
            "last_scanned",
            "created",
            "last_updated",
            "actions",
        )
        default_columns = (
            "server",
            "label",
            "verdict",
            "foreign_id",
            "matched_object",
            "completeness_gaps",
            "last_scanned",
        )


class RequisitionTable(NetBoxTable):
    name = tables.Column(linkify=True)

    class Meta(NetBoxTable.Meta):
        model = Requisition
        fields = (
            "pk",
            "id",
            "name",
            "description",
            "object_types",
            "scan_interval",
            "default_interfaces",
            "location",
            "created",
            "last_updated",
            "actions",
        )
        default_columns = (
            "name",
            "object_types",
            "scan_interval",
            "location",
        )


class MonitoringDetectorTable(NetBoxTable):
    name = tables.Column(linkify=True)
    requisition = tables.Column(linkify=True)

    class Meta(NetBoxTable.Meta):
        model = MonitoringDetector
        fields = (
            "pk",
            "id",
            "requisition",
            "name",
            "preset",
            "rule_class",
            "created",
            "last_updated",
            "actions",
        )
        default_columns = ("requisition", "name", "preset", "rule_class")


class MonitoringPolicyTable(NetBoxTable):
    name = tables.Column(linkify=True)
    requisition = tables.Column(linkify=True)

    class Meta(NetBoxTable.Meta):
        model = MonitoringPolicy
        fields = (
            "pk",
            "id",
            "requisition",
            "name",
            "preset",
            "rule_class",
            "created",
            "last_updated",
            "actions",
        )
        default_columns = ("requisition", "name", "preset", "rule_class")


class MonitoringOverrideTable(NetBoxTable):
    assigned_object = tables.Column(linkify=True, verbose_name="Object")
    assigned_object_type = columns.ContentTypeColumn(verbose_name="Type")
    management_ip = tables.Column(linkify=True, verbose_name="Management IP")
    exclude = columns.BooleanColumn()

    class Meta(NetBoxTable.Meta):
        model = MonitoringOverride
        fields = (
            "pk",
            "id",
            "assigned_object",
            "assigned_object_type",
            "exclude",
            "management_ip",
            "location",
            "created",
            "last_updated",
            "actions",
        )
        default_columns = (
            "assigned_object",
            "assigned_object_type",
            "exclude",
            "management_ip",
        )


class MonitoredServiceTable(NetBoxTable):
    override = tables.Column(linkify=True)
    ip_address = tables.Column(linkify=True, verbose_name="Interface IP")
    name = tables.Column(linkify=True)

    class Meta(NetBoxTable.Meta):
        model = MonitoredService
        fields = (
            "pk",
            "id",
            "override",
            "ip_address",
            "name",
            "created",
            "last_updated",
            "actions",
        )
        default_columns = ("override", "ip_address", "name")


class MonitoredInterfaceTable(NetBoxTable):
    override = tables.Column(linkify=True)
    ip_address = tables.Column(linkify=True, verbose_name="Interface IP")

    class Meta(NetBoxTable.Meta):
        model = MonitoredInterface
        fields = (
            "pk",
            "id",
            "override",
            "ip_address",
            "role",
            "created",
            "last_updated",
            "actions",
        )
        default_columns = ("override", "ip_address", "role")


class AssetMappingTable(NetBoxTable):
    requisition = tables.Column(linkify=True)
    asset_field = tables.Column(linkify=True)

    class Meta(NetBoxTable.Meta):
        model = AssetMapping
        fields = (
            "pk",
            "id",
            "requisition",
            "netbox_source",
            "asset_field",
            "created",
            "last_updated",
            "actions",
        )
        default_columns = ("requisition", "netbox_source", "asset_field")


class MetadataEntryTable(NetBoxTable):
    requisition = tables.Column(linkify=True)
    key = tables.Column(linkify=True)

    class Meta(NetBoxTable.Meta):
        model = MetadataEntry
        fields = (
            "pk",
            "id",
            "requisition",
            "scope",
            "context",
            "key",
            "value_source",
            "literal_value",
            "created",
            "last_updated",
            "actions",
        )
        default_columns = ("requisition", "scope", "context", "key")
