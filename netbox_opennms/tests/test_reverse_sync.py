# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Tests for One-Time Sync: pull OpenNMS data into a Device/VM (issue #23).

Covers ``plan_reverse_sync`` (interface create/update/unchanged classification,
neighbor-link -> cable resolution reused from #16), ``apply_reverse_sync_plan``
(commits interfaces + cables), and ``run_reverse_sync`` (the per-node
fetch/plan/apply wrapper, never all-or-nothing across a batch). Uses
``TestCase`` (not ``SimpleTestCase``) because ``plan_reverse_sync`` reads
NetBox's current Interface state directly, per its own docstring.
"""

from unittest import mock

from dcim.models import (
    Cable,
    Device,
    DeviceRole,
    DeviceType,
    Interface,
    Manufacturer,
    Site,
)
from django.test import TestCase
from virtualization.models import Cluster, ClusterType, VirtualMachine

from netbox_opennms.client import OpenNMSError
from netbox_opennms.models import DiscoveredNode, OpenNMSServer
from netbox_opennms.reverse_sync import (
    ReverseSyncNodeData,
    apply_reverse_sync_plan,
    fetch_node_data,
    plan_reverse_sync,
    run_reverse_sync,
)

LLDP_PAYLOAD = {
    "lldpLinkNodes": [
        {
            "lldpLocalPort": "Gi0/1",
            "lldpRemChassisId": "aa:bb:cc:dd:ee:ff",
            "ldpRemPort": "Gi0/2",
        }
    ]
}


def _device(name="rtr-1"):
    site = Site.objects.create(name=f"Site {name}", slug=f"site-{name}")
    mfr = Manufacturer.objects.create(name=f"Vendor {name}", slug=f"vendor-{name}")
    dt = DeviceType.objects.create(manufacturer=mfr, model=name, slug=f"model-{name}")
    role, _ = DeviceRole.objects.get_or_create(
        name="Router", defaults={"slug": "router"}
    )
    return Device.objects.create(name=name, device_type=dt, role=role, site=site)


def _vm(name="vm-1"):
    cluster_type, _ = ClusterType.objects.get_or_create(
        name="Type 1", defaults={"slug": "type-1"}
    )
    cluster = Cluster.objects.create(name=f"Cluster {name}", type=cluster_type)
    return VirtualMachine.objects.create(name=name, cluster=cluster)


def _interface(device, name):
    return Interface.objects.create(device=device, name=name, type="1000base-t")


class ReverseSyncTestBase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.server = OpenNMSServer.objects.create(
            name="Acme", url="https://onms.example", username="svc", password="x"
        )

    def _node_for(self, target, node_id=1):
        node = DiscoveredNode.objects.create(
            server=self.server,
            opennms_node_id=node_id,
            label=target.name,
            verdict="red",
        )
        node.link_to(target)
        return node


class PlanReverseSyncInterfacesTest(ReverseSyncTestBase):
    def test_new_snmp_interface_is_a_create(self):
        device = _device()
        node = self._node_for(device)
        node_data = ReverseSyncNodeData(
            discovered_node=node,
            snmp_interfaces=[{"ifName": "eth0", "ifAlias": "uplink"}],
        )

        plan = plan_reverse_sync(node_data, device)

        self.assertEqual(len(plan.interfaces), 1)
        change = plan.interfaces[0]
        self.assertEqual(change.action, "create")
        self.assertEqual(change.name, "eth0")
        self.assertEqual(change.description, "uplink")
        self.assertTrue(plan.has_changes)

    def test_matching_snmp_interface_with_same_fields_is_unchanged(self):
        device = _device()
        node = self._node_for(device)
        Interface.objects.create(
            device=device,
            name="eth0",
            type="virtual",
            description="uplink",
            enabled=True,
        )
        node_data = ReverseSyncNodeData(
            discovered_node=node,
            snmp_interfaces=[
                {"ifName": "eth0", "ifAlias": "uplink", "ifAdminStatus": "1"}
            ],
        )

        plan = plan_reverse_sync(node_data, device)

        self.assertEqual(plan.interfaces[0].action, "unchanged")
        self.assertFalse(plan.has_changes)

    def test_matching_snmp_interface_with_different_description_is_an_update(self):
        device = _device()
        node = self._node_for(device)
        Interface.objects.create(
            device=device, name="eth0", type="virtual", description="old"
        )
        node_data = ReverseSyncNodeData(
            discovered_node=node,
            snmp_interfaces=[{"ifName": "eth0", "ifAlias": "new"}],
        )

        plan = plan_reverse_sync(node_data, device)

        change = plan.interfaces[0]
        self.assertEqual(change.action, "update")
        self.assertIn("description", change.changes[0])
        self.assertTrue(plan.has_changes)

    def test_vm_interfaces_use_vminterface_model(self):
        vm = _vm()
        node = self._node_for(vm)
        node_data = ReverseSyncNodeData(
            discovered_node=node, snmp_interfaces=[{"ifName": "eth0"}]
        )

        plan = plan_reverse_sync(node_data, vm)

        self.assertEqual(plan.kind, "vm")
        self.assertEqual(plan.interfaces[0].action, "create")

    def test_non_dict_snmp_interface_entries_are_skipped(self):
        device = _device()
        node = self._node_for(device)
        node_data = ReverseSyncNodeData(
            discovered_node=node, snmp_interfaces=["not-a-dict"]
        )

        plan = plan_reverse_sync(node_data, device)

        self.assertEqual(plan.interfaces, [])


class PlanReverseSyncLinksTest(ReverseSyncTestBase):
    def _remote_matched_lldp_payload(self, remote_node_id):
        return {
            "lldpLinkNodes": [
                {
                    "lldpLocalPort": "Gi0/1",
                    "lldpRemChassisId": "aa:bb:cc:dd:ee:ff",
                    "ldpRemPort": "Gi0/2",
                    "lldpRemChassisIdUrl": (
                        f"element/linkednode.jsp?node={remote_node_id}"
                    ),
                }
            ]
        }

    def test_link_actionable_when_both_endpoints_matched_with_interfaces(self):
        device = _device("rtr-1")
        remote_device = _device("rtr-2")
        node = self._node_for(device, node_id=1)
        remote_node = self._node_for(remote_device, node_id=2)
        _interface(device, "Gi0/1")
        _interface(remote_device, "Gi0/2")
        node_data = ReverseSyncNodeData(
            discovered_node=node,
            node_links_payload=self._remote_matched_lldp_payload(
                remote_node.opennms_node_id
            ),
        )

        plan = plan_reverse_sync(node_data, device)

        self.assertEqual(len(plan.links), 1)
        self.assertTrue(plan.links[0].actionable)
        self.assertTrue(plan.has_changes)

    def test_link_not_actionable_when_remote_node_unmatched(self):
        device = _device("rtr-3")
        node = self._node_for(device, node_id=3)
        _interface(device, "Gi0/1")
        node_data = ReverseSyncNodeData(
            discovered_node=node,
            node_links_payload=self._remote_matched_lldp_payload(999),
        )

        plan = plan_reverse_sync(node_data, device)

        self.assertFalse(plan.links[0].actionable)
        self.assertTrue(plan.links[0].blocked_reason)
        self.assertFalse(plan.has_changes)

    def test_no_node_links_payload_yields_no_link_rows(self):
        device = _device("rtr-4")
        node = self._node_for(device, node_id=4)
        node_data = ReverseSyncNodeData(discovered_node=node)

        plan = plan_reverse_sync(node_data, device)

        self.assertEqual(plan.links, [])


class ApplyReverseSyncPlanTest(ReverseSyncTestBase):
    def test_creates_and_updates_interfaces_and_cables(self):
        device = _device("rtr-5")
        remote_device = _device("rtr-6")
        node = self._node_for(device, node_id=5)
        remote_node = self._node_for(remote_device, node_id=6)
        Interface.objects.create(
            device=device, name="Gi0/1", type="1000base-t", description="old"
        )
        _interface(remote_device, "Gi0/2")
        node_data = ReverseSyncNodeData(
            discovered_node=node,
            snmp_interfaces=[
                {"ifName": "Gi0/1", "ifAlias": "new"},
                {"ifName": "Gi0/3"},
            ],
            node_links_payload={
                "lldpLinkNodes": [
                    {
                        "lldpLocalPort": "Gi0/1",
                        "lldpRemChassisId": "aa:bb:cc:dd:ee:ff",
                        "ldpRemPort": "Gi0/2",
                        "lldpRemChassisIdUrl": (
                            f"element/linkednode.jsp?node={remote_node.opennms_node_id}"
                        ),
                    }
                ]
            },
        )
        plan = plan_reverse_sync(node_data, device)

        created, updated, cabled = apply_reverse_sync_plan(plan)

        self.assertEqual((created, updated, cabled), (1, 1, 1))
        self.assertEqual(Interface.objects.filter(device=device).count(), 2)
        self.assertEqual(Cable.objects.count(), 1)
        gi01 = Interface.objects.get(device=device, name="Gi0/1")
        self.assertEqual(gi01.description, "new")


class RunReverseSyncTest(ReverseSyncTestBase):
    @mock.patch("netbox_opennms.reverse_sync.OpenNMSClient.from_server")
    def test_successful_node_reports_counts(self, mock_from_server):
        device = _device("rtr-7")
        node = self._node_for(device, node_id=7)
        client = mock_from_server.return_value.__enter__.return_value
        client.list_snmp_interfaces.return_value = [{"ifName": "eth0"}]
        client.get_node_links.return_value = {}

        results = run_reverse_sync(self.server, [node])

        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].success)
        self.assertEqual(results[0].interfaces_created, 1)

    @mock.patch("netbox_opennms.reverse_sync.OpenNMSClient.from_server")
    def test_unmatched_node_reports_failure_without_aborting_batch(
        self, mock_from_server
    ):
        device = _device("rtr-8")
        matched_node = self._node_for(device, node_id=8)
        unmatched_node = DiscoveredNode.objects.create(
            server=self.server, opennms_node_id=9, label="rtr-9", verdict="red"
        )
        client = mock_from_server.return_value.__enter__.return_value
        client.list_snmp_interfaces.return_value = []
        client.get_node_links.return_value = {}

        results = run_reverse_sync(self.server, [unmatched_node, matched_node])

        self.assertFalse(results[0].success)
        self.assertIn("No matched", results[0].error)
        self.assertTrue(results[1].success)

    @mock.patch("netbox_opennms.reverse_sync.OpenNMSClient.from_server")
    def test_opennms_error_reports_failure_without_aborting_batch(
        self, mock_from_server
    ):
        device_a = _device("rtr-10")
        device_b = _device("rtr-11")
        node_a = self._node_for(device_a, node_id=10)
        node_b = self._node_for(device_b, node_id=11)
        client = mock_from_server.return_value.__enter__.return_value
        client.list_snmp_interfaces.side_effect = [
            OpenNMSError("unreachable"),
            [],
        ]
        client.get_node_links.return_value = {}

        results = run_reverse_sync(self.server, [node_a, node_b])

        self.assertFalse(results[0].success)
        self.assertEqual(results[0].error, "unreachable")
        self.assertTrue(results[1].success)


class FetchNodeDataTest(ReverseSyncTestBase):
    def test_fetches_snmp_interfaces_and_node_links(self):
        device = _device("rtr-12")
        node = self._node_for(device, node_id=12)
        client = mock.Mock()
        client.list_snmp_interfaces.return_value = [{"ifName": "eth0"}]
        client.get_node_links.return_value = LLDP_PAYLOAD

        node_data = fetch_node_data(client, node)

        self.assertEqual(node_data.snmp_interfaces, [{"ifName": "eth0"}])
        self.assertEqual(node_data.node_links_payload, LLDP_PAYLOAD)
        client.list_snmp_interfaces.assert_called_once_with(12)
        client.get_node_links.assert_called_once_with(12)
