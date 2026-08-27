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

## Consequences

Because the trigger is fire-and-forget, a scan that never finds anything (bad
IP range, no SNMP reachability) looks identical to one that hasn't started yet
— both just show zero Discovered Nodes and an un-advancing idle timer. There
is no error path from OpenNMS distinguishing "nothing found" from "didn't
run."
