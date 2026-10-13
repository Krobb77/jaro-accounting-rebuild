"""Alembic environment — sync migrations, H5 migration-first releases.

Migrations run in a one-shot job as the jaro_migrate role BEFORE traffic
shifts; the app never migrates itself. DATABASE_URL comes from
--sqlalchemy.url or the DATABASE_URL environment variable.
"""

import os
import sys

from alembic import context
from sqlalchemy import create_engine

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
# Alembic loads version modules without their directory on sys.path; the
# shared migration helpers live next to env.py.
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from app.models import Base  # noqa: E402

config = context.config
target_metadata = Base.metadata


def get_url() -> str:
    url = config.get_main_option("sqlalchemy.url") or os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError("DATABASE_URL is not set (or --sqlalchemy.url)")
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=get_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(get_url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
