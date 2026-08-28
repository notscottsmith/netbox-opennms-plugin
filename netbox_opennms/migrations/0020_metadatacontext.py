# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
# Hand-authored (no Docker/host Python available to run makemigrations here) —
# verify with `make makemigrations` before merge.
#
# Adds the MetadataContext registry (issue #41): a persisted, user-extensible
# set of OpenNMS metadata Contexts backing MetadataEntry.context. Seeds
# OpenNMS's five built-in contexts (node, requisition, interface, service,
# pattern — Horizon deep-dive doc, "Metadata Contexts") as protected/
# undeletable rows, and backfills a MetadataContext for any nonstandard
# context value already present on an existing MetadataEntry row, so the
# model's new registry-membership check in clean() doesn't retroactively
# invalidate data that pre-dates this migration.

import netbox.models.deletion
import taggit.managers
import utilities.json
from django.db import migrations, models

BASE_CONTEXTS = ("node", "requisition", "interface", "service", "pattern")


def seed_metadata_contexts(apps, schema_editor):
    MetadataContext = apps.get_model("netbox_opennms", "MetadataContext")
    MetadataEntry = apps.get_model("netbox_opennms", "MetadataEntry")

    for name in BASE_CONTEXTS:
        MetadataContext.objects.get_or_create(
            name=name, defaults={"is_builtin": True}
        )

    # Backfill: any context value already in use that isn't one of the base
    # contexts above (almost certainly an 'X-'-prefixed custom value under
    # the pre-existing clean() rule) becomes a non-builtin registry row, so
    # it keeps validating after this migration.
    existing = (
        MetadataEntry.objects.exclude(context__in=BASE_CONTEXTS)
        .exclude(context="")
        .values_list("context", flat=True)
        .distinct()
    )
    for name in existing:
        MetadataContext.objects.get_or_create(name=name, defaults={"is_builtin": False})


def noop(apps, schema_editor):
    # Pre-1.0: rollback is a DB restore, not a reversible data migration (L1).
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_opennms', '0019_discoveryscan_cleaned_up_at'),
    ]

    operations = [
        migrations.CreateModel(
            name='MetadataContext',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('created', models.DateTimeField(auto_now_add=True, null=True)),
                ('last_updated', models.DateTimeField(auto_now=True, null=True)),
                ('custom_field_data', models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder)),
                ('name', models.CharField(max_length=64, unique=True)),
                ('is_builtin', models.BooleanField(default=False, editable=False)),
                ('description', models.CharField(blank=True, max_length=200)),
                ('tags', taggit.managers.TaggableManager(through='extras.TaggedItem', to='extras.Tag')),
            ],
            options={
                'verbose_name': 'metadata context',
                'verbose_name_plural': 'metadata contexts',
                'ordering': ('name',),
            },
            bases=(netbox.models.deletion.DeleteMixin, models.Model),
        ),
        migrations.RunPython(seed_metadata_contexts, noop),
    ]
