# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Plugin data models (Requisition redesign).

A **Requisition** is one user-named OpenNMS Foreign Source: it owns the
foreign-source *definition* (inline detectors + policies + scan-interval) and the
*requisition* (a live NetBox **filter** over Devices/VMs → nodes → interfaces →
services). Filters must be **disjoint**: an object matching two Requisitions is a
blocking *conflict* the user resolves (a node lives in exactly one Foreign
Source), so membership is deterministic and order-free. A **MonitoringOverride**
is an optional per-object exception (exclude / override management IP / add
interfaces / add-or-suppress services / override location). See the OpenSpec
changes ``requisition-redesign`` (R1–R8) and ``replace-priority-with-conflicts``
(C1–C7) for the full design.
"""

import ipaddress

from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.db import models
from django.urls import reverse
from django.utils import timezone
from netbox.models import NetBoxModel

from .choices import (
    DetectorPresetChoices,
    InterfaceRoleChoices,
    InterfaceScopeChoices,
    MetadataScopeChoices,
    NetBoxSourceChoices,
    ObjectTypeChoices,
    PolicyPresetChoices,
    ServiceChoices,
)
from .derivation import (
    discovery_foreign_source_for,
    validate_location_name,
    validate_requisition_name,
)
from .fields import EncryptedJSONField, EncryptedTextField
from .presets import (
    detector_required_params,
    policy_required_params,
    resolve_detector,
    resolve_policy,
)

# A Monitoring Override may attach to a Device or a VirtualMachine.
ASSIGNMENT_MODELS = models.Q(
    models.Q(app_label="dcim", model="device")
    | models.Q(app_label="virtualization", model="virtualmachine")
)


def _validate_service_names(names, field):
    """Raise if any entry in *names* is not a known ``ServiceChoices`` value."""
    valid = {value for value, _label in ServiceChoices()}
    bad = [name for name in (names or []) if name not in valid]
    if bad:
        raise ValidationError({field: f"Unknown service name(s): {', '.join(bad)}."})


def _require_preset_params(rule, required):
    """Raise if a preset's class-required params are unset (e.g. tcp ``port``).

    Some preset classes have no sensible default for a parameter (TcpDetector's
    port, NodeCategorySettingPolicy's category), so a user who picks the preset
    and skips the field would render a no-op/server-rejected rule. Caught here
    rather than at push time.
    """
    params = rule.parameters or {}
    missing = [key for key in required if not str(params.get(key, "")).strip()]
    if missing:
        raise ValidationError(
            {
                "parameters": f"The {rule.preset!r} preset requires: "
                f"{', '.join(missing)}."
            }
        )


class Requisition(NetBoxModel):
    """A user-named OpenNMS Foreign Source (definition + filter-scoped requisition).

    The **name** is the Foreign Source name (URL-path-safe, R1/H7). Membership is a
    live NetBox **filter** over the selected ``object_types``; an object matching
    two Requisitions' filters is a blocking **conflict** the user resolves (C1) —
    there is no automatic precedence. It owns its detectors/policies/scan-interval
    (the definition) and a set of declared ``services`` applied to every member's
    interfaces (R5).
    """

    name = models.CharField(max_length=100, unique=True)
    description = models.CharField(max_length=200, blank=True)
    # Which NetBox object types this Requisition's filter draws from.
    object_types = models.CharField(
        max_length=10,
        choices=ObjectTypeChoices,
        default=ObjectTypeChoices.BOTH,
    )
    # NetBox FilterSet query params (e.g. {"role": ["switch"], "tag": ["critical"]})
    # applied to the Device/VM filtersets to compute members (R2).
    filter_params = models.JSONField(default=dict, blank=True)
    # OpenNMS scan interval (a duration string, e.g. "1d", "30m").
    scan_interval = models.CharField(max_length=32, default="1d")
    # Which of a node's NetBox IPs become interfaces before per-object overrides.
    default_interfaces = models.CharField(
        max_length=16,
        choices=InterfaceScopeChoices,
        default=InterfaceScopeChoices.PRIMARY,
    )
    # Declared service names applied to every member's interfaces (R5); a
    # per-object override may add extra or suppress one of these.
    services = models.JSONField(default=list, blank=True)
    # OpenNMS monitoring location (which Minion polls these nodes). Blank falls
    # back to the configured default location at render time.
    location = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ("name",)
        verbose_name = "requisition"
        verbose_name_plural = "requisitions"

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("plugins:netbox_opennms:requisition", args=[self.pk])

    def clean(self):
        super().clean()
        try:
            validate_requisition_name(self.name)
        except ValueError as exc:
            raise ValidationError({"name": str(exc)}) from exc
        try:
            validate_location_name(self.location)
        except ValueError as exc:
            raise ValidationError({"location": str(exc)}) from exc
        if not isinstance(self.filter_params, dict):
            raise ValidationError({"filter_params": "Filter must be a mapping."})
        # Empty / no-effective-key filters are rejected in the resolution layer
        # (which knows the filtersets' keys); the model only guards the shape.
        if not isinstance(self.services, list):
            raise ValidationError({"services": "Services must be a list."})
        _validate_service_names(self.services, "services")


class _ProvisioningRule(NetBoxModel):
    """Shared base for a detector or policy: name + (preset|class) + parameters."""

    name = models.CharField(max_length=100)
    # The OpenNMS class; filled from the preset when one is chosen, or entered
    # directly for a freeform rule.
    rule_class = models.CharField(max_length=255, blank=True)
    parameters = models.JSONField(default=dict, blank=True)

    class Meta:
        abstract = True

    def __str__(self):
        return self.name


class MonitoringDetector(_ProvisioningRule):
    """A detector on a Requisition's definition (OpenNMS auto-discovers services)."""

    requisition = models.ForeignKey(
        to=Requisition, on_delete=models.CASCADE, related_name="detectors"
    )
    preset = models.CharField(max_length=50, choices=DetectorPresetChoices, blank=True)

    class Meta:
        ordering = ("requisition", "name")
        constraints = [
            models.UniqueConstraint(
                fields=("requisition", "name"),
                name="%(app_label)s_%(class)s_unique_name",
            ),
        ]
        verbose_name = "monitoring detector"
        verbose_name_plural = "monitoring detectors"

    def _apply_preset(self):
        # A KNOWN preset owns the class: it is (re)derived from the preset and the
        # user can't override it. An unknown preset (admin-extended via FIELD_CHOICES
        # with no registry entry) leaves any existing class untouched — never blanked.
        # Defaults are seeded only when parameters are empty, so a user-tuned/-deleted
        # parameter is not resurrected. Applied in both clean() and save() so it holds
        # on every path — the API/bulk paths don't run clean().
        if self.preset:
            cls, defaults = resolve_detector(self.preset)
            if cls:
                self.rule_class = cls
                if not self.parameters:
                    self.parameters = dict(defaults)

    def clean(self):
        super().clean()
        self._apply_preset()
        if not self.rule_class:
            raise ValidationError(
                {"rule_class": "Choose a preset or enter a detector class."}
            )
        _require_preset_params(self, detector_required_params(self.preset))

    def save(self, *args, **kwargs):
        self._apply_preset()
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse("plugins:netbox_opennms:monitoringdetector", args=[self.pk])


class MonitoringPolicy(_ProvisioningRule):
    """A policy on a Requisition's definition (categories, interface management…)."""

    requisition = models.ForeignKey(
        to=Requisition, on_delete=models.CASCADE, related_name="policies"
    )
    preset = models.CharField(max_length=50, choices=PolicyPresetChoices, blank=True)

    class Meta:
        ordering = ("requisition", "name")
        constraints = [
            models.UniqueConstraint(
                fields=("requisition", "name"),
                name="%(app_label)s_%(class)s_unique_name",
            ),
        ]
        verbose_name = "monitoring policy"
        verbose_name_plural = "monitoring policies"

    def _apply_preset(self):
        # A known preset owns the class (see MonitoringDetector._apply_preset).
        if self.preset:
            cls, defaults = resolve_policy(self.preset)
            if cls:
                self.rule_class = cls
                if not self.parameters:
                    self.parameters = dict(defaults)

    def clean(self):
        super().clean()
        self._apply_preset()
        if not self.rule_class:
            raise ValidationError(
                {"rule_class": "Choose a preset or enter a policy class."}
            )
        _require_preset_params(self, policy_required_params(self.preset))

    def save(self, *args, **kwargs):
        self._apply_preset()
        super().save(*args, **kwargs)

    def get_absolute_url(self):
        return reverse("plugins:netbox_opennms:monitoringpolicy", args=[self.pk])


class MonitoringOverride(NetBoxModel):
    """An optional per-object exception to its Requisition's defaults (R5/R6/H3).

    Absent an override, a matching Device/VM is monitored by the Requisition that
    claims it. One override per object (the GFK unique constraint also indexes the
    GFK). Resolution applies an override by object (via the GFK), so an override
    on an object that no Requisition currently claims is simply never applied.
    """

    assigned_object_type = models.ForeignKey(
        to="contenttypes.ContentType",
        on_delete=models.PROTECT,
        limit_choices_to=ASSIGNMENT_MODELS,
        related_name="+",
    )
    assigned_object_id = models.PositiveBigIntegerField()
    assigned_object = GenericForeignKey(
        ct_field="assigned_object_type",
        fk_field="assigned_object_id",
    )
    # Drop this object from monitoring entirely (monitored nowhere, M2; an
    # excluded object also never counts as a filter conflict, C3).
    exclude = models.BooleanField(default=False)
    # Override the management (primary) interface; null = use the object's primary_ip.
    management_ip = models.ForeignKey(
        to="ipam.IPAddress",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    # The SNMP role of the management interface (RD-5). Primary by default; set to
    # Secondary/Not-eligible to promote an additional interface to Primary instead.
    management_role = models.CharField(
        max_length=1,
        choices=InterfaceRoleChoices,
        default=InterfaceRoleChoices.PRIMARY,
    )
    # Extra interfaces are their own child rows (MonitoredInterface), each with a
    # per-interface SNMP role (RD-5), reachable via ``override.interfaces``.
    # Declared-service names to suppress for this object (R5); effective services =
    # (requisition.services ∪ added MonitoredService) − suppressed_services.
    suppressed_services = models.JSONField(default=list, blank=True)
    # Override the OpenNMS location for just this object; blank = use the Requisition's.
    location = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ("pk",)
        constraints = [
            models.UniqueConstraint(
                fields=("assigned_object_type", "assigned_object_id"),
                name="%(app_label)s_%(class)s_unique_object",
            ),
        ]
        verbose_name = "monitoring override"
        verbose_name_plural = "monitoring overrides"

    def __str__(self):
        if self.assigned_object is not None:
            return f"Override: {self.assigned_object}"
        return "Monitoring override"

    def get_absolute_url(self):
        return reverse("plugins:netbox_opennms:monitoringoverride", args=[self.pk])

    def clean(self):
        super().clean()
        try:
            validate_location_name(self.location)
        except ValueError as exc:
            raise ValidationError({"location": str(exc)}) from exc
        if not isinstance(self.suppressed_services, list):
            raise ValidationError(
                {"suppressed_services": "Suppressed services must be a list."}
            )
        _validate_service_names(self.suppressed_services, "suppressed_services")
        # At most one Primary per node (RD-5): making the management interface
        # Primary while an additional interface is already Primary is rejected —
        # the other direction is caught in MonitoredInterface.clean().
        if (
            self.pk
            and self.management_role == InterfaceRoleChoices.PRIMARY
            and self.interfaces.filter(role=InterfaceRoleChoices.PRIMARY).exists()
        ):
            raise ValidationError(
                {
                    "management_role": "An additional interface is already Primary; "
                    "at most one Primary interface per node."
                }
            )


class MonitoredService(NetBoxModel):
    """An explicit service ADDED on a Monitoring Override's interface (R5).

    The Requisition's declared services are the default; this is the additive
    per-object exception ("also monitor X on this IP"). ``name`` is drawn from the
    admin-extensible ``ServiceChoices``.
    """

    override = models.ForeignKey(
        to=MonitoringOverride, on_delete=models.CASCADE, related_name="services"
    )
    ip_address = models.ForeignKey(
        to="ipam.IPAddress", on_delete=models.CASCADE, related_name="+"
    )
    name = models.CharField(max_length=100, choices=ServiceChoices)

    class Meta:
        ordering = ("override", "ip_address", "name")
        constraints = [
            models.UniqueConstraint(
                fields=("override", "ip_address", "name"),
                name="%(app_label)s_%(class)s_unique_service",
            ),
        ]
        verbose_name = "monitored service"
        verbose_name_plural = "monitored services"

    def __str__(self):
        return f"{self.name} on {self.ip_address}"

    def get_absolute_url(self):
        return reverse("plugins:netbox_opennms:monitoredservice", args=[self.pk])

    def clean(self):
        super().clean()
        if (
            self.override_id
            and self.ip_address_id
            and self.ip_address_id not in override_ip_pks(self.override)
        ):
            raise ValidationError(
                {"ip_address": "Must be one of the override's IPs."}
            )


class MonitoredInterface(NetBoxModel):
    """An additional interface on a Monitoring Override, with its SNMP role (RD-5).

    Beyond the management (primary) interface, an override may add interfaces from
    the object's own NetBox IPs, each with a role — Primary / Secondary /
    Not-eligible (OpenNMS ``snmp-primary`` P/S/N). A node has **at most one**
    Primary (the management interface by default), enforced in ``clean()`` across
    the management interface + these rows.
    """

    override = models.ForeignKey(
        to=MonitoringOverride, on_delete=models.CASCADE, related_name="interfaces"
    )
    ip_address = models.ForeignKey(
        to="ipam.IPAddress", on_delete=models.CASCADE, related_name="+"
    )
    role = models.CharField(
        max_length=1,
        choices=InterfaceRoleChoices,
        default=InterfaceRoleChoices.NOT_ELIGIBLE,
    )

    class Meta:
        ordering = ("override", "ip_address")
        constraints = [
            models.UniqueConstraint(
                fields=("override", "ip_address"),
                name="%(app_label)s_%(class)s_unique_ip",
            ),
        ]
        verbose_name = "monitored interface"
        verbose_name_plural = "monitored interfaces"

    def __str__(self):
        return f"{self.ip_address} ({self.get_role_display()})"

    def get_absolute_url(self):
        return reverse("plugins:netbox_opennms:monitoredinterface", args=[self.pk])

    def clean(self):
        super().clean()
        if not self.override_id or not self.ip_address_id:
            return
        target = self.override.assigned_object
        # An additional interface must be one of the object's own IPs (AD-15).
        if target is not None and self.ip_address_id not in object_ip_pks(target):
            raise ValidationError(
                {"ip_address": "Must be an IP assigned to the override's object."}
            )
        # The management IP is the primary interface, modelled separately.
        management = self.override.management_ip or (
            target.primary_ip if target is not None else None
        )
        if management is not None and self.ip_address_id == management.pk:
            raise ValidationError(
                {"ip_address": "This is the management IP (the primary interface)."}
            )
        # At most one Primary per node (RD-5).
        if self.role == InterfaceRoleChoices.PRIMARY:
            if self.override.management_role == InterfaceRoleChoices.PRIMARY:
                raise ValidationError(
                    {
                        "role": "The management interface is Primary; set the "
                        "override's management role to Secondary/Not-eligible "
                        "before making another interface Primary."
                    }
                )
            others = self.override.interfaces.exclude(pk=self.pk).filter(
                role=InterfaceRoleChoices.PRIMARY
            )
            if others.exists():
                raise ValidationError(
                    {"role": "Another interface is already Primary; at most one "
                     "Primary interface per node."}
                )


class AssetMapping(NetBoxModel):
    """Maps a NetBox attribute to an OpenNMS node **asset** field (RD-2).

    Assets are a *fixed* schema (the ``OnmsAssetRecord`` field set, discovered via
    ``catalog.get_asset_fields``); ``asset_field`` is validated against it. The value
    is resolved per member from ``netbox_source`` at render time (unresolved → the
    ``<asset>`` is omitted). Data with no matching asset field belongs in metadata.
    """

    requisition = models.ForeignKey(
        to=Requisition, on_delete=models.CASCADE, related_name="asset_mappings"
    )
    netbox_source = models.CharField(max_length=100, choices=NetBoxSourceChoices)
    asset_field = models.CharField(max_length=64)

    class Meta:
        ordering = ("requisition", "asset_field")
        constraints = [
            models.UniqueConstraint(
                fields=("requisition", "asset_field"),
                name="%(app_label)s_%(class)s_unique_field",
            ),
        ]
        verbose_name = "asset mapping"
        verbose_name_plural = "asset mappings"

    def __str__(self):
        return f"{self.netbox_source} → {self.asset_field}"

    def get_absolute_url(self):
        return reverse("plugins:netbox_opennms:assetmapping", args=[self.pk])

    def clean(self):
        super().clean()
        # Validate against the discovered/known OnmsAssetRecord field set (RD-2).
        from .catalog import get_asset_fields

        if self.asset_field and self.asset_field not in get_asset_fields():
            raise ValidationError(
                {"asset_field": f"Unknown OpenNMS asset field {self.asset_field!r}."}
            )


class MetadataEntry(NetBoxModel):
    """A metadata triad at node / interface / service scope on a Requisition (RD-3).

    Rendered as ``<meta-data context=… key=… value=…/>`` under the matching element.
    ``context`` defaults to ``requisition``; a custom context MUST be ``X-``-prefixed.
    The value is a literal or resolved per member from ``value_source`` (a curated
    attribute or ``cf_<name>``); an unresolved value omits the ``<meta-data>``.
    """

    requisition = models.ForeignKey(
        to=Requisition, on_delete=models.CASCADE, related_name="metadata_entries"
    )
    scope = models.CharField(
        max_length=16, choices=MetadataScopeChoices, default=MetadataScopeChoices.NODE
    )
    context = models.CharField(max_length=64, default="requisition")
    key = models.CharField(max_length=100)
    # A curated attribute key or ``cf_<name>``; blank → use ``literal_value``.
    value_source = models.CharField(max_length=100, blank=True)
    literal_value = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ("requisition", "scope", "context", "key")
        constraints = [
            models.UniqueConstraint(
                fields=("requisition", "scope", "context", "key"),
                name="%(app_label)s_%(class)s_unique_meta",
            ),
        ]
        verbose_name = "metadata entry"
        verbose_name_plural = "metadata entries"

    def __str__(self):
        return f"{self.scope}:{self.context}:{self.key}"

    def get_absolute_url(self):
        return reverse("plugins:netbox_opennms:metadataentry", args=[self.pk])

    def clean(self):
        super().clean()
        if self.context != "requisition" and not self.context.startswith("X-"):
            raise ValidationError(
                {"context": "A custom context must be prefixed 'X-' (or use "
                 "'requisition')."}
            )
        if not self.value_source and not self.literal_value:
            raise ValidationError(
                "Set a value source or a literal value."
            )
        if self.value_source and self.literal_value:
            raise ValidationError(
                "Set either a value source or a literal value, not both."
            )


class OpenNMSServer(NetBoxModel):
    """One OpenNMS instance this NetBox manages monitoring intent for (ADR 0002).

    An MSP customer typically maps to one Server. Scope (the five ``ManyToMany``
    fields below, mirroring core's ``ConfigContext``) decides which Devices/VMs
    resolve to this Server — see ``scope.resolve_scope`` for the most-specific-
    wins precedence engine (location > site > site group > tenant > tenant
    group). The **Default Server** (``is_default=True``) carries no Scope
    bindings and is the fallback when nothing more specific matches; at most one
    may exist (enforced here), and it may not carry Scope bindings (enforced in
    ``OpenNMSServerForm``, where the M2M data is available pre-save).

    Credentials (``username``/``password``) and ``headers`` (ADR 0004, e.g. a
    Cloudflare Access service-token pair for a server behind a Tunnel) are
    Fernet-encrypted at rest (ADR 0005, superseding the prior AD-13).
    """

    name = models.CharField(max_length=100, unique=True)
    url = models.CharField(max_length=255)
    username = EncryptedTextField()
    password = EncryptedTextField()
    # Merged into every outbound request to this server (ADR 0004).
    headers = EncryptedJSONField(default=dict, blank=True)
    # Which OpenNMS Monitoring Location a member falls back to when it (and its
    # Requisition) set none — blank means the render leaves it unset.
    default_location = models.CharField(max_length=255, blank=True, default="")
    # OpenNMS Monitoring Locations this Server reported on the last successful
    # connection test (manual or hourly health check) — sourced from
    # ``OpenNMSClient.list_locations()``, cached here so the Requisition/Server
    # forms can offer a real dropdown instead of a free-text field.
    available_locations = models.JSONField(default=list, blank=True)
    is_default = models.BooleanField(
        default=False,
        help_text="Fallback Server used when no Scope binding matches an object.",
    )
    # Health check state (manual "Test connection" + hourly CheckServerHealthJob):
    # "failed" hard-blocks Sync/Remove/Move against this Server (SyncForeignSourceJob).
    # "unknown" (never checked) does not block — only a confirmed failure does.
    last_check_status = models.CharField(
        max_length=10,
        choices=(
            ("unknown", "Unknown"),
            ("ok", "OK"),
            ("failed", "Failed"),
        ),
        default="unknown",
    )
    last_check_time = models.DateTimeField(null=True, blank=True)
    last_check_message = models.CharField(max_length=500, blank=True, default="")
    tenant_groups = models.ManyToManyField(
        to="tenancy.TenantGroup", blank=True, related_name="+"
    )
    tenants = models.ManyToManyField(to="tenancy.Tenant", blank=True, related_name="+")
    site_groups = models.ManyToManyField(
        to="dcim.SiteGroup", blank=True, related_name="+"
    )
    sites = models.ManyToManyField(to="dcim.Site", blank=True, related_name="+")
    locations = models.ManyToManyField(to="dcim.Location", blank=True, related_name="+")

    class Meta:
        ordering = ("name",)
        verbose_name = "OpenNMS server"
        verbose_name_plural = "OpenNMS servers"

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("plugins:netbox_opennms:opennmsserver", args=[self.pk])

    def record_check_result(self, ok, message="", locations=None):
        """Persist a connection-test outcome (manual test or the hourly health
        check job) — the single write path so both stay consistent.

        ``locations`` is the Server's freshly-fetched ``list_locations()``
        result; pass it whenever a check successfully retrieved one so the
        cache stays current, or leave it ``None`` to leave the cache as-is
        (e.g. on failure, or when the caller skipped the location fetch).
        """
        self.last_check_status = "ok" if ok else "failed"
        self.last_check_time = timezone.now()
        self.last_check_message = "" if ok else message
        update_fields = ["last_check_status", "last_check_time", "last_check_message"]
        if locations is not None:
            self.available_locations = sorted(locations)
            update_fields.append("available_locations")
        self.save(update_fields=update_fields)

    @property
    def is_healthy(self):
        """False only once a check has explicitly failed — never-checked ("unknown")
        is not treated as unhealthy (see SyncForeignSourceJob's health guard)."""
        return self.last_check_status != "failed"

    def clean(self):
        super().clean()
        if not self.url.startswith(("http://", "https://")):
            raise ValidationError({"url": "URL must start with http:// or https://."})
        if self.default_location:
            try:
                validate_location_name(self.default_location)
            except ValueError as exc:
                raise ValidationError({"default_location": str(exc)}) from exc
        if (
            self.is_default
            and OpenNMSServer.objects.filter(is_default=True)
            .exclude(pk=self.pk)
            .exists()
        ):
            raise ValidationError(
                {"is_default": "Only one OpenNMS Server may be the Default Server."}
            )


class MonitoringExclusion(NetBoxModel):
    """Disable monitoring for a whole Scope level, without per-device overrides.

    Reuses the identical five-level Scope/precedence engine as ``OpenNMSServer``
    (``scope.resolve_scope``, ADR 0003): exclusion is just another possible
    resolution outcome, so a more specific inclusion — a site bound directly to
    a Server, or a per-device ``MonitoringOverride`` — re-enables monitoring
    underneath an excluded ancestor.
    """

    description = models.CharField(max_length=200, blank=True)
    tenant_groups = models.ManyToManyField(
        to="tenancy.TenantGroup", blank=True, related_name="+"
    )
    tenants = models.ManyToManyField(to="tenancy.Tenant", blank=True, related_name="+")
    site_groups = models.ManyToManyField(
        to="dcim.SiteGroup", blank=True, related_name="+"
    )
    sites = models.ManyToManyField(to="dcim.Site", blank=True, related_name="+")
    locations = models.ManyToManyField(to="dcim.Location", blank=True, related_name="+")

    class Meta:
        ordering = ("pk",)
        verbose_name = "monitoring exclusion"
        verbose_name_plural = "monitoring exclusions"

    def __str__(self):
        return self.description or f"Monitoring exclusion #{self.pk}"

    def get_absolute_url(self):
        return reverse("plugins:netbox_opennms:monitoringexclusion", args=[self.pk])


class DiscoveryScan(NetBoxModel):
    """One triggered OpenNMS Discovery scan over an IP range (ADR 0006).

    ``POST /api/v2/discovery`` is fire-and-forget: OpenNMS gives no job-status
    endpoint, so a Discovery Scan instead tags its request with a throwaway
    ``foreign_source`` (derived once at creation via
    ``derivation.discovery_foreign_source_for`` and never changed), which
    routes every resulting ``newSuspect`` event into a live ``OnmsNode`` row
    under that name. A later background Job (issue #27, out of scope here)
    polls for those nodes to infer completion; this model only covers the
    trigger itself.

    ``location`` is OpenNMS's own Monitoring Location (AD-9) — a live value
    from the bound Server's ``monitoringLocations`` endpoint, not a NetBox
    object; there is no NetBox "site" on an OpenNMS discovery request at all.
    ``requisition`` is the Requisition this scan's discovered nodes are
    imported against — its scope (site/location/tenant/…) is what
    ``scope.resolve_vrf`` uses to resolve a VRF for the addresses this scan
    finds (ADR 0009), via NetBox's own ``ipam.Prefix`` scope+vrf rather than a
    bespoke binding table.
    """

    server = models.ForeignKey(
        to=OpenNMSServer, on_delete=models.CASCADE, related_name="discovery_scans"
    )
    requisition = models.ForeignKey(
        to="Requisition",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="discovery_scans",
        help_text="Discovered nodes are imported against this Requisition's "
        "scope, which is also how their VRF is resolved.",
    )
    location = models.CharField(
        max_length=100,
        blank=True,
        help_text="The OpenNMS Monitoring Location (not a NetBox Location).",
    )
    # Derived once in save() — never user-editable (excluded from forms via
    # editable=False, mirroring DeployedForeignSource's ownership model).
    foreign_source = models.CharField(
        max_length=100, unique=True, editable=False, blank=True
    )
    ip_range_begin = models.GenericIPAddressField(verbose_name="IP Range Begin")
    ip_range_end = models.GenericIPAddressField(verbose_name="IP Range End")
    retries = models.PositiveSmallIntegerField(default=1)
    timeout = models.PositiveIntegerField(
        default=2000, help_text="Per-address timeout, in milliseconds."
    )
    # Set by the Trigger action (mark_triggered) — None means never triggered.
    last_triggered = models.DateTimeField(null=True, blank=True, editable=False)
    # Written by PollDiscoveryScansJob (issue #27) — the newest ``createTime``
    # seen across this scan's own OpenNMS nodes on any poll so far. Together
    # with last_triggered, this is the "reference point" completion inference
    # measures idleness from: None means no node has appeared yet.
    latest_node_created = models.DateTimeField(null=True, blank=True, editable=False)
    # Set once the poll infers the scan has gone quiet (no new node for
    # discovery_settle_idle_minutes) — never cleared, so a settled scan stays
    # settled and the poll stops re-fetching it.
    settled_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        ordering = ("-created",)
        verbose_name = "discovery scan"
        verbose_name_plural = "discovery scans"

    def __str__(self):
        return self.foreign_source or f"Discovery scan #{self.pk}"

    def get_absolute_url(self):
        return reverse("plugins:netbox_opennms:discoveryscan", args=[self.pk])

    @property
    def monitoring_location(self):
        """The OpenNMS Monitoring Location this scan supplies (ADR 0006)."""
        return self.location

    @property
    def status(self):
        """This scan's lifecycle state (ADR 0006/issue #27), for display.

        ``"pending"`` — never triggered. ``"running"`` — triggered, still
        being polled (nodes may still be appearing). ``"settled"`` — the poll
        has inferred completion (no new node for a while); the scan's node
        count is stable.
        """
        if not self.last_triggered:
            return "pending"
        if self.settled_at:
            return "settled"
        return "running"

    def clean(self):
        super().clean()
        if not self.requisition_id:
            raise ValidationError(
                {"requisition": "A Discovery Scan requires a Requisition."}
            )
        if not self.location:
            raise ValidationError(
                {"location": "A Discovery Scan requires an OpenNMS Monitoring "
                "Location."}
            )
        try:
            validate_location_name(self.location)
        except ValueError as exc:
            raise ValidationError({"location": str(exc)}) from exc
        if self.ip_range_begin and self.ip_range_end:
            try:
                begin = ipaddress.ip_address(self.ip_range_begin)
                end = ipaddress.ip_address(self.ip_range_end)
            except ValueError:
                return  # field validators already flag an unparseable address
            if begin.version != end.version:
                raise ValidationError(
                    {
                        "ip_range_end": "Must be the same IP version as the "
                        "range start."
                    }
                )
            if end < begin:
                raise ValidationError(
                    {"ip_range_end": "Must not be before the range start."}
                )

    def save(self, *args, **kwargs):
        if not self.foreign_source:
            self.foreign_source = discovery_foreign_source_for(timezone.now())
        super().save(*args, **kwargs)

    def mark_triggered(self):
        """Persist the fired timestamp — the single write path (Trigger action)."""
        self.last_triggered = timezone.now()
        self.save(update_fields=["last_triggered"])


class DeployedForeignSource(models.Model):
    """A Foreign Source name NetBox has pushed to OpenNMS — the reconciler's
    ownership record (review #4).

    Requisition names are user-chosen, so the drift reconciler can't use a
    ``netbox.`` prefix to tell ours from foreign requisitions. A row is written when
    a sync succeeds and removed when the Foreign Source's shell is deleted, so
    ``orphaned_foreign_sources`` scopes cleanup to exactly the names we manage and
    never touches a requisition NetBox didn't create. ``server`` records which
    OpenNMS Server the name was last pushed to, so the reconciler and a manual
    Remove of a Requisition-less name can find the right client to target.
    """

    name = models.CharField(max_length=100, unique=True)
    server = models.ForeignKey(
        to="netbox_opennms.OpenNMSServer",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        ordering = ("name",)
        verbose_name = "deployed foreign source"
        verbose_name_plural = "deployed foreign sources"

    def __str__(self):
        return self.name


class DiscoveredNode(NetBoxModel):
    """One OpenNMS node found by a Discovery scan, with its NetBox match
    verdict (issue #7).

    Populated by scanning an ``OpenNMSServer``'s live node inventory
    (``scan.scan_server``) and upserting by ``(server, opennms_node_id)``, so
    a re-scan against unchanged state refreshes the same rows rather than
    creating duplicates. ``matched_object`` is set for a green/orange verdict
    (the NetBox Device/VM the OpenNMS node's Foreign ID resolved to) and left
    unset for red — the attachment point manual linking (issue #8) and later
    import resolve against.

    ``resolution`` distinguishes a row whose match came from the scan's own
    Foreign-ID reconciliation (``"scanned"``, the default) from one an
    operator has manually linked (``"linked"``, issue #8). A re-scan's
    upsert (``OpenNMSServerScanView``) never overwrites ``verdict``,
    ``diff_detail``, or ``matched_object`` on a ``"linked"`` row — otherwise
    a scan that can't itself resolve the node's Foreign ID would silently
    erase the operator's decision on every re-scan.
    """

    server = models.ForeignKey(
        to="netbox_opennms.OpenNMSServer",
        on_delete=models.CASCADE,
        related_name="discovered_nodes",
    )
    # Set only when this row came from a Discovery Scan's own poll (issue
    # #27); null for a row from the general per-Server scan (issue #7). Lets
    # a scan's stale-row cleanup scope itself to just its own rows — see
    # scan.upsert_discovered_nodes.
    discovery_scan = models.ForeignKey(
        to="netbox_opennms.DiscoveryScan",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="discovered_nodes",
    )
    opennms_node_id = models.PositiveIntegerField()
    label = models.CharField(max_length=255)
    foreign_source = models.CharField(max_length=100, blank=True, default="")
    foreign_id = models.CharField(max_length=100, blank=True, default="")
    location = models.CharField(max_length=255, blank=True, default="")
    verdict = models.CharField(
        max_length=6,
        choices=(
            ("green", "Matches NetBox"),
            ("orange", "Differs from NetBox"),
            ("red", "Missing from NetBox"),
        ),
    )
    diff_detail = models.JSONField(blank=True, default=list)
    # Walked once by PollDiscoveryScansJob for a Discovery Scan row (issue
    # #28, ADR 0007) and persisted here so review/import reads NetBox's own
    # stored copy rather than depending on the OpenNMS-side node still
    # existing once auto-cleanup (issue #29) removes it. Left empty/unset for
    # a general per-Server scan row (issue #7) — those are never walked; the
    # review view falls back to a live fetch when walked_at is unset.
    node_detail = models.JSONField(blank=True, default=dict)
    ip_interfaces = models.JSONField(blank=True, default=list)
    services_by_ip = models.JSONField(blank=True, default=dict)
    # Field names the walked OpenNMS data didn't cover, computed at walk time
    # via import_node.compute_completeness_gaps (issue #28) — surfaced so an
    # operator can tell "needs manual input" apart from "not walked yet".
    completeness_gaps = models.JSONField(blank=True, default=list)
    walked_at = models.DateTimeField(null=True, blank=True, editable=False)
    matched_object_type = models.ForeignKey(
        to="contenttypes.ContentType",
        on_delete=models.PROTECT,
        limit_choices_to=ASSIGNMENT_MODELS,
        null=True,
        blank=True,
        related_name="+",
    )
    matched_object_id = models.PositiveBigIntegerField(null=True, blank=True)
    matched_object = GenericForeignKey(
        ct_field="matched_object_type",
        fk_field="matched_object_id",
    )
    resolution = models.CharField(
        max_length=8,
        choices=(
            ("scanned", "Scanned"),
            ("linked", "Manually linked"),
        ),
        default="scanned",
    )
    last_scanned = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("server", "label")
        constraints = [
            models.UniqueConstraint(
                fields=("server", "opennms_node_id"),
                name="%(app_label)s_%(class)s_unique_server_node",
            ),
        ]
        verbose_name = "discovered node"
        verbose_name_plural = "discovered nodes"

    def __str__(self):
        return self.label or f"Node #{self.opennms_node_id}"

    def get_absolute_url(self):
        return reverse("plugins:netbox_opennms:discoverednode", args=[self.pk])

    @classmethod
    def for_object(cls, target):
        """The Discovery row (if any) matched to *target* (a Device/VM).

        The reverse of ``matched_object`` — a ``GenericForeignKey`` can't be
        queried backwards without going through its content type explicitly.
        Used by the Node Links tab (#15) to find a Device's OpenNMS node.
        """
        content_type = ContentType.objects.get_for_model(type(target))
        return cls.objects.filter(
            matched_object_type=content_type, matched_object_id=target.pk
        ).first()

    def link_to(self, target):
        """Manually resolve this row to *target* (a Device or VirtualMachine).

        The single write path for a manual link/correct action (issue #8),
        so a linked row's fields are always set together — mirrors
        ``OpenNMSServer.record_check_result``'s "one place, always
        consistent" write. Marking ``resolution="linked"`` is what stops a
        later re-scan from overwriting the decision (see the class
        docstring).
        """
        self.matched_object = target
        self.resolution = "linked"
        self.verdict = "green"
        self.diff_detail = []
        self.save(
            update_fields=[
                "matched_object_type",
                "matched_object_id",
                "resolution",
                "verdict",
                "diff_detail",
            ]
        )


def object_ip_pks(target):
    """PKs of the IPs assigned to a Device/VM's interfaces (its own addresses)."""
    pks = set()
    interfaces = getattr(target, "interfaces", None)
    if interfaces is None:
        return pks
    for interface in interfaces.all():
        pks.update(interface.ip_addresses.values_list("pk", flat=True))
    return pks


def override_ip_pks(override):
    """PKs of an override's interfaces: management IP + additional interfaces."""
    pks = set()
    if override.management_ip_id:
        pks.add(override.management_ip_id)
    pks.update(override.interfaces.values_list("ip_address_id", flat=True))
    return pks
