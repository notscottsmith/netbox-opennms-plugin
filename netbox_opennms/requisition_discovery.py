# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Unmirrored Foreign Source discovery + import (issues #11, #22).

Which of an OpenNMS Server's Foreign Sources/Requisitions have no
corresponding NetBox ``Requisition`` row (#11), and turning one of those
Foreign Sources' live OpenNMS definition into a Requisition shell (#22) —
name, scan-interval, and detectors/policies copied verbatim; the filter is
deliberately left for the admin to define, so an import alone never grows a
Requisition's membership. The parsing here is pure (given an already-fetched
``get_foreign_source()`` JSON document); ``list_unmirrored`` is the only I/O
wrapper, mirroring ``requisition_scan.py``'s pure-diff/thin-fetch split.
"""

from dataclasses import dataclass, field

from .requisition_scan import _as_list


@dataclass
class RuleImport:
    """One detector/policy entry parsed from a Foreign Source definition."""

    name: str
    rule_class: str
    parameters: dict = field(default_factory=dict)


@dataclass
class ForeignSourceImport:
    """The importable pieces of a Foreign Source definition (issue #22)."""

    scan_interval: str
    detectors: list = field(default_factory=list)
    policies: list = field(default_factory=list)


def _parameters_from_entry(entry):
    """Parse a detector/policy entry's parameters into a flat ``{key: value}`` dict.

    UNVERIFIED against a live OpenNMS server (no Docker/live API access while
    building this — see issue #22's implementation notes): by symmetry with how
    ``detectors``/``policies`` themselves nest (confirmed via ``requisition_scan.py``'s
    ``_definition_changes``) and how JAXB/Jackson serializes a repeated child
    element, a parameter is assumed to arrive at ``entry["parameter"]`` in the
    same "list, bare dict, or absent" shape ``_as_list`` normalizes, each item a
    ``{"key": ..., "value": ...}`` dict. Kept as its own small function so this
    assumption is easy to correct in isolation if the real shape differs.
    """
    if not isinstance(entry, dict):
        return {}
    parameters = {}
    for item in _as_list(entry.get("parameter")):
        if isinstance(item, dict) and item.get("key") is not None:
            parameters[item["key"]] = item.get("value", "")
    return parameters


def _rules_from_definition(definition, plural_key, singular_key):
    """Parse a definition's ``detectors``/``policies`` entries into ``RuleImport``s.

    Mirrors ``requisition_scan._definition_changes``'s own unwrapping of the same JSON
    (``{"detectors": {"detector": [...]}}``, a lone entry unwrapped to a bare
    dict, or the key absent entirely) via the shared ``_as_list`` helper.
    """
    entries = _as_list(definition.get(plural_key), singular_key)
    rules = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        rules.append(
            RuleImport(
                name=entry["name"],
                rule_class=entry.get("class", ""),
                parameters=_parameters_from_entry(entry),
            )
        )
    return rules


def build_foreign_source_import(definition):
    """Pure: turn a ``get_foreign_source()`` JSON document into importable pieces.

    ``definition`` may be any falsy/non-dict value (defensive, mirrors
    ``requisition_scan._definition_changes``) — treated as an empty definition rather
    than raising.
    """
    definition = definition if isinstance(definition, dict) else {}
    return ForeignSourceImport(
        scan_interval=definition.get("scan-interval") or "1d",
        detectors=_rules_from_definition(definition, "detectors", "detector"),
        policies=_rules_from_definition(definition, "policies", "policy"),
    )


def unmirrored_requisitions(opennms_names, netbox_names):
    """Names present in *opennms_names* but not in *netbox_names* (sorted).

    ``Requisition.name`` IS the Foreign Source name (models.py), so this is a
    plain set difference — no separate join key needed the way node matching
    needs the Foreign ID (``scan.reconcile``).
    """
    netbox_set = set(netbox_names)
    return sorted({name for name in opennms_names if name not in netbox_set})


def list_unmirrored(server):
    """Fetch *server*'s live Foreign Source names and diff against NetBox.

    The thin I/O wrapper around ``unmirrored_requisitions`` (mirrors
    ``scan.scan_server``). Raises ``OpenNMSError`` on a client failure —
    callers degrade per their own convention (AD-16).
    """
    from .client import OpenNMSClient
    from .models import Requisition

    with OpenNMSClient.from_server(server) as client:
        names = client.list_requisition_names()
    return unmirrored_requisitions(
        names, Requisition.objects.values_list("name", flat=True)
    )
