# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_opennms', '0008_migrate_legacy_opennms_config'),
    ]

    operations = [
        migrations.AddField(
            model_name='opennmsserver',
            name='last_check_status',
            field=models.CharField(
                choices=[('unknown', 'Unknown'), ('ok', 'OK'), ('failed', 'Failed')],
                default='unknown',
                max_length=10,
            ),
        ),
        migrations.AddField(
            model_name='opennmsserver',
            name='last_check_time',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name='opennmsserver',
            name='last_check_message',
            field=models.CharField(blank=True, default='', max_length=500),
        ),
    ]
