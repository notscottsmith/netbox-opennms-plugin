# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Propose and create a NetBox Device/VM from a red Discovery row (issue #9).

Split the same way ``scan.py`` splits ``reconcile`` (pure) from ``scan_server``
(I/O): ``build_proposal`` only reads already-fetched OpenNMS payloads plus the
ORM (no network calls, no writes), so it stays unit-testable without mocking
an ``OpenNMSClient``. ``import_node`` is the write path, called once the
operator has reviewed/corrected the proposal.
"""

import ipaddress
from dataclasses import dataclass, field

from dcim.choices import InterfaceTypeChoices
from dcim.models import Device, DeviceRole, Interface, Manufacturer, Platform
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import transaction
from ipam.models import IPAddress
from tenancy.models import Tenant
from virtualization.models import VirtualMachine, VMInterface

from .choices import InterfaceRoleChoices
from .derivation import location_name_error
from .membership import matching_requisitions, resolve_all
from .models import (
    AssetMapping,
    MonitoredInterface,
    MonitoredService,
    MonitoringOverride,
)

# Direct OpenNMS asset-field fallback for a NetBox source with an obvious 1:1
# correspondent (catalog.ASSET_FIELDS has no site/tenant/role field at all, so
# those two are only ever inferred via a configured AssetMapping, never here).
DEFAULT_ASSET_FIELD_MAP = {
    "serial": "serialNumber",
    "asset_tag": "assetNumber",
    "manufacturer": "manufacturer",
    "rack": "rack",
    "comments": "comment",
    "description": "description",
    "model": "modelNumber",
    "platform": "operatingSystem",
}

KIND_INTERFACE_MODELS = {"device": Interface, "vm": VMInterface}


class ImportRejected(Exception):
    """Raised when creating the proposed object would violate ADR-0001."""


@dataclass
class FieldProposal:
    """One reviewable field: a guess NetBox can act on, or an untouched gap.

    ``detected`` is the raw OpenNMS asset value (if any) shown to the operator
    even when it couldn't be resolved to a NetBox object — a low-confidence
    guess (``guessed=True``) is visibly distinct from a field OpenNMS's data
    simply doesn't cover (``detected == ""``).
    """

    value: object = None
    detected: str = ""
    guessed: bool = False


@dataclass
class InterfaceProposal:
    ip_address: str
    role: str = InterfaceRoleChoices.NOT_ELIGIBLE

    @property
    def is_primary(self):
        return self.role == InterfaceRoleChoices.PRIMARY


@dataclass
class ServiceProposal:
    ip_address: str
    name: str


@dataclass
class ImportProposal:
    label: str
    location: str = ""
    tenant: FieldProposal = field(default_factory=FieldProposal)
    site: FieldProposal = field(default_factory=FieldProposal)
    role: FieldProposal = field(default_factory=FieldProposal)
    manufacturer: FieldProposal = field(default_factory=FieldProposal)
    platform: FieldProposal = field(default_factory=FieldProposal)
    interfaces: list = field(default_factory=list)
    services: list = field(default_factory=list)


def asset_field_overrides():
    """The admin's own OpenNMS-asset-field -> NetBox-source vocabulary.

    Reads every configured ``AssetMapping`` regardless of Requisition — a red
    row has no Requisition yet, so this reuses the outbound mapping as an
    instance-wide convention for what an asset field means.
    """
    return {m.asset_field: m.netbox_source for m in AssetMapping.objects.all()}


def _resolve_source_field(source, overrides):
    for asset_field, netbox_source in overrides.items():
        if netbox_source == source:
            return asset_field
    return DEFAULT_ASSET_FIELD_MAP.get(source)


def _match_by_name(queryset, value):
    if not value:
        return None
    return queryset.filter(name__iexact=value).first()


_RESOLVERS = {
    "tenant": lambda v: _match_by_name(Tenant.objects.all(), v),
    "site": None,  # resolved by the caller (Site is the one caller-provided model)
    "role": lambda v: _match_by_name(DeviceRole.objects.all(), v),
    "manufacturer": lambda v: _match_by_name(Manufacturer.objects.all(), v),
    "platform": lambda v: _match_by_name(Platform.objects.all(), v),
}


def _propose_field(source, asset_record, overrides, site_model):
    asset_field = _resolve_source_field(source, overrides)
    if not asset_field:
        return FieldProposal()
    raw = asset_record.get(asset_field)
    if not raw or not isinstance(raw, str):
        return FieldProposal()
    if source == "site":
        matched = _match_by_name(site_model.objects.all(), raw)
    else:
        matched = _RESOLVERS[source](raw)
    return FieldProposal(value=matched, detected=raw, guessed=matched is not None)


def _parse_categories(node_detail):
    raw = node_detail.get("categories", node_detail.get("category", []))
    if isinstance(raw, dict):
        raw = raw.get("category", [])
    if not isinstance(raw, list):
        return []
    names = []
    for entry in raw:
        if isinstance(entry, dict):
            name = entry.get("name")
        elif isinstance(entry, str):
            name = entry
        else:
            name = None
        if name:
            names.append(name)
    return names


def _propose_role(asset_record, categories, overrides):
    proposal = _propose_field("role", asset_record, overrides, site_model=None)
    if proposal.value is not None:
        return proposal
    # No asset-based guess — fall back to a category name that happens to
    # match an existing Device Role. OpenNMS categories are free-form
    # operator tags, so an unmatched one is simply shown as a bare detection.
    for name in categories:
        matched = _match_by_name(DeviceRole.objects.all(), name)
        if matched is not None:
            return FieldProposal(value=matched, detected=name, guessed=True)
    if categories and not proposal.detected:
        return FieldProposal(detected=", ".join(categories))
    return proposal


def _parse_ip_interfaces(ip_interfaces):
    interfaces = []
    for iface in ip_interfaces or []:
        if not isinstance(iface, dict):
            continue
        ip = iface.get("ipAddress") or iface.get("ip-address")
        if not ip:
            continue
        role = str(iface.get("snmpPrimary", "N")).upper()
        if role not in (InterfaceRoleChoices.PRIMARY, InterfaceRoleChoices.SECONDARY):
            role = InterfaceRoleChoices.NOT_ELIGIBLE
        interfaces.append(InterfaceProposal(ip_address=ip, role=role))
    return interfaces


def _service_name(raw):
    if not isinstance(raw, dict):
        return None
    service_type = raw.get("serviceType") or raw.get("service-type") or {}
    if isinstance(service_type, dict):
        name = service_type.get("name")
        if name:
            return name
    return raw.get("name")


def parse_discovery_payload(ip_interfaces, services_by_ip):
    """IP interfaces + services from raw OpenNMS payloads, with no field-guessing.

    Split out of ``build_proposal`` so bulk import (#10) can reuse this half
    without ever calling the other half (``_propose_field``/``_propose_role``/
    ``asset_field_overrides``) — a bulk batch must apply only the operator's
    explicit choice for the whole batch, never a per-row auto-detected guess.
    """
    interfaces = _parse_ip_interfaces(ip_interfaces)
    services = []
    for iface in interfaces:
        for raw in (services_by_ip or {}).get(iface.ip_address, []):
            name = _service_name(raw)
            if name:
                services.append(ServiceProposal(ip_address=iface.ip_address, name=name))
    return interfaces, services


def build_proposal(
    node, node_detail, ip_interfaces, services_by_ip, overrides, site_model
):
    """The reviewable import proposal for one red ``DiscoveredNode`` row.

    *node_detail* is ``client.get_node()``'s payload; *ip_interfaces* is
    ``client.list_ip_interfaces()``'s; *services_by_ip* maps each interface's
    IP address to ``client.list_services()``'s payload for it. *overrides* is
    ``asset_field_overrides()``'s result and *site_model* is ``dcim.Site``
    (both passed in so this stays a pure function of its arguments).
    """
    asset_record = (node_detail or {}).get("assetRecord") or {}
    categories = _parse_categories(node_detail or {})
    interfaces, services = parse_discovery_payload(ip_interfaces, services_by_ip)

    return ImportProposal(
        label=node.label,
        location=node.location,
        tenant=_propose_field("tenant", asset_record, overrides, site_model),
        site=_propose_field("site", asset_record, overrides, site_model),
        role=_propose_role(asset_record, categories, overrides),
        manufacturer=_propose_field(
            "manufacturer", asset_record, overrides, site_model
        ),
        platform=_propose_field("platform", asset_record, overrides, site_model),
        interfaces=interfaces,
        services=services,
    )


def _create_target(kind, cleaned_data):
    common = {
        "name": cleaned_data["name"],
        "tenant": cleaned_data.get("tenant"),
        "platform": cleaned_data.get("platform"),
    }
    if kind == "device":
        return Device(
            site=cleaned_data["site"],
            role=cleaned_data["role"],
            device_type=cleaned_data["device_type"],
            **common,
        )
    return VirtualMachine(
        site=cleaned_data.get("site"),
        role=cleaned_data.get("role"),
        **common,
    )


def _create_interfaces_and_ips(target, kind, interfaces):
    interface_model = KIND_INTERFACE_MODELS[kind]
    ip_by_addr = {}
    primary_ip = None
    for index, iface in enumerate(interfaces):
        if kind == "device":
            nic = interface_model.objects.create(
                device=target,
                name=f"eth{index}",
                type=InterfaceTypeChoices.TYPE_OTHER,
            )
        else:
            nic = interface_model.objects.create(
                virtual_machine=target, name=f"eth{index}"
            )
        version = ipaddress.ip_address(iface.ip_address).version
        prefix = 32 if version == 4 else 128
        ip_obj = IPAddress.objects.create(
            address=f"{iface.ip_address}/{prefix}", assigned_object=nic
        )
        ip_by_addr[iface.ip_address] = ip_obj
        if iface.is_primary and primary_ip is None:
            primary_ip = ip_obj
    return ip_by_addr, primary_ip


def _check_server_conflict(target):
    """Raise ``ImportRejected`` if *target* would create an ADR-0001 conflict.

    Reuses the canonical ``matching_requisitions``/``resolve_all`` resolution
    path rather than a bespoke check, per issue #9: import "proposes into the
    existing model, it does not introduce a parallel resolution path."
    ``matching_requisitions`` requires an already-persisted object, so this
    can only run after *target* has been saved.
    """
    matched_ids = {r.pk for r in matching_requisitions(target)}
    if not matched_ids:
        return
    conflicted = {
        resolution.requisition.name
        for resolution in resolve_all()
        if resolution.requisition.pk in matched_ids
        and resolution.server_conflict is not None
    }
    if conflicted:
        names = ", ".join(sorted(conflicted))
        raise ImportRejected(
            f"Importing {target} would create a Server Conflict (ADR-0001) on "
            f"Requisition(s) {names} — adjust Scope before importing, or "
            "exclude this object from one of the conflicting Servers."
        )


def import_node(node, kind, cleaned_data, proposal):
    """Create the proposed Device/VM and link *node* to it, all-or-nothing.

    *cleaned_data* is ``DiscoveredNodeImportForm.cleaned_data`` (the
    operator's reviewed/corrected fields); *proposal* supplies the IP
    interfaces and services, imported verbatim. Raises ``ImportRejected``
    (rolling back every write) on an ADR-0001 Server Conflict.
    """
    with transaction.atomic():
        target = _create_target(kind, cleaned_data)
        target.full_clean()
        target.save()

        ip_by_addr, primary_ip = _create_interfaces_and_ips(
            target, kind, proposal.interfaces
        )
        if primary_ip is not None:
            if primary_ip.address.version == 4:
                target.primary_ip4 = primary_ip
            else:
                target.primary_ip6 = primary_ip
            target.save()

        location = cleaned_data.get("location") or ""
        if location_name_error(location):
            location = ""
        override = MonitoringOverride.objects.create(
            assigned_object=target, management_ip=primary_ip, location=location
        )

        for iface in proposal.interfaces:
            if iface.is_primary:
                continue
            ip_obj = ip_by_addr.get(iface.ip_address)
            if ip_obj is None:
                continue
            interface = MonitoredInterface(
                override=override, ip_address=ip_obj, role=iface.role
            )
            try:
                interface.full_clean()
                interface.save()
            except DjangoValidationError:
                continue

        seen = set()
        for svc in proposal.services:
            ip_obj = ip_by_addr.get(svc.ip_address)
            if ip_obj is None or (ip_obj.pk, svc.name) in seen:
                continue
            seen.add((ip_obj.pk, svc.name))
            service = MonitoredService(
                override=override, ip_address=ip_obj, name=svc.name
            )
            try:
                service.full_clean()
                service.save()
            except DjangoValidationError:
                continue

        _check_server_conflict(target)
        node.link_to(target)

    return target
