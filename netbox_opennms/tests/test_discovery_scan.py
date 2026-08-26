# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Tests for ``OpenNMSServerScanView`` (issue #7).

Covers the view's upsert-by-``(server, opennms_node_id)`` behaviour: a scan
populates ``DiscoveredNode`` rows, a re-scan against unchanged OpenNMS state
refreshes those same rows rather than duplicating them, and a node no longer
present on the server is removed.
"""

from unittest import mock

from django.contrib.auth.models import Permission, User
from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from netbox_opennms.client import OpenNMSError
from netbox_opennms.models import DiscoveredNode, OpenNMSServer

ADD_PERM = "netbox_opennms.add_discoverednode"


def _node(node_id, label="rtr-1", foreign_id="ghost"):
    return {
        "id": node_id,
        "label": label,
        "foreignSource": "fs",
        "foreignId": foreign_id,
        "location": "Perth",
    }


class OpenNMSServerScanViewTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.server = OpenNMSServer.objects.create(
            name="Acme", url="https://onms.example", username="svc", password="x"
        )

    def setUp(self):
        self.user = User.objects.create_user(username="tester")
        self.user.user_permissions.add(
            Permission.objects.get(
                codename="add_discoverednode", content_type__app_label="netbox_opennms"
            )
        )
        self.client.force_login(self.user)

    def _url(self):
        return reverse(
            "plugins:netbox_opennms:opennmsserver_scan", args=[self.server.pk]
        )

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def _scan(self, nodes, mock_from_server):
        client = mock_from_server.return_value.__enter__.return_value
        client.list_nodes.return_value = nodes
        return self.client.post(self._url())

    def test_scan_creates_rows_for_each_node(self):
        response = self._scan([_node(1, foreign_id="a"), _node(2, foreign_id="b")])
        self.assertEqual(response.status_code, 302)
        self.assertEqual(DiscoveredNode.objects.filter(server=self.server).count(), 2)

    def test_rescan_upserts_without_duplicating(self):
        self._scan([_node(1, foreign_id="a"), _node(2, foreign_id="b")])
        first_ids = set(
            DiscoveredNode.objects.filter(server=self.server)
            .values_list("pk", flat=True)
        )
        self._scan([_node(1, foreign_id="a"), _node(2, foreign_id="b")])
        second_ids = set(
            DiscoveredNode.objects.filter(server=self.server)
            .values_list("pk", flat=True)
        )
        self.assertEqual(first_ids, second_ids)
        self.assertEqual(len(second_ids), 2)

    def test_rescan_drops_nodes_no_longer_present(self):
        self._scan([_node(1, foreign_id="a"), _node(2, foreign_id="b")])
        self._scan([_node(1, foreign_id="a")])
        remaining = DiscoveredNode.objects.filter(server=self.server)
        self.assertEqual(remaining.count(), 1)
        self.assertEqual(remaining.get().opennms_node_id, 1)

    def test_rescan_refreshes_changed_fields(self):
        self._scan([_node(1, label="rtr-1", foreign_id="a")])
        self._scan([_node(1, label="rtr-1-renamed", foreign_id="a")])
        row = DiscoveredNode.objects.get(server=self.server, opennms_node_id=1)
        self.assertEqual(row.label, "rtr-1-renamed")

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_client_failure_creates_no_rows_and_shows_error(self, mock_from_server):
        client = mock_from_server.return_value.__enter__.return_value
        client.list_nodes.side_effect = OpenNMSError("unreachable")
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)
        self.assertEqual(DiscoveredNode.objects.filter(server=self.server).count(), 0)
        messages = list(get_messages(response.wsgi_request))
        self.assertTrue(any("unreachable" in str(m) for m in messages))

    def test_requires_add_permission(self):
        self.user.user_permissions.clear()
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, 403)
