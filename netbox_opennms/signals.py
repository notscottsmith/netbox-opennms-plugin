# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Signal handlers (Epic 5).

A GenericForeignKey has no database-level cascade, so deleting a monitored
Device or VirtualMachine would otherwise leave an orphaned MonitoringOverride
pointing at a non-existent object. And an override's explicit services must sit
on one of its own interfaces (its management IP or an additional IP) — when an
IP leaves the override, its dangling services are pruned so stored intent
matches what is rendered (AD-15).

A deleted ``DiscoveryScan`` row is likewise the only record of which OpenNMS
Foreign Source needs deleting (issue #72) — ``CleanupDiscoveryScansJob``'s
retention-based cleanup (issue #29) can only clean up a scan it can still
query, so a scan deleted from NetBox before that job gets to it would
otherwise orphan its OpenNMS-side requisition forever.
"""

import logging

from dcim.models import Device
from django.contrib.contenttypes.models import ContentType
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver
from virtualization.models import VirtualMachine

from .models import (
    DiscoveryScan,
    MonitoredInterface,
    MonitoringOverride,
    override_ip_pks,
)

logger = logging.getLogger(__name__)


@receiver(post_delete, sender=Device)
@receiver(post_delete, sender=VirtualMachine)
def delete_orphaned_overrides(sender, instance, **kwargs):
    content_type = ContentType.objects.get_for_model(sender)
    MonitoringOverride.objects.filter(
        assigned_object_type=content_type,
        assigned_object_id=instance.pk,
    ).delete()


def _prune_orphaned_services(override):
    """Delete an override's services whose IP is no longer one of its IPs (AD-15)."""
    override.services.exclude(ip_address_id__in=override_ip_pks(override)).delete()


@receiver(post_save, sender=MonitoringOverride)
def prune_services_on_override_save(sender, instance, **kwargs):
    # Catches a management_ip change (its old-IP services orphan).
    _prune_orphaned_services(instance)


@receiver(post_save, sender=MonitoredInterface)
def prune_services_on_interface_save(sender, instance, **kwargs):
    # Editing an interface's ip_address orphans its services on the OLD IP (AD-15).
    # An in-place edit is an UPDATE (post_save), not a delete, so prune here too.
    _prune_orphaned_services(instance.override)


@receiver(post_delete, sender=MonitoredInterface)
def prune_services_on_interface_delete(sender, instance, **kwargs):
    # An additional interface deleted → its services on that IP orphan (AD-15).
    # Guard against the override already being gone (its services cascade with it).
    if MonitoringOverride.objects.filter(pk=instance.override_id).exists():
        _prune_orphaned_services(instance.override)


@receiver(post_delete, sender=DiscoveryScan)
def delete_opennms_requisition_on_scan_delete(sender, instance, **kwargs):
    """Clean up this scan's OpenNMS-side requisition immediately (issue #72).

    Mirrors ``CleanupDiscoveryScansJob``'s own ``delete_requisition`` call —
    but that job can only act on a scan it can still query, so once the
    NetBox row is gone nothing else can ever find this ``foreign_source``
    again to clean it up later. Skipped if cleanup already ran (retention
    window elapsed before the row was deleted) or the scan never got as far
    as having a ``foreign_source``. Best-effort like the job it mirrors: an
    OpenNMS outage is logged, not raised, so it can never block the NetBox
    delete itself.
    """
    from .client import OpenNMSClient, OpenNMSError

    if instance.cleaned_up_at or not instance.foreign_source:
        return
    try:
        with OpenNMSClient.from_server(instance.server) as client:
            client.delete_requisition(instance.foreign_source)
    except OpenNMSError as exc:
        logger.warning(
            "Discovery Scan %s deleted from NetBox but its OpenNMS "
            "requisition %r could not be cleaned up: %s",
            instance,
            instance.foreign_source,
            exc,
        )
