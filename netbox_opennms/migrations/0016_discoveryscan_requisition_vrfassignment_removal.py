# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
# Hand-authored (no Docker/host Python available to run makemigrations here) —
# ADR 0009 (supersedes ADR 0008). Verify with `make makemigrations` before merge.
#
# Drops VRFAssignment entirely (VRF resolution now reads NetBox's own
# ipam.Prefix scope+vrf natively, see scope.resolve_vrf) and reshapes
# DiscoveryScan: drops the `site` FK (there is no NetBox "site" on an OpenNMS
# discovery request), converts `location` from a dcim.Location FK to a plain
# CharField (OpenNMS's own Monitoring Location name, not a NetBox object —
# RemoveField+AddField rather than AlterField since the column types are not
# convertible in place), and adds a `requisition` FK (the anchor VRF
# resolution now uses).

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_opennms', '0015_discoveryscan_ip_range_labels'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='discoveryscan',
            name='site',
        ),
        migrations.RemoveField(
            model_name='discoveryscan',
            name='location',
        ),
        migrations.AddField(
            model_name='discoveryscan',
            name='location',
            field=models.CharField(
                blank=True,
                help_text='The OpenNMS Monitoring Location (not a NetBox Location).',
                max_length=100,
            ),
        ),
        migrations.AddField(
            model_name='discoveryscan',
            name='requisition',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='discovery_scans',
                to='netbox_opennms.requisition',
                help_text="Discovered nodes are imported against this "
                "Requisition's scope, which is also how their VRF is resolved.",
            ),
        ),
        migrations.AlterField(
            model_name='discoveryscan',
            name='ip_range_begin',
            field=models.GenericIPAddressField(verbose_name='IP Range Begin'),
        ),
        migrations.AlterField(
            model_name='discoveryscan',
            name='ip_range_end',
            field=models.GenericIPAddressField(verbose_name='IP Range End'),
        ),
        migrations.RemoveField(
            model_name='vrfassignment',
            name='locations',
        ),
        migrations.RemoveField(
            model_name='vrfassignment',
            name='site_groups',
        ),
        migrations.RemoveField(
            model_name='vrfassignment',
            name='sites',
        ),
        migrations.RemoveField(
            model_name='vrfassignment',
            name='tags',
        ),
        migrations.RemoveField(
            model_name='vrfassignment',
            name='tenant_groups',
        ),
        migrations.RemoveField(
            model_name='vrfassignment',
            name='tenants',
        ),
        migrations.RemoveField(
            model_name='vrfassignment',
            name='vrf',
        ),
        migrations.DeleteModel(
            name='VRFAssignment',
        ),
    ]
