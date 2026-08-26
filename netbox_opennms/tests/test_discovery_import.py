# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Tests for ``DiscoveredNodeImportView`` (issue #9).

Covers creating a new Device/VM from a red Discovery row: the full object
graph (interfaces, IPs, primary IP, Monitoring Override + services), rejection
on an incomplete submission, all-or-nothing rollback on an ADR-0001 Server
Conflict, permission enforcement per object type, and that a successful
import persists the same way a manual link (#8) does so a re-scan leaves it
alone. Follows ``test_discovery_link.py`` and ``test_forms.py`` as prior art.
"""

from unittest import mock

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.contrib.auth.models import Permission, User
from django.test import TestCase
from django.urls import reverse

from netbox_opennms.membership import Resolution, ServerConflict
from netbox_opennms.models import (
    DiscoveredNode,
    MonitoredInterface,
    MonitoredService,
    MonitoringOverride,
    OpenNMSServer,
    Requisition,
)


def _node_detail():
    return {
        "assetRecord": {
            "serialNumber": "SN123",
            "manufacturer": "Acme",
            "operatingSystem": "Linux",
        },
        "categories": {"category": [{"name": "Router"}]},
    }


def _ip_interfaces():
    return [
        {"ipAddress": "10.0.0.5", "snmpPrimary": "P"},
        {"ipAddress": "10.0.0.6", "snmpPrimary": "S"},
    ]


def _services_for(node_id, ip):
    return {
        "10.0.0.5": [{"serviceType": {"name": "ICMP"}}],
        "10.0.0.6": [{"serviceType": {"name": "SNMP"}}],
    }[ip]


class DiscoveredNodeImportViewTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.server = OpenNMSServer.objects.create(
            name="Acme", url="https://onms.example", username="svc", password="x"
        )
        cls.site = Site.objects.create(name="Site 1", slug="site-1")
        cls.mfr = Manufacturer.objects.create(name="Acme", slug="acme")
        cls.device_type = DeviceType.objects.create(
            manufacturer=cls.mfr, model="Model 1", slug="model-1"
        )
        cls.role = DeviceRole.objects.create(name="Router", slug="router")

    def setUp(self):
        self.node = DiscoveredNode.objects.create(
            server=self.server,
            opennms_node_id=1,
            label="rtr-1",
            foreign_id="ghost",
            location="Perth",
            verdict="red",
        )
        self.user = User.objects.create_user(username="tester")
        self.user.user_permissions.add(
            Permission.objects.get(
                codename="add_device", content_type__app_label="dcim"
            ),
            Permission.objects.get(
                codename="add_virtualmachine",
                content_type__app_label="virtualization",
            ),
        )
        self.client.force_login(self.user)
        patcher = mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
        mock_from_server = patcher.start()
        self.addCleanup(patcher.stop)
        self.mock_client = mock_from_server.return_value.__enter__.return_value
        self.mock_client.get_node.return_value = _node_detail()
        self.mock_client.list_ip_interfaces.return_value = _ip_interfaces()
        self.mock_client.list_services.side_effect = _services_for

    def _url(self, node=None):
        return reverse(
            "plugins:netbox_opennms:discoverednode_import",
            args=[(node or self.node).pk],
        )

    def _device_post(self, **overrides):
        data = {
            "kind": "device",
            "name": "rtr-1",
            "site": self.site.pk,
            "role": self.role.pk,
            "device_type": self.device_type.pk,
            "location": "Perth",
        }
        data.update(overrides)
        return data

    def test_get_shows_detected_proposal(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        proposal = response.context["proposal"]
        self.assertEqual(proposal.manufacturer.value, self.mfr)
        self.assertEqual(proposal.role.value, self.role)
        self.assertIsNone(proposal.tenant.value)
        self.assertEqual(proposal.tenant.detected, "")

    def test_import_creates_full_device_graph(self):
        response = self.client.post(self._url(), self._device_post())
        errors = (
            response.context["form"].errors if response.status_code == 200 else None
        )
        self.assertEqual(response.status_code, 302, errors)

        device = Device.objects.get(name="rtr-1")
        self.assertEqual(device.site, self.site)
        self.assertEqual(device.role, self.role)
        self.assertEqual(device.device_type, self.device_type)
        self.assertEqual(device.interfaces.count(), 2)
        self.assertIsNotNone(device.primary_ip4)
        self.assertEqual(str(device.primary_ip4.address), "10.0.0.5/32")

        override = MonitoringOverride.objects.get(
            assigned_object_id=device.pk,
            assigned_object_type__model="device",
        )
        self.assertEqual(override.management_ip, device.primary_ip4)
        self.assertEqual(override.location, "Perth")
        self.assertEqual(
            MonitoredInterface.objects.filter(override=override).count(), 1
        )
        self.assertEqual(
            MonitoredService.objects.filter(override=override).count(), 2
        )

        self.node.refresh_from_db()
        self.assertEqual(self.node.resolution, "linked")
        self.assertEqual(self.node.matched_object, device)
        self.assertEqual(self.node.verdict, "green")

    def test_import_creates_vm_without_device_type(self):
        response = self.client.post(
            self._url(), {"kind": "vm", "name": "vm-1", "location": "Perth"}
        )
        self.assertEqual(response.status_code, 302)
        from virtualization.models import VirtualMachine

        vm = VirtualMachine.objects.get(name="vm-1")
        self.assertIsNotNone(vm.primary_ip4)

    def test_rejects_incomplete_device_submission(self):
        response = self.client.post(
            self._url(), {"kind": "device", "name": "rtr-1", "location": "Perth"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Device.objects.filter(name="rtr-1").exists())
        self.node.refresh_from_db()
        self.assertEqual(self.node.resolution, "scanned")

    def test_rejects_invalid_opennms_location_name(self):
        response = self.client.post(
            self._url(), self._device_post(location="bad location!")
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Device.objects.filter(name="rtr-1").exists())

    def test_rejects_and_rolls_back_on_server_conflict(self):
        requisition = Requisition.objects.create(name="fs-1")
        conflict = ServerConflict(servers=["A", "B"])
        resolution = Resolution(
            foreign_source="fs-1",
            requisition=requisition,
            server_conflict=conflict,
        )
        with (
            mock.patch(
                "netbox_opennms.import_node.matching_requisitions",
                return_value=[requisition],
            ),
            mock.patch(
                "netbox_opennms.import_node.resolve_all", return_value=[resolution]
            ),
        ):
            response = self.client.post(self._url(), self._device_post())
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Device.objects.filter(name="rtr-1").exists())
        self.node.refresh_from_db()
        self.assertEqual(self.node.resolution, "scanned")

    def test_requires_add_permission_for_chosen_kind(self):
        self.user.user_permissions.remove(
            Permission.objects.get(
                codename="add_device", content_type__app_label="dcim"
            )
        )
        response = self.client.post(self._url(), self._device_post())
        self.assertEqual(response.status_code, 403)

    def test_get_requires_some_add_permission(self):
        self.user.user_permissions.clear()
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 403)

    def test_client_failure_shows_error(self):
        from netbox_opennms.client import OpenNMSError

        self.mock_client.get_node.side_effect = OpenNMSError("unreachable")
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "unreachable")


class ImportedNodeRescanTest(TestCase):
    """A row resolved by import must survive a re-scan untouched (issue #9/#8)."""

    @classmethod
    def setUpTestData(cls):
        cls.server = OpenNMSServer.objects.create(
            name="Acme", url="https://onms.example", username="svc", password="x"
        )

    def setUp(self):
        self.node = DiscoveredNode.objects.create(
            server=self.server,
            opennms_node_id=1,
            label="rtr-1",
            foreign_id="ghost",
            verdict="red",
        )
        site = Site.objects.create(name="Site 1", slug="site-1")
        mfr = Manufacturer.objects.create(name="Acme", slug="acme")
        dt = DeviceType.objects.create(
            manufacturer=mfr, model="Model 1", slug="model-1"
        )
        role = DeviceRole.objects.create(name="Router", slug="router")
        self.device = Device.objects.create(
            name="rtr-1", device_type=dt, role=role, site=site
        )
        self.node.link_to(self.device)

        self.user = User.objects.create_user(username="tester")
        self.user.user_permissions.add(
            Permission.objects.get(
                codename="add_discoverednode", content_type__app_label="netbox_opennms"
            )
        )
        self.client.force_login(self.user)

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_rescan_leaves_imported_row_untouched(self, mock_from_server):
        client = mock_from_server.return_value.__enter__.return_value
        client.list_nodes.return_value = [
            {
                "id": 1,
                "label": "rtr-1",
                "foreignSource": "fs",
                "foreignId": "ghost",
                "location": "Perth",
            }
        ]
        response = self.client.post(
            reverse("plugins:netbox_opennms:opennmsserver_scan", args=[self.server.pk])
        )
        self.assertEqual(response.status_code, 302)
        self.node.refresh_from_db()
        self.assertEqual(self.node.resolution, "linked")
        self.assertEqual(self.node.matched_object, self.device)
