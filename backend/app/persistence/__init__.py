"""Persistence layer (§12). ORM models and repositories live here only.

Services and agent logic depend on repository protocols (DB-005), never on
`sqlalchemy` directly — this package is where that boundary is drawn.
"""
