# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Tests for ``DiscoveredNodeConfirmIPView`` (issue #31).

Covers the view's permission enforcement end-to-end via ``self.client``,
following ``test_discovery_import.py``'s ``DiscoveredNodeImportViewTest``
pattern -- granted/removed ``Permission`` objects, asserting ``403`` when a
permission ``required_confirm_permissions()`` demands is missing, including
the matched-Device case (``dcim.add_interface``) that a prior code review
found was unchecked.
"""

from dcim.models import Device, DeviceRole, DeviceType, Manufacturer, Site
from django.contrib.auth.models import Permission, User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from ipam.models import IPAddress

from netbox_opennms.models import DiscoveredNode, OpenNMSServer


class DiscoveredNodeConfirmIPViewTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.server = OpenNMSServer.objects.create(
            name="Acme", url="https://onms.example", username="svc", password="x"
        )
        cls.site = Site.objects.create(name="Site 1", slug="site-1")
        mfr = Manufacturer.objects.create(name="Acme", slug="acme")
        device_type = DeviceType.objects.create(
            manufacturer=mfr, model="Model 1", slug="model-1"
        )
        role = DeviceRole.objects.create(name="Router", slug="router")
        cls.device = Device.objects.create(
            name="rtr-1", device_type=device_type, role=role, site=cls.site
        )

    def setUp(self):
        self.user = User.objects.create_user(username="tester")
        self.client.force_login(self.user)

    def _grant(self, *codename_apps):
        for codename, app_label in codename_apps:
            self.user.user_permissions.add(
                Permission.objects.get(
                    codename=codename, content_type__app_label=app_label
                )
            )

    def _node(self, *, matched=None):
        return DiscoveredNode.objects.create(
            server=self.server,
            opennms_node_id=1,
            label="rtr-1",
            foreign_id="ghost",
            location="Site 1",
            verdict="red",
            ip_interfaces=[
                {
                    "ipAddress": "10.0.0.9",
                    "snmpPrimary": "P",
                    "netMask": "255.255.255.0",
                }
            ],
            walked_at=timezone.now(),
            matched_object=matched,
        )

    def _url(self, node):
        return reverse(
            "plugins:netbox_opennms:discoverednode_confirm_ip", args=[node.pk]
        )

    def test_requires_ipam_add_ipaddress_permission(self):
        node = self._node()
        response = self.client.post(self._url(node), {"ip_address": "10.0.0.9"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(IPAddress.objects.exists())

    def test_requires_ipam_add_prefix_permission(self):
        node = self._node()
        self._grant(("add_ipaddress", "ipam"))
        response = self.client.post(self._url(node), {"ip_address": "10.0.0.9"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(IPAddress.objects.exists())

    def test_requires_dcim_add_interface_permission_when_matched(self):
        # The gap a prior code review caught: without a matched Device,
        # ipam-only permissions are enough; with one, confirming also
        # creates an Interface, so that permission must be checked too.
        node = self._node(matched=self.device)
        self._grant(("add_ipaddress", "ipam"), ("add_prefix", "ipam"))
        response = self.client.post(self._url(node), {"ip_address": "10.0.0.9"})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(IPAddress.objects.exists())

    def test_confirm_succeeds_without_match_once_ipam_permissions_granted(self):
        node = self._node()
        self._grant(("add_ipaddress", "ipam"), ("add_prefix", "ipam"))
        response = self.client.post(self._url(node), {"ip_address": "10.0.0.9"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(IPAddress.objects.filter(address="10.0.0.9/24").exists())

    def test_confirm_succeeds_when_matched_once_interface_permission_granted(self):
        node = self._node(matched=self.device)
        self._grant(
            ("add_ipaddress", "ipam"),
            ("add_prefix", "ipam"),
            ("add_interface", "dcim"),
        )
        response = self.client.post(self._url(node), {"ip_address": "10.0.0.9"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(IPAddress.objects.filter(address="10.0.0.9/24").exists())
