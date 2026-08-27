# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Form-layer tests for the Epic 5 models (override IP ownership)."""

from core.models import ObjectType
from dcim.models import (
    Device,
    DeviceRole,
    DeviceType,
    Interface,
    Location,
    Manufacturer,
    Site,
    SiteGroup,
)
from django.core.exceptions import ValidationError
from django.test import TestCase
from extras.models import SavedFilter
from ipam.models import IPAddress
from tenancy.models import Tenant, TenantGroup

from netbox_opennms.forms import (
    DiscoveryScanForm,
    MonitoringOverrideForm,
    OpenNMSServerForm,
    RequisitionForm,
)
from netbox_opennms.models import (
    MonitoredInterface,
    MonitoredService,
    MonitoringOverride,
    OpenNMSServer,
    Requisition,
)


class MonitoringOverrideFormTest(TestCase):
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
        cls.own_ip = IPAddress.objects.create(
            address="10.0.0.1/24", assigned_object=iface
        )
        cls.other = Device.objects.create(
            name="rtr-2", device_type=dt, role=role, site=site
        )
        oface = Interface.objects.create(device=cls.other, name="eth0", type="virtual")
        cls.foreign_ip = IPAddress.objects.create(
            address="10.0.0.2/24", assigned_object=oface
        )

    def test_exactly_one_target_required(self):
        form = MonitoringOverrideForm(data={"exclude": False, "location": ""})
        self.assertFalse(form.is_valid())


class RequisitionSavedFilterImportTest(TestCase):
    def _form(self, **overrides):
        data = {
            "name": "core-switches",
            "object_types": "device",
            "filter_params": "{}",
            "scan_interval": "1d",
            "default_interfaces": "primary",
        }
        data.update(overrides)
        return RequisitionForm(data=data)

    def test_import_copies_saved_filter_parameters(self):
        saved = SavedFilter.objects.create(
            name="Switches", slug="switches", parameters={"role": ["switch"]}
        )
        saved.object_types.set([ObjectType.objects.get_for_model(Device)])
        form = self._form(import_from_saved_filter=saved.pk)
        self.assertTrue(form.is_valid(), form.errors)
        # One-shot copy: the empty filter is replaced by the Saved Filter's params.
        self.assertEqual(form.cleaned_data["filter_params"], {"role": ["switch"]})

    def test_import_and_typed_filter_conflict_is_rejected(self):
        # Picking a Saved Filter AND typing a filter is ambiguous — reject, don't
        # silently discard the typed one (review #5).
        saved = SavedFilter.objects.create(
            name="Switches", slug="switches", parameters={"role": ["switch"]}
        )
        saved.object_types.set([ObjectType.objects.get_for_model(Device)])
        form = self._form(
            import_from_saved_filter=saved.pk, filter_params='{"role": ["router"]}'
        )
        self.assertFalse(form.is_valid())
        self.assertIn("import_from_saved_filter", form.errors)

    def test_import_of_empty_saved_filter_is_still_guarded(self):
        # Importing a Saved Filter with no effective constraint is rejected (H1).
        saved = SavedFilter.objects.create(
            name="Everything", slug="everything", parameters={}
        )
        saved.object_types.set([ObjectType.objects.get_for_model(Device)])
        form = self._form(import_from_saved_filter=saved.pk)
        self.assertFalse(form.is_valid())
        self.assertIn("filter_params", form.errors)


class RequisitionScopePickerTest(TestCase):
    """Issue #19: the Scope picker writes/updates filter_params and its options
    are constrained to the Requisition's own target Server."""

    @classmethod
    def setUpTestData(cls):
        cls.role = DeviceRole.objects.create(name="Router", slug="router")
        mfr = Manufacturer.objects.create(name="Acme", slug="acme")
        cls.device_type = DeviceType.objects.create(
            manufacturer=mfr, model="M1", slug="m1"
        )
        cls.tenant_group = TenantGroup.objects.create(name="MSP", slug="msp")
        cls.tenant = Tenant.objects.create(
            name="Acme Corp", slug="acme-corp", group=cls.tenant_group
        )
        cls.site_group = SiteGroup.objects.create(name="East", slug="east")
        cls.site_a = Site.objects.create(
            name="Raleigh", slug="raleigh", group=cls.site_group
        )
        cls.site_b = Site.objects.create(name="Durham", slug="durham")
        cls.location = Location.objects.create(
            name="Rack 1", slug="rack-1", site=cls.site_a
        )
        cls.server_a = OpenNMSServer.objects.create(
            name="Server A", url="https://a.example"
        )
        cls.server_a.sites.add(cls.site_a)
        cls.server_b = OpenNMSServer.objects.create(
            name="Server B", url="https://b.example"
        )
        cls.server_b.sites.add(cls.site_b)

    def _data(self, **overrides):
        data = {
            "name": "core-switches",
            "object_types": "device",
            "filter_params": "{}",
            "scan_interval": "1d",
            "default_interfaces": "primary",
        }
        data.update(overrides)
        return data

    def test_new_requisition_picker_is_unconstrained(self):
        # No target Server yet (never scoped, never deployed) — every Site is a
        # valid pick, from either bound Server.
        form = RequisitionForm()
        sites = set(form.fields["scope_site"].queryset)
        self.assertIn(self.site_a, sites)
        self.assertIn(self.site_b, sites)

    def test_picking_a_site_writes_filter_params(self):
        form = RequisitionForm(data=self._data(scope_site=self.site_a.pk))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(
            form.cleaned_data["filter_params"], {"site": [self.site_a.slug]}
        )

    def test_picker_merges_with_hand_written_filter_params(self):
        form = RequisitionForm(
            data=self._data(
                filter_params='{"role": ["router"]}', scope_tenant=self.tenant.pk
            )
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(
            form.cleaned_data["filter_params"],
            {"role": ["router"], "tenant": [self.tenant.slug]},
        )

    def test_editing_requisition_constrains_picker_to_its_target_server(self):
        # A Device on Site A gives the Requisition current members that resolve
        # to Server A (bound to Site A) — resolve_target_server's "current
        # members" path.
        Device.objects.create(
            name="rtr-a", device_type=self.device_type, role=self.role, site=self.site_a
        )
        requisition = Requisition.objects.create(
            name="scoped-to-a",
            object_types="device",
            filter_params={"site": [self.site_a.slug]},
        )
        form = RequisitionForm(instance=requisition)
        sites = set(form.fields["scope_site"].queryset)
        self.assertIn(self.site_a, sites)
        self.assertNotIn(self.site_b, sites)

    def test_scope_location_rejected_for_vm_only_requisition(self):
        # Virtual Machines have no NetBox Location — VirtualMachineFilterSet has
        # no "location" key, so picking one on a VM-only Requisition trips the
        # existing unknown-key guard (H8).
        form = RequisitionForm(
            data=self._data(object_types="vm", scope_location=self.location.pk)
        )
        self.assertFalse(form.is_valid())
        self.assertIn("filter_params", form.errors)


class MonitoredInterfaceValidationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        site = Site.objects.create(name="Durham", slug="durham")
        role = DeviceRole.objects.create(name="Switch", slug="switch")
        mfr = Manufacturer.objects.create(name="Acme2", slug="acme2")
        dt = DeviceType.objects.create(manufacturer=mfr, model="M2", slug="m2")
        cls.device = Device.objects.create(
            name="sw-1", device_type=dt, role=role, site=site
        )
        iface = Interface.objects.create(device=cls.device, name="eth0", type="virtual")
        cls.mgmt = IPAddress.objects.create(
            address="10.1.0.1/24", assigned_object=iface
        )
        cls.extra = IPAddress.objects.create(
            address="10.1.0.2/24", assigned_object=iface
        )
        cls.device.primary_ip4 = cls.mgmt
        cls.device.save()
        other = Device.objects.create(
            name="sw-2", device_type=dt, role=role, site=site
        )
        oface = Interface.objects.create(device=other, name="eth0", type="virtual")
        cls.foreign = IPAddress.objects.create(
            address="10.1.0.9/24", assigned_object=oface
        )
        cls.override = MonitoringOverride.objects.create(assigned_object=cls.device)

    def test_foreign_ip_rejected(self):
        interface = MonitoredInterface(
            override=self.override, ip_address=self.foreign, role="N"
        )
        with self.assertRaises(ValidationError):
            interface.clean()

    def test_own_ip_accepted(self):
        interface = MonitoredInterface(
            override=self.override, ip_address=self.extra, role="N"
        )
        interface.clean()  # no raise

    def test_second_primary_rejected(self):
        # management_role defaults to Primary, so a second Primary is rejected.
        interface = MonitoredInterface(
            override=self.override, ip_address=self.extra, role="P"
        )
        with self.assertRaises(ValidationError):
            interface.clean()


class OpenNMSServerFormTest(TestCase):
    """ADR 0002: a Scope object may be bound directly to only one Server."""

    @classmethod
    def setUpTestData(cls):
        cls.site = Site.objects.create(name="Raleigh", slug="raleigh")

    def _data(self, **overrides):
        data = {
            "name": "New Server",
            "url": "https://new.example",
            "username": "svc",
            "password": "hunter2",
            "headers": "{}",
        }
        data.update(overrides)
        return data

    def test_default_server_cannot_carry_scope_bindings(self):
        form = OpenNMSServerForm(
            data=self._data(is_default="on", sites=[self.site.pk])
        )
        self.assertFalse(form.is_valid())
        self.assertIn("is_default", form.errors)

    def test_site_already_bound_to_another_server_is_rejected(self):
        OpenNMSServer.objects.create(
            name="Existing", url="https://existing.example"
        ).sites.add(self.site)
        form = OpenNMSServerForm(data=self._data(sites=[self.site.pk]))
        self.assertFalse(form.is_valid())
        self.assertIn("sites", form.errors)

    def test_unbound_site_is_accepted(self):
        form = OpenNMSServerForm(data=self._data(sites=[self.site.pk]))
        self.assertTrue(form.is_valid(), form.errors)

    def test_editing_the_server_that_already_owns_the_binding_is_allowed(self):
        server = OpenNMSServer.objects.create(
            name="Existing", url="https://existing.example"
        )
        server.sites.add(self.site)
        form = OpenNMSServerForm(
            data=self._data(name="Existing", sites=[self.site.pk]), instance=server
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_default_location_choices_seeded_from_current_value(self):
        server = OpenNMSServer.objects.create(
            name="Existing", url="https://existing.example",
            default_location="edge-1",
        )
        form = OpenNMSServerForm(instance=server)
        self.assertIn(
            ("edge-1", "edge-1"), form.fields["default_location"].widget.choices
        )

    def test_new_server_has_no_default_location_choices(self):
        form = OpenNMSServerForm()
        self.assertEqual(list(form.fields["default_location"].widget.choices), [])

    def test_default_location_value_outside_choices_still_saves(self):
        # A value the JS added client-side after "Test connection" (not among the
        # server-rendered <option>s) must still validate and save (CharField).
        form = OpenNMSServerForm(
            data=self._data(default_location="edge-9-not-in-choices")
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(
            form.cleaned_data["default_location"], "edge-9-not-in-choices"
        )


class DiscoveryScanFormTest(TestCase):
    """ADR 0009: a Requisition and an OpenNMS Monitoring Location are required."""

    @classmethod
    def setUpTestData(cls):
        cls.server = OpenNMSServer.objects.create(
            name="Acme", url="https://onms.example"
        )
        cls.requisition = Requisition.objects.create(name="fs-1")

    def _data(self, **overrides):
        data = {
            "server": self.server.pk,
            "requisition": self.requisition.pk,
            "location": "raleigh",
            "ip_range_begin": "10.0.0.1",
            "ip_range_end": "10.0.0.254",
            "retries": 1,
            "timeout": 2000,
        }
        data.update(overrides)
        return data

    def test_requisition_required(self):
        form = DiscoveryScanForm(data=self._data(requisition=""))
        self.assertFalse(form.is_valid())

    def test_location_required(self):
        form = DiscoveryScanForm(data=self._data(location=""))
        self.assertFalse(form.is_valid())

    def test_with_requisition_and_location_is_accepted(self):
        form = DiscoveryScanForm(data=self._data())
        self.assertTrue(form.is_valid(), form.errors)

    def test_end_before_begin_is_rejected(self):
        form = DiscoveryScanForm(
            data=self._data(
                ip_range_begin="10.0.0.254",
                ip_range_end="10.0.0.1",
            )
        )
        self.assertFalse(form.is_valid())


class InterfaceServicePruneTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        site = Site.objects.create(name="Cary", slug="cary")
        role = DeviceRole.objects.create(name="Sw", slug="sw")
        mfr = Manufacturer.objects.create(name="Acme3", slug="acme3")
        dt = DeviceType.objects.create(manufacturer=mfr, model="M3", slug="m3")
        cls.device = Device.objects.create(
            name="sw-p", device_type=dt, role=role, site=site
        )
        iface = Interface.objects.create(device=cls.device, name="eth0", type="virtual")
        cls.ip_a = IPAddress.objects.create(
            address="10.2.0.2/24", assigned_object=iface
        )
        cls.ip_b = IPAddress.objects.create(
            address="10.2.0.3/24", assigned_object=iface
        )
        cls.override = MonitoringOverride.objects.create(assigned_object=cls.device)

    def test_editing_interface_ip_prunes_stale_service(self):
        interface = MonitoredInterface.objects.create(
            override=self.override, ip_address=self.ip_a, role="N"
        )
        MonitoredService.objects.create(
            override=self.override, ip_address=self.ip_a, name="HTTP"
        )
        # Move the interface to IP-B: the service on IP-A is now orphaned and pruned.
        interface.ip_address = self.ip_b
        interface.save()
        self.assertFalse(
            MonitoredService.objects.filter(
                override=self.override, ip_address=self.ip_a
            ).exists()
        )
