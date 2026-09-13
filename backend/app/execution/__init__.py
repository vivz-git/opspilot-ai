"""Execution ownership (§2.4, ADR-004, ADR-023): who is driving a run, and
what happens when that process dies.

`leases` — a worker's lease on a run and the heartbeat that keeps it alive.
`recovery` — the reconciler that finds runs whose worker died and resumes
them from their LangGraph checkpoint, repairs them to `awaiting_approval`,
or marks them `failed(orphaned)`.

API-007's `Executor` composes these; nothing here starts a graph on its
own.
"""
