# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
# Hand-authored (no Docker/host Python available to run makemigrations here) —
# verify with `make makemigrations` before merge.
#
# Part C: OpenNMS monitoring Categories (distinct from asset-data
# "category"/role) as a first-class, freely-synced/created model — no
# builtin/protected-rename machinery, unlike MetadataContext/MetadataKey.
# Selectable as Requisition-level defaults and per-object
# (MonitoringOverride) additions, unioned at render time and emitted
# directly as <category name=.../> — additive alongside the pre-existing
# set-node-category policy preset, not a replacement for it.

import netbox.models.deletion
import taggit.managers
import utilities.json
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_opennms', '0022_metadataentry_requisition_scope'),
    ]

    operations = [
        migrations.CreateModel(
            name='Category',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('created', models.DateTimeField(auto_now_add=True, null=True)),
                ('last_updated', models.DateTimeField(auto_now=True, null=True)),
                ('custom_field_data', models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder)),
                ('name', models.CharField(max_length=100, unique=True)),
                ('description', models.CharField(blank=True, max_length=200)),
                ('tags', taggit.managers.TaggableManager(through='extras.TaggedItem', to='extras.Tag')),
            ],
            options={
                'verbose_name': 'category',
                'verbose_name_plural': 'categories',
                'ordering': ('name',),
            },
            bases=(netbox.models.deletion.DeleteMixin, models.Model),
        ),
        migrations.AddField(
            model_name='requisition',
            name='default_categories',
            field=models.ManyToManyField(blank=True, related_name='requisitions', to='netbox_opennms.category'),
        ),
        migrations.AddField(
            model_name='monitoringoverride',
            name='categories',
            field=models.ManyToManyField(blank=True, related_name='overrides', to='netbox_opennms.category'),
        ),
    ]
