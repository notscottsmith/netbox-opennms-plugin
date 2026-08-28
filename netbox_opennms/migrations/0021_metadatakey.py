# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
# Hand-authored (no Docker/host Python available to run makemigrations here) —
# verify with `make makemigrations` before merge.
#
# Adds the MetadataKey registry (issue #41 follow-up): a persisted,
# user-extensible set of OpenNMS metadata Keys, scoped per Context, backing
# MetadataEntry.key. Seeds the fixed key vocabularies OpenNMS documents for
# the node/interface/service contexts (Horizon deep-dive doc, "Metadata
# Contexts") as protected/undeletable rows — requisition and pattern
# contexts have no documented key vocabulary and are seeded with none — and
# backfills a MetadataKey for any (context, key) pair already present on an
# existing MetadataEntry row, so the model's new registry-membership check
# in clean() doesn't retroactively invalidate data that pre-dates this
# migration.

import netbox.models.deletion
import taggit.managers
import utilities.json
from django.db import migrations, models

# Keys OpenNMS documents as fixed for these contexts (Horizon deep-dive doc).
# requisition and pattern contexts have no such vocabulary — no entry here
# means no keys are seeded for them, which is intentional, not an omission.
BUILTIN_KEYS = {
    "node": (
        "label",
        "foreign-source",
        "foreign-id",
        "netbios-domain",
        "netbios-name",
        "os",
        "sys-name",
        "sys-location",
        "sys-contact",
        "sys-description",
        "location",
        "area",
        "geohash",
    ),
    "interface": (
        "hostname",
        "address",
        "netmask",
        "if-index",
        "if-alias",
        "if-description",
        "if-name",
        "phy-addr",
    ),
    "service": ("name",),
}


def seed_metadata_keys(apps, schema_editor):
    MetadataContext = apps.get_model("netbox_opennms", "MetadataContext")
    MetadataKey = apps.get_model("netbox_opennms", "MetadataKey")
    MetadataEntry = apps.get_model("netbox_opennms", "MetadataEntry")

    for context_name, key_names in BUILTIN_KEYS.items():
        context, _ = MetadataContext.objects.get_or_create(
            name=context_name, defaults={"is_builtin": True}
        )
        for key_name in key_names:
            MetadataKey.objects.get_or_create(
                context=context, name=key_name, defaults={"is_builtin": True}
            )

    # Backfill: any (context, key) pair already in use that isn't one of the
    # builtin keys seeded above becomes a non-builtin registry row, so it
    # keeps validating after this migration.
    existing = (
        MetadataEntry.objects.exclude(context="", key="")
        .values_list("context", "key")
        .distinct()
    )
    for context_name, key_name in existing:
        context, _ = MetadataContext.objects.get_or_create(name=context_name)
        MetadataKey.objects.get_or_create(
            context=context, name=key_name, defaults={"is_builtin": False}
        )


def noop(apps, schema_editor):
    # Pre-1.0: rollback is a DB restore, not a reversible data migration (L1).
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_opennms', '0020_metadatacontext'),
    ]

    operations = [
        migrations.CreateModel(
            name='MetadataKey',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False)),
                ('created', models.DateTimeField(auto_now_add=True, null=True)),
                ('last_updated', models.DateTimeField(auto_now=True, null=True)),
                ('custom_field_data', models.JSONField(blank=True, default=dict, encoder=utilities.json.CustomFieldJSONEncoder)),
                ('name', models.CharField(max_length=100)),
                ('is_builtin', models.BooleanField(default=False, editable=False)),
                ('description', models.CharField(blank=True, max_length=200)),
                ('context', models.ForeignKey(on_delete=models.deletion.CASCADE, related_name='keys', to='netbox_opennms.metadatacontext')),
                ('tags', taggit.managers.TaggableManager(through='extras.TaggedItem', to='extras.Tag')),
            ],
            options={
                'verbose_name': 'metadata key',
                'verbose_name_plural': 'metadata keys',
                'ordering': ('context', 'name'),
            },
            bases=(netbox.models.deletion.DeleteMixin, models.Model),
        ),
        migrations.AddConstraint(
            model_name='metadatakey',
            constraint=models.UniqueConstraint(fields=('context', 'name'), name='netbox_opennms_metadatakey_unique_key'),
        ),
        migrations.RunPython(seed_metadata_keys, noop),
    ]
