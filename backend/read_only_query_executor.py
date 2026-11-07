"""
Strict read-only execution layer for assistant-generated SQL
============================================================

Defence in depth — every layer below is independent, so a bug in the
validator cannot turn into a write:

  1. The connection is opened with SQLite's URI `mode=ro` (SQLITE_OPEN_READONLY).
  2. `PRAGMA query_only = ON` makes the connection reject writes even if a
     statement slipped past the parser.
  3. A `set_authorizer` callback allows only SELECT + READ on the allowlisted
     table/column pairs + an explicit function allowlist. Writes, ATTACH,
     PRAGMA, transactions, temp tables and reads of `sqlite_master` /
     restricted columns return SQLITE_DENY — SQLite itself then refuses.
  4. `set_progress_handler` aborts a statement once the deadline passes
     (query timeout), and the caller only ever reads `max_rows + 1` rows.

Nothing here builds SQL from user text: statements and their bound values
arrive already validated from sql_validation_service.
"""

import re
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import quote

from database import DB_PATH, normkey

DEFAULT_MAX_ROWS = 200
MAX_MAX_ROWS = 1000
DEFAULT_TIMEOUT = 4.0

# Built-ins the assistant may use. Anything else (including load_extension,
# writefile, readfile, sqlite_* helpers) is denied by the authorizer.
ALLOWED_FUNCTIONS = {
    "count", "sum", "avg", "total", "min", "max", "round", "abs",
    "coalesce", "ifnull", "nullif", "lower", "upper", "trim", "ltrim",
    "rtrim", "length", "substr", "instr", "replace", "cast", "group_concat",
    "date", "strftime", "julianday", "distinct", "normkey",
}

_ACTIONS = {name: getattr(sqlite3, name, -1) for name in (
    "SQLITE_SELECT", "SQLITE_READ", "SQLITE_FUNCTION", "SQLITE_RECURSIVE",
    "SQLITE_OK", "SQLITE_DENY",
)}


class QueryRejected(Exception):
    """Raised when the authorizer/compiler refuses a statement."""


class QueryTimeout(Exception):
    """Raised when a statement exceeds its execution deadline."""


def _uri() -> str:
    return f"file:{quote(DB_PATH, safe='/')}?mode=ro"


def connect(allowed_tables: Sequence[str], allowed_columns: Dict[str, Sequence[str]]):
    """Read-only connection with the authorizer installed."""
    conn = sqlite3.connect(_uri(), uri=True, timeout=5.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        # Must run BEFORE the authorizer is installed (PRAGMA is denied after).
        conn.execute("PRAGMA query_only=ON")
        conn.execute("PRAGMA busy_timeout=2000")
    except sqlite3.Error:
        pass

    table_set = {t.lower() for t in allowed_tables}
    column_map = {t.lower(): {c.lower() for c in cols} for t, cols in allowed_columns.items()}

    def authorizer(action, arg1, arg2, dbname, trigger):  # noqa: ANN001
        if action == _ACTIONS["SQLITE_SELECT"]:
            return _ACTIONS["SQLITE_OK"]
        if action == _ACTIONS["SQLITE_RECURSIVE"]:
            return _ACTIONS["SQLITE_OK"]
        if action == _ACTIONS["SQLITE_READ"]:
            table = (arg1 or "").lower()
            column = (arg2 or "").lower()
            # Pseudo-reads with no table are expression / subquery-alias
            # columns (SQLite reports table="" for those), not real tables.
            if not table:
                return _ACTIONS["SQLITE_OK"]
            if table not in table_set:
                return _ACTIONS["SQLITE_DENY"]
            if column and column not in column_map.get(table, set()):
                return _ACTIONS["SQLITE_DENY"]
            return _ACTIONS["SQLITE_OK"]
        if action == _ACTIONS["SQLITE_FUNCTION"]:
            name = (arg2 or "").lower()
            return _ACTIONS["SQLITE_OK"] if name in ALLOWED_FUNCTIONS else _ACTIONS["SQLITE_DENY"]
        # Everything else — INSERT/UPDATE/DELETE/CREATE/DROP/ALTER/PRAGMA/
        # ATTACH/DETACH/TRANSACTION/SAVEPOINT/TEMP — is refused.
        return _ACTIONS["SQLITE_DENY"]

    conn.set_authorizer(authorizer)
    try:
        conn.create_function("normkey", 1, normkey, deterministic=True)
    except Exception:
        try:
            conn.create_function("normkey", 1, normkey)
        except Exception:
            pass
    return conn


def compile_check(sql: str, allowed_tables: Sequence[str],
                  allowed_columns: Dict[str, Sequence[str]],
                  params: Optional[Sequence[Any]] = None) -> Optional[str]:
    """Compile with EXPLAIN (no rows produced). Returns an error string or None.

    Compilation runs through the same authorizer, so unknown tables/columns
    and prohibited constructs fail here rather than at execution time. Bound
    values are supplied (or NULL-filled when the caller has none) because
    SQLite requires a binding count match even for EXPLAIN.
    """
    try:
        conn = connect(allowed_tables, allowed_columns)
    except sqlite3.Error as exc:  # pragma: no cover - connection failure
        return f"database unavailable: {exc.__class__.__name__}"
    binds = tuple(params) if params is not None else tuple([None] * sql.count("?"))
    try:
        conn.execute("EXPLAIN " + sql, binds).fetchall()
        return None
    except sqlite3.Error as exc:
        return str(exc)
    finally:
        conn.close()


def execute(sql: str, params: Optional[Sequence[Any]] = None, *,
            allowed_tables: Sequence[str],
            allowed_columns: Dict[str, Sequence[str]],
            max_rows: int = DEFAULT_MAX_ROWS,
            timeout: float = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    """Run one validated SELECT and return at most `max_rows` rows."""
    cap = max(1, min(int(max_rows or DEFAULT_MAX_ROWS), MAX_MAX_ROWS))
    deadline = time.monotonic() + max(0.5, float(timeout or DEFAULT_TIMEOUT))
    started = time.monotonic()
    conn = connect(allowed_tables, allowed_columns)
    try:
        conn.set_progress_handler(
            lambda: 1 if (time.monotonic() > deadline or _ABORT.is_set()) else 0,
            2000,
        )
        cursor = conn.execute(sql, tuple(params or ()))
        columns = [d[0] for d in (cursor.description or [])]
        raw = cursor.fetchmany(cap + 1)
        truncated = len(raw) > cap
        rows: List[List[Any]] = []
        for row in raw[:cap]:
            rows.append([_jsonable(row[i]) for i in range(len(columns))])
        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
            "limit": cap,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        }
    except sqlite3.OperationalError as exc:
        message = str(exc).lower()
        if "interrupted" in message or "progress" in message:
            raise QueryTimeout("the query exceeded its time limit") from None
        raise QueryRejected(_safe_error(exc)) from None
    except sqlite3.DatabaseError as exc:
        raise QueryRejected(_safe_error(exc)) from None
    finally:
        try:
            conn.set_progress_handler(None, 2000)
        except sqlite3.Error:
            pass
        conn.close()


def _jsonable(value: Any) -> Any:
    """SQLite values → JSON-safe primitives (bytes are never echoed raw)."""
    if value is None or isinstance(value, (int, float, str, bool)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    try:
        return str(value)
    except Exception:
        return None


def _safe_error(exc: Exception) -> str:
    """Error text with filesystem paths stripped (never leak the DB location)."""
    text = str(exc) or exc.__class__.__name__
    text = re.sub(r"(?:[A-Za-z]:)?[\\/][^\s'\"()]+", "<path>", text)
    return text[:240]


_ABORT = threading.Event()


def abort_all() -> None:
    """Test/shutdown hook: makes in-flight statements stop at the next tick."""
    _ABORT.set()
    threading.Timer(0.5, _ABORT.clear).start()
