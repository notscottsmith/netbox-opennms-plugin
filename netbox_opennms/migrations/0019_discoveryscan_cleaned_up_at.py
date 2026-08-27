# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
# Hand-authored (no Docker/host Python available to run makemigrations here) —
# verify with `make makemigrations` before merge.
#
# Adds DiscoveryScan.cleaned_up_at (issue #29, ADR 0006) — marks a settled
# scan whose OpenNMS-side requisition has already been deleted by
# CleanupDiscoveryScansJob, so cleanup is never repeated for the same scan.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_opennms', '0018_discoverednode_walk_data'),
    ]

    operations = [
        migrations.AddField(
            model_name='discoveryscan',
            name='cleaned_up_at',
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
    ]
