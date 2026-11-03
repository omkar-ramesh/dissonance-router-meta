"""SQLite connection helpers."""
import sqlite3
from pathlib import Path
from typing import Optional

from src import config

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def get_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Open a connection with dict-like rows, creating the parent folder if needed."""
    path = db_path or config.DB_PATH
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


# Columns added after a table first shipped: CREATE TABLE IF NOT EXISTS won't add them to an old database.
ADDED_COLUMNS = {
    "interactions": {"conflict_dims": "TEXT NOT NULL DEFAULT ''", "memory_state": "TEXT NOT NULL DEFAULT ''"},
    "conversations": {"unresolved": "TEXT NOT NULL DEFAULT ''", "raw_bytes": "INTEGER NOT NULL DEFAULT 0",
                      "settled_turns": "INTEGER NOT NULL DEFAULT 0", "calm_turns": "INTEGER NOT NULL DEFAULT 0"},
}


def init_db(db_path: Optional[str] = None) -> None:
    """Create all tables if they don't exist yet, and add any newer columns to old ones."""
    conn = get_connection(db_path)
    try:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        for table, cols in ADDED_COLUMNS.items():
            have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols.items():
                if name not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        conn.commit()
    finally:
        conn.close()
