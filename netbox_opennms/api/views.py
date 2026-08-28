# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""REST API views (Requisition redesign)."""

from netbox.api.viewsets import NetBoxModelViewSet

from ..filtersets import (
    AssetMappingFilterSet,
    DiscoveredNodeFilterSet,
    DiscoveryScanFilterSet,
    MetadataContextFilterSet,
    MetadataEntryFilterSet,
    MetadataKeyFilterSet,
    MonitoredInterfaceFilterSet,
    MonitoredServiceFilterSet,
    MonitoringDetectorFilterSet,
    MonitoringExclusionFilterSet,
    MonitoringOverrideFilterSet,
    MonitoringPolicyFilterSet,
    OpenNMSServerFilterSet,
    RequisitionFilterSet,
)
from ..models import (
    AssetMapping,
    DiscoveredNode,
    DiscoveryScan,
    MetadataContext,
    MetadataEntry,
    MetadataKey,
    MonitoredInterface,
    MonitoredService,
    MonitoringDetector,
    MonitoringExclusion,
    MonitoringOverride,
    MonitoringPolicy,
    OpenNMSServer,
    Requisition,
)
from .serializers import (
    AssetMappingSerializer,
    DiscoveredNodeSerializer,
    DiscoveryScanSerializer,
    MetadataContextSerializer,
    MetadataEntrySerializer,
    MetadataKeySerializer,
    MonitoredInterfaceSerializer,
    MonitoredServiceSerializer,
    MonitoringDetectorSerializer,
    MonitoringExclusionSerializer,
    MonitoringOverrideSerializer,
    MonitoringPolicySerializer,
    OpenNMSServerSerializer,
    RequisitionSerializer,
)


class RequisitionViewSet(NetBoxModelViewSet):
    queryset = Requisition.objects.prefetch_related("detectors", "policies")
    serializer_class = RequisitionSerializer
    filterset_class = RequisitionFilterSet


class MonitoringDetectorViewSet(NetBoxModelViewSet):
    queryset = MonitoringDetector.objects.select_related("requisition")
    serializer_class = MonitoringDetectorSerializer
    filterset_class = MonitoringDetectorFilterSet


class MonitoringPolicyViewSet(NetBoxModelViewSet):
    queryset = MonitoringPolicy.objects.select_related("requisition")
    serializer_class = MonitoringPolicySerializer
    filterset_class = MonitoringPolicyFilterSet


class MonitoringOverrideViewSet(NetBoxModelViewSet):
    queryset = MonitoringOverride.objects.prefetch_related(
        "interfaces", "services"
    ).select_related("assigned_object_type", "management_ip")
    serializer_class = MonitoringOverrideSerializer
    filterset_class = MonitoringOverrideFilterSet


class MonitoredServiceViewSet(NetBoxModelViewSet):
    queryset = MonitoredService.objects.select_related("override", "ip_address")
    serializer_class = MonitoredServiceSerializer
    filterset_class = MonitoredServiceFilterSet


class MonitoredInterfaceViewSet(NetBoxModelViewSet):
    queryset = MonitoredInterface.objects.select_related("override", "ip_address")
    serializer_class = MonitoredInterfaceSerializer
    filterset_class = MonitoredInterfaceFilterSet


class AssetMappingViewSet(NetBoxModelViewSet):
    queryset = AssetMapping.objects.select_related("requisition")
    serializer_class = AssetMappingSerializer
    filterset_class = AssetMappingFilterSet


class MetadataContextViewSet(NetBoxModelViewSet):
    # No further destroy()/perform_destroy() override needed: a built-in
    # row's MetadataContext.delete() raises ProtectedError, which NetBox's
    # DRF exception handler already turns into a 409 (the same path as any
    # on_delete=PROTECT FK elsewhere in NetBox core).
    queryset = MetadataContext.objects.all()
    serializer_class = MetadataContextSerializer
    filterset_class = MetadataContextFilterSet


class MetadataKeyViewSet(NetBoxModelViewSet):
    # No further destroy()\perform_destroy() override needed: same rationale
    # as MetadataContextViewSet above — MetadataKey.delete() raises
    # ProtectedError for built-in rows, which NetBox's DRF exception handler
    # already turns into a 409.
    queryset = MetadataKey.objects.select_related("context")
    serializer_class = MetadataKeySerializer
    filterset_class = MetadataKeyFilterSet


class MetadataEntryViewSet(NetBoxModelViewSet):
    queryset = MetadataEntry.objects.select_related("requisition")
    serializer_class = MetadataEntrySerializer
    filterset_class = MetadataEntryFilterSet


class OpenNMSServerViewSet(NetBoxModelViewSet):
    queryset = OpenNMSServer.objects.prefetch_related(
        "tenant_groups", "tenants", "site_groups", "sites", "locations"
    )
    serializer_class = OpenNMSServerSerializer
    filterset_class = OpenNMSServerFilterSet


class MonitoringExclusionViewSet(NetBoxModelViewSet):
    queryset = MonitoringExclusion.objects.prefetch_related(
        "tenant_groups", "tenants", "site_groups", "sites", "locations"
    )
    serializer_class = MonitoringExclusionSerializer
    filterset_class = MonitoringExclusionFilterSet


class DiscoveredNodeViewSet(NetBoxModelViewSet):
    queryset = DiscoveredNode.objects.select_related("server", "matched_object_type")
    serializer_class = DiscoveredNodeSerializer
    filterset_class = DiscoveredNodeFilterSet


class DiscoveryScanViewSet(NetBoxModelViewSet):
    queryset = DiscoveryScan.objects.select_related("server", "requisition")
    serializer_class = DiscoveryScanSerializer
    filterset_class = DiscoveryScanFilterSet
