# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Tests for per-IP reconciliation (issue #30, ADR 0008/0009)."""

from dataclasses import dataclass

from dcim.models import Device, DeviceRole, DeviceType, Interface, Manufacturer, Site
from django.contrib.contenttypes.models import ContentType
from django.test import SimpleTestCase, TestCase
from django.utils import timezone
from ipam.models import VRF, IPAddress, Prefix

from netbox_opennms.import_node import InterfaceProposal
from netbox_opennms.ip_reconcile import (
    IPRangeProposal,
    PrefixProposal,
    classful_network,
    netbox_ip_index,
    reconcile_interfaces,
    reconcile_node_interfaces,
)
from netbox_opennms.models import (
    DiscoveredNode,
    DiscoveryScan,
    OpenNMSServer,
    Requisition,
)


def _scoped_prefix(prefix, vrf, scope):
    return Prefix.objects.create(
        prefix=prefix,
        vrf=vrf,
        scope_type=ContentType.objects.get_for_model(scope),
        scope_id=scope.pk,
    )


class ClassfulNetworkTest(SimpleTestCase):
    def test_class_a_private_sizes_to_slash_8(self):
        self.assertEqual(str(classful_network("10.1.2.3")), "10.0.0.0/8")

    def test_class_b_private_sizes_to_slash_16(self):
        self.assertEqual(str(classful_network("172.16.5.5")), "172.16.0.0/16")

    def test_class_c_private_sizes_to_slash_24(self):
        self.assertEqual(str(classful_network("192.168.1.5")), "192.168.1.0/24")

    def test_public_ipv4_falls_back_to_slash_24(self):
        self.assertEqual(str(classful_network("203.0.113.5")), "203.0.113.0/24")

    def test_ipv6_falls_back_to_slash_64(self):
        self.assertEqual(
            str(classful_network("2001:db8::1")), "2001:db8::/64"
        )


@dataclass
class _FakeAssigned:
    device: object = None
    virtual_machine: object = None


@dataclass
class _FakeIPRow:
    vrf: object = None
    vrf_id: object = None
    assigned_object: object = None


class ReconcileInterfacesTest(SimpleTestCase):
    """Pure per-IP reconciliation (issue #30), same shape as ``scan.reconcile``'s
    own tests: hand-built input data, no DB/client access."""

    def test_red_when_address_not_in_netbox(self):
        results = reconcile_interfaces(
            [InterfaceProposal(ip_address="10.0.0.1", netmask="255.255.255.0")],
            matched_object=None,
            ip_index={},
        )
        self.assertEqual(len(results), 1)
        verdict = results[0]
        self.assertEqual(verdict.verdict, "red")
        self.assertEqual(verdict.diff_detail, [])
        self.assertIsInstance(verdict.proposal, PrefixProposal)
        self.assertEqual(verdict.proposal.prefix, "10.0.0.0/24")

    def test_red_proposes_ip_range_when_netmask_unknown(self):
        results = reconcile_interfaces(
            [InterfaceProposal(ip_address="10.0.0.1")],
            matched_object=None,
            ip_index={},
        )
        verdict = results[0]
        self.assertEqual(verdict.verdict, "red")
        self.assertIsInstance(verdict.proposal, IPRangeProposal)
        self.assertEqual(verdict.proposal.start_address, "10.0.0.0")
        self.assertEqual(verdict.proposal.end_address, "10.255.255.255")

    def test_green_on_matching_assignment_and_no_vrf_to_check(self):
        device = object()
        row = _FakeIPRow(assigned_object=_FakeAssigned(device=device))
        results = reconcile_interfaces(
            [InterfaceProposal(ip_address="10.0.0.1")],
            matched_object=device,
            ip_index={"10.0.0.1": row},
        )
        verdict = results[0]
        self.assertEqual(verdict.verdict, "green")
        self.assertEqual(verdict.diff_detail, [])
        self.assertIsNone(verdict.proposal)

    def test_green_when_vrf_matches(self):
        device = object()
        row = _FakeIPRow(
            assigned_object=_FakeAssigned(device=device), vrf_id=1, vrf="VRF A"
        )
        expected_vrf = type("VRF", (), {"pk": 1})()
        results = reconcile_interfaces(
            [InterfaceProposal(ip_address="10.0.0.1")],
            matched_object=device,
            ip_index={"10.0.0.1": row},
            vrf=expected_vrf,
        )
        self.assertEqual(results[0].verdict, "green")

    def test_orange_when_unassigned(self):
        row = _FakeIPRow(assigned_object=None)
        results = reconcile_interfaces(
            [InterfaceProposal(ip_address="10.0.0.1", netmask="255.255.255.0")],
            matched_object=object(),
            ip_index={"10.0.0.1": row},
        )
        verdict = results[0]
        self.assertEqual(verdict.verdict, "orange")
        self.assertIn("unassigned", verdict.diff_detail)
        self.assertIsInstance(verdict.proposal, PrefixProposal)

    def test_orange_when_assigned_to_a_different_object(self):
        matched = object()
        other = object()
        row = _FakeIPRow(assigned_object=_FakeAssigned(device=other))
        results = reconcile_interfaces(
            [InterfaceProposal(ip_address="10.0.0.1")],
            matched_object=matched,
            ip_index={"10.0.0.1": row},
        )
        verdict = results[0]
        self.assertEqual(verdict.verdict, "orange")
        self.assertEqual(len(verdict.diff_detail), 1)
        self.assertIn("assigned to", verdict.diff_detail[0])

    def test_orange_when_no_matched_object_but_address_is_assigned(self):
        row = _FakeIPRow(assigned_object=_FakeAssigned(device=object()))
        results = reconcile_interfaces(
            [InterfaceProposal(ip_address="10.0.0.1")],
            matched_object=None,
            ip_index={"10.0.0.1": row},
        )
        self.assertEqual(results[0].verdict, "orange")

    def test_orange_when_vrf_mismatches(self):
        device = object()
        row = _FakeIPRow(
            assigned_object=_FakeAssigned(device=device), vrf_id=1, vrf="VRF A"
        )
        expected_vrf = type("VRF", (), {"pk": 2})()
        results = reconcile_interfaces(
            [InterfaceProposal(ip_address="10.0.0.1")],
            matched_object=device,
            ip_index={"10.0.0.1": row},
            vrf=expected_vrf,
        )
        verdict = results[0]
        self.assertEqual(verdict.verdict, "orange")
        self.assertTrue(any("VRF" in d for d in verdict.diff_detail))

    def test_orange_reports_both_assignment_and_vrf_mismatches(self):
        matched = object()
        other = object()
        row = _FakeIPRow(assigned_object=_FakeAssigned(device=other), vrf_id=1)
        expected_vrf = type("VRF", (), {"pk": 2})()
        results = reconcile_interfaces(
            [InterfaceProposal(ip_address="10.0.0.1")],
            matched_object=matched,
            ip_index={"10.0.0.1": row},
            vrf=expected_vrf,
        )
        self.assertEqual(len(results[0].diff_detail), 2)

    def test_empty_input_returns_empty(self):
        self.assertEqual(reconcile_interfaces([], None, {}), [])

    def test_preserves_ip_address_and_netmask(self):
        results = reconcile_interfaces(
            [InterfaceProposal(ip_address="10.0.0.1", netmask="255.255.255.0")],
            matched_object=None,
            ip_index={},
        )
        self.assertEqual(results[0].ip_address, "10.0.0.1")
        self.assertEqual(results[0].netmask, "255.255.255.0")


class NetboxIpIndexTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        site = Site.objects.create(name="Raleigh", slug="raleigh")
        role = DeviceRole.objects.create(name="Router", slug="router")
        mfr = Manufacturer.objects.create(name="Acme", slug="acme")
        dt = DeviceType.objects.create(manufacturer=mfr, model="M1", slug="m1")
        device = Device.objects.create(
            name="rtr-1", device_type=dt, role=role, site=site
        )
        iface = Interface.objects.create(device=device, name="eth0", type="virtual")
        cls.address = IPAddress.objects.create(
            address="10.0.0.1/24", assigned_object=iface
        )

    def test_indexes_by_bare_host_address(self):
        index = netbox_ip_index(["10.0.0.1"])
        self.assertEqual(index["10.0.0.1"], self.address)

    def test_ignores_addresses_not_requested(self):
        IPAddress.objects.create(address="10.0.0.2/24")
        index = netbox_ip_index(["10.0.0.1"])
        self.assertNotIn("10.0.0.2", index)

    def test_empty_input_returns_empty_dict(self):
        self.assertEqual(netbox_ip_index([]), {})


class ReconcileNodeInterfacesTest(TestCase):
    """DB-backed wrapper (issue #30): stored walk data + Requisition scope ->
    verdicts, mirroring ``scan.WalkNodeTest``'s fixture conventions."""

    @classmethod
    def setUpTestData(cls):
        cls.site = Site.objects.create(name="Raleigh", slug="raleigh")
        role = DeviceRole.objects.create(name="Router", slug="router")
        mfr = Manufacturer.objects.create(name="Acme", slug="acme")
        dt = DeviceType.objects.create(manufacturer=mfr, model="M1", slug="m1")
        cls.device = Device.objects.create(
            name="rtr-1", device_type=dt, role=role, site=cls.site
        )
        iface = Interface.objects.create(
            device=cls.device, name="eth0", type="virtual"
        )
        cls.vrf = VRF.objects.create(name="Raleigh VRF")
        cls.address = IPAddress.objects.create(
            address="10.0.0.1/24", assigned_object=iface, vrf=cls.vrf
        )
        _scoped_prefix("10.0.0.0/24", cls.vrf, cls.site)
        cls.server = OpenNMSServer.objects.create(
            name="Acme", url="https://onms.example", username="svc", password="x"
        )
        cls.requisition = Requisition.objects.create(
            name="raleigh-discovery", filter_params={"site": ["raleigh"]}
        )

    def _scan(self):
        return DiscoveryScan.objects.create(
            server=self.server,
            requisition=self.requisition,
            location="Raleigh",
            ip_range_begin="10.0.0.1",
            ip_range_end="10.0.0.10",
        )

    def _walked_node(self, *, matched=None, scan=None):
        return DiscoveredNode.objects.create(
            server=self.server,
            discovery_scan=scan,
            opennms_node_id=1,
            label="rtr-1",
            foreign_id="device-1",
            location="Raleigh",
            verdict="red",
            ip_interfaces=[
                {
                    "ipAddress": "10.0.0.1",
                    "snmpPrimary": "P",
                    "netMask": "255.255.255.0",
                }
            ],
            walked_at=timezone.now(),
            matched_object=matched,
        )

    def test_unwalked_node_returns_empty(self):
        node = DiscoveredNode.objects.create(
            server=self.server,
            opennms_node_id=2,
            label="rtr-2",
            foreign_id="device-2",
            location="Raleigh",
            verdict="red",
        )
        self.assertEqual(reconcile_node_interfaces(node), [])

    def test_green_verdict_resolves_vrf_via_requisition_scope(self):
        scan = self._scan()
        node = self._walked_node(matched=self.device, scan=scan)

        results = reconcile_node_interfaces(node)

        self.assertEqual(len(results), 1)
        verdict = results[0]
        self.assertEqual(verdict.ip_address, "10.0.0.1")
        self.assertEqual(verdict.netmask, "255.255.255.0")
        vrf_mismatch = [d for d in verdict.diff_detail if "VRF" in d]
        self.assertEqual(vrf_mismatch, [])

    def test_no_discovery_scan_resolves_no_vrf_and_still_reconciles(self):
        node = self._walked_node(matched=self.device, scan=None)

        results = reconcile_node_interfaces(node)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].verdict, "green")

    def test_red_when_address_missing_proposes_prefix_with_scope_and_vrf(self):
        scan = self._scan()
        node = DiscoveredNode.objects.create(
            server=self.server,
            discovery_scan=scan,
            opennms_node_id=3,
            label="rtr-3",
            foreign_id="device-3",
            location="Raleigh",
            verdict="red",
            ip_interfaces=[
                {
                    "ipAddress": "10.0.0.9",
                    "snmpPrimary": "P",
                    "netMask": "255.255.255.0",
                }
            ],
            walked_at=timezone.now(),
        )

        results = reconcile_node_interfaces(node)

        verdict = results[0]
        self.assertEqual(verdict.verdict, "red")
        self.assertIsInstance(verdict.proposal, PrefixProposal)
        self.assertEqual(verdict.proposal.prefix, "10.0.0.0/24")
        self.assertEqual(verdict.proposal.vrf, self.vrf)
        self.assertEqual(verdict.proposal.scope, self.site)
