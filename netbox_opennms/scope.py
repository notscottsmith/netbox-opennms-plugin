# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Scope Resolution (ADR 0002/0003): which OpenNMS Server a Device/VM belongs to.

``OpenNMSServer`` and ``MonitoringExclusion`` share the identical five-level
Scope precedence engine — location > site > site group > tenant > tenant
group, most-specific-wins — mirroring NetBox core's own ``ConfigContext``
inheritance. A binding on a parent site group/tenant group/location cascades
to everything nested beneath it; a more specific level (including a
per-device ``MonitoringOverride.exclude``, resolved separately in
``membership``) overrides a less specific one.

Pure and read-only (ORM reads only, no writes/network) — the primary new
testing seam for multi-server support.
"""

from dataclasses import dataclass

from dcim.models import Location, Site
from django.contrib.contenttypes.models import ContentType
from ipam.models import Prefix

from .derivation import site_for
from .models import MonitoringExclusion, OpenNMSServer

SCOPE_FIELDS = ("tenant_groups", "tenants", "site_groups", "sites", "locations")


@dataclass
class ScopeResolution:
    """The outcome of resolving one Device/VM's Scope.

    At most one of ``server``/``excluded`` is meaningful: ``excluded=True``
    means a Scope level excludes the object (before any Default Server
    fallback is considered); otherwise ``server`` is the resolved
    ``OpenNMSServer`` (the Default Server if nothing more specific matched),
    or ``None`` if no Default Server exists either.
    """

    server: object = None
    excluded: bool = False


def _ancestor_chain(node):
    """*node* plus its ancestors, nearest-first — the cascade order for a
    ``NestedGroupModel`` (``Location``/``SiteGroup``/``TenantGroup``)."""
    if node is None:
        return []
    return [node, *node.get_ancestors(ascending=True)]


def _scope_levels(obj):
    """The five Scope levels for *obj*, in strict precedence order (ADR 0002).

    Each entry is ``(field_name, candidates)`` — ``field_name`` is the
    ``OpenNMSServer``/``MonitoringExclusion`` M2M field to filter on;
    ``candidates`` are that level's objects to check, nearest-first. A
    VirtualMachine has no ``location`` field, so that level is naturally empty.
    """
    location = getattr(obj, "location", None)
    site = site_for(obj)
    site_group = getattr(site, "group", None) if site else None
    tenant = getattr(obj, "tenant", None)
    tenant_group = getattr(tenant, "group", None) if tenant else None
    return [
        ("locations", _ancestor_chain(location)),
        ("sites", [site] if site else []),
        ("site_groups", _ancestor_chain(site_group)),
        ("tenants", [tenant] if tenant else []),
        ("tenant_groups", _ancestor_chain(tenant_group)),
    ]


def resolve_scope(obj):
    """Resolve one Device/VM's Scope to a Server, an exclusion, or the default.

    Walks levels in precedence order and, within a level, candidates
    nearest-first; the first candidate with EITHER a ``MonitoringExclusion``
    or an ``OpenNMSServer`` bound to it wins outright. Exclusion is checked
    before Server at each candidate: no ADR/spec case defines what a server
    bound to the exact same object an exclusion also names should do, so
    exclusion is treated as the conservative default. Falls through to the
    Default Server only once every level is exhausted with no match.
    """
    for field_name, candidates in _scope_levels(obj):
        for candidate in candidates:
            if MonitoringExclusion.objects.filter(**{field_name: candidate}).exists():
                return ScopeResolution(excluded=True)
            server = OpenNMSServer.objects.filter(**{field_name: candidate}).first()
            if server is not None:
                return ScopeResolution(server=server)
    return ScopeResolution(server=OpenNMSServer.objects.filter(is_default=True).first())


_NESTED_SCOPE_FIELDS = ("locations", "site_groups", "tenant_groups")


def scope_options(server):
    """Which Scope objects (per level) resolve to *server* via ``resolve_scope``.

    The reverse of ``resolve_scope``: used to constrain the Requisition Scope
    picker (issue #19) to only the objects that would actually land a member
    on *server*. A Scope object resolves to *server* if it's one of *server*'s
    own bound M2M objects (direct binding), or — for the three nested-group
    levels (``locations``/``site_groups``/``tenant_groups``) — a descendant of
    one of them, cascading DOWN the hierarchy via ``get_descendants()`` (the
    same hierarchy ``resolve_scope`` cascades UP via ``get_ancestors()``).
    ``sites``/``tenants`` aren't nested (``NestedGroupModel``), so only direct
    bindings apply to them.

    Returns ``None`` (unconstrained — every object is a valid pick) when
    *server* is ``None`` or is the Default Server: the Default Server carries
    no Scope bindings by validation (it's what everything unscoped falls
    through to), so constraining "its own bindings" would wrongly reject
    every choice instead of leaving the picker open. Otherwise returns a
    dict of five querysets keyed by ``SCOPE_FIELDS``.
    """
    if server is None or server.is_default:
        return None
    options = {}
    for field_name in SCOPE_FIELDS:
        direct = getattr(server, field_name).all()
        if field_name not in _NESTED_SCOPE_FIELDS:
            options[field_name] = direct
            continue
        pks = set(direct.values_list("pk", flat=True))
        for obj in direct:
            pks.update(obj.get_descendants().values_list("pk", flat=True))
        options[field_name] = direct.model.objects.filter(pk__in=pks)
    return options


def find_scope_collision(field, selected, exclude_pk=None, model=OpenNMSServer):
    """The *model* row already bound directly to any of *selected* on *field*.

    ADR 0002: a given object may be bound directly to only one Server at a
    time — a same-level collision is a hard validation error at assignment
    time, so ``OpenNMSServerForm``/``OpenNMSServerSerializer`` both call this
    to reject it. Deliberately NOT enforced here at resolve time: by the time
    ``resolve_scope`` runs, a collision is an existing data-entry mistake, not
    something to arbitrate.
    """
    if not selected:
        return None
    others = model.objects.filter(**{f"{field}__in": selected})
    if exclude_pk is not None:
        others = others.exclude(pk=exclude_pk)
    return others.first()


def _scope_levels_for_site_or_location(site=None, location=None):
    """The five Scope levels starting from an explicit Site/Location (ADR 0009).

    Same shape as ``_scope_levels``, but for a Requisition's resolved
    site/location rather than a Device/VM's own attributes: tenant is drawn
    from the Location's own ``tenant`` first (falling back to the Site's),
    mirroring how a Location may carry a more specific tenant than its Site.
    """
    if location is not None and site is None:
        site = location.site
    site_group = getattr(site, "group", None) if site else None
    tenant = getattr(location, "tenant", None) if location else None
    if tenant is None and site is not None:
        tenant = site.tenant
    tenant_group = getattr(tenant, "group", None) if tenant else None
    return [
        ("locations", _ancestor_chain(location)),
        ("sites", [site] if site else []),
        ("site_groups", _ancestor_chain(site_group)),
        ("tenants", [tenant] if tenant else []),
        ("tenant_groups", _ancestor_chain(tenant_group)),
    ]


def _single_scope_object(model, slugs):
    """The one object *slugs* (a filter_params value) unambiguously names."""
    if not slugs or len(slugs) != 1:
        return None
    return model.objects.filter(slug=slugs[0]).first()


def requisition_scope_site_and_location(requisition):
    """The Site/Location a Requisition's own ``filter_params`` resolves to.

    Reads the same ``"site"``/``"location"`` filter keys the Requisition Scope
    picker (issue #19) writes (a single slug per key) — this is how a
    Requisition's own scope becomes readable without a separate structured
    field. Returns ``(site, location)``; either is ``None`` when its key is
    absent, empty, or names more than one object (no single scope to resolve
    a VRF from, ADR 0009).
    """
    params = requisition.filter_params or {}
    site = _single_scope_object(Site, params.get("site"))
    location = _single_scope_object(Location, params.get("location"))
    return site, location


def resolve_vrf(*, site=None, location=None):
    """Resolve the VRF scoped to *site*/*location* (ADR 0009), or ``None``.

    Supersedes ADR 0008's bespoke ``VRFAssignment`` binding table: a VRF is
    already "assigned" to a NetBox entity natively, via any ``ipam.Prefix``
    scoped (``CachedScopeMixin``) to that entity and carrying a ``vrf`` — so
    this reads that off directly instead of maintaining a parallel mechanism.
    Same most-specific-wins precedence as ``resolve_scope``, starting from an
    explicit Site/Location rather than a Device/VM. Unlike ``resolve_scope``
    there is no Default VRF fallback — ``None`` means no scoped Prefix at any
    level carries a VRF.
    """
    for _field_name, candidates in _scope_levels_for_site_or_location(
        site=site, location=location
    ):
        for candidate in candidates:
            ct = ContentType.objects.get_for_model(candidate)
            prefix = Prefix.objects.filter(
                scope_type=ct, scope_id=candidate.pk, vrf__isnull=False
            ).first()
            if prefix is not None:
                return prefix.vrf
    return None
