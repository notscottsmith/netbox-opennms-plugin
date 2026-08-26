# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
# Hand-authored (no Docker/host Python available to run makemigrations here) —
# issue #7. Verify with `make makemigrations` before merge.

import django.db.models.deletion
import netbox.models.deletion
import taggit.managers
import utilities.json
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('contenttypes', '0002_remove_content_type_name'),
        ('netbox_opennms', '0009_opennmsserver_health_check'),
    ]

    operations = [
        migrations.CreateModel(
            name='DiscoveredNode',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('created', models.DateTimeField(auto_now_add=True, null=True)),
                ('last_updated', models.DateTimeField(auto_now=True, null=True)),
                ('custom_field_data', models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder)),
                ('opennms_node_id', models.PositiveIntegerField()),
                ('label', models.CharField(max_length=255)),
                ('foreign_source', models.CharField(blank=True, default='', max_length=100)),
                ('foreign_id', models.CharField(blank=True, default='', max_length=100)),
                ('location', models.CharField(blank=True, default='', max_length=255)),
                ('verdict', models.CharField(choices=[('green', 'Matches NetBox'), ('orange', 'Differs from NetBox'), ('red', 'Missing from NetBox')], max_length=6)),
                ('diff_detail', models.JSONField(blank=True, default=list)),
                ('matched_object_id', models.PositiveBigIntegerField(blank=True, null=True)),
                ('last_scanned', models.DateTimeField(auto_now=True)),
                ('matched_object_type', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='contenttypes.contenttype')),
                ('server', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='discovered_nodes', to='netbox_opennms.opennmsserver')),
                ('tags', taggit.managers.TaggableManager(through='extras.TaggedItem', to='extras.Tag')),
            ],
            options={
                'verbose_name': 'discovered node',
                'verbose_name_plural': 'discovered nodes',
                'ordering': ('server', 'label'),
            },
            bases=(netbox.models.deletion.DeleteMixin, models.Model),
        ),
        migrations.AddConstraint(
            model_name='discoverednode',
            constraint=models.UniqueConstraint(fields=('server', 'opennms_node_id'), name='netbox_opennms_discoverednode_unique_server_node'),
        ),
    ]
