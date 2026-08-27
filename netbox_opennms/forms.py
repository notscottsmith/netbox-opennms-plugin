# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Forms for plugin models (Requisition redesign)."""

import logging

from dcim.models import (
    Device,
    DeviceRole,
    DeviceType,
    Location,
    Manufacturer,
    Platform,
    Site,
    SiteGroup,
)
from django import forms
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db.models import Q
from django.utils.translation import gettext_lazy as _
from extras.models import SavedFilter
from ipam.models import IPAddress
from netbox.forms import NetBoxModelFilterSetForm, NetBoxModelForm
from netbox.plugins import get_plugin_config
from tenancy.models import Tenant, TenantGroup
from utilities.forms.fields import (
    DynamicModelChoiceField,
    DynamicModelMultipleChoiceField,
    JSONField,
)
from virtualization.models import VirtualMachine

from .catalog import get_detector_catalog, get_policy_catalog
from .choices import ObjectTypeChoices, ServiceChoices
from .derivation import default_requisition_name, location_name_error
from .membership import filter_errors, target_server_for
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
from .scope import SCOPE_FIELDS, find_scope_collision, scope_options

logger = logging.getLogger("netbox_opennms")


class RequisitionForm(NetBoxModelForm):
    """Create/edit a Requisition (one user-named OpenNMS Foreign Source)."""

    # Required=False at the form level only (issue #20): the model field
    # itself stays required (Requisition.clean() enforces it), but a blank
    # submission must be allowed to reach clean() below so a Scope-picked
    # Requisition can have its name auto-derived instead of bouncing off
    # Django's own "This field is required." before that logic ever runs.
    name = forms.CharField(
        max_length=100,
        required=False,
        label=_("Name"),
        help_text=_(
            "The Foreign Source name. Leave blank when using the Scope "
            "picker below to auto-derive one from the picked tenant/site/"
            "location (per the configured naming template) — a raw/"
            "freeform filter still requires an explicit name."
        ),
    )
    import_from_saved_filter = forms.ModelChoiceField(
        queryset=SavedFilter.objects.filter(
            Q(object_types__app_label="dcim", object_types__model="device")
            | Q(
                object_types__app_label="virtualization",
                object_types__model="virtualmachine",
            )
        ).distinct(),
        required=False,
        label=_("Import from Saved Filter"),
        help_text=_(
            "Copy a NetBox Device/VM Saved Filter's parameters into the filter "
            "below — a one-time copy, with no live link to the Saved Filter."
        ),
    )
    filter_params = JSONField(
        required=False,
        label=_("Advanced filter"),
        help_text=_(
            "NetBox filter parameters, e.g. "
            '{"role": ["switch"], "tag": ["critical"]}. Applied to the selected '
            "object types to compute members. Not needed if the Scope picker "
            "below (tenant/site/location/etc.) already covers this Requisition — "
            "only required for finer-grained filtering (role, tag, ...)."
        ),
    )
    # A <select>, not a ChoiceField: the option list is populated server-side in
    # __init__ from the resolved target Server's cached available_locations
    # (OpenNMSServer.available_locations, itself populated by "Test connection")
    # — the same non-live, __init__-time resolution the Scope picker below
    # already uses (target_server_for), not a live AJAX flow. CharField doesn't
    # validate against the widget's choices (see OpenNMSServerForm.default_location
    # for the identical rationale) — the widget is UX only, so a stale/missing
    # cache never blocks a save.
    location = forms.CharField(
        required=False,
        label=_("Location"),
        help_text=_(
            "The OpenNMS Monitoring Location this Requisition's nodes report "
            "to. Sourced from the resolved target Server's known locations — "
            "test that Server's connection to (re)populate this list."
        ),
        widget=forms.Select(choices=()),
    )
    services = forms.MultipleChoiceField(
        choices=ServiceChoices,
        required=False,
        label=_("Declared services"),
        help_text=_("Applied to every member's interfaces (overridable per object)."),
    )

    # Scope picker (issue #19): five optional convenience fields that write/update
    # the matching NetBox FilterSet key(s) in filter_params on save, so the common
    # case (every Device/VM under one Tenant Group/Tenant/Site Group/Site/Location)
    # doesn't require hand-writing a filter. Named "scope_*", not e.g. "location",
    # to avoid colliding with the model's own ``location`` field (the OpenNMS
    # monitoring location string, already on this form/Meta.fields below) — this is
    # pure UI sugar over filter_params, not a second membership mechanism (ADR
    # 0001): nothing is stored on the Requisition itself, a pick is merged into
    # filter_params only on save, and the raw JSON stays hand-editable afterward.
    scope_tenant_group = DynamicModelChoiceField(
        queryset=TenantGroup.objects.all(), required=False, label=_("Tenant group")
    )
    scope_tenant = DynamicModelChoiceField(
        queryset=Tenant.objects.all(),
        required=False,
        label=_("Tenant"),
        query_params={"group_id": "$scope_tenant_group"},
    )
    scope_site_group = DynamicModelChoiceField(
        queryset=SiteGroup.objects.all(), required=False, label=_("Site group")
    )
    scope_site = DynamicModelChoiceField(
        queryset=Site.objects.all(),
        required=False,
        label=_("Site"),
        query_params={"group_id": "$scope_site_group"},
    )
    scope_location = DynamicModelChoiceField(
        queryset=Location.objects.all(),
        required=False,
        label=_("Location"),
        help_text=_(
            "Devices only — Virtual Machines have no NetBox Location, so this is "
            "rejected on a Virtual-Machine-only Requisition."
        ),
        query_params={"site_id": "$scope_site"},
    )

    # (picker field name, scope.SCOPE_FIELDS key, filter_params key) for every
    # picker level — drives __init__'s option-narrowing and clean()'s merge.
    _SCOPE_PICKER_FIELDS = (
        ("scope_tenant_group", "tenant_groups", "tenant_group"),
        ("scope_tenant", "tenants", "tenant"),
        ("scope_site_group", "site_groups", "site_group"),
        ("scope_site", "sites", "site"),
        ("scope_location", "locations", "location"),
    )

    class Meta:
        model = Requisition
        fields = (
            "name",
            "description",
            "object_types",
            "import_from_saved_filter",
            "filter_params",
            "scan_interval",
            "default_interfaces",
            "services",
            "location",
            "tags",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Constrain the picker to Scope objects that scope.resolve_scope actually
        # resolves to THIS Requisition's own target Server (issue #19) — the same
        # (current members, else DeployedForeignSource) resolution
        # resolve_target_server already does elsewhere. A brand-new, never-scoped
        # Requisition has no target Server yet, so options is None and every field
        # keeps its full queryset (unconstrained) until the first level is picked
        # and saved.
        server = target_server_for(self.instance)
        current_location = self.instance.location if self.instance.pk else ""
        location_choices = list(server.available_locations) if server else []
        if current_location and current_location not in location_choices:
            location_choices = [current_location, *location_choices]
        self.fields["location"].widget.choices = [("", "---------")] + [
            (loc, loc) for loc in location_choices
        ]
        options = scope_options(server)
        if options is None:
            return
        for form_field, scope_field, _filter_key in self._SCOPE_PICKER_FIELDS:
            field = self.fields[form_field]
            queryset = options[scope_field]
            field.queryset = queryset
            # Also constrain the live APISelect widget itself (not just server-side
            # validation) to the same static ID list, so the dropdown only offers
            # what would actually validate.
            field.query_params = {
                **field.query_params,
                "id": list(queryset.values_list("pk", flat=True)),
            }

    def clean(self):
        super().clean()
        # A one-shot copy: importing a Saved Filter seeds the filter params (no live
        # link, R2). Done before the guard so the copied params are checked. Refuse
        # to silently discard a filter the user also typed in the same submit.
        saved = self.cleaned_data.get("import_from_saved_filter")
        if saved is not None:
            if self.cleaned_data.get("filter_params"):
                self.add_error(
                    "import_from_saved_filter",
                    _(
                        "Clear the Filter field to import a Saved Filter, or drop "
                        "the import and edit the filter directly — not both."
                    ),
                )
            else:
                self.cleaned_data["filter_params"] = dict(saved.parameters or {})
        # Scope picker: a picked level writes/updates its matching filter key (by
        # slug, the same key format NetBox's own Device/VM filter UI uses) merged
        # into whatever's already in filter_params — an untouched key (including one
        # a previous pick wrote and the user then hand-edited) is left alone.
        params = dict(self.cleaned_data.get("filter_params") or {})
        picked = {}
        for form_field, _scope_field, filter_key in self._SCOPE_PICKER_FIELDS:
            value = self.cleaned_data.get(form_field)
            picked[filter_key] = value
            if value is not None:
                params[filter_key] = [value.slug]
        self.cleaned_data["filter_params"] = params
        # Auto-derive a blank name from the Scope picker (issue #20), scoped
        # to whichever levels requisition_naming_template lists -- a raw/
        # freeform filter (no Scope-picker fields set at all) is left blank
        # here, falling through to Requisition.clean()'s "name required"
        # check exactly as before this feature existed.
        if not self.cleaned_data.get("name") and any(picked.values()):
            template = get_plugin_config(
                "netbox_opennms", "requisition_naming_template"
            )
            separator = get_plugin_config(
                "netbox_opennms", "requisition_naming_separator"
            )
            scope_values = {level: picked.get(level) for level in template}
            derived = default_requisition_name(scope_values, separator)
            if derived:
                self.cleaned_data["name"] = derived
        # Reject unknown/empty filters here (the same guard the resolver uses), so a
        # typo can't be saved into a fleet-wide catch-all (H1). Read from
        # cleaned_data — self.instance isn't populated until _post_clean(), after this.
        if not self.errors:
            probe = Requisition(
                object_types=self.cleaned_data.get("object_types")
                or ObjectTypeChoices.BOTH,
                filter_params=self.cleaned_data.get("filter_params") or {},
            )
            for error in filter_errors(probe):
                self.add_error("filter_params", error)
        return self.cleaned_data


class _PresetRuleForm(NetBoxModelForm):
    """Shared: the preset owns the rule class, so it isn't user-editable.

    The class is filled from the preset by the model; the form makes ``rule_class``
    optional (a preset provides it) and locks the field once a preset is set —
    freeform entry is only for a rule with no preset.

    When the rule's class is known, the parameter editor is driven by the **live
    OpenNMS catalog** (``catalog.py``, RD-1): one field per catalog parameter —
    enum parameters (with discovered ``options``) render as a dropdown, others as
    text, seeded with the overlay's label/default. The raw ``parameters`` JSON is
    hidden and reassembled from those fields on save. If OpenNMS is unreachable the
    editor degrades to the curated overlay and notes it — the save is never blocked.
    """

    requisition = DynamicModelChoiceField(
        queryset=Requisition.objects.all(), label=_("Requisition")
    )

    def _get_catalog(self):
        """The detector/policy catalog, or ``None``. Overridden by subclasses."""
        return None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        field = self.fields["rule_class"]
        field.required = False
        field.help_text = _(
            "Set automatically from the preset and locked; enter a class only for "
            "a freeform rule (no preset selected)."
        )
        # An existing preset-backed rule: the class is fixed to the preset's.
        if self.instance and self.instance.pk and self.instance.preset:
            field.disabled = True
        self._param_fields = []
        self._add_catalog_param_fields()

    def _catalog_entry(self):
        """The catalog entry for this rule's class/preset, and the live-avail flag."""
        rule_class = getattr(self.instance, "rule_class", "") or ""
        preset = getattr(self.instance, "preset", "") or ""
        # Nothing to look up (a blank add form or a freeform rule) — don't fetch the
        # catalog just to compute an entry that is structurally always None.
        if not rule_class and not preset:
            return None, False
        try:
            catalog = self._get_catalog()
        except Exception:  # noqa: BLE001 — the editor must never fail on discovery
            # _get_catalog degrades network errors internally; anything here is a
            # real bug — log it and flag degraded so the UI note fires (never silent).
            logger.exception("detector/policy catalog lookup failed")
            return None, True
        if catalog is None:
            return None, False
        entry = catalog.by_class(rule_class) if rule_class else None
        if entry is None and preset:
            entry = catalog.by_preset(preset)
        return entry, catalog.live_unavailable

    def _add_catalog_param_fields(self):
        entry, live_unavailable = self._catalog_entry()
        if live_unavailable:
            self.fields["rule_class"].help_text += _(
                " Live OpenNMS catalog unavailable — showing curated presets."
            )
        if entry is None or not entry.parameters:
            return
        # Drive parameters from the schema; hide the raw JSON and rebuild it in clean().
        self.fields.pop("parameters", None)
        current = (getattr(self.instance, "parameters", None)) or {}
        for param in entry.parameters:
            name = f"param_{param.key}"
            initial = current.get(param.key, param.default)
            hint = _("required by OpenNMS") if param.required else ""
            if param.options:
                self.fields[name] = forms.ChoiceField(
                    label=param.label or param.key,
                    required=False,
                    choices=[("", "---------")] + [(o, o) for o in param.options],
                    initial=initial if initial in param.options else "",
                    help_text=hint,
                )
            else:
                self.fields[name] = forms.CharField(
                    label=param.label or param.key,
                    required=False,
                    initial=initial,
                    help_text=hint,
                )
            self._param_fields.append((name, param.key))

    def clean(self):
        super().clean()
        # Rebuild parameters from the per-parameter fields, but PRESERVE any stored
        # key the catalog didn't surface as a field (freeform/API-set keys, or keys
        # dropped when the catalog is degraded to the overlay) — only touch the keys
        # we actually rendered. A blank field clears its own key; the model's
        # required-param guard still fires for a genuinely missing required value.
        if self._param_fields:
            params = dict(self.instance.parameters or {})
            for name, key in self._param_fields:
                value = self.cleaned_data.get(name)
                if value in (None, ""):
                    params.pop(str(key), None)
                else:
                    params[str(key)] = str(value)
            self.instance.parameters = params
        return self.cleaned_data


class MonitoringDetectorForm(_PresetRuleForm):
    """Add/edit a detector on a Requisition (a preset, or a freeform class)."""

    def _get_catalog(self):
        return get_detector_catalog()

    class Meta:
        model = MonitoringDetector
        fields = ("requisition", "name", "preset", "rule_class", "parameters", "tags")


class MonitoringPolicyForm(_PresetRuleForm):
    """Add/edit a policy on a Requisition (a preset, or a freeform class)."""

    def _get_catalog(self):
        return get_policy_catalog()

    class Meta:
        model = MonitoringPolicy
        fields = ("requisition", "name", "preset", "rule_class", "parameters", "tags")


class MonitoringOverrideForm(NetBoxModelForm):
    """Per-object exception. The target is one of Device / Virtual Machine."""

    device = DynamicModelChoiceField(
        queryset=Device.objects.all(), required=False, label=_("Device")
    )
    virtual_machine = DynamicModelChoiceField(
        queryset=VirtualMachine.objects.all(),
        required=False,
        label=_("Virtual Machine"),
    )
    management_ip = DynamicModelChoiceField(
        queryset=IPAddress.objects.all(),
        required=False,
        label=_("Management IP"),
        query_params={
            "device_id": "$device",
            "virtual_machine_id": "$virtual_machine",
        },
        help_text=_("Overrides the object's primary IP if set."),
    )
    suppressed_services = forms.MultipleChoiceField(
        choices=ServiceChoices,
        required=False,
        label=_("Suppress declared services"),
        help_text=_("Declared services to remove for this object only."),
    )

    class Meta:
        model = MonitoringOverride
        fields = (
            "device",
            "virtual_machine",
            "exclude",
            "management_ip",
            "management_role",
            "suppressed_services",
            "location",
            "tags",
        )

    def __init__(self, *args, **kwargs):
        instance = kwargs.get("instance")
        initial = kwargs.get("initial", {}).copy()
        if instance is not None and instance.assigned_object_id:
            obj = instance.assigned_object
            if isinstance(obj, Device):
                initial.setdefault("device", obj)
            elif isinstance(obj, VirtualMachine):
                initial.setdefault("virtual_machine", obj)
        kwargs["initial"] = initial
        super().__init__(*args, **kwargs)

    def clean(self):
        super().clean()
        device = self.cleaned_data.get("device")
        virtual_machine = self.cleaned_data.get("virtual_machine")
        if bool(device) == bool(virtual_machine):
            raise ValidationError(_("Select exactly one of Device or Virtual Machine."))
        target = device or virtual_machine
        self.instance.assigned_object = target

        # The unique constraint references assigned_object_type/_id (not form
        # fields), so surface a clean duplicate error instead of an IntegrityError.
        content_type = ContentType.objects.get_for_model(target)
        duplicate = (
            MonitoringOverride.objects.filter(
                assigned_object_type=content_type,
                assigned_object_id=target.pk,
            )
            .exclude(pk=self.instance.pk)
            .exists()
        )
        if duplicate:
            raise ValidationError(_("This object already has a Monitoring Override."))
        return self.cleaned_data


class MonitoredServiceForm(NetBoxModelForm):
    """Add/edit an explicit added service on one of an override's interfaces."""

    override = DynamicModelChoiceField(
        queryset=MonitoringOverride.objects.all(), label=_("Monitoring Override")
    )
    ip_address = DynamicModelChoiceField(
        queryset=IPAddress.objects.all(),
        label=_("Interface IP"),
        help_text=_("Must be the override's management IP or an additional IP."),
    )

    class Meta:
        model = MonitoredService
        fields = ("override", "ip_address", "name", "tags")


class MonitoredInterfaceForm(NetBoxModelForm):
    """Add/edit an additional interface (with its SNMP role) on an override (RD-5)."""

    override = DynamicModelChoiceField(
        queryset=MonitoringOverride.objects.all(), label=_("Monitoring Override")
    )
    ip_address = DynamicModelChoiceField(
        queryset=IPAddress.objects.all(),
        label=_("Interface IP"),
        help_text=_("An IP of the override's object (not its management IP)."),
    )

    class Meta:
        model = MonitoredInterface
        fields = ("override", "ip_address", "role", "tags")


class AssetMappingForm(NetBoxModelForm):
    """Map a NetBox attribute to an OpenNMS asset field on a Requisition (RD-2)."""

    requisition = DynamicModelChoiceField(
        queryset=Requisition.objects.all(), label=_("Requisition")
    )

    class Meta:
        model = AssetMapping
        fields = ("requisition", "netbox_source", "asset_field", "tags")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from .catalog import get_asset_fields

        fields = sorted(get_asset_fields())
        current = getattr(self.instance, "asset_field", "") or ""
        if current and current not in fields:
            fields.append(current)
        self.fields["asset_field"] = forms.ChoiceField(
            choices=[(f, f) for f in fields],
            label=_("OpenNMS asset field"),
            help_text=_("Discovered from OpenNMS (falls back to the known field set)."),
        )


class _ScopeForm(NetBoxModelForm):
    """Shared: the five-level Scope M2M fields (ADR 0002/0003)."""

    tenant_groups = DynamicModelMultipleChoiceField(
        queryset=TenantGroup.objects.all(), required=False, label=_("Tenant groups")
    )
    tenants = DynamicModelMultipleChoiceField(
        queryset=Tenant.objects.all(), required=False, label=_("Tenants")
    )
    site_groups = DynamicModelMultipleChoiceField(
        queryset=SiteGroup.objects.all(), required=False, label=_("Site groups")
    )
    sites = DynamicModelMultipleChoiceField(
        queryset=Site.objects.all(), required=False, label=_("Sites")
    )
    locations = DynamicModelMultipleChoiceField(
        queryset=Location.objects.all(), required=False, label=_("Locations")
    )


class OpenNMSServerForm(_ScopeForm):
    """Add/edit an OpenNMS Server: connection, credentials, and Scope (ADR 0002)."""

    headers = JSONField(
        required=False,
        label=_("Headers"),
        help_text=_(
            'Merged into every outbound request, e.g. {"CF-Access-Client-Id": '
            '"...", "CF-Access-Client-Secret": "..."} for Cloudflare Access.'
        ),
    )
    # A <select>, not a ChoiceField: seeded server-side from the persisted
    # available_locations cache (below), then further refreshed client-side
    # from a fresh OpenNMSClient.list_locations() after a successful "Test
    # connection" (server_test_connection.js) — so a submitted value
    # legitimately won't always be among the choices rendered server-side.
    # CharField doesn't validate against the widget's choices, only
    # OpenNMSServer.clean() does (via validate_location_name) — the widget is
    # UX only.
    #
    # NOT rendered as HTML `disabled`: a disabled <select> is excluded from
    # form submission entirely, which would silently blank out an existing
    # Server's default_location on any save that doesn't re-run the test. The
    # "test to populate" gating is CSS/JS-only (server_test_connection.js
    # toggles the "onms-location-pending" class) so the current value always
    # keeps posting normally.
    default_location = forms.CharField(
        required=False,
        label=_("Default location"),
        help_text=_(
            "Which OpenNMS monitoring location a member falls back to. Test "
            "the connection to (re)populate this from the Server's known "
            "locations."
        ),
        widget=forms.Select(choices=(), attrs={"class": "onms-location-pending"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        current = self.instance.default_location if self.instance.pk else ""
        choices = list(self.instance.available_locations) if self.instance.pk else []
        if current and current not in choices:
            choices = [current, *choices]
        self.fields["default_location"].widget.choices = [(loc, loc) for loc in choices]

    class Meta:
        model = OpenNMSServer
        fields = (
            "name",
            "url",
            "username",
            "password",
            "headers",
            "default_location",
            "is_default",
            "tenant_groups",
            "tenants",
            "site_groups",
            "sites",
            "locations",
            "tags",
        )
        widgets = {
            "username": forms.TextInput(),
            "password": forms.PasswordInput(render_value=True),
        }

    def clean(self):
        super().clean()
        if self.cleaned_data.get("is_default") and any(
            self.cleaned_data.get(f) for f in SCOPE_FIELDS
        ):
            self.add_error(
                "is_default", _("The Default Server may not carry Scope bindings.")
            )
        # A given object may be bound directly to only one Server at a time
        # (ADR 0002) — a same-level collision is a data-entry mistake, not a
        # legitimate case, so it's rejected here rather than left for
        # resolve_scope() to arbitrarily pick one of the two at sync time.
        for field in SCOPE_FIELDS:
            other = find_scope_collision(
                field,
                self.cleaned_data.get(field),
                exclude_pk=self.instance.pk or None,
            )
            if other is not None:
                self.add_error(
                    field,
                    _('Already bound directly to Server "%(server)s".')
                    % {"server": other},
                )
        return self.cleaned_data


class MonitoringExclusionForm(_ScopeForm):
    """Exclude a whole Scope level from monitoring (ADR 0003)."""

    class Meta:
        model = MonitoringExclusion
        fields = (
            "description",
            "tenant_groups",
            "tenants",
            "site_groups",
            "sites",
            "locations",
            "tags",
        )


class DiscoveryScanForm(NetBoxModelForm):
    """Trigger an OpenNMS Discovery scan over an IP range (ADR 0006).

    Not ``_ScopeForm``: a Discovery Scan targets one Requisition directly
    (for the VRF resolution it supplies via that Requisition's own scope, ADR
    0009) rather than binding across the five-level Scope hierarchy the way
    ``OpenNMSServer``/``MonitoringExclusion`` do. ``location`` is a plain
    ``<select>``, populated client-side from the chosen Server's own
    ``OpenNMSClient.list_locations()`` (``discoveryscan_server_locations.js``)
    the same way ``OpenNMSServerForm.default_location`` is — there is no
    NetBox "site" on an OpenNMS discovery request at all.
    """

    server = DynamicModelChoiceField(
        queryset=OpenNMSServer.objects.all(), label=_("OpenNMS Server")
    )
    requisition = DynamicModelChoiceField(
        queryset=Requisition.objects.all(),
        label=_("Requisition"),
        help_text=_(
            "Discovered nodes are imported against this Requisition's scope, "
            "which is also how their VRF is resolved."
        ),
    )
    location = forms.CharField(
        label=_("Location"),
        help_text=_(
            "The OpenNMS Monitoring Location. Pick the Server first to "
            "populate this from its known locations."
        ),
        widget=forms.Select(choices=(), attrs={"class": "onms-location-pending"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        current = self.instance.location if self.instance.pk else ""
        self.fields["location"].widget.choices = (
            [(current, current)] if current else []
        )

    class Meta:
        model = DiscoveryScan
        fields = (
            "server",
            "requisition",
            "location",
            "ip_range_begin",
            "ip_range_end",
            "retries",
            "timeout",
        )


class DiscoveredNodeFilterForm(NetBoxModelFilterSetForm):
    """Filter the Discovery scan results list by match verdict (issue #7)."""

    model = DiscoveredNode

    server = DynamicModelMultipleChoiceField(
        queryset=OpenNMSServer.objects.all(), required=False
    )
    verdict = forms.MultipleChoiceField(
        choices=DiscoveredNode._meta.get_field("verdict").choices,
        required=False,
    )


class DiscoveredNodeLinkForm(forms.Form):
    """Manually link (or correct) a Discovery row's matched NetBox object (issue #8).

    A plain form, not a ``NetBoxModelForm`` — it drives a single targeted
    action (``DiscoveredNode.link_to``) rather than editing the model's full
    field set, the same "device XOR virtual_machine" shape as
    ``MonitoringOverrideForm``.
    """

    device = DynamicModelChoiceField(
        queryset=Device.objects.all(), required=False, label=_("Device")
    )
    virtual_machine = DynamicModelChoiceField(
        queryset=VirtualMachine.objects.all(),
        required=False,
        label=_("Virtual Machine"),
    )

    def clean(self):
        cleaned_data = super().clean()
        device = cleaned_data.get("device")
        virtual_machine = cleaned_data.get("virtual_machine")
        if bool(device) == bool(virtual_machine):
            raise ValidationError(_("Select exactly one of Device or Virtual Machine."))
        return cleaned_data

    @property
    def target(self):
        return self.cleaned_data.get("device") or self.cleaned_data.get(
            "virtual_machine"
        )


class DiscoveredNodeImportFieldsMixin(forms.Form):
    """The fields ``import_node.import_node()`` needs from an operator, shared
    between single-row import (#9) and bulk import (#10).

    A plain ``forms.Form`` mixin, not ``NetBoxModelForm``: it builds one of
    two different model types depending on ``kind``, so its field set doesn't
    map 1:1 onto either model's ``Meta.fields``.
    """

    KIND_CHOICES = (("device", _("Device")), ("vm", _("Virtual Machine")))

    kind = forms.ChoiceField(choices=KIND_CHOICES, label=_("Object type"))
    site = DynamicModelChoiceField(
        queryset=Site.objects.all(), required=False, label=_("Site")
    )
    tenant = DynamicModelChoiceField(
        queryset=Tenant.objects.all(), required=False, label=_("Tenant")
    )
    role = DynamicModelChoiceField(
        queryset=DeviceRole.objects.all(), required=False, label=_("Role")
    )
    manufacturer = DynamicModelChoiceField(
        queryset=Manufacturer.objects.all(), required=False, label=_("Manufacturer")
    )
    device_type = DynamicModelChoiceField(
        queryset=DeviceType.objects.all(),
        required=False,
        label=_("Device type"),
        query_params={"manufacturer_id": "$manufacturer"},
        help_text=_("Required to create a Device."),
    )
    platform = DynamicModelChoiceField(
        queryset=Platform.objects.all(), required=False, label=_("Platform")
    )
    location = forms.CharField(
        required=False,
        label=_("OpenNMS location"),
        help_text=_("The OpenNMS monitoring location, not a NetBox Location."),
    )

    def clean(self):
        cleaned_data = super().clean()
        kind = cleaned_data.get("kind")
        if kind == "device":
            for required in ("site", "role", "device_type"):
                if not cleaned_data.get(required):
                    self.add_error(required, _("Required to create a Device."))
        location = cleaned_data.get("location") or ""
        error = location_name_error(location)
        if error:
            self.add_error("location", error)
        return cleaned_data


class DiscoveredNodeImportForm(DiscoveredNodeImportFieldsMixin):
    """Create a new Device/VM from a red Discovery row's proposal (issue #9).

    Every field mirrors an ``import_node.FieldProposal`` the operator can
    accept or correct — nothing here is ever applied without being shown
    first.
    """

    name = forms.CharField(label=_("Name"))
    field_order = [
        "kind",
        "name",
        "site",
        "tenant",
        "role",
        "manufacturer",
        "device_type",
        "platform",
        "location",
    ]


class DiscoveredNodeBulkImportForm(DiscoveredNodeImportFieldsMixin):
    """One shared field set applied to every row in a bulk import (issue #10).

    Deliberately has no per-row fields and no OpenNMS-derived initial values:
    bulk import must never apply an auto-detected guess, only what the
    operator explicitly chose for the whole batch. Each row's own ``name``
    comes from its Discovery row's label, not from this form.
    """


class MetadataEntryForm(NetBoxModelForm):
    """Define a metadata triad at a scope on a Requisition (RD-3)."""

    requisition = DynamicModelChoiceField(
        queryset=Requisition.objects.all(), label=_("Requisition")
    )

    class Meta:
        model = MetadataEntry
        fields = (
            "requisition",
            "scope",
            "context",
            "key",
            "value_source",
            "literal_value",
            "tags",
        )
