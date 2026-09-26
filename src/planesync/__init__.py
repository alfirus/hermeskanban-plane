"""planesync — lightweight Plane <-> Hermes Kanban bidirectional sync.

Plane is the source of truth for WHAT we are doing (issues, human-facing).
Hermes Kanban is the dispatch runtime for HOW work executes (agent tasks).
This package is the plumbing between them; see docs/adr/0001.
"""

__version__ = "0.1.0"

# One-way write rules (enforced in sync.py, documented in README + ADR 0001):
#   Plane -> kanban: CREATE ONLY. A mapped Plane work item yields at most one
#     kanban task (idempotent via link store + --idempotency-key). The sync
#     never edits a kanban task it created.
#   kanban -> Plane: REFLECT ONLY. On task done -> move the linked work item to
#     a 'completed'-group state and post one comment with the run summary. On
#     task blocked -> post one comment (no state change). The sync never
#     creates/edits/deletes Plane work item text, assignees or priority.
#   Because kanban writes never create kanban tasks and Plane writes never
#     re-create linked tasks, no feedback loop can form.
