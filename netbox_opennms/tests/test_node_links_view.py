# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Tests for the Device/VirtualMachine "Node Links" tab (issues #15, #16).

Covers the provenance lookup (``DiscoveredNode.for_object``), the live
``enlinkd`` call + parse, degrading to no links on an OpenNMS error, that the
tab is absent from an object's page when there's no Discovery match — the
``hide_if_empty`` badge gate (#15) — and turning a fully-matched link into a
real NetBox cable, with not-yet-actionable flagging otherwise (#16). Follows
``test_discovery_link.py`` as prior art.
"""

from unittest import mock

from dcim.models import (
    Cable,
    Device,
    DeviceRole,
    DeviceType,
    Interface,
    Manufacturer,
    Site,
)
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
    site = Site.objects.create(name=f"Site {name}", slug=f"site-{name}")
    mfr = Manufacturer.objects.create(name=f"Vendor {name}", slug=f"vendor-{name}")
    dt = DeviceType.objects.create(manufacturer=mfr, model=name, slug=f"model-{name}")
    role, _ = DeviceRole.objects.get_or_create(
        name="Router", defaults={"slug": "router"}
    )
    return Device.objects.create(name=name, device_type=dt, role=role, site=site)


def _vm(name="vm-1"):
    cluster_type, _ = ClusterType.objects.get_or_create(
        name="Type 1", defaults={"slug": "type-1"}
    )
    cluster = Cluster.objects.create(name=f"Cluster {name}", type=cluster_type)
    return VirtualMachine.objects.create(name=name, cluster=cluster)


def _interface(device, name):
    return Interface.objects.create(device=device, name=name, type="1000base-t")


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

    def _remote_matched_lldp_payload(self, remote_node_id):
        return {
            "lldpLinkNodes": [
                {
                    "lldpLocalPort": "Gi0/1",
                    "lldpRemChassisId": "aa:bb:cc:dd:ee:ff",
                    "ldpRemPort": "Gi0/2",
                    "lldpRemChassisIdUrl": (
                        f"element/linkednode.jsp?node={remote_node_id}"
                    ),
                }
            ]
        }

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

    # --- Create-cable action (issue #16) --------------------------------

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_link_row_actionable_when_both_endpoints_matched_with_interfaces(
        self, mock_from_server
    ):
        device = _device("rtr-1")
        remote_device = _device("rtr-2")
        self._node_for(device, node_id=1)
        remote_node = self._node_for(remote_device, node_id=2)
        local_iface = _interface(device, "Gi0/1")
        remote_iface = _interface(remote_device, "Gi0/2")
        client = mock_from_server.return_value.__enter__.return_value
        client.get_node_links.return_value = self._remote_matched_lldp_payload(
            remote_node.opennms_node_id
        )

        response = self.client.get(self._device_url(device))

        self.assertEqual(response.status_code, 200)
        row = response.context["link_rows"][0]
        self.assertEqual(row["local_interface"], local_iface)
        self.assertEqual(row["remote_interface"], remote_iface)
        self.assertIsNone(row["blocked_reason"])
        self.assertIn(b"Create cable", response.content)

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_link_row_not_actionable_when_remote_node_unmatched(
        self, mock_from_server
    ):
        device = _device("rtr-3")
        self._node_for(device, node_id=3)
        _interface(device, "Gi0/1")
        client = mock_from_server.return_value.__enter__.return_value
        client.get_node_links.return_value = self._remote_matched_lldp_payload(999)

        response = self.client.get(self._device_url(device))

        self.assertEqual(response.status_code, 200)
        row = response.context["link_rows"][0]
        self.assertIsNone(row["local_interface"])
        self.assertIsNotNone(row["blocked_reason"])
        self.assertNotIn(b"Create cable", response.content)

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_link_row_not_actionable_when_local_port_has_no_matching_interface(
        self, mock_from_server
    ):
        device = _device("rtr-4")
        remote_device = _device("rtr-5")
        self._node_for(device, node_id=4)
        remote_node = self._node_for(remote_device, node_id=5)
        _interface(remote_device, "Gi0/2")
        client = mock_from_server.return_value.__enter__.return_value
        client.get_node_links.return_value = self._remote_matched_lldp_payload(
            remote_node.opennms_node_id
        )

        response = self.client.get(self._device_url(device))

        self.assertEqual(response.status_code, 200)
        row = response.context["link_rows"][0]
        self.assertIsNone(row["local_interface"])
        self.assertIn("Gi0/1", row["blocked_reason"])

    def test_create_cable_creates_cable_and_redirects_to_local_device_tab(self):
        device = _device("rtr-6")
        remote_device = _device("rtr-7")
        local_iface = _interface(device, "Gi0/1")
        remote_iface = _interface(remote_device, "Gi0/2")

        response = self.client.post(
            reverse("plugins:netbox_opennms:node_link_create_cable"),
            {"local_interface": local_iface.pk, "remote_interface": remote_iface.pk},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, self._device_url(device))
        self.assertEqual(Cable.objects.count(), 1)
        local_iface.refresh_from_db()
        remote_iface.refresh_from_db()
        self.assertIsNotNone(local_iface.cable_id)
        self.assertEqual(local_iface.cable_id, remote_iface.cable_id)

    def test_create_cable_requires_permission(self):
        user = User.objects.create_user(username="nopower", password="pw")
        self.client.force_login(user)
        device = _device("rtr-8")
        remote_device = _device("rtr-9")
        local_iface = _interface(device, "Gi0/1")
        remote_iface = _interface(remote_device, "Gi0/2")

        response = self.client.post(
            reverse("plugins:netbox_opennms:node_link_create_cable"),
            {"local_interface": local_iface.pk, "remote_interface": remote_iface.pk},
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(Cable.objects.count(), 0)

    def test_create_cable_rejects_already_cabled_interface(self):
        device = _device("rtr-10")
        remote_device = _device("rtr-11")
        other_device = _device("rtr-12")
        local_iface = _interface(device, "Gi0/1")
        other_iface = _interface(other_device, "Gi0/1")
        Cable.objects.create(a_terminations=[local_iface], b_terminations=[other_iface])
        remote_iface = _interface(remote_device, "Gi0/2")

        response = self.client.post(
            reverse("plugins:netbox_opennms:node_link_create_cable"),
            {"local_interface": local_iface.pk, "remote_interface": remote_iface.pk},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(Cable.objects.count(), 1)
