# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Tests for the pure Discovery reconciler (issue #7)."""

from django.test import SimpleTestCase

from netbox_opennms.scan import NodeMatch, reconcile


def _opennms_node(
    node_id=1,
    label="rtr-1",
    foreign_source="fs",
    foreign_id="device-1",
    location="Perth",
):
    return {
        "id": node_id,
        "label": label,
        "foreignSource": foreign_source,
        "foreignId": foreign_id,
        "location": location,
    }


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
