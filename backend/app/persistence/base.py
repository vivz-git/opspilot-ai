"""The shared declarative base for the `opspilot` control-plane schema (§12.1).

`OPSPILOT_SCHEMA` is the single place that names the schema FOUND-003's
migration created (`CREATE SCHEMA IF NOT EXISTS opspilot`) — every model in
this package inherits it from `Base.metadata` rather than repeating it.
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

OPSPILOT_SCHEMA = "opspilot"
MOCK_CRM_SCHEMA = "mock_crm"

#: Deterministic constraint names so a hand-written migration and a future
#: `--autogenerate` diff agree on what a constraint is called.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(schema=OPSPILOT_SCHEMA, naming_convention=NAMING_CONVENTION)
