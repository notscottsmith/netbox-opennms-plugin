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
from tenancy.models import Tenant, TenantGroup

from netbox_opennms.models import MonitoringExclusion, OpenNMSServer
from netbox_opennms.scope import resolve_scope


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
