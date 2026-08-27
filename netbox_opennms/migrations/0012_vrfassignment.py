# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
# Hand-authored (no Docker/host Python available to run makemigrations here) —
# ADR 0008. Verify with `make makemigrations` before merge.

import django.db.models.deletion
import netbox.models.deletion
import taggit.managers
import utilities.json
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('dcim', '0237_module_remove_local_context_data'),
        ('tenancy', '0024_default_ordering_indexes'),
        ('ipam', '0001_initial'),
        ('extras', '0140_imageattachment_image_size'),
        ('netbox_opennms', '0011_discoverednode_resolution'),
    ]

    operations = [
        migrations.CreateModel(
            name='VRFAssignment',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('created', models.DateTimeField(auto_now_add=True, null=True)),
                ('last_updated', models.DateTimeField(auto_now=True, null=True)),
                ('custom_field_data', models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder)),
                ('description', models.CharField(blank=True, max_length=200)),
                ('locations', models.ManyToManyField(blank=True, related_name='+', to='dcim.location')),
                ('site_groups', models.ManyToManyField(blank=True, related_name='+', to='dcim.sitegroup')),
                ('sites', models.ManyToManyField(blank=True, related_name='+', to='dcim.site')),
                ('tags', taggit.managers.TaggableManager(through='extras.TaggedItem', to='extras.Tag')),
                ('tenant_groups', models.ManyToManyField(blank=True, related_name='+', to='tenancy.tenantgroup')),
                ('tenants', models.ManyToManyField(blank=True, related_name='+', to='tenancy.tenant')),
                ('vrf', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='+', to='ipam.vrf')),
            ],
            options={
                'verbose_name': 'VRF assignment',
                'verbose_name_plural': 'VRF assignments',
                'ordering': ('pk',),
            },
            bases=(netbox.models.deletion.DeleteMixin, models.Model),
        ),
    ]
