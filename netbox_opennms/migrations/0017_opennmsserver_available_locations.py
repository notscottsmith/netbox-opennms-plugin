# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
# Hand-authored (no Docker/host Python available to run makemigrations here) —
# verify with `make makemigrations` before merge.
#
# Adds OpenNMSServer.available_locations — the Monitoring Locations OpenNMS
# reported on the last successful connection test, cached so the Requisition
# and Server forms can offer a real dropdown instead of a free-text field.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_opennms', '0016_discoveryscan_requisition_vrfassignment_removal'),
    ]

    operations = [
        migrations.AddField(
            model_name='opennmsserver',
            name='available_locations',
            field=models.JSONField(blank=True, default=list),
        ),
    ]
