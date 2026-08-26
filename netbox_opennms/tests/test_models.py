# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Tests for the data model: clean() rules, constraints, helpers."""

from dcim.models import (
    Device,
    DeviceRole,
    DeviceType,
    Interface,
    Manufacturer,
    Site,
)
from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from ipam.models import IPAddress
from tenancy.models import Tenant

from netbox_opennms.models import (
    MonitoredService,
    MonitoringDetector,
    MonitoringExclusion,
    MonitoringOverride,
    MonitoringPolicy,
    OpenNMSServer,
    Requisition,
    object_ip_pks,
    override_ip_pks,
)
from netbox_opennms.presets import resolve_policy

FILTER = {"site": ["raleigh"], "role": ["router"]}


class RequisitionAndRuleTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.req = Requisition.objects.create(
            name="netbox.raleigh.router", filter_params=FILTER
        )

    def test_requisition_str(self):
        self.assertEqual(str(self.req), "netbox.raleigh.router")

    def test_detector_preset_fills_class_and_params(self):
        detector = MonitoringDetector(requisition=self.req, name="ICMP", preset="icmp")
        detector.clean()
        self.assertTrue(detector.rule_class.endswith("IcmpDetector"))
        self.assertIn("timeout", detector.parameters)

    def test_detector_user_params_win_over_preset_defaults(self):
        detector = MonitoringDetector(
            requisition=self.req, name="ICMP", preset="icmp",
            parameters={"timeout": "9000"},
        )
        detector.clean()
        self.assertEqual(detector.parameters["timeout"], "9000")

    def test_detector_save_persists_preset_class(self):
        detector = MonitoringDetector.objects.create(
            requisition=self.req, name="ICMP", preset="icmp"
        )
        detector.refresh_from_db()
        self.assertTrue(detector.rule_class.endswith("IcmpDetector"))
        self.assertIn("timeout", detector.parameters)

    def test_detector_without_preset_or_class_is_invalid(self):
        detector = MonitoringDetector(requisition=self.req, name="x")
        with self.assertRaises(ValidationError):
            detector.clean()

    def test_policy_preset_fills_class(self):
        policy = MonitoringPolicy(
            requisition=self.req, name="cat", preset="set-node-category",
            parameters={"category": "Routers"},
        )
        policy.clean()
        self.assertTrue(policy.rule_class.endswith("NodeCategorySettingPolicy"))

    def test_preset_owns_rule_class(self):
        # A preset always (re)derives the class — a user-supplied rule_class can't
        # override it (hard association).
        detector = MonitoringDetector(
            requisition=self.req, name="ICMP", preset="icmp",
            rule_class="org.example.NotThis",
        )
        detector.clean()
        self.assertTrue(detector.rule_class.endswith("IcmpDetector"))

    def test_unknown_preset_does_not_blank_existing_class(self):
        # An admin-extended preset with no registry entry must not wipe the class
        # (review #1) — an existing freeform class is preserved.
        detector = MonitoringDetector(
            requisition=self.req, name="x", preset="not-a-registered-preset",
            rule_class="org.example.Custom",
        )
        detector.clean()
        self.assertEqual(detector.rule_class, "org.example.Custom")

    def test_preset_default_not_resurrected_after_deletion(self):
        # Deleting a seeded default and saving must not re-add it (review #4).
        detector = MonitoringDetector.objects.create(
            requisition=self.req, name="i", preset="icmp"
        )
        self.assertIn("retries", detector.parameters)
        detector.parameters = {"timeout": "2000"}
        detector.save()
        detector.refresh_from_db()
        self.assertNotIn("retries", detector.parameters)

    def test_all_policy_presets_resolve_to_a_class(self):
        for preset, suffix in (
            ("match-ip-interface", "MatchingIpInterfacePolicy"),
            ("match-snmp-interface", "MatchingSnmpInterfacePolicy"),
            ("script-policy", "ScriptPolicy"),
            ("set-interface-metadata", "InterfaceMetadataSettingPolicy"),
            ("set-node-category", "NodeCategorySettingPolicy"),
            ("set-node-metadata", "NodeMetadataSettingPolicy"),
        ):
            cls, _params = resolve_policy(preset)
            self.assertTrue(cls.endswith(suffix), f"{preset} → {cls}")

    def test_tcp_preset_requires_port(self):
        bad = MonitoringDetector(requisition=self.req, name="tcp", preset="tcp")
        with self.assertRaises(ValidationError):
            bad.clean()
        ok = MonitoringDetector(
            requisition=self.req, name="tcp2", preset="tcp",
            parameters={"port": "8080"},
        )
        ok.clean()

    def test_set_category_preset_requires_category(self):
        bad = MonitoringPolicy(
            requisition=self.req, name="cat", preset="set-node-category"
        )
        with self.assertRaises(ValidationError):
            bad.clean()

    def test_detector_unique_per_requisition_name(self):
        MonitoringDetector.objects.create(
            requisition=self.req, name="ICMP", rule_class="X"
        )
        with transaction.atomic(), self.assertRaises(IntegrityError):
            MonitoringDetector.objects.create(
                requisition=self.req, name="ICMP", rule_class="Y"
            )


class RequisitionModelTest(TestCase):
    def test_url_unsafe_name_rejected(self):
        req = Requisition(name="bad name", filter_params={"site": ["x"]})
        with self.assertRaises(ValidationError):
            req.clean()

    def test_invalid_service_name_rejected(self):
        req = Requisition(name="x", filter_params=FILTER, services=["BOGUS"])
        with self.assertRaises(ValidationError):
            req.clean()


class OverrideAndServiceTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        site = Site.objects.create(name="Raleigh", slug="raleigh")
        role = DeviceRole.objects.create(name="Router", slug="router")
        mfr = Manufacturer.objects.create(name="Acme", slug="acme")
        dt = DeviceType.objects.create(manufacturer=mfr, model="M1", slug="m1")
        cls.device = Device.objects.create(
            name="rtr-1", device_type=dt, role=role, site=site
        )
        iface = Interface.objects.create(device=cls.device, name="eth0", type="virtual")
        cls.ip = IPAddress.objects.create(address="10.0.0.1/24", assigned_object=iface)
        cls.other_ip = IPAddress.objects.create(address="10.9.9.9/24")

    def test_object_ip_pks(self):
        self.assertEqual(object_ip_pks(self.device), {self.ip.pk})

    def test_override_str_and_ip_pks(self):
        override = MonitoringOverride.objects.create(
            assigned_object=self.device, management_ip=self.ip
        )
        self.assertEqual(str(override), "Override: rtr-1")
        self.assertEqual(override_ip_pks(override), {self.ip.pk})

    def test_override_unique_per_object(self):
        MonitoringOverride.objects.create(assigned_object=self.device)
        with transaction.atomic(), self.assertRaises(IntegrityError):
            MonitoringOverride.objects.create(assigned_object=self.device)

    def test_override_invalid_location(self):
        override = MonitoringOverride(assigned_object=self.device, location="bad name")
        with self.assertRaises(ValidationError):
            override.clean()

    def test_override_invalid_suppressed_service(self):
        override = MonitoringOverride(
            assigned_object=self.device, suppressed_services=["BOGUS"]
        )
        with self.assertRaises(ValidationError):
            override.clean()

    def test_service_must_be_on_override_ip(self):
        override = MonitoringOverride.objects.create(
            assigned_object=self.device, management_ip=self.ip
        )
        bad = MonitoredService(override=override, ip_address=self.other_ip, name="ICMP")
        with self.assertRaises(ValidationError):
            bad.clean()
        ok = MonitoredService(override=override, ip_address=self.ip, name="ICMP")
        ok.clean()

    def test_service_unique(self):
        override = MonitoringOverride.objects.create(
            assigned_object=self.device, management_ip=self.ip
        )
        MonitoredService.objects.create(
            override=override, ip_address=self.ip, name="ICMP"
        )
        with transaction.atomic(), self.assertRaises(IntegrityError):
            MonitoredService.objects.create(
                override=override, ip_address=self.ip, name="ICMP"
            )


class OpenNMSServerTest(TestCase):
    def test_str_is_name(self):
        server = OpenNMSServer.objects.create(name="Acme", url="https://onms.example")
        self.assertEqual(str(server), "Acme")

    def test_url_must_have_a_scheme(self):
        server = OpenNMSServer(name="Acme", url="onms.example")
        with self.assertRaises(ValidationError):
            server.clean()

    def test_invalid_default_location_rejected(self):
        server = OpenNMSServer(
            name="Acme", url="https://onms.example", default_location="bad name"
        )
        with self.assertRaises(ValidationError):
            server.clean()

    def test_only_one_default_server_allowed(self):
        OpenNMSServer.objects.create(
            name="Acme", url="https://onms.example", is_default=True
        )
        other = OpenNMSServer(
            name="Other", url="https://other.example", is_default=True
        )
        with self.assertRaises(ValidationError):
            other.clean()

    def test_editing_the_existing_default_server_is_allowed(self):
        server = OpenNMSServer.objects.create(
            name="Acme", url="https://onms.example", is_default=True
        )
        server.default_location = "Default"
        server.clean()  # must not raise — excludes itself from the uniqueness check

    def test_credentials_and_headers_are_encrypted_at_rest(self):
        server = OpenNMSServer.objects.create(
            name="Acme",
            url="https://onms.example",
            username="svc",
            password="hunter2",
            headers={"CF-Access-Client-Secret": "shh"},
        )
        table = OpenNMSServer._meta.db_table
        with connection.cursor() as cursor:
            cursor.execute(
                f"SELECT username, password, headers FROM {table} WHERE id = %s",
                [server.pk],
            )
            raw_username, raw_password, raw_headers = cursor.fetchone()
        self.assertNotEqual(raw_username, "svc")
        self.assertNotEqual(raw_password, "hunter2")
        self.assertNotIn("shh", raw_headers)

        server.refresh_from_db()
        self.assertEqual(server.username, "svc")
        self.assertEqual(server.password, "hunter2")
        self.assertEqual(server.headers, {"CF-Access-Client-Secret": "shh"})

    def test_is_healthy_when_never_checked(self):
        server = OpenNMSServer.objects.create(name="Acme", url="https://onms.example")
        self.assertEqual(server.last_check_status, "unknown")
        self.assertTrue(server.is_healthy)

    def test_record_check_result_ok_clears_message(self):
        server = OpenNMSServer.objects.create(
            name="Acme", url="https://onms.example",
            last_check_status="failed", last_check_message="boom",
        )
        server.record_check_result(True)
        self.assertEqual(server.last_check_status, "ok")
        self.assertEqual(server.last_check_message, "")
        self.assertIsNotNone(server.last_check_time)
        self.assertTrue(server.is_healthy)

    def test_record_check_result_failure_keeps_message(self):
        server = OpenNMSServer.objects.create(name="Acme", url="https://onms.example")
        server.record_check_result(False, "connection refused")
        self.assertEqual(server.last_check_status, "failed")
        self.assertEqual(server.last_check_message, "connection refused")
        self.assertFalse(server.is_healthy)

    def test_record_check_result_persists(self):
        server = OpenNMSServer.objects.create(name="Acme", url="https://onms.example")
        server.record_check_result(False, "boom")
        server.refresh_from_db()
        self.assertEqual(server.last_check_status, "failed")
        self.assertEqual(server.last_check_message, "boom")


class MonitoringExclusionTest(TestCase):
    def test_str_falls_back_to_a_placeholder(self):
        exclusion = MonitoringExclusion.objects.create()
        self.assertEqual(str(exclusion), f"Monitoring exclusion #{exclusion.pk}")

    def test_str_uses_description_when_set(self):
        exclusion = MonitoringExclusion.objects.create(description="No monitoring")
        self.assertEqual(str(exclusion), "No monitoring")

    def test_scope_bindings_are_optional(self):
        # A MonitoringExclusion with no bindings at all is valid but inert —
        # matches nothing in scope.resolve_scope.
        exclusion = MonitoringExclusion.objects.create(description="unbound")
        tenant = Tenant.objects.create(name="Acme Corp", slug="acme-corp")
        self.assertEqual(list(exclusion.tenants.all()), [])
        exclusion.tenants.add(tenant)
        self.assertEqual(list(exclusion.tenants.all()), [tenant])
