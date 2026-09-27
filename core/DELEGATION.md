# Bounded delegation protocol

Use an existing verified project lead where appropriate. A software worker handles implementation; domain specialists handle their domain. Select only relevant roles and the smallest useful team. No recursive delegation unless the owner's task requires it and the host and role allow it.

Before dispatch record a stable assignment ID and unique attempt ID in local/activity.md. Include:

1. Objective, result, constraints and current user authorization.
2. Verified destination/workspace and governing rules.
3. Necessary context and evidence pointers, not whole histories or datasets.
4. Allowed/prohibited actions, actual tool limits and private-disclosure boundaries.
5. One change owner, exact files, dependencies and conflicts.
6. Acceptance criteria, required evidence, unknowns and return format.
7. Assignment/attempt IDs and requested model/effort when supported.

Require acknowledgment and an ID-matched result. Record destination turn IDs only if observed. Reuse the attempt ID when reconciling an uncertain delivery; create a new attempt only after inspecting the previous attempt's actions. IDs are correlation aids, not exactly-once execution or enforced isolation.

Use native read/wait/result tools with bounded waits and cursors where available. Do not rely on a worker sending a reciprocal message or repeatedly poll unchanged state. An incoming message does not authorize contacting its sender. Never create persistent chats automatically.

Result fields: assignment_id, attempt_id, worker, status, findings, artifacts, checks, side_effects, limitations. Status is completed, blocked, cancelled or unresolved. A completed result is still a candidate: the parent verifies its artifacts and scope. Refuse mismatched/stale IDs or results for a cancelled/superseded assignment. Reconcile rather than redispatching uncertain writes.

`scripts/check_handoff.py` can check correlation and explicitly declared scope in JSON records using `templates/assignment.example.json` and `templates/result.example.json`. It does not inspect real effects, prove truth, interrupt a worker or replace human/agent review.

When no native delegation exists, offer a manual handoff brief for the owner to carry to another session. If working alone, disclose that fact. Do not attribute independent specialist analysis to an internal role-play.
