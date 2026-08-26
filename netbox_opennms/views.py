# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""UI views for plugin models (Requisition redesign)."""

import json
from copy import deepcopy

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, PermissionRequiredMixin
from django.contrib.contenttypes.models import ContentType
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.generic import View
from netbox.views import generic
from utilities.rqworker import any_workers_for_queue
from utilities.views import GetReturnURLMixin

from . import filtersets, forms, tables
from .client import OpenNMSClient, OpenNMSError
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


# --- Discovery (issue #7) ----------------------------------------------------


class DiscoveredNodeView(generic.ObjectView):
    queryset = DiscoveredNode.objects.all()


class DiscoveredNodeListView(generic.ObjectListView):
    queryset = DiscoveredNode.objects.all()
    table = tables.DiscoveredNodeTable
    filterset = filtersets.DiscoveredNodeFilterSet
    filterset_form = forms.DiscoveredNodeFilterForm


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

        seen_ids = []
        for match in matches:
            seen_ids.append(match.opennms_node_id)
            matched_object_type = None
            if match.matched_kind:
                matched_object_type = ContentType.objects.get_for_model(
                    KIND_MODELS[match.matched_kind]
                )
            DiscoveredNode.objects.update_or_create(
                server=server,
                opennms_node_id=match.opennms_node_id,
                defaults={
                    "label": match.label,
                    "foreign_source": match.foreign_source,
                    "foreign_id": match.foreign_id,
                    "location": match.location,
                    "verdict": match.verdict,
                    "diff_detail": match.diff_detail,
                    "matched_object_type": matched_object_type,
                    "matched_object_id": match.matched_pk,
                },
            )
        server.discovered_nodes.exclude(opennms_node_id__in=seen_ids).delete()
        messages.success(
            request, f"Discovery scan of {server.name!r} found {len(matches)} node(s)."
        )
        return redirect(return_url)


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
