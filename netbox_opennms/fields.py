# Copyright 2026 Ronny Trommer <ronny@no42.org>
# SPDX-License-Identifier: MIT
"""Field-level encryption for per-server credentials (ADR 0005).

Ciphertext only ever touches the database — the Python-side value is always
plaintext (a ``str`` or, for :class:`EncryptedJSONField`, a ``dict``). The
Fernet key is a required plugin setting; see ADR 0005 for why it is not
derived from NetBox's own ``SECRET_KEY``.
"""

import json

from cryptography.fernet import Fernet, InvalidToken
from django.core.exceptions import ImproperlyConfigured
from django.db import models
from netbox.plugins import get_plugin_config


def _fernet():
    key = get_plugin_config("netbox_opennms", "opennms_secret_key")
    if not key:
        raise ImproperlyConfigured(
            "PLUGINS_CONFIG['netbox_opennms']['opennms_secret_key'] is required "
            "to store per-server credentials (ADR 0005)."
        )
    try:
        return Fernet(key.encode("utf-8") if isinstance(key, str) else key)
    except (ValueError, TypeError) as exc:
        raise ImproperlyConfigured(
            "PLUGINS_CONFIG['netbox_opennms']['opennms_secret_key'] is not a "
            "valid Fernet key (ADR 0005)."
        ) from exc


def _decrypt(ciphertext):
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ImproperlyConfigured(
            "A stored value could not be decrypted with the configured "
            "opennms_secret_key (ADR 0005) — has the key changed?"
        ) from exc


class EncryptedTextField(models.TextField):
    """A ``TextField`` whose value is Fernet-encrypted at rest (ADR 0005)."""

    def get_prep_value(self, value):
        value = super().get_prep_value(value)
        if not value:
            return value
        return _fernet().encrypt(value.encode("utf-8")).decode("ascii")

    def from_db_value(self, value, expression, connection):
        if not value:
            return value
        return _decrypt(value)


class EncryptedJSONField(models.TextField):
    """A JSON object, Fernet-encrypted at rest (ADR 0005/0004). Python-side: a dict."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("default", dict)
        kwargs.setdefault("blank", True)
        super().__init__(*args, **kwargs)

    def get_prep_value(self, value):
        if not value:
            return ""
        return _fernet().encrypt(json.dumps(value).encode("utf-8")).decode("ascii")

    def from_db_value(self, value, expression, connection):
        if not value:
            return {}
        return json.loads(_decrypt(value))

    def to_python(self, value):
        if value is None or isinstance(value, dict):
            return value
        if isinstance(value, str) and value:
            try:
                return json.loads(value)
            except ValueError:
                return value
        return value

    def value_to_string(self, obj):
        return json.dumps(self.value_from_object(obj))
