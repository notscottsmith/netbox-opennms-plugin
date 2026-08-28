# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Regression tests for issue #32: list-view row actions must not render a
nested <form> inside NetBox's outer bulk-action <form>, since browsers hoist
invalid nested forms out during parsing and the click ends up submitting
through the wrong form/method (405).

These tests render each TemplateColumn cell in isolation and assert the
formaction/formmethod fix is in place instead of a nested <form>.
"""

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from netbox_opennms.models import DiscoveryScan, OpenNMSServer, Requisition
from netbox_opennms.tables import DiscoveryScanTable, OpenNMSServerTable

FILTER = {"site": ["raleigh"]}


def _tbody(response):
    """The rendered <tbody>...</tbody> slice of a list-view response, so a
    nested-form assertion only looks at the row content the fix touches —
    not any legitimate top-level form the page has elsewhere (e.g. a filter
    form)."""
    content = response.content.decode()
    return content.split("<tbody", 1)[1].split("</tbody>", 1)[0]


class OpenNMSServerTableActionsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.server = OpenNMSServer.objects.create(
            name="Acme", url="https://onms.example", username="svc", password="x"
        )

    def test_test_action_has_no_nested_form(self):
        table = OpenNMSServerTable(OpenNMSServer.objects.all())
        html = table.rows[0].get_cell("test_action")

        self.assertNotIn("<form", html)
        self.assertIn("formaction=", html)
        self.assertIn("formmethod=\"post\"", html)
        self.assertIn(f"/opennms-servers/{self.server.pk}/test/", html)

    def test_scan_action_has_no_nested_form(self):
        table = OpenNMSServerTable(OpenNMSServer.objects.all())
        html = table.rows[0].get_cell("scan_action")

        self.assertNotIn("<form", html)
        self.assertIn("formaction=", html)
        self.assertIn(f"/opennms-servers/{self.server.pk}/scan/", html)

    def test_list_page_rows_have_no_nested_forms(self):
        user = User.objects.create_user(username="tester", is_superuser=True)
        self.client.force_login(user)
        test_url = reverse(
            "plugins:netbox_opennms:opennmsserver_test", args=[self.server.pk]
        )

        response = self.client.get(
            reverse("plugins:netbox_opennms:opennmsserver_list")
        )

        self.assertEqual(response.status_code, 200)
        tbody = _tbody(response)
        self.assertNotIn("<form", tbody)
        self.assertIn(f'formaction="{test_url}"', tbody)


class DiscoveryScanTableActionsTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        server = OpenNMSServer.objects.create(
            name="Acme", url="https://onms.example", username="svc", password="x"
        )
        requisition = Requisition.objects.create(name="fs-1", filter_params=FILTER)
        cls.scan = DiscoveryScan.objects.create(
            server=server,
            requisition=requisition,
            location="raleigh",
            ip_range_begin="10.0.0.1",
            ip_range_end="10.0.0.254",
        )

    def test_trigger_action_has_no_nested_form(self):
        table = DiscoveryScanTable(DiscoveryScan.objects.all())
        html = table.rows[0].get_cell("trigger_action")

        self.assertNotIn("<form", html)
        self.assertIn("formaction=", html)
        self.assertIn(f"/discovery-scans/{self.scan.pk}/trigger/", html)

    def test_list_page_rows_have_no_nested_forms(self):
        user = User.objects.create_user(username="tester", is_superuser=True)
        self.client.force_login(user)
        trigger_url = reverse(
            "plugins:netbox_opennms:discoveryscan_trigger", args=[self.scan.pk]
        )

        response = self.client.get(
            reverse("plugins:netbox_opennms:discoveryscan_list")
        )

        self.assertEqual(response.status_code, 200)
        tbody = _tbody(response)
        self.assertNotIn("<form", tbody)
        self.assertIn(f'formaction="{trigger_url}"', tbody)
