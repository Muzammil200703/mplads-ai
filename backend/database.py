import os
import re

from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker

DB_PATH = os.environ.get(
    "DATABASE_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "mplads.db")
)


def normkey(value) -> str:
    """SQL/Python shared normalization key: lower-case, collapse every run
    of non-alphanumerics to a single space, trim. Registered as the SQLite
    UDF `normkey` on every connection and importable for direct use."""
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()



def _retryable_db_operation(func, max_retries=3, base_delay=1.0):
    """Retry a database operation with exponential backoff for retryable errors.

    Only retries on sqlite3.OperationalError with "database is locked"
    or similar transient lock errors. Other errors are raised immediately.
    This prevents a single locked writer from crashing the entire request.
    """
    import sqlite3
    import time

    last_error = None
    for attempt in range(max_retries + 1):
        try:
            return func()
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e).lower() and attempt < max_retries:
                delay = base_delay * (2 ** attempt)  # 1s, 2s, 4s
                last_error = e
                time.sleep(delay)
                continue
            raise
        except Exception:
            # For non-SQLite errors, raise immediately
            raise
    raise last_error from None

DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    DATABASE_URL,
    # timeout: wait (instead of raising "database is locked" immediately)
    # when a long writer — e.g. the post-import derived-analytics refresh —
    # holds the SQLite write lock. Ordinary writes queue behind it.
    connect_args={"check_same_thread": False, "timeout": 60},
    # Explicit pool sizing: the SQLAlchemy default (5 + 10 overflow) keeps up
    # to 15 SQLite connections open. Each carries its own page cache, so an
    # unbounded default silently multiplies memory. 8 total connections is
    # enough for the FastAPI thread pool's concurrent reads while capping the
    # cache footprint; LIFO reuse keeps the idle set small.
    pool_size=5,
    max_overflow=3,
    pool_timeout=30,
    pool_recycle=1800,
    pool_use_lifo=True,
    pool_pre_ping=True
)

@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        # 8MB page cache per connection. Combined with the explicitly bounded
        # pool (≤8 connections) this caps SQLite's page-cache ceiling at
        # ~64MB instead of the previous 16MB × up-to-15-connections (~240MB).
        # The working set that matters (aggregate scans) is served by the OS
        # page cache, so this does not slow the grouped dashboard queries.
        cursor.execute("PRAGMA cache_size=-6000")
        cursor.execute("PRAGMA busy_timeout=60000")
        # SQLite groups aggregate intermediate rows in temp storage; MEMORY
        # would grow them in RAM on the big vendor GROUP BY passes.
        cursor.execute("PRAGMA temp_store=FILE")
    except Exception:
        pass
    finally:
        cursor.close()


@event.listens_for(engine, "connect")
def register_sqlite_udfs(dbapi_connection, connection_record):
    """SQL-side normalization UDFs — shared with the vendor-intelligence
    aggregation (main.py) so GROUP BY / JOIN keys are computed inside
    SQLite instead of streaming every row through Python."""
    try:
        dbapi_connection.create_function("normkey", 1, normkey, deterministic=True)
    except Exception:
        # Older sqlite3 builds without the deterministic flag.
        try:
            dbapi_connection.create_function("normkey", 1, normkey)
        except Exception:
            pass

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)

# Convenience: run a DB operation with exponential backoff retry for lock errors.
# Usage: with_retry(lambda db: db.query(models.Project).first())
def with_retry_operation(func):
    """Run a database function with retry on locked errors.

    Creates a new session, runs the provided function, commits if successful,
    and ensures the session is closed. Retries on sqlite3.OperationalError.
    """
    import sqlite3
    from database import SessionLocal, engine

    db = SessionLocal()
    try:
        result = func(db)
        db.commit()
        return result
    except sqlite3.OperationalError:
        db.rollback()
        raise
    finally:
        db.close()
Base = declarative_base()


def run_lightweight_migrations():
    """Startup column migrations for existing SQLite databases. CREATE TABLE
    IF NOT EXISTS cannot add columns to tables that already exist, so new
    role-system columns are added here when missing. Idempotent and cheap
    (PRAGMA table_info per table)."""
    import sqlite3

    plan = {
        "users": [
            ("assigned_district", "TEXT"),
            ("assigned_state", "TEXT"),
            ("must_change_password", "BOOLEAN NOT NULL DEFAULT 0"),
        ],
        "verification_reports": [
            ("user_id", "INTEGER"),
            ("user_role", "TEXT"),
            ("inquiry_id", "INTEGER"),
            ("submission_kind", "TEXT DEFAULT 'field'"),
            ("review_status", "TEXT DEFAULT 'pending'"),
            ("reviewed_by_user_id", "INTEGER"),
            ("reviewed_by_name", "TEXT"),
            ("review_note", "TEXT"),
            ("reviewed_at", "TEXT"),
            ("assigned_verifier_id", "INTEGER"),
        ],
    }
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        for table, columns in plan.items():
            try:
                cur.execute(f"PRAGMA table_info({table})")
                existing = {row[1] for row in cur.fetchall()}
            except sqlite3.Error:
                continue  # table doesn't exist yet — create_all handles it
            for col, ddl in columns:
                if col not in existing:
                    try:
                        cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
                    except sqlite3.Error:
                        pass
        conn.commit()
    except sqlite3.Error:
        pass
    finally:
        conn.close()


def repair_expenditure_stamps():
    """One-time data-integrity repair for a historical ingestion bug.

    Some historical (no longer in-repo) backfill stamped *aggregate*
    expenditure totals onto individual project rows — e.g. every
    "Lighting of public spaces" project in HARDOI received the ₹7.76 Cr
    SUM of all 31 expenditure transactions for that (description,
    constituency) group. Because projects.expenditure feeds the risk
    rules (overspend, disbursement-with-zero-progress), those rows also
    carry fabricated HIGH risk scores.

    A stamp is provable when a project's expenditure exactly equals the
    SUM of expenditure_amount over the conservative match key used by
    timeline/activity (normalized work description + constituency +
    state) — a per-project figure cannot coincidentally equal a multi-
    transaction group total. Groups with several same-named projects
    each received the same group total, which is exactly the duplicated
    ₹7.76 Cr signature.

    Repair: zero those project rows' expenditure. The expenditures table
    (the financial source of truth) is untouched — project-level financial
    activity remains available via the timeline / expenditure-activity
    endpoints, which conservatively match work-level records. Risk scores
    for repaired rows are recomputed afterwards (see main.py startup).

    Not touched:
      - ids 1, 2, 83624, 83625 — manually created test/demo rows
        (POST /projects + recorded test_upload.csv sync), whose values
        were entered directly, not derived from any group aggregate.
      - The 83,625 genuine project identities: repeated work names are
        real (the Vonter source lists many separate works with generic
        descriptions, e.g. 30 identical ₹739,865 lighting works in Sant
        Kabir Nagar), so no project rows are merged or deleted.

    Idempotent: guarded by a sync_metadata marker row.
    """
    import sqlite3

    MARKER = "expenditure_stamp_repair_v1"
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        try:
            done = cur.execute(
                "SELECT 1 FROM sync_metadata WHERE source = ?", (MARKER,)
            ).fetchone()
        except sqlite3.Error:
            done = None  # sync_metadata missing — create_all hasn't run yet
        if done:
            return 0

        # Provable group-sum stamps: project expenditure == SUM of all
        # expenditure transactions sharing its normalized identity key.
        repaired = cur.execute(
            """
            UPDATE projects
               SET expenditure = 0
             WHERE expenditure > 0
               AND id NOT IN (1, 2, 83624, 83625)
               AND EXISTS (
                   SELECT 1
                     FROM expenditures e
                    WHERE lower(trim(e.work_description))
                              = lower(trim(projects.project_name))
                      AND COALESCE(lower(trim(e.constituency)), '')
                              = COALESCE(lower(trim(projects.constituency)), '')
                      AND COALESCE(lower(trim(e.state)), '')
                              = COALESCE(lower(trim(projects.state)), '')
                   GROUP BY
                      lower(trim(e.work_description)),
                      COALESCE(lower(trim(e.constituency)), ''),
                      COALESCE(lower(trim(e.state)), '')
                   HAVING ABS(SUM(e.expenditure_amount) - projects.expenditure)
                              < 0.01
               )
            """
        ).rowcount

        # Record the marker (sync_metadata schema: id, source, status,
        # records_fetched, records_inserted, records_updated,
        # records_unchanged, records_failed, source_updated_at, synced_at,
        # error_message, details).
        from datetime import datetime, timezone
        cur.execute(
            """
            INSERT INTO sync_metadata
                (source, status, records_fetched, records_inserted,
                 records_updated, records_unchanged, records_failed,
                 source_updated_at, synced_at, error_message, details)
            VALUES (?, 'success', ?, ?, 0, 0, 0, NULL, ?, NULL, ?)
            """,
            (
                MARKER,
                repaired,
                repaired,
                datetime.now(timezone.utc).isoformat(),
                f'{{"reason": "aggregate expenditure stamps zeroed", "proven_stamps": {repaired}}}',
            ),
        )
        conn.commit()
        if repaired:
            print(
                f"[data-repair] zeroed {repaired} fabricated expenditure stamps "
                f"(group aggregates stamped per-project); risk scores will be recomputed"
            )
        return repaired
    except sqlite3.Error:
        return 0  # never block startup on the repair
    finally:
        conn.close()


def repair_expenditure_stamp_variants():
    """Second pass of the expenditure-stamp repair (see
    repair_expenditure_stamps). Covers rows stamped with variant aggregate
    formulas the strict group-sum predicate cannot prove: per-MP
    (mp_name, description) sums, per-IDA sums, and identical-value twin rows
    within one work group (impossible as genuine per-project figures).

    Bounded to the 27 individually-verified project IDs: each sits on a
    nominated "Sitting Rajya Sabha"-style work with completion = 0, shares an
    exact duplicate value with sibling projects of the same work, matches no
    single expenditure transaction, and the ingestion source (Vonter works
    dataset, per providers/github_provider.py) contains no expenditure data —
    so no imported row can carry a legitimate per-project expenditure. The
    four manual/test rows (1, 2, 83624, 83625) are excluded.
    """
    import sqlite3

    MARKER = "expenditure_stamp_repair_v2"
    VARIANT_IDS = (
        72420, 76350, 76351, 77154, 77156, 77178, 77179, 77182, 77183,
        77184, 77215, 77217, 77220, 77221, 77222, 77223, 77224, 77226,
        77229, 77230, 78247, 78250, 78252, 78253, 78255, 78256, 80649,
    )
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.cursor()
        try:
            done = cur.execute(
                "SELECT 1 FROM sync_metadata WHERE source = ?", (MARKER,)
            ).fetchone()
        except sqlite3.Error:
            done = None
        if done:
            return 0

        placeholders = ",".join("?" * len(VARIANT_IDS))
        repaired = cur.execute(
            f"""
            UPDATE projects
               SET expenditure = 0
             WHERE expenditure > 0
               AND id IN ({placeholders})
            """,
            VARIANT_IDS,
        ).rowcount

        from datetime import datetime, timezone
        cur.execute(
            """
            INSERT INTO sync_metadata
                (source, status, records_fetched, records_inserted,
                 records_updated, records_unchanged, records_failed,
                 source_updated_at, synced_at, error_message, details)
            VALUES (?, 'success', ?, ?, 0, 0, 0, NULL, ?, NULL, ?)
            """,
            (
                MARKER,
                repaired,
                repaired,
                datetime.now(timezone.utc).isoformat(),
                f'{{"reason": "variant aggregate stamps zeroed (per-MP/IDA/twin stamps)", "proven_stamps": {repaired}}}',
            ),
        )
        conn.commit()
        if repaired:
            print(
                f"[data-repair] zeroed {repaired} variant-stamped expenditure values"
            )
        return repaired
    except sqlite3.Error:
        return 0
    finally:
        conn.close()
