# A new VRF Assignment model, not Prefix's native scope, resolves VRF for discovered IPs

Reconciling a Discovered Node's IP interfaces against NetBox (Track A) needs
to assign a VRF to newly-proposed Prefixes and IP Ranges, because this
plugin's MSP use case has many sites reusing the same private address space
— VRF is how they're delineated. NetBox's `Prefix` model has native scope
(Region/SiteGroup/Site/Location via `CachedScopeMixin`) alongside its `vrf`
field, so a proposed Prefix can carry both directly. `IPRange`, confirmed
against NetBox 4.6 source, has a `vrf` field but no scope fields at all —
nothing on the object itself can answer "which VRF does this address bucket
belong to."

A new **VRF Assignment** model closes that gap: it binds a VRF to a Scope
(tenant group/tenant/site group/site/location), reusing the exact precedence
engine already built for `OpenNMSServer`/`MonitoringExclusion` (ADR 0002),
rather than inventing a second resolution mechanism. Every Discovery Scan
now requires a NetBox site/location at creation time (also needed to supply
OpenNMS's own `location` field on the discovery request), and every
Discovered Node it produces resolves its VRF through that site/location
against VRF Assignment — for both Prefix and IP Range proposals, so the two
mechanisms stay consistent instead of Prefix using its own native scope
while IP Range uses something else.

When a node's IP interface carries no netmask (OpenNMS only populates this
when SNMP was reachable), there's nothing to compute a real prefix length
from, so the proposed IP Range is sized by RFC1918 classful convention
(10.0.0.0/8 space → /8, 172.16.0.0/12 → /16, 192.168.0.0/16 → /24; anything
else, including IPv6, → a flat /24 or /64) rather than left unsized.

## Consequences

A proposed Prefix and a proposed IP Range for the same discovered address
space can end up with different VRFs if VRF Assignment's binding changes
between when each was proposed — both are resolved at proposal time, not
pinned once created. All creation is propose-then-operator-confirms (never
an automatic write from the background walk), so this is a review-time
risk, not a silent data-integrity one.
