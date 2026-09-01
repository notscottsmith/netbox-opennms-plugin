# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
# Hand-authored (no Docker/host Python available to run makemigrations here) —
# verify with `make makemigrations` before merge.
#
# RD-3 bugfix: MetadataEntry.scope gains a REQUISITION choice, and
# MetadataEntry.clean() now requires scope == context for the four base
# contexts (node/interface/service/requisition) — context IS placement for
# these, closing the bug where a context="requisition" entry (scope
# defaulting to "node") rendered once per matching node instead of once at
# the requisition root. Existing rows are reclassified to match the new
# invariant; "node"/"pattern"/custom contexts are unaffected (node already
# matches the default, pattern/custom stay freeform).

from django.db import migrations, models
from django.db.models import F


def reclassify_scope(apps, schema_editor):
    MetadataEntry = apps.get_model("netbox_opennms", "MetadataEntry")
    MetadataEntry.objects.filter(
        context__in=("requisition", "interface", "service")
    ).update(scope=F("context"))


def noop(apps, schema_editor):
    # Pre-1.0: rollback is a DB restore, not a reversible data migration (L1).
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_opennms', '0021_metadatakey'),
    ]

    operations = [
        migrations.AlterField(
            model_name='metadataentry',
            name='scope',
            field=models.CharField(
                choices=[
                    ('node', 'Node'),
                    ('interface', 'Interface'),
                    ('service', 'Service'),
                    ('requisition', 'Requisition'),
                ],
                default='node',
                max_length=16,
            ),
        ),
        migrations.RunPython(reclassify_scope, noop),
    ]
