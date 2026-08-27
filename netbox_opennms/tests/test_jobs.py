# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Tests for the OpenNMS sync background job (mocked port, no network)."""

from datetime import timedelta
from unittest import mock

from core.exceptions import JobFailed
from core.models import Job
from dcim.models import (
    Device,
    DeviceRole,
    DeviceType,
    Interface,
    Manufacturer,
    Site,
)
from django.test import TestCase
from django.utils import timezone
from ipam.models import IPAddress

from netbox_opennms.client import OpenNMSError, OpenNMSHTTPError
from netbox_opennms.jobs import (
    CheckServerHealthJob,
    PollDiscoveryScansJob,
    ReconcileOrphansJob,
    SyncForeignSourceJob,
    unknown_locations,
)
from netbox_opennms.membership import monitored_foreign_sources, resolve
from netbox_opennms.models import (
    DeployedForeignSource,
    DiscoveredNode,
    DiscoveryScan,
    MonitoringDetector,
    MonitoringOverride,
    OpenNMSServer,
    Requisition,
)
from netbox_opennms.translation import (
    render_foreign_source_definition,
    render_requisition,
)

FS = "netbox.raleigh.router"


class SyncForeignSourceJobTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.site = Site.objects.create(name="Raleigh", slug="raleigh")
        cls.role = DeviceRole.objects.create(name="Router", slug="router")
        mfr = Manufacturer.objects.create(name="Acme", slug="acme")
        cls.dt = DeviceType.objects.create(manufacturer=mfr, model="M1", slug="m1")
        cls.requisition = Requisition.objects.create(
            name=FS, filter_params={"site": ["raleigh"], "role": ["router"]}
        )
        MonitoringDetector.objects.create(
            requisition=cls.requisition,
            name="ICMP",
            rule_class="org.opennms.netmgt.provision.detector.icmp.IcmpDetector",
        )
        cls.device = cls._make_device("rtr-1", "10.0.0.1/24")
        cls.server = OpenNMSServer.objects.create(
            name="Acme", url="https://onms.example/opennms", is_default=True
        )

    @classmethod
    def _make_device(cls, name, ip, primary=True, role=None, site=None):
        device = Device.objects.create(
            name=name, device_type=cls.dt, role=role or cls.role, site=site or cls.site
        )
        iface = Interface.objects.create(device=device, name="eth0", type="virtual")
        address = IPAddress.objects.create(address=ip, assigned_object=iface)
        if primary:
            device.primary_ip4 = address
            device.save()
        return device

    def _runner(self):
        return SyncForeignSourceJob(job=mock.Mock())

    @mock.patch("netbox_opennms.jobs.advisory_lock")
    @mock.patch("netbox_opennms.jobs.OpenNMSClient.from_server")
    def test_posts_fs_then_requisition_then_import(self, mock_from_server, _lock):
        client = mock_from_server.return_value.__enter__.return_value
        client.import_requisition.return_value = mock.Mock(status_code=202)

        self._runner().run(foreign_source=FS)

        # get_requisition is the pre-render adoption check (issue #4); it must
        # happen before anything is pushed, but before that ordering assertion.
        call_names = [c[0] for c in client.mock_calls]
        self.assertEqual(call_names[0], "get_requisition")
        self.assertEqual(
            call_names[1:4],
            ["post_foreign_source", "post_requisition", "import_requisition"],
        )
        self.assertEqual(
            client.post_foreign_source.call_args.args[0],
            render_foreign_source_definition(FS, self.requisition),
        )
        resolution = resolve(FS)
        self.assertEqual(
            client.post_requisition.call_args.args[0],
            render_requisition(FS, resolution.nodes),
        )
        self.assertEqual(
            client.import_requisition.call_args.kwargs["rescan_existing"], "false"
        )

    @mock.patch("netbox_opennms.jobs.advisory_lock")
    @mock.patch("netbox_opennms.jobs.OpenNMSClient.from_server")
    def test_render_error_marks_failed(self, mock_from_server, _lock):
        MonitoringDetector.objects.create(
            requisition=self.requisition, name="bad", rule_class=""
        )
        with self.assertRaises(JobFailed):
            self._runner().run(foreign_source=FS)
        mock_from_server.assert_not_called()

    @mock.patch("netbox_opennms.jobs.advisory_lock")
    @mock.patch("netbox_opennms.jobs.OpenNMSClient.from_server")
    def test_opennms_error_marks_failed(self, mock_from_server, _lock):
        client = mock_from_server.return_value.__enter__.return_value
        client.post_requisition.side_effect = OpenNMSHTTPError("boom", status_code=500)
        with self.assertRaises(JobFailed):
            self._runner().run(foreign_source=FS)

    @mock.patch("netbox_opennms.jobs.advisory_lock")
    @mock.patch("netbox_opennms.jobs.OpenNMSClient.from_server")
    def test_202_is_accepted_not_provisioned(self, mock_from_server, _lock):
        client = mock_from_server.return_value.__enter__.return_value
        client.import_requisition.return_value = mock.Mock(status_code=202)
        with self.assertLogs(
            "netbox.jobs.SyncForeignSourceJob", level="INFO"
        ) as captured:
            self._runner().run(foreign_source=FS)
        output = "\n".join(captured.output)
        self.assertIn("succeeded-accepted", output)
        self.assertIn("not verified", output)
        self.assertNotIn("provisioned", output)

    @mock.patch("netbox_opennms.jobs.advisory_lock")
    @mock.patch("netbox_opennms.jobs.OpenNMSClient.from_server")
    def test_serializes_per_foreign_source(self, mock_from_server, mock_lock):
        client = mock_from_server.return_value.__enter__.return_value
        client.import_requisition.return_value = mock.Mock(status_code=202)
        self._runner().run(foreign_source=FS)
        mock_lock.assert_called_once_with(f"netbox_opennms:fs:{FS}")

    @mock.patch("netbox_opennms.jobs.advisory_lock")
    @mock.patch("netbox_opennms.jobs.OpenNMSClient.from_server")
    def test_conflict_freezes_sync(self, mock_from_server, _lock):
        # C1: an overlap blocks Sync of the involved Requisition — JobFailed with
        # the conflict error, and NOTHING is pushed to OpenNMS.
        Requisition.objects.create(
            name="overlap", filter_params={"site": ["raleigh"]}
        )
        with self.assertRaises(JobFailed):
            self._runner().run(foreign_source=FS)
        mock_from_server.assert_not_called()

    @mock.patch("netbox_opennms.jobs.OpenNMSClient.from_server")
    def test_reconcile_ignores_frozen_requisition(self, mock_from_server):
        # C5: a frozen (conflicted) Requisition counts as monitored — the drift
        # reconciler must never tear down the state the freeze protects.
        DeployedForeignSource.objects.create(name=FS, server=self.server)
        Requisition.objects.create(
            name="overlap", filter_params={"site": ["raleigh"]}
        )
        client = mock_from_server.return_value.__enter__.return_value
        client.list_requisition_names.return_value = {FS}
        with mock.patch.object(SyncForeignSourceJob, "enqueue_sync") as enqueue:
            ReconcileOrphansJob(job=mock.Mock()).run()
        enqueue.assert_not_called()

    @mock.patch("netbox_opennms.jobs.advisory_lock")
    @mock.patch("netbox_opennms.jobs.OpenNMSClient.from_server")
    def test_sync_records_ownership(self, mock_from_server, _lock):
        # A successful sync records the FS name so the reconciler owns it (review #4).
        client = mock_from_server.return_value.__enter__.return_value
        client.import_requisition.return_value = mock.Mock(status_code=202)
        self._runner().run(foreign_source=FS)
        self.assertTrue(DeployedForeignSource.objects.filter(name=FS).exists())

    def test_enqueue_sync_coalesces_pending(self):
        job1 = SyncForeignSourceJob.enqueue_sync(FS)
        job2 = SyncForeignSourceJob.enqueue_sync(FS)
        self.assertEqual(job1.pk, job2.pk)
        self.assertEqual(Job.objects.filter(name=f"OpenNMS sync: {FS}").count(), 1)

    @mock.patch("netbox_opennms.jobs.advisory_lock")
    @mock.patch("netbox_opennms.jobs.OpenNMSClient.from_server")
    def test_ungoverned_foreign_source_skips_import(self, mock_from_server, _lock):
        # No Requisition is named this → a Sync must not push anything.
        self._runner().run(foreign_source="netbox.durham.router")
        mock_from_server.assert_not_called()

    @mock.patch("netbox_opennms.jobs.advisory_lock")
    @mock.patch("netbox_opennms.jobs.OpenNMSClient.from_server")
    def test_no_monitorable_members_skips_import(self, mock_from_server, _lock):
        # Genuinely empty (the sole member is EXCLUDED → monitored nowhere): a Sync
        # must not push an empty (mass-delete) requisition. A no-management-IP member
        # is NOT empty — it provisions as an interface-less node (RD-6/h).
        MonitoringOverride.objects.create(assigned_object=self.device, exclude=True)
        self._runner().run(foreign_source=FS)
        mock_from_server.assert_not_called()

    @mock.patch("netbox_opennms.jobs.advisory_lock")
    @mock.patch("netbox_opennms.jobs.OpenNMSClient.from_server")
    def test_remove_pushes_empty_requisition(self, mock_from_server, _lock):
        client = mock_from_server.return_value.__enter__.return_value
        client.import_requisition.return_value = mock.Mock(status_code=202)
        DeployedForeignSource.objects.create(name=FS, server=self.server)
        Device.objects.filter(pk=self.device.pk).delete()
        self._runner().run(foreign_source=FS, allow_empty=True)
        requisition_xml = client.post_requisition.call_args.args[0]
        self.assertNotIn(b"<node", requisition_xml)
        client.import_requisition.assert_called_once()

    @mock.patch("netbox_opennms.jobs.advisory_lock")
    @mock.patch("netbox_opennms.jobs.OpenNMSClient.from_server")
    def test_remove_ungoverned_skips_definition(self, mock_from_server, _lock):
        # A bare Remove of a name with no Requisition has no definition to push —
        # only the empty requisition + import that clears the nodes.
        client = mock_from_server.return_value.__enter__.return_value
        client.import_requisition.return_value = mock.Mock(status_code=202)
        DeployedForeignSource.objects.create(
            name="netbox.durham.router", server=self.server
        )
        self._runner().run(foreign_source="netbox.durham.router", allow_empty=True)
        client.post_foreign_source.assert_not_called()
        client.post_requisition.assert_called_once()
        client.import_requisition.assert_called_once()

    @mock.patch("netbox_opennms.jobs.advisory_lock")
    @mock.patch("netbox_opennms.jobs.OpenNMSClient.from_server")
    def test_validation_error_marks_failed(self, mock_from_server, _lock):
        # An invalid location on the Requisition (ORM-set, bypassing clean) fails
        # the job before any push (it would 400 on import).
        Requisition.objects.filter(pk=self.requisition.pk).update(
            location="bad location"
        )
        with self.assertRaises(JobFailed):
            self._runner().run(foreign_source=FS)
        mock_from_server.assert_not_called()

    @mock.patch("netbox_opennms.jobs.advisory_lock")
    @mock.patch("netbox_opennms.jobs.OpenNMSClient.from_server")
    @mock.patch("netbox_opennms.jobs.get_plugin_config")
    def test_invalid_import_mode_marks_failed(self, mock_cfg, mock_from_server, _lock):
        mock_cfg.side_effect = lambda _plugin, key: (
            "bogus" if key == "import_mode" else ""
        )
        with self.assertRaises(JobFailed):
            self._runner().run(foreign_source=FS)
        mock_from_server.assert_not_called()

    def test_unknown_locations_helper(self):
        fake = mock.Mock()
        fake.list_locations.return_value = {"Default", "edge-2"}
        self.assertEqual(unknown_locations(fake, {"edge-1", "edge-2", ""}), ["edge-1"])

    def test_unknown_locations_skips_when_none(self):
        fake = mock.Mock()
        self.assertEqual(unknown_locations(fake, {""}), [])
        fake.list_locations.assert_not_called()

    @mock.patch("netbox_opennms.jobs.advisory_lock")
    @mock.patch("netbox_opennms.jobs.OpenNMSClient.from_server")
    def test_unknown_location_logs_warning(self, mock_from_server, _lock):
        client = mock_from_server.return_value.__enter__.return_value
        client.import_requisition.return_value = mock.Mock(status_code=202)
        client.list_locations.return_value = {"Default"}
        Requisition.objects.filter(pk=self.requisition.pk).update(location="edge-1")
        with self.assertLogs(
            "netbox.jobs.SyncForeignSourceJob", level="WARNING"
        ) as captured:
            self._runner().run(foreign_source=FS)
        self.assertIn("edge-1", "\n".join(captured.output))

    @mock.patch("netbox_opennms.jobs.advisory_lock")
    @mock.patch("netbox_opennms.jobs.OpenNMSClient.from_server")
    def test_location_check_failure_does_not_fail_sync(self, mock_from_server, _lock):
        client = mock_from_server.return_value.__enter__.return_value
        client.import_requisition.return_value = mock.Mock(status_code=202)
        client.list_locations.side_effect = OpenNMSHTTPError("boom", status_code=500)
        Requisition.objects.filter(pk=self.requisition.pk).update(location="edge-1")
        self._runner().run(foreign_source=FS)
        client.import_requisition.assert_called_once()

    @mock.patch("netbox_opennms.jobs.advisory_lock")
    @mock.patch("netbox_opennms.jobs.OpenNMSClient.from_server")
    def test_ungoverned_remove_purges_shell(self, mock_from_server, _lock):
        client = mock_from_server.return_value.__enter__.return_value
        client.import_requisition.return_value = mock.Mock(status_code=202)
        DeployedForeignSource.objects.create(
            name="netbox.durham.router", server=self.server
        )
        self._runner().run(foreign_source="netbox.durham.router", allow_empty=True)
        client.delete_requisition.assert_called_once_with("netbox.durham.router")
        client.delete_foreign_source.assert_called_once_with("netbox.durham.router")

    @mock.patch("netbox_opennms.jobs.advisory_lock")
    @mock.patch("netbox_opennms.jobs.OpenNMSClient.from_server")
    def test_remove_of_empty_requisition_tears_down_shell(
        self, mock_from_server, _lock
    ):
        # A Remove that resolves to ZERO nodes tears down the shell + the ownership
        # record, so the reconciler can't re-Remove it every interval (review #3).
        client = mock_from_server.return_value.__enter__.return_value
        client.import_requisition.return_value = mock.Mock(status_code=202)
        DeployedForeignSource.objects.create(name=FS, server=self.server)
        Device.objects.filter(pk=self.device.pk).delete()
        self._runner().run(foreign_source=FS, allow_empty=True)
        client.delete_requisition.assert_called_once_with(FS)
        client.delete_foreign_source.assert_called_once_with(FS)
        self.assertFalse(DeployedForeignSource.objects.filter(name=FS).exists())

    @mock.patch("netbox_opennms.jobs.OpenNMSClient.from_server")
    def test_reconcile_enqueues_remove_for_orphans(self, mock_from_server):
        # Ownership is tracked by DeployedForeignSource, not a name prefix — so a
        # USER-named orphan (no netbox. prefix) is detected (review #4).
        DeployedForeignSource.objects.create(name=FS, server=self.server)
        DeployedForeignSource.objects.create(
            name="core-switches", server=self.server
        )
        client = mock_from_server.return_value.__enter__.return_value
        client.list_requisition_names.return_value = {
            FS,  # managed + monitored (requisition + member) → kept
            "core-switches",  # managed, no requisition → orphan
            "external.thing",  # not managed → ignored
        }
        with mock.patch.object(SyncForeignSourceJob, "enqueue_sync") as enqueue:
            ReconcileOrphansJob(job=mock.Mock()).run()
        enqueue.assert_called_once_with("core-switches", allow_empty=True)

    @mock.patch("netbox_opennms.jobs.get_plugin_config")
    def test_reconcile_disabled_skips(self, mock_cfg):
        mock_cfg.return_value = "false"
        with mock.patch.object(SyncForeignSourceJob, "enqueue_sync") as enqueue:
            ReconcileOrphansJob(job=mock.Mock()).run()
        enqueue.assert_not_called()

    @mock.patch("netbox_opennms.jobs.OpenNMSClient.from_server")
    def test_reconcile_swallows_opennms_error(self, mock_from_server):
        mock_from_server.side_effect = OpenNMSError("down")
        ReconcileOrphansJob(job=mock.Mock()).run()  # must not raise

    def test_reconcile_registered_as_recurring_system_job(self):
        from netbox.registry import registry

        from netbox_opennms.jobs import RECONCILE_INTERVAL_MINUTES

        self.assertIn(ReconcileOrphansJob, registry["system_jobs"])
        self.assertEqual(
            registry["system_jobs"][ReconcileOrphansJob]["interval"],
            RECONCILE_INTERVAL_MINUTES,
        )

    def test_monitored_foreign_sources_lists_syncable_requisitions(self):
        durham = Site.objects.create(name="Durham", slug="durham")
        Requisition.objects.create(
            name="netbox.durham.router",
            filter_params={"site": ["durham"], "role": ["router"]},
        )
        self._make_device("rtr-d", "10.0.1.1/24", site=durham)
        self.assertEqual(
            monitored_foreign_sources(),
            ["netbox.durham.router", "netbox.raleigh.router"],
        )

    @mock.patch("netbox_opennms.jobs.advisory_lock")
    @mock.patch("netbox_opennms.jobs.OpenNMSClient.from_server")
    def test_remove_with_members_pushes_full_requisition(
        self, mock_from_server, _lock
    ):
        # A Remove of a still-populated FS pushes the FULL requisition (the
        # membership is intact) and must NOT tear down the shell (review #6).
        client = mock_from_server.return_value.__enter__.return_value
        client.import_requisition.return_value = mock.Mock(status_code=202)
        self._runner().run(foreign_source=FS, allow_empty=True)
        requisition_xml = client.post_requisition.call_args.args[0]
        self.assertIn(b"<node", requisition_xml)
        client.delete_requisition.assert_not_called()
        client.delete_foreign_source.assert_not_called()

    @mock.patch("netbox_opennms.jobs.advisory_lock")
    @mock.patch("netbox_opennms.jobs.OpenNMSClient.from_server")
    def test_remove_allowed_for_rejected_filter(self, mock_from_server, _lock):
        # Review #3 (round 2): a rejected filter must NOT block a deliberate
        # Remove — that is the teardown escape hatch for a broken-filter FS.
        client = mock_from_server.return_value.__enter__.return_value
        client.import_requisition.return_value = mock.Mock(status_code=202)
        DeployedForeignSource.objects.create(name=FS, server=self.server)
        Requisition.objects.filter(pk=self.requisition.pk).update(
            filter_params={"bogus_key": ["x"]}
        )
        self._runner().run(foreign_source=FS, allow_empty=True)
        requisition_xml = client.post_requisition.call_args.args[0]
        self.assertNotIn(b"<node", requisition_xml)  # rejected → no members
        client.import_requisition.assert_called_once()
        client.delete_requisition.assert_called_once_with(FS)

    @mock.patch("netbox_opennms.jobs.advisory_lock")
    @mock.patch("netbox_opennms.jobs.OpenNMSClient.from_server")
    def test_rejected_filter_blocks_sync(self, mock_from_server, _lock):
        # Review #8: an unknown-key filter fails the job loudly (JobFailed),
        # never a quiet skip with a green COMPLETED job.
        Requisition.objects.filter(pk=self.requisition.pk).update(
            filter_params={"bogus_key": ["x"]}
        )
        with self.assertRaises(JobFailed):
            self._runner().run(foreign_source=FS)
        mock_from_server.assert_not_called()

    @mock.patch("netbox_opennms.jobs.advisory_lock")
    @mock.patch("netbox_opennms.jobs.OpenNMSClient.from_server")
    def test_unhealthy_server_hard_blocks_sync(self, mock_from_server, _lock):
        # A Server whose last health check FAILED refuses Sync/Remove/Move —
        # this covers the single entry point shared by all three.
        OpenNMSServer.objects.filter(pk=self.server.pk).update(
            last_check_status="failed", last_check_message="connection refused"
        )
        with self.assertRaises(JobFailed):
            self._runner().run(foreign_source=FS)
        mock_from_server.assert_not_called()

    @mock.patch("netbox_opennms.jobs.advisory_lock")
    @mock.patch("netbox_opennms.jobs.OpenNMSClient.from_server")
    def test_unchecked_server_is_not_blocked(self, mock_from_server, _lock):
        # "unknown" (never checked) must behave as today — only a confirmed
        # "failed" blocks a sync.
        client = mock_from_server.return_value.__enter__.return_value
        client.import_requisition.return_value = mock.Mock(status_code=202)
        self.assertEqual(self.server.last_check_status, "unknown")
        self._runner().run(foreign_source=FS)
        client.import_requisition.assert_called_once()

    @mock.patch("netbox_opennms.jobs.advisory_lock")
    @mock.patch("netbox_opennms.jobs.OpenNMSClient.from_server")
    def test_healthy_server_is_not_blocked(self, mock_from_server, _lock):
        OpenNMSServer.objects.filter(pk=self.server.pk).update(last_check_status="ok")
        client = mock_from_server.return_value.__enter__.return_value
        client.import_requisition.return_value = mock.Mock(status_code=202)
        self._runner().run(foreign_source=FS)
        client.import_requisition.assert_called_once()


    @mock.patch("netbox_opennms.jobs.advisory_lock")
    @mock.patch("netbox_opennms.jobs.OpenNMSClient.from_server")
    def test_adoption_reuses_existing_foreign_id(self, mock_from_server, _lock):
        # Issue #4: an unambiguous node-label match against OpenNMS's live state
        # reuses the existing Foreign ID instead of the freshly-derived one, so
        # taking over a hand-managed Foreign Source keeps its history.
        client = mock_from_server.return_value.__enter__.return_value
        client.get_requisition.return_value = {
            "node": [{"node-label": "rtr-1", "foreign-id": "legacy-42"}]
        }
        client.import_requisition.return_value = mock.Mock(status_code=202)
        self._runner().run(foreign_source=FS)
        client.get_requisition.assert_called_once_with(FS)
        requisition_xml = client.post_requisition.call_args.args[0]
        self.assertIn(b'foreign-id="legacy-42"', requisition_xml)
        self.assertNotIn(
            f"netbox-device-{self.device.pk}".encode(), requisition_xml
        )

    @mock.patch("netbox_opennms.jobs.advisory_lock")
    @mock.patch("netbox_opennms.jobs.OpenNMSClient.from_server")
    def test_adoption_no_match_uses_derived_id(self, mock_from_server, _lock):
        client = mock_from_server.return_value.__enter__.return_value
        client.get_requisition.return_value = {
            "node": [{"node-label": "someone-else", "foreign-id": "other-1"}]
        }
        client.import_requisition.return_value = mock.Mock(status_code=202)
        self._runner().run(foreign_source=FS)
        requisition_xml = client.post_requisition.call_args.args[0]
        self.assertIn(f"netbox-device-{self.device.pk}".encode(), requisition_xml)

    @mock.patch("netbox_opennms.jobs.advisory_lock")
    @mock.patch("netbox_opennms.jobs.OpenNMSClient.from_server")
    def test_adoption_ambiguous_match_falls_back_and_warns(
        self, mock_from_server, _lock
    ):
        client = mock_from_server.return_value.__enter__.return_value
        client.get_requisition.return_value = {
            "node": [
                {"node-label": "rtr-1", "foreign-id": "legacy-42"},
                {"node-label": "rtr-1", "foreign-id": "legacy-43"},
            ]
        }
        client.import_requisition.return_value = mock.Mock(status_code=202)
        with self.assertLogs(
            "netbox.jobs.SyncForeignSourceJob", level="WARNING"
        ) as captured:
            self._runner().run(foreign_source=FS)
        self.assertIn("ambiguous", "\n".join(captured.output))
        requisition_xml = client.post_requisition.call_args.args[0]
        self.assertIn(f"netbox-device-{self.device.pk}".encode(), requisition_xml)

    @mock.patch("netbox_opennms.jobs.advisory_lock")
    @mock.patch("netbox_opennms.jobs.OpenNMSClient.from_server")
    def test_adoption_unreachable_opennms_fails_job(self, mock_from_server, _lock):
        client = mock_from_server.return_value.__enter__.return_value
        client.get_requisition.side_effect = OpenNMSHTTPError("down", status_code=503)
        with self.assertRaises(JobFailed):
            self._runner().run(foreign_source=FS)
        client.post_requisition.assert_not_called()

    @mock.patch("netbox_opennms.jobs.advisory_lock")
    @mock.patch("netbox_opennms.jobs.OpenNMSClient.from_server")
    def test_adoption_skipped_when_never_synced(self, mock_from_server, _lock):
        # A brand-new Foreign Source (never synced) behaves as today: the GET
        # still runs (it's the only way to know that), returns None, and the
        # derived id is used untouched.
        client = mock_from_server.return_value.__enter__.return_value
        client.get_requisition.return_value = None
        client.import_requisition.return_value = mock.Mock(status_code=202)
        self._runner().run(foreign_source=FS)
        requisition_xml = client.post_requisition.call_args.args[0]
        self.assertIn(f"netbox-device-{self.device.pk}".encode(), requisition_xml)


class CheckServerHealthJobTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.good = OpenNMSServer.objects.create(name="Good", url="https://good.example")
        cls.bad = OpenNMSServer.objects.create(name="Bad", url="https://bad.example")

    @mock.patch("netbox_opennms.jobs.OpenNMSClient.from_server")
    def test_checks_every_server_independently(self, mock_from_server):
        def from_server(server):
            client = mock.Mock()
            client.__enter__ = mock.Mock(return_value=client)
            client.__exit__ = mock.Mock(return_value=False)
            if server.pk == self.bad.pk:
                client.test_connection.side_effect = OpenNMSError("unreachable")
            else:
                client.list_locations.return_value = {"edge-2", "edge-1"}
            return client

        mock_from_server.side_effect = from_server
        CheckServerHealthJob(job=mock.Mock()).run()

        self.good.refresh_from_db()
        self.bad.refresh_from_db()
        self.assertEqual(self.good.last_check_status, "ok")
        self.assertEqual(self.good.available_locations, ["edge-1", "edge-2"])
        self.assertEqual(self.bad.last_check_status, "failed")
        self.assertIn("unreachable", self.bad.last_check_message)

    @mock.patch("netbox_opennms.jobs.OpenNMSClient.from_server")
    def test_location_fetch_failure_does_not_fail_the_check(self, mock_from_server):
        client = mock_from_server.return_value.__enter__.return_value
        client.list_locations.side_effect = OpenNMSError("boom")

        CheckServerHealthJob(job=mock.Mock()).run()

        self.good.refresh_from_db()
        self.assertEqual(self.good.last_check_status, "ok")
        self.assertEqual(self.good.available_locations, [])

    def test_registered_as_recurring_system_job(self):
        from netbox.registry import registry

        from netbox_opennms.jobs import HEALTH_CHECK_INTERVAL_MINUTES

        self.assertIn(CheckServerHealthJob, registry["system_jobs"])
        self.assertEqual(
            registry["system_jobs"][CheckServerHealthJob]["interval"],
            HEALTH_CHECK_INTERVAL_MINUTES,
        )


def _onms_node(node_id, foreign_id="a", create_time=None):
    node = {
        "id": node_id,
        "label": "rtr-1",
        "foreignSource": "netbox-discovery-x",
        "foreignId": foreign_id,
        "location": "Perth",
    }
    if create_time is not None:
        node["createTime"] = create_time
    return node


class PollDiscoveryScansJobTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.server = OpenNMSServer.objects.create(
            name="Acme", url="https://onms.example", username="svc", password="x"
        )
        cls.site = Site.objects.create(name="Perth", slug="perth")

    def _scan(self, *, triggered=True, settled=False, idle_minutes_ago=0):
        scan = DiscoveryScan.objects.create(
            server=self.server,
            site=self.site,
            ip_range_begin="10.0.0.1",
            ip_range_end="10.0.0.10",
        )
        if triggered:
            DiscoveryScan.objects.filter(pk=scan.pk).update(
                last_triggered=timezone.now() - timedelta(minutes=idle_minutes_ago)
            )
        if settled:
            DiscoveryScan.objects.filter(pk=scan.pk).update(settled_at=timezone.now())
        scan.refresh_from_db()
        return scan

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_skips_pending_and_already_settled_scans(self, mock_from_server):
        pending = self._scan(triggered=False)
        settled = self._scan(settled=True)
        client = mock_from_server.return_value.__enter__.return_value
        client.list_nodes.return_value = []

        PollDiscoveryScansJob(job=mock.Mock()).run()

        client.list_nodes.assert_not_called()
        pending.refresh_from_db()
        settled.refresh_from_db()
        self.assertIsNone(pending.settled_at)

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_upserts_discovered_nodes_scoped_to_scan(self, mock_from_server):
        scan = self._scan()
        client = mock_from_server.return_value.__enter__.return_value
        client.list_nodes.return_value = [_onms_node(1), _onms_node(2, foreign_id="b")]

        PollDiscoveryScansJob(job=mock.Mock()).run()

        rows = DiscoveredNode.objects.filter(server=self.server, discovery_scan=scan)
        self.assertEqual(rows.count(), 2)
        client.list_nodes.assert_called_once_with(
            foreign_source=scan.foreign_source
        )

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_repolling_unchanged_state_refreshes_not_duplicates(
        self, mock_from_server
    ):
        scan = self._scan()
        client = mock_from_server.return_value.__enter__.return_value
        client.list_nodes.return_value = [_onms_node(1), _onms_node(2, foreign_id="b")]

        PollDiscoveryScansJob(job=mock.Mock()).run()
        PollDiscoveryScansJob(job=mock.Mock()).run()

        rows = DiscoveredNode.objects.filter(server=self.server, discovery_scan=scan)
        self.assertEqual(rows.count(), 2)

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_stale_cleanup_scoped_to_own_scan_only(self, mock_from_server):
        # Two Discovery Scans on the same Server, each finding a distinct node
        # under its own throwaway Foreign Source.
        scan_a = self._scan()
        scan_b = self._scan()
        client = mock_from_server.return_value.__enter__.return_value

        def list_nodes(foreign_source=None):
            if foreign_source == scan_a.foreign_source:
                return [_onms_node(1, foreign_id="a")]
            return [_onms_node(2, foreign_id="b")]

        client.list_nodes.side_effect = list_nodes
        PollDiscoveryScansJob(job=mock.Mock()).run()
        rows_a = DiscoveredNode.objects.filter(discovery_scan=scan_a)
        rows_b = DiscoveredNode.objects.filter(discovery_scan=scan_b)
        self.assertEqual(rows_a.count(), 1)
        self.assertEqual(rows_b.count(), 1)

        # scan_a's node vanishes; scan_b's is still there. scan_a's stale-row
        # cleanup must be scoped to ITS OWN rows only — it must not delete
        # scan_b's row for node 2, which it never even queried for.
        def list_nodes_after(foreign_source=None):
            if foreign_source == scan_a.foreign_source:
                return []
            return [_onms_node(2, foreign_id="b")]

        client.list_nodes.side_effect = list_nodes_after
        PollDiscoveryScansJob(job=mock.Mock()).run()

        self.assertEqual(
            DiscoveredNode.objects.filter(discovery_scan=scan_a).count(), 0
        )
        self.assertEqual(
            DiscoveredNode.objects.filter(discovery_scan=scan_b).count(), 1
        )

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_settles_after_idle_window_with_no_new_nodes(self, mock_from_server):
        scan = self._scan(idle_minutes_ago=10)
        client = mock_from_server.return_value.__enter__.return_value
        client.list_nodes.return_value = []

        PollDiscoveryScansJob(job=mock.Mock()).run()

        scan.refresh_from_db()
        self.assertIsNotNone(scan.settled_at)

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_does_not_settle_within_idle_window(self, mock_from_server):
        scan = self._scan(idle_minutes_ago=1)
        client = mock_from_server.return_value.__enter__.return_value
        client.list_nodes.return_value = []

        PollDiscoveryScansJob(job=mock.Mock()).run()

        scan.refresh_from_db()
        self.assertIsNone(scan.settled_at)

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_new_node_resets_idle_clock(self, mock_from_server):
        # last_triggered is old enough to have settled on its own, but a node
        # with a recent createTime means the scan is still actively finding
        # things — it must not settle yet.
        scan = self._scan(idle_minutes_ago=10)
        client = mock_from_server.return_value.__enter__.return_value
        recent = (timezone.now() - timedelta(minutes=1)).isoformat()
        client.list_nodes.return_value = [_onms_node(1, create_time=recent)]

        PollDiscoveryScansJob(job=mock.Mock()).run()

        scan.refresh_from_db()
        self.assertIsNone(scan.settled_at)
        self.assertIsNotNone(scan.latest_node_created)

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_opennms_error_on_one_scan_does_not_block_others(self, mock_from_server):
        first = self._scan(idle_minutes_ago=10)
        second = self._scan(idle_minutes_ago=10)
        client = mock_from_server.return_value.__enter__.return_value

        def list_nodes(foreign_source=None):
            if foreign_source == first.foreign_source:
                raise OpenNMSError("unreachable")
            return []

        client.list_nodes.side_effect = list_nodes

        with self.assertLogs("netbox.jobs.PollDiscoveryScansJob", level="WARNING"):
            PollDiscoveryScansJob(job=mock.Mock()).run()

        first.refresh_from_db()
        second.refresh_from_db()
        # `first` errored so it never settles; `second` was unaffected.
        self.assertIsNone(first.settled_at)
        self.assertIsNotNone(second.settled_at)

    def test_registered_as_recurring_system_job(self):
        from netbox.registry import registry

        from netbox_opennms.jobs import DISCOVERY_POLL_INTERVAL_MINUTES

        self.assertIn(PollDiscoveryScansJob, registry["system_jobs"])
        self.assertEqual(
            registry["system_jobs"][PollDiscoveryScansJob]["interval"],
            DISCOVERY_POLL_INTERVAL_MINUTES,
        )
