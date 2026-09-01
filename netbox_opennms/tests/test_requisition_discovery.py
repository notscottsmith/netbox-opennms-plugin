# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Tests for unmirrored Foreign Source discovery + import (issues #11, #22)."""

from unittest import mock

from dcim.models import Site
from django.contrib.auth.models import Permission, User
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from netbox_opennms.client import OpenNMSError
from netbox_opennms.models import MonitoringDetector, OpenNMSServer, Requisition
from netbox_opennms.requisition_discovery import (
    RuleImport,
    _parameters_from_entry,
    build_foreign_source_import,
    unmirrored_requisitions,
)

VIEW_PERM = "netbox_opennms.view_opennmsserver"
ADD_REQUISITION_PERM = "netbox_opennms.add_requisition"


class UnmirroredRequisitionsTest(SimpleTestCase):
    def test_names_not_in_netbox_are_unmirrored(self):
        result = unmirrored_requisitions(["fs-a", "fs-b"], ["fs-a"])
        self.assertEqual(result, ["fs-b"])

    def test_fully_mirrored_returns_empty(self):
        self.assertEqual(unmirrored_requisitions(["fs-a"], ["fs-a", "fs-b"]), [])

    def test_empty_opennms_names_returns_empty(self):
        self.assertEqual(unmirrored_requisitions([], ["fs-a"]), [])

    def test_result_is_sorted(self):
        result = unmirrored_requisitions(["fs-c", "fs-a", "fs-b"], [])
        self.assertEqual(result, ["fs-a", "fs-b", "fs-c"])

    def test_duplicates_collapse(self):
        result = unmirrored_requisitions(["fs-a", "fs-a"], [])
        self.assertEqual(result, ["fs-a"])


class UnmirroredRequisitionsViewTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.server = OpenNMSServer.objects.create(
            name="Acme", url="https://onms.example", username="svc", password="x"
        )
        Requisition.objects.create(name="fs-mirrored")

    def setUp(self):
        self.user = User.objects.create_user(username="tester")
        self.user.user_permissions.add(
            Permission.objects.get(
                codename="view_opennmsserver", content_type__app_label="netbox_opennms"
            )
        )
        self.client.force_login(self.user)

    def _url(self):
        return reverse(
            "plugins:netbox_opennms:opennmsserver_unmirrored_requisitions",
            args=[self.server.pk],
        )

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_shows_only_unmirrored_names(self, mock_from_server):
        client = mock_from_server.return_value.__enter__.return_value
        client.list_requisition_names.return_value = ["fs-mirrored", "fs-orphan"]
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["names"], ["fs-orphan"])
        self.assertContains(response, "fs-orphan")
        self.assertNotContains(response, "fs-mirrored")

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_client_failure_shows_error(self, mock_from_server):
        client = mock_from_server.return_value.__enter__.return_value
        client.list_requisition_names.side_effect = OpenNMSError("unreachable")
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "unreachable")

    def test_requires_view_permission(self):
        self.user.user_permissions.clear()
        response = self.client.get(self._url())
        self.assertEqual(response.status_code, 403)


class ParametersFromEntryTest(SimpleTestCase):
    """Unit tests for the (unverified-against-a-live-server, see docstring)
    parameter-parsing helper, per each of ``_as_list``'s own documented shapes.
    """

    def test_list_shape(self):
        entry = {
            "name": "ICMP",
            "parameter": [
                {"key": "retries", "value": "3"},
                {"key": "timeout", "value": "3000"},
            ],
        }
        self.assertEqual(
            _parameters_from_entry(entry), {"retries": "3", "timeout": "3000"}
        )

    def test_bare_dict_shape(self):
        # OpenNMS's v1 REST serializer unwraps a single-element collection.
        entry = {"name": "ICMP", "parameter": {"key": "retries", "value": "3"}}
        self.assertEqual(_parameters_from_entry(entry), {"retries": "3"})

    def test_absent_shape(self):
        entry = {"name": "ICMP"}
        self.assertEqual(_parameters_from_entry(entry), {})

    def test_non_dict_entry_returns_empty(self):
        self.assertEqual(_parameters_from_entry(None), {})


class BuildForeignSourceImportTest(SimpleTestCase):
    def test_full_definition(self):
        definition = {
            "scan-interval": "30m",
            "detectors": {
                "detector": [
                    {
                        "name": "ICMP",
                        "class": "org.opennms.netmgt.provision.detector.icmp."
                        "IcmpDetector",
                        "parameter": [{"key": "retries", "value": "3"}],
                    }
                ]
            },
            "policies": {
                "policy": {
                    "name": "Persist",
                    "class": "org.opennms.netmgt.provision.persist.policies."
                    "MatchingIpInterfacePolicy",
                }
            },
        }
        result = build_foreign_source_import(definition)
        self.assertEqual(result.scan_interval, "30m")
        self.assertEqual(
            result.detectors,
            [
                RuleImport(
                    name="ICMP",
                    rule_class="org.opennms.netmgt.provision.detector.icmp."
                    "IcmpDetector",
                    parameters={"retries": "3"},
                )
            ],
        )
        self.assertEqual(
            result.policies,
            [
                RuleImport(
                    name="Persist",
                    rule_class="org.opennms.netmgt.provision.persist.policies."
                    "MatchingIpInterfacePolicy",
                    parameters={},
                )
            ],
        )

    def test_missing_scan_interval_defaults_to_1d(self):
        self.assertEqual(build_foreign_source_import({}).scan_interval, "1d")

    def test_none_definition_treated_as_empty(self):
        result = build_foreign_source_import(None)
        self.assertEqual(result.scan_interval, "1d")
        self.assertEqual(result.detectors, [])
        self.assertEqual(result.policies, [])

    def test_entry_without_name_is_skipped(self):
        definition = {"detectors": {"detector": [{"class": "x.Y"}]}}
        self.assertEqual(build_foreign_source_import(definition).detectors, [])


class RequisitionImportViewTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.server = OpenNMSServer.objects.create(
            name="Acme", url="https://onms.example", username="svc", password="x"
        )
        cls.site = Site.objects.create(name="Raleigh", slug="raleigh")
        Requisition.objects.create(name="fs-existing")

    def setUp(self):
        self.user = User.objects.create_user(username="importer")
        self.user.user_permissions.add(
            Permission.objects.get(
                codename="add_requisition", content_type__app_label="netbox_opennms"
            )
        )
        self.client.force_login(self.user)

    def _url(self):
        return reverse(
            "plugins:netbox_opennms:opennmsserver_import_requisition",
            args=[self.server.pk],
        )

    def _post_data(self, foreign_source, **overrides):
        data = {
            "foreign_source": foreign_source,
            "object_types": "device",
            "filter_params": "{}",
            "scan_interval": "1d",
            "default_interfaces": "primary",
            "scope_site": self.site.pk,
        }
        data.update(overrides)
        return data

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_get_renders_form_with_name_locked(self, mock_from_server):
        client = mock_from_server.return_value.__enter__.return_value
        client.get_foreign_source.return_value = {"scan-interval": "30m"}
        response = self.client.get(self._url() + "?foreign_source=fs-new")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["form"].fields["name"].disabled)
        self.assertContains(response, "fs-new")

    def test_get_with_no_foreign_source_redirects(self):
        response = self.client.get(self._url(), follow=True)
        self.assertContains(response, "No Foreign Source given")

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_import_creates_requisition_with_detectors_and_policies(
        self, mock_from_server
    ):
        client = mock_from_server.return_value.__enter__.return_value
        client.get_foreign_source.return_value = {
            "scan-interval": "30m",
            "detectors": {
                "detector": {
                    "name": "ICMP",
                    "class": "org.opennms.netmgt.provision.detector.icmp."
                    "IcmpDetector",
                    "parameter": [{"key": "retries", "value": "3"}],
                }
            },
            "policies": {
                "policy": {
                    "name": "Persist",
                    "class": "org.opennms.netmgt.provision.persist.policies."
                    "MatchingIpInterfacePolicy",
                }
            },
        }
        response = self.client.post(
            self._url(), self._post_data("fs-new"), follow=True
        )
        self.assertEqual(response.status_code, 200)

        requisition = Requisition.objects.get(name="fs-new")
        self.assertEqual(requisition.scan_interval, "30m")
        self.assertEqual(requisition.filter_params, {"site": [self.site.slug]})

        detector = requisition.detectors.get()
        self.assertEqual(detector.name, "ICMP")
        self.assertEqual(
            detector.rule_class,
            "org.opennms.netmgt.provision.detector.icmp.IcmpDetector",
        )
        self.assertEqual(detector.parameters, {"retries": "3"})

        policy = requisition.policies.get()
        self.assertEqual(policy.name, "Persist")
        self.assertEqual(
            policy.rule_class,
            "org.opennms.netmgt.provision.persist.policies."
            "MatchingIpInterfacePolicy",
        )

        self.assertContains(response, "Imported")

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_no_scope_pick_is_rejected(self, mock_from_server):
        client = mock_from_server.return_value.__enter__.return_value
        client.get_foreign_source.return_value = {
            "detectors": {}, "policies": {},
        }
        response = self.client.post(
            self._url(),
            self._post_data("fs-new", scope_site=""),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pick at least one Scope level")
        self.assertFalse(Requisition.objects.filter(name="fs-new").exists())

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_existing_name_is_rejected(self, mock_from_server):
        response = self.client.post(
            self._url(), self._post_data("fs-existing"), follow=True
        )
        self.assertContains(response, "already exists")
        mock_from_server.assert_not_called()
        self.assertEqual(
            Requisition.objects.filter(name="fs-existing").count(), 1
        )

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_missing_foreign_source_is_rejected(self, mock_from_server):
        client = mock_from_server.return_value.__enter__.return_value
        client.get_foreign_source.return_value = None
        response = self.client.post(
            self._url(), self._post_data("fs-gone"), follow=True
        )
        self.assertContains(response, "no longer exists")
        self.assertFalse(Requisition.objects.filter(name="fs-gone").exists())

    @mock.patch("netbox_opennms.client.OpenNMSClient.from_server")
    def test_unreachable_server_is_rejected(self, mock_from_server):
        client = mock_from_server.return_value.__enter__.return_value
        client.get_foreign_source.side_effect = OpenNMSError("unreachable")
        response = self.client.post(
            self._url(), self._post_data("fs-new"), follow=True
        )
        self.assertContains(response, "unreachable")
        self.assertFalse(Requisition.objects.filter(name="fs-new").exists())

    def test_requires_add_requisition_permission(self):
        self.user.user_permissions.clear()
        response = self.client.post(self._url(), self._post_data("fs-new"))
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Requisition.objects.filter(name="fs-new").exists())

    def test_no_foreign_source_given(self):
        response = self.client.post(self._url(), {}, follow=True)
        self.assertContains(response, "No Foreign Source given")

    def test_detector_import_leaves_no_parameters_when_absent(self):
        with mock.patch(
            "netbox_opennms.client.OpenNMSClient.from_server"
        ) as mock_from_server:
            client = mock_from_server.return_value.__enter__.return_value
            client.get_foreign_source.return_value = {
                "detectors": {"detector": {"name": "SNMP", "class": "x.Y"}}
            }
            self.client.post(self._url(), self._post_data("fs-noparams"))
        detector = MonitoringDetector.objects.get(
            requisition__name="fs-noparams", name="SNMP"
        )
        self.assertEqual(detector.parameters, {})
