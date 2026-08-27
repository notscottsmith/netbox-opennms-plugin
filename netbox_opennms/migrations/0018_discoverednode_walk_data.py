# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
# Hand-authored (no Docker/host Python available to run makemigrations here) —
# verify with `make makemigrations` before merge.
#
# Adds DiscoveredNode.node_detail/ip_interfaces/services_by_ip (the walked
# OpenNMS payload, persisted at scan time per ADR 0007), completeness_gaps
# (issue #28), and walked_at (gates re-walking and whether review reads the
# persisted snapshot or falls back to a live fetch).

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_opennms', '0017_opennmsserver_available_locations'),
    ]

    operations = [
        migrations.AddField(
            model_name='discoverednode',
            name='node_detail',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='discoverednode',
            name='ip_interfaces',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name='discoverednode',
            name='services_by_ip',
            field=models.JSONField(blank=True, default=dict),
        ),
        migrations.AddField(
            model_name='discoverednode',
            name='completeness_gaps',
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name='discoverednode',
            name='walked_at',
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
    ]
