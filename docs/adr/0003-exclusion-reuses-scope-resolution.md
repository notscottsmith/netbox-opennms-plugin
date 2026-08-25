# Monitoring Exclusion reuses Scope Resolution, not a separate mechanism

Beyond per-device `MonitoringOverride.exclude`, admins need to disable monitoring for a whole tenant, tenant group, site group, site, or NetBox Location without hand-creating an override per device. Rather than building a second, independent exclusion mechanism, `MonitoringExclusion` reuses the exact same five-level Scope and precedence engine built for OpenNMS Server assignment (see [0002](./0002-scope-mirrors-configcontext.md)): exclusion is just another possible outcome of Scope Resolution, so a more specific level can re-enable monitoring underneath an excluded ancestor, symmetric with how a site-level server binding already overrides its parent tenant group's.

## Considered Options

A simpler one-way exclusion list (cascades down, no override) was considered. Rejected: it's asymmetric with how server binding already works, meaning two separate resolution algorithms to maintain, and a real MSP scenario exists (a customer decommissioning most sites but keeping one still monitored) that a one-way cascade can't express without falling back to per-device overrides anyway.
