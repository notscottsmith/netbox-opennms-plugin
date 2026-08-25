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
