"""SQLite connection, schema and consistent read snapshots."""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, Optional

from .bounds import StreetCounts, aggregate_records

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS households (
    household_id TEXT PRIMARY KEY,
    street TEXT NOT NULL,
    eligible INTEGER CHECK (eligible IS NULL OR eligible IN (0, 1)),
    received INTEGER CHECK (received IS NULL OR received IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (NOT (eligible = 0 AND received = 1))
);

CREATE INDEX IF NOT EXISTS idx_households_street ON households(street);

CREATE TABLE IF NOT EXISTS revision_log (
    revision INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    changed_fields INTEGER NOT NULL DEFAULT 0,
    detail TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

AGGREGATE_SQL = """
    SELECT
        street,
        COUNT(*) AS total,
        COALESCE(SUM(CASE WHEN received = 1 THEN 1 ELSE 0 END), 0) AS a,
        COALESCE(SUM(CASE WHEN eligible = 1 AND received = 0 THEN 1 ELSE 0 END), 0) AS b,
        COALESCE(SUM(CASE WHEN eligible = 1 AND received IS NULL THEN 1 ELSE 0 END), 0) AS d,
        COALESCE(SUM(CASE WHEN eligible IS NULL AND received = 0 THEN 1 ELSE 0 END), 0) AS f,
        COALESCE(SUM(CASE WHEN eligible IS NULL AND received IS NULL THEN 1 ELSE 0 END), 0) AS g,
        COALESCE(SUM(CASE WHEN eligible = 0 THEN 1 ELSE 0 END), 0) AS x
    FROM households
    GROUP BY street
    ORDER BY street
"""


def fetch_street_counts(conn: sqlite3.Connection) -> Dict[str, StreetCounts]:
    rows = conn.execute(AGGREGATE_SQL).fetchall()
    return aggregate_records([dict(row) for row in rows])



def database_path() -> Path:
    raw = os.environ.get("DATABASE_PATH")
    if raw:
        return Path(raw)
    return Path(__file__).resolve().parent.parent / "data" / "subsidy.db"


def connect(path: os.PathLike[str] | str | None = None) -> sqlite3.Connection:
    db_path = Path(path) if path is not None else database_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(db_path),
        timeout=30.0,
        isolation_level=None,  # explicit BEGIN IMMEDIATE / COMMIT
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.DatabaseError:
        # Some restricted filesystems do not support WAL; DELETE still works.
        conn.execute("PRAGMA journal_mode=DELETE")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(path: os.PathLike[str] | str | None = None) -> None:
    db_path = Path(path) if path is not None else database_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(connect(db_path)) as conn:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO meta(key, value) VALUES ('dataset_revision', '0')"
        )


def get_meta(conn: sqlite3.Connection, key: str, default: Optional[str] = None) -> Optional[str]:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return None if row is None else row["value"]


def get_revision(conn: sqlite3.Connection) -> int:
    value = get_meta(conn, "dataset_revision", "0") or "0"
    try:
        return int(value)
    except ValueError:
        return 0


def get_district_total(conn: sqlite3.Connection) -> Optional[int]:
    value = get_meta(conn, "district_eligible_total")
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def set_meta(conn: sqlite3.Connection, key: str, value: Any) -> None:
    conn.execute(
        """
        INSERT INTO meta(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (key, str(value)),
    )


def bump_revision(conn: sqlite3.Connection, kind: str, changed_fields: int, detail: str = "") -> int:
    current = get_revision(conn)
    new_revision = current + 1
    set_meta(conn, "dataset_revision", new_revision)
    conn.execute(
        "INSERT INTO revision_log(revision, kind, changed_fields, detail) VALUES (?, ?, ?, ?)",
        (new_revision, kind, changed_fields, detail),
    )
    return new_revision


def take_snapshot(path: os.PathLike[str] | str | None = None) -> Dict[str, Any]:
    """Read one revision/total/aggregate snapshot and release the database.

    Calculation happens after this function returns, so a long bounds
    computation never holds a SQLite read transaction or blocks writers.
    """

    db_path = Path(path) if path is not None else database_path()
    with closing(connect(db_path)) as conn:
        conn.execute("BEGIN")
        try:
            revision = get_revision(conn)
            total = get_district_total(conn)
            streets = fetch_street_counts(conn)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    return {"revision": revision, "district_total": total, "streets": streets}


def dataset_status(path: os.PathLike[str] | str | None = None) -> Dict[str, Any]:
    db_path = Path(path) if path is not None else database_path()
    if not db_path.exists():
        return {
            "initialized": False,
            "has_data": False,
            "revision": 0,
            "district_total": None,
            "household_count": 0,
            "streets": [],
        }

    with closing(connect(db_path)) as conn:
        conn.execute("BEGIN")
        try:
            revision = get_revision(conn)
            total = get_district_total(conn)
            household_count = conn.execute("SELECT COUNT(*) FROM households").fetchone()[0]
            street_rows = conn.execute(
                "SELECT street, COUNT(*) AS n FROM households GROUP BY street ORDER BY street"
            ).fetchall()
            eligible_yes = conn.execute(
                "SELECT COUNT(*) FROM households WHERE eligible=1"
            ).fetchone()[0]
            eligible_no = conn.execute(
                "SELECT COUNT(*) FROM households WHERE eligible=0"
            ).fetchone()[0]
            eligible_blank = conn.execute(
                "SELECT COUNT(*) FROM households WHERE eligible IS NULL"
            ).fetchone()[0]
            received_yes = conn.execute(
                "SELECT COUNT(*) FROM households WHERE received=1"
            ).fetchone()[0]
            received_no = conn.execute(
                "SELECT COUNT(*) FROM households WHERE received=0"
            ).fetchone()[0]
            received_blank = conn.execute(
                "SELECT COUNT(*) FROM households WHERE received IS NULL"
            ).fetchone()[0]
            street_counts = fetch_street_counts(conn) if household_count else {}
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    feasible = None
    minimum_eligible = maximum_eligible = None
    if street_counts and total is not None:
        minimum_eligible = sum(s.p for s in street_counts.values())
        maximum_eligible = sum(s.u for s in street_counts.values())
        feasible = minimum_eligible <= total <= maximum_eligible

    return {
        "initialized": True,
        "has_data": household_count > 0,
        "revision": revision,
        "district_total": total,
        "household_count": household_count,
        "streets": [row["street"] for row in street_rows],
        "street_counts": [dict(row) for row in street_rows],
        "eligible_yes": eligible_yes,
        "eligible_no": eligible_no,
        "eligible_blank": eligible_blank,
        "received_yes": received_yes,
        "received_no": received_no,
        "received_blank": received_blank,
        "minimum_eligible": minimum_eligible,
        "maximum_eligible": maximum_eligible,
        "feasible": feasible,
    }


def list_households(
    path: os.PathLike[str] | str | None = None,
    street: Optional[str] = None,
    limit: Optional[int] = None,
) -> list[sqlite3.Row]:
    with closing(connect(path)) as conn:
        if street:
            query = "SELECT * FROM households WHERE street=? ORDER BY household_id"
            params: tuple[Any, ...] = (street,)
        else:
            query = "SELECT * FROM households ORDER BY household_id"
            params = ()
        if limit is not None:
            query += " LIMIT ?"
            params = params + (int(limit),)
        return list(conn.execute(query, params).fetchall())
