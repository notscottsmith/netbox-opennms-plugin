# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Tests for the pure dry-run differ (Requisition redesign, R7)."""

from unittest import mock

from dcim.models import (
    Device,
    DeviceRole,
    DeviceType,
    Interface,
    Manufacturer,
    Site,
)
from django.test import SimpleTestCase, TestCase
from ipam.models import IPAddress

from netbox_opennms.dryrun import NodeDiff, diff, dry_run
from netbox_opennms.membership import (
    Conflict,
    InterfaceSpec,
    NodeSpec,
    Resolution,
    ServerConflict,
)
from netbox_opennms.models import DeployedForeignSource, OpenNMSServer, Requisition


class _Rules:
    def __init__(self, items=()):
        self._items = list(items)

    def all(self):
        return self._items


class _Req:
    def __init__(self, scan_interval="1d"):
        self.scan_interval = scan_interval
        self.detectors = _Rules()
        self.policies = _Rules()


def _resolution(nodes):
    return Resolution("fs", _Req(), nodes=nodes, warnings=[])


def _node(ip="10.0.0.1", services=("ICMP",)):
    return NodeSpec(
        "rtr-1", "device-1", "",
        [InterfaceSpec(ip, "P", services=list(services))],
    )


def _current(ip="10.0.0.1", services=("ICMP",)):
    return {
        "node": [
            {
                "foreign-id": "device-1",
                "node-label": "rtr-1",
                "interface": [
                    {
                        "ip-addr": ip,
                        "snmp-primary": "P",
                        "monitored-service": [{"service-name": s} for s in services],
                    }
                ],
            }
        ]
    }


class DryRunDiffTest(SimpleTestCase):
    def test_empty_diff_on_identical(self):
        result = diff(_resolution([_node()]), _current(), {"scan-interval": "1d"})
        self.assertFalse(result.has_changes)
        self.assertEqual(len(result.unchanged), 1)

    def test_unchanged_is_a_full_nodediff_not_a_bare_count(self):
        # Issue #18: an in-sync member is listed like added/removed/changed,
        # not folded into a count.
        result = diff(_resolution([_node()]), _current(), {"scan-interval": "1d"})
        self.assertEqual(
            result.unchanged, [NodeDiff("device-1", "rtr-1", "unchanged")]
        )

    def test_never_synced_is_all_added(self):
        result = diff(_resolution([_node()]), None, None)
        self.assertFalse(result.exists)
        self.assertEqual([n.foreign_id for n in result.added], ["device-1"])
        self.assertEqual(result.unchanged, [])

    def test_management_ip_change(self):
        result = diff(
            _resolution([_node(ip="10.0.0.1")]),
            _current(ip="10.0.0.9"),
            {"scan-interval": "1d"},
        )
        self.assertEqual(len(result.changed), 1)
        self.assertTrue(
            any("management IP" in c for c in result.changed[0].changes)
        )

    def test_service_change(self):
        result = diff(
            _resolution([_node(services=("ICMP", "SNMP"))]),
            _current(services=("ICMP",)),
            {"scan-interval": "1d"},
        )
        self.assertEqual(len(result.changed), 1)

    def test_removed_node(self):
        result = diff(_resolution([]), _current(), {"scan-interval": "1d"})
        self.assertEqual([n.foreign_id for n in result.removed], ["device-1"])

    def test_definition_scan_interval_change(self):
        result = diff(_resolution([_node()]), _current(), {"scan-interval": "30m"})
        self.assertTrue(
            any("scan-interval" in c for c in result.definition_changes)
        )

    def test_conflict_reports_freeze_instead_of_diff(self):
        # C1: a frozen Requisition's dry-run reports the conflicts, not a node
        # diff of a push that is blocked anyway.
        resolution = _resolution([_node()])
        resolution.conflicts = [Conflict("rtr-1", "device-1", ["a", "b"])]
        result = diff(resolution, _current(), {"scan-interval": "1d"})
        self.assertEqual(len(result.conflicts), 1)
        self.assertEqual(result.added, [])
        self.assertEqual(result.removed, [])
        self.assertEqual(result.changed, [])
        self.assertEqual(len(result.unchanged), 0)

    def test_server_conflict_reports_freeze_instead_of_diff(self):
        # ADR 0002: members disagreeing on (or none resolving to) an OpenNMS
        # Server is the same kind of freeze as a filter conflict — report it
        # instead of a node diff of a push that is blocked anyway.
        resolution = _resolution([_node()])
        resolution.server_conflict = ServerConflict(["Server A", "Server B"])
        result = diff(resolution, _current(), {"scan-interval": "1d"})
        self.assertIn("Server A", result.server_conflict)
        self.assertIn("Server B", result.server_conflict)
        self.assertEqual(result.added, [])
        self.assertEqual(result.removed, [])
        self.assertEqual(result.changed, [])
        self.assertEqual(len(result.unchanged), 0)

    def test_blank_location_matches_configured_default(self):
        # Node location blank + OpenNMS holds the configured default_location →
        # not a change (the renderer substitutes the default).
        current = _current()
        current["node"][0]["location"] = "Default"
        result = diff(
            _resolution([_node()]), current, {"scan-interval": "1d"},
            default_location="Default",
        )
        self.assertFalse(result.has_changes)

    def test_single_element_json_not_mislabeled(self):
        # OpenNMS v1 REST may serialize a lone node/interface/service as a bare
        # object rather than a list — an in-sync node must not read as "added".
        current = {
            "node": {
                "foreign-id": "device-1",
                "node-label": "rtr-1",
                "interface": {
                    "ip-addr": "10.0.0.1",
                    "snmp-primary": "P",
                    "monitored-service": {"service-name": "ICMP"},
                },
            }
        }
        result = diff(_resolution([_node()]), current, {"scan-interval": "1d"})
        self.assertEqual(result.added, [])
        self.assertEqual(len(result.unchanged), 1)


FS = "netbox.raleigh.router"


class DryRunFetchTest(TestCase):
    """``dry_run()``'s target-Server resolution (ADR 0002), mirroring
    ``jobs._render_and_replace``: a cleanly-resolved Server is used first, else
    the Server this Foreign Source was last deployed to, else there is nothing
    live to compare against.
    """

    @classmethod
    def setUpTestData(cls):
        cls.site = Site.objects.create(name="Raleigh", slug="raleigh")
        cls.role = DeviceRole.objects.create(name="Router", slug="router")
        mfr = Manufacturer.objects.create(name="Acme", slug="acme")
        cls.dt = DeviceType.objects.create(manufacturer=mfr, model="M1", slug="m1")
        cls.requisition = Requisition.objects.create(
            name=FS, filter_params={"site": ["raleigh"], "role": ["router"]}
        )
        device = Device.objects.create(
            name="rtr-1", device_type=cls.dt, role=cls.role, site=cls.site
        )
        iface = Interface.objects.create(device=device, name="eth0", type="virtual")
        address = IPAddress.objects.create(address="10.0.0.1/24", assigned_object=iface)
        device.primary_ip4 = address
        device.save()
        cls.server = OpenNMSServer.objects.create(
            name="Acme", url="https://onms.example/opennms", is_default=True
        )

    @mock.patch("netbox_opennms.dryrun.OpenNMSClient.from_server")
    def test_uses_the_resolved_server(self, mock_from_server):
        client = mock_from_server.return_value.__enter__.return_value
        client.get_requisition.return_value = None
        client.get_foreign_source.return_value = None
        dry_run(FS)
        mock_from_server.assert_called_once_with(self.server)

    @mock.patch("netbox_opennms.dryrun.OpenNMSClient.from_server")
    def test_falls_back_to_the_previously_deployed_server(self, mock_from_server):
        # Zero members (e.g. the requisition's device was removed) → resolution.
        # server is None; fall back to where this Foreign Source last landed.
        other = OpenNMSServer.objects.create(
            name="Other", url="https://other.example"
        )
        DeployedForeignSource.objects.create(name=FS, server=other)
        Requisition.objects.filter(pk=self.requisition.pk).update(
            filter_params={"role": ["nonexistent"]}
        )
        client = mock_from_server.return_value.__enter__.return_value
        client.get_requisition.return_value = None
        client.get_foreign_source.return_value = None
        dry_run(FS)
        mock_from_server.assert_called_once_with(other)

    @mock.patch("netbox_opennms.dryrun.OpenNMSClient.from_server")
    def test_no_resolvable_or_deployed_server_skips_the_fetch(self, mock_from_server):
        Requisition.objects.filter(pk=self.requisition.pk).update(
            filter_params={"role": ["nonexistent"]}
        )
        OpenNMSServer.objects.all().delete()
        result = dry_run(FS)
        mock_from_server.assert_not_called()
        self.assertFalse(result.exists)

    @mock.patch("netbox_opennms.dryrun.OpenNMSClient.from_server")
    def test_server_conflict_skips_the_fetch(self, mock_from_server):
        # Two members in different Sites, each bound to a different Server →
        # they disagree — the dry-run reports the freeze without ever calling
        # OpenNMS.
        other_site = Site.objects.create(name="Durham", slug="durham")
        other_server = OpenNMSServer.objects.create(
            name="Other", url="https://other.example"
        )
        other_server.sites.add(other_site)
        device2 = Device.objects.create(
            name="rtr-2", device_type=self.dt, role=self.role, site=other_site
        )
        iface2 = Interface.objects.create(device=device2, name="eth0", type="virtual")
        address2 = IPAddress.objects.create(
            address="10.0.0.2/24", assigned_object=iface2
        )
        device2.primary_ip4 = address2
        device2.save()
        Requisition.objects.filter(pk=self.requisition.pk).update(
            filter_params={"role": ["router"]}
        )
        result = dry_run(FS)
        mock_from_server.assert_not_called()
        self.assertTrue(result.server_conflict)

    @mock.patch("netbox_opennms.dryrun.OpenNMSClient.from_server")
    def test_adopted_node_shown_unchanged_not_added_and_removed(
        self, mock_from_server
    ):
        # Issue #5: dry-run must show the SAME Foreign ID a Sync would actually
        # push — an unambiguous label match reuses the existing Foreign ID, so
        # an otherwise-identical node reads as unchanged, not a spurious
        # added+removed pair (two different Foreign IDs for the same node).
        client = mock_from_server.return_value.__enter__.return_value
        client.get_requisition.return_value = {
            "node": [
                {
                    "foreign-id": "legacy-42",
                    "node-label": "rtr-1",
                    "interface": [
                        {
                            "ip-addr": "10.0.0.1",
                            "snmp-primary": "P",
                            "monitored-service": [],
                        }
                    ],
                }
            ]
        }
        client.get_foreign_source.return_value = None
        result = dry_run(FS)
        self.assertEqual(result.added, [])
        self.assertEqual(result.removed, [])
        self.assertEqual(len(result.unchanged), 1)

    @mock.patch("netbox_opennms.dryrun.OpenNMSClient.from_server")
    def test_non_adopted_node_shows_freshly_derived_id(self, mock_from_server):
        client = mock_from_server.return_value.__enter__.return_value
        client.get_requisition.return_value = None
        client.get_foreign_source.return_value = None
        result = dry_run(FS)
        self.assertEqual(len(result.added), 1)
        self.assertTrue(result.added[0].foreign_id.startswith("netbox-device-"))

    @mock.patch("netbox_opennms.dryrun.OpenNMSClient.from_server")
    def test_ambiguous_adoption_match_warns(self, mock_from_server):
        client = mock_from_server.return_value.__enter__.return_value
        client.get_requisition.return_value = {
            "node": [
                {"foreign-id": "legacy-42", "node-label": "rtr-1"},
                {"foreign-id": "legacy-43", "node-label": "rtr-1"},
            ]
        }
        client.get_foreign_source.return_value = None
        result = dry_run(FS)
        self.assertTrue(any("ambiguous" in w for w in result.warnings))
