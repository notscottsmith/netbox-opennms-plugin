# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""REST API serializers (Requisition redesign)."""

from django.contrib.contenttypes.models import ContentType
from netbox.api.fields import ContentTypeField
from netbox.api.serializers import NetBoxModelSerializer
from rest_framework import serializers
from utilities.api import get_serializer_for_model

from ..derivation import location_name_error, requisition_name_error
from ..membership import filter_errors
from ..models import (
    ASSIGNMENT_MODELS,
    AssetMapping,
    Category,
    DiscoveredNode,
    DiscoveryScan,
    MetadataContext,
    MetadataEntry,
    MetadataKey,
    MetadataPullMapping,
    MonitoredInterface,
    MonitoredService,
    MonitoringDetector,
    MonitoringExclusion,
    MonitoringOverride,
    MonitoringPolicy,
    OpenNMSServer,
    Requisition,
)
from ..scope import SCOPE_FIELDS, find_scope_collision


def _validate_location(value):
    error = location_name_error(value)
    if error:
        raise serializers.ValidationError(error)
    return value


class RequisitionSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_opennms-api:requisition-detail"
    )

    class Meta:
        model = Requisition
        fields = (
            "id",
            "url",
            "display",
            "name",
            "description",
            "object_types",
            "filter_params",
            "scan_interval",
            "default_interfaces",
            "services",
            "location",
            "default_categories",
            "tags",
            "custom_fields",
            "created",
            "last_updated",
        )
        brief_fields = ("id", "url", "display", "name")

    def validate_location(self, value):
        return _validate_location(value)

    def validate_name(self, value):
        error = requisition_name_error(value)
        if error:
            raise serializers.ValidationError(error)
        return value

    def validate(self, data):
        data = super().validate(data)
        # Build a transient instance to reuse the resolver's filter guard (C1/H1).
        instance = self.instance or Requisition()
        for attr in ("object_types", "filter_params"):
            if attr in data:
                setattr(instance, attr, data[attr])
        errors = filter_errors(instance)
        if errors:
            raise serializers.ValidationError({"filter_params": errors})
        return data


class MonitoringDetectorSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_opennms-api:monitoringdetector-detail"
    )

    class Meta:
        model = MonitoringDetector
        fields = (
            "id",
            "url",
            "display",
            "requisition",
            "name",
            "preset",
            "rule_class",
            "parameters",
            "tags",
            "custom_fields",
            "created",
            "last_updated",
        )
        brief_fields = ("id", "url", "display", "name")


class MonitoringPolicySerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_opennms-api:monitoringpolicy-detail"
    )

    class Meta:
        model = MonitoringPolicy
        fields = (
            "id",
            "url",
            "display",
            "requisition",
            "name",
            "preset",
            "rule_class",
            "parameters",
            "tags",
            "custom_fields",
            "created",
            "last_updated",
        )
        brief_fields = ("id", "url", "display", "name")


class MonitoringOverrideSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_opennms-api:monitoringoverride-detail"
    )
    assigned_object_type = ContentTypeField(
        queryset=ContentType.objects.filter(ASSIGNMENT_MODELS)
    )
    assigned_object = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = MonitoringOverride
        fields = (
            "id",
            "url",
            "display",
            "assigned_object_type",
            "assigned_object_id",
            "assigned_object",
            "exclude",
            "management_ip",
            "management_role",
            "suppressed_services",
            "location",
            "categories",
            "tags",
            "custom_fields",
            "created",
            "last_updated",
        )
        brief_fields = ("id", "url", "display", "exclude")

    def validate_location(self, value):
        return _validate_location(value)

    def get_assigned_object(self, obj):
        if obj.assigned_object is None:
            return None
        serializer = get_serializer_for_model(obj.assigned_object)
        context = {"request": self.context["request"]}
        return serializer(obj.assigned_object, nested=True, context=context).data

    def validate(self, data):
        data = super().validate(data)
        content_type = data.get("assigned_object_type") or getattr(
            self.instance, "assigned_object_type", None
        )
        object_id = data.get("assigned_object_id") or getattr(
            self.instance, "assigned_object_id", None
        )
        if content_type is not None and object_id is not None:
            model = content_type.model_class()
            if not model.objects.filter(pk=object_id).exists():
                raise serializers.ValidationError(
                    {"assigned_object_id": "The referenced object does not exist."}
                )
            duplicate = MonitoringOverride.objects.filter(
                assigned_object_type=content_type, assigned_object_id=object_id
            )
            if self.instance is not None:
                duplicate = duplicate.exclude(pk=self.instance.pk)
            if duplicate.exists():
                raise serializers.ValidationError(
                    "This object already has a Monitoring Override."
                )
        return data


class MonitoredServiceSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_opennms-api:monitoredservice-detail"
    )

    class Meta:
        model = MonitoredService
        fields = (
            "id",
            "url",
            "display",
            "override",
            "ip_address",
            "name",
            "tags",
            "custom_fields",
            "created",
            "last_updated",
        )
        brief_fields = ("id", "url", "display", "name")


class MonitoredInterfaceSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_opennms-api:monitoredinterface-detail"
    )

    class Meta:
        model = MonitoredInterface
        fields = (
            "id",
            "url",
            "display",
            "override",
            "ip_address",
            "role",
            "tags",
            "custom_fields",
            "created",
            "last_updated",
        )
        brief_fields = ("id", "url", "display", "role")


class AssetMappingSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_opennms-api:assetmapping-detail"
    )

    class Meta:
        model = AssetMapping
        fields = (
            "id",
            "url",
            "display",
            "requisition",
            "netbox_source",
            "asset_field",
            "tags",
            "custom_fields",
            "created",
            "last_updated",
        )
        brief_fields = ("id", "url", "display", "asset_field")


class OpenNMSServerSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_opennms-api:opennmsserver-detail"
    )
    # The model's own `url` field (the OpenNMS connection URL) collides with the
    # self-link above, so it's exposed under a distinct API name.
    server_url = serializers.CharField(source="url")
    headers = serializers.JSONField(required=False, write_only=True)

    class Meta:
        model = OpenNMSServer
        fields = (
            "id",
            "url",
            "display",
            "name",
            "server_url",
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
            "custom_fields",
            "created",
            "last_updated",
        )
        brief_fields = ("id", "url", "display", "name")
        extra_kwargs = {"password": {"write_only": True}}

    def validate_server_url(self, value):
        if not value.startswith(("http://", "https://")):
            raise serializers.ValidationError(
                "URL must start with http:// or https://."
            )
        return value

    def validate_default_location(self, value):
        return _validate_location(value)

    def validate(self, data):
        data = super().validate(data)
        is_default = data.get("is_default", getattr(self.instance, "is_default", False))
        if is_default:
            existing = OpenNMSServer.objects.filter(is_default=True)
            if self.instance is not None:
                existing = existing.exclude(pk=self.instance.pk)
            if existing.exists():
                raise serializers.ValidationError(
                    {"is_default": "Only one OpenNMS Server may be the Default Server."}
                )

            def _effective(f):
                if f in data:
                    return data[f]
                return self.instance and getattr(self.instance, f).all()

            if any(_effective(f) for f in SCOPE_FIELDS):
                raise serializers.ValidationError(
                    {"is_default": "The Default Server may not carry Scope bindings."}
                )
        # A given object may be bound directly to only one Server at a time
        # (ADR 0002) — mirrors OpenNMSServerForm's identical check, since the
        # API is an equally valid assignment surface.
        exclude_pk = self.instance.pk if self.instance is not None else None
        for field in SCOPE_FIELDS:
            other = find_scope_collision(field, data.get(field), exclude_pk=exclude_pk)
            if other is not None:
                raise serializers.ValidationError(
                    {field: f'Already bound directly to Server "{other}".'}
                )
        return data


class MonitoringExclusionSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_opennms-api:monitoringexclusion-detail"
    )

    class Meta:
        model = MonitoringExclusion
        fields = (
            "id",
            "url",
            "display",
            "description",
            "tenant_groups",
            "tenants",
            "site_groups",
            "sites",
            "locations",
            "tags",
            "custom_fields",
            "created",
            "last_updated",
        )
        brief_fields = ("id", "url", "display", "description")


class DiscoveryScanSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_opennms-api:discoveryscan-detail"
    )

    class Meta:
        model = DiscoveryScan
        fields = (
            "id",
            "url",
            "display",
            "server",
            "requisition",
            "location",
            "foreign_source",
            "ip_range_begin",
            "ip_range_end",
            "retries",
            "timeout",
            "last_triggered",
            "tags",
            "custom_fields",
            "created",
            "last_updated",
        )
        brief_fields = ("id", "url", "display", "foreign_source")
        # Not independently settable (issue report) — DiscoveryScan.clean()
        # always derives it from requisition.location / server.default_location.
        read_only_fields = ("location",)

    # Requisition/location derivation and validation both happen in
    # DiscoveryScan.clean(), invoked by NetBoxModelSerializer.validate()
    # via instance.full_clean() — no serializer-level override needed.


class DiscoveredNodeSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_opennms-api:discoverednode-detail"
    )
    matched_object_type = ContentTypeField(
        queryset=ContentType.objects.filter(ASSIGNMENT_MODELS),
        allow_null=True,
        required=False,
    )
    matched_object = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = DiscoveredNode
        fields = (
            "id",
            "url",
            "display",
            "server",
            "opennms_node_id",
            "label",
            "foreign_source",
            "foreign_id",
            "location",
            "verdict",
            "diff_detail",
            "resolution",
            "matched_object_type",
            "matched_object_id",
            "matched_object",
            "last_scanned",
            "tags",
            "custom_fields",
            "created",
            "last_updated",
        )
        brief_fields = ("id", "url", "display", "label", "verdict")

    def get_matched_object(self, obj):
        if obj.matched_object is None:
            return None
        serializer = get_serializer_for_model(obj.matched_object)
        context = {"request": self.context["request"]}
        return serializer(obj.matched_object, nested=True, context=context).data


class MetadataContextSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_opennms-api:metadatacontext-detail"
    )

    class Meta:
        model = MetadataContext
        fields = (
            "id",
            "url",
            "display",
            "name",
            "is_builtin",
            "description",
            "tags",
            "custom_fields",
            "created",
            "last_updated",
        )
        brief_fields = ("id", "url", "display", "name")


class MetadataKeySerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_opennms-api:metadatakey-detail"
    )

    class Meta:
        model = MetadataKey
        fields = (
            "id",
            "url",
            "display",
            "context",
            "name",
            "is_builtin",
            "description",
            "tags",
            "custom_fields",
            "created",
            "last_updated",
        )
        brief_fields = ("id", "url", "display", "name")


class MetadataEntrySerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_opennms-api:metadataentry-detail"
    )

    class Meta:
        model = MetadataEntry
        fields = (
            "id",
            "url",
            "display",
            "requisition",
            "scope",
            "context",
            "key",
            "value_source",
            "literal_value",
            "tags",
            "custom_fields",
            "created",
            "last_updated",
        )
        brief_fields = ("id", "url", "display", "key")


class CategorySerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_opennms-api:category-detail"
    )

    class Meta:
        model = Category
        fields = (
            "id",
            "url",
            "display",
            "name",
            "description",
            "tags",
            "custom_fields",
            "created",
            "last_updated",
        )
        brief_fields = ("id", "url", "display", "name")


class MetadataPullMappingSerializer(NetBoxModelSerializer):
    url = serializers.HyperlinkedIdentityField(
        view_name="plugins-api:netbox_opennms-api:metadatapullmapping-detail"
    )

    class Meta:
        model = MetadataPullMapping
        fields = (
            "id",
            "url",
            "display",
            "requisition",
            "context",
            "key",
            "netbox_target",
            "tags",
            "custom_fields",
            "created",
            "last_updated",
        )
        brief_fields = ("id", "url", "display", "key")
