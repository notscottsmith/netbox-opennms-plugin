# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Form-layer tests for the Epic 5 models (override IP ownership)."""

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
from ipam.models import IPAddress
from tenancy.models import Tenant, TenantGroup

from netbox_opennms.forms import (
    DiscoveryScanForm,
    MetadataContextForm,
    MetadataEntryForm,
    MetadataKeyForm,
    MonitoringOverrideForm,
    OpenNMSServerForm,
    RequisitionForm,
)
from netbox_opennms.models import (
    Category,
    MetadataContext,
    MetadataEntry,
    MetadataKey,
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


class RequisitionScopeRequiredTest(TestCase):
    """A Requisition must always be Scope-anchored: an Advanced filter alone
    (no Tenant Group/Tenant/Site Group/Site/Location pick) is rejected, and
    "Import from Saved Filter" no longer exists as a way around that."""

    @classmethod
    def setUpTestData(cls):
        cls.site = Site.objects.create(name="Raleigh", slug="raleigh")

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

    def test_saved_filter_import_field_no_longer_exists(self):
        form = self._form()
        self.assertNotIn("import_from_saved_filter", form.fields)

    def test_advanced_filter_alone_is_rejected(self):
        form = self._form(filter_params='{"role": ["router"]}')
        self.assertFalse(form.is_valid())
        self.assertIn("Pick at least one Scope level", str(form.errors))

    def test_scope_pick_with_no_advanced_filter_is_accepted(self):
        form = self._form(scope_site=self.site.pk)
        self.assertTrue(form.is_valid(), form.errors)

    def test_scope_location_auto_selected_from_matching_slug(self):
        location = Location.objects.create(
            name="raleigh", slug="raleigh", site=self.site
        )
        requisition = Requisition.objects.create(
            name="core-switches",
            object_types="device",
            filter_params={"site": [self.site.slug]},
            location="raleigh",
        )
        form = RequisitionForm(instance=requisition)
        self.assertEqual(form.initial.get("scope_location"), location.pk)


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

    def test_location_choices_sourced_from_resolved_target_server(self):
        # Issue: default_location's dropdown was already fixed to seed from
        # available_locations — the Requisition's own `location` field needs
        # the same treatment, sourced from whichever Server it resolves to.
        self.server_a.available_locations = ["edge-1", "edge-2"]
        self.server_a.save(update_fields=["available_locations"])
        Device.objects.create(
            name="rtr-a", device_type=self.device_type, role=self.role, site=self.site_a
        )
        requisition = Requisition.objects.create(
            name="scoped-to-a",
            object_types="device",
            filter_params={"site": [self.site_a.slug]},
        )
        form = RequisitionForm(instance=requisition)
        self.assertEqual(
            list(form.fields["location"].widget.choices),
            [("", "---------"), ("edge-1", "edge-1"), ("edge-2", "edge-2")],
        )

    def test_location_choices_empty_for_new_requisition(self):
        # No target Server resolved yet (never scoped, never deployed) — only
        # the blank option is offered.
        form = RequisitionForm()
        self.assertEqual(
            list(form.fields["location"].widget.choices), [("", "---------")]
        )

    def test_location_current_value_added_to_cached_choices(self):
        # A value outside the cache (e.g. set before the cache existed, or the
        # cache changed since) must still appear so the edit form doesn't
        # silently drop it.
        self.server_a.available_locations = ["edge-1"]
        self.server_a.save(update_fields=["available_locations"])
        Device.objects.create(
            name="rtr-a", device_type=self.device_type, role=self.role, site=self.site_a
        )
        requisition = Requisition.objects.create(
            name="scoped-to-a",
            object_types="device",
            filter_params={"site": [self.site_a.slug]},
            location="edge-9",
        )
        form = RequisitionForm(instance=requisition)
        self.assertEqual(
            list(form.fields["location"].widget.choices),
            [("", "---------"), ("edge-9", "edge-9"), ("edge-1", "edge-1")],
        )

    def test_location_value_outside_choices_still_saves(self):
        # CharField doesn't validate against the widget's choices — a value the
        # cache doesn't (yet) know about must still validate and save.
        form = RequisitionForm(
            data=self._data(
                location="edge-9-not-in-choices", scope_site=self.site_a.pk
            )
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["location"], "edge-9-not-in-choices")


class RequisitionAutoNamingTest(TestCase):
    """Issue #20: a blank name is derived from the Scope picker; a raw/freeform
    filter (no Scope-picker fields set) still requires an explicit name."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant = Tenant.objects.create(name="Acme Corp", slug="acme-corp")
        cls.site = Site.objects.create(name="Raleigh", slug="raleigh")

    def _data(self, **overrides):
        data = {
            "name": "",
            "object_types": "device",
            "filter_params": "{}",
            "scan_interval": "1d",
            "default_interfaces": "primary",
        }
        data.update(overrides)
        return data

    def test_blank_name_derived_from_scope_picker(self):
        form = RequisitionForm(
            data=self._data(scope_tenant=self.tenant.pk, scope_site=self.site.pk)
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["name"], "acme-corp-raleigh")

    def test_blank_name_without_scope_picker_still_rejected(self):
        # A raw/freeform filter with no Scope-picker fields set is unchanged:
        # a name is still required, enforced by Requisition.clean().
        form = RequisitionForm(data=self._data(filter_params='{"role": ["router"]}'))
        self.assertFalse(form.is_valid())
        self.assertIn("A Requisition name is required.", str(form.errors))


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
        other = Device.objects.create(name="sw-2", device_type=dt, role=role, site=site)
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
        form = OpenNMSServerForm(data=self._data(is_default="on", sites=[self.site.pk]))
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
            name="Existing",
            url="https://existing.example",
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
        self.assertEqual(form.cleaned_data["default_location"], "edge-9-not-in-choices")

    def test_default_location_choices_seeded_from_available_locations(self):
        # The persisted list_locations() cache (populated by "Test connection")
        # must seed the dropdown even when no default_location is set yet.
        server = OpenNMSServer.objects.create(
            name="Existing",
            url="https://existing.example",
            available_locations=["edge-1", "edge-2"],
        )
        form = OpenNMSServerForm(instance=server)
        self.assertEqual(
            list(form.fields["default_location"].widget.choices),
            [("edge-1", "edge-1"), ("edge-2", "edge-2")],
        )

    def test_default_location_current_value_added_to_cached_choices(self):
        # A current value outside the cache (e.g. the cache changed since it was
        # picked) must still appear as a choice, alongside the cached ones.
        server = OpenNMSServer.objects.create(
            name="Existing",
            url="https://existing.example",
            default_location="edge-9",
            available_locations=["edge-1", "edge-2"],
        )
        form = OpenNMSServerForm(instance=server)
        choices = list(form.fields["default_location"].widget.choices)
        self.assertEqual(
            choices,
            [("edge-9", "edge-9"), ("edge-1", "edge-1"), ("edge-2", "edge-2")],
        )


class DiscoveryScanFormTest(TestCase):
    """A Requisition is required, and Location is derived from it, not entered."""

    @classmethod
    def setUpTestData(cls):
        cls.server = OpenNMSServer.objects.create(
            name="Acme", url="https://onms.example"
        )
        # No location and no default_location: the "nothing to derive from"
        # case for test_location_required below.
        cls.requisition = Requisition.objects.create(name="fs-1")

    def _data(self, **overrides):
        data = {
            "server": self.server.pk,
            "requisition": self.requisition.pk,
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
        form = DiscoveryScanForm(data=self._data())
        self.assertFalse(form.is_valid())

    def test_location_field_is_not_on_the_form(self):
        form = DiscoveryScanForm()
        self.assertNotIn("location", form.fields)

    def test_location_derived_from_requisition_is_accepted(self):
        requisition = Requisition.objects.create(name="fs-2", location="raleigh")
        form = DiscoveryScanForm(data=self._data(requisition=requisition.pk))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.instance.location, "raleigh")

    def test_end_before_begin_is_rejected(self):
        requisition = Requisition.objects.create(name="fs-3", location="raleigh")
        form = DiscoveryScanForm(
            data=self._data(
                requisition=requisition.pk,
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


class MetadataContextFormTest(TestCase):
    def test_rejects_non_x_prefixed_name(self):
        form = MetadataContextForm(data={"name": "custom", "description": ""})
        self.assertFalse(form.is_valid())
        self.assertIn("name", form.errors)

    def test_accepts_x_prefixed_name(self):
        form = MetadataContextForm(data={"name": "X-billing", "description": ""})
        self.assertTrue(form.is_valid(), form.errors)


class MetadataKeyFormTest(TestCase):
    def test_accepts_any_name_no_prefix_required(self):
        # Unlike MetadataContextForm, OpenNMS defines no naming-reservation
        # rule for custom keys.
        node = MetadataContext.objects.get(name="node")
        form = MetadataKeyForm(
            data={"context": node.pk, "name": "anything-goes", "description": ""}
        )
        self.assertTrue(form.is_valid(), form.errors)

    def test_duplicate_name_in_same_context_is_rejected(self):
        node = MetadataContext.objects.get(name="node")
        MetadataKey.objects.create(context=node, name="X-dup")
        form = MetadataKeyForm(
            data={"context": node.pk, "name": "X-dup", "description": ""}
        )
        self.assertFalse(form.is_valid())


class MetadataEntryFormContextChoicesTest(TestCase):
    """MetadataEntryForm.context is a dropdown sourced from MetadataContext (#41)."""

    @classmethod
    def setUpTestData(cls):
        cls.req = Requisition.objects.create(
            name="me-form-req", filter_params={"role": ["switch"]}
        )

    def test_context_choices_include_seeded_base_contexts(self):
        form = MetadataEntryForm()
        choice_values = {value for value, _label in form.fields["context"].choices}
        self.assertTrue(
            {"node", "requisition", "interface", "service", "pattern"}.issubset(
                choice_values
            )
        )

    def test_context_choices_include_registered_custom_context(self):
        MetadataContext.objects.create(name="X-billing")
        form = MetadataEntryForm()
        choice_values = {value for value, _label in form.fields["context"].choices}
        self.assertIn("X-billing", choice_values)

    def test_unregistered_context_is_rejected_on_submit(self):
        form = MetadataEntryForm(
            data={
                "requisition": self.req.pk,
                "scope": "node",
                "context": "X-not-registered",
                "key": "k1",
                "value_source": "",
                "literal_value": "v",
            }
        )
        self.assertFalse(form.is_valid())
        self.assertIn("context", form.errors)

    def test_existing_instances_unregistered_value_stays_selectable(self):
        # An entry saved before its context was registered (e.g. a fixture,
        # or data that pre-dates migration 0020's backfill) must still show
        # its current value in the dropdown when editing, even though it
        # wouldn't otherwise validate as a fresh submission.
        entry = MetadataEntry(
            requisition=self.req,
            scope="node",
            context="requisition",
            key="k1",
            literal_value="v",
        )
        entry.context = "X-legacy-unregistered"
        form = MetadataEntryForm(instance=entry)
        choice_values = {value for value, _label in form.fields["context"].choices}
        self.assertIn("X-legacy-unregistered", choice_values)


class MetadataEntryFormScopeLockTest(TestCase):
    """RD-3 bugfix: for a base context, MetadataEntryForm locks scope to match
    (context IS placement), so the UI can't recreate the old conflation bug."""

    @classmethod
    def setUpTestData(cls):
        cls.req = Requisition.objects.create(
            name="me-form-scopelock", filter_params={"role": ["switch"]}
        )

    def test_scope_field_disabled_for_existing_base_context_instance(self):
        entry = MetadataEntry(
            requisition=self.req,
            scope="requisition",
            context="requisition",
            key="k1",
            literal_value="v",
        )
        form = MetadataEntryForm(instance=entry)
        self.assertTrue(form.fields["scope"].disabled)
        self.assertEqual(form.initial["scope"], "requisition")

    def test_scope_field_not_disabled_for_custom_context_instance(self):
        MetadataContext.objects.create(name="X-billing")
        entry = MetadataEntry(
            requisition=self.req,
            scope="service",
            context="X-billing",
            key="k1",
            literal_value="v",
        )
        form = MetadataEntryForm(instance=entry)
        self.assertFalse(form.fields["scope"].disabled)

    def test_submitting_requisition_context_forces_matching_scope(self):
        # Even if a stray "scope" value were submitted, the base context wins
        # (belt-and-suspenders alongside the disabled-field lock).
        form = MetadataEntryForm(
            data={
                "requisition": self.req.pk,
                "scope": "node",
                "context": "requisition",
                "key": "k1",
                "value_source": "",
                "literal_value": "v",
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["scope"], "requisition")


class RequisitionDefaultCategoriesFormTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.site = Site.objects.create(name="Raleigh", slug="raleigh")
        cls.category = Category.objects.create(name="X-core")

    def test_default_categories_field_present(self):
        form = RequisitionForm()
        self.assertIn("default_categories", form.fields)

    def test_saving_default_categories(self):
        form = RequisitionForm(
            data={
                "name": "core-switches",
                "object_types": "device",
                "filter_params": "{}",
                "scan_interval": "1d",
                "default_interfaces": "primary",
                "scope_site": self.site.pk,
                "default_categories": [self.category.pk],
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        requisition = form.save()
        self.assertEqual(list(requisition.default_categories.all()), [self.category])


class MonitoringOverrideCategoriesFormTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        site = Site.objects.create(name="Raleigh", slug="raleigh")
        role = DeviceRole.objects.create(name="Router", slug="router")
        mfr = Manufacturer.objects.create(name="Acme", slug="acme")
        dt = DeviceType.objects.create(manufacturer=mfr, model="M1", slug="m1")
        cls.device = Device.objects.create(
            name="rtr-1", device_type=dt, role=role, site=site
        )
        cls.category = Category.objects.create(name="X-core")

    def test_categories_field_present(self):
        form = MonitoringOverrideForm()
        self.assertIn("categories", form.fields)

    def test_saving_categories(self):
        form = MonitoringOverrideForm(
            data={
                "device": self.device.pk,
                "exclude": False,
                "location": "",
                "categories": [self.category.pk],
            }
        )
        self.assertTrue(form.is_valid(), form.errors)
        override = form.save()
        self.assertEqual(list(override.categories.all()), [self.category])
