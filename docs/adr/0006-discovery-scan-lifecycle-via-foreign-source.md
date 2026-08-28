# Discovery Scan triggers OpenNMS via a throwaway Foreign Source, polled to infer completion

`POST /api/v2/discovery` (OpenNMS's real network-scan feature — ICMP/SNMP sweep
over IP ranges) is fire-and-forget: confirmed against source
(`DiscoveryRestService`/`DiscoveryTaskExecutorImpl`, Horizon 36), there is no
job-status endpoint anywhere on that resource. Tagging the scan request with a
`foreignSource` routes each resulting `newSuspect` event straight into a live
`OnmsNode` row under that Foreign Source (`NewSuspectScan.scanUndiscoveredNode`
→ `createUndiscoveredNode`) — not into a pending requisition awaiting a
separate import step, unlike a normal Requisition sync.

A Discovery Scan derives a throwaway Foreign Source name
(`{foreign_id_prefix}-discovery-{timestamp}`) and a recurring background Job
polls `GET /api/v2/nodes?_s=foreignSource==<name>` (a first-class searchable
node property) to find newly-appeared nodes and infer completion: no new node
`createTime` for a configurable idle window means the scan is considered
settled. After a further configurable retention window past settling, the Job
reuses the plugin's existing `delete_requisition()` (deployed-then-pending
two-step delete) to clean up the OpenNMS-side data — this is the only way to
remove nodes tied to a Foreign Source, since v2's `NodeRestService` exposes no
node-level `DELETE` at all, only sub-resource deletes (metadata).

### Cleanup is requisition-only — no foreign-source definition is ever created

`CleanupDiscoveryScansJob` intentionally calls only `delete_requisition()`, not
`delete_foreign_source()`, even though the Sync/Remove teardown in
`SyncForeignSourceJob._render_and_replace` always pairs the two (plus clearing
its `DeployedForeignSource` tracking row) when an `allow_empty` Remove empties
a Foreign Source. This is not an oversight: a Discovery Scan's `foreignSource`
tag never goes through this plugin's definition-push path at all, so there is
no `/rest/foreignSources/{fs}` definition for it to delete.

`client.post_foreign_source()` — the only call in this codebase that creates a
foreign-source *definition* (the detectors/policies XML shell POSTed to
`/rest/foreignSources`) — is invoked exclusively from
`SyncForeignSourceJob._render_and_replace`, i.e. only as part of a Requisition
sync. `DiscoveryScanTriggerView.post` (the sole discovery-trigger code path)
calls `client.run_discovery()` and nothing else; `run_discovery()` itself
issues exactly one HTTP request, `POST /api/v2/discovery`, and never touches
`/rest/foreignSources`. Server-side, OpenNMS's `foreignSource` tag on a
discovery request just labels the `OnmsNode` row that
`NewSuspectScan.scanUndiscoveredNode` → `createUndiscoveredNode` creates
directly (see above) — it does not cause OpenNMS to synthesize a
foreign-source definition for that name. A definition-less foreign source
falls back to default scan behavior; nothing about the newSuspect path writes
one into existence.

The absence of any `DeployedForeignSource`-equivalent tracking row for
`DiscoveryScan` is further evidence for this: that model exists specifically
to record when this plugin has pushed a definition/requisition pair worth
reconciling later (`_render_and_replace`, `update_or_create`d only when nodes
are actually pushed), and Discovery Scans never populate it because they never
go through that push.

Net effect: `delete_foreign_source()` would be a guaranteed no-op (or a 404)
for every Discovery Scan cleanup, so it is deliberately omitted rather than
called defensively. If a future change ever makes discovery push a
foreign-source definition (e.g. to apply custom detectors/policies to
discovered nodes), this reasoning — and `CleanupDiscoveryScansJob` — must be
revisited together (tracked as issue #51, closed on this basis).

## Consequences

Because the trigger is fire-and-forget, a scan that never finds anything (bad
IP range, no SNMP reachability) looks identical to one that hasn't started yet
— both just show zero Discovered Nodes and an un-advancing idle timer. There
is no error path from OpenNMS distinguishing "nothing found" from "didn't
run."
