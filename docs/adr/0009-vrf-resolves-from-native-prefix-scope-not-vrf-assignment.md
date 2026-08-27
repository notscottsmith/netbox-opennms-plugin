# VRF resolution reads NetBox's native Prefix scope, not a VRF Assignment model

Status: supersedes ADR 0008.

ADR 0008 introduced a bespoke **VRF Assignment** model because `ipam.IPRange`
has a `vrf` field but no scope field, so nothing on an IP Range itself could
answer "which VRF does this belong to" — unlike `ipam.Prefix`, whose native
`CachedScopeMixin` scope sits alongside its own `vrf` field. That gap is real,
but a VRF is already "assigned" to a NetBox entity the moment an operator
scopes a `Prefix` to it and gives that `Prefix` a `vrf` — building a second,
parallel binding table duplicates a mechanism NetBox already ships, and lets
the two drift out of sync with each other and with the `Prefix` data an
operator actually maintains.

`scope.resolve_vrf()` now walks the same five-level, most-specific-wins
precedence used by `OpenNMSServer`/`MonitoringExclusion` (ADR 0002), starting
from a Requisition's own resolved site/location, and at each level queries
`ipam.Prefix` directly for one scoped to that site/location/tenant/… with a
non-null `vrf`. `IPRange`'s missing scope field is no longer a gap to fill —
IP Range proposals resolve their VRF through the same `Prefix`-based lookup
as everything else, rather than through their own `vrf` field.

This also removes the requirement ADR 0008 placed on Discovery Scan (a
NetBox site/location at creation time): a Discovery Scan now instead requires
a `Requisition` link, whose already-existing scope (`filter_params`, the
Requisition Scope picker) supplies the site/location `resolve_vrf` needs —
no separate site/location capture on the scan itself. `VRFAssignment` and its
full CRUD/API/UI stack are deleted outright; there is no data migration
concern, since no production caller ever read from it.
