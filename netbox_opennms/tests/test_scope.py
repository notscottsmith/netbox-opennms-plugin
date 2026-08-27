# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Tests for Scope Resolution (ADR 0002/0003)."""

from dcim.models import (
    Device,
    DeviceRole,
    DeviceType,
    Location,
    Manufacturer,
    Site,
    SiteGroup,
)
from django.test import TestCase
from ipam.models import VRF
from tenancy.models import Tenant, TenantGroup

from netbox_opennms.models import MonitoringExclusion, OpenNMSServer, VRFAssignment
from netbox_opennms.scope import resolve_scope, resolve_vrf, scope_options


class ScopeResolutionTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.tenant_group = TenantGroup.objects.create(
            name="MSP Customers", slug="msp-customers"
        )
        cls.tenant = Tenant.objects.create(
            name="Acme Corp", slug="acme-corp", group=cls.tenant_group
        )
        cls.site_group = SiteGroup.objects.create(name="East Coast", slug="east-coast")
        cls.site = Site.objects.create(
            name="Raleigh", slug="raleigh", group=cls.site_group
        )
        cls.location = Location.objects.create(
            name="Rack 1", slug="rack-1", site=cls.site
        )
        cls.nested_location = Location.objects.create(
            name="Shelf A", slug="shelf-a", site=cls.site, parent=cls.location
        )
        cls.role = DeviceRole.objects.create(name="Core Router", slug="core-router")
        manufacturer = Manufacturer.objects.create(name="Acme", slug="acme")
        cls.device_type = DeviceType.objects.create(
            manufacturer=manufacturer, model="Model 1", slug="model-1"
        )

    def _device(self, **kwargs):
        kwargs.setdefault("device_type", self.device_type)
        kwargs.setdefault("role", self.role)
        return Device.objects.create(name=kwargs.pop("name", "dev"), **kwargs)

    def test_no_bindings_and_no_default_resolves_to_nothing(self):
        device = self._device(site=self.site)
        result = resolve_scope(device)
        self.assertIsNone(result.server)
        self.assertFalse(result.excluded)

    def test_falls_back_to_default_server(self):
        default = OpenNMSServer.objects.create(
            name="Default", url="https://default.example", is_default=True
        )
        device = self._device(site=self.site)
        result = resolve_scope(device)
        self.assertEqual(result.server, default)
        self.assertFalse(result.excluded)

    def test_tenant_group_binding_matches(self):
        server = OpenNMSServer.objects.create(name="TG Server", url="https://tg.example")
        server.tenant_groups.add(self.tenant_group)
        device = self._device(site=self.site, tenant=self.tenant)
        result = resolve_scope(device)
        self.assertEqual(result.server, server)

    def test_tenant_binding_beats_tenant_group(self):
        group_server = OpenNMSServer.objects.create(
            name="Group Server", url="https://group.example"
        )
        group_server.tenant_groups.add(self.tenant_group)
        tenant_server = OpenNMSServer.objects.create(
            name="Tenant Server", url="https://tenant.example"
        )
        tenant_server.tenants.add(self.tenant)
        device = self._device(site=self.site, tenant=self.tenant)
        result = resolve_scope(device)
        self.assertEqual(result.server, tenant_server)

    def test_site_group_binding_beats_tenant(self):
        tenant_server = OpenNMSServer.objects.create(
            name="Tenant Server", url="https://tenant.example"
        )
        tenant_server.tenants.add(self.tenant)
        sg_server = OpenNMSServer.objects.create(
            name="Site Group Server", url="https://sg.example"
        )
        sg_server.site_groups.add(self.site_group)
        device = self._device(site=self.site, tenant=self.tenant)
        result = resolve_scope(device)
        self.assertEqual(result.server, sg_server)

    def test_site_binding_beats_site_group(self):
        sg_server = OpenNMSServer.objects.create(
            name="Site Group Server", url="https://sg.example"
        )
        sg_server.site_groups.add(self.site_group)
        site_server = OpenNMSServer.objects.create(
            name="Site Server", url="https://site.example"
        )
        site_server.sites.add(self.site)
        device = self._device(site=self.site)
        result = resolve_scope(device)
        self.assertEqual(result.server, site_server)

    def test_location_binding_beats_site(self):
        site_server = OpenNMSServer.objects.create(
            name="Site Server", url="https://site.example"
        )
        site_server.sites.add(self.site)
        location_server = OpenNMSServer.objects.create(
            name="Location Server", url="https://location.example"
        )
        location_server.locations.add(self.location)
        device = self._device(site=self.site, location=self.location)
        result = resolve_scope(device)
        self.assertEqual(result.server, location_server)

    def test_location_binding_cascades_to_nested_location(self):
        server = OpenNMSServer.objects.create(name="Rack Server", url="https://rack.example")
        server.locations.add(self.location)
        device = self._device(site=self.site, location=self.nested_location)
        result = resolve_scope(device)
        self.assertEqual(result.server, server)

    def test_nearest_location_wins_over_ancestor(self):
        ancestor_server = OpenNMSServer.objects.create(
            name="Rack Server", url="https://rack.example"
        )
        ancestor_server.locations.add(self.location)
        shelf_server = OpenNMSServer.objects.create(
            name="Shelf Server", url="https://shelf.example"
        )
        shelf_server.locations.add(self.nested_location)
        device = self._device(site=self.site, location=self.nested_location)
        result = resolve_scope(device)
        self.assertEqual(result.server, shelf_server)

    def test_site_group_exclusion_matches(self):
        exclusion = MonitoringExclusion.objects.create(description="No monitoring")
        exclusion.site_groups.add(self.site_group)
        device = self._device(site=self.site)
        result = resolve_scope(device)
        self.assertTrue(result.excluded)
        self.assertIsNone(result.server)

    def test_more_specific_server_binding_overrides_ancestor_exclusion(self):
        exclusion = MonitoringExclusion.objects.create(description="No monitoring")
        exclusion.site_groups.add(self.site_group)
        site_server = OpenNMSServer.objects.create(
            name="Site Server", url="https://site.example"
        )
        site_server.sites.add(self.site)
        device = self._device(site=self.site)
        result = resolve_scope(device)
        self.assertFalse(result.excluded)
        self.assertEqual(result.server, site_server)

    def test_exclusion_at_same_level_as_server_wins(self):
        # No ADR/spec case defines this; resolve_scope's documented behavior is
        # that exclusion is checked first at each candidate.
        exclusion = MonitoringExclusion.objects.create(description="No monitoring")
        exclusion.sites.add(self.site)
        server = OpenNMSServer.objects.create(name="Site Server", url="https://site.example")
        server.sites.add(self.site)
        device = self._device(site=self.site)
        result = resolve_scope(device)
        self.assertTrue(result.excluded)

    def test_device_with_no_location_only_checks_site_and_up(self):
        server = OpenNMSServer.objects.create(name="Site Server", url="https://site.example")
        server.sites.add(self.site)
        device = self._device(site=self.site)
        result = resolve_scope(device)
        self.assertEqual(result.server, server)


class VRFResolutionTest(TestCase):
    """Tests for ``resolve_vrf`` (ADR 0008) — same precedence engine as
    ``resolve_scope``, but starting from an explicit Site/Location rather
    than a Device/VM, and with no Default VRF fallback."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant_group = TenantGroup.objects.create(
            name="MSP Customers", slug="msp-customers"
        )
        cls.tenant = Tenant.objects.create(
            name="Acme Corp", slug="acme-corp", group=cls.tenant_group
        )
        cls.site_group = SiteGroup.objects.create(name="East Coast", slug="east-coast")
        cls.site = Site.objects.create(
            name="Raleigh", slug="raleigh", group=cls.site_group, tenant=cls.tenant
        )
        cls.location = Location.objects.create(
            name="Rack 1", slug="rack-1", site=cls.site
        )
        cls.nested_location = Location.objects.create(
            name="Shelf A", slug="shelf-a", site=cls.site, parent=cls.location
        )

    def test_no_bindings_resolves_to_none(self):
        self.assertIsNone(resolve_vrf(site=self.site))

    def test_site_binding_matches(self):
        vrf = VRF.objects.create(name="Site VRF")
        assignment = VRFAssignment.objects.create(vrf=vrf)
        assignment.sites.add(self.site)
        self.assertEqual(resolve_vrf(site=self.site), vrf)

    def test_location_binding_beats_site(self):
        site_vrf = VRF.objects.create(name="Site VRF")
        site_assignment = VRFAssignment.objects.create(vrf=site_vrf)
        site_assignment.sites.add(self.site)
        location_vrf = VRF.objects.create(name="Location VRF")
        location_assignment = VRFAssignment.objects.create(vrf=location_vrf)
        location_assignment.locations.add(self.location)
        self.assertEqual(
            resolve_vrf(site=self.site, location=self.location), location_vrf
        )

    def test_location_binding_cascades_to_nested_location(self):
        vrf = VRF.objects.create(name="Rack VRF")
        assignment = VRFAssignment.objects.create(vrf=vrf)
        assignment.locations.add(self.location)
        self.assertEqual(
            resolve_vrf(site=self.site, location=self.nested_location), vrf
        )

    def test_location_only_derives_site_and_tenant(self):
        # No explicit site is passed — resolve_vrf must derive it (and the
        # tenant) from the location itself.
        vrf = VRF.objects.create(name="Site VRF")
        assignment = VRFAssignment.objects.create(vrf=vrf)
        assignment.sites.add(self.site)
        self.assertEqual(resolve_vrf(location=self.location), vrf)

    def test_tenant_binding_matches_via_site_tenant(self):
        vrf = VRF.objects.create(name="Tenant VRF")
        assignment = VRFAssignment.objects.create(vrf=vrf)
        assignment.tenants.add(self.tenant)
        self.assertEqual(resolve_vrf(site=self.site), vrf)

    def test_location_tenant_overrides_site_tenant(self):
        other_tenant = Tenant.objects.create(name="Other Co", slug="other-co")
        location = Location.objects.create(
            name="Rack 2", slug="rack-2", site=self.site, tenant=other_tenant
        )
        site_tenant_vrf = VRF.objects.create(name="Site Tenant VRF")
        site_tenant_assignment = VRFAssignment.objects.create(vrf=site_tenant_vrf)
        site_tenant_assignment.tenants.add(self.tenant)
        other_tenant_vrf = VRF.objects.create(name="Other Tenant VRF")
        other_tenant_assignment = VRFAssignment.objects.create(vrf=other_tenant_vrf)
        other_tenant_assignment.tenants.add(other_tenant)
        self.assertEqual(
            resolve_vrf(site=self.site, location=location), other_tenant_vrf
        )

    def test_no_default_fallback_when_nothing_matches(self):
        # Unlike resolve_scope, there is no Default VRF concept (ADR 0008).
        vrf = VRF.objects.create(name="Unrelated VRF")
        other_site = Site.objects.create(name="Wilmington", slug="wilmington")
        assignment = VRFAssignment.objects.create(vrf=vrf)
        assignment.sites.add(other_site)
        self.assertIsNone(resolve_vrf(site=self.site))


class ScopeOptionsTest(TestCase):
    """Tests for ``scope_options`` (issue #19) — the reverse of ``resolve_scope``:
    which Scope objects, per level, resolve to a given Server."""

    @classmethod
    def setUpTestData(cls):
        cls.tenant_group = TenantGroup.objects.create(
            name="MSP Customers", slug="msp-customers"
        )
        cls.child_tenant_group = TenantGroup.objects.create(
            name="MSP Customers - EU", slug="msp-customers-eu", parent=cls.tenant_group
        )
        cls.unrelated_tenant_group = TenantGroup.objects.create(
            name="Other", slug="other-tg"
        )
        cls.tenant = Tenant.objects.create(name="Acme Corp", slug="acme-corp")
        cls.site_group = SiteGroup.objects.create(name="East Coast", slug="east-coast")
        cls.child_site_group = SiteGroup.objects.create(
            name="Raleigh Metro", slug="raleigh-metro", parent=cls.site_group
        )
        cls.unrelated_site_group = SiteGroup.objects.create(
            name="West Coast", slug="west-coast"
        )
        cls.site = Site.objects.create(name="Raleigh", slug="raleigh")
        cls.unrelated_site = Site.objects.create(name="Durham", slug="durham")
        cls.location = Location.objects.create(
            name="Rack 1", slug="rack-1", site=cls.site
        )
        cls.nested_location = Location.objects.create(
            name="Shelf A", slug="shelf-a", site=cls.site, parent=cls.location
        )
        cls.unrelated_location = Location.objects.create(
            name="Rack 2", slug="rack-2", site=cls.site
        )
        cls.server = OpenNMSServer.objects.create(
            name="Server", url="https://onms.example"
        )

    def test_no_server_is_unconstrained(self):
        self.assertIsNone(scope_options(None))

    def test_direct_site_binding_only_includes_that_site(self):
        self.server.sites.add(self.site)
        options = scope_options(self.server)
        self.assertEqual(set(options["sites"]), {self.site})

    def test_direct_tenant_binding_only_includes_that_tenant(self):
        self.server.tenants.add(self.tenant)
        options = scope_options(self.server)
        self.assertEqual(set(options["tenants"]), {self.tenant})

    def test_site_group_binding_cascades_to_descendant_group(self):
        self.server.site_groups.add(self.site_group)
        options = scope_options(self.server)
        self.assertEqual(
            set(options["site_groups"]), {self.site_group, self.child_site_group}
        )
        self.assertNotIn(self.unrelated_site_group, options["site_groups"])

    def test_tenant_group_binding_cascades_to_descendant_group(self):
        self.server.tenant_groups.add(self.tenant_group)
        options = scope_options(self.server)
        self.assertEqual(
            set(options["tenant_groups"]), {self.tenant_group, self.child_tenant_group}
        )
        self.assertNotIn(self.unrelated_tenant_group, options["tenant_groups"])

    def test_location_binding_cascades_to_nested_location(self):
        self.server.locations.add(self.location)
        options = scope_options(self.server)
        self.assertEqual(
            set(options["locations"]), {self.location, self.nested_location}
        )
        self.assertNotIn(self.unrelated_location, options["locations"])

    def test_site_group_binding_does_not_expand_to_member_sites(self):
        # Sites aren't a NestedGroupModel — resolve_scope only matches a site
        # itself at the "sites" level (a site under a bound site group instead
        # matches at the site_groups level), so scope_options must not offer
        # every site under a bound site group as a "sites" pick.
        self.server.site_groups.add(self.site_group)
        self.site.group = self.site_group
        self.site.save()
        options = scope_options(self.server)
        self.assertNotIn(self.site, options["sites"])

    def test_no_bindings_yields_empty_options(self):
        options = scope_options(self.server)
        fields = ("tenant_groups", "tenants", "site_groups", "sites", "locations")
        for field_name in fields:
            self.assertEqual(list(options[field_name]), [])
