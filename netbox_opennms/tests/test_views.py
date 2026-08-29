# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""UI view (CRUD) tests for the plugin models."""

from unittest import mock

from dcim.models import (
    Device,
    DeviceRole,
    DeviceType,
    Interface,
    Manufacturer,
    Site,
)
from django.contrib.auth.models import Permission, User
from django.contrib.messages import get_messages
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from ipam.models import IPAddress
from utilities.testing import ViewTestCases

from netbox_opennms.client import OpenNMSError
from netbox_opennms.membership import (
    Conflict,
    InterfaceSpec,
    NodeSpec,
    Resolution,
    ServerConflict,
)
from netbox_opennms.models import (
    AssetMapping,
    DiscoveredNode,
    DiscoveryScan,
    MetadataContext,
    MetadataEntry,
    MetadataKey,
    MonitoredInterface,
    MonitoredService,
    MonitoringDetector,
    MonitoringExclusion,
    MonitoringOverride,
    MonitoringPolicy,
    OpenNMSServer,
    Requisition,
)
from netbox_opennms.requisition_scan import NodeDiff, RequisitionScanResult
from netbox_opennms.tables import DiscoveredNodeTable
from netbox_opennms.translation import RenderError

DETECTOR_CLASS = "org.opennms.netmgt.provision.detector.icmp.IcmpDetector"
POLICY_CLASS = "org.opennms.netmgt.provision.persist.policies.NodeCategorySettingPolicy"
FILTER = {"site": ["site-1"], "role": ["router"]}


def _devices(count):
    site = Site.objects.create(name="Site 1", slug="site-1")
    mfr = Manufacturer.objects.create(name="Acme", slug="acme")
    dt = DeviceType.objects.create(manufacturer=mfr, model="Model 1", slug="model-1")
    role = DeviceRole.objects.create(name="Router", slug="router")
    return [
        Device.objects.create(name=f"device-{i}", device_type=dt, role=role, site=site)
        for i in range(count)
    ]


class RequisitionViewTest(
    ViewTestCases.GetObjectViewTestCase,
    ViewTestCases.GetObjectChangelogViewTestCase,
    ViewTestCases.CreateObjectViewTestCase,
    ViewTestCases.EditObjectViewTestCase,
    ViewTestCases.DeleteObjectViewTestCase,
    ViewTestCases.ListObjectsViewTestCase,
    ViewTestCases.BulkDeleteObjectsViewTestCase,
):
    model = Requisition
    # JSON / multi-value fields don't round-trip as plain equality in
    # assertInstanceEqual (dict vs. string, list vs. list-of-choices).
    validation_excluded_fields = ("filter_params", "services")

    def _get_base_url(self):
        return "plugins:netbox_opennms:requisition_{}"

    @classmethod
    def setUpTestData(cls):
        for name in ("req-1", "req-2", "req-3"):
            Requisition.objects.create(name=name, filter_params=FILTER)
        cls.form_data = {
            "name": "req-4",
            "object_types": "both",
            "filter_params": '{"site": ["site-1"]}',
            "scan_interval": "1d",
            "default_interfaces": "primary",
            "services": ["ICMP", "SNMP"],
            "location": "",
        }


class MonitoringDetectorViewTest(
    ViewTestCases.GetObjectViewTestCase,
    ViewTestCases.GetObjectChangelogViewTestCase,
    ViewTestCases.CreateObjectViewTestCase,
    ViewTestCases.EditObjectViewTestCase,
    ViewTestCases.DeleteObjectViewTestCase,
    ViewTestCases.ListObjectsViewTestCase,
    ViewTestCases.BulkDeleteObjectsViewTestCase,
):
    model = MonitoringDetector

    def _get_base_url(self):
        return "plugins:netbox_opennms:monitoringdetector_{}"

    @classmethod
    def setUpTestData(cls):
        req = Requisition.objects.create(name="req", filter_params=FILTER)
        for name in ("d1", "d2", "d3"):
            MonitoringDetector.objects.create(
                requisition=req, name=name, rule_class=DETECTOR_CLASS
            )
        cls.form_data = {
            "requisition": req.pk,
            "name": "d4",
            "rule_class": DETECTOR_CLASS,
        }


class MonitoringPolicyViewTest(
    ViewTestCases.GetObjectViewTestCase,
    ViewTestCases.GetObjectChangelogViewTestCase,
    ViewTestCases.CreateObjectViewTestCase,
    ViewTestCases.EditObjectViewTestCase,
    ViewTestCases.DeleteObjectViewTestCase,
    ViewTestCases.ListObjectsViewTestCase,
    ViewTestCases.BulkDeleteObjectsViewTestCase,
):
    model = MonitoringPolicy

    def _get_base_url(self):
        return "plugins:netbox_opennms:monitoringpolicy_{}"

    @classmethod
    def setUpTestData(cls):
        req = Requisition.objects.create(name="req", filter_params=FILTER)
        for name in ("p1", "p2", "p3"):
            MonitoringPolicy.objects.create(
                requisition=req, name=name, rule_class=POLICY_CLASS
            )
        cls.form_data = {
            "requisition": req.pk,
            "name": "p4",
            "rule_class": POLICY_CLASS,
        }


class MonitoringOverrideViewTest(
    ViewTestCases.GetObjectViewTestCase,
    ViewTestCases.GetObjectChangelogViewTestCase,
    ViewTestCases.CreateObjectViewTestCase,
    ViewTestCases.EditObjectViewTestCase,
    ViewTestCases.DeleteObjectViewTestCase,
    ViewTestCases.ListObjectsViewTestCase,
    ViewTestCases.BulkDeleteObjectsViewTestCase,
):
    model = MonitoringOverride

    def _get_base_url(self):
        return "plugins:netbox_opennms:monitoringoverride_{}"

    @classmethod
    def setUpTestData(cls):
        devices = _devices(6)
        for device in devices[:3]:
            MonitoringOverride.objects.create(assigned_object=device)
        cls.form_data = {
            "device": devices[3].pk,
            "exclude": True,
            "management_role": "P",
            "location": "",
        }


class MonitoredServiceViewTest(
    ViewTestCases.GetObjectViewTestCase,
    ViewTestCases.GetObjectChangelogViewTestCase,
    ViewTestCases.CreateObjectViewTestCase,
    ViewTestCases.EditObjectViewTestCase,
    ViewTestCases.DeleteObjectViewTestCase,
    ViewTestCases.ListObjectsViewTestCase,
    ViewTestCases.BulkDeleteObjectsViewTestCase,
):
    model = MonitoredService

    def _get_base_url(self):
        return "plugins:netbox_opennms:monitoredservice_{}"

    @classmethod
    def setUpTestData(cls):
        device = _devices(1)[0]
        iface = Interface.objects.create(device=device, name="eth0", type="virtual")
        ips = [
            IPAddress.objects.create(address=f"10.0.0.{i}/24", assigned_object=iface)
            for i in range(1, 7)
        ]
        override = MonitoringOverride.objects.create(
            assigned_object=device, management_ip=ips[0]
        )
        for extra_ip in ips[1:]:
            MonitoredInterface.objects.create(override=override, ip_address=extra_ip)
        for ip, name in [(ips[0], "ICMP"), (ips[0], "SNMP"), (ips[1], "HTTP")]:
            MonitoredService.objects.create(override=override, ip_address=ip, name=name)
        cls.form_data = {
            "override": override.pk,
            "ip_address": ips[2].pk,
            "name": "DNS",
        }


class MonitoredInterfaceViewTest(
    ViewTestCases.GetObjectViewTestCase,
    ViewTestCases.GetObjectChangelogViewTestCase,
    ViewTestCases.CreateObjectViewTestCase,
    ViewTestCases.EditObjectViewTestCase,
    ViewTestCases.DeleteObjectViewTestCase,
    ViewTestCases.ListObjectsViewTestCase,
    ViewTestCases.BulkDeleteObjectsViewTestCase,
):
    model = MonitoredInterface

    def _get_base_url(self):
        return "plugins:netbox_opennms:monitoredinterface_{}"

    @classmethod
    def setUpTestData(cls):
        device = _devices(1)[0]
        iface = Interface.objects.create(device=device, name="eth0", type="virtual")
        ips = [
            IPAddress.objects.create(address=f"10.9.0.{i}/24", assigned_object=iface)
            for i in range(1, 7)
        ]
        override = MonitoringOverride.objects.create(
            assigned_object=device, management_ip=ips[0]
        )
        for extra_ip in ips[1:4]:
            MonitoredInterface.objects.create(override=override, ip_address=extra_ip)
        cls.form_data = {
            "override": override.pk,
            "ip_address": ips[4].pk,
            "role": "N",
        }


class AssetMappingViewTest(
    ViewTestCases.GetObjectViewTestCase,
    ViewTestCases.GetObjectChangelogViewTestCase,
    ViewTestCases.CreateObjectViewTestCase,
    ViewTestCases.EditObjectViewTestCase,
    ViewTestCases.DeleteObjectViewTestCase,
    ViewTestCases.ListObjectsViewTestCase,
    ViewTestCases.BulkDeleteObjectsViewTestCase,
):
    model = AssetMapping

    def _get_base_url(self):
        return "plugins:netbox_opennms:assetmapping_{}"

    @classmethod
    def setUpTestData(cls):
        req = Requisition.objects.create(
            name="am-req", filter_params={"role": ["switch"]}
        )
        for source, field in [
            ("serial", "serialNumber"),
            ("name", "displayCategory"),
            ("description", "description"),
        ]:
            AssetMapping.objects.create(
                requisition=req, netbox_source=source, asset_field=field
            )
        cls.form_data = {
            "requisition": req.pk,
            "netbox_source": "asset_tag",
            "asset_field": "assetNumber",
        }


class OpenNMSServerViewTest(
    ViewTestCases.GetObjectViewTestCase,
    ViewTestCases.GetObjectChangelogViewTestCase,
    ViewTestCases.CreateObjectViewTestCase,
    ViewTestCases.EditObjectViewTestCase,
    ViewTestCases.DeleteObjectViewTestCase,
    ViewTestCases.ListObjectsViewTestCase,
    ViewTestCases.BulkDeleteObjectsViewTestCase,
):
    model = OpenNMSServer

    def _get_base_url(self):
        return "plugins:netbox_opennms:opennmsserver_{}"

    @classmethod
    def setUpTestData(cls):
        for name in ("srv-1", "srv-2", "srv-3"):
            OpenNMSServer.objects.create(
                name=name, url=f"https://{name}.example", username="svc", password="x"
            )
        cls.form_data = {
            "name": "srv-4",
            "url": "https://srv-4.example",
            "username": "svc",
            "password": "hunter2",
            "headers": "{}",
        }


class OpenNMSServerRequisitionsSectionViewTest(TestCase):
    """OpenNMSServerView's live "Requisitions" section (issue #63).

    No persisted FK exists — a Requisition's Server membership is fully
    derived from Scope (ADR 0002/0003) via ``membership.target_server_for``.
    Two Requisitions, each filtered to a different Site, and each Site
    scoped to a different Server: each Requisition must appear only on its
    own Server's page.
    """

    @classmethod
    def setUpTestData(cls):
        mfr = Manufacturer.objects.create(name="Acme", slug="acme")
        dt = DeviceType.objects.create(
            manufacturer=mfr, model="Model 1", slug="model-1"
        )
        role = DeviceRole.objects.create(name="Router", slug="router")
        site_a = Site.objects.create(name="Site A", slug="site-a")
        site_b = Site.objects.create(name="Site B", slug="site-b")
        Device.objects.create(name="dev-a", device_type=dt, role=role, site=site_a)
        Device.objects.create(name="dev-b", device_type=dt, role=role, site=site_b)

        cls.server_a = OpenNMSServer.objects.create(
            name="Server A", url="https://server-a.example"
        )
        cls.server_a.sites.add(site_a)
        cls.server_b = OpenNMSServer.objects.create(
            name="Server B", url="https://server-b.example"
        )
        cls.server_b.sites.add(site_b)

        cls.requisition_a = Requisition.objects.create(
            name="req-a", filter_params={"site": ["site-a"]}
        )
        cls.requisition_b = Requisition.objects.create(
            name="req-b", filter_params={"site": ["site-b"]}
        )

    def setUp(self):
        self.user = User.objects.create_user(username="tester")
        self.user.user_permissions.add(
            Permission.objects.get(
                codename="view_opennmsserver",
                content_type__app_label="netbox_opennms",
            )
        )
        self.client.force_login(self.user)

    def test_requisitions_resolving_to_this_server_are_listed(self):
        response = self.client.get(self.server_a.get_absolute_url())
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("req-a", content)
        self.assertNotIn("req-b", content)

    def test_requisitions_resolving_to_other_server_are_excluded(self):
        response = self.client.get(self.server_b.get_absolute_url())
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("req-b", content)
        self.assertNotIn("req-a", content)


class MonitoringExclusionViewTest(
    ViewTestCases.GetObjectViewTestCase,
    ViewTestCases.GetObjectChangelogViewTestCase,
    ViewTestCases.CreateObjectViewTestCase,
    ViewTestCases.EditObjectViewTestCase,
    ViewTestCases.DeleteObjectViewTestCase,
    ViewTestCases.ListObjectsViewTestCase,
    ViewTestCases.BulkDeleteObjectsViewTestCase,
):
    model = MonitoringExclusion

    def _get_base_url(self):
        return "plugins:netbox_opennms:monitoringexclusion_{}"

    @classmethod
    def setUpTestData(cls):
        for description in ("excl-1", "excl-2", "excl-3"):
            MonitoringExclusion.objects.create(description=description)
        cls.form_data = {
            "description": "excl-4",
        }


class DiscoveryScanViewTest(
    ViewTestCases.GetObjectViewTestCase,
    ViewTestCases.GetObjectChangelogViewTestCase,
    ViewTestCases.CreateObjectViewTestCase,
    ViewTestCases.EditObjectViewTestCase,
    ViewTestCases.DeleteObjectViewTestCase,
    ViewTestCases.ListObjectsViewTestCase,
    ViewTestCases.BulkDeleteObjectsViewTestCase,
):
    model = DiscoveryScan

    def _get_base_url(self):
        return "plugins:netbox_opennms:discoveryscan_{}"

    @classmethod
    def setUpTestData(cls):
        server = OpenNMSServer.objects.create(name="Acme", url="https://onms.example")
        requisition = Requisition.objects.create(name="fs-1", location="raleigh")
        for i in range(3):
            DiscoveryScan.objects.create(
                server=server,
                requisition=requisition,
                location="raleigh",
                ip_range_begin=f"10.0.{i}.1",
                ip_range_end=f"10.0.{i}.254",
            )
        cls.form_data = {
            "server": server.pk,
            "requisition": requisition.pk,
            "ip_range_begin": "10.0.9.1",
            "ip_range_end": "10.0.9.254",
            "retries": 1,
            "timeout": 2000,
        }


class DiscoveryScanTriggerViewTest(TestCase):
    """``DiscoveryScanTriggerView`` (issue #25): fires ``POST /api/v2/discovery``."""

    @classmethod
    def setUpTestData(cls):
        cls.requisition = Requisition.objects.create(name="fs-1")
        cls.server = OpenNMSServer.objects.create(
            name="Acme", url="https://onms.example", username="svc", password="x"
        )
        cls.scan = DiscoveryScan.objects.create(
            server=cls.server,
            requisition=cls.requisition,
            location="raleigh",
            ip_range_begin="10.0.0.1",
            ip_range_end="10.0.0.254",
        )

    def setUp(self):
        self.user = User.objects.create_user(username="tester")
        self.user.user_permissions.add(
            Permission.objects.get(
                codename="change_discoveryscan",
                content_type__app_label="netbox_opennms",
            )
        )
        self.client.force_login(self.user)

    def _url(self):
        return reverse(
            "plugins:netbox_opennms:discoveryscan_trigger", args=[self.scan.pk]
        )

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_trigger_runs_discovery_and_marks_triggered(self, mock_from_server):
        client = mock_from_server.return_value.__enter__.return_value
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)
        client.run_discovery.assert_called_once_with(
            foreign_source=self.scan.foreign_source,
            location=self.scan.monitoring_location,
            ip_range_begin="10.0.0.1",
            ip_range_end="10.0.0.254",
            retries=self.scan.retries,
            timeout=self.scan.timeout,
        )
        self.scan.refresh_from_db()
        self.assertIsNotNone(self.scan.last_triggered)
        messages = list(get_messages(response.wsgi_request))
        self.assertTrue(any("Triggered" in str(m) for m in messages))

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_client_failure_shows_error_and_does_not_mark_triggered(
        self, mock_from_server
    ):
        client = mock_from_server.return_value.__enter__.return_value
        client.run_discovery.side_effect = OpenNMSError("unreachable")
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)
        self.scan.refresh_from_db()
        self.assertIsNone(self.scan.last_triggered)
        messages = list(get_messages(response.wsgi_request))
        self.assertTrue(any("unreachable" in str(m) for m in messages))

    def test_requires_change_permission(self):
        self.user.user_permissions.clear()
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, 403)

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_rejects_retrigger_of_running_scan(self, mock_from_server):
        self.scan.mark_triggered()
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)
        mock_from_server.assert_not_called()
        messages = list(get_messages(response.wsgi_request))
        self.assertTrue(any("already been triggered" in str(m) for m in messages))

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_rejects_retrigger_of_settled_scan(self, mock_from_server):
        self.scan.mark_triggered()
        DiscoveryScan.objects.filter(pk=self.scan.pk).update(settled_at=timezone.now())
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)
        mock_from_server.assert_not_called()
        messages = list(get_messages(response.wsgi_request))
        self.assertTrue(any("already been triggered" in str(m) for m in messages))

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_rejects_retrigger_of_cleaned_up_scan(self, mock_from_server):
        self.scan.mark_triggered()
        DiscoveryScan.objects.filter(pk=self.scan.pk).update(
            settled_at=timezone.now(), cleaned_up_at=timezone.now()
        )
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)
        mock_from_server.assert_not_called()
        messages = list(get_messages(response.wsgi_request))
        self.assertTrue(any("already been triggered" in str(m) for m in messages))


class DiscoveryScanDiscoveredNodesTableTest(TestCase):
    """The scan detail page reuses ``DiscoveredNodeTable`` (issue #54).

    Replaces the old hand-rolled inline ``<table>`` (Node/Verdict/
    Resolution/NetBox object/Completeness) with the same ``NetBoxTable``
    the standalone Discovered Nodes list uses -- verdict badges, sorting,
    pagination, and column configuration all included.
    """

    @classmethod
    def setUpTestData(cls):
        cls.server = OpenNMSServer.objects.create(
            name="Acme", url="https://onms.example"
        )
        cls.requisition = Requisition.objects.create(name="fs-1", location="raleigh")
        cls.scan = DiscoveryScan.objects.create(
            server=cls.server,
            requisition=cls.requisition,
            location="raleigh",
            ip_range_begin="10.0.0.1",
            ip_range_end="10.0.0.254",
        )
        cls.node = DiscoveredNode.objects.create(
            server=cls.server,
            discovery_scan=cls.scan,
            opennms_node_id=1,
            label="node-1",
            verdict="red",
        )

    def setUp(self):
        self.user = User.objects.create_user(username="tester")
        self.user.user_permissions.add(
            Permission.objects.get(
                codename="view_discoveryscan",
                content_type__app_label="netbox_opennms",
            )
        )
        self.client.force_login(self.user)

    def _url(self):
        return reverse("plugins:netbox_opennms:discoveryscan", args=[self.scan.pk])

    def test_context_attaches_discovered_node_table(self):
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        table = response.context["discovered_nodes_table"]
        self.assertIsInstance(table, DiscoveredNodeTable)
        self.assertIn(self.node, list(table.data))

    def test_page_shows_verdict_badge_from_shared_table(self):
        response = self.client.get(self._url())
        self.assertContains(response, "Missing from NetBox")
        self.assertContains(response, "node-1")

    def test_empty_scan_shows_placeholder_not_table(self):
        empty_scan = DiscoveryScan.objects.create(
            server=self.server,
            requisition=self.requisition,
            location="raleigh",
            ip_range_begin="10.0.1.1",
            ip_range_end="10.0.1.254",
        )
        response = self.client.get(
            reverse("plugins:netbox_opennms:discoveryscan", args=[empty_scan.pk])
        )
        self.assertContains(response, "No nodes have been discovered by this scan yet.")


class DiscoveryScanDetailTriggerGatingTest(TestCase):
    """The Discovery Scan detail page's Trigger button gets the same
    disabled+tooltip gating as DiscoveryScanTable's trigger_action column
    once the scan leaves "pending" (issue #53), mirroring #50's server-side
    guard in DiscoveryScanTriggerView.post().
    """

    @classmethod
    def setUpTestData(cls):
        server = OpenNMSServer.objects.create(name="Acme", url="https://onms.example")
        requisition = Requisition.objects.create(name="fs-1", location="raleigh")
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

    def setUp(self):
        self.user = User.objects.create_user(username="tester")
        self.user.user_permissions.add(
            *Permission.objects.filter(
                codename__in=["view_discoveryscan", "change_discoveryscan"],
                content_type__app_label="netbox_opennms",
            )
        )
        self.client.force_login(self.user)

    def _get(self, scan):
        return self.client.get(
            reverse("plugins:netbox_opennms:discoveryscan", args=[scan.pk])
        )

    def test_pending_scan_shows_enabled_trigger_button(self):
        response = self._get(self.pending)
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("Pending", content)
        self.assertNotIn("Already triggered", content)

    def test_running_scan_shows_disabled_trigger_button_and_status(self):
        response = self._get(self.running)
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("Running", content)
        self.assertIn("disabled", content)
        self.assertIn("Already triggered", content)

    def test_settled_scan_shows_disabled_trigger_button_and_status(self):
        response = self._get(self.settled)
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("Settled", content)
        self.assertIn("disabled", content)
        self.assertIn("Already triggered", content)


class DiscoveredNodeViewTest(
    ViewTestCases.GetObjectViewTestCase,
    ViewTestCases.GetObjectChangelogViewTestCase,
    ViewTestCases.DeleteObjectViewTestCase,
    ViewTestCases.ListObjectsViewTestCase,
    ViewTestCases.BulkDeleteObjectsViewTestCase,
):
    # No Create/Edit views: a DiscoveredNode is only ever produced by a scan
    # (OpenNMSServerScanView), never hand-entered (issue #7).
    model = DiscoveredNode

    def _get_base_url(self):
        return "plugins:netbox_opennms:discoverednode_{}"

    @classmethod
    def setUpTestData(cls):
        server = OpenNMSServer.objects.create(name="Acme", url="https://onms.example")
        for i, verdict in enumerate(("green", "orange", "red")):
            DiscoveredNode.objects.create(
                server=server, opennms_node_id=i, label=f"node-{i}", verdict=verdict
            )

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_get_object(self, mock_from_server):
        # DiscoveredNodeView live-fetches OpenNMS data (issue #21) -- stub it
        # out so this inherited test doesn't reach the network.
        client = mock_from_server.return_value.__enter__.return_value
        client.get_node.return_value = {"label": "node-0"}
        client.list_ip_interfaces.return_value = []
        super().test_get_object()


class DiscoveredNodeLiveFetchViewTest(TestCase):
    """DiscoveredNodeView's live OpenNMS data panel (issue #21)."""

    @classmethod
    def setUpTestData(cls):
        cls.server = OpenNMSServer.objects.create(
            name="Acme", url="https://onms.example"
        )
        cls.node = DiscoveredNode.objects.create(
            server=cls.server,
            opennms_node_id=1,
            label="node-1",
            verdict="green",
            node_detail={"label": "cached-node-1"},
            ip_interfaces=[{"ipAddress": "10.0.0.1", "snmpPrimary": "P"}],
            services_by_ip={"10.0.0.1": [{"serviceType": {"name": "ICMP"}}]},
            walked_at=timezone.now(),
        )

    def setUp(self):
        self.user = User.objects.create_user(username="tester")
        self.user.user_permissions.add(
            Permission.objects.get(
                codename="view_discoverednode",
                content_type__app_label="netbox_opennms",
            )
        )
        self.client.force_login(self.user)

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_live_fetch_success_is_shown(self, mock_from_server):
        client = mock_from_server.return_value.__enter__.return_value
        client.get_node.return_value = {
            "label": "live-node-1",
            "sysObjectId": "1.2.3",
        }
        client.list_ip_interfaces.return_value = [
            {"ipAddress": "10.0.0.2", "snmpPrimary": "P"}
        ]
        client.list_services.return_value = [{"serviceType": {"name": "SNMP"}}]
        response = self.client.get(self.node.get_absolute_url())
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("live-node-1", content)
        self.assertIn("10.0.0.2", content)
        self.assertIn("SNMP", content)

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_live_fetch_failure_degrades_to_cache(self, mock_from_server):
        client = mock_from_server.return_value.__enter__.return_value
        client.get_node.side_effect = OpenNMSError("unreachable")
        response = self.client.get(self.node.get_absolute_url())
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIn("Could not reach OpenNMS", content)
        self.assertIn("cached-node-1", content)
        self.assertIn("10.0.0.1", content)


class RequisitionNodesViewTest(TestCase):
    """RequisitionNodesView's Server scoping and match rendering (issue #21)."""

    @classmethod
    def setUpTestData(cls):
        cls.server = OpenNMSServer.objects.create(
            name="Acme", url="https://onms.example"
        )
        cls.other_server = OpenNMSServer.objects.create(
            name="Other", url="https://other.example"
        )
        cls.requisition = Requisition.objects.create(name="fs-1", filter_params=FILTER)
        cls.matched_node = DiscoveredNode.objects.create(
            server=cls.server,
            opennms_node_id=1,
            label="node-1",
            foreign_source="fs-1",
            verdict="red",
        )
        cls.stale_node = DiscoveredNode.objects.create(
            server=cls.other_server,
            opennms_node_id=2,
            label="node-2",
            foreign_source="fs-1",
            verdict="red",
        )

    def setUp(self):
        self.user = User.objects.create_user(username="tester")
        self.user.user_permissions.add(
            Permission.objects.get(
                codename="view_requisition",
                content_type__app_label="netbox_opennms",
            )
        )
        self.client.force_login(self.user)

    def _url(self):
        return reverse(
            "plugins:netbox_opennms:requisition_opennms_nodes",
            args=[self.requisition.pk],
        )

    @mock.patch("netbox_opennms.views.target_server_for")
    def test_nodes_scoped_to_target_server(self, mock_target_server_for):
        mock_target_server_for.return_value = self.server
        response = self.client.get(self._url())
        content = response.content.decode()
        self.assertIn("node-1", content)
        self.assertNotIn("node-2", content)

    @mock.patch("netbox_opennms.views.target_server_for")
    def test_no_netbox_match_rendered_explicitly(self, mock_target_server_for):
        mock_target_server_for.return_value = self.server
        response = self.client.get(self._url())
        self.assertContains(response, "No NetBox match")

    @mock.patch("netbox_opennms.views.target_server_for")
    def test_scan_now_shown_when_target_server_resolves(self, mock_target_server_for):
        mock_target_server_for.return_value = self.server
        response = self.client.get(self._url())
        self.assertContains(
            response,
            reverse("plugins:netbox_opennms:opennmsserver_scan", args=[self.server.pk]),
        )

    @mock.patch("netbox_opennms.views.target_server_for")
    def test_scan_now_hidden_when_target_server_unresolved(
        self, mock_target_server_for
    ):
        mock_target_server_for.return_value = None
        response = self.client.get(self._url())
        content = response.content.decode()
        self.assertNotIn("Scan now", content)
        self.assertContains(response, "could not be resolved")

    @mock.patch("netbox_opennms.views.target_server_for")
    def test_one_time_sync_shown_when_target_server_resolves(
        self, mock_target_server_for
    ):
        mock_target_server_for.return_value = self.server
        response = self.client.get(self._url())
        self.assertContains(
            response,
            reverse(
                "plugins:netbox_opennms:requisition_opennms_pull",
                args=[self.requisition.pk],
            ),
        )

    @mock.patch("netbox_opennms.views.target_server_for")
    def test_one_time_sync_hidden_when_target_server_unresolved(
        self, mock_target_server_for
    ):
        mock_target_server_for.return_value = None
        response = self.client.get(self._url())
        content = response.content.decode()
        self.assertNotIn("One-Time Sync", content)


class RequisitionScanViewTest(TestCase):
    """NodeDiffTable rendering for the scan view (issue #34)."""

    @classmethod
    def setUpTestData(cls):
        cls.server = OpenNMSServer.objects.create(
            name="Acme", url="https://onms.example/opennms"
        )
        cls.site = Site.objects.create(name="Site 1", slug="site-1")
        cls.role = DeviceRole.objects.create(name="Router", slug="router")
        mfr = Manufacturer.objects.create(name="Acme", slug="acme")
        dt = DeviceType.objects.create(manufacturer=mfr, model="M1", slug="m1")
        cls.device = Device.objects.create(
            name="rtr-1", device_type=dt, role=cls.role, site=cls.site
        )
        iface = Interface.objects.create(device=cls.device, name="eth0", type="virtual")
        address = IPAddress.objects.create(address="10.0.0.1/24", assigned_object=iface)
        cls.device.primary_ip4 = address
        cls.device.save()
        cls.requisition = Requisition.objects.create(name="fs-1", filter_params=FILTER)

    def setUp(self):
        self.user = User.objects.create_user(username="tester")
        self.user.user_permissions.add(
            Permission.objects.get(
                codename="view_requisition",
                content_type__app_label="netbox_opennms",
            )
        )
        self.client.force_login(self.user)

    def _url(self):
        return reverse(
            "plugins:netbox_opennms:requisition_scan", args=[self.requisition.pk]
        )

    def test_login_required(self):
        self.client.logout()
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 302)

    @mock.patch("netbox_opennms.views.scan_requisition")
    def test_error_is_shown_and_no_table_rendered(self, mock_scan_requisition):
        mock_scan_requisition.side_effect = OpenNMSError("unreachable")
        response = self.client.get(self._url())
        self.assertContains(response, "Could not reach OpenNMS")
        self.assertIsNone(response.context["table"])

    @mock.patch("netbox_opennms.views.scan_requisition")
    def test_conflicts_freeze_hides_the_table(self, mock_scan_requisition):
        mock_scan_requisition.return_value = RequisitionScanResult(
            foreign_source="fs-1", exists=True, conflicts=["c1"]
        )
        response = self.client.get(self._url())
        self.assertContains(response, "Frozen")
        self.assertIsNone(response.context["table"])

    @mock.patch("netbox_opennms.views.scan_requisition")
    def test_row_status_drives_the_row_css_class(self, mock_scan_requisition):
        mock_scan_requisition.return_value = RequisitionScanResult(
            foreign_source="fs-1",
            exists=True,
            target_server=self.server,
            added=[NodeDiff("device-1", "rtr-1", "added")],
            removed=[NodeDiff("device-2", "rtr-2", "removed")],
            changed=[NodeDiff("device-3", "rtr-3", "changed", ["+interface x"])],
            unchanged=[NodeDiff("device-4", "rtr-4", "unchanged")],
        )
        response = self.client.get(self._url())
        table = response.context["table"]
        rows_by_label = {row.record.label: row for row in table.rows}
        self.assertIn("table-success", rows_by_label["rtr-1"].attrs["class"])
        self.assertIn("table-danger", rows_by_label["rtr-2"].attrs["class"])
        self.assertIn("table-warning", rows_by_label["rtr-3"].attrs["class"])
        self.assertEqual(rows_by_label["rtr-4"].attrs["class"], "")

    @mock.patch("netbox_opennms.views.scan_requisition")
    def test_matched_netbox_object_is_linked(self, mock_scan_requisition):
        mock_scan_requisition.return_value = RequisitionScanResult(
            foreign_source="fs-1",
            exists=True,
            target_server=self.server,
            added=[NodeDiff("device-1", "rtr-1", "added", netbox_object=self.device)],
        )
        response = self.client.get(self._url())
        self.assertContains(response, self.device.get_absolute_url())

    @mock.patch("netbox_opennms.views.scan_requisition")
    def test_unmatched_row_shows_no_match(self, mock_scan_requisition):
        mock_scan_requisition.return_value = RequisitionScanResult(
            foreign_source="fs-1",
            exists=True,
            target_server=self.server,
            removed=[NodeDiff("device-9", "rtr-9", "removed")],
        )
        response = self.client.get(self._url())
        self.assertContains(response, "No match")

    @mock.patch("netbox_opennms.views.scan_requisition")
    def test_added_row_has_no_walk_link_or_opennms_node_link(
        self, mock_scan_requisition
    ):
        # Not yet provisioned in OpenNMS — opennms_node_id stays None.
        mock_scan_requisition.return_value = RequisitionScanResult(
            foreign_source="fs-1",
            exists=False,
            target_server=self.server,
            added=[NodeDiff("device-1", "rtr-1", "added")],
        )
        response = self.client.get(self._url())
        self.assertNotContains(
            response,
            reverse(
                "plugins:netbox_opennms:requisition_node_walk",
                args=[self.requisition.pk, 1],
            ),
        )

    @mock.patch("netbox_opennms.views.scan_requisition")
    def test_row_with_opennms_node_id_links_to_the_walk_view(
        self, mock_scan_requisition
    ):
        node = NodeDiff("device-1", "rtr-1", "unchanged")
        node.opennms_node_id = 42
        mock_scan_requisition.return_value = RequisitionScanResult(
            foreign_source="fs-1",
            exists=True,
            target_server=self.server,
            unchanged=[node],
        )
        response = self.client.get(self._url())
        self.assertContains(
            response,
            reverse(
                "plugins:netbox_opennms:requisition_node_walk",
                args=[self.requisition.pk, 42],
            ),
        )

    @mock.patch("netbox_opennms.views.scan_requisition")
    def test_opennms_node_column_shows_the_live_label_and_opens_in_a_new_tab(
        self, mock_scan_requisition
    ):
        # Issue #38: the "OpenNMS node" column links to the live OpenNMS node
        # but must show its live OpenNMS *label*, not the raw numeric id, and
        # must open the link in a new tab rather than navigating away.
        node = NodeDiff("device-1", "rtr-1-desired", "unchanged")
        node.opennms_node_id = 42
        node.opennms_node_label = "rtr-1-live"
        mock_scan_requisition.return_value = RequisitionScanResult(
            foreign_source="fs-1",
            exists=True,
            target_server=self.server,
            unchanged=[node],
        )
        response = self.client.get(self._url())
        content = response.content.decode()
        self.assertIn("rtr-1-live", content)
        self.assertNotIn(">42<", content)
        self.assertIn('target="_blank"', content)
        self.assertIn('rel="noopener"', content)

    @mock.patch("netbox_opennms.views.scan_requisition")
    def test_opennms_node_column_falls_back_to_id_without_a_live_label(
        self, mock_scan_requisition
    ):
        # A defensive fallback for the unlikely case OpenNMS didn't report a
        # label for this node — better to show the id than nothing at all.
        node = NodeDiff("device-1", "rtr-1-desired", "unchanged")
        node.opennms_node_id = 42
        mock_scan_requisition.return_value = RequisitionScanResult(
            foreign_source="fs-1",
            exists=True,
            target_server=self.server,
            unchanged=[node],
        )
        response = self.client.get(self._url())
        self.assertContains(response, ">42<")


class RequisitionNodeWalkViewTest(TestCase):
    """RequisitionNodeWalkView's live SNMP/neighbor-link rendering (issue #34)."""

    @classmethod
    def setUpTestData(cls):
        cls.server = OpenNMSServer.objects.create(
            name="Acme", url="https://onms.example/opennms"
        )
        cls.requisition = Requisition.objects.create(name="fs-1", filter_params=FILTER)

    def setUp(self):
        self.user = User.objects.create_user(username="tester")
        self.user.user_permissions.add(
            Permission.objects.get(
                codename="view_requisition",
                content_type__app_label="netbox_opennms",
            )
        )
        self.client.force_login(self.user)

    def _url(self, node_id=42):
        return reverse(
            "plugins:netbox_opennms:requisition_node_walk",
            args=[self.requisition.pk, node_id],
        )

    def test_login_required(self):
        self.client.logout()
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 302)

    @mock.patch("netbox_opennms.views.target_server_for")
    def test_unresolved_server_shows_error(self, mock_target_server_for):
        mock_target_server_for.return_value = None
        response = self.client.get(self._url())
        self.assertContains(response, "could not be resolved")

    @mock.patch("netbox_opennms.views.target_server_for")
    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_fetch_failure_is_shown(self, mock_from_server, mock_target_server_for):
        mock_target_server_for.return_value = self.server
        client = mock_from_server.return_value.__enter__.return_value
        client.list_snmp_interfaces.side_effect = OpenNMSError("unreachable")
        response = self.client.get(self._url())
        self.assertContains(response, "Could not reach OpenNMS")

    @mock.patch("netbox_opennms.views.target_server_for")
    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_snmp_interfaces_and_links_are_rendered(
        self, mock_from_server, mock_target_server_for
    ):
        mock_target_server_for.return_value = self.server
        client = mock_from_server.return_value.__enter__.return_value
        client.list_snmp_interfaces.return_value = [
            {
                "ifIndex": 1,
                "ifName": "eth0",
                "ifDescr": "eth0",
                "ifAlias": "wan",
                "ifAdminStatus": 1,
            }
        ]
        client.get_node_links.return_value = {
            "lldpLinkNodes": {
                "lldpLocalPort": "eth0",
                "lldpRemChassisId": "switch-1",
                "ldpRemPort": "Gi0/1",
            }
        }
        client.get_node.return_value = {}
        client.list_ip_interfaces.return_value = []
        response = self.client.get(self._url())
        content = response.content.decode()
        self.assertIn("eth0", content)
        self.assertIn("switch-1", content)

    @mock.patch("netbox_opennms.views.target_server_for")
    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_categories_and_assets_show_mapping_status(
        self, mock_from_server, mock_target_server_for
    ):
        # Issue #39: assets that match a configured Asset Mapping are flagged
        # as such, distinct from ones with no mapping configured. Likewise,
        # categories already live on the node that a policy also targets are
        # flagged as already existing, categories only targeted by a policy
        # are flagged as pending, and categories with no policy at all are
        # unbadged.
        AssetMapping.objects.create(
            requisition=self.requisition,
            netbox_source="serial",
            asset_field="serialNumber",
        )
        MonitoringPolicy.objects.create(
            requisition=self.requisition,
            name="cat-routers",
            preset="set-node-category",
            parameters={"category": "Routers"},
        )
        MonitoringPolicy.objects.create(
            requisition=self.requisition,
            name="cat-servers",
            preset="set-node-category",
            parameters={"category": "Servers"},
        )
        mock_target_server_for.return_value = self.server
        client = mock_from_server.return_value.__enter__.return_value
        client.list_snmp_interfaces.return_value = []
        client.get_node_links.return_value = None
        client.get_node.return_value = {
            "categories": {"category": [{"name": "Routers"}, {"name": "Edge"}]},
            "assetRecord": {
                "serialNumber": "ABC123",
                "assetNumber": "UNMAPPED-1",
            },
        }
        client.list_ip_interfaces.return_value = []
        response = self.client.get(self._url())
        content = response.content.decode()
        self.assertIn("Routers", content)
        self.assertIn("Edge", content)
        self.assertIn("Servers", content)
        self.assertIn("already exists", content)
        self.assertIn("would be created by policy", content)
        self.assertIn("ABC123", content)
        self.assertIn("UNMAPPED-1", content)
        self.assertIn("Mapped", content)
        self.assertIn("Unmapped", content)

        category_rows = {row["name"]: row for row in response.context["category_rows"]}
        self.assertTrue(category_rows["Routers"]["already_exists"])
        self.assertFalse(category_rows["Edge"].get("already_exists"))
        self.assertFalse(category_rows["Edge"].get("pending_policy"))
        self.assertTrue(category_rows["Servers"]["pending_policy"])

    @mock.patch("netbox_opennms.views.target_server_for")
    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_snmp_metadata_excludes_categories_and_assets(
        self, mock_from_server, mock_target_server_for
    ):
        mock_target_server_for.return_value = self.server
        client = mock_from_server.return_value.__enter__.return_value
        client.list_snmp_interfaces.return_value = []
        client.get_node_links.return_value = None
        client.get_node.return_value = {
            "sysObjectId": "1.3.6.1.4.1.9.1.1",
            "sysLocation": "DC1",
            "categories": {"category": [{"name": "Routers"}]},
            "assetRecord": {"serialNumber": "ABC123"},
        }
        client.list_ip_interfaces.return_value = []
        response = self.client.get(self._url())
        content = response.content.decode()
        self.assertIn("sysObjectId", content)
        self.assertIn("1.3.6.1.4.1.9.1.1", content)
        self.assertIn("sysLocation", content)
        self.assertIn("DC1", content)

    @mock.patch("netbox_opennms.views.target_server_for")
    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_ip_interfaces_and_services_are_rendered(
        self, mock_from_server, mock_target_server_for
    ):
        mock_target_server_for.return_value = self.server
        client = mock_from_server.return_value.__enter__.return_value
        client.list_snmp_interfaces.return_value = []
        client.get_node_links.return_value = None
        client.get_node.return_value = {}
        client.list_ip_interfaces.return_value = [
            {"ipAddress": "10.0.0.1", "snmpPrimary": "P"}
        ]
        client.list_services.return_value = [{"serviceType": {"name": "ICMP"}}]
        response = self.client.get(self._url())
        content = response.content.decode()
        self.assertIn("10.0.0.1", content)
        self.assertIn("ICMP", content)

    @mock.patch("netbox_opennms.views.target_server_for")
    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_no_categories_assets_or_snmp_data_renders_cleanly(
        self, mock_from_server, mock_target_server_for
    ):
        # Issue #39 acceptance criterion: a node with nothing to report on
        # any of these fronts renders cleanly rather than erroring (related
        # to issue #40's spurious "unparseable snmpinterfaces" bug).
        mock_target_server_for.return_value = self.server
        client = mock_from_server.return_value.__enter__.return_value
        client.list_snmp_interfaces.return_value = []
        client.get_node_links.return_value = None
        client.get_node.return_value = {}
        client.list_ip_interfaces.return_value = []
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        content = response.content.decode()
        self.assertIsNone(response.context.get("error"))
        self.assertIn("No categories reported", content)
        self.assertIn("No asset record fields reported", content)
        self.assertIn("No SNMP metadata reported", content)
        self.assertIn("No SNMP interfaces available", content)
        self.assertIn("No IP interfaces available", content)


class RequisitionSyncNodeViewTest(TestCase):
    """RequisitionSyncNodeView: single-node push from a scan row (issue #35)."""

    @classmethod
    def setUpTestData(cls):
        cls.server = OpenNMSServer.objects.create(
            name="Acme", url="https://onms.example/opennms"
        )
        cls.requisition = Requisition.objects.create(name="fs-1", filter_params=FILTER)

    def setUp(self):
        self.user = User.objects.create_user(username="tester")
        self.user.user_permissions.add(
            Permission.objects.get(
                codename="change_requisition",
                content_type__app_label="netbox_opennms",
            )
        )
        self.client.force_login(self.user)

    def _url(self, foreign_id="device-1"):
        return reverse(
            "plugins:netbox_opennms:requisition_sync_node",
            args=[self.requisition.pk, foreign_id],
        )

    def _node(self, foreign_id="device-1"):
        return NodeSpec(
            node_label="rtr-1",
            foreign_id=foreign_id,
            location="",
            interfaces=[InterfaceSpec("10.0.0.1", "P", services=["ICMP"])],
        )

    def _resolution(self, **kw):
        kw.setdefault("nodes", [self._node()])
        kw.setdefault("server", self.server)
        return Resolution(foreign_source="fs-1", requisition=self.requisition, **kw)

    def test_login_required(self):
        self.client.logout()
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)

    def test_requires_change_permission(self):
        self.user.user_permissions.clear()
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, 403)

    @mock.patch("netbox_opennms.views.resolve")
    def test_no_resolution_shows_error(self, mock_resolve):
        mock_resolve.return_value = None
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)
        messages = list(get_messages(response.wsgi_request))
        self.assertTrue(any("no resolvable membership" in str(m) for m in messages))

    @mock.patch("netbox_opennms.views.resolve")
    def test_conflicts_block_the_push(self, mock_resolve):
        mock_resolve.return_value = self._resolution(
            conflicts=[Conflict("dup", "device-1", ["fs-1", "fs-2"])]
        )
        response = self.client.post(self._url())
        messages = list(get_messages(response.wsgi_request))
        self.assertTrue(any("matched by" in str(m) for m in messages))

    @mock.patch("netbox_opennms.views.resolve")
    def test_server_conflict_blocks_the_push(self, mock_resolve):
        mock_resolve.return_value = self._resolution(
            server=None, server_conflict=ServerConflict(["Acme", "Other"])
        )
        response = self.client.post(self._url())
        messages = list(get_messages(response.wsgi_request))
        self.assertTrue(any("different OpenNMS Servers" in str(m) for m in messages))

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    @mock.patch("netbox_opennms.views.resolve")
    def test_missing_node_shows_error(self, mock_resolve, mock_from_server):
        mock_resolve.return_value = self._resolution(nodes=[self._node("other-id")])
        client = mock_from_server.return_value.__enter__.return_value
        client.get_requisition.return_value = None
        response = self.client.post(self._url(foreign_id="device-1"))
        messages = list(get_messages(response.wsgi_request))
        self.assertTrue(any("no longer part of" in str(m) for m in messages))
        client.post_node.assert_not_called()

    @mock.patch("netbox_opennms.views.resolve")
    def test_unhealthy_server_blocks_the_push(self, mock_resolve):
        self.server.last_check_status = "failed"
        self.server.last_check_message = "connection refused"
        self.server.save()
        mock_resolve.return_value = self._resolution()
        response = self.client.post(self._url())
        messages = list(get_messages(response.wsgi_request))
        self.assertTrue(any("unhealthy" in str(m) for m in messages))

    @mock.patch("netbox_opennms.views.get_plugin_config")
    @mock.patch("netbox_opennms.views.resolve")
    def test_invalid_import_mode_shows_error(self, mock_resolve, mock_cfg):
        mock_resolve.return_value = self._resolution()
        mock_cfg.side_effect = lambda _plugin, key: (
            "bogus" if key == "import_mode" else ""
        )
        response = self.client.post(self._url())
        messages = list(get_messages(response.wsgi_request))
        self.assertTrue(any("Invalid import_mode" in str(m) for m in messages))

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    @mock.patch("netbox_opennms.views.resolve")
    def test_adopts_the_existing_foreign_id_before_matching_the_row(
        self, mock_resolve, mock_from_server
    ):
        # The scan row's foreign_id is the ADOPTED id
        # (requisition_scan.scan_requisition runs adopt_foreign_ids before
        # building rows) — a freshly-resolved NodeSpec carries the un-adopted
        # id until this view runs the same adoption pass.
        mock_resolve.return_value = self._resolution(
            nodes=[self._node(foreign_id="netbox-device-1")]
        )
        client = mock_from_server.return_value.__enter__.return_value
        client.get_requisition.return_value = {
            "node": {"node-label": "rtr-1", "foreign-id": "legacy-42"}
        }
        response = self.client.post(self._url(foreign_id="legacy-42"))
        self.assertEqual(response.status_code, 302)
        client.post_node.assert_called_once()
        messages = list(get_messages(response.wsgi_request))
        self.assertTrue(any("pushed to OpenNMS" in str(m) for m in messages))

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    @mock.patch("netbox_opennms.views.resolve")
    def test_success_pushes_node_and_imports(self, mock_resolve, mock_from_server):
        mock_resolve.return_value = self._resolution()
        client = mock_from_server.return_value.__enter__.return_value
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)
        client.post_node.assert_called_once()
        args, _ = client.post_node.call_args
        self.assertEqual(args[0], "fs-1")
        client.import_requisition.assert_called_once()
        messages = list(get_messages(response.wsgi_request))
        self.assertTrue(any("pushed to OpenNMS" in str(m) for m in messages))

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    @mock.patch("netbox_opennms.views.resolve")
    def test_client_failure_is_reported_per_node(self, mock_resolve, mock_from_server):
        mock_resolve.return_value = self._resolution()
        client = mock_from_server.return_value.__enter__.return_value
        client.post_node.side_effect = OpenNMSError("unreachable")
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)
        messages = list(get_messages(response.wsgi_request))
        self.assertTrue(any("unreachable" in str(m) for m in messages))

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    @mock.patch("netbox_opennms.views.resolve")
    def test_render_error_is_reported_per_node(self, mock_resolve, mock_from_server):
        mock_resolve.return_value = self._resolution(
            nodes=[self._node(foreign_id="device-1")]
        )
        client = mock_from_server.return_value.__enter__.return_value
        client.get_requisition.return_value = None
        with mock.patch(
            "netbox_opennms.views.render_node_document",
            side_effect=RenderError("no node-label"),
        ):
            response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)
        messages = list(get_messages(response.wsgi_request))
        self.assertTrue(any("failed" in str(m) for m in messages))


class RequisitionSyncNodeOverrideViewTest(TestCase):
    """RequisitionSyncNodeOverrideView: Foreign ID override push (issue #36).

    Shares ``_prepare_node_push``/``_find_node_after_adoption`` with
    ``RequisitionSyncNodeView`` (see ``RequisitionSyncNodeViewTest`` for full
    coverage of those shared gates) — this class covers what's specific to the
    override: deriving the new Foreign ID, pushing under it, and leaving the
    old node alone (grilled decisions for #36).
    """

    @classmethod
    def setUpTestData(cls):
        cls.server = OpenNMSServer.objects.create(
            name="Acme", url="https://onms.example/opennms"
        )
        cls.requisition = Requisition.objects.create(name="fs-1", filter_params=FILTER)
        cls.device = _devices(1)[0]

    def setUp(self):
        self.user = User.objects.create_user(username="tester")
        self.user.user_permissions.add(
            Permission.objects.get(
                codename="change_requisition",
                content_type__app_label="netbox_opennms",
            )
        )
        self.client.force_login(self.user)

    def _url(self, foreign_id="legacy-42"):
        return reverse(
            "plugins:netbox_opennms:requisition_sync_node_override",
            args=[self.requisition.pk, foreign_id],
        )

    def _node(self, foreign_id="legacy-42"):
        return NodeSpec(
            node_label="rtr-1",
            foreign_id=foreign_id,
            location="",
            interfaces=[InterfaceSpec("10.0.0.1", "P", services=["ICMP"])],
            netbox_object=self.device,
        )

    def _resolution(self, **kw):
        kw.setdefault("nodes", [self._node()])
        kw.setdefault("server", self.server)
        return Resolution(foreign_source="fs-1", requisition=self.requisition, **kw)

    def test_login_required(self):
        self.client.logout()
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login/", response.url)

    def test_requires_change_permission(self):
        self.user.user_permissions.clear()
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, 403)

    @mock.patch("netbox_opennms.views.resolve")
    def test_conflicts_block_the_push(self, mock_resolve):
        mock_resolve.return_value = self._resolution(
            conflicts=[Conflict("dup", "legacy-42", ["fs-1", "fs-2"])]
        )
        response = self.client.post(self._url())
        messages = list(get_messages(response.wsgi_request))
        self.assertTrue(any("matched by" in str(m) for m in messages))

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    @mock.patch("netbox_opennms.views.resolve")
    def test_missing_node_shows_error(self, mock_resolve, mock_from_server):
        mock_resolve.return_value = self._resolution(nodes=[self._node("other-id")])
        client = mock_from_server.return_value.__enter__.return_value
        client.get_requisition.return_value = None
        response = self.client.post(self._url(foreign_id="legacy-42"))
        messages = list(get_messages(response.wsgi_request))
        self.assertTrue(any("no longer part of" in str(m) for m in messages))
        client.post_node.assert_not_called()

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    @mock.patch("netbox_opennms.views.resolve")
    def test_success_pushes_node_under_the_derived_foreign_id(
        self, mock_resolve, mock_from_server
    ):
        mock_resolve.return_value = self._resolution()
        client = mock_from_server.return_value.__enter__.return_value
        client.get_requisition.return_value = None
        response = self.client.post(self._url(foreign_id="legacy-42"))
        self.assertEqual(response.status_code, 302)

        expected_id = f"netbox-device-{self.device.pk}"
        client.post_node.assert_called_once()
        args, _ = client.post_node.call_args
        self.assertEqual(args[0], "fs-1")
        self.assertIn(expected_id.encode(), args[1])
        client.import_requisition.assert_called_once()

        messages = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertTrue(
            any(
                expected_id in m and "legacy-42" in m and "left untouched" in m
                for m in messages
            )
        )

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    @mock.patch("netbox_opennms.views.resolve")
    def test_old_node_and_foreign_source_are_left_untouched(
        self, mock_resolve, mock_from_server
    ):
        # #36's grilled decision: the old node stays in OpenNMS, and this
        # scopes to the one node — the Foreign Source shell isn't touched.
        mock_resolve.return_value = self._resolution()
        client = mock_from_server.return_value.__enter__.return_value
        client.get_requisition.return_value = None
        self.client.post(self._url(foreign_id="legacy-42"))
        client.delete_requisition.assert_not_called()
        client.delete_foreign_source.assert_not_called()
        client.post_requisition.assert_not_called()

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    @mock.patch("netbox_opennms.views.resolve")
    def test_client_failure_is_reported_per_node(self, mock_resolve, mock_from_server):
        mock_resolve.return_value = self._resolution()
        client = mock_from_server.return_value.__enter__.return_value
        client.get_requisition.return_value = None
        client.post_node.side_effect = OpenNMSError("unreachable")
        response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)
        messages = list(get_messages(response.wsgi_request))
        self.assertTrue(any("unreachable" in str(m) for m in messages))

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    @mock.patch("netbox_opennms.views.resolve")
    def test_render_error_is_reported_per_node(self, mock_resolve, mock_from_server):
        mock_resolve.return_value = self._resolution()
        client = mock_from_server.return_value.__enter__.return_value
        client.get_requisition.return_value = None
        with mock.patch(
            "netbox_opennms.views.render_node_document",
            side_effect=RenderError("no node-label"),
        ):
            response = self.client.post(self._url())
        self.assertEqual(response.status_code, 302)
        messages = list(get_messages(response.wsgi_request))
        self.assertTrue(any("failed" in str(m) for m in messages))


class MetadataContextViewTest(
    ViewTestCases.GetObjectViewTestCase,
    ViewTestCases.GetObjectChangelogViewTestCase,
    ViewTestCases.CreateObjectViewTestCase,
    ViewTestCases.ListObjectsViewTestCase,
):
    """Get/changelog/create/list only.

    Edit/Delete/BulkDelete are deliberately NOT exercised through NetBox's
    generic ``ViewTestCases`` mixins here: those mixins operate on
    ``self._get_queryset().first()``, and migration 0020 seeds five
    ``is_builtin=True`` rows alongside whatever ``setUpTestData`` creates —
    which row sorts first under ``Meta.ordering = ("name",)`` depends on the
    test database's collation (e.g. Postgres's default locale collation
    interleaves case, unlike SQLite's byte-order collation), so ``.first()``
    is not reliably one of the non-builtin rows created below. Since
    MetadataContextEditView/DeleteView/BulkDeleteView all intentionally
    exclude built-in rows from their querysets (issue #41's undeletable-base
    guarantee), a generic mixin landing on a builtin row would 404
    unpredictably depending on the backend, not because of a real bug. The
    hand-written tests below instead target known rows by name, and assert
    the protection semantics directly rather than incidentally.
    """

    model = MetadataContext

    def _get_base_url(self):
        return "plugins:netbox_opennms:metadatacontext_{}"

    @classmethod
    def setUpTestData(cls):
        for name in ("X-vt-1", "X-vt-2", "X-vt-3"):
            MetadataContext.objects.create(name=name)
        cls.form_data = {
            "name": "X-vt-4",
            "description": "created by the view test suite",
        }


class MetadataContextProtectionViewTest(TestCase):
    """Built-in rows are immutable/undeletable through the CRUD views (#41)."""

    def setUp(self):
        self.user = User.objects.create_user(username="mc-tester")
        self.client.force_login(self.user)

    def _grant(self, *codenames):
        for codename in codenames:
            self.user.user_permissions.add(
                Permission.objects.get(
                    codename=codename, content_type__app_label="netbox_opennms"
                )
            )

    def test_builtin_edit_view_404s(self):
        builtin = MetadataContext.objects.get(name="node")
        self._grant("change_metadatacontext")
        url = reverse("plugins:netbox_opennms:metadatacontext_edit", args=[builtin.pk])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_builtin_delete_view_404s(self):
        builtin = MetadataContext.objects.get(name="node")
        self._grant("delete_metadatacontext")
        url = reverse(
            "plugins:netbox_opennms:metadatacontext_delete", args=[builtin.pk]
        )
        response = self.client.post(url, data={"confirm": "true"})
        self.assertEqual(response.status_code, 404)
        self.assertTrue(MetadataContext.objects.filter(pk=builtin.pk).exists())

    def test_custom_context_delete_view_succeeds(self):
        custom = MetadataContext.objects.create(name="X-protection-test")
        self._grant("delete_metadatacontext")
        url = reverse("plugins:netbox_opennms:metadatacontext_delete", args=[custom.pk])
        response = self.client.post(url, data={"confirm": "true"})
        self.assertIn(response.status_code, (200, 302))
        self.assertFalse(MetadataContext.objects.filter(pk=custom.pk).exists())

    def test_bulk_delete_skips_builtin_rows(self):
        builtin = MetadataContext.objects.get(name="node")
        custom = MetadataContext.objects.create(name="X-bulk-test")
        self._grant("delete_metadatacontext")
        url = reverse("plugins:netbox_opennms:metadatacontext_bulk_delete")
        self.client.post(
            url,
            data={"pk": [builtin.pk, custom.pk], "confirm": "true", "_confirm": "1"},
        )
        # The builtin row is excluded from BulkDeleteView's queryset (issue
        # #41's protection), so it must survive regardless of the response
        # returned for the (partially invalid) selection.
        self.assertTrue(MetadataContext.objects.filter(pk=builtin.pk).exists())


class MetadataKeyViewTest(
    ViewTestCases.GetObjectViewTestCase,
    ViewTestCases.GetObjectChangelogViewTestCase,
    ViewTestCases.CreateObjectViewTestCase,
    ViewTestCases.ListObjectsViewTestCase,
):
    """Get/changelog/create/list only -- see MetadataContextViewTest above for
    why Edit/Delete/BulkDelete aren't exercised through the generic mixins
    (the same builtin-row-vs-collation ordering flake risk applies here).
    """

    model = MetadataKey

    def _get_base_url(self):
        return "plugins:netbox_opennms:metadatakey_{}"

    @classmethod
    def setUpTestData(cls):
        context = MetadataContext.objects.create(name="X-vt-keys")
        for name in ("X-vt-key-1", "X-vt-key-2", "X-vt-key-3"):
            MetadataKey.objects.create(context=context, name=name)
        cls.form_data = {
            "context": context.pk,
            "name": "X-vt-key-4",
            "description": "created by the view test suite",
        }


class MetadataKeyProtectionViewTest(TestCase):
    """Built-in rows are immutable/undeletable through the CRUD views (#41)."""

    def setUp(self):
        self.user = User.objects.create_user(username="mk-tester")
        self.client.force_login(self.user)

    def _grant(self, *codenames):
        for codename in codenames:
            self.user.user_permissions.add(
                Permission.objects.get(
                    codename=codename, content_type__app_label="netbox_opennms"
                )
            )

    def test_builtin_edit_view_404s(self):
        builtin = MetadataKey.objects.get(context__name="node", name="label")
        self._grant("change_metadatakey")
        url = reverse("plugins:netbox_opennms:metadatakey_edit", args=[builtin.pk])
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_builtin_delete_view_404s(self):
        builtin = MetadataKey.objects.get(context__name="node", name="label")
        self._grant("delete_metadatakey")
        url = reverse("plugins:netbox_opennms:metadatakey_delete", args=[builtin.pk])
        response = self.client.post(url, data={"confirm": "true"})
        self.assertEqual(response.status_code, 404)
        self.assertTrue(MetadataKey.objects.filter(pk=builtin.pk).exists())

    def test_custom_key_delete_view_succeeds(self):
        context = MetadataContext.objects.get(name="node")
        custom = MetadataKey.objects.create(context=context, name="X-protection-test")
        self._grant("delete_metadatakey")
        url = reverse("plugins:netbox_opennms:metadatakey_delete", args=[custom.pk])
        response = self.client.post(url, data={"confirm": "true"})
        self.assertIn(response.status_code, (200, 302))
        self.assertFalse(MetadataKey.objects.filter(pk=custom.pk).exists())

    def test_bulk_delete_skips_builtin_rows(self):
        builtin = MetadataKey.objects.get(context__name="node", name="label")
        context = MetadataContext.objects.get(name="node")
        custom = MetadataKey.objects.create(context=context, name="X-bulk-test")
        self._grant("delete_metadatakey")
        url = reverse("plugins:netbox_opennms:metadatakey_bulk_delete")
        self.client.post(
            url,
            data={"pk": [builtin.pk, custom.pk], "confirm": "true", "_confirm": "1"},
        )
        # The builtin row is excluded from BulkDeleteView's queryset (issue
        # #41's protection), so it must survive regardless of the response
        # returned for the (partially invalid) selection.
        self.assertTrue(MetadataKey.objects.filter(pk=builtin.pk).exists())


class MetadataEntryViewTest(
    ViewTestCases.GetObjectViewTestCase,
    ViewTestCases.GetObjectChangelogViewTestCase,
    ViewTestCases.CreateObjectViewTestCase,
    ViewTestCases.EditObjectViewTestCase,
    ViewTestCases.DeleteObjectViewTestCase,
    ViewTestCases.ListObjectsViewTestCase,
    ViewTestCases.BulkDeleteObjectsViewTestCase,
):
    model = MetadataEntry

    def _get_base_url(self):
        return "plugins:netbox_opennms:metadataentry_{}"

    @classmethod
    def setUpTestData(cls):
        req = Requisition.objects.create(
            name="me-req", filter_params={"role": ["switch"]}
        )
        for scope, key in [("node", "k1"), ("node", "k2"), ("interface", "k3")]:
            MetadataEntry.objects.create(
                requisition=req,
                scope=scope,
                context="requisition",
                key=key,
                literal_value="v",
            )
        cls.form_data = {
            "requisition": req.pk,
            "scope": "node",
            "context": "requisition",
            "key": "k4",
            "value_source": "",
            "literal_value": "v4",
        }
