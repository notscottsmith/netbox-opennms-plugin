# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Tests for the Device/VirtualMachine "Node Links" tab (issue #15).

Covers the provenance lookup (``DiscoveredNode.for_object``), the live
``enlinkd`` call + parse, degrading to no links on an OpenNMS error, and that
the tab is absent from an object's page when there's no Discovery match — the
``hide_if_empty`` badge gate. Follows ``test_discovery_link.py`` as prior art.
"""

from unittest import mock

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from virtualization.models import Cluster, ClusterType, VirtualMachine

from netbox_opennms.models import DiscoveredNode, OpenNMSServer

LLDP_PAYLOAD = {
    "lldpLinkNodes": [
        {
            "lldpLocalPort": "Gi0/1",
            "lldpRemChassisId": "aa:bb:cc:dd:ee:ff",
            "ldpRemPort": "Gi0/2",
        }
    ]
}


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


class NodeLinksTabTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.server = OpenNMSServer.objects.create(
            name="Acme", url="https://onms.example", username="svc", password="x"
        )

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
            verdict="red",
        )
        node.link_to(target)
        return node

    def _device_url(self, device):
        return reverse("dcim:device_opennms_node_links", args=[device.pk])

    def _vm_url(self, vm):
        return reverse(
            "virtualization:virtualmachine_opennms_node_links", args=[vm.pk]
        )

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_device_tab_shows_links_for_matched_node(self, mock_from_server):
        device = _device()
        node = self._node_for(device)
        client = mock_from_server.return_value.__enter__.return_value
        client.get_node_links.return_value = LLDP_PAYLOAD

        response = self.client.get(self._device_url(device))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["discovered_node"], node)
        links = response.context["links"]
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0].protocol, "LLDP")
        self.assertEqual(links[0].local_port, "Gi0/1")
        client.get_node_links.assert_called_once_with(1)

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_vm_tab_shows_links_for_matched_node(self, mock_from_server):
        vm = _vm()
        node = self._node_for(vm)
        client = mock_from_server.return_value.__enter__.return_value
        client.get_node_links.return_value = LLDP_PAYLOAD

        response = self.client.get(self._vm_url(vm))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["discovered_node"], node)
        self.assertEqual(len(response.context["links"]), 1)

    def test_tab_content_for_unmatched_device_has_no_node_or_links(self):
        device = _device()
        response = self.client.get(self._device_url(device))
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["discovered_node"])
        self.assertEqual(response.context["links"], [])

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_opennms_error_degrades_to_no_links(self, mock_from_server):
        from netbox_opennms.client import OpenNMSError

        device = _device()
        self._node_for(device)
        client = mock_from_server.return_value.__enter__.return_value
        client.get_node_links.side_effect = OpenNMSError("unreachable")

        response = self.client.get(self._device_url(device))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["links"], [])

    def test_tab_hidden_from_device_page_without_a_discovery_match(self):
        device = _device()
        response = self.client.get(device.get_absolute_url())
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(
            self._device_url(device).encode(), response.content
        )

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_tab_visible_from_device_page_with_links(self, mock_from_server):
        device = _device()
        self._node_for(device)
        client = mock_from_server.return_value.__enter__.return_value
        client.get_node_links.return_value = LLDP_PAYLOAD

        response = self.client.get(device.get_absolute_url())

        self.assertEqual(response.status_code, 200)
        self.assertIn(self._device_url(device).encode(), response.content)

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_tab_hidden_from_device_page_when_node_has_no_links(
        self, mock_from_server
    ):
        device = _device()
        self._node_for(device)
        client = mock_from_server.return_value.__enter__.return_value
        client.get_node_links.return_value = {}

        response = self.client.get(device.get_absolute_url())

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(
            self._device_url(device).encode(), response.content
        )
