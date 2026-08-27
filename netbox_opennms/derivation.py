# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Foreign Source name derivation — the single owner (AD-14).

Every consumer (translation, jobs, views) must call ``foreign_source_for``;
no module may derive the name inline. The name groups monitored objects by
(site, role) so OpenNMS node identity stays stable.

AD-14 specifies a VM's site as ``vm.site or vm.cluster.site``. NetBox 4.x
replaced ``Cluster.site`` with a generic ``scope``, so the cluster fallback
resolves the cluster's scope when that scope is a Site.

The function has no side effects (no writes, no network) and is deterministic,
but reading the target's ``site``/``role``/``cluster.scope`` may lazily load
related objects — callers in the render/sync paths should pass prefetched
(``select_related``) instances to keep it query-free.
"""

import re

from dcim.models import Device, Site
from netbox.plugins import get_plugin_config
from virtualization.models import VirtualMachine

PLUGIN_NAME = "netbox_opennms"

# Characters OpenNMS forbids in a Foreign Source (requisition) name. ':' is
# forbidden too — OpenNMS rejects it on import with HTTP 400 (caught by the
# Story 4.4 live round-trip), which is why the delimiter below is '.'.
_FORBIDDEN_CHARS = set("/\\?*'\":")

# A Requisition name is now USER-CHOSEN and goes straight into a REST URL path
# (GET/POST /rest/requisitions/{name}), so the slug-era ``_FORBIDDEN_CHARS`` guard
# is insufficient (R1/H7): whitespace and URL-significant characters must also be
# rejected. This is the superset the Requisition name validator enforces.
_NAME_FORBIDDEN_CHARS = _FORBIDDEN_CHARS | set("#%&+ \t\n\r")

# OpenNMS Monitoring Location names: ASCII alphanumeric plus '-' and '.' (AD-9).
# \A...\Z (not ^...$) so a trailing newline is rejected, not accepted.
_LOCATION_ALLOWED = re.compile(r"\A[A-Za-z0-9.-]*\Z")


def location_name_error(name):
    """Return an error message if *name* is not a valid OpenNMS location name
    (AD-9), else ``None``.

    An empty value is allowed (it means "use the default location"). Otherwise
    only ASCII letters, digits, ``-`` and ``.`` are permitted.
    """
    if name and not _LOCATION_ALLOWED.match(name):
        return (
            f"Location name {name!r} may contain only ASCII letters, digits, "
            "'-' and '.'."
        )
    return None


def validate_location_name(name):
    """Raise ``ValueError`` if *name* is not a valid OpenNMS location name (AD-9)."""
    error = location_name_error(name)
    if error:
        raise ValueError(error)
    return name


def validate_foreign_source_name(name):
    """Raise ``ValueError`` if *name* contains an OpenNMS-forbidden character.

    NetBox slugs are already URL-safe, so this is a contract guard rather than
    an expected failure path.
    """
    bad = sorted(_FORBIDDEN_CHARS.intersection(name))
    if bad:
        raise ValueError(
            f"Foreign Source name {name!r} contains forbidden characters: "
            f"{''.join(bad)}"
        )
    return name


def requisition_name_error(name):
    """Return an error message if *name* is not a safe OpenNMS Foreign Source
    name (H7), else ``None``.

    A Requisition's name IS the Foreign Source name and is placed directly into a
    REST URL path, so it must reject whitespace and URL-significant characters in
    addition to the OpenNMS-forbidden set. An empty name is rejected (a Foreign
    Source must be named).
    """
    if not name or not name.strip():
        return "A Requisition name is required."
    bad = sorted(_NAME_FORBIDDEN_CHARS.intersection(name))
    if bad:
        # repr() already renders whitespace legibly (' ', '\t', '\n').
        printable = ", ".join(repr(c) for c in bad)
        return (
            f"Requisition name {name!r} contains characters that are not safe in "
            f"an OpenNMS Foreign Source / URL: {printable}."
        )
    return None


def validate_requisition_name(name):
    """Raise ``ValueError`` if *name* is not a safe OpenNMS Foreign Source name (H7)."""
    error = requisition_name_error(name)
    if error:
        raise ValueError(error)
    return name


def site_for(target):
    """Resolve a target's site: a VM falls back to its cluster's scope (4.x)."""
    site = getattr(target, "site", None)
    if site is None:
        cluster = getattr(target, "cluster", None)
        if cluster is not None:
            scope = getattr(cluster, "scope", None)
            if isinstance(scope, Site):
                site = scope
    return site


def foreign_id_for(target):
    """Return the type-qualified OpenNMS Foreign ID for a Device/VM (AD-8).

    ``{foreign_id_prefix}-device-{pk}`` / ``{foreign_id_prefix}-vm-{pk}`` — the
    type token keeps a Device and a VM with the same primary key from colliding
    on node identity. ``foreign_id_prefix`` (default ``"netbox"``, issue #3) is a
    plugin-wide setting read via ``get_plugin_config``, an in-memory lookup over
    already-loaded settings — no network/DB access — so this stays pure and
    deterministic. An empty prefix reproduces the legacy unprefixed format.
    """
    prefix = get_plugin_config(PLUGIN_NAME, "foreign_id_prefix")
    kind = None
    if isinstance(target, Device):
        kind = "device"
    elif isinstance(target, VirtualMachine):
        kind = "vm"
    else:
        raise TypeError(
            "foreign_id_for() expects a Device or VirtualMachine, "
            f"got {type(target).__name__}."
        )
    return f"{prefix}-{kind}-{target.pk}" if prefix else f"{kind}-{target.pk}"


def discovery_foreign_source_for(timestamp):
    """Return a throwaway Foreign Source name for one Discovery Scan (ADR 0006).

    Format: ``{foreign_id_prefix}-discovery-{timestamp:%Y%m%d%H%M%S%f}`` —
    unique per scan (OpenNMS routes each run's ``newSuspect`` events into their
    own node set via this name) and visually distinct from a Requisition's
    ``netbox.{site}.{role}`` Foreign Source. Microsecond resolution avoids
    colliding with the field's unique constraint when two scans are created
    within the same second. Takes the timestamp as a param rather than reading
    the clock itself, so this stays pure/deterministic like
    ``foreign_source_for`` — the caller (``DiscoveryScan.save``) owns "now".
    """
    prefix = get_plugin_config(PLUGIN_NAME, "foreign_id_prefix")
    # Microsecond resolution (not just seconds): two scans created within the
    # same second would otherwise collide on the field's unique constraint.
    stamp = timestamp.strftime("%Y%m%d%H%M%S%f")
    name = f"{prefix}-discovery-{stamp}" if prefix else f"discovery-{stamp}"
    return validate_foreign_source_name(name)


def monitoring_location_for(*, site=None, location=None):
    """Return the OpenNMS Monitoring Location a Discovery Scan's NetBox
    site/location supplies on the scan request (ADR 0006/0008).

    NetBox Location and OpenNMS Monitoring Location are unrelated concepts
    that happen to share a name (CONTEXT.md) — this is the single point that
    bridges them (AD-14), mirroring ``foreign_source_for``'s slug-based
    naming. The most specific NetBox object supplies the name: ``location``
    wins over ``site`` when both are given. Raises ``ValueError`` (via
    ``validate_location_name``) if the slug isn't also a legal OpenNMS
    location name (AD-9) — NetBox slugs permit ``_``, which OpenNMS doesn't.
    """
    obj = location or site
    if obj is None:
        raise ValueError("monitoring_location_for() requires a site or location.")
    return validate_location_name(obj.slug)


def foreign_source_for(target):
    """Return the Foreign Source name for a monitored Device or VirtualMachine.

    Format: ``netbox.{site.slug}.{role.slug}``, with ``no-site`` / ``no-role``
    substituted when the site or role is absent (AD-9, AD-14).
    """
    if not isinstance(target, (Device, VirtualMachine)):
        raise TypeError(
            "foreign_source_for() expects a Device or VirtualMachine, "
            f"got {type(target).__name__}."
        )
    site = site_for(target)
    role = getattr(target, "role", None)
    site_slug = site.slug if (site and site.slug) else "no-site"
    role_slug = role.slug if (role and role.slug) else "no-role"
    # '.' delimiter (not '-'): NetBox slugs are [-A-Za-z0-9_] so a hyphen (or
    # underscore) separator is ambiguous (site "a-b"+role "c" vs site "a"+role
    # "b-c"). A slug cannot contain '.', keeping the name injective — and '.' is
    # OpenNMS-legal whereas ':' is FORBIDDEN by OpenNMS (it 400s on import).
    name = f"netbox.{site_slug}.{role_slug}"
    return validate_foreign_source_name(name)
