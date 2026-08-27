# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""UI views for plugin models (Requisition redesign)."""

import json
from copy import deepcopy

from dcim.models import Cable, Device, Interface, Site
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.generic import View
from netbox.views import generic
from utilities.rqworker import any_workers_for_queue
from utilities.views import GetReturnURLMixin, ViewTab, register_model_view
from virtualization.models import VirtualMachine

from . import filtersets, forms, import_node, tables
from .client import OpenNMSClient, OpenNMSError, parse_node_links
from .dryrun import dry_run
from .ip_reconcile import (
    ConfirmRejected,
    confirm_ip_interface,
    reconcile_node_interfaces,
    required_confirm_permissions,
)
from .jobs import (
    SyncForeignSourceJob,
    unknown_locations,
)
from .membership import (
    filter_errors,
    requisition_conflicts,
    resolve,
    resolve_all,
    target_server_for,
)
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
from .requisition_discovery import build_foreign_source_import, list_unmirrored
from .reverse_sync import (
    _cable_endpoints,
    fetch_node_data,
    plan_reverse_sync,
    preview_reverse_sync,
    run_reverse_sync,
)
from .scan import KIND_MODELS, scan_server, upsert_discovered_nodes
from .validation import validate_resolution

# Sync jobs are enqueued without an instance, so they run on the default RQ
# queue (get_queue_for_model(None) -> RQ_QUEUE_DEFAULT). FR-13 / AD-16.
SYNC_QUEUE = "default"
SYNC_PERM = "netbox_opennms.change_requisition"


def _no_worker_running():
    """True if no live RQ worker is servicing the Sync queue (best-effort, AD-16)."""
    try:
        return not any_workers_for_queue(SYNC_QUEUE)
    except Exception:
        return True


def _location_warnings(server, locations):
    """Best-effort warnings for chosen locations with no Minion (FR-5/AD-16)."""
    if server is None:
        return []
    try:
        with OpenNMSClient.from_server(server) as client:
            missing = unknown_locations(client, locations)
    except Exception:
        return []
    return [
        f"Location {location!r} is not a known OpenNMS monitoring location — "
        "no Minion will poll it (check the OpenNMS Minion/location setup)."
        for location in missing
    ]


def _enqueue_foreign_source(request, foreign_source, allow_empty=False):
    """Validate a Foreign Source's resolved intent and enqueue a sync (FR-8)."""
    resolution = resolve(foreign_source)
    result = validate_resolution(resolution, removing=allow_empty)
    for warning in result.warnings:
        messages.warning(request, warning)
    if result.errors:
        for error in result.errors:
            messages.error(request, error)
        return None

    locations = set()
    if resolution is not None:
        locations.add(resolution.requisition.location)
        locations.update(node.location for node in resolution.nodes)
    server = resolution.server if resolution is not None else None
    for warning in _location_warnings(server, locations):
        messages.warning(request, warning)

    return SyncForeignSourceJob.enqueue_sync(
        foreign_source, user=request.user, allow_empty=allow_empty
    )


# --- Requisition ------------------------------------------------------------


class RequisitionView(generic.ObjectView):
    queryset = Requisition.objects.prefetch_related("detectors", "policies")

    def get_extra_context(self, request, instance):
        # The post-save landing page doubles as the overlap warning surface (C2):
        # a save with an overlapping filter succeeds, and the conflict banner here
        # names the object + parties immediately. requisition_conflicts tests only
        # THIS requisition's members against the other filters — narrow queries,
        # no fleet-wide node resolution (review #12). Stale-value warnings and a
        # rejected filter are surfaced too — the post-save page must not be
        # silent about a filter that now matches nothing (review #7).
        resolution_warnings = []
        conflicts = requisition_conflicts(instance, resolution_warnings)
        return {
            "no_worker_warning": _no_worker_running(),
            "conflicts": conflicts,
            "filter_problems": filter_errors(instance),
            "resolution_warnings": resolution_warnings,
        }


class RequisitionListView(generic.ObjectListView):
    queryset = Requisition.objects.all()
    table = tables.RequisitionTable
    filterset = filtersets.RequisitionFilterSet


class RequisitionEditView(generic.ObjectEditView):
    queryset = Requisition.objects.all()
    form = forms.RequisitionForm


class RequisitionDeleteView(generic.ObjectDeleteView):
    queryset = Requisition.objects.all()


class RequisitionBulkDeleteView(generic.BulkDeleteView):
    queryset = Requisition.objects.all()
    table = tables.RequisitionTable


class RequisitionDuplicateView(PermissionRequiredMixin, View):
    """Deep-copy a Requisition (rules, services, filter) into a new named one (R4)."""

    permission_required = "netbox_opennms.add_requisition"

    def post(self, request, pk):
        source = get_object_or_404(Requisition, pk=pk)
        detectors = list(source.detectors.all())
        policies = list(source.policies.all())

        # Bound to Requisition.name max_length (100), leaving room for a "-N" tag.
        base = f"{source.name}-copy"[:100]
        name = base
        suffix = 2
        while Requisition.objects.filter(name=name).exists():
            tag = f"-{suffix}"
            name = f"{base[: 100 - len(tag)]}{tag}"
            suffix += 1

        clone = Requisition(
            name=name,
            description=source.description,
            object_types=source.object_types,
            filter_params=deepcopy(source.filter_params),
            scan_interval=source.scan_interval,
            default_interfaces=source.default_interfaces,
            services=list(source.services or []),
            location=source.location,
        )
        clone.save()
        for detector in detectors:
            MonitoringDetector.objects.create(
                requisition=clone,
                name=detector.name,
                preset=detector.preset,
                rule_class=detector.rule_class,
                parameters=deepcopy(detector.parameters),
            )
        for policy in policies:
            MonitoringPolicy.objects.create(
                requisition=clone,
                name=policy.name,
                preset=policy.preset,
                rule_class=policy.rule_class,
                parameters=deepcopy(policy.parameters),
            )
        messages.success(request, f"Duplicated {source.name} → {clone.name}.")
        # A verbatim filter copy overlaps the source on every CURRENT member —
        # warn only when that actually froze something (a zero-member source
        # duplicates harmlessly, review #4-fix).
        if requisition_conflicts(clone):
            messages.warning(
                request,
                f"{clone.name} has the same filter as {source.name}: every "
                "shared member is now a conflict and BOTH requisitions are "
                f"frozen. Edit {clone.name}'s filter (or delete it) to unfreeze.",
            )
        else:
            messages.info(
                request,
                f"{clone.name} shares {source.name}'s filter — edit it before "
                "the filters match the same objects, or they will conflict.",
            )
        return redirect(clone.get_absolute_url())


class RequisitionSyncView(PermissionRequiredMixin, View):
    """Enqueue a Sync for the Foreign Source this Requisition owns (AD-4/5)."""

    permission_required = SYNC_PERM

    def post(self, request, pk):
        requisition = get_object_or_404(Requisition, pk=pk)
        job = _enqueue_foreign_source(request, requisition.name)
        if job is not None:
            messages.success(
                request, f"Sync submitted for {requisition.name} (job #{job.pk})."
            )
        return redirect(requisition.get_absolute_url())


class RequisitionDryRunView(PermissionRequiredMixin, View):
    """Show the per-node diff of a Requisition against the live OpenNMS state (R7).

    Permission-gated (not merely login) because it issues live outbound calls to
    OpenNMS and returns the node/interface/service topology (review #7).
    """

    permission_required = "netbox_opennms.view_requisition"
    template_name = "netbox_opennms/dry_run.html"

    def get(self, request, pk):
        requisition = get_object_or_404(Requisition, pk=pk)
        error = None
        result = None
        try:
            result = dry_run(requisition.name)
        except OpenNMSError as exc:
            error = str(exc)
        return render(
            request,
            self.template_name,
            {"object": requisition, "dryrun": result, "error": error},
        )


# --- Monitoring Detector ----------------------------------------------------


class MonitoringDetectorView(generic.ObjectView):
    queryset = MonitoringDetector.objects.all()


class MonitoringDetectorListView(generic.ObjectListView):
    queryset = MonitoringDetector.objects.select_related("requisition")
    table = tables.MonitoringDetectorTable
    filterset = filtersets.MonitoringDetectorFilterSet


class MonitoringDetectorEditView(generic.ObjectEditView):
    queryset = MonitoringDetector.objects.all()
    form = forms.MonitoringDetectorForm


class MonitoringDetectorDeleteView(generic.ObjectDeleteView):
    queryset = MonitoringDetector.objects.all()


class MonitoringDetectorBulkDeleteView(generic.BulkDeleteView):
    queryset = MonitoringDetector.objects.all()
    table = tables.MonitoringDetectorTable


# --- Monitoring Policy ------------------------------------------------------


class MonitoringPolicyView(generic.ObjectView):
    queryset = MonitoringPolicy.objects.all()


class MonitoringPolicyListView(generic.ObjectListView):
    queryset = MonitoringPolicy.objects.select_related("requisition")
    table = tables.MonitoringPolicyTable
    filterset = filtersets.MonitoringPolicyFilterSet


class MonitoringPolicyEditView(generic.ObjectEditView):
    queryset = MonitoringPolicy.objects.all()
    form = forms.MonitoringPolicyForm


class MonitoringPolicyDeleteView(generic.ObjectDeleteView):
    queryset = MonitoringPolicy.objects.all()


class MonitoringPolicyBulkDeleteView(generic.BulkDeleteView):
    queryset = MonitoringPolicy.objects.all()
    table = tables.MonitoringPolicyTable


# --- Monitoring Override ----------------------------------------------------


class MonitoringOverrideView(generic.ObjectView):
    queryset = MonitoringOverride.objects.all()


class MonitoringOverrideListView(generic.ObjectListView):
    queryset = MonitoringOverride.objects.select_related(
        "assigned_object_type", "management_ip"
    )
    table = tables.MonitoringOverrideTable
    filterset = filtersets.MonitoringOverrideFilterSet


class MonitoringOverrideEditView(generic.ObjectEditView):
    queryset = MonitoringOverride.objects.all()
    form = forms.MonitoringOverrideForm


class MonitoringOverrideDeleteView(generic.ObjectDeleteView):
    queryset = MonitoringOverride.objects.all()


class MonitoringOverrideBulkDeleteView(generic.BulkDeleteView):
    queryset = MonitoringOverride.objects.all()
    table = tables.MonitoringOverrideTable


# --- Monitored Service ------------------------------------------------------


class MonitoredServiceView(generic.ObjectView):
    queryset = MonitoredService.objects.all()


class MonitoredServiceListView(generic.ObjectListView):
    queryset = MonitoredService.objects.select_related("override", "ip_address")
    table = tables.MonitoredServiceTable
    filterset = filtersets.MonitoredServiceFilterSet


class MonitoredServiceEditView(generic.ObjectEditView):
    queryset = MonitoredService.objects.all()
    form = forms.MonitoredServiceForm


class MonitoredServiceDeleteView(generic.ObjectDeleteView):
    queryset = MonitoredService.objects.all()


class MonitoredServiceBulkDeleteView(generic.BulkDeleteView):
    queryset = MonitoredService.objects.all()
    table = tables.MonitoredServiceTable


# --- Monitored Interface ----------------------------------------------------


class MonitoredInterfaceView(generic.ObjectView):
    queryset = MonitoredInterface.objects.all()


class MonitoredInterfaceListView(generic.ObjectListView):
    queryset = MonitoredInterface.objects.select_related("override", "ip_address")
    table = tables.MonitoredInterfaceTable
    filterset = filtersets.MonitoredInterfaceFilterSet


class MonitoredInterfaceEditView(generic.ObjectEditView):
    queryset = MonitoredInterface.objects.all()
    form = forms.MonitoredInterfaceForm


class MonitoredInterfaceDeleteView(generic.ObjectDeleteView):
    queryset = MonitoredInterface.objects.all()


class MonitoredInterfaceBulkDeleteView(generic.BulkDeleteView):
    queryset = MonitoredInterface.objects.all()
    table = tables.MonitoredInterfaceTable


# --- Asset Mapping ----------------------------------------------------------


class AssetMappingView(generic.ObjectView):
    queryset = AssetMapping.objects.all()


class AssetMappingListView(generic.ObjectListView):
    queryset = AssetMapping.objects.select_related("requisition")
    table = tables.AssetMappingTable
    filterset = filtersets.AssetMappingFilterSet


class AssetMappingEditView(generic.ObjectEditView):
    queryset = AssetMapping.objects.all()
    form = forms.AssetMappingForm


class AssetMappingDeleteView(generic.ObjectDeleteView):
    queryset = AssetMapping.objects.all()


class AssetMappingBulkDeleteView(generic.BulkDeleteView):
    queryset = AssetMapping.objects.all()
    table = tables.AssetMappingTable


# --- Metadata Entry ---------------------------------------------------------


class MetadataEntryView(generic.ObjectView):
    queryset = MetadataEntry.objects.all()


class MetadataEntryListView(generic.ObjectListView):
    queryset = MetadataEntry.objects.select_related("requisition")
    table = tables.MetadataEntryTable
    filterset = filtersets.MetadataEntryFilterSet


class MetadataEntryEditView(generic.ObjectEditView):
    queryset = MetadataEntry.objects.all()
    form = forms.MetadataEntryForm


class MetadataEntryDeleteView(generic.ObjectDeleteView):
    queryset = MetadataEntry.objects.all()


class MetadataEntryBulkDeleteView(generic.BulkDeleteView):
    queryset = MetadataEntry.objects.all()
    table = tables.MetadataEntryTable


# --- OpenNMS Server ----------------------------------------------------------


class OpenNMSServerView(generic.ObjectView):
    queryset = OpenNMSServer.objects.all()


class OpenNMSServerListView(generic.ObjectListView):
    queryset = OpenNMSServer.objects.all()
    table = tables.OpenNMSServerTable
    filterset = filtersets.OpenNMSServerFilterSet


class OpenNMSServerEditView(generic.ObjectEditView):
    queryset = OpenNMSServer.objects.all()
    form = forms.OpenNMSServerForm


class OpenNMSServerDeleteView(generic.ObjectDeleteView):
    queryset = OpenNMSServer.objects.all()


class OpenNMSServerBulkDeleteView(generic.BulkDeleteView):
    queryset = OpenNMSServer.objects.all()
    table = tables.OpenNMSServerTable


# --- Monitoring Exclusion ----------------------------------------------------


class MonitoringExclusionView(generic.ObjectView):
    queryset = MonitoringExclusion.objects.all()


class MonitoringExclusionListView(generic.ObjectListView):
    queryset = MonitoringExclusion.objects.all()
    table = tables.MonitoringExclusionTable
    filterset = filtersets.MonitoringExclusionFilterSet


class MonitoringExclusionEditView(generic.ObjectEditView):
    queryset = MonitoringExclusion.objects.all()
    form = forms.MonitoringExclusionForm


class MonitoringExclusionDeleteView(generic.ObjectDeleteView):
    queryset = MonitoringExclusion.objects.all()


class MonitoringExclusionBulkDeleteView(generic.BulkDeleteView):
    queryset = MonitoringExclusion.objects.all()
    table = tables.MonitoringExclusionTable


# --- Discovery Scan (issue #25) -----------------------------------------------


class DiscoveryScanView(generic.ObjectView):
    queryset = DiscoveryScan.objects.all()


class DiscoveryScanListView(generic.ObjectListView):
    queryset = DiscoveryScan.objects.all()
    table = tables.DiscoveryScanTable
    filterset = filtersets.DiscoveryScanFilterSet


class DiscoveryScanEditView(generic.ObjectEditView):
    queryset = DiscoveryScan.objects.all()
    form = forms.DiscoveryScanForm


class DiscoveryScanDeleteView(generic.ObjectDeleteView):
    queryset = DiscoveryScan.objects.all()


class DiscoveryScanBulkDeleteView(generic.BulkDeleteView):
    queryset = DiscoveryScan.objects.all()
    table = tables.DiscoveryScanTable


class DiscoveryScanTriggerView(GetReturnURLMixin, PermissionRequiredMixin, View):
    """Fire one Discovery Scan's ``POST /api/v2/discovery`` request (ADR 0006).

    Fire-and-forget: OpenNMS accepting the request is the full extent of what
    this view can confirm (``OpenNMSClient.run_discovery``'s docstring) —
    inferring completion from the resulting Discovered Nodes is a later Job
    (issue #27), out of scope here.
    """

    permission_required = "netbox_opennms.change_discoveryscan"
    default_return_url = "plugins:netbox_opennms:discoveryscan_list"

    def post(self, request, pk):
        scan = get_object_or_404(DiscoveryScan, pk=pk)
        return_url = request.META.get("HTTP_REFERER") or scan.get_absolute_url()
        try:
            with OpenNMSClient.from_server(scan.server) as client:
                client.run_discovery(
                    foreign_source=scan.foreign_source,
                    location=scan.monitoring_location,
                    ip_range_begin=scan.ip_range_begin,
                    ip_range_end=scan.ip_range_end,
                    retries=scan.retries,
                    timeout=scan.timeout,
                )
        except OpenNMSError as exc:
            messages.error(request, f"Discovery scan {scan} failed: {exc}")
            return redirect(return_url)
        scan.mark_triggered()
        messages.success(request, f"Triggered Discovery scan {scan}.")
        return redirect(return_url)


class DiscoveryScanServerLocationsAjaxView(PermissionRequiredMixin, View):
    """JSON location list for the add/edit Discovery Scan form
    (``discoveryscan_server_locations.js``).

    Unlike ``OpenNMSServerTestAjaxView``, this targets an already-saved
    Server (picked from the Discovery Scan form's own Server field) rather
    than posted, not-yet-saved credentials — so it's a plain GET keyed by
    ``server_id``.
    """

    permission_required = "netbox_opennms.add_discoveryscan"

    def get(self, request):
        server_id = request.GET.get("server_id")
        server = get_object_or_404(OpenNMSServer, pk=server_id)
        try:
            with OpenNMSClient.from_server(server) as client:
                locations = sorted(client.list_locations())
        except OpenNMSError as exc:
            return JsonResponse({"ok": False, "message": str(exc)})
        return JsonResponse({"ok": True, "locations": locations})


# --- Discovery (issue #7) ----------------------------------------------------


def _fetch_ip_interfaces_and_services(client, opennms_node_id):
    """IP interfaces plus per-interface services for one OpenNMS node.

    Shared by the live-fetch paths in ``DiscoveredNodeView``,
    ``DiscoveredNodeImportView``, and ``DiscoveredNodeBulkImportView`` — all
    three walk the same ``list_ip_interfaces`` -> per-IP ``list_services``
    shape.
    """
    ip_interfaces = client.list_ip_interfaces(opennms_node_id)
    services_by_ip = {}
    for iface in ip_interfaces:
        ip = iface.get("ipAddress") if isinstance(iface, dict) else None
        if ip:
            services_by_ip[ip] = client.list_services(opennms_node_id, ip)
    return ip_interfaces, services_by_ip


class DiscoveredNodeView(generic.ObjectView):
    queryset = DiscoveredNode.objects.all()

    def get_extra_context(self, request, instance):
        live_fetch_error = None
        try:
            with OpenNMSClient.from_server(instance.server) as client:
                node_detail = client.get_node(instance.opennms_node_id) or {}
                ip_interfaces, services_by_ip = _fetch_ip_interfaces_and_services(
                    client, instance.opennms_node_id
                )
        except OpenNMSError as exc:
            live_fetch_error = str(exc)
            node_detail = instance.node_detail
            ip_interfaces = instance.ip_interfaces
            services_by_ip = instance.services_by_ip
        parsed_interfaces, parsed_services = import_node.parse_discovery_payload(
            ip_interfaces, services_by_ip
        )
        service_names_by_ip = {}
        for service in parsed_services:
            service_names_by_ip.setdefault(service.ip_address, []).append(
                service.name
            )
        live_interface_rows = [
            {
                "interface": iface,
                "services": service_names_by_ip.get(iface.ip_address, []),
            }
            for iface in parsed_interfaces
        ]
        return {
            "interface_verdicts": reconcile_node_interfaces(instance),
            "live_node_detail": node_detail,
            "live_interface_rows": live_interface_rows,
            "live_fetch_error": live_fetch_error,
        }


class DiscoveredNodeListView(generic.ObjectListView):
    queryset = DiscoveredNode.objects.all()
    table = tables.DiscoveredNodeTable
    filterset = filtersets.DiscoveredNodeFilterSet
    filterset_form = forms.DiscoveredNodeFilterForm
    template_name = "netbox_opennms/discoverednode_list.html"


class DiscoveredNodeDeleteView(generic.ObjectDeleteView):
    queryset = DiscoveredNode.objects.all()


class DiscoveredNodeBulkDeleteView(generic.BulkDeleteView):
    queryset = DiscoveredNode.objects.all()
    table = tables.DiscoveredNodeTable


class OpenNMSServerScanView(GetReturnURLMixin, PermissionRequiredMixin, View):
    """Run a Discovery scan against a Server and upsert its ``DiscoveredNode`` rows.

    Re-running a scan is idempotent: matches are upserted keyed on
    ``(server, opennms_node_id)`` (issue #7's unique constraint), and any row
    for a node no longer present on the server is deleted.
    """

    permission_required = "netbox_opennms.add_discoverednode"
    default_return_url = "plugins:netbox_opennms:opennmsserver_list"

    def post(self, request, pk):
        server = get_object_or_404(OpenNMSServer, pk=pk)
        return_url = request.META.get("HTTP_REFERER") or server.get_absolute_url()
        try:
            matches = scan_server(server)
        except OpenNMSError as exc:
            messages.error(
                request, f"OpenNMS Discovery scan of {server.name!r} failed: {exc}"
            )
            return redirect(return_url)

        upsert_discovered_nodes(server, matches)
        messages.success(
            request, f"Discovery scan of {server.name!r} found {len(matches)} node(s)."
        )
        return redirect(return_url)


class DiscoveredNodeLinkView(GetReturnURLMixin, PermissionRequiredMixin, View):
    """Manually link (or correct) a Discovery row's matched NetBox object (issue #8).

    Writes through ``DiscoveredNode.link_to``, which also marks the row
    ``resolution="linked"`` so a later re-scan (``OpenNMSServerScanView``)
    leaves the decision alone instead of recomputing it from the node's
    Foreign ID.
    """

    permission_required = "netbox_opennms.change_discoverednode"
    default_return_url = "plugins:netbox_opennms:discoverednode_list"
    template_name = "netbox_opennms/discoverednode_link.html"

    def get(self, request, pk):
        node = get_object_or_404(DiscoveredNode, pk=pk)
        initial = {}
        if node.matched_object is not None:
            field = (
                "device"
                if isinstance(node.matched_object, KIND_MODELS["device"])
                else "virtual_machine"
            )
            initial[field] = node.matched_object
        form = forms.DiscoveredNodeLinkForm(initial=initial)
        return render(request, self.template_name, {"object": node, "form": form})

    def post(self, request, pk):
        node = get_object_or_404(DiscoveredNode, pk=pk)
        form = forms.DiscoveredNodeLinkForm(request.POST)
        if not form.is_valid():
            return render(request, self.template_name, {"object": node, "form": form})
        node.link_to(form.target)
        messages.success(request, f"Linked {node} to {form.target}.")
        return redirect(self.get_return_url(request, node))


class DiscoveredNodeImportView(GetReturnURLMixin, PermissionRequiredMixin, View):
    """Create a new Device/VM from a red Discovery row's OpenNMS data (issue #9).

    GET renders a reviewable proposal (tenant/site/role/manufacturer/platform
    guessed from the node's OpenNMS asset record and categories; IP interfaces
    and services shown read-only) so nothing is applied without the operator
    seeing it first. POST commits it through ``import_node.import_node`` — the
    only write path — which re-checks the ADR-0001 Server Conflict invariant
    against the newly-created object and, on success, links ``node`` to it the
    same way a manual link (#8) is.
    """

    default_return_url = "plugins:netbox_opennms:discoverednode_list"
    template_name = "netbox_opennms/discoverednode_import.html"

    def has_permission(self):
        # The specific dcim.add_device / virtualization.add_virtualmachine
        # permission is checked in post() once the operator's chosen kind is
        # known; this only gates whether the import action exists at all.
        user = self.request.user
        return user.has_perm("dcim.add_device") or user.has_perm(
            "virtualization.add_virtualmachine"
        )

    def _fetch(self, node):
        """OpenNMS data for *node*, built into an import proposal.

        A walked Discovery Scan row (issue #28) reads its own persisted
        snapshot — the OpenNMS-side node may already be gone (ADR 0007). Any
        other row (never walked) falls back to a live fetch, as before.
        """
        overrides = import_node.asset_field_overrides()
        if node.walked_at is not None:
            proposal = import_node.build_proposal(
                node,
                node.node_detail,
                node.ip_interfaces,
                node.services_by_ip,
                overrides,
                Site,
            )
            return proposal, None
        try:
            with OpenNMSClient.from_server(node.server) as client:
                detail = client.get_node(node.opennms_node_id) or {}
                ip_interfaces, services_by_ip = _fetch_ip_interfaces_and_services(
                    client, node.opennms_node_id
                )
        except OpenNMSError as exc:
            return None, str(exc)
        proposal = import_node.build_proposal(
            node, detail, ip_interfaces, services_by_ip, overrides, Site
        )
        return proposal, None

    def _initial(self, node, proposal):
        initial = {"kind": "device", "name": node.label, "location": node.location}
        if proposal is not None:
            initial.update(
                {
                    "tenant": proposal.tenant.value,
                    "site": proposal.site.value,
                    "role": proposal.role.value,
                    "manufacturer": proposal.manufacturer.value,
                    "platform": proposal.platform.value,
                }
            )
        return initial

    def get(self, request, pk):
        node = get_object_or_404(DiscoveredNode, pk=pk)
        proposal, error = self._fetch(node)
        form = forms.DiscoveredNodeImportForm(initial=self._initial(node, proposal))
        return render(
            request,
            self.template_name,
            {"object": node, "form": form, "proposal": proposal, "error": error},
        )

    def post(self, request, pk):
        node = get_object_or_404(DiscoveredNode, pk=pk)
        proposal, error = self._fetch(node)
        form = forms.DiscoveredNodeImportForm(request.POST)
        if error:
            messages.error(request, f"Could not reach OpenNMS: {error}")
            return render(
                request,
                self.template_name,
                {"object": node, "form": form, "proposal": proposal, "error": error},
            )
        if not form.is_valid():
            return render(
                request,
                self.template_name,
                {"object": node, "form": form, "proposal": proposal},
            )
        kind = form.cleaned_data["kind"]
        perm = (
            "dcim.add_device"
            if kind == "device"
            else "virtualization.add_virtualmachine"
        )
        if not request.user.has_perm(perm):
            raise PermissionDenied(f"You do not have permission to create a {kind}.")
        try:
            target = import_node.import_node(node, kind, form.cleaned_data, proposal)
        except import_node.ImportRejected as exc:
            messages.error(request, str(exc))
            return render(
                request,
                self.template_name,
                {"object": node, "form": form, "proposal": proposal},
            )
        messages.success(
            request, f"Imported {target} from OpenNMS node {node.label!r}."
        )
        return redirect(self.get_return_url(request, node))


class DiscoveredNodeConfirmIPView(GetReturnURLMixin, PermissionRequiredMixin, View):
    """Confirm one red IP interface into NetBox (issue #31).

    POST-only: the review already happened on the Discovered Node detail
    page, where each row's proposed Prefix/IPRange is shown (issue #30) --
    this re-derives the current verdict itself and applies exactly that
    proposal, never anything from the request body beyond which IP was
    picked. Independent of Device/VM conversion (#9): available whether or
    not the node has a match, so IPAM gaps can be fixed even for nodes
    nobody intends to onboard as a monitored Device.
    """

    default_return_url = "plugins:netbox_opennms:discoverednode_list"

    def has_permission(self):
        # ipam.add_ipaddress is always needed; the rest (Prefix vs IPRange,
        # plus an Interface/VMInterface permission when the node has a
        # matched Device/VM) is checked in post() via the same
        # required_confirm_permissions() confirm_ip_interface itself derives
        # from, once this IP's current verdict is known.
        return self.request.user.has_perm("ipam.add_ipaddress")

    def post(self, request, pk):
        node = get_object_or_404(DiscoveredNode, pk=pk)
        ip_address = request.POST.get("ip_address", "")
        missing = [
            perm
            for perm in required_confirm_permissions(node, ip_address)
            if not request.user.has_perm(perm)
        ]
        if missing:
            raise PermissionDenied(
                "You do not have permission to confirm this IP: missing "
                + ", ".join(missing)
            )
        try:
            confirm_ip_interface(node, ip_address)
        except ConfirmRejected as exc:
            messages.error(request, str(exc))
            return redirect(self.get_return_url(request, node))
        messages.success(request, f"Confirmed {ip_address} into NetBox.")
        return redirect(self.get_return_url(request, node))


class DiscoveredNodeBulkImportView(GetReturnURLMixin, PermissionRequiredMixin, View):
    """Import several red Discovery rows at once (issue #10).

    Deliberately never calls ``import_node.build_proposal`` (or anything that
    guesses tenant/site/role/manufacturer/platform from OpenNMS asset data) —
    only ``import_node.parse_discovery_payload``, which carries IP interfaces
    and services through verbatim. One bad automatic guess must not be
    multipliable across a whole batch of new Devices, so every row in a batch
    gets exactly the operator's one explicit field selection; nothing here is
    ever pre-filled from a per-row detection.
    """

    default_return_url = "plugins:netbox_opennms:discoverednode_list"
    template_name = "netbox_opennms/discoverednode_bulk_import.html"

    def has_permission(self):
        # As with DiscoveredNodeImportView, the specific dcim.add_device /
        # virtualization.add_virtualmachine permission is checked in post()
        # once the operator's chosen kind is known.
        user = self.request.user
        return user.has_perm("dcim.add_device") or user.has_perm(
            "virtualization.add_virtualmachine"
        )

    @staticmethod
    def _candidates():
        return DiscoveredNode.objects.filter(
            verdict="red", matched_object_id__isnull=True
        ).order_by("label")

    def get(self, request):
        form = forms.DiscoveredNodeBulkImportForm()
        return render(
            request,
            self.template_name,
            {"form": form, "candidates": self._candidates()},
        )

    def post(self, request):
        form = forms.DiscoveredNodeBulkImportForm(request.POST)
        selected_pks = request.POST.getlist("nodes")
        if not selected_pks:
            form.add_error(None, "Select at least one Discovery row to import.")
        if not form.is_valid():
            return render(
                request,
                self.template_name,
                {"form": form, "candidates": self._candidates()},
            )
        kind = form.cleaned_data["kind"]
        perm = (
            "dcim.add_device"
            if kind == "device"
            else "virtualization.add_virtualmachine"
        )
        if not request.user.has_perm(perm):
            raise PermissionDenied(f"You do not have permission to create a {kind}.")

        nodes = DiscoveredNode.objects.filter(
            pk__in=selected_pks, verdict="red", matched_object_id__isnull=True
        )
        created = []
        errors = []
        for node in nodes:
            row_data = dict(form.cleaned_data)
            row_data["name"] = node.label
            try:
                with OpenNMSClient.from_server(node.server) as client:
                    ip_interfaces, services_by_ip = (
                        _fetch_ip_interfaces_and_services(
                            client, node.opennms_node_id
                        )
                    )
            except OpenNMSError as exc:
                errors.append(f"{node.label}: could not reach OpenNMS ({exc}).")
                continue
            interfaces, services = import_node.parse_discovery_payload(
                ip_interfaces, services_by_ip
            )
            proposal = import_node.ImportProposal(
                label=node.label, interfaces=interfaces, services=services
            )
            try:
                target = import_node.import_node(node, kind, row_data, proposal)
            except import_node.ImportRejected as exc:
                errors.append(str(exc))
                continue
            created.append(target)

        if created:
            names = ", ".join(str(target) for target in created)
            messages.success(request, f"Imported {len(created)} node(s): {names}.")
        for error in errors:
            messages.error(request, error)
        return redirect(self.get_return_url(request))


class UnmirroredRequisitionsView(PermissionRequiredMixin, View):
    """Foreign Sources on a Server with no matching NetBox Requisition (issue #11).

    Read-only — computed live on each request (mirrors ``RequisitionDryRunView``)
    rather than persisted like ``DiscoveredNode`` (#7), since there's no
    per-row state (verdict, resolution) to track: a name is either mirrored
    or it isn't. Each unmirrored row's Import action is ``RequisitionImportView``
    (issue #22); once imported, the name gains a matching Requisition and drops
    out of this list on the next load.
    """

    permission_required = "netbox_opennms.view_opennmsserver"
    template_name = "netbox_opennms/unmirrored_requisitions.html"

    def get(self, request, pk):
        server = get_object_or_404(OpenNMSServer, pk=pk)
        error = None
        names = None
        try:
            names = list_unmirrored(server)
        except OpenNMSError as exc:
            error = str(exc)
        return render(
            request,
            self.template_name,
            {"object": server, "names": names, "error": error},
        )


class RequisitionImportView(PermissionRequiredMixin, View):
    """Create a Requisition shell from an unmirrored Foreign Source (issue #22).

    Copies name/scan-interval/detectors/policies straight off OpenNMS's own
    Foreign Source definition, mirroring ``RequisitionDuplicateView``'s
    create-then-loop-create-rows pattern — but ``filter_params`` is left empty
    (a live filter must be an explicit admin decision, not guessed from
    OpenNMS), so the new Requisition has zero members until one is defined.
    """

    permission_required = "netbox_opennms.add_requisition"

    def post(self, request, pk):
        server = get_object_or_404(OpenNMSServer, pk=pk)
        return_url = request.META.get("HTTP_REFERER") or reverse(
            "plugins:netbox_opennms:opennmsserver_unmirrored_requisitions",
            args=[server.pk],
        )
        foreign_source = request.POST.get("foreign_source", "").strip()
        if not foreign_source:
            messages.error(request, "No Foreign Source given.")
            return redirect(return_url)
        # Re-checked here (not just trusted from the list view that only shows
        # unmirrored names) — the POST body is user-controlled and a name may
        # have been imported by someone else between page load and this POST.
        if Requisition.objects.filter(name=foreign_source).exists():
            messages.error(
                request,
                f"A Requisition named {foreign_source!r} already exists — "
                "import skipped.",
            )
            return redirect(return_url)

        try:
            with OpenNMSClient.from_server(server) as client:
                definition = client.get_foreign_source(foreign_source)
        except OpenNMSError as exc:
            messages.error(request, f"Could not reach OpenNMS: {exc}")
            return redirect(return_url)
        if definition is None:
            messages.error(
                request,
                f"Foreign Source {foreign_source!r} no longer exists on "
                f"{server.name!r}.",
            )
            return redirect(return_url)

        imported = build_foreign_source_import(definition)
        requisition = Requisition(
            name=foreign_source, scan_interval=imported.scan_interval
        )
        requisition.save()
        for rule in imported.detectors:
            MonitoringDetector.objects.create(
                requisition=requisition,
                name=rule.name,
                rule_class=rule.rule_class,
                parameters=rule.parameters,
            )
        for rule in imported.policies:
            MonitoringPolicy.objects.create(
                requisition=requisition,
                name=rule.name,
                rule_class=rule.rule_class,
                parameters=rule.parameters,
            )
        messages.success(
            request, f"Imported {foreign_source!r} as Requisition {requisition.name}."
        )
        messages.warning(
            request,
            f"{requisition.name} has no filter or Scope yet — it has zero "
            "members and will sync nothing until you define one.",
        )
        return redirect(requisition.get_absolute_url())


# --- Sync actions -----------------------------------------------------------


class ForeignSourceSyncView(GetReturnURLMixin, PermissionRequiredMixin, View):
    """Enqueue a Sync (or Remove) for one Foreign Source named in the POST."""

    permission_required = SYNC_PERM
    default_return_url = "plugins:netbox_opennms:sync_preview"

    def post(self, request):
        foreign_source = request.POST.get("foreign_source", "").strip()
        allow_empty = bool(request.POST.get("remove"))
        return_url = self.get_return_url(request)
        if not foreign_source:
            messages.error(request, "No Foreign Source given.")
            return redirect(return_url)

        job = _enqueue_foreign_source(request, foreign_source, allow_empty=allow_empty)
        if job is not None:
            verb = "Remove" if allow_empty else "Sync"
            messages.success(
                request,
                f"{verb} submitted for Foreign Source {foreign_source} "
                f"(job #{job.pk}).",
            )
        return redirect(return_url)


class MonitoringSyncAllView(PermissionRequiredMixin, View):
    """Enqueue a Sync for every syncable Requisition (FR-9).

    Fans out over one ``resolve_all()`` pass: requisitions that resolve to nodes
    are enqueued; **frozen** ones (conflicts) are skipped with a warning instead
    of being enqueued into a guaranteed-failed Job (review #2) — the freeze is
    enforced here just as it is on the per-requisition Sync path.
    """

    permission_required = SYNC_PERM

    def post(self, request):
        # The CANONICAL gate — validate_resolution — decides what is blocked,
        # exactly as the per-requisition Sync path does: conflicts (frozen),
        # rejected filters, and invalid locations are all skipped with a warning
        # here instead of becoming guaranteed-failed jobs or silent skips
        # (reviews #2/#8 applied symmetrically).
        submitted, blocked = 0, 0
        for resolution in resolve_all():
            if validate_resolution(resolution).errors:
                blocked += 1
                continue
            if not resolution.nodes:
                continue
            SyncForeignSourceJob.enqueue_sync(
                resolution.foreign_source, user=request.user
            )
            submitted += 1
        if blocked:
            messages.warning(
                request,
                f"Skipped {blocked} requisition(s) blocked by validation errors "
                "(frozen by a conflict, a rejected filter, or an invalid "
                "location) — open their pages to resolve.",
            )
        if submitted:
            messages.success(
                request, f"Submitted {submitted} Foreign Source sync(s)."
            )
        else:
            messages.info(request, "Nothing to sync.")
        return redirect("plugins:netbox_opennms:sync_preview")


class SyncPreviewView(LoginRequiredMixin, View):
    """The preview-and-sync overview: every Requisition + its resolved members.

    Lists every Requisition with its node count, any resolution warnings
    (rejected filters, member skips), and its blocking conflicts (a frozen
    Requisition cannot sync until the overlap is resolved — C1), so the operator
    sees what will go before pressing Sync. The per-node dry-run diff against
    OpenNMS is a per-Requisition action (RequisitionDryRunView).
    """

    template_name = "netbox_opennms/sync_preview.html"

    def get(self, request):
        rows = []
        for resolution in resolve_all():
            rows.append(
                {
                    "foreign_source": resolution.foreign_source,
                    "requisition": resolution.requisition,
                    "node_count": len(resolution.nodes),
                    # Rejected-filter errors join the warnings badge so a broken
                    # filter stays visible on the preview.
                    "warnings": [*resolution.rejected, *resolution.warnings],
                    "conflicts": resolution.conflicts,
                }
            )
        return render(
            request,
            self.template_name,
            {"rows": rows, "no_worker_warning": _no_worker_running()},
        )


class OpenNMSServerTestView(PermissionRequiredMixin, View):
    """Test an already-saved Server's connection from the list row or detail page.

    Persists the outcome onto the Server row (``record_check_result``) so it's
    visible as a badge everywhere and feeds ``SyncForeignSourceJob``'s health
    guard — unlike the old standalone "Connect OpenNMS" page, this result is
    not a one-shot flash message.
    """

    permission_required = "netbox_opennms.change_opennmsserver"

    def post(self, request, pk):
        server = get_object_or_404(OpenNMSServer, pk=pk)
        try:
            with OpenNMSClient.from_server(server) as client:
                client.test_connection()
                try:
                    locations = sorted(client.list_locations())
                except OpenNMSError:
                    locations = None
        except OpenNMSError as exc:
            server.record_check_result(ok=False, message=str(exc))
            messages.error(
                request, f"OpenNMS connection to {server.name!r} failed: {exc}"
            )
        else:
            server.record_check_result(ok=True, locations=locations)
            messages.success(
                request,
                f"OpenNMS connection to {server.name!r} OK — reachable and "
                "credentials accepted.",
            )
        return_url = request.META.get("HTTP_REFERER") or server.get_absolute_url()
        return redirect(return_url)


class OpenNMSServerTestAjaxView(PermissionRequiredMixin, View):
    """JSON connection test for the add/edit Server form (server_test_connection.js).

    Unlike ``OpenNMSServerTestView``, this tests the *posted* url/username/
    password/headers — not a saved row — so it works before a new Server has
    been saved. When ``server_id`` is present (editing an existing Server) the
    outcome is also persisted via ``record_check_result``, same as the
    synchronous test, so testing from the edit form keeps the badge current
    too. On success, returns the live location list
    (``OpenNMSClient.list_locations()``) for the ``default_location`` dropdown.
    """

    permission_required = "netbox_opennms.change_opennmsserver"

    def post(self, request):
        server_id = request.POST.get("server_id")
        try:
            headers = json.loads(request.POST.get("headers") or "{}")
            if not isinstance(headers, dict):
                raise ValueError("headers must be a JSON object")
        except ValueError as exc:
            return JsonResponse({"ok": False, "message": f"Invalid headers: {exc}"})

        client = OpenNMSClient(
            base_url=request.POST.get("url", ""),
            username=request.POST.get("username", ""),
            password=request.POST.get("password", ""),
        )
        if headers:
            client._session.headers.update(headers)

        try:
            with client:
                client.test_connection()
                locations = sorted(client.list_locations())
        except OpenNMSError as exc:
            if server_id:
                get_object_or_404(OpenNMSServer, pk=server_id).record_check_result(
                    ok=False, message=str(exc)
                )
            return JsonResponse({"ok": False, "message": str(exc)})

        if server_id:
            get_object_or_404(OpenNMSServer, pk=server_id).record_check_result(
                ok=True, locations=locations
            )
        return JsonResponse({"ok": True, "locations": locations})


# --- Requisition Nodes tab (issue #21, narrow scope) --------------------------


def _requisition_nodes_badge(instance):
    return DiscoveredNode.objects.filter(foreign_source=instance.name).count() or None


@register_model_view(Requisition, name="opennms_nodes", path="nodes")
class RequisitionNodesView(generic.ObjectView):
    """The OpenNMS nodes belonging to this Requisition's Foreign Source, and the
    NetBox Device/VM (if any) each currently maps to.

    ``foreign_source`` is the join key: it's set to the owning Requisition's
    ``name`` (the Foreign Source name) whenever a node is upserted (manual link,
    import, or a Discovery Scan's poll). A Requisition's ``name`` is unique
    (AD-1), but the same Foreign Source name could in principle exist as stale
    ``DiscoveredNode`` rows from a different Server (e.g. after a Requisition
    is re-pointed at a new target Server) — so rows are additionally scoped to
    ``target_server_for(instance)`` (issue #21) whenever that resolves.
    """

    queryset = Requisition.objects.all()
    tab = ViewTab(label="Nodes", badge=_requisition_nodes_badge)
    template_name = "netbox_opennms/requisition_nodes_tab.html"

    def get_extra_context(self, request, instance):
        target_server = target_server_for(instance)
        nodes = DiscoveredNode.objects.filter(foreign_source=instance.name)
        if target_server is not None:
            nodes = nodes.filter(server=target_server)
        nodes = nodes.order_by("label")
        return {"nodes": nodes, "target_server": target_server}


# --- Node Links tab (issue #15) ----------------------------------------------


def _node_links_payload(instance):
    """This Device/VM's discovered-links payload, cached on *instance*.

    ``{% model_view_tabs object %}`` (``generic/object.html``) evaluates every
    registered tab's ``badge`` callable on *every* page of the model, not just
    this tab's own page — so without caching, opening this tab would fetch the
    same OpenNMS data twice in one request (once for its own tab-bar badge,
    once for ``get_extra_context``). Caching on ``instance`` works because
    ``get_extra_context`` always runs, for the same instance, before the
    template (and its tab bar) renders.
    """
    if hasattr(instance, "_opennms_node_links_payload"):
        return instance._opennms_node_links_payload
    payload = None
    node = DiscoveredNode.for_object(instance)
    if node is not None:
        try:
            with OpenNMSClient.from_server(node.server) as client:
                payload = client.get_node_links(node.opennms_node_id)
        except OpenNMSError:
            payload = None
    instance._opennms_node_links_payload = payload
    return payload


def _node_links_badge(instance):
    return len(parse_node_links(_node_links_payload(instance))) or None


# --- Discovered link → NetBox cable resolution (issue #16) -------------------
# _remote_discovered_node / _cable_endpoints moved to reverse_sync.py (issue
# #23), which needs the same join for its bulk engine — imported below so
# this tab and that engine share exactly one resolution, not two.


class _NodeLinksView(generic.ObjectView):
    """OpenNMS-discovered neighbor links for a Device/VirtualMachine (#15).

    Reachable only via the provenance mapping a manual link (#8) or import (#9)
    establishes (``DiscoveredNode.for_object``). The tab itself is hidden
    unless that mapping exists *and* OpenNMS currently reports at least one
    link for the node — a falsy badge plus ``hide_if_empty`` hides it, rather
    than showing an empty page for every other Device/VM.
    """

    tab = ViewTab(label="Node Links", badge=_node_links_badge, hide_if_empty=True)
    template_name = "netbox_opennms/node_links_tab.html"

    def get_extra_context(self, request, instance):
        discovered_node = DiscoveredNode.for_object(instance)
        links = parse_node_links(_node_links_payload(instance))
        rows = []
        for link in links:
            endpoints, reason = (
                (None, "No OpenNMS Discovery match for this object.")
                if discovered_node is None
                else _cable_endpoints(instance, discovered_node, link)
            )
            rows.append(
                {
                    "link": link,
                    "local_interface": endpoints[0] if endpoints else None,
                    "remote_interface": endpoints[1] if endpoints else None,
                    "blocked_reason": reason,
                }
            )
        return {
            "discovered_node": discovered_node,
            "links": links,
            "link_rows": rows,
        }


class NodeLinkCreateCableView(PermissionRequiredMixin, View):
    """Turn a discovered Node Link into a real NetBox cable (issue #16).

    The Node Links tab already resolved and displayed which two interfaces
    this connects (``_cable_endpoints``); POST re-validates them (not already
    cabled, still a clean pair) rather than re-deriving the link from a fresh
    OpenNMS call — the operator is confirming the exact pairing they were shown.
    """

    permission_required = "dcim.add_cable"

    def post(self, request):
        try:
            local_interface = get_object_or_404(
                Interface, pk=request.POST.get("local_interface")
            )
            remote_interface = get_object_or_404(
                Interface, pk=request.POST.get("remote_interface")
            )
        except (TypeError, ValueError) as exc:
            raise Http404 from exc
        return_url = reverse(
            "dcim:device_opennms_node_links", args=[local_interface.device_id]
        )
        if local_interface.cable_id or remote_interface.cable_id:
            messages.error(
                request, "One of these interfaces is already connected to a cable."
            )
            return redirect(return_url)
        cable = Cable(
            a_terminations=[local_interface], b_terminations=[remote_interface]
        )
        try:
            cable.full_clean()
            cable.save()
        except ValidationError as exc:
            messages.error(request, f"Couldn't create cable: {exc}")
            return redirect(return_url)
        messages.success(
            request, f"Created cable between {local_interface} and {remote_interface}."
        )
        return redirect(return_url)


@register_model_view(Device, name="opennms_node_links", path="opennms-node-links")
class DeviceNodeLinksView(_NodeLinksView):
    queryset = Device.objects.all()


@register_model_view(
    VirtualMachine, name="opennms_node_links", path="opennms-node-links"
)
class VirtualMachineNodeLinksView(_NodeLinksView):
    queryset = VirtualMachine.objects.all()


# --- One-Time Sync: pull OpenNMS data into a single Device/VM (issue #23) ----


class _OpenNMSPullView(GetReturnURLMixin, PermissionRequiredMixin, View):
    """Preview (GET) + commit (POST) "Pull OpenNMS data" for a Device/VM.

    Mirrors ``DiscoveredNodeImportView``'s review-then-commit shape: nothing
    is written until the operator has seen the plan. Both steps share
    ``_context``, which also gates on ``OpenNMSServer.is_healthy`` so a
    server currently flagged unhealthy can't be pulled from.
    """

    queryset = None
    template_name = "netbox_opennms/opennms_pull.html"

    def has_permission(self):
        # The commit path can both create and update Interfaces, and (for a
        # Device) create Cables — gate entry on the full set up front so the
        # preview never shows an action (e.g. "Create cable") the requesting
        # user isn't actually allowed to commit.
        if self.queryset.model is Device:
            perms = ["dcim.add_interface", "dcim.change_interface", "dcim.add_cable"]
        else:
            perms = [
                "virtualization.add_vminterface",
                "virtualization.change_vminterface",
            ]
        return self.request.user.has_perms(perms)

    def _context(self, pk):
        obj = get_object_or_404(self.queryset, pk=pk)
        discovered_node = DiscoveredNode.for_object(obj)
        if discovered_node is None:
            return obj, None, None, "No OpenNMS Discovery match for this object."
        server = discovered_node.server
        if not server.is_healthy:
            return (
                obj,
                discovered_node,
                None,
                f"Server {server.name!r} is marked unhealthy — refusing to pull "
                "until the connection is restored.",
            )
        try:
            with OpenNMSClient.from_server(server) as client:
                node_data = fetch_node_data(client, discovered_node)
        except OpenNMSError as exc:
            return obj, discovered_node, None, str(exc)
        plan = plan_reverse_sync(node_data, obj)
        return obj, discovered_node, plan, None

    def get(self, request, pk):
        obj, discovered_node, plan, error = self._context(pk)
        return render(
            request,
            self.template_name,
            {
                "object": obj,
                "discovered_node": discovered_node,
                "plan": plan,
                "error": error,
            },
        )

    def post(self, request, pk):
        obj, discovered_node, plan, error = self._context(pk)
        if error:
            messages.error(request, f"Could not pull OpenNMS data: {error}")
            return redirect(self.get_return_url(request, obj))
        if not plan.has_changes:
            messages.info(request, "Nothing to sync — already up to date.")
            return redirect(self.get_return_url(request, obj))
        result = run_reverse_sync(discovered_node.server, [discovered_node])[0]
        if result.success:
            messages.success(
                request,
                f"Pulled OpenNMS data for {obj}: "
                f"{result.interfaces_created} interface(s) created, "
                f"{result.interfaces_updated} updated, "
                f"{result.cables_created} cable(s) created.",
            )
        else:
            messages.error(request, f"Could not pull OpenNMS data: {result.error}")
        return redirect(self.get_return_url(request, obj))


@register_model_view(Device, name="opennms_pull", path="opennms-pull")
class DeviceOpenNMSPullView(_OpenNMSPullView):
    queryset = Device.objects.all()


@register_model_view(VirtualMachine, name="opennms_pull", path="opennms-pull")
class VirtualMachineOpenNMSPullView(_OpenNMSPullView):
    queryset = VirtualMachine.objects.all()


# --- One-Time Sync: bulk pull for a Requisition's matched nodes (issue #24) ---


@register_model_view(Requisition, name="opennms_pull", path="opennms-pull")
class RequisitionOpenNMSPullView(GetReturnURLMixin, PermissionRequiredMixin, View):
    """Preview (GET) + commit (POST) a bulk "One-Time Sync" over every matched
    node on a Requisition's Nodes tab (#21).

    Reuses #23's engine as-is: ``preview_reverse_sync``/``run_reverse_sync``
    already operate over an arbitrary list of ``DiscoveredNode`` objects, one
    client for the whole batch, per-node try/except so one node's failure
    never hides the rest (AC #3). Unmatched nodes aren't part of what a bulk
    pull can act on, so they're excluded up front rather than reported as
    errors; neighbour links with an unmatched remote endpoint remain
    per-node "Skipped" rows in the plan, same as the single-object view
    (AC #4).
    """

    queryset = Requisition.objects.all()
    template_name = "netbox_opennms/requisition_opennms_pull.html"

    def has_permission(self):
        # A Requisition's matched nodes may be a mix of Devices and VMs —
        # gate on the full superset up front, same reasoning as
        # _OpenNMSPullView.has_permission.
        perms = [
            "dcim.add_interface",
            "dcim.change_interface",
            "dcim.add_cable",
            "virtualization.add_vminterface",
            "virtualization.change_vminterface",
        ]
        return self.request.user.has_perms(perms)

    def _context(self, pk):
        instance = get_object_or_404(self.queryset, pk=pk)
        target_server = target_server_for(instance)
        if target_server is None:
            return (
                instance,
                None,
                None,
                "This Requisition's target OpenNMS Server could not be resolved.",
            )
        if not target_server.is_healthy:
            return (
                instance,
                target_server,
                None,
                f"Server {target_server.name!r} is marked unhealthy — refusing to "
                "pull until the connection is restored.",
            )
        nodes = DiscoveredNode.objects.filter(
            foreign_source=instance.name,
            server=target_server,
            matched_object_id__isnull=False,
        ).order_by("label")
        rows = preview_reverse_sync(target_server, nodes)
        return instance, target_server, rows, None

    def get(self, request, pk):
        instance, target_server, rows, error = self._context(pk)
        has_changes = bool(rows) and any(
            row.plan and row.plan.has_changes for row in rows
        )
        return render(
            request,
            self.template_name,
            {
                "object": instance,
                "target_server": target_server,
                "rows": rows,
                "error": error,
                "has_changes": has_changes,
            },
        )

    def post(self, request, pk):
        instance, target_server, rows, error = self._context(pk)
        if error:
            messages.error(request, f"Could not pull OpenNMS data: {error}")
            return redirect(self.get_return_url(request, instance))
        if not rows:
            messages.info(
                request, "Nothing to sync — no matched nodes for this Requisition."
            )
            return redirect(self.get_return_url(request, instance))
        nodes = [row.discovered_node for row in rows]
        results = run_reverse_sync(target_server, nodes)
        succeeded = [r for r in results if r.success]
        failed = [r for r in results if not r.success]
        if succeeded:
            messages.success(
                request,
                f"Pulled OpenNMS data for {len(succeeded)} node(s): "
                f"{sum(r.interfaces_created for r in succeeded)} interface(s) "
                f"created, {sum(r.interfaces_updated for r in succeeded)} "
                f"updated, {sum(r.cables_created for r in succeeded)} cable(s) "
                "created.",
            )
        for result in failed:
            messages.error(
                request,
                f"Could not pull OpenNMS data for {result.discovered_node}: "
                f"{result.error}",
            )
        return redirect(self.get_return_url(request, instance))
