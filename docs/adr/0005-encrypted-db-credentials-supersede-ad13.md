# Per-server encrypted DB credentials supersede AD-13

This codebase has a named, deliberate architecture decision — referred to inline as AD-13 (`netbox_opennms/__init__.py:18`, `netbox_opennms/client/client.py:58,232`, `netbox_opennms/views.py:546`) — that credentials are read at runtime from `PLUGINS_CONFIG` and are never stored in plugin models. Multi-server support breaks that constraint by necessity: an MSP managing many customer servers through the NetBox UI needs to add, edit, and rotate per-server credentials without editing `configuration.py` and restarting NetBox for every change. We chose to store credentials (`username`, `password`, `headers`) on `OpenNMSServer` in the database, encrypted at the field level with `cryptography`'s Fernet, superseding AD-13 while preserving the *property* it protected — no casually-readable plaintext secret sitting in the open — even though it moves *where* that property is enforced.

The Fernet key itself is **not** derived from anything already in the database or from NetBox's own `SECRET_KEY`; it's a new, required `PLUGINS_CONFIG["netbox_opennms"]["opennms_secret_key"]` setting, enforced via NetBox's `PluginConfig.required_settings` — NetBox refuses to start without it. This keeps the key itself out of the database, in the same file-based trust boundary AD-13 originally relied on, even though the ciphertext it protects now lives in a model.

## Considered Options

- **`netbox-secrets` plugin**: the NetBox-idiomatic way to store secrets today, but adds a hard dependency on a second plugin's models, migrations, and permission system for a first iteration. Deferred, not ruled out — the encrypted-field design doesn't preclude adding it as an alternative backend later.
- **Cloudflare Secrets Store / ITGlue as a credential backend**: both real possibilities raised during design, but both require new external API integration work (unverified at design time) disproportionate to a first iteration. Deferred for the same reason as above.
- **Deriving the Fernet key from NetBox's `SECRET_KEY`** when a dedicated key isn't set: rejected. `SECRET_KEY` isn't itself a valid Fernet key without a derivation step, and if `SECRET_KEY` is ever rotated (e.g. after a security incident) with no dedicated key configured, every stored credential would silently become undecryptable with no way to distinguish that from corruption.

## Consequences

The four inline `AD-13` references in the codebase describe a constraint this decision deliberately reverses; they should be updated to point at this ADR instead of continuing to assert a rule that's no longer true.
