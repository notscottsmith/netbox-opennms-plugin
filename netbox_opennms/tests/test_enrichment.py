# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Tests for asset/metadata enrichment (RD-2/RD-3): resolver, models, resolution."""

from types import SimpleNamespace

from dcim.models import (
    Device,
    DeviceRole,
    DeviceType,
    Interface,
    Manufacturer,
    Site,
)
from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase
from ipam.models import IPAddress

from netbox_opennms.enrichment import _parse_physical_address, resolve_source
from netbox_opennms.membership import resolve_node
from netbox_opennms.models import (
    AssetMapping,
    MetadataContext,
    MetadataEntry,
    Requisition,
)


class ResolveSourceTest(SimpleTestCase):
    def _obj(self, **kw):
        kw.setdefault("custom_field_data", {})
        return SimpleNamespace(**kw)

    def test_curated_attribute(self):
        self.assertEqual(resolve_source(self._obj(serial="SN-9"), "serial"), "SN-9")

    def test_related_name(self):
        obj = self._obj(site=SimpleNamespace(name="Raleigh"))
        self.assertEqual(resolve_source(obj, "site"), "Raleigh")

    def test_absent_attribute_is_none(self):
        self.assertIsNone(resolve_source(self._obj(), "serial"))

    def test_empty_string_is_none(self):
        self.assertIsNone(resolve_source(self._obj(description=""), "description"))

    def test_custom_field(self):
        obj = self._obj(custom_field_data={"owner": "neteng"})
        self.assertEqual(resolve_source(obj, "cf_owner"), "neteng")

    def test_unknown_source_is_none(self):
        self.assertIsNone(resolve_source(self._obj(), "bogus"))


class ParsePhysicalAddressTest(SimpleTestCase):
    """_parse_physical_address (Part A): best-effort split of Site's freeform
    physical_address into OpenNMS's discrete address1/address2/city/state/zip."""

    def _site(self, physical_address):
        return SimpleNamespace(physical_address=physical_address)

    def test_well_formed_two_line_address(self):
        result = _parse_physical_address(
            self._site("123 Main St\nRaleigh, NC 27601")
        )
        self.assertEqual(
            result,
            {
                "address1": "123 Main St",
                "address2": "",
                "city": "Raleigh",
                "state": "NC",
                "zip": "27601",
            },
        )

    def test_multi_line_address_before_city_state_zip(self):
        result = _parse_physical_address(
            self._site("123 Main St\nSuite 400\nRaleigh, NC 27601")
        )
        self.assertEqual(result["address1"], "123 Main St")
        self.assertEqual(result["address2"], "Suite 400")
        self.assertEqual(result["city"], "Raleigh")
        self.assertEqual(result["state"], "NC")
        self.assertEqual(result["zip"], "27601")

    def test_no_city_state_zip_line_leaves_them_blank(self):
        result = _parse_physical_address(self._site("123 Main St\nBuilding 2"))
        self.assertEqual(result["address1"], "123 Main St")
        self.assertEqual(result["address2"], "Building 2")
        self.assertEqual(result["city"], "")
        self.assertEqual(result["state"], "")
        self.assertEqual(result["zip"], "")

    def test_single_line_address_has_no_city_state_zip_match(self):
        result = _parse_physical_address(self._site("Raleigh, NC 27601"))
        self.assertEqual(result["address1"], "Raleigh, NC 27601")
        self.assertEqual(result["address2"], "")
        self.assertEqual(result["city"], "")

    def test_blank_address_returns_all_blank(self):
        result = _parse_physical_address(self._site(""))
        self.assertEqual(
            result,
            {"address1": "", "address2": "", "city": "", "state": "", "zip": ""},
        )

    def test_none_site_returns_all_blank(self):
        result = _parse_physical_address(None)
        self.assertEqual(
            result,
            {"address1": "", "address2": "", "city": "", "state": "", "zip": ""},
        )

    def test_blank_lines_are_ignored(self):
        result = _parse_physical_address(
            self._site("123 Main St\n\n  \nRaleigh, NC 27601")
        )
        self.assertEqual(result["address1"], "123 Main St")
        self.assertEqual(result["city"], "Raleigh")


class ResolveSourceSiteFieldsTest(SimpleTestCase):
    """The seven new CURATED site_* resolvers (Part A)."""

    def _obj(self, site=None):
        return SimpleNamespace(custom_field_data={}, site=site)

    def test_site_address1(self):
        site = SimpleNamespace(physical_address="123 Main St\nRaleigh, NC 27601")
        self.assertEqual(
            resolve_source(self._obj(site=site), "site_address1"), "123 Main St"
        )

    def test_site_address2(self):
        site = SimpleNamespace(
            physical_address="123 Main St\nSuite 400\nRaleigh, NC 27601"
        )
        self.assertEqual(
            resolve_source(self._obj(site=site), "site_address2"), "Suite 400"
        )

    def test_site_city(self):
        site = SimpleNamespace(physical_address="123 Main St\nRaleigh, NC 27601")
        self.assertEqual(resolve_source(self._obj(site=site), "site_city"), "Raleigh")

    def test_site_state(self):
        site = SimpleNamespace(physical_address="123 Main St\nRaleigh, NC 27601")
        self.assertEqual(resolve_source(self._obj(site=site), "site_state"), "NC")

    def test_site_zip(self):
        site = SimpleNamespace(physical_address="123 Main St\nRaleigh, NC 27601")
        self.assertEqual(resolve_source(self._obj(site=site), "site_zip"), "27601")

    def test_site_latitude(self):
        site = SimpleNamespace(latitude=35.7796)
        self.assertEqual(
            resolve_source(self._obj(site=site), "site_latitude"), "35.7796"
        )

    def test_site_longitude(self):
        site = SimpleNamespace(longitude=-78.6382)
        self.assertEqual(
            resolve_source(self._obj(site=site), "site_longitude"), "-78.6382"
        )

    def test_no_site_yields_none_for_all_site_sources(self):
        obj = self._obj(site=None)
        for source in (
            "site_address1",
            "site_address2",
            "site_city",
            "site_state",
            "site_zip",
            "site_latitude",
            "site_longitude",
        ):
            self.assertIsNone(resolve_source(obj, source))

    def test_blank_physical_address_yields_none_for_address_fields(self):
        site = SimpleNamespace(physical_address="")
        obj = self._obj(site=site)
        for source in ("site_address1", "site_address2", "site_city", "site_state", "site_zip"):
            self.assertIsNone(resolve_source(obj, source))


class AssetMappingValidationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.req = Requisition.objects.create(
            name="asset-req", filter_params={"role": ["x"]}
        )

    def test_known_asset_field_ok(self):
        AssetMapping(
            requisition=self.req, netbox_source="serial", asset_field="serialNumber"
        ).clean()  # no raise

    def test_unknown_asset_field_rejected(self):
        mapping = AssetMapping(
            requisition=self.req, netbox_source="serial", asset_field="bogusField"
        )
        with self.assertRaises(ValidationError):
            mapping.clean()


class MetadataEntryValidationTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.req = Requisition.objects.create(
            name="meta-req", filter_params={"role": ["x"]}
        )
        MetadataContext.objects.create(name="X-netbox")

    def _entry(self, **kw):
        kw.setdefault("requisition", self.req)
        kw.setdefault("scope", "node")
        kw.setdefault("key", "k")
        return MetadataEntry(**kw)

    def test_requisition_context_with_literal_ok(self):
        self._entry(
            context="requisition", scope="requisition", literal_value="v"
        ).clean()

    def test_custom_context_must_be_x_prefixed(self):
        with self.assertRaises(ValidationError):
            self._entry(context="custom", literal_value="v").clean()

    def test_x_prefixed_context_ok(self):
        self._entry(context="X-netbox", literal_value="v").clean()

    def test_requires_a_value(self):
        with self.assertRaises(ValidationError):
            self._entry(context="requisition").clean()

    def test_value_source_and_literal_are_exclusive(self):
        with self.assertRaises(ValidationError):
            self._entry(
                context="requisition", value_source="name", literal_value="v"
            ).clean()


class EnrichmentResolveTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        site = Site.objects.create(name="R", slug="r")
        role = DeviceRole.objects.create(name="Rtr", slug="rtr")
        mfr = Manufacturer.objects.create(name="Acme", slug="acme")
        dt = DeviceType.objects.create(manufacturer=mfr, model="M1", slug="m1")
        cls.device = Device.objects.create(
            name="rtr-1", device_type=dt, role=role, site=site, serial="SN-42"
        )
        iface = Interface.objects.create(device=cls.device, name="eth0", type="virtual")
        ip = IPAddress.objects.create(address="10.0.0.1/24", assigned_object=iface)
        cls.device.primary_ip4 = ip
        cls.device.save()
        cls.req = Requisition.objects.create(
            name="enrich", filter_params={"role": ["rtr"]}, services=["ICMP"]
        )
        MetadataContext.objects.create(name="X-netbox")
        AssetMapping.objects.create(
            requisition=cls.req, netbox_source="serial", asset_field="serialNumber"
        )
        MetadataEntry.objects.create(
            requisition=cls.req,
            scope="node",
            context="requisition",
            key="owner",
            literal_value="neteng",
        )
        MetadataEntry.objects.create(
            requisition=cls.req,
            scope="interface",
            context="X-netbox",
            key="src",
            value_source="name",
        )

    def test_resolve_node_attaches_enrichment(self):
        node, _ = resolve_node(self.device, self.req, None)
        self.assertIn(("serialNumber", "SN-42"), node.assets)
        self.assertIn(("requisition", "owner", "neteng"), node.node_metadata)
        self.assertIn(("X-netbox", "src", "rtr-1"), node.interface_metadata)

    def test_unresolved_source_is_omitted(self):
        # A mapping whose source doesn't resolve for this member yields no <asset>.
        AssetMapping.objects.create(
            requisition=self.req, netbox_source="asset_tag", asset_field="assetNumber"
        )
        node, _ = resolve_node(self.device, self.req, None)
        self.assertNotIn("assetNumber", [name for name, _ in node.assets])
