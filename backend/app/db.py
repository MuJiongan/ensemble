from __future__ import annotations
import os
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./workflow_builder.db")

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Tiny ad-hoc migrations: SQLAlchemy's create_all only adds *tables*, not new
# columns. For the small number of column additions we've made post-v0 we do
# `ALTER TABLE ADD COLUMN` directly. Idempotent — checked against PRAGMA.
_PENDING_COLUMNS: list[tuple[str, str, str]] = [
    # (table, column, type with default)
    ("messages", "reasoning_details", "JSON DEFAULT '[]'"),
    ("messages", "cost", "FLOAT DEFAULT 0.0"),
    ("runs", "workflow_snapshot", "JSON DEFAULT NULL"),
    ("mcp_credentials", "token_endpoint_auth_method", "VARCHAR DEFAULT NULL"),
    # provider_id/variant were added to call_chats partway through the
    # continue-chat work, so a dev DB created from an earlier checkout of this
    # branch has the table but not these columns (create_all only adds whole
    # tables). Patch them in so a continuation pins the recorded model. Harmless
    # on a fresh DB (the columns already exist) — kept so nobody has to drop their
    # local DB across the branch.
    ("call_chats", "provider_id", "VARCHAR DEFAULT ''"),
    ("call_chats", "variant", "VARCHAR DEFAULT ''"),
]


def _ensure_columns() -> None:
    if not engine.url.drivername.startswith("sqlite"):
        return  # other backends would need a real migration tool
    with engine.connect() as conn:
        for table, column, type_def in _PENDING_COLUMNS:
            existing = {
                row[1]
                for row in conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
            }
            if column not in existing:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {type_def}"))
                conn.commit()
        _ensure_callchat_unique_index(conn)


def _ensure_callchat_unique_index(conn) -> None:
    """Back-fill (node_run_id, call_id) uniqueness on a call_chats table that
    predates the model's UniqueConstraint.

    create_all builds that constraint for fresh DBs (SQLite materializes it as a
    unique autoindex), but it skips an already-existing table and SQLite cannot
    ALTER TABLE ADD CONSTRAINT. A unique INDEX gives the same guarantee, so the
    first-turn create's IntegrityError dedup (_get_or_create_continuation) stays
    effective on a DB whose table was created before the constraint existed. No-op when uniqueness is already
    enforced (the normal case — matched by columns, not index name, so the
    constraint's autoindex counts). Never deletes rows: if pre-existing
    duplicates block the index it is left uncreated rather than risk data loss at
    startup (a constraint-less DB carrying duplicates is an unreachable corner in
    practice, and the runtime dedup still prevents new ones once an index exists)."""
    try:
        indexes = conn.execute(text("PRAGMA index_list(call_chats)")).fetchall()
    except Exception:
        return  # table absent (fresh DB handled by create_all) or unreadable
    for ix in indexes:
        name, unique = ix[1], ix[2]
        if not unique:
            continue
        cols = [
            r[2] for r in conn.execute(text(f'PRAGMA index_info("{name}")')).fetchall()
        ]
        if cols == ["node_run_id", "call_id"]:
            return  # already enforced (constraint autoindex or a prior back-fill)
    try:
        conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_callchat_call "
            "ON call_chats(node_run_id, call_id)"
        ))
        conn.commit()
    except Exception:
        # Pre-existing duplicates block the unique index. Leave them for manual
        # cleanup rather than auto-delete at startup; roll back the failed DDL so
        # the connection stays usable for the rest of init.
        try:
            conn.rollback()
        except Exception:
            pass


def _strip_legacy_node_config_keys() -> None:
    """One-shot cleanup: remove obsolete keys from `node.config` on every row.

    `tools_enabled` was an LLM tool allow-list that's been removed in favour
    of trusting whatever the node's Python code passes to `ctx.agent`.
    Old rows can still carry it; strip it on startup so the orchestrator
    and frontend never see stale data.
    """
    from app import models

    LEGACY_KEYS = ("tools_enabled",)
    with SessionLocal() as session:
        rows = session.query(models.Node).all()
        changed = False
        for n in rows:
            cfg = n.config or {}
            if any(k in cfg for k in LEGACY_KEYS):
                new_cfg = {k: v for k, v in cfg.items() if k not in LEGACY_KEYS}
                n.config = new_cfg
                changed = True
        if changed:
            session.commit()


def init_db() -> None:
    from app import models  # noqa: F401  -- ensure models are registered

    Base.metadata.create_all(bind=engine)
    _ensure_columns()
    _strip_legacy_node_config_keys()
