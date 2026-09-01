# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
# Hand-authored (no Docker/host Python available to run makemigrations here) —
# verify with `make makemigrations` before merge.
#
# Part B2: MetadataPullMapping (RD-3 pull-back) — maps an OpenNMS metadata
# key observed on a Requisition's live nodes back onto a NetBox field
# (netbox_target deliberately narrow — see NetBoxTargetChoices). Applying a
# mapping is a distinct, explicit operator action (pull.apply_pull_mappings),
# never part of the normal render/sync job.

import netbox.models.deletion
import taggit.managers
import utilities.json
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_opennms', '0023_category'),
    ]

    operations = [
        migrations.CreateModel(
            name='MetadataPullMapping',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('created', models.DateTimeField(auto_now_add=True, null=True)),
                ('last_updated', models.DateTimeField(auto_now=True, null=True)),
                ('custom_field_data', models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder)),
                ('context', models.CharField(max_length=64)),
                ('key', models.CharField(max_length=100)),
                ('netbox_target', models.CharField(choices=[('description', 'Description'), ('comments', 'Comments')], max_length=100)),
                ('requisition', models.ForeignKey(on_delete=models.deletion.CASCADE, related_name='pull_mappings', to='netbox_opennms.requisition')),
                ('tags', taggit.managers.TaggableManager(through='extras.TaggedItem', to='extras.Tag')),
            ],
            options={
                'verbose_name': 'metadata pull mapping',
                'verbose_name_plural': 'metadata pull mappings',
                'ordering': ('requisition', 'context', 'key'),
            },
            bases=(netbox.models.deletion.DeleteMixin, models.Model),
        ),
        migrations.AddConstraint(
            model_name='metadatapullmapping',
            constraint=models.UniqueConstraint(fields=('requisition', 'context', 'key'), name='netbox_opennms_metadatapullmapping_unique_pull'),
        ),
    ]
