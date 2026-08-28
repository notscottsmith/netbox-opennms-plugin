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
from django.db.models import Count
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from netbox_opennms.models import (
    DiscoveredNode,
    DiscoveryScan,
    OpenNMSServer,
    Requisition,
)
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
        self.assertIn('formmethod="post"', html)
        self.assertIn(f"/servers/{self.server.pk}/test/", html)

    def test_scan_action_has_no_nested_form(self):
        table = OpenNMSServerTable(OpenNMSServer.objects.all())
        html = table.rows[0].get_cell("scan_action")

        self.assertNotIn("<form", html)
        self.assertIn("formaction=", html)
        self.assertIn(f"/servers/{self.server.pk}/scan/", html)

    def test_list_page_rows_have_no_nested_forms(self):
        user = User.objects.create_user(username="tester", is_superuser=True)
        self.client.force_login(user)
        test_url = reverse(
            "plugins:netbox_opennms:opennmsserver_test", args=[self.server.pk]
        )

        response = self.client.get(reverse("plugins:netbox_opennms:opennmsserver_list"))

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
        self.assertIn(f"/discovery/{self.scan.pk}/trigger/", html)

    def test_list_page_rows_have_no_nested_forms(self):
        user = User.objects.create_user(username="tester", is_superuser=True)
        self.client.force_login(user)
        trigger_url = reverse(
            "plugins:netbox_opennms:discoveryscan_trigger", args=[self.scan.pk]
        )

        response = self.client.get(reverse("plugins:netbox_opennms:discoveryscan_list"))

        self.assertEqual(response.status_code, 200)
        tbody = _tbody(response)
        self.assertNotIn("<form", tbody)
        self.assertIn(f'formaction="{trigger_url}"', tbody)


class DiscoveryScanTableStatusAndNodeCountTest(TestCase):
    """Issue #53: DiscoveryScanTable's status/node-count columns and the
    Trigger button's disabled+tooltip gating once a scan leaves "pending" —
    the table-side counterpart of #50's DiscoveryScanTriggerView guard.
    """

    @classmethod
    def setUpTestData(cls):
        server = OpenNMSServer.objects.create(name="Acme", url="https://onms.example")
        requisition = Requisition.objects.create(name="fs-1", filter_params=FILTER)
        cls.pending = DiscoveryScan.objects.create(
            server=server,
            requisition=requisition,
            location="raleigh",
            ip_range_begin="10.0.0.1",
            ip_range_end="10.0.0.254",
        )
        cls.running = DiscoveryScan.objects.create(
            server=server,
            requisition=requisition,
            location="raleigh",
            ip_range_begin="10.0.1.1",
            ip_range_end="10.0.1.254",
        )
        cls.running.mark_triggered()
        cls.settled = DiscoveryScan.objects.create(
            server=server,
            requisition=requisition,
            location="raleigh",
            ip_range_begin="10.0.2.1",
            ip_range_end="10.0.2.254",
        )
        cls.settled.mark_triggered()
        DiscoveryScan.objects.filter(pk=cls.settled.pk).update(
            settled_at=timezone.now()
        )
        cls.settled.refresh_from_db()

        for i in range(2):
            DiscoveredNode.objects.create(
                server=server,
                discovery_scan=cls.pending,
                opennms_node_id=i,
                label=f"node-{i}",
                verdict="green",
            )

    def _row(self, scan, *, annotated=True):
        queryset = DiscoveryScan.objects.filter(pk=scan.pk)
        if annotated:
            queryset = queryset.annotate(node_count=Count("discovered_nodes"))
        table = DiscoveryScanTable(queryset)
        return table.rows[0]

    def test_status_column_shows_pending(self):
        html = self._row(self.pending).get_cell("status")
        self.assertIn("Pending", html)

    def test_status_column_shows_running(self):
        html = self._row(self.running).get_cell("status")
        self.assertIn("Running", html)

    def test_status_column_shows_settled(self):
        html = self._row(self.settled).get_cell("status")
        self.assertIn("Settled", html)

    def test_node_count_column_reflects_discovered_nodes_count(self):
        self.assertEqual(self._row(self.pending).get_cell("node_count"), 2)
        self.assertEqual(self._row(self.running).get_cell("node_count"), 0)

    def test_node_count_falls_back_without_annotation(self):
        # DiscoveryScanBulkDeleteView (and this table's other pre-existing
        # test above) render this table off an unannotated queryset —
        # render_node_count must still reflect discovered_nodes.count().
        row = self._row(self.pending, annotated=False)
        self.assertEqual(row.get_cell("node_count"), 2)

    def test_trigger_action_enabled_when_pending(self):
        html = self._row(self.pending).get_cell("trigger_action")
        self.assertNotIn("disabled", html)

    def test_trigger_action_disabled_when_running(self):
        html = self._row(self.running).get_cell("trigger_action")
        self.assertIn("disabled", html)
        self.assertIn("title=", html)

    def test_trigger_action_disabled_when_settled(self):
        html = self._row(self.settled).get_cell("trigger_action")
        self.assertIn("disabled", html)
        self.assertIn("title=", html)
