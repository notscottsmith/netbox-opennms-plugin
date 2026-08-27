# netbox-opennms-plugin

A NetBox plugin that renders monitoring intent — a live NetBox filter over Devices/VMs — into OpenNMS requisitions across one or more OpenNMS servers, and keeps them in sync.

## Language

### Provisioning

**Requisition**:
A user-named OpenNMS Foreign Source: the live NetBox filter that determines which Devices/VMs are monitored, plus the detector/policy definitions applied to them. Its matched objects must resolve to exactly one OpenNMS Server.
_Avoid_: filter (that's the mechanism inside a Requisition, not the Requisition itself)

**Foreign Source**:
OpenNMS's own term for a named provisioning unit (detectors, policies, scan interval). A Requisition owns exactly one Foreign Source.

**Monitoring Override**:
A per-Device/VM exception to its Requisition's defaults — exclude, override management IP, add interfaces, add or suppress services, override Monitoring Location. The most specific level of Scope Resolution.

### Multi-server scope

**OpenNMS Server**:
One target OpenNMS instance the plugin can provision into, with its own URL, credentials, and Monitoring Locations.
_Avoid_: instance, connection (legacy single-server terms)

**Scope**:
The set of NetBox organizational objects — tenant group, tenant, site group, site, NetBox Location — that an OpenNMS Server or a Monitoring Exclusion is bound to.

**Scope Resolution**:
The process of finding which OpenNMS Server, or which Monitoring Exclusion, applies to a given Device/VM: walk its NetBox Location, site, site group, tenant, and tenant group from most to least specific, and take the first bound match. A binding on a parent tenant group or site group applies to everything nested beneath it, unless a more specific binding overrides it.

**Default Server**:
The OpenNMS Server used when Scope Resolution finds no bound match for a Device/VM. Has no Scope of its own; at most one may exist.

**Monitoring Exclusion**:
A declaration that a tenant group, tenant, site group, site, or NetBox Location is not monitored, regardless of whether its Devices/VMs would otherwise match a Requisition's filter. Resolved by the same Scope Resolution precedence as OpenNMS Server assignment, so a more specific inclusion can override a less specific exclusion.

### Node identity

**Foreign ID**:
The stable per-node identifier the plugin derives for each Requisition member and pushes to OpenNMS (`{foreign_id_prefix}-device-{pk}` / `{foreign_id_prefix}-vm-{pk}`, or the unprefixed legacy form when the prefix is empty). Node identity within a Requisition is the pair (Foreign Source, Foreign ID); `derivation.py` is the sole owner of this derivation.

**Foreign ID Prefix**:
The `foreign_id_prefix` plugin setting (default `netbox`) prepended to every Foreign ID this plugin derives. Configurable per install; changing it is a node-identity change, not a cosmetic one.

**Adoption**:
Before a Sync renders and pushes a Requisition, matching a desired node's label against OpenNMS's current live state by Foreign Source and reusing the existing node's Foreign ID verbatim, instead of assigning a freshly-derived one — so a pre-existing OpenNMS node (created by hand, by a prior scheme, or by another tool) is kept in place rather than duplicated. Unambiguous by construction: a label matching more than one node on either side is skipped (keeps the freshly-derived id) and raises a non-blocking warning. Applied identically before a real Sync and before a dry-run diff, so the preview always matches what a Sync would actually push.

### Discovery

**Discovered Node**:
One OpenNMS node found by scanning a Server's live node inventory — either a full inventory scan or a Discovery Scan — holding its NetBox match verdict (matches, differs, or missing). Once its OpenNMS detail has been walked, that snapshot is persisted on the row itself, so review and conversion into a Device/VM don't depend on the OpenNMS-side node still existing. Its walked IP interfaces carry their own per-address verdict alongside the node-level one, reconciled against NetBox independently of whether the node itself has been converted to a Device/VM.
_Avoid_: Discovery on its own — always say Discovered Node or Discovery Scan; this repo has two other unrelated senses of the bare word (the plugin's internal detector/policy catalog reader, and the general software-engineering sense).

**Discovery Scan**:
A NetBox record for one triggered OpenNMS network-scan (an ICMP/SNMP sweep over IP ranges via OpenNMS's own Discovery feature), bound to a single OpenNMS Server and a NetBox site/location, and identified by a throwaway Foreign Source. The site/location supplies OpenNMS's own required Monitoring Location field on the discovery request, and is what every Discovered Node it produces uses to resolve VRF Assignment for its IP interfaces. OpenNMS gives no synchronous completion signal, so a Discovery Scan is polled until no new nodes appear for a while (considered settled), then its OpenNMS-side data is cleaned up after a retention period — the Discovered Node rows it produced remain in NetBox regardless.
_Avoid_: Discovery on its own (see Discovered Node)

**VRF Assignment**:
A record binding a VRF to a Scope (tenant group/tenant/site group/site/location), resolved by the same most-specific-wins precedence engine as OpenNMS Server and Monitoring Exclusion. Used to determine which VRF a Discovered Node's proposed Prefix or IP Range belongs to, since NetBox's own IP Range has no site/location scope of its own to carry that.

### Conflicts

**Conflict**:
A blocking condition that an administrator must resolve by hand before a sync can proceed — never resolved automatically. Two kinds exist: a **Filter Conflict** (a Device/VM matches more than one Requisition's filter) and a **Server Conflict** (a Requisition's matched Devices/VMs don't all resolve to the same OpenNMS Server via Scope Resolution).

### Locations

**NetBox Location**:
A DCIM sub-division of a Site in NetBox's own organizational hierarchy. One of the five levels a Scope can bind to.
_Avoid_: location on its own — always qualify as NetBox Location or Monitoring Location, they are unrelated concepts that happen to share a name.

**Monitoring Location**:
OpenNMS's own concept naming which Minion polls a node. Configured independently per OpenNMS Server — not a NetBox object, and not shared across servers.
_Avoid_: location on its own; site (a NetBox Site is unrelated)
