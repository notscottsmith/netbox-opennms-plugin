# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
# Hand-authored (no Docker/host Python available to run makemigrations here) —
# ADR 0006. Verify with `make makemigrations` before merge.

import django.db.models.deletion
import netbox.models.deletion
import taggit.managers
import utilities.json
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('dcim', '0237_module_remove_local_context_data'),
        ('extras', '0140_imageattachment_image_size'),
        ('netbox_opennms', '0012_vrfassignment'),
    ]

    operations = [
        migrations.CreateModel(
            name='DiscoveryScan',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('created', models.DateTimeField(auto_now_add=True, null=True)),
                ('last_updated', models.DateTimeField(auto_now=True, null=True)),
                ('custom_field_data', models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder)),
                ('foreign_source', models.CharField(blank=True, editable=False, max_length=100, unique=True)),
                ('ip_range_begin', models.GenericIPAddressField()),
                ('ip_range_end', models.GenericIPAddressField()),
                ('retries', models.PositiveSmallIntegerField(default=1)),
                ('timeout', models.PositiveIntegerField(default=2000, help_text='Per-address timeout, in milliseconds.')),
                ('last_triggered', models.DateTimeField(blank=True, editable=False, null=True)),
                ('location', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='dcim.location')),
                ('server', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='discovery_scans', to='netbox_opennms.opennmsserver')),
                ('site', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='+', to='dcim.site')),
                ('tags', taggit.managers.TaggableManager(through='extras.TaggedItem', to='extras.Tag')),
            ],
            options={
                'verbose_name': 'discovery scan',
                'verbose_name_plural': 'discovery scans',
                'ordering': ('-created',),
            },
            bases=(netbox.models.deletion.DeleteMixin, models.Model),
        ),
    ]
