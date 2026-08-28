# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Tables for plugin list views (Requisition redesign)."""

import django_tables2 as tables
from django.urls import reverse
from django.utils.html import format_html
from netbox.tables import BaseTable, NetBoxTable, columns

from . import labels
from .models import (
    AssetMapping,
    DiscoveredNode,
    DiscoveryScan,
    MetadataContext,
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


class _ActionColumn(tables.TemplateColumn):
    """A ``TemplateColumn`` whose button/link URLs need ``record``/``table``.

    django-tables2 2.8.0 (the version NetBox 4.6.9 pins) requires
    ``extra_context`` to be a plain ``dict``: its own
    ``additional_context.update(self.extra_context)`` raises ``TypeError:
    'function' object is not iterable`` if handed a callable, since
    ``extra_context``-as-callable support wasn't added until a later
    django-tables2 release. This resolves ``context_fn`` ourselves, per
    row, and always hands the base class a plain dict.
    """

    def __init__(self, *, context_fn, **kwargs):
        super().__init__(**kwargs)
        self._context_fn = context_fn

    def render(self, record, table, **kwargs):
        self.extra_context = self._context_fn(record, table)
        return super().render(record=record, table=table, **kwargs)


class OpenNMSServerTable(NetBoxTable):
    name = tables.Column(linkify=True)
    is_default = columns.BooleanColumn()
    last_check_status = tables.TemplateColumn(
        template_code="""
            {% if record.last_check_status == "ok" %}
              <span class="badge text-bg-green">OK</span>
            {% elif record.last_check_status == "failed" %}
              <span class="badge text-bg-red"
                    title="{{ record.last_check_message }}">Failed</span>
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
    # The URL is resolved in extra_context, not a {% url %} tag: Django's tag
    # lexer (django/template/base.py's tag_re) has no re.DOTALL, so a tag can
    # never span multiple lines, and these names are too long to fit one.
    test_action = _ActionColumn(
        template_code="""
            <button type="submit"
                    formaction="{{ test_url }}"
                    formmethod="post"
                    class="btn btn-sm btn-outline-primary">
              Test
            </button>
        """,
        context_fn=lambda record, table: {
            "test_url": reverse(
                "plugins:netbox_opennms:opennmsserver_test", args=[record.pk]
            ),
        },
        verbose_name="",
        orderable=False,
    )
    scan_action = _ActionColumn(
        template_code="""
            <button type="submit"
                    formaction="{{ scan_url }}"
                    formmethod="post"
                    class="btn btn-sm btn-outline-secondary">
              Scan
            </button>
        """,
        context_fn=lambda record, table: {
            "scan_url": reverse(
                "plugins:netbox_opennms:opennmsserver_scan", args=[record.pk]
            ),
        },
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


class NodeDiffTable(BaseTable):
    """One row per node in a Requisition scan diff (issue #34).

    ``NodeDiff`` (``requisition_scan.py``) is a plain dataclass, not a Django
    model, so
    this subclasses ``BaseTable`` rather than ``NetBoxTable`` — confirmed
    against NetBox 4.6.8's source (this plugin's pinned NetBox version):
    ``NetBoxTable.__init__`` unconditionally calls
    ``ObjectType.objects.get_for_model(self._meta.model)`` to wire up custom
    fields/links, which requires a real registered model and would raise for
    a bare dataclass. ``BaseTable`` has none of that — it only adds the
    per-user "Configure Table" column persistence (``configure()``, keyed by
    ``self.__class__.__name__`` under ``request.user.config``), which is
    model-agnostic and is all this table needs. ``empty_text`` is set
    explicitly since ``BaseTable.__init__`` otherwise defaults it from
    ``Meta.model._meta.verbose_name_plural``, which doesn't exist here.
    Sorting is disabled throughout since the row set is a diff, not a
    queryset NetBox can order server-side.
    """

    label = tables.Column(verbose_name="Node", orderable=False)
    foreign_id = tables.Column(verbose_name="Foreign ID", orderable=False)
    management_ip = tables.Column(verbose_name="Management IP", orderable=False)
    location = tables.Column(orderable=False)
    netbox_object = tables.Column(verbose_name="Matched NetBox object", orderable=False)
    opennms_node = tables.Column(
        accessor="opennms_node_id", verbose_name="OpenNMS node", orderable=False
    )
    changes = tables.Column(orderable=False)
    # Plain per-row <form>s are safe here (unlike OpenNMSServerTable's
    # formaction/formmethod workaround, #32): this page's table is included
    # via NetBox's bare htmx/table.html, not rendered inside an
    # ObjectListView bulk-action <form> (confirmed against the pinned
    # NetBox 4.6.8 template source — no outer <form> wraps it). The override
    # option's confirm() (issue #36) is inline JS, not a modal: this table has
    # no other client-side dependency, and the dialog's whole job is to state,
    # once, that the action creates a new OpenNMS node before anything sends.
    sync_action = _ActionColumn(
        template_code="""
            {% if record.status == "added" or record.status == "changed" %}
              <div class="dropdown">
                <button type="button"
                        class="btn btn-sm btn-outline-primary dropdown-toggle"
                        data-bs-toggle="dropdown" aria-expanded="false">
                  Sync to OpenNMS
                </button>
                <ul class="dropdown-menu dropdown-menu-end">
                  <li>
                    <form method="post" action="{{ sync_url }}">
                      {% csrf_token %}
                      <button type="submit" class="dropdown-item">
                        Sync to OpenNMS
                      </button>
                    </form>
                  </li>
                  <li>
                    <form method="post"
                          action="{{ override_url }}"
                          onsubmit="return confirm(
                            'This pushes the node to OpenNMS under the plugin '
                            + 'default Foreign ID. If that differs from its '
                            + 'current Foreign ID, OpenNMS treats it as a brand '
                            + 'new node, and the existing one is left untouched. '
                            + 'Continue?'
                          );">
                      {% csrf_token %}
                      <button type="submit" class="dropdown-item">
                        Override Foreign ID &amp; Sync
                      </button>
                    </form>
                  </li>
                </ul>
              </div>
            {% endif %}
        """,
        context_fn=lambda record, table: {
            "sync_url": reverse(
                "plugins:netbox_opennms:requisition_sync_node",
                args=[table.requisition_pk, record.foreign_id],
            ),
            "override_url": reverse(
                "plugins:netbox_opennms:requisition_sync_node_override",
                args=[table.requisition_pk, record.foreign_id],
            ),
        },
        verbose_name="",
        orderable=False,
    )

    class Meta(BaseTable.Meta):
        fields = (
            "label",
            "foreign_id",
            "management_ip",
            "location",
            "netbox_object",
            "opennms_node",
            "changes",
            "sync_action",
        )
        default_columns = fields
        empty_text = "No nodes"
        row_attrs = {
            "class": lambda record: {
                "added": "table-success",
                "removed": "table-danger",
                "changed": "table-warning",
            }.get(record.status, ""),
        }

    def __init__(self, *args, requisition_pk=None, server_url="", **kwargs):
        # Passed in by the view rather than derived from the record (issue
        # #34): the walk-view link needs the Requisition's pk, and the
        # OpenNMS node link needs the target Server's base URL — neither is
        # data the dataclass row itself carries.
        self.requisition_pk = requisition_pk
        self.server_url = server_url
        super().__init__(*args, **kwargs)

    def render_label(self, record):
        if not record.opennms_node_id:
            return record.label
        url = reverse(
            "plugins:netbox_opennms:requisition_node_walk",
            args=[self.requisition_pk, record.opennms_node_id],
        )
        return format_html('<a href="{}">{}</a>', url, record.label)

    def render_netbox_object(self, record):
        if record.netbox_object is None:
            return "No match"
        return format_html(
            '<a href="{}">{}</a>',
            record.netbox_object.get_absolute_url(),
            record.netbox_object,
        )

    def render_opennms_node(self, record):
        if not record.opennms_node_id or not self.server_url:
            return "—"
        base = self.server_url.rstrip("/")
        url = f"{base}/element/node.jsp?node={record.opennms_node_id}"
        return format_html('<a href="{}">{}</a>', url, record.opennms_node_id)

    def render_changes(self, record):
        return "; ".join(record.changes) or "—"


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
    # ``status`` is a model @property (models.py), not a DB column, so it
    # can't be ordered server-side — matches the badge markup already used
    # on discoveryscan.html's detail page.
    status = tables.TemplateColumn(
        template_code="""
            {% if record.status == "settled" %}
              <span class="badge text-bg-green">Settled</span>
            {% elif record.status == "running" %}
              <span class="badge text-bg-cyan">Running</span>
            {% else %}
              <span class="badge text-bg-secondary">Pending</span>
            {% endif %}
        """,
        orderable=False,
    )
    # DiscoveryScanListView annotates its queryset with
    # ``node_count=Count("discovered_nodes")`` so this sorts server-side.
    # ``empty_values=()`` plus ``render_node_count`` below fall back to
    # ``discovered_nodes.count()`` for any other view (e.g. bulk-delete's
    # confirmation table) that renders this table off an unannotated
    # queryset.
    node_count = tables.Column(
        accessor="node_count", verbose_name="Discovered Nodes", empty_values=()
    )
    # See OpenNMSServerTable.test_action above (#32) — formaction/formmethod
    # avoids nesting a <form> inside the list view's outer bulk-action form.
    # Disabled once the scan has left "pending" (#50's guard rejects the POST
    # anyway; this just keeps the button honest) — mirrors RequisitionTable
    # .sync_action's "frozen" tooltip pattern above.
    trigger_action = _ActionColumn(
        template_code="""
            <button type="submit"
                    formaction="{{ trigger_url }}"
                    formmethod="post"
                    class="btn btn-sm btn-outline-secondary"
                    {% if frozen %}
                      disabled title="Already triggered — create a new scan"
                    {% endif %}>
              Trigger
            </button>
        """,
        context_fn=lambda record, table: {
            "trigger_url": reverse(
                "plugins:netbox_opennms:discoveryscan_trigger", args=[record.pk]
            ),
            "frozen": record.status != "pending",
        },
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
            "status",
            "node_count",
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
            "status",
            "node_count",
            "ip_range_begin",
            "ip_range_end",
            "last_triggered",
            "trigger_action",
        )

    def render_node_count(self, record, value):
        return value if value is not None else record.discovered_nodes.count()


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
            "resolution",
            "foreign_id",
            "matched_object",
            "completeness_gaps",
            "last_scanned",
        )


class RequisitionTable(NetBoxTable):
    """The Requisition list, absorbing the former standalone Sync Preview page
    (issue #46): conflicts/nodes/warnings come from a ``membership.Resolution``
    the view attaches to each row as ``record._resolution`` (``resolve_all()``
    has no per-object queryset filtering of its own, so it can't be expressed
    as a table ``Column`` accessor/annotation — the view resolves once for
    every Requisition and hands each row its match).
    """

    name = tables.Column(linkify=True)
    location = tables.Column(verbose_name=labels.MONITORING_LOCATION)
    conflicts = tables.Column(
        empty_values=(), orderable=False, verbose_name="Conflicts"
    )
    node_count = tables.Column(empty_values=(), orderable=False, verbose_name="Nodes")
    warnings = tables.Column(empty_values=(), orderable=False, verbose_name="Warnings")
    # See OpenNMSServerTable.test_action above (#32) — formaction/formmethod
    # avoids nesting a <form> inside the list view's outer bulk-action form.
    sync_action = _ActionColumn(
        template_code="""
            <a href="{{ scan_url }}" class="btn btn-sm btn-outline-primary">
              Scan
            </a>
            <button type="submit"
                    formaction="{{ sync_url }}"
                    formmethod="post"
                    class="btn btn-sm btn-primary"
                    {% if frozen %}
                      disabled title="Frozen — resolve the filter conflicts first"
                    {% endif %}>
              Sync
            </button>
        """,
        context_fn=lambda record, table: {
            "scan_url": reverse(
                "plugins:netbox_opennms:requisition_scan", args=[record.pk]
            ),
            "sync_url": reverse(
                "plugins:netbox_opennms:requisition_sync", args=[record.pk]
            ),
            "frozen": bool(
                getattr(record, "_resolution", None) and record._resolution.conflicts
            ),
        },
        verbose_name="",
        orderable=False,
    )

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
            "conflicts",
            "node_count",
            "warnings",
            "created",
            "last_updated",
            "sync_action",
            "actions",
        )
        default_columns = (
            "name",
            "object_types",
            "scan_interval",
            "location",
            "conflicts",
            "node_count",
            "warnings",
            "sync_action",
        )

    def render_conflicts(self, record):
        resolution = getattr(record, "_resolution", None)
        if resolution and resolution.conflicts:
            return format_html(
                '<span class="badge text-bg-danger" '
                'title="Sync blocked — resolve the filter overlap">'
                "{} — frozen</span>",
                len(resolution.conflicts),
            )
        return "—"

    def render_node_count(self, record):
        resolution = getattr(record, "_resolution", None)
        return len(resolution.nodes) if resolution else 0

    def render_warnings(self, record):
        resolution = getattr(record, "_resolution", None)
        if not resolution:
            return "—"
        count = len(resolution.rejected) + len(resolution.warnings)
        if not count:
            return "—"
        return format_html('<span class="badge text-bg-warning">{}</span>', count)


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


class MetadataContextTable(NetBoxTable):
    name = tables.Column(linkify=True)
    is_builtin = columns.BooleanColumn(verbose_name="Built-in")

    class Meta(NetBoxTable.Meta):
        model = MetadataContext
        fields = (
            "pk",
            "id",
            "name",
            "is_builtin",
            "description",
            "created",
            "last_updated",
            "actions",
        )
        default_columns = ("name", "is_builtin", "description")


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
