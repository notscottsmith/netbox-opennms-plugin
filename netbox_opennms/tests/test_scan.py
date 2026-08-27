# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Tests for the pure Discovery reconciler (issue #7)."""

from django.test import SimpleTestCase
from django.utils import timezone

from netbox_opennms.scan import NodeMatch, _parse_node_created, reconcile


def _opennms_node(
    node_id=1,
    label="rtr-1",
    foreign_source="fs",
    foreign_id="device-1",
    location="Perth",
    create_time=None,
):
    node = {
        "id": node_id,
        "label": label,
        "foreignSource": foreign_source,
        "foreignId": foreign_id,
        "location": location,
    }
    if create_time is not None:
        node["createTime"] = create_time
    return node


def _netbox_entry(kind="device", pk=42, label="rtr-1", location="Perth"):
    return {"kind": kind, "pk": pk, "label": label, "location": location}


class ReconcileTest(SimpleTestCase):
    def test_green_on_full_match(self):
        results = reconcile(
            [_opennms_node()], {"device-1": _netbox_entry()}
        )
        self.assertEqual(len(results), 1)
        match = results[0]
        self.assertIsInstance(match, NodeMatch)
        self.assertEqual(match.verdict, "green")
        self.assertEqual(match.diff_detail, [])
        self.assertEqual(match.matched_kind, "device")
        self.assertEqual(match.matched_pk, 42)

    def test_orange_on_label_mismatch(self):
        results = reconcile(
            [_opennms_node(label="rtr-1-renamed")],
            {"device-1": _netbox_entry(label="rtr-1")},
        )
        match = results[0]
        self.assertEqual(match.verdict, "orange")
        self.assertEqual(len(match.diff_detail), 1)
        self.assertIn("label", match.diff_detail[0])
        self.assertIn("rtr-1-renamed", match.diff_detail[0])
        self.assertIn("rtr-1", match.diff_detail[0])

    def test_orange_on_location_mismatch(self):
        results = reconcile(
            [_opennms_node(location="Sydney")],
            {"device-1": _netbox_entry(location="Perth")},
        )
        match = results[0]
        self.assertEqual(match.verdict, "orange")
        self.assertEqual(len(match.diff_detail), 1)
        self.assertIn("location", match.diff_detail[0])

    def test_orange_reports_both_mismatches(self):
        results = reconcile(
            [_opennms_node(label="rtr-1-renamed", location="Sydney")],
            {"device-1": _netbox_entry(label="rtr-1", location="Perth")},
        )
        match = results[0]
        self.assertEqual(match.verdict, "orange")
        self.assertEqual(len(match.diff_detail), 2)

    def test_red_on_unresolved_foreign_id(self):
        results = reconcile([_opennms_node(foreign_id="ghost")], {})
        match = results[0]
        self.assertEqual(match.verdict, "red")
        self.assertEqual(match.diff_detail, [])
        self.assertEqual(match.matched_kind, "")
        self.assertIsNone(match.matched_pk)

    def test_red_on_blank_foreign_id(self):
        results = reconcile(
            [_opennms_node(foreign_id="")], {"device-1": _netbox_entry()}
        )
        self.assertEqual(results[0].verdict, "red")

    def test_matched_kind_vm(self):
        results = reconcile(
            [_opennms_node()],
            {"device-1": _netbox_entry(kind="vm", pk=7)},
        )
        match = results[0]
        self.assertEqual(match.verdict, "green")
        self.assertEqual(match.matched_kind, "vm")
        self.assertEqual(match.matched_pk, 7)

    def test_non_dict_entries_are_skipped(self):
        results = reconcile(
            ["not-a-dict", _opennms_node()], {"device-1": _netbox_entry()}
        )
        self.assertEqual(len(results), 1)

    def test_empty_input_returns_empty(self):
        self.assertEqual(reconcile([], {}), [])

    def test_preserves_opennms_node_id_and_foreign_source(self):
        results = reconcile(
            [_opennms_node(node_id=99, foreign_source="acme")],
            {"device-1": _netbox_entry()},
        )
        match = results[0]
        self.assertEqual(match.opennms_node_id, 99)
        self.assertEqual(match.foreign_source, "acme")

    def test_created_is_none_when_create_time_absent(self):
        results = reconcile([_opennms_node()], {"device-1": _netbox_entry()})
        self.assertIsNone(results[0].created)

    def test_created_parses_create_time_on_green_and_red(self):
        green = reconcile(
            [_opennms_node(create_time="2026-08-01T10:00:00+00:00")],
            {"device-1": _netbox_entry()},
        )
        red = reconcile(
            [
                _opennms_node(
                    foreign_id="ghost", create_time="2026-08-01T10:00:00+00:00"
                )
            ],
            {},
        )
        self.assertIsNotNone(green[0].created)
        self.assertIsNotNone(red[0].created)


class ParseNodeCreatedTest(SimpleTestCase):
    def test_missing_create_time_returns_none(self):
        self.assertIsNone(_parse_node_created({}))

    def test_blank_create_time_returns_none(self):
        self.assertIsNone(_parse_node_created({"createTime": ""}))

    def test_unparseable_create_time_returns_none(self):
        self.assertIsNone(_parse_node_created({"createTime": "not-a-date"}))

    def test_aware_create_time_is_preserved(self):
        parsed = _parse_node_created({"createTime": "2026-08-01T10:00:00+00:00"})
        self.assertFalse(timezone.is_naive(parsed))

    def test_naive_create_time_is_made_aware_as_utc(self):
        parsed = _parse_node_created({"createTime": "2026-08-01T10:00:00"})
        self.assertFalse(timezone.is_naive(parsed))
