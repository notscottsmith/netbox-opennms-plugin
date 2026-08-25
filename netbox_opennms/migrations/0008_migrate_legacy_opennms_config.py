# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
# Multi-server support (ADR 0002) removes the global opennms_url/
# opennms_username/opennms_password/default_location PLUGINS_CONFIG keys in
# favor of OpenNMSServer rows. An existing single-server install still has
# those keys set in its PLUGINS_CONFIG at upgrade time (they're just no longer
# documented/defaulted) — read them straight off settings.PLUGINS_CONFIG here
# so the upgrade needs zero manual steps, then never look at them again.

from django.conf import settings
from django.db import migrations


def create_default_server_from_legacy_config(apps, schema_editor):
    """Auto-create the Default Server from the pre-multi-server PLUGINS_CONFIG."""
    OpenNMSServer = apps.get_model("netbox_opennms", "OpenNMSServer")
    if OpenNMSServer.objects.exists():
        return
    config = settings.PLUGINS_CONFIG.get("netbox_opennms", {})
    url = config.get("opennms_url")
    if not url:
        return
    OpenNMSServer.objects.create(
        name="Default",
        url=url,
        username=config.get("opennms_username") or "",
        password=config.get("opennms_password") or "",
        default_location=config.get("default_location") or "",
        is_default=True,
    )


def remove_legacy_default_server(apps, schema_editor):
    """Reverse: drop the Server this migration created, if still untouched."""
    OpenNMSServer = apps.get_model("netbox_opennms", "OpenNMSServer")
    OpenNMSServer.objects.filter(name="Default", is_default=True).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('netbox_opennms', '0007_opennmsserver_monitoringexclusion'),
    ]

    operations = [
        migrations.RunPython(
            create_default_server_from_legacy_config, remove_legacy_default_server
        ),
    ]
