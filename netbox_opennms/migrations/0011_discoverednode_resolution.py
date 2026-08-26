# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
# Hand-authored (no Docker/host Python available to run makemigrations here) —
# issue #8. Verify with `make makemigrations` before merge.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_opennms', '0010_discoverednode'),
    ]

    operations = [
        migrations.AddField(
            model_name='discoverednode',
            name='resolution',
            field=models.CharField(
                choices=[('scanned', 'Scanned'), ('linked', 'Manually linked')],
                default='scanned',
                max_length=8,
            ),
        ),
    ]
