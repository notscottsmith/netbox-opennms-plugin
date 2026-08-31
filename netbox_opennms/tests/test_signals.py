# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Tests for signals.py (mocked port, no network)."""

from unittest import mock

from django.test import TestCase
from django.utils import timezone

from netbox_opennms.client import OpenNMSError
from netbox_opennms.models import DiscoveryScan, OpenNMSServer, Requisition


class DeleteOpenNMSRequisitionOnScanDeleteTest(TestCase):
    """Issue #72: a deleted Discovery Scan must clean up its own OpenNMS
    requisition immediately -- it's the only record of that foreign_source,
    so CleanupDiscoveryScansJob (issue #29) can never find it afterward.
    """

    @classmethod
    def setUpTestData(cls):
        cls.server = OpenNMSServer.objects.create(
            name="Acme", url="https://onms.example", username="svc", password="x"
        )
        cls.requisition = Requisition.objects.create(name="perth-discovery")

    def _scan(self, **kwargs):
        return DiscoveryScan.objects.create(
            server=self.server,
            requisition=self.requisition,
            location="Perth",
            ip_range_begin="10.0.0.1",
            ip_range_end="10.0.0.10",
            **kwargs,
        )

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_deletes_opennms_requisition(self, mock_from_server):
        scan = self._scan()
        foreign_source = scan.foreign_source
        client = mock_from_server.return_value.__enter__.return_value

        scan.delete()

        client.delete_requisition.assert_called_once_with(foreign_source)

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_skips_already_cleaned_up_scan(self, mock_from_server):
        scan = self._scan(cleaned_up_at=timezone.now())
        client = mock_from_server.return_value.__enter__.return_value

        scan.delete()

        client.delete_requisition.assert_not_called()

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_opennms_error_does_not_block_delete(self, mock_from_server):
        scan = self._scan()
        client = mock_from_server.return_value.__enter__.return_value
        client.delete_requisition.side_effect = OpenNMSError("unreachable")

        with self.assertLogs("netbox_opennms.signals", level="WARNING"):
            scan.delete()

        self.assertFalse(DiscoveryScan.objects.filter(pk=scan.pk).exists())

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_bulk_queryset_delete_also_cleans_up(self, mock_from_server):
        # A bulk queryset.delete() must not bypass the signal (it can take a
        # fast path that skips per-instance signals when nothing is
        # listening) -- registering this receiver is what rules that out.
        scan = self._scan()
        foreign_source = scan.foreign_source
        client = mock_from_server.return_value.__enter__.return_value

        DiscoveryScan.objects.filter(pk=scan.pk).delete()

        client.delete_requisition.assert_called_once_with(foreign_source)
