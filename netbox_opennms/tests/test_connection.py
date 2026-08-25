# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Tests for the Connect OpenNMS UI action (mocked client, no network).

One row per ``OpenNMSServer`` (ADR 0002): permission-gated, shows each Server's
URL/username read-only (never the password), and POSTs a ``server_id`` to test
that one Server's connection — no user-supplied URL/credentials, nothing
persisted.
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from netbox_opennms.client import OpenNMSError
from netbox_opennms.models import OpenNMSServer

URL = "plugins:netbox_opennms:connection_test"


class ConnectionTestViewTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        user_model = get_user_model()
        # Superuser passes the view_requisition permission gate.
        cls.user = user_model.objects.create_user(
            username="admin", password="pw", is_superuser=True
        )
        # An authenticated user without the plugin permission.
        cls.plain = user_model.objects.create_user(username="plain", password="pw")
        cls.server = OpenNMSServer.objects.create(
            name="Acme",
            url="https://onms.example.org/opennms",
            username="provision-svc",
            password="SUPER-SECRET",
        )

    def setUp(self):
        self.client.force_login(self.user)

    def test_get_renders_page(self):
        response = self.client.get(reverse(URL))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Connect OpenNMS")

    def test_anonymous_is_redirected(self):
        self.client.logout()
        self.assertIn(self.client.get(reverse(URL)).status_code, (302, 403))

    def test_requires_permission(self):
        self.client.force_login(self.plain)
        self.assertEqual(self.client.get(reverse(URL)).status_code, 403)

    def test_get_shows_configured_url_username_not_password(self):
        response = self.client.get(reverse(URL))
        self.assertContains(response, "https://onms.example.org/opennms")
        self.assertContains(response, "provision-svc")
        # The password is reported as configured, never rendered.
        self.assertContains(response, "Configured")
        self.assertNotContains(response, "SUPER-SECRET")

    def test_get_with_no_servers_shows_placeholder(self):
        OpenNMSServer.objects.all().delete()
        response = self.client.get(reverse(URL))
        self.assertContains(response, "No OpenNMS Servers are configured yet.")

    @mock.patch("netbox_opennms.views.OpenNMSClient.from_server")
    def test_post_probes_the_named_server(self, mock_from_server):
        client = mock_from_server.return_value.__enter__.return_value
        client.test_connection.return_value = True
        response = self.client.post(
            reverse(URL), {"server_id": self.server.pk}, follow=True
        )
        self.assertContains(response, "OpenNMS connection to 'Acme' OK")
        mock_from_server.assert_called_once_with(self.server)

    @mock.patch("netbox_opennms.views.OpenNMSClient.from_server")
    def test_post_failure_message(self, mock_from_server):
        client = mock_from_server.return_value.__enter__.return_value
        client.test_connection.side_effect = OpenNMSError("unreachable")
        response = self.client.post(
            reverse(URL), {"server_id": self.server.pk}, follow=True
        )
        self.assertContains(response, "OpenNMS connection to 'Acme' failed")

    def test_post_unknown_server_404s(self):
        response = self.client.post(reverse(URL), {"server_id": 999999})
        self.assertEqual(response.status_code, 404)
