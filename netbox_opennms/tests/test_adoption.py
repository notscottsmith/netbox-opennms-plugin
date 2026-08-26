# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Tests for label-based adoption of pre-existing OpenNMS nodes (issue #4)."""

from django.test import SimpleTestCase

from netbox_opennms.adoption import adopt_foreign_ids, existing_foreign_ids_by_label
from netbox_opennms.membership import NodeSpec


def _node(label, foreign_id):
    return NodeSpec(label, foreign_id, "")


class ExistingForeignIdsByLabelTest(SimpleTestCase):
    def test_none_yields_empty(self):
        self.assertEqual(existing_foreign_ids_by_label(None), {})

    def test_maps_label_to_foreign_id(self):
        current = {"node": [{"node-label": "rtr-1", "foreign-id": "legacy-42"}]}
        self.assertEqual(
            existing_foreign_ids_by_label(current), {"rtr-1": ["legacy-42"]}
        )

    def test_single_node_not_wrapped_in_list(self):
        # OpenNMS's v1 REST serializer unwraps a lone collection member.
        current = {"node": {"node-label": "rtr-1", "foreign-id": "legacy-42"}}
        self.assertEqual(
            existing_foreign_ids_by_label(current), {"rtr-1": ["legacy-42"]}
        )

    def test_duplicate_label_collects_both_ids(self):
        current = {
            "node": [
                {"node-label": "rtr-1", "foreign-id": "legacy-42"},
                {"node-label": "rtr-1", "foreign-id": "legacy-43"},
            ]
        }
        self.assertEqual(
            existing_foreign_ids_by_label(current), {"rtr-1": ["legacy-42", "legacy-43"]}
        )

    def test_node_missing_label_or_id_skipped(self):
        current = {
            "node": [
                {"node-label": "rtr-1"},
                {"foreign-id": "legacy-1"},
            ]
        }
        self.assertEqual(existing_foreign_ids_by_label(current), {})


class AdoptForeignIdsTest(SimpleTestCase):
    def test_no_existing_state_is_a_noop(self):
        node = _node("rtr-1", "netbox-device-1")
        warnings = adopt_foreign_ids([node], {})
        self.assertEqual(node.foreign_id, "netbox-device-1")
        self.assertEqual(warnings, [])

    def test_unambiguous_match_adopts(self):
        node = _node("rtr-1", "netbox-device-1")
        warnings = adopt_foreign_ids([node], {"rtr-1": ["legacy-42"]})
        self.assertEqual(node.foreign_id, "legacy-42")
        self.assertEqual(warnings, [])

    def test_no_label_match_keeps_derived_id(self):
        node = _node("rtr-1", "netbox-device-1")
        warnings = adopt_foreign_ids([node], {"someone-else": ["legacy-42"]})
        self.assertEqual(node.foreign_id, "netbox-device-1")
        self.assertEqual(warnings, [])

    def test_multiple_existing_nodes_same_label_skips_and_warns(self):
        node = _node("rtr-1", "netbox-device-1")
        warnings = adopt_foreign_ids(
            [node], {"rtr-1": ["legacy-42", "legacy-43"]}
        )
        self.assertEqual(node.foreign_id, "netbox-device-1")
        self.assertEqual(len(warnings), 1)
        self.assertIn("rtr-1", warnings[0])

    def test_multiple_desired_nodes_same_label_skips_and_warns(self):
        node_a = _node("rtr-1", "netbox-device-1")
        node_b = _node("rtr-1", "netbox-device-2")
        warnings = adopt_foreign_ids(
            [node_a, node_b], {"rtr-1": ["legacy-42"]}
        )
        self.assertEqual(node_a.foreign_id, "netbox-device-1")
        self.assertEqual(node_b.foreign_id, "netbox-device-2")
        self.assertEqual(len(warnings), 2)

    def test_already_matching_id_is_a_noop(self):
        node = _node("rtr-1", "legacy-42")
        warnings = adopt_foreign_ids([node], {"rtr-1": ["legacy-42"]})
        self.assertEqual(node.foreign_id, "legacy-42")
        self.assertEqual(warnings, [])
