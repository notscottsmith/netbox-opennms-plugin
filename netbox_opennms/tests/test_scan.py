# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Tests for the pure Discovery reconciler (issue #7) and node walking (#28)."""

from unittest import mock

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from netbox_opennms.models import DiscoveredNode, OpenNMSServer
from netbox_opennms.scan import NodeMatch, _parse_node_created, reconcile, walk_node


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


class WalkNodeTest(TestCase):
    """Tests for ``walk_node`` (issue #28, ADR 0007)."""

    def setUp(self):
        self.server = OpenNMSServer.objects.create(
            name="Acme", url="https://onms.example", username="svc", password="x"
        )
        self.node = DiscoveredNode.objects.create(
            server=self.server,
            opennms_node_id=1,
            label="rtr-1",
            foreign_id="device-1",
            location="Perth",
            verdict="red",
        )

    def test_persists_walked_payload_and_completeness_gaps(self):
        client = mock.Mock()
        client.get_node.return_value = {"assetRecord": {"manufacturer": "Acme"}}
        client.list_ip_interfaces.return_value = [
            {"ipAddress": "10.0.0.5", "snmpPrimary": "P"}
        ]
        client.list_services.return_value = [{"serviceType": {"name": "ICMP"}}]

        walk_node(client, self.node, overrides={})

        self.node.refresh_from_db()
        self.assertIsNotNone(self.node.walked_at)
        self.assertEqual(
            self.node.node_detail, {"assetRecord": {"manufacturer": "Acme"}}
        )
        self.assertEqual(
            self.node.ip_interfaces,
            [{"ipAddress": "10.0.0.5", "snmpPrimary": "P"}],
        )
        self.assertEqual(
            self.node.services_by_ip["10.0.0.5"],
            [{"serviceType": {"name": "ICMP"}}],
        )
        client.list_services.assert_called_once_with(1, "10.0.0.5")
        # manufacturer was detected and interfaces are present — neither is
        # a gap; role/site/platform have no corresponding data at all.
        self.assertNotIn("manufacturer", self.node.completeness_gaps)
        self.assertNotIn(
            "no IP interfaces (SNMP data may be unavailable)",
            self.node.completeness_gaps,
        )
        self.assertIn("role", self.node.completeness_gaps)
        self.assertIn("site", self.node.completeness_gaps)
        self.assertIn("platform", self.node.completeness_gaps)

    def test_no_interfaces_flagged_as_completeness_gap(self):
        client = mock.Mock()
        client.get_node.return_value = {}
        client.list_ip_interfaces.return_value = []

        walk_node(client, self.node, overrides={})

        self.node.refresh_from_db()
        self.assertIn(
            "no IP interfaces (SNMP data may be unavailable)",
            self.node.completeness_gaps,
        )
        client.list_services.assert_not_called()

    def test_missing_node_detail_falls_back_to_empty_dict(self):
        client = mock.Mock()
        client.get_node.return_value = None
        client.list_ip_interfaces.return_value = []

        walk_node(client, self.node, overrides={})

        self.node.refresh_from_db()
        self.assertEqual(self.node.node_detail, {})
