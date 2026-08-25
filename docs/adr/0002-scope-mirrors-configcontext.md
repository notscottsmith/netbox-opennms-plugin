# Scope is modeled as parallel ManyToMany fields, mirroring ConfigContext

An OpenNMS Server needs to bind to any combination of tenant group, tenant, site group, site, and NetBox Location, without duplicating a server's URL/credentials every time a new binding is added. We modeled this the same way NetBox core's own `ConfigContext` already solves an identical problem: five `ManyToManyField`s directly on `OpenNMSServer` (`tenant_groups`, `tenants`, `site_groups`, `sites`, `locations`), rather than a generic content-type/object-id assignment table. Precedence for Scope Resolution is strict specificity order — location > site > site group > tenant > tenant group — and a binding on a parent site group or tenant group cascades to everything nested beneath it, matching `ConfigContext`'s own inheritance behavior. A given object may be bound directly to only one server at a time; this is enforced as a hard validation error at assignment time, not deferred to a runtime conflict, since two servers claiming the same object directly is a data-entry mistake rather than a legitimate case.

The Default Server (the fallback when no Scope binding matches) is a real `OpenNMSServer` row with no Scope bindings, rather than a separate code path — on upgrade, existing single-server installs get one created automatically from their prior global configuration, so nothing breaks silently.

## Considered Options

- A generic assignment table keyed by content-type + object-id: more open-ended, but doesn't match `ConfigContext`'s established shape for this exact fixed set of five types, and adds a GFK indirection layer for no real benefit here.
- Fixed FK columns per server row, one scope per row: rejected because binding one physical server to multiple scopes (e.g. a tenant-wide default plus a site-level override) would mean duplicate rows repeating the same URL and credentials, with nothing keeping them in sync if the server's connection details change.
