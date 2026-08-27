# Discovered Node review data is persisted at scan time, not fetched live

The existing Device/VM import review flow (issue #9,
`DiscoveredNodeImportView`/`import_node.build_proposal`) fetches a node's
detail, IP interfaces, and services live from OpenNMS at the moment an
operator opens the review page. A Discovery Scan's auto-cleanup ([ADR
0006](./0006-discovery-scan-lifecycle-via-foreign-source.md)) deletes the
OpenNMS-side node once it's settled and past the retention window — so a live
fetch at review time would return nothing for any node an operator gets to
after cleanup, defeating the point of retaining Discovered Node rows in
NetBox for later review.

A Discovery Scan's background Job already walks each newly-found node
(detail/interfaces/services) to compute a per-node completeness flag
(surfacing nodes missing the data required to create a Device, e.g. no SNMP
credentials configured on OpenNMS). That same walked payload is persisted
onto the Discovered Node row, so review and conversion read from NetBox's own
stored copy — independent of whether the OpenNMS-side node still exists.

## Consequences

Review data is a point-in-time snapshot from scan time, not live. If OpenNMS
gathers more data on a node later (e.g. an admin fixes SNMP credentials after
the initial walk), only a fresh Discovery Scan re-walk picks that up — the
existing snapshot on an already-created Discovered Node row does not
retroactively improve.
