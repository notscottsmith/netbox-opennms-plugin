# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Unit tests for ``parse_node_links`` (Node Links tab, issue #15)."""

from django.test import SimpleTestCase

from netbox_opennms.client import DiscoveredLink, parse_node_links


class ParseNodeLinksTest(SimpleTestCase):
    def test_none_payload_returns_empty(self):
        self.assertEqual(parse_node_links(None), [])

    def test_empty_payload_returns_empty(self):
        self.assertEqual(parse_node_links({}), [])

    def test_lldp_link(self):
        payload = {
            "lldpLinkNodes": [
                {
                    "lldpLocalPort": "GigabitEthernet0/1",
                    "lldpRemChassisId": "aa:bb:cc:dd:ee:ff",
                    "ldpRemPort": "GigabitEthernet0/2",
                    "lldpRemChassisIdUrl": "element/linkednode.jsp?node=42",
                }
            ]
        }
        self.assertEqual(
            parse_node_links(payload),
            [
                DiscoveredLink(
                    protocol="LLDP",
                    local_port="GigabitEthernet0/1",
                    remote_device="aa:bb:cc:dd:ee:ff",
                    remote_port="GigabitEthernet0/2",
                    remote_node_id=42,
                )
            ],
        )

    def test_lldp_link_without_remote_url_has_no_node_id(self):
        payload = {
            "lldpLinkNodes": [
                {
                    "lldpLocalPort": "Gi0/1",
                    "lldpRemChassisId": "aa:bb:cc:dd:ee:ff",
                    "ldpRemPort": "Gi0/2",
                }
            ]
        }
        self.assertIsNone(parse_node_links(payload)[0].remote_node_id)

    def test_cdp_link(self):
        payload = {
            "cdpLinkNodes": [
                {
                    "cdpLocalPort": "Fa0/1",
                    "cdpCacheDevice": "switch-2",
                    "cdpCacheDevicePort": "Fa0/24",
                }
            ]
        }
        self.assertEqual(
            parse_node_links(payload),
            [
                DiscoveredLink(
                    protocol="CDP",
                    local_port="Fa0/1",
                    remote_device="switch-2",
                    remote_port="Fa0/24",
                )
            ],
        )

    def test_ospf_link(self):
        payload = {
            "ospfLinkNodes": [
                {
                    "ospfLocalPort": "10",
                    "ospfRemRouterId": "10.0.0.2",
                    "ospfRemPort": "12",
                }
            ]
        }
        self.assertEqual(
            parse_node_links(payload),
            [
                DiscoveredLink(
                    protocol="OSPF",
                    local_port="10",
                    remote_device="10.0.0.2",
                    remote_port="12",
                )
            ],
        )

    def test_isis_link(self):
        payload = {
            "isisLinkNodes": [
                {
                    "isisCircIfIndex": "5",
                    "isisISAdjNeighSysID": "0000.0000.0002",
                    "isisISAdjNeighPort": "6",
                }
            ]
        }
        self.assertEqual(
            parse_node_links(payload),
            [
                DiscoveredLink(
                    protocol="IS-IS",
                    local_port="5",
                    remote_device="0000.0000.0002",
                    remote_port="6",
                )
            ],
        )

    def test_bridge_link_expands_one_record_per_remote(self):
        payload = {
            "bridgeLinkNodes": [
                {
                    "bridgeLocalPort": "1",
                    "BridgeLinkRemoteNodes": [
                        {
                            "bridgeRemote": "switch-a",
                            "bridgeRemotePort": "2",
                            "bridgeRemoteUrl": "element/linkednode.jsp?node=7",
                        },
                        {"bridgeRemote": "switch-b", "bridgeRemotePort": "3"},
                    ],
                }
            ]
        }
        self.assertEqual(
            parse_node_links(payload),
            [
                DiscoveredLink(
                    protocol="Bridge",
                    local_port="1",
                    remote_device="switch-a",
                    remote_port="2",
                    remote_node_id=7,
                ),
                DiscoveredLink(
                    protocol="Bridge",
                    local_port="1",
                    remote_device="switch-b",
                    remote_port="3",
                ),
            ],
        )

    def test_missing_fields_default_to_empty_string(self):
        payload = {"lldpLinkNodes": [{}]}
        self.assertEqual(
            parse_node_links(payload),
            [
                DiscoveredLink(
                    protocol="LLDP", local_port="", remote_device="", remote_port=""
                )
            ],
        )

    def test_non_dict_entries_are_ignored(self):
        payload = {"lldpLinkNodes": ["not-a-dict", None, 42]}
        self.assertEqual(parse_node_links(payload), [])

    def test_bridge_link_with_no_remotes_yields_nothing(self):
        payload = {"bridgeLinkNodes": [{"bridgeLocalPort": "1"}]}
        self.assertEqual(parse_node_links(payload), [])

    def test_malformed_remote_url_has_no_node_id(self):
        payload = {
            "lldpLinkNodes": [
                {"lldpRemChassisIdUrl": "element/linkednode.jsp?node=not-a-number"}
            ]
        }
        self.assertIsNone(parse_node_links(payload)[0].remote_node_id)
