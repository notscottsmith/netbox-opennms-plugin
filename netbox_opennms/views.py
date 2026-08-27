# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""UI views for plugin models (Requisition redesign)."""

import json
from copy import deepcopy

from dcim.models import Cable, Device, Interface, Site
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.contrib.contenttypes.models import ContentType
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
from .jobs import (
    SyncForeignSourceJob,
    unknown_locations,
)
from .membership import (
    filter_errors,
    requisition_conflicts,
    resolve,
    resolve_all,
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
    VRFAssignment,
)
from .requisition_discovery import build_foreign_source_import, list_unmirrored
from .scan import KIND_MODELS, scan_server
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


class VRFAssignmentView(generic.ObjectView):
    queryset = VRFAssignment.objects.all()


class VRFAssignmentListView(generic.ObjectListView):
    queryset = VRFAssignment.objects.all()
    table = tables.VRFAssignmentTable
    filterset = filtersets.VRFAssignmentFilterSet


class VRFAssignmentEditView(generic.ObjectEditView):
    queryset = VRFAssignment.objects.all()
    form = forms.VRFAssignmentForm


class VRFAssignmentDeleteView(generic.ObjectDeleteView):
    queryset = VRFAssignment.objects.all()


class VRFAssignmentBulkDeleteView(generic.BulkDeleteView):
    queryset = VRFAssignment.objects.all()
    table = tables.VRFAssignmentTable


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


# --- Discovery (issue #7) ----------------------------------------------------


class DiscoveredNodeView(generic.ObjectView):
    queryset = DiscoveredNode.objects.all()


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

        linked_ids = set(
            server.discovered_nodes.filter(resolution="linked").values_list(
                "opennms_node_id", flat=True
            )
        )
        seen_ids = []
        for match in matches:
            seen_ids.append(match.opennms_node_id)
            defaults = {
                "label": match.label,
                "foreign_source": match.foreign_source,
                "foreign_id": match.foreign_id,
                "location": match.location,
            }
            # A manually-linked row's match came from the operator, not this
            # scan's Foreign-ID reconciliation — never overwrite it (issue #8).
            if match.opennms_node_id not in linked_ids:
                matched_object_type = None
                if match.matched_kind:
                    matched_object_type = ContentType.objects.get_for_model(
                        KIND_MODELS[match.matched_kind]
                    )
                defaults.update(
                    {
                        "verdict": match.verdict,
                        "diff_detail": match.diff_detail,
                        "matched_object_type": matched_object_type,
                        "matched_object_id": match.matched_pk,
                    }
                )
            DiscoveredNode.objects.update_or_create(
                server=server,
                opennms_node_id=match.opennms_node_id,
                defaults=defaults,
            )
        server.discovered_nodes.exclude(opennms_node_id__in=seen_ids).delete()
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
        """Live OpenNMS data for *node*, built into an import proposal."""
        try:
            with OpenNMSClient.from_server(node.server) as client:
                detail = client.get_node(node.opennms_node_id) or {}
                ip_interfaces = client.list_ip_interfaces(node.opennms_node_id)
                services_by_ip = {}
                for iface in ip_interfaces:
                    ip = iface.get("ipAddress") if isinstance(iface, dict) else None
                    if ip:
                        services_by_ip[ip] = client.list_services(
                            node.opennms_node_id, ip
                        )
        except OpenNMSError as exc:
            return None, str(exc)
        overrides = import_node.asset_field_overrides()
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
                    ip_interfaces = client.list_ip_interfaces(node.opennms_node_id)
                    services_by_ip = {}
                    for iface in ip_interfaces:
                        ip = (
                            iface.get("ipAddress")
                            if isinstance(iface, dict)
                            else None
                        )
                        if ip:
                            services_by_ip[ip] = client.list_services(
                                node.opennms_node_id, ip
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
        except OpenNMSError as exc:
            server.record_check_result(ok=False, message=str(exc))
            messages.error(
                request, f"OpenNMS connection to {server.name!r} failed: {exc}"
            )
        else:
            server.record_check_result(ok=True)
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
            get_object_or_404(OpenNMSServer, pk=server_id).record_check_result(ok=True)
        return JsonResponse({"ok": True, "locations": locations})


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


def _remote_discovered_node(local_node, link):
    """The ``DiscoveredNode`` for *link*'s remote endpoint, if any.

    ``link.remote_node_id`` is the OpenNMS node id parsed out of the payload's
    ``*Url`` field (see ``node_links._remote_node_id``) — every remote endpoint
    OpenNMS reports lives on the *same* server as the local node, so matching
    is scoped to ``local_node.server``.
    """
    if link.remote_node_id is None:
        return None
    return DiscoveredNode.objects.filter(
        server=local_node.server, opennms_node_id=link.remote_node_id
    ).first()


def _cable_endpoints(local_object, local_node, link):
    """Resolve *link* to ``(local_interface, remote_interface)``, or ``(None, reason)``.

    Both endpoints must already be matched/imported NetBox Devices (#8/#9) with
    an Interface named for the port OpenNMS reported, and neither interface may
    already be cabled — anything else is "not-yet-actionable", per #16's
    review-don't-guess principle, not an error.
    """
    if not isinstance(local_object, Device):
        return None, "This object isn't a Device, and can't be cabled."
    remote_node = _remote_discovered_node(local_node, link)
    remote_object = remote_node.matched_object if remote_node else None
    if remote_object is None:
        return (
            None,
            "The remote node for this link hasn't been matched or imported "
            "into NetBox yet.",
        )
    if not isinstance(remote_object, Device):
        return (
            None,
            f"Remote object is a {remote_object._meta.verbose_name}, "
            "which can't be cabled.",
        )
    local_interface = Interface.objects.filter(
        device=local_object, name=link.local_port
    ).first()
    if local_interface is None:
        return None, f"No interface named '{link.local_port}' on {local_object}."
    remote_interface = Interface.objects.filter(
        device=remote_object, name=link.remote_port
    ).first()
    if remote_interface is None:
        return None, f"No interface named '{link.remote_port}' on {remote_object}."
    if local_interface.cable_id or remote_interface.cable_id:
        return None, "One of these interfaces is already connected to a cable."
    return (local_interface, remote_interface), None


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
