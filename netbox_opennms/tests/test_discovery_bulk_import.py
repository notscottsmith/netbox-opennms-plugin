# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Tests for ``DiscoveredNodeBulkImportView`` (issue #10).

Covers importing several red Discovery rows at once with one shared,
explicitly-chosen field set: successful batch creation, per-row rejection on
an ADR-0001 Server Conflict without failing the rest of the batch, that no
per-row auto-detection path (``import_node._propose_field`` et al.) is ever
reachable from the bulk flow, and the same permission gating as single import
(#9). Follows ``test_discovery_import.py`` as prior art.
"""

from unittest import mock

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.contrib.auth.models import Permission, User
from django.test import TestCase
from django.urls import reverse

from netbox_opennms.membership import Resolution, ServerConflict
from netbox_opennms.models import DiscoveredNode, OpenNMSServer, Requisition


def _ip_interfaces_for(node_id):
    return {
        1: [{"ipAddress": "10.0.0.5", "snmpPrimary": "P"}],
        2: [{"ipAddress": "10.0.0.6", "snmpPrimary": "P"}],
    }[node_id]


class DiscoveredNodeBulkImportViewTest(TestCase):
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
        self.node1 = DiscoveredNode.objects.create(
            server=self.server,
            opennms_node_id=1,
            label="rtr-1",
            foreign_id="ghost-1",
            location="Perth",
            verdict="red",
        )
        self.node2 = DiscoveredNode.objects.create(
            server=self.server,
            opennms_node_id=2,
            label="rtr-2",
            foreign_id="ghost-2",
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
        self.mock_client.list_ip_interfaces.side_effect = _ip_interfaces_for
        self.mock_client.list_services.return_value = []

    def _url(self):
        return reverse("plugins:netbox_opennms:discoverednode_bulk_import")

    def _batch_data(self, nodes, **overrides):
        data = {
            "nodes": [str(node.pk) for node in nodes],
            "kind": "device",
            "site": self.site.pk,
            "role": self.role.pk,
            "device_type": self.device_type.pk,
            "location": "Perth",
        }
        data.update(overrides)
        return data

    def test_get_lists_only_unresolved_red_rows(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        candidates = list(response.context["candidates"])
        self.assertCountEqual(candidates, [self.node1, self.node2])

    def test_batch_import_creates_both_with_shared_fields(self):
        response = self.client.post(
            self._url(), self._batch_data([self.node1, self.node2])
        )
        self.assertEqual(response.status_code, 302)

        for name in ("rtr-1", "rtr-2"):
            device = Device.objects.get(name=name)
            self.assertEqual(device.site, self.site)
            self.assertEqual(device.role, self.role)
            self.assertEqual(device.device_type, self.device_type)
            self.assertIsNotNone(device.primary_ip4)

        self.node1.refresh_from_db()
        self.node2.refresh_from_db()
        self.assertEqual(self.node1.resolution, "linked")
        self.assertEqual(self.node2.resolution, "linked")

    def test_no_auto_detection_path_is_reachable(self):
        """Even matching asset/category data must never leak into a bulk row.

        Since the bulk view calls ``parse_discovery_payload`` (interfaces and
        services only), never ``build_proposal``, the field-guessing helpers
        must not be invoked at all — patch them to explode if they are.
        """
        with (
            mock.patch(
                "netbox_opennms.import_node._propose_field",
                side_effect=AssertionError("bulk import must not guess fields"),
            ),
            mock.patch(
                "netbox_opennms.import_node._propose_role",
                side_effect=AssertionError("bulk import must not guess fields"),
            ),
            mock.patch(
                "netbox_opennms.import_node.asset_field_overrides",
                side_effect=AssertionError("bulk import must not guess fields"),
            ),
        ):
            response = self.client.post(
                self._url(), self._batch_data([self.node1, self.node2])
            )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Device.objects.filter(name="rtr-1").exists())
        self.assertTrue(Device.objects.filter(name="rtr-2").exists())

    def test_per_row_conflict_is_rejected_without_failing_the_batch(self):
        requisition = Requisition.objects.create(name="fs-1")
        conflict = ServerConflict(servers=["A", "B"])
        conflicted_resolution = Resolution(
            foreign_source="fs-1",
            requisition=requisition,
            server_conflict=conflict,
        )

        def matching_side_effect(target):
            return [requisition] if target.name == "rtr-1" else []

        with (
            mock.patch(
                "netbox_opennms.import_node.matching_requisitions",
                side_effect=matching_side_effect,
            ),
            mock.patch(
                "netbox_opennms.import_node.resolve_all",
                return_value=[conflicted_resolution],
            ),
        ):
            response = self.client.post(
                self._url(), self._batch_data([self.node1, self.node2])
            )
        self.assertEqual(response.status_code, 302)

        self.assertFalse(Device.objects.filter(name="rtr-1").exists())
        self.assertTrue(Device.objects.filter(name="rtr-2").exists())

        self.node1.refresh_from_db()
        self.node2.refresh_from_db()
        self.assertEqual(self.node1.resolution, "scanned")
        self.assertEqual(self.node2.resolution, "linked")

    def test_requires_no_selection_shows_error(self):
        response = self.client.post(self._url(), self._batch_data([]))
        self.assertEqual(response.status_code, 200)
        self.assertFalse(Device.objects.filter(name="rtr-1").exists())

    def test_requires_add_permission_for_chosen_kind(self):
        self.user.user_permissions.remove(
            Permission.objects.get(
                codename="add_device", content_type__app_label="dcim"
            )
        )
        response = self.client.post(
            self._url(), self._batch_data([self.node1, self.node2])
        )
        self.assertEqual(response.status_code, 403)

    def test_get_requires_some_add_permission(self):
        self.user.user_permissions.clear()
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 403)
