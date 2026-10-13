"""Shared migration helpers: idempotent DDL + B1 fail-closed downgrades."""

import sqlalchemy as sa
import warnings


def table_names(bind):
    return set(sa.inspect(bind).get_table_names())


def create_table_idempotent(op, table: sa.Table) -> None:
    """Create ``table`` only if it does not exist (B7 resumable DDL)."""
    table.create(bind=op.get_bind(), checkfirst=True)


def drop_table_if_exists(op, name: str) -> None:
    if name in table_names(op.get_bind()):
        op.drop_table(name)


def index_names(bind, table_name: str) -> set:
    # SQLite cannot reflect expression-based indexes; the SAWarning is
    # expected and the create path tolerates "already exists".
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return {ix["name"] for ix in sa.inspect(bind).get_indexes(table_name)}


def create_index_idempotent(op, name: str, table: str, columns, **kw) -> None:
    bind = op.get_bind()
    if table not in table_names(bind) or name in index_names(bind, table):
        return
    try:
        op.create_index(name, table, columns, **kw)
    except sa.exc.OperationalError as exc:
        # SQLite cannot reflect expression-based indexes, so the check
        # above misses them; treat "already exists" as success.
        if "already exists" not in str(exc).lower():
            raise


def drop_index_if_exists(op, name: str, table: str) -> None:
    bind = op.get_bind()
    if table in table_names(bind) and name in index_names(bind, table):
        op.drop_index(name, table_name=table)


def refuse_if_live_rows(op, *tables: str) -> None:
    """B1: refuse a downgrade that would silently destroy live rows."""
    bind = op.get_bind()
    existing = table_names(bind)
    for table in tables:
        if table not in existing:
            continue
        count = bind.execute(sa.text(f"SELECT COUNT(*) FROM {table}")).scalar()
        if count:
            raise RuntimeError(
                f"B1 fail-closed downgrade refused: table '{table}' has {count} "
                "live row(s). Downgrading would silently destroy data; this "
                "requires an explicit, audited owner decision first."
            )


def ensure_stub(metadata: sa.MetaData, name: str, pk: str = "id") -> sa.Table:
    """Register a minimal stub for an FK target created by an EARLIER
    migration, so string FK references resolve at DDL render time.

    The stub is never created (it already exists in the database); it only
    gives the local MetaData enough shape to render REFERENCES clauses.
    """
    if name not in metadata.tables:
        return sa.Table(name, metadata, sa.Column(pk, sa.String(36), primary_key=True))
    return metadata.tables[name]
