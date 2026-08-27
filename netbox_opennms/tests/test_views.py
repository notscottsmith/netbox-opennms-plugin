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
from netbox_opennms.models import (
    AssetMapping,
    DiscoveredNode,
    DiscoveryScan,
    MetadataEntry,
    MonitoredInterface,
    MonitoredService,
    MonitoringDetector,
    MonitoringExclusion,
    MonitoringOverride,
    MonitoringPolicy,
    OpenNMSServer,
    Requisition,
)

DETECTOR_CLASS = "org.opennms.netmgt.provision.detector.icmp.IcmpDetector"
POLICY_CLASS = (
    "org.opennms.netmgt.provision.persist.policies.NodeCategorySettingPolicy"
)
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
        server = OpenNMSServer.objects.create(
            name="Acme", url="https://onms.example"
        )
        requisition = Requisition.objects.create(name="fs-1")
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
            "location": "raleigh",
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
        server = OpenNMSServer.objects.create(
            name="Acme", url="https://onms.example"
        )
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
    def test_scan_now_shown_when_target_server_resolves(
        self, mock_target_server_for
    ):
        mock_target_server_for.return_value = self.server
        response = self.client.get(self._url())
        self.assertContains(
            response,
            reverse(
                "plugins:netbox_opennms:opennmsserver_scan", args=[self.server.pk]
            ),
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
                requisition=req, scope=scope, context="requisition",
                key=key, literal_value="v",
            )
        cls.form_data = {
            "requisition": req.pk,
            "scope": "node",
            "context": "requisition",
            "key": "k4",
            "value_source": "",
            "literal_value": "v4",
        }
