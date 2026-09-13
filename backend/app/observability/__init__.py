"""Observability (§14): the product trace and what may be written into it.

OBS-001 adds the `TraceRecorder` and `@traced_node`; the redaction and
truncation rules of §14.5 live here already because TOOL-002's dispatcher is
the first choke point that persists tool payloads, and nothing may be
persisted unredacted.
"""
