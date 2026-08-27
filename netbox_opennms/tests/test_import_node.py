# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Tests for ``import_node.compute_completeness_gaps`` (issue #28).

Pure-function tests, hand-built ``ImportProposal``/``FieldProposal``/
``InterfaceProposal`` inputs — follows ``test_scan.py``'s ``ReconcileTest``
convention.
"""

from django.test import SimpleTestCase

from netbox_opennms.import_node import (
    FieldProposal,
    ImportProposal,
    InterfaceProposal,
    compute_completeness_gaps,
)


def _complete_proposal():
    detected = FieldProposal(value=object(), detected="x", guessed=True)
    return ImportProposal(
        label="rtr-1",
        tenant=FieldProposal(),  # tenant is never a gap — left undetected here
        site=detected,
        role=detected,
        manufacturer=detected,
        platform=detected,
    )


class ComputeCompletenessGapsTest(SimpleTestCase):
    def test_no_gaps_when_everything_detected_and_interfaces_present(self):
        gaps = compute_completeness_gaps(
            _complete_proposal(), [InterfaceProposal(ip_address="10.0.0.5")]
        )
        self.assertEqual(gaps, [])

    def test_undetected_role_is_a_gap(self):
        proposal = _complete_proposal()
        proposal.role = FieldProposal()
        gaps = compute_completeness_gaps(
            proposal, [InterfaceProposal(ip_address="10.0.0.5")]
        )
        self.assertIn("role", gaps)

    def test_undetected_site_is_a_gap(self):
        proposal = _complete_proposal()
        proposal.site = FieldProposal()
        gaps = compute_completeness_gaps(
            proposal, [InterfaceProposal(ip_address="10.0.0.5")]
        )
        self.assertIn("site", gaps)

    def test_undetected_manufacturer_is_a_gap(self):
        proposal = _complete_proposal()
        proposal.manufacturer = FieldProposal()
        gaps = compute_completeness_gaps(
            proposal, [InterfaceProposal(ip_address="10.0.0.5")]
        )
        self.assertIn("manufacturer", gaps)

    def test_undetected_platform_is_a_gap(self):
        proposal = _complete_proposal()
        proposal.platform = FieldProposal()
        gaps = compute_completeness_gaps(
            proposal, [InterfaceProposal(ip_address="10.0.0.5")]
        )
        self.assertIn("platform", gaps)

    def test_undetected_tenant_is_never_a_gap(self):
        gaps = compute_completeness_gaps(
            _complete_proposal(), [InterfaceProposal(ip_address="10.0.0.5")]
        )
        self.assertNotIn("tenant", gaps)

    def test_no_interfaces_is_its_own_gap(self):
        gaps = compute_completeness_gaps(_complete_proposal(), [])
        self.assertIn("no IP interfaces (SNMP data may be unavailable)", gaps)

    def test_low_confidence_guess_is_not_a_gap(self):
        # detected is set (OpenNMS's data covers the field) even though the
        # value couldn't be resolved to a NetBox object — not a gap.
        proposal = _complete_proposal()
        proposal.role = FieldProposal(value=None, detected="Router", guessed=False)
        gaps = compute_completeness_gaps(
            proposal, [InterfaceProposal(ip_address="10.0.0.5")]
        )
        self.assertNotIn("role", gaps)
