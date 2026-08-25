# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
# Hand-authored (no Docker/host Python available to run makemigrations here) —
# ADR 0002/0003/0005. Verify with `make makemigrations` before merge.

import django.db.models.deletion
import netbox.models.deletion
import netbox_opennms.fields
import taggit.managers
import utilities.json
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('dcim', '0237_module_remove_local_context_data'),
        ('tenancy', '0024_default_ordering_indexes'),
        ('extras', '0140_imageattachment_image_size'),
        ('netbox_opennms', '0006_assetmapping_metadataentry'),
    ]

    operations = [
        migrations.CreateModel(
            name='OpenNMSServer',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('created', models.DateTimeField(auto_now_add=True, null=True)),
                ('last_updated', models.DateTimeField(auto_now=True, null=True)),
                ('custom_field_data', models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder)),
                ('name', models.CharField(max_length=100, unique=True)),
                ('url', models.CharField(max_length=255)),
                ('username', netbox_opennms.fields.EncryptedTextField()),
                ('password', netbox_opennms.fields.EncryptedTextField()),
                ('headers', netbox_opennms.fields.EncryptedJSONField(blank=True, default=dict)),
                ('default_location', models.CharField(blank=True, default='', max_length=255)),
                ('is_default', models.BooleanField(default=False, help_text='Fallback Server used when no Scope binding matches an object.')),
                ('locations', models.ManyToManyField(blank=True, related_name='+', to='dcim.location')),
                ('site_groups', models.ManyToManyField(blank=True, related_name='+', to='dcim.sitegroup')),
                ('sites', models.ManyToManyField(blank=True, related_name='+', to='dcim.site')),
                ('tags', taggit.managers.TaggableManager(through='extras.TaggedItem', to='extras.Tag')),
                ('tenant_groups', models.ManyToManyField(blank=True, related_name='+', to='tenancy.tenantgroup')),
                ('tenants', models.ManyToManyField(blank=True, related_name='+', to='tenancy.tenant')),
            ],
            options={
                'verbose_name': 'OpenNMS server',
                'verbose_name_plural': 'OpenNMS servers',
                'ordering': ('name',),
            },
            bases=(netbox.models.deletion.DeleteMixin, models.Model),
        ),
        migrations.CreateModel(
            name='MonitoringExclusion',
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
            ],
            options={
                'verbose_name': 'monitoring exclusion',
                'verbose_name_plural': 'monitoring exclusions',
                'ordering': ('pk',),
            },
            bases=(netbox.models.deletion.DeleteMixin, models.Model),
        ),
        migrations.AddField(
            model_name='deployedforeignsource',
            name='server',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='netbox_opennms.opennmsserver'),
        ),
    ]
