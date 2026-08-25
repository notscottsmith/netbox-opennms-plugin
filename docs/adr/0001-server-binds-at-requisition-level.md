# Server binds at the Requisition level, not per-device

Adding multi-OpenNMS-server support raised the question of what happens when a single Requisition's filter matches Devices/VMs that belong to different customers/scopes. We decided a Requisition still owns exactly one Foreign Source on exactly one OpenNMS Server: if Scope Resolution finds its matched objects don't agree on a server, that's a blocking Server Conflict the administrator resolves by tightening the filter or splitting the Requisition — mirroring the existing Filter Conflict UX rather than inventing a new one.

## Considered Options

We considered letting one Requisition's filter span multiple servers, with the plugin automatically fragmenting it into N per-server Foreign Sources behind the scenes. Rejected: it changes what a Requisition *means*, and would require every call site that currently assumes "one Requisition → one client → one Foreign Source" (the render/sync pipeline) to be restructured around a one-to-many relationship, for an automatic behavior that's harder to reason about than a conflict the admin explicitly resolves.
