# Per-server auth extensions are a generic `headers` field, not Cloudflare-specific fields

Some OpenNMS Servers sit behind Cloudflare Tunnels and need a service-token pair sent as extra HTTP headers on every request, on top of OpenNMS's own username/password. Rather than adding typed `cf_access_client_id`/`cf_access_client_secret` fields, `OpenNMSServer` carries a single optional, encrypted `headers` field — a JSON object merged into every outbound request to that server. This covers the Cloudflare Access case without hardcoding to Cloudflare, so any future auth-proxy scheme in front of an OpenNMS instance is supported with zero code changes.

## Considered Options

- Cloudflare-specific typed fields: simpler for the one known case, but ties the schema to a single vendor for a mechanism (extra request headers) that's inherently generic.
- A parallel `body` JSON field for arbitrary payload injection: rejected outright. OpenNMS's provisioning API already has fixed XML/JSON request bodies; there's no defined way to merge arbitrary keys into them, and Cloudflare Access itself is headers-only, so nothing needed it.
