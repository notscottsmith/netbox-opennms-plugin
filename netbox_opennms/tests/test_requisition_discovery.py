# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Tests for unmirrored Foreign Source discovery (issue #11)."""

from unittest import mock

from django.contrib.auth.models import Permission, User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from netbox_opennms.client import OpenNMSError
from netbox_opennms.models import OpenNMSServer, Requisition
from netbox_opennms.requisition_discovery import unmirrored_requisitions

VIEW_PERM = "netbox_opennms.view_opennmsserver"


class UnmirroredRequisitionsTest(SimpleTestCase):
    def test_names_not_in_netbox_are_unmirrored(self):
        result = unmirrored_requisitions(["fs-a", "fs-b"], ["fs-a"])
        self.assertEqual(result, ["fs-b"])

    def test_fully_mirrored_returns_empty(self):
        self.assertEqual(unmirrored_requisitions(["fs-a"], ["fs-a", "fs-b"]), [])

    def test_empty_opennms_names_returns_empty(self):
        self.assertEqual(unmirrored_requisitions([], ["fs-a"]), [])

    def test_result_is_sorted(self):
        result = unmirrored_requisitions(["fs-c", "fs-a", "fs-b"], [])
        self.assertEqual(result, ["fs-a", "fs-b", "fs-c"])

    def test_duplicates_collapse(self):
        result = unmirrored_requisitions(["fs-a", "fs-a"], [])
        self.assertEqual(result, ["fs-a"])


class UnmirroredRequisitionsViewTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.server = OpenNMSServer.objects.create(
            name="Acme", url="https://onms.example", username="svc", password="x"
        )
        Requisition.objects.create(name="fs-mirrored")

    def setUp(self):
        self.user = User.objects.create_user(username="tester")
        self.user.user_permissions.add(
            Permission.objects.get(
                codename="view_opennmsserver", content_type__app_label="netbox_opennms"
            )
        )
        self.client.force_login(self.user)

    def _url(self):
        return reverse(
            "plugins:netbox_opennms:opennmsserver_unmirrored_requisitions",
            args=[self.server.pk],
        )

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_shows_only_unmirrored_names(self, mock_from_server):
        client = mock_from_server.return_value.__enter__.return_value
        client.list_requisition_names.return_value = ["fs-mirrored", "fs-orphan"]
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["names"], ["fs-orphan"])
        self.assertContains(response, "fs-orphan")
        self.assertNotContains(response, "fs-mirrored")

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_client_failure_shows_error(self, mock_from_server):
        client = mock_from_server.return_value.__enter__.return_value
        client.list_requisition_names.side_effect = OpenNMSError("unreachable")
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "unreachable")

    def test_requires_view_permission(self):
        self.user.user_permissions.clear()
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 403)
