# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""REST API tests for the plugin models."""

import unittest

from dcim.models import (
    Device,
    DeviceRole,
    DeviceType,
    Interface,
    Manufacturer,
    Site,
)
from django.test import TestCase
from ipam.models import VRF, IPAddress
from utilities.testing import APIViewTestCases

from netbox_opennms.api.serializers import (
    DiscoveryScanSerializer,
    OpenNMSServerSerializer,
)
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
    VRFAssignment,
)

DETECTOR_CLASS = "org.opennms.netmgt.provision.detector.icmp.IcmpDetector"
POLICY_CLASS = (
    "org.opennms.netmgt.provision.persist.policies.NodeCategorySettingPolicy"
)
FILTER = {"site": ["raleigh"], "role": ["router"]}


class _NoGraphQL:
    """Mixin: GraphQL is out of scope (no schema)."""

    graphql_auto_filter_required = False

    @unittest.skip("GraphQL not implemented yet (deferred)")
    def test_graphql_get_object(self):
        pass

    @unittest.skip("GraphQL not implemented yet (deferred)")
    def test_graphql_list_objects(self):
        pass

    @unittest.skip("GraphQL not implemented yet (deferred)")
    def test_graphql_filter_objects(self):
        pass


def _devices(count):
    site = Site.objects.create(name="Site 1", slug="site-1")
    mfr = Manufacturer.objects.create(name="Acme", slug="acme")
    dt = DeviceType.objects.create(manufacturer=mfr, model="Model 1", slug="model-1")
    role = DeviceRole.objects.create(name="Router", slug="router")
    return [
        Device.objects.create(name=f"device-{i}", device_type=dt, role=role, site=site)
        for i in range(count)
    ]


class RequisitionAPITest(_NoGraphQL, APIViewTestCases.APIViewTestCase):
    model = Requisition
    view_namespace = "plugins-api:netbox_opennms"
    brief_fields = ["display", "id", "name", "url"]

    @classmethod
    def setUpTestData(cls):
        for name in ("r1", "r2", "r3"):
            Requisition.objects.create(name=name, filter_params=FILTER)
        cls.create_data = [
            {"name": "r4", "object_types": "both", "filter_params": {"site": ["rdu"]}},
            {"name": "r5", "filter_params": {"role": ["router"]}},
            {"name": "r6", "filter_params": {"site": ["raleigh"]}},
        ]


class MonitoringDetectorAPITest(_NoGraphQL, APIViewTestCases.APIViewTestCase):
    model = MonitoringDetector
    view_namespace = "plugins-api:netbox_opennms"
    brief_fields = ["display", "id", "name", "url"]

    @classmethod
    def setUpTestData(cls):
        req = Requisition.objects.create(name="req", filter_params=FILTER)
        for name in ("d1", "d2", "d3"):
            MonitoringDetector.objects.create(
                requisition=req, name=name, rule_class=DETECTOR_CLASS
            )
        cls.create_data = [
            {"requisition": req.pk, "name": "d4", "rule_class": DETECTOR_CLASS},
            {"requisition": req.pk, "name": "d5", "rule_class": DETECTOR_CLASS},
            {"requisition": req.pk, "name": "d6", "rule_class": DETECTOR_CLASS},
        ]


class MonitoringPolicyAPITest(_NoGraphQL, APIViewTestCases.APIViewTestCase):
    model = MonitoringPolicy
    view_namespace = "plugins-api:netbox_opennms"
    brief_fields = ["display", "id", "name", "url"]

    @classmethod
    def setUpTestData(cls):
        req = Requisition.objects.create(name="req", filter_params=FILTER)
        for name in ("p1", "p2", "p3"):
            MonitoringPolicy.objects.create(
                requisition=req, name=name, rule_class=POLICY_CLASS
            )
        cls.create_data = [
            {"requisition": req.pk, "name": "p4", "rule_class": POLICY_CLASS},
            {"requisition": req.pk, "name": "p5", "rule_class": POLICY_CLASS},
            {"requisition": req.pk, "name": "p6", "rule_class": POLICY_CLASS},
        ]


class MonitoringOverrideAPITest(_NoGraphQL, APIViewTestCases.APIViewTestCase):
    model = MonitoringOverride
    view_namespace = "plugins-api:netbox_opennms"
    brief_fields = ["display", "exclude", "id", "url"]

    @classmethod
    def setUpTestData(cls):
        devices = _devices(6)
        for device in devices[:3]:
            MonitoringOverride.objects.create(assigned_object=device)
        cls.create_data = [
            {"assigned_object_type": "dcim.device", "assigned_object_id": d.pk}
            for d in devices[3:6]
        ]


class MonitoredServiceAPITest(_NoGraphQL, APIViewTestCases.APIViewTestCase):
    model = MonitoredService
    view_namespace = "plugins-api:netbox_opennms"
    brief_fields = ["display", "id", "name", "url"]

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
        cls.create_data = [
            {"override": override.pk, "ip_address": ips[2].pk, "name": "ICMP"},
            {"override": override.pk, "ip_address": ips[3].pk, "name": "SNMP"},
            {"override": override.pk, "ip_address": ips[4].pk, "name": "HTTP"},
        ]


class MonitoredInterfaceAPITest(_NoGraphQL, APIViewTestCases.APIViewTestCase):
    model = MonitoredInterface
    view_namespace = "plugins-api:netbox_opennms"
    brief_fields = ["display", "id", "role", "url"]

    @classmethod
    def setUpTestData(cls):
        device = _devices(1)[0]
        iface = Interface.objects.create(device=device, name="eth0", type="virtual")
        ips = [
            IPAddress.objects.create(address=f"10.9.0.{i}/24", assigned_object=iface)
            for i in range(1, 8)
        ]
        override = MonitoringOverride.objects.create(
            assigned_object=device, management_ip=ips[0]
        )
        for extra_ip in ips[1:4]:
            MonitoredInterface.objects.create(override=override, ip_address=extra_ip)
        cls.create_data = [
            {"override": override.pk, "ip_address": ips[4].pk, "role": "N"},
            {"override": override.pk, "ip_address": ips[5].pk, "role": "S"},
            {"override": override.pk, "ip_address": ips[6].pk, "role": "N"},
        ]


class AssetMappingAPITest(_NoGraphQL, APIViewTestCases.APIViewTestCase):
    model = AssetMapping
    view_namespace = "plugins-api:netbox_opennms"
    brief_fields = ["asset_field", "display", "id", "url"]

    @classmethod
    def setUpTestData(cls):
        req = Requisition.objects.create(
            name="am-api", filter_params={"role": ["switch"]}
        )
        for source, field in [
            ("serial", "serialNumber"),
            ("name", "displayCategory"),
            ("description", "description"),
        ]:
            AssetMapping.objects.create(
                requisition=req, netbox_source=source, asset_field=field
            )
        cls.create_data = [
            {"requisition": req.pk, "netbox_source": "role", "asset_field": "category"},
            {"requisition": req.pk, "netbox_source": "site", "asset_field": "building"},
            {"requisition": req.pk, "netbox_source": "rack", "asset_field": "rack"},
        ]


class OpenNMSServerAPITest(_NoGraphQL, APIViewTestCases.APIViewTestCase):
    model = OpenNMSServer
    view_namespace = "plugins-api:netbox_opennms"
    brief_fields = ["display", "id", "name", "url"]

    @classmethod
    def setUpTestData(cls):
        for name in ("s1", "s2", "s3"):
            OpenNMSServer.objects.create(
                name=name, url=f"https://{name}.example", username="svc", password="x"
            )
        cls.create_data = [
            {
                "name": "s4",
                "server_url": "https://s4.example",
                "username": "svc",
                "password": "hunter2",
            },
            {
                "name": "s5",
                "server_url": "https://s5.example",
                "username": "svc",
                "password": "hunter2",
            },
            {
                "name": "s6",
                "server_url": "https://s6.example",
                "username": "svc",
                "password": "hunter2",
            },
        ]


class OpenNMSServerSerializerScopeCollisionTest(TestCase):
    """ADR 0002: mirrors OpenNMSServerForm's identical checks (test_forms.py) —
    the API is an equally valid assignment surface."""

    def test_default_server_cannot_carry_scope_bindings(self):
        site = Site.objects.create(name="Raleigh", slug="raleigh")
        serializer = OpenNMSServerSerializer(
            data={
                "name": "New",
                "server_url": "https://new.example",
                "username": "svc",
                "password": "x",
                "is_default": True,
                "sites": [site.pk],
            }
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("is_default", serializer.errors)

    def test_promoting_an_existing_scoped_server_to_default_is_rejected(self):
        site = Site.objects.create(name="Raleigh", slug="raleigh")
        server = OpenNMSServer.objects.create(
            name="Existing", url="https://existing.example"
        )
        server.sites.add(site)
        serializer = OpenNMSServerSerializer(
            instance=server, data={"is_default": True}, partial=True
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("is_default", serializer.errors)

    def test_site_already_bound_to_another_server_is_rejected(self):
        site = Site.objects.create(name="Raleigh", slug="raleigh")
        OpenNMSServer.objects.create(
            name="Existing", url="https://existing.example"
        ).sites.add(site)
        serializer = OpenNMSServerSerializer(
            data={
                "name": "New",
                "server_url": "https://new.example",
                "username": "svc",
                "password": "x",
                "sites": [site.pk],
            }
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("sites", serializer.errors)

    def test_unbound_site_is_accepted(self):
        site = Site.objects.create(name="Raleigh", slug="raleigh")
        serializer = OpenNMSServerSerializer(
            data={
                "name": "New",
                "server_url": "https://new.example",
                "username": "svc",
                "password": "x",
                "sites": [site.pk],
            }
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)


class MonitoringExclusionAPITest(_NoGraphQL, APIViewTestCases.APIViewTestCase):
    model = MonitoringExclusion
    view_namespace = "plugins-api:netbox_opennms"
    brief_fields = ["description", "display", "id", "url"]

    @classmethod
    def setUpTestData(cls):
        for description in ("excl-1", "excl-2", "excl-3"):
            MonitoringExclusion.objects.create(description=description)
        cls.create_data = [
            {"description": "excl-4"},
            {"description": "excl-5"},
            {"description": "excl-6"},
        ]


class VRFAssignmentAPITest(_NoGraphQL, APIViewTestCases.APIViewTestCase):
    model = VRFAssignment
    view_namespace = "plugins-api:netbox_opennms"
    brief_fields = ["description", "display", "id", "url"]

    @classmethod
    def setUpTestData(cls):
        vrf = VRF.objects.create(name="Customer VRF")
        for description in ("va-1", "va-2", "va-3"):
            VRFAssignment.objects.create(vrf=vrf, description=description)
        cls.create_data = [
            {"vrf": vrf.pk, "description": "va-4"},
            {"vrf": vrf.pk, "description": "va-5"},
            {"vrf": vrf.pk, "description": "va-6"},
        ]


class DiscoveryScanAPITest(_NoGraphQL, APIViewTestCases.APIViewTestCase):
    model = DiscoveryScan
    view_namespace = "plugins-api:netbox_opennms"
    brief_fields = ["display", "foreign_source", "id", "url"]

    @classmethod
    def setUpTestData(cls):
        server = OpenNMSServer.objects.create(
            name="ds-api", url="https://ds-api.example"
        )
        site = Site.objects.create(name="Raleigh", slug="raleigh")
        for i in range(3):
            DiscoveryScan.objects.create(
                server=server,
                site=site,
                ip_range_begin=f"10.1.{i}.1",
                ip_range_end=f"10.1.{i}.254",
            )
        cls.create_data = [
            {
                "server": server.pk,
                "site": site.pk,
                "ip_range_begin": "10.1.10.1",
                "ip_range_end": "10.1.10.254",
            },
            {
                "server": server.pk,
                "site": site.pk,
                "ip_range_begin": "10.1.11.1",
                "ip_range_end": "10.1.11.254",
            },
            {
                "server": server.pk,
                "site": site.pk,
                "ip_range_begin": "10.1.12.1",
                "ip_range_end": "10.1.12.254",
            },
        ]


class DiscoveryScanSerializerValidationTest(TestCase):
    """ADR 0006/0008: mirrors DiscoveryScanForm's identical check (test_forms.py)."""

    @classmethod
    def setUpTestData(cls):
        cls.server = OpenNMSServer.objects.create(
            name="Acme", url="https://onms.example"
        )
        cls.site = Site.objects.create(name="Raleigh", slug="raleigh")

    def _data(self, **overrides):
        data = {
            "server": self.server.pk,
            "ip_range_begin": "10.0.0.1",
            "ip_range_end": "10.0.0.254",
        }
        data.update(overrides)
        return data

    def test_site_or_location_required(self):
        serializer = DiscoveryScanSerializer(data=self._data())
        self.assertFalse(serializer.is_valid())

    def test_with_site_is_accepted(self):
        serializer = DiscoveryScanSerializer(data=self._data(site=self.site.pk))
        self.assertTrue(serializer.is_valid(), serializer.errors)


class DiscoveredNodeAPITest(_NoGraphQL, APIViewTestCases.APIViewTestCase):
    model = DiscoveredNode
    view_namespace = "plugins-api:netbox_opennms"
    brief_fields = ["display", "id", "label", "url", "verdict"]

    @classmethod
    def setUpTestData(cls):
        server = OpenNMSServer.objects.create(
            name="dn-api", url="https://dn-api.example"
        )
        for i in range(3):
            DiscoveredNode.objects.create(
                server=server, opennms_node_id=i, label=f"node-{i}", verdict="red"
            )
        cls.create_data = [
            {
                "server": server.pk,
                "opennms_node_id": 10,
                "label": "node-10",
                "verdict": "red",
            },
            {
                "server": server.pk,
                "opennms_node_id": 11,
                "label": "node-11",
                "verdict": "red",
            },
            {
                "server": server.pk,
                "opennms_node_id": 12,
                "label": "node-12",
                "verdict": "red",
            },
        ]


class MetadataEntryAPITest(_NoGraphQL, APIViewTestCases.APIViewTestCase):
    model = MetadataEntry
    view_namespace = "plugins-api:netbox_opennms"
    brief_fields = ["display", "id", "key", "url"]

    @classmethod
    def setUpTestData(cls):
        req = Requisition.objects.create(
            name="me-api", filter_params={"role": ["switch"]}
        )
        for key in ["k1", "k2", "k3"]:
            MetadataEntry.objects.create(
                requisition=req, scope="node", context="requisition",
                key=key, literal_value="v",
            )
        cls.create_data = [
            {"requisition": req.pk, "scope": "node", "context": "requisition",
             "key": "a", "literal_value": "1"},
            {"requisition": req.pk, "scope": "node", "context": "requisition",
             "key": "b", "literal_value": "2"},
            {"requisition": req.pk, "scope": "service", "context": "X-netbox",
             "key": "c", "value_source": "name"},
        ]
