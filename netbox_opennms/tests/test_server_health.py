# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Tests for connection-test views integrated into the Servers area.

Covers ``OpenNMSServerTestView`` (synchronous, list/detail row action on an
already-saved Server) and ``OpenNMSServerTestAjaxView`` (JSON, add/edit form —
also works against an unsaved Server, since it tests the *posted* fields
rather than a saved row).
"""

from unittest import mock

from django.contrib.auth.models import Permission, User
from django.test import TestCase
from django.urls import reverse

from netbox_opennms.client import OpenNMSHTTPError
from netbox_opennms.models import OpenNMSServer

CHANGE_PERM = "netbox_opennms.change_opennmsserver"


class OpenNMSServerTestViewTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.server = OpenNMSServer.objects.create(
            name="Acme", url="https://onms.example", username="svc", password="x"
        )

    def setUp(self):
        self.user = User.objects.create_user(username="tester")
        self.user.user_permissions.add(
            Permission.objects.get(
                codename="change_opennmsserver", content_type__app_label="netbox_opennms"
            )
        )
        self.client.force_login(self.user)

    def _url(self):
        return reverse(
            "plugins:netbox_opennms:opennmsserver_test", args=[self.server.pk]
        )

    @mock.patch("netbox_opennms.views.OpenNMSClient.from_server")
    def test_success_persists_ok(self, mock_from_server):
        client = mock_from_server.return_value.__enter__.return_value
        client.test_connection.return_value = True
        client.list_locations.return_value = {"edge-2", "edge-1"}
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)
        self.server.refresh_from_db()
        self.assertEqual(self.server.last_check_status, "ok")
        self.assertEqual(self.server.last_check_message, "")
        self.assertIsNotNone(self.server.last_check_time)
        self.assertEqual(self.server.available_locations, ["edge-1", "edge-2"])

    @mock.patch("netbox_opennms.views.OpenNMSClient.from_server")
    def test_failure_persists_failed_with_message(self, mock_from_server):
        client = mock_from_server.return_value.__enter__.return_value
        client.test_connection.side_effect = OpenNMSHTTPError(
            "unauthorized", status_code=401
        )
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)
        self.server.refresh_from_db()
        self.assertEqual(self.server.last_check_status, "failed")
        self.assertIn("unauthorized", self.server.last_check_message)

    def test_requires_change_permission(self):
        self.user.user_permissions.clear()
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, 403)


class OpenNMSServerTestAjaxViewTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.server = OpenNMSServer.objects.create(
            name="Acme", url="https://onms.example", username="svc", password="x"
        )

    def setUp(self):
        self.user = User.objects.create_user(username="tester")
        self.user.user_permissions.add(
            Permission.objects.get(
                codename="change_opennmsserver", content_type__app_label="netbox_opennms"
            )
        )
        self.client.force_login(self.user)

    def _url(self):
        return reverse("plugins:netbox_opennms:opennmsserver_test_ajax")

    @mock.patch("netbox_opennms.views.OpenNMSClient")
    def test_success_returns_locations(self, mock_client_cls):
        client = mock_client_cls.return_value
        client.__enter__ = mock.Mock(return_value=client)
        client.__exit__ = mock.Mock(return_value=False)
        client.test_connection.return_value = True
        client.list_locations.return_value = {"edge-2", "edge-1"}

        response = self.client.post(
            self._url(),
            {
                "url": "https://new.example",
                "username": "svc",
                "password": "hunter2",
                "headers": "{}",
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["locations"], ["edge-1", "edge-2"])
        self.server.refresh_from_db()
        self.assertEqual(self.server.available_locations, [])  # no server_id posted

    @mock.patch("netbox_opennms.views.OpenNMSClient")
    def test_failure_returns_message(self, mock_client_cls):
        client = mock_client_cls.return_value
        client.__enter__ = mock.Mock(return_value=client)
        client.__exit__ = mock.Mock(return_value=False)
        client.test_connection.side_effect = OpenNMSHTTPError(
            "boom", status_code=500
        )

        response = self.client.post(
            self._url(),
            {"url": "https://new.example", "username": "svc", "password": "x"},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data["ok"])
        self.assertIn("boom", data["message"])

    def test_invalid_headers_json_rejected(self):
        response = self.client.post(
            self._url(),
            {
                "url": "https://new.example",
                "username": "svc",
                "password": "x",
                "headers": "not json",
            },
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertFalse(data["ok"])

    @mock.patch("netbox_opennms.views.OpenNMSClient")
    def test_unsaved_form_does_not_touch_the_database(self, mock_client_cls):
        # No server_id: this is a not-yet-saved add-form test — nothing in the
        # DB should change.
        client = mock_client_cls.return_value
        client.__enter__ = mock.Mock(return_value=client)
        client.__exit__ = mock.Mock(return_value=False)
        client.test_connection.return_value = True
        client.list_locations.return_value = set()

        before = self.server.last_check_status
        self.client.post(
            self._url(),
            {"url": "https://new.example", "username": "svc", "password": "x"},
        )
        self.server.refresh_from_db()
        self.assertEqual(self.server.last_check_status, before)

    @mock.patch("netbox_opennms.views.OpenNMSClient")
    def test_server_id_persists_result(self, mock_client_cls):
        client = mock_client_cls.return_value
        client.__enter__ = mock.Mock(return_value=client)
        client.__exit__ = mock.Mock(return_value=False)
        client.test_connection.return_value = True
        client.list_locations.return_value = {"edge-1"}

        self.client.post(
            self._url(),
            {
                "url": "https://new.example",
                "username": "svc",
                "password": "x",
                "server_id": self.server.pk,
            },
        )
        self.server.refresh_from_db()
        self.assertEqual(self.server.last_check_status, "ok")
        self.assertEqual(self.server.available_locations, ["edge-1"])

    def test_requires_change_permission(self):
        self.user.user_permissions.clear()
        response = self.client.post(
            self._url(),
            {"url": "https://new.example", "username": "svc", "password": "x"},
        )
        self.assertEqual(response.status_code, 403)
