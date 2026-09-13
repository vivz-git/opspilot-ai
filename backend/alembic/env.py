import asyncio
from logging.config import fileConfig

from alembic import context
from app.config import get_settings
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The single settings object is the only thing that reads the environment
# (§17.1) — alembic.ini's own sqlalchemy.url is a placeholder, never real.
config.set_main_option("sqlalchemy.url", get_settings().database_url.get_secret_value())

# DB-001 adds the first ORM models. `app.persistence.models` must be imported
# (not just `app.persistence.base`) so its mapped classes register on
# `Base.metadata` before Alembic reads it — otherwise `--autogenerate` would
# see an empty schema. Every migration is still written by hand (§12.1); this
# only makes autogenerate diffs available as a check, not a generator.
from app.persistence import mock_crm as _mock_crm  # noqa: E402,F401
from app.persistence import models as _models  # noqa: E402,F401
from app.persistence.base import Base as _Base  # noqa: E402

target_metadata = _Base.metadata

#: The schemas Alembic owns (§12.1). `None` is the connection's default
#: schema, where Alembic keeps `alembic_version`. Everything else — above all
#: `langgraph`, which the LangGraph saver creates and migrates itself
#: (`app.persistence.checkpointing`, ADR-011) — is invisible to autogenerate,
#: so `alembic check` never proposes dropping tables we deliberately do not
#: model, and no revision of ours can ever touch them.
OWNED_SCHEMAS: frozenset[str | None] = frozenset({None, "opspilot", "mock_crm"})


def include_name(name: str | None, type_: str, parent_names: dict[str, str | None]) -> bool:
    if type_ == "schema":
        return name in OWNED_SCHEMAS
    return True


# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
        include_name=include_name,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_schemas=True,
        include_name=include_name,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """In this scenario we need to create an Engine
    and associate a connection with the context.

    """

    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        # Force a `search_path` that never includes `opspilot`, even though
        # the connecting role is itself named `opspilot` (see
        # `docker-compose.yml` / `.env.example`). Postgres's default
        # `search_path` is `"$user", public`, so a role named `opspilot`
        # makes `opspilot` the connection's *ambient default* schema — the
        # exact same name as our real schema. Every ORM table declares
        # `schema="opspilot"` explicitly (`app.persistence.base.Base`), but
        # unqualified reflection of the ambient-default schema reports
        # `referred_schema: None` for objects that are actually in
        # `opspilot`, which reads as a different identity than the
        # metadata's explicit `"opspilot"` — so `--autogenerate`/`alembic
        # check` reported every foreign key as simultaneously removed (as
        # `schema=None`) and re-added (as `schema='opspilot'`), even with
        # `include_schemas=True` (which is still required — it is what
        # makes Alembic reflect and compare `opspilot` and `mock_crm` at
        # all, rather than only the connection's default schema). Pinning
        # `search_path` to `public` — a schema with no ORM tables — removes
        # the ambiguity: no schema can now be mistaken for "the default",
        # so `opspilot` is always reflected and compared under its own
        # name. This is a reflection-time fix only: every migration already
        # fully qualifies its DDL with `schema=SCHEMA`, and every ORM query
        # is schema-qualified through `Base.metadata`, so nothing here
        # relies on `search_path` for correctness at runtime — only
        # Alembic's autogenerate comparator does.
        connect_args={"server_settings": {"search_path": "public"}},
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""

    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
