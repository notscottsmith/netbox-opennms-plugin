# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""UI URL routing (Requisition redesign)."""

from django.urls import path
from netbox.views.generic import ObjectChangeLogView

from . import views
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


def _crud(prefix, name, view_prefix, model, *, bulk_delete=True):
    """The standard list/add/<pk>/edit/delete/changelog routes for a model."""
    routes = [
        path(
            f"{prefix}/",
            getattr(views, f"{view_prefix}ListView").as_view(),
            name=f"{name}_list",
        ),
        path(
            f"{prefix}/add/",
            getattr(views, f"{view_prefix}EditView").as_view(),
            name=f"{name}_add",
        ),
    ]
    if bulk_delete:
        routes.append(
            path(
                f"{prefix}/delete/",
                getattr(views, f"{view_prefix}BulkDeleteView").as_view(),
                name=f"{name}_bulk_delete",
            )
        )
    routes += [
        path(
            f"{prefix}/<int:pk>/",
            getattr(views, f"{view_prefix}View").as_view(),
            name=name,
        ),
        path(
            f"{prefix}/<int:pk>/edit/",
            getattr(views, f"{view_prefix}EditView").as_view(),
            name=f"{name}_edit",
        ),
        path(
            f"{prefix}/<int:pk>/delete/",
            getattr(views, f"{view_prefix}DeleteView").as_view(),
            name=f"{name}_delete",
        ),
        path(
            f"{prefix}/<int:pk>/changelog/",
            ObjectChangeLogView.as_view(),
            name=f"{name}_changelog",
            kwargs={"model": model},
        ),
    ]
    return routes


urlpatterns = (
    *_crud("requisitions", "requisition", "Requisition", Requisition),
    path(
        "requisitions/<int:pk>/sync/",
        views.RequisitionSyncView.as_view(),
        name="requisition_sync",
    ),
    path(
        "requisitions/<int:pk>/duplicate/",
        views.RequisitionDuplicateView.as_view(),
        name="requisition_duplicate",
    ),
    path(
        "requisitions/<int:pk>/scan/",
        views.RequisitionScanView.as_view(),
        name="requisition_scan",
    ),
    path(
        "requisitions/<int:pk>/nodes/<int:opennms_node_id>/walk/",
        views.RequisitionNodeWalkView.as_view(),
        name="requisition_node_walk",
    ),
    path(
        "requisitions/<int:pk>/nodes/<str:foreign_id>/sync/",
        views.RequisitionSyncNodeView.as_view(),
        name="requisition_sync_node",
    ),
    path(
        "requisitions/<int:pk>/nodes/<str:foreign_id>/sync/override/",
        views.RequisitionSyncNodeOverrideView.as_view(),
        name="requisition_sync_node_override",
    ),
    *_crud(
        "monitoring-detectors",
        "monitoringdetector",
        "MonitoringDetector",
        MonitoringDetector,
    ),
    *_crud(
        "monitoring-policies", "monitoringpolicy", "MonitoringPolicy", MonitoringPolicy
    ),
    *_crud(
        "monitoring-overrides",
        "monitoringoverride",
        "MonitoringOverride",
        MonitoringOverride,
    ),
    *_crud(
        "monitored-services", "monitoredservice", "MonitoredService", MonitoredService
    ),
    *_crud(
        "monitored-interfaces",
        "monitoredinterface",
        "MonitoredInterface",
        MonitoredInterface,
    ),
    *_crud("asset-mappings", "assetmapping", "AssetMapping", AssetMapping),
    *_crud("metadata-contexts", "metadatacontext", "MetadataContext", MetadataContext),
    *_crud("metadata-entries", "metadataentry", "MetadataEntry", MetadataEntry),
    *_crud("servers", "opennmsserver", "OpenNMSServer", OpenNMSServer),
    path(
        "servers/<int:pk>/test/",
        views.OpenNMSServerTestView.as_view(),
        name="opennmsserver_test",
    ),
    path(
        "servers/test-ajax/",
        views.OpenNMSServerTestAjaxView.as_view(),
        name="opennmsserver_test_ajax",
    ),
    path(
        "servers/<int:pk>/scan/",
        views.OpenNMSServerScanView.as_view(),
        name="opennmsserver_scan",
    ),
    path(
        "servers/<int:pk>/unmirrored-requisitions/",
        views.UnmirroredRequisitionsView.as_view(),
        name="opennmsserver_unmirrored_requisitions",
    ),
    path(
        "servers/<int:pk>/import-requisition/",
        views.RequisitionImportView.as_view(),
        name="opennmsserver_import_requisition",
    ),
    *_crud(
        "monitoring-exclusions",
        "monitoringexclusion",
        "MonitoringExclusion",
        MonitoringExclusion,
    ),
    *_crud("discovery", "discoveryscan", "DiscoveryScan", DiscoveryScan),
    path(
        "discovery/<int:pk>/trigger/",
        views.DiscoveryScanTriggerView.as_view(),
        name="discoveryscan_trigger",
    ),
    path(
        "discovery/nodes/",
        views.DiscoveredNodeListView.as_view(),
        name="discoverednode_list",
    ),
    path(
        "discovery/nodes/delete/",
        views.DiscoveredNodeBulkDeleteView.as_view(),
        name="discoverednode_bulk_delete",
    ),
    path(
        "discovery/nodes/bulk-import/",
        views.DiscoveredNodeBulkImportView.as_view(),
        name="discoverednode_bulk_import",
    ),
    path(
        "discovery/nodes/<int:pk>/",
        views.DiscoveredNodeView.as_view(),
        name="discoverednode",
    ),
    path(
        "discovery/nodes/<int:pk>/delete/",
        views.DiscoveredNodeDeleteView.as_view(),
        name="discoverednode_delete",
    ),
    path(
        "discovery/nodes/<int:pk>/link/",
        views.DiscoveredNodeLinkView.as_view(),
        name="discoverednode_link",
    ),
    path(
        "discovery/nodes/<int:pk>/import/",
        views.DiscoveredNodeImportView.as_view(),
        name="discoverednode_import",
    ),
    path(
        "discovery/nodes/<int:pk>/confirm-ip/",
        views.DiscoveredNodeConfirmIPView.as_view(),
        name="discoverednode_confirm_ip",
    ),
    path(
        "discovery/nodes/<int:pk>/changelog/",
        ObjectChangeLogView.as_view(),
        name="discoverednode_changelog",
        kwargs={"model": DiscoveredNode},
    ),
    path(
        "node-links/create-cable/",
        views.NodeLinkCreateCableView.as_view(),
        name="node_link_create_cable",
    ),
    path(
        "sync/all/",
        views.MonitoringSyncAllView.as_view(),
        name="sync_all",
    ),
    path(
        "sync/foreign-source/",
        views.ForeignSourceSyncView.as_view(),
        name="foreign_source_sync",
    ),
)
