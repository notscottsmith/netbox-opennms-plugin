# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Tests for the Device/VirtualMachine "Pull OpenNMS data" view (issue #23).

Covers the GET preview (plan built from mocked OpenNMS data), the POST commit
(delegating to ``reverse_sync.run_reverse_sync``), the unhealthy-server gate
(AC #9), and the unmatched-object case. Follows ``test_node_links_view.py`` as
prior art for fixtures and mocking conventions.
"""

from unittest import mock

from dcim.models import (
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


class OpenNMSPullViewTest(TestCase):
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
        return reverse("dcim:device_opennms_pull", args=[device.pk])

    def _vm_url(self, vm):
        return reverse("virtualization:virtualmachine_opennms_pull", args=[vm.pk])

    @mock.patch("netbox_opennms.views.OpenNMSClient.from_server")
    def test_get_shows_plan_for_matched_device(self, mock_from_server):
        device = _device()
        self._node_for(device)
        Interface.objects.create(device=device, name="eth0", type="virtual")
        client = mock_from_server.return_value.__enter__.return_value
        client.list_snmp_interfaces.return_value = [{"ifName": "eth1"}]
        client.get_node_links.return_value = {}

        response = self.client.get(self._device_url(device))

        self.assertEqual(response.status_code, 200)
        plan = response.context["plan"]
        self.assertEqual(plan.interfaces[0].action, "create")
        self.assertIsNone(response.context["error"])

    @mock.patch("netbox_opennms.views.OpenNMSClient.from_server")
    def test_get_shows_plan_for_matched_vm(self, mock_from_server):
        vm = _vm()
        self._node_for(vm)
        client = mock_from_server.return_value.__enter__.return_value
        client.list_snmp_interfaces.return_value = []
        client.get_node_links.return_value = {}

        response = self.client.get(self._vm_url(vm))

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["error"])

    def test_get_reports_error_when_no_discovery_match(self):
        device = _device()

        response = self.client.get(self._device_url(device))

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["plan"])
        self.assertIn("No OpenNMS Discovery match", response.context["error"])

    def test_get_reports_error_when_server_unhealthy(self):
        device = _device()
        self._node_for(device)
        self.server.last_check_status = "failed"
        self.server.save()

        response = self.client.get(self._device_url(device))

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.context["plan"])
        self.assertIn("unhealthy", response.context["error"])

    @mock.patch("netbox_opennms.views.OpenNMSClient.from_server")
    def test_post_commits_plan_and_redirects(self, mock_from_server):
        device = _device()
        self._node_for(device)
        client = mock_from_server.return_value.__enter__.return_value
        client.list_snmp_interfaces.return_value = [{"ifName": "eth0"}]
        client.get_node_links.return_value = {}

        response = self.client.post(self._device_url(device))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, device.get_absolute_url())
        self.assertEqual(Interface.objects.filter(device=device).count(), 1)

    def test_post_without_discovery_match_redirects_with_error(self):
        device = _device()

        response = self.client.post(self._device_url(device))

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, device.get_absolute_url())

    def test_get_requires_permission(self):
        user = User.objects.create_user(username="nopower", password="pw")
        self.client.force_login(user)
        device = _device()
        self._node_for(device)

        response = self.client.get(self._device_url(device))

        self.assertEqual(response.status_code, 403)
