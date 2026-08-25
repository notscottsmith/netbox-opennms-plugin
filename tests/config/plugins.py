# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
# NetBox plugin config for the throwaway test stack (mounted at
# /etc/netbox/config/plugins.py by compose.yml). Enables the plugin under test.
PLUGINS = ["netbox_opennms"]

PLUGINS_CONFIG = {
    "netbox_opennms": {
        # Throwaway key for the test DB only (ADR 0005) — a fixed 32-byte
        # urlsafe-base64 value so OpenNMSServer credential fields encrypt/decrypt
        # consistently across a single test run.
        "opennms_secret_key": "rl34UKgJe7JXYDjjH70tmzdHcDVQmDYgf1HBOXFTMpw=",
    },
}
