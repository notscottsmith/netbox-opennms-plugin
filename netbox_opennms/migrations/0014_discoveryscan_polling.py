# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
# Hand-authored (no Docker/host Python available to run makemigrations here) —
# issue #27. Verify with `make makemigrations` before merge.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_opennms', '0013_discoveryscan'),
    ]

    operations = [
        migrations.AddField(
            model_name='discoveryscan',
            name='latest_node_created',
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name='discoveryscan',
            name='settled_at',
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name='discoverednode',
            name='discovery_scan',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='discovered_nodes',
                to='netbox_opennms.discoveryscan',
            ),
        ),
    ]
