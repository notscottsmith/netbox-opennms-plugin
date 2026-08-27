# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Tests for the Requisition-level bulk "One-Time Sync" view (issue #24).

Covers the GET aggregate preview (built from mocked OpenNMS data across
several nodes), the POST commit (delegating to ``reverse_sync.run_reverse_sync``
over every matched node), the unhealthy-server and unresolved-server gates
(AC #5), and permission enforcement. Follows ``test_reverse_sync_view.py`` as
prior art for fixtures and mocking conventions.
"""

from unittest import mock

from dcim.models import (
    Device,
    DeviceRole,
    DeviceType,
    Manufacturer,
    Site,
)
from django.contrib.auth.models import User
from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse

from netbox_opennms.models import DiscoveredNode, OpenNMSServer, Requisition

FILTER = {"site": ["raleigh"]}


def _device(name="rtr-1"):
    site = Site.objects.create(name=f"Site {name}", slug=f"site-{name}")
    mfr = Manufacturer.objects.create(name=f"Vendor {name}", slug=f"vendor-{name}")
    dt = DeviceType.objects.create(manufacturer=mfr, model=name, slug=f"model-{name}")
    role, _ = DeviceRole.objects.get_or_create(
        name="Router", defaults={"slug": "router"}
    )
    return Device.objects.create(name=name, device_type=dt, role=role, site=site)


class RequisitionOpenNMSPullViewTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.server = OpenNMSServer.objects.create(
            name="Acme", url="https://onms.example", username="svc", password="x"
        )
        cls.requisition = Requisition.objects.create(name="fs-1", filter_params=FILTER)

    def setUp(self):
        self.user = User.objects.create_user(
            username="tester", password="pw", is_superuser=True
        )
        self.client.force_login(self.user)

    def _node_for(self, target, node_id=1):
        node = DiscoveredNode.objects.create(
            server=self.server,
            opennms_node_id=node_id,
            label=target.name,
            foreign_source="fs-1",
            verdict="red",
        )
        node.link_to(target)
        return node

    def _url(self):
        return reverse(
            "plugins:netbox_opennms:requisition_opennms_pull",
            args=[self.requisition.pk],
        )

    @mock.patch("netbox_opennms.views.target_server_for")
    @mock.patch("netbox_opennms.views.OpenNMSClient.from_server")
    def test_get_shows_aggregate_plan_across_matched_nodes(
        self, mock_from_server, mock_target_server_for
    ):
        mock_target_server_for.return_value = self.server
        device_a = _device("rtr-a")
        device_b = _device("rtr-b")
        self._node_for(device_a, node_id=1)
        self._node_for(device_b, node_id=2)
        client = mock_from_server.return_value.__enter__.return_value
        client.list_snmp_interfaces.return_value = [{"ifName": "eth0"}]
        client.get_node_links.return_value = {}

        response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200)
        rows = response.context["rows"]
        self.assertEqual(len(rows), 2)
        self.assertTrue(response.context["has_changes"])
        self.assertIsNone(response.context["error"])

    @mock.patch("netbox_opennms.views.target_server_for")
    def test_get_excludes_unmatched_nodes(self, mock_target_server_for):
        mock_target_server_for.return_value = self.server
        DiscoveredNode.objects.create(
            server=self.server,
            opennms_node_id=3,
            label="rtr-c",
            foreign_source="fs-1",
            verdict="red",
        )

        response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["rows"], [])
        self.assertFalse(response.context["has_changes"])

    @mock.patch("netbox_opennms.views.target_server_for")
    def test_get_reports_error_when_target_server_unresolved(
        self, mock_target_server_for
    ):
        mock_target_server_for.return_value = None

        response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["rows"])
        self.assertIn("could not be resolved", response.context["error"])

    @mock.patch("netbox_opennms.views.target_server_for")
    def test_get_reports_error_when_server_unhealthy(self, mock_target_server_for):
        mock_target_server_for.return_value = self.server
        self.server.last_check_status = "failed"
        self.server.save()

        response = self.client.get(self._url())

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["rows"])
        self.assertIn("unhealthy", response.context["error"])

    @mock.patch("netbox_opennms.views.target_server_for")
    @mock.patch("netbox_opennms.views.run_reverse_sync")
    @mock.patch("netbox_opennms.views.OpenNMSClient.from_server")
    def test_post_commits_and_reports_per_node_success_and_failure(
        self, mock_from_server, mock_run_reverse_sync, mock_target_server_for
    ):
        mock_target_server_for.return_value = self.server
        device_a = _device("rtr-d")
        device_b = _device("rtr-e")
        node_a = self._node_for(device_a, node_id=4)
        node_b = self._node_for(device_b, node_id=5)
        client = mock_from_server.return_value.__enter__.return_value
        client.list_snmp_interfaces.return_value = [{"ifName": "eth0"}]
        client.get_node_links.return_value = {}

        from netbox_opennms.reverse_sync import ReverseSyncResult

        mock_run_reverse_sync.return_value = [
            ReverseSyncResult(node_a, True, interfaces_created=1),
            ReverseSyncResult(node_b, False, "boom"),
        ]

        response = self.client.post(self._url())

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, self.requisition.get_absolute_url())
        mock_run_reverse_sync.assert_called_once()
        called_nodes = mock_run_reverse_sync.call_args.args[1]
        self.assertEqual({n.pk for n in called_nodes}, {node_a.pk, node_b.pk})
        messages = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertTrue(any("1 node(s)" in m for m in messages))
        self.assertTrue(any("boom" in m for m in messages))

    @mock.patch("netbox_opennms.views.target_server_for")
    def test_post_with_no_matched_nodes_redirects_with_info(
        self, mock_target_server_for
    ):
        mock_target_server_for.return_value = self.server

        response = self.client.post(self._url())

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, self.requisition.get_absolute_url())

    def test_get_requires_permission(self):
        user = User.objects.create_user(username="nopower", password="pw")
        self.client.force_login(user)

        response = self.client.get(self._url())

        self.assertEqual(response.status_code, 403)
