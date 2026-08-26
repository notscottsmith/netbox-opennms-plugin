# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Tests for ``DiscoveredNodeLinkView`` (issue #8).

Covers the manual link/correct action: a link persists onto the row
(``resolution="linked"``, ``verdict="green"``), a later re-scan
(``OpenNMSServerScanView``, issue #7) leaves a linked row's match alone
instead of recomputing it from the node's Foreign ID, correcting an
existing link updates the same row rather than creating a duplicate, and
the action is gated on ``change_discoverednode``.
"""

from unittest import mock

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.contrib.auth.models import Permission, User
from django.test import TestCase
from django.urls import reverse
from virtualization.models import Cluster, ClusterType, VirtualMachine

from netbox_opennms.models import DiscoveredNode, OpenNMSServer

CHANGE_PERM = "netbox_opennms.change_discoverednode"


def _device(name="rtr-1"):
    site = Site.objects.create(name="Site 1", slug="site-1")
    mfr = Manufacturer.objects.create(name="Acme", slug="acme")
    dt = DeviceType.objects.create(manufacturer=mfr, model="Model 1", slug="model-1")
    role = DeviceRole.objects.create(name="Router", slug="router")
    return Device.objects.create(name=name, device_type=dt, role=role, site=site)


def _vm(name="vm-1"):
    cluster_type = ClusterType.objects.create(name="Type 1", slug="type-1")
    cluster = Cluster.objects.create(name="Cluster 1", type=cluster_type)
    return VirtualMachine.objects.create(name=name, cluster=cluster)


def _scan_node(node_id, label="rtr-1", foreign_id="ghost"):
    return {
        "id": node_id,
        "label": label,
        "foreignSource": "fs",
        "foreignId": foreign_id,
        "location": "Perth",
    }


class DiscoveredNodeLinkViewTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.server = OpenNMSServer.objects.create(
            name="Acme", url="https://onms.example", username="svc", password="x"
        )
        cls.node = DiscoveredNode.objects.create(
            server=cls.server,
            opennms_node_id=1,
            label="rtr-1",
            foreign_id="ghost",
            verdict="red",
        )

    def setUp(self):
        self.user = User.objects.create_user(username="tester")
        self.user.user_permissions.add(
            Permission.objects.get(
                codename="change_discoverednode",
                content_type__app_label="netbox_opennms",
            )
        )
        self.client.force_login(self.user)

    def _url(self, node=None):
        return reverse(
            "plugins:netbox_opennms:discoverednode_link",
            args=[(node or self.node).pk],
        )

    def test_link_persists(self):
        device = _device()
        response = self.client.post(self._url(), {"device": device.pk})
        self.assertEqual(response.status_code, 302)
        self.node.refresh_from_db()
        self.assertEqual(self.node.matched_object, device)
        self.assertEqual(self.node.resolution, "linked")
        self.assertEqual(self.node.verdict, "green")

    def test_link_to_virtual_machine_persists(self):
        vm = _vm()
        response = self.client.post(self._url(), {"virtual_machine": vm.pk})
        self.assertEqual(response.status_code, 302)
        self.node.refresh_from_db()
        self.assertEqual(self.node.matched_object, vm)
        self.assertEqual(self.node.resolution, "linked")

    def test_get_prefills_existing_vm_link_on_the_vm_field(self):
        vm = _vm()
        self.node.link_to(vm)
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        form = response.context["form"]
        self.assertEqual(form.initial.get("virtual_machine"), vm)
        self.assertNotIn("device", form.initial)

    def test_correcting_a_link_updates_the_same_row(self):
        first = _device("rtr-1")
        second = _device("rtr-2")
        self.client.post(self._url(), {"device": first.pk})
        self.client.post(self._url(), {"device": second.pk})
        self.assertEqual(DiscoveredNode.objects.filter(server=self.server).count(), 1)
        self.node.refresh_from_db()
        self.assertEqual(self.node.matched_object, second)

    def test_requires_exactly_one_of_device_or_virtual_machine(self):
        response = self.client.post(self._url(), {})
        self.assertEqual(response.status_code, 200)
        self.node.refresh_from_db()
        self.assertEqual(self.node.resolution, "scanned")

    def test_requires_change_permission(self):
        self.user.user_permissions.clear()
        device = _device()
        response = self.client.post(self._url(), {"device": device.pk})
        self.assertEqual(response.status_code, 403)


class LinkedNodeRescanTest(TestCase):
    """A linked row must survive ``OpenNMSServerScanView`` untouched (issue #8)."""

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
        self.device = _device()
        self.node = DiscoveredNode.objects.create(
            server=self.server,
            opennms_node_id=1,
            label="rtr-1",
            foreign_id="ghost",
            verdict="red",
        )
        self.node.link_to(self.device)

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_rescan_leaves_linked_row_untouched(self, mock_from_server):
        client = mock_from_server.return_value.__enter__.return_value
        client.list_nodes.return_value = [
            _scan_node(1, label="rtr-1-renamed", foreign_id="ghost")
        ]
        response = self.client.post(
            reverse(
                "plugins:netbox_opennms:opennmsserver_scan", args=[self.server.pk]
            )
        )
        self.assertEqual(response.status_code, 302)
        self.node.refresh_from_db()
        self.assertEqual(self.node.resolution, "linked")
        self.assertEqual(self.node.verdict, "green")
        self.assertEqual(self.node.matched_object, self.device)
        # A re-scan still refreshes non-match fields (label) on a linked row.
        self.assertEqual(self.node.label, "rtr-1-renamed")

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_rescan_drops_linked_row_no_longer_present(self, mock_from_server):
        client = mock_from_server.return_value.__enter__.return_value
        client.list_nodes.return_value = []
        self.client.post(
            reverse(
                "plugins:netbox_opennms:opennmsserver_scan", args=[self.server.pk]
            )
        )
        self.assertEqual(
            DiscoveredNode.objects.filter(server=self.server).count(), 0
        )
