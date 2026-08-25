# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
# Example plugin config for the quickstart deployment, mounted into NetBox at
# /etc/netbox/config/plugins.py.
#
# OpenNMS connection details (URL, credentials, headers, default location) are
# no longer set here — they live on OpenNMSServer rows (ADR 0002), managed from
# the NetBox UI/API so multiple OpenNMS instances can be scoped per
# tenant/site/location. After NetBox starts, create an OpenNMS Server pointing
# at the bundled OpenNMS service (`docker compose --profile opennms up`):
#   URL: http://opennms:8980/opennms   Username: admin   Password: admin
PLUGINS = ["netbox_opennms"]

PLUGINS_CONFIG = {
    "netbox_opennms": {
        # Fernet key protecting OpenNMSServer credentials/headers at rest (ADR
        # 0005) — required to start. This is a throwaway key for the quickstart
        # only; generate your own for a real deployment and keep it secret:
        #   python -c "from cryptography.fernet import Fernet; \
        #   print(Fernet.generate_key().decode())"
        "opennms_secret_key": "qM8k4kcMHkClEKDED_DUa0WoIFuVXVu9FW8tHSkBC3Y=",
        # rescanExisting value for the import step: "true" | "false" | "dbonly".
        "import_mode": "false",
    },
}
