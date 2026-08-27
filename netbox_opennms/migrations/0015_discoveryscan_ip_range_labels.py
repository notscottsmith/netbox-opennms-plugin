# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
# Hand-authored (no Docker/host Python available to run makemigrations here) —
# verbose_name-only change. Verify with `make makemigrations` before merge.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_opennms', '0014_discoveryscan_polling'),
    ]

    operations = [
        migrations.AlterField(
            model_name='discoveryscan',
            name='ip_range_begin',
            field=models.GenericIPAddressField(verbose_name='IP range begin'),
        ),
        migrations.AlterField(
            model_name='discoveryscan',
            name='ip_range_end',
            field=models.GenericIPAddressField(verbose_name='IP range end'),
        ),
    ]
