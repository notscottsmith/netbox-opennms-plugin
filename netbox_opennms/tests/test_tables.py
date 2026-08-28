# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Regression tests for issue #32: list-view row actions must not render a
nested <form> inside NetBox's outer bulk-action <form>, since browsers hoist
invalid nested forms out during parsing and the click ends up submitting
through the wrong form/method (405).

These tests render each TemplateColumn cell in isolation and assert the
formaction/formmethod fix is in place instead of a nested <form>.
"""

from django.test import TestCase

from netbox_opennms.models import DiscoveryScan, OpenNMSServer, Requisition
from netbox_opennms.tables import DiscoveryScanTable, OpenNMSServerTable

FILTER = {"site": ["raleigh"]}


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
