"""
Schema-aware context for the assistant's natural-language query engine
======================================================================

The model (or the deterministic planner) is given ONLY the tables and
columns that actually exist in this database and that the assistant is
allowed to read. Nothing here is hand-invented: every table and column is
verified against the live SQLite schema via PRAGMA table_info() at build
time, so a renamed column disappears from the prompt instead of producing
SQL that fails.

Three jobs:
  * `table_allowlist()` / `column_allowlist()` — the exact read surface the
    read-only executor enforces (authorizer) and the validator checks.
  * `value_dictionary()` — the real state / constituency / house / status
    values used to resolve entities in a question (never guess a name).
  * `build_context(question)` — a compact schema excerpt (≤ 4 tables) plus
    business definitions, for the LLM prompt.

Cached for `_TTL` seconds, so a chatty conversation does not re-introspect
SQLite on every turn.
"""

import threading
import time
from typing import Any, Dict, Iterable, List, Optional

from database import DB_PATH  # noqa: F401  (kept for callers/tests)
import sqlite3

# ── Allowed read surface ────────────────────────────────────────────────
# min_role: the assistant role ladder (auth.ROLE_RANK) required to read the
# table. Import/sync bookkeeping is administrative data, so it is admin-only;
# everything else mirrors the existing analytics surface (analyst and above,
# public reference totals for everyone).
TABLE_REGISTRY: Dict[str, Dict[str, Any]] = {
    "projects": {
        "description": (
            "Project catalogue — one row per MPLADS project identity. "
            "house is 'Lok Sabha' / 'Rajya Sabha' where attributed; NULL means "
            "unattributed and must never be guessed."
        ),
        "min_role": "public",
        "columns": {
            "id": "INTEGER primary key (project identity)",
            "project_name": "TEXT work name",
            "state": "TEXT",
            "district": "TEXT",
            "constituency": "TEXT",
            "project_type": "TEXT category",
            "sanctioned_amount": "REAL rupees sanctioned",
            "expenditure": "REAL legacy stamp — mostly zeroed, NOT authoritative spend",
            "completion_percentage": "REAL 0-100 physical completion",
            "status": "TEXT 'Completed' | 'Ongoing'",
            "fy": "TEXT financial year 'YYYY-YY'",
            "house": "TEXT 'Lok Sabha' | 'Rajya Sabha' | NULL",
        },
    },
    "project_rec_info": {
        "description": (
            "Derived per-project facts built from the payment ledger by "
            "conservative work-key linkage. linked_expenditure is the "
            "authoritative per-project spend; work_key_expenditure is the "
            "work-level total (valid only when work_key_rows = 1)."
        ),
        "min_role": "public",
        "columns": {
            "project_id": "INTEGER → projects.id (1:1)",
            "recommendation_date": "TEXT ISO date or NULL",
            "recommended_by": "TEXT MP name",
            "approx_start_date": "TEXT ISO date or NULL",
            "has_expenditure": "BOOLEAN",
            "start_status": "TEXT valid | pre_recommendation | no_expenditure | ambiguous_shared_work",
            "linked_expenditure": "REAL authoritative linked spend (0 when unlinked/ambiguous)",
            "linked_tx_count": "INTEGER linked payment rows",
            "first_expenditure_date": "TEXT",
            "latest_expenditure_date": "TEXT",
            "work_key": "TEXT normalized 'name|constituency|state'",
            "work_key_expenditure": "REAL work-level ledger total",
            "work_key_rows": "INTEGER catalogue rows sharing the work key",
        },
    },
    "expenditures": {
        "description": (
            "Payment ledger — one row per recorded payment. Use for vendor, "
            "payment status and payment-date analysis. Do not sum this table "
            "and the project aggregates together."
        ),
        "min_role": "public",
        "columns": {
            "id": "INTEGER",
            "mp_name": "TEXT",
            "constituency": "TEXT",
            "state": "TEXT",
            "house": "TEXT",
            "work_description": "TEXT work name (joins projects via normkey)",
            "vendor": "TEXT vendor / executing agency",
            "ida": "TEXT executing division/agency code",
            "expenditure_amount": "REAL rupees paid",
            "expenditure_date": "TEXT ISO date, may be empty",
            "payment_status": "TEXT",
        },
    },
    "recommended_works": {
        "description": "Recommended works (sanction stage) with MP, category and recommendation date.",
        "min_role": "public",
        "columns": {
            "id": "INTEGER",
            "work_id": "INTEGER eSAKSHI work id",
            "work_description": "TEXT",
            "category": "TEXT",
            "mp_name": "TEXT",
            "constituency": "TEXT",
            "state": "TEXT",
            "house": "TEXT",
            "recommended_amount": "REAL rupees",
            "recommendation_date": "TEXT ISO date",
            "has_images": "BOOLEAN",
            "ida": "TEXT",
            "source": "TEXT 'esakshi' official batch | 'github_legacy'",
        },
    },
    "completed_works": {
        "description": "Completed works with final amount, completion date and citizen rating.",
        "min_role": "public",
        "columns": {
            "id": "INTEGER",
            "work_id": "INTEGER eSAKSHI work id",
            "work_description": "TEXT",
            "category": "TEXT",
            "mp_name": "TEXT",
            "constituency": "TEXT",
            "state": "TEXT",
            "house": "TEXT",
            "final_amount": "REAL rupees",
            "completed_date": "TEXT ISO date",
            "average_rating": "REAL",
            "source": "TEXT",
        },
    },
    "mp_summaries": {
        "description": "One row per MP: allocation, expenditure, utilization and completion rate.",
        "min_role": "public",
        "columns": {
            "mp_name": "TEXT",
            "constituency": "TEXT",
            "state": "TEXT",
            "house": "TEXT",
            "allocated_amount": "REAL rupees",
            "total_expenditure": "REAL rupees",
            "utilization_percentage": "REAL 0-100",
            "completed_works": "INTEGER",
            "recommended_works": "INTEGER",
            "completion_rate_percentage": "REAL",
            "unspent_amount": "REAL",
            "transaction_count": "INTEGER",
            "successful_payments": "INTEGER",
            "pending_payments": "INTEGER",
            "average_rating": "REAL",
        },
    },
    "risk_scores": {
        "description": "Rule/ML risk result per project (1:1 with projects).",
        "min_role": "public",
        "columns": {
            "project_id": "INTEGER → projects.id",
            "ml_anomaly": "BOOLEAN",
            "ml_score": "REAL",
            "risk_score": "INTEGER 0-100",
            "risk_level": "TEXT 'High' | 'Medium' | 'Low' | 'None'",
        },
    },
    "reference_dashboard_metrics": {
        "description": (
            "Official historical snapshot totals (house level), captured from "
            "the eSAKSHI dashboard. This is the only stored historical "
            "aggregate; it is not reconstructible from current project rows."
        ),
        "min_role": "public",
        "columns": {
            "house": "TEXT",
            "source": "TEXT provenance of the snapshot",
            "snapshot_label": "TEXT human snapshot label",
            "total_allocated": "REAL rupees",
            "total_expenditure": "REAL rupees",
            "fund_utilization_percentage": "REAL",
            "expenditure_rate_percentage": "REAL",
            "total_mps": "INTEGER",
            "works_completed": "INTEGER",
            "completed_work_value": "REAL",
            "works_pending": "INTEGER",
            "ongoing_work_payments": "REAL",
        },
    },
    "import_batches": {
        "description": (
            "Admin CSV import history — one row per upload/commit. Used for "
            "'added in the latest import' questions."
        ),
        "min_role": "admin",
        "columns": {
            "id": "INTEGER",
            "filename": "TEXT",
            "dataset_type": "TEXT",
            "status": "TEXT preview | importing | success | failed | rolled_back | discarded",
            "total_rows": "INTEGER",
            "valid_rows": "INTEGER",
            "new_rows": "INTEGER (rows added)",
            "update_rows": "INTEGER (rows updated)",
            "unchanged_rows": "INTEGER",
            "duplicate_rows": "INTEGER",
            "rejected_rows": "INTEGER",
            "house_coverage": "TEXT",
            "uploaded_at": "TEXT ISO timestamp",
            "committed_at": "TEXT ISO timestamp",
            "dataset_version": "INTEGER",
        },
    },
    "import_changes": {
        "description": (
            "Per-row before-state captured during an import commit (rollback "
            "journal). The only stored per-row history for project updates."
        ),
        "min_role": "admin",
        "columns": {
            "batch_id": "INTEGER → import_batches.id",
            "table_name": "TEXT",
            "record_pk": "INTEGER row id in the source table",
            "change_type": "TEXT 'insert' | 'update'",
            "before_json": "TEXT JSON of previous column values (updates only)",
        },
    },
    "sync_metadata": {
        "description": "Data-sync run bookkeeping (when data was refreshed and how many rows changed).",
        "min_role": "admin",
        "columns": {
            "source": "TEXT",
            "status": "TEXT",
            "records_fetched": "INTEGER",
            "records_inserted": "INTEGER",
            "records_updated": "INTEGER",
            "records_unchanged": "INTEGER",
            "records_failed": "INTEGER",
            "synced_at": "TEXT ISO timestamp",
        },
    },
}

BUSINESS_DEFINITIONS = (
    "Business definitions (authoritative):\n"
    "• Project identity = projects.id (one catalogue row). Never invent an id.\n"
    "• Authoritative per-project spend = project_rec_info.linked_expenditure "
    "(payments linked by normalized work key). projects.expenditure is a "
    "legacy stamp that was zeroed where it held fabricated group totals — do "
    "NOT use it for spend analysis.\n"
    "• Work-key linkage is normkey(work_description) + '|' + normkey(constituency) "
    "+ '|' + normkey(state); normkey() lower-cases and collapses non-alphanumerics.\n"
    "• Utilization % = spend / sanctioned_amount × 100 (guard sanctioned_amount > 0).\n"
    "• house NULL = unattributed. Never assign a house to a NULL row.\n"
    "• risk_level is 'High' | 'Medium' | 'Low' | 'None' exactly.\n"
    "• status is 'Completed' | 'Ongoing' exactly.\n"
    "• The payment ledger (expenditures) and the project aggregates measure "
    "different populations; never add them together in one total.\n"
    "• Historical data: reference_dashboard_metrics holds official house-level "
    "snapshot totals captured on a date (snapshot_label/source). There is NO "
    "per-project historical snapshot table; per-row history only exists in "
    "import_changes.before_json for imported updates. Historical values must "
    "come from those stored rows, never be reconstructed from current data.\n"
)

_KEYWORD_TABLES = (
    (r"\b(vendor|contractor|supplier|payment|paid|ledger)\b", ("expenditures",)),
    (r"\b(risk|anomal|flag|suspicious)\b", ("risk_scores", "projects")),
    (r"\b(mp|member of parliament|mps)\b", ("mp_summaries", "recommended_works")),
    (r"\b(recommend|sanction(ed)? works?)\b", ("recommended_works", "projects")),
    (r"\b(completed?|finished|completion|rating)\b", ("completed_works", "projects")),
    (r"\b(import|batch|upload|sync)\b", ("import_batches", "sync_metadata", "import_changes")),
    (r"\b(snapshot|historical|history|previous import|earlier import|checkpoint)\b",
     ("reference_dashboard_metrics", "import_batches", "import_changes")),
    (r"\b(expenditure|spend|spent|spending|utilisation|utilization|fund|funds|overspend|cost)\b",
     ("project_rec_info", "expenditures", "projects")),
    (r"\b(project|work|constituency|district|state|completion_percentage)\b", ("projects", "project_rec_info")),
)

_CACHE_LOCK = threading.Lock()
_CACHE: Dict[str, Any] = {"ts": 0.0}
_TTL = 300.0


# ── live introspection ──────────────────────────────────────────────────

def _connect():
    return sqlite3.connect(DB_PATH, timeout=10)


def _introspect() -> Dict[str, Any]:
    conn = _connect()
    try:
        cur = conn.cursor()
        tables: Dict[str, Dict[str, Any]] = {}
        for table, meta in TABLE_REGISTRY.items():
            try:
                info = cur.execute(f"PRAGMA table_info({table})").fetchall()
            except sqlite3.Error:
                continue
            if not info:
                continue
            live = {row[1]: (row[2] or "").upper() for row in info}
            cols = {c: d for c, d in meta["columns"].items() if c in live}
            if not cols:
                continue
            try:
                count = int(cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            except sqlite3.Error:
                count = 0
            tables[table] = {
                "description": meta["description"],
                "min_role": meta["min_role"],
                "columns": cols,
                "types": {c: live[c] for c in cols},
                "rows": count,
            }
        values = _values(cur)
        return {"tables": tables, "values": values, "ts": time.time()}
    finally:
        conn.close()


def _values(cur) -> Dict[str, Any]:
    out: Dict[str, Any] = {"states": [], "constituencies": [], "houses": [],
                           "statuses": [], "risk_levels": [], "fys": [], "snapshots": []}
    probes = (
        ("states", "SELECT state, COUNT(*) FROM projects WHERE state IS NOT NULL AND TRIM(state) <> '' GROUP BY state ORDER BY 2 DESC", True),
        ("constituencies", "SELECT constituency, COUNT(*) FROM projects WHERE constituency IS NOT NULL AND TRIM(constituency) <> '' GROUP BY constituency ORDER BY 2 DESC LIMIT 400", True),
        ("houses", "SELECT DISTINCT house FROM projects WHERE house IS NOT NULL", False),
        ("statuses", "SELECT DISTINCT status FROM projects WHERE status IS NOT NULL", False),
        ("risk_levels", "SELECT DISTINCT risk_level FROM risk_scores WHERE risk_level IS NOT NULL", False),
        ("fys", "SELECT DISTINCT fy FROM projects WHERE fy IS NOT NULL", False),
    )
    for key, sql, counted in probes:
        try:
            rows = cur.execute(sql).fetchall()
        except sqlite3.Error:
            rows = []
        out[key] = [(str(r[0]).strip(), int(r[1])) for r in rows] if counted else [str(r[0]).strip() for r in rows]
    try:
        out["snapshots"] = [
            {"label": str(r[0]), "house": str(r[1]), "source": str(r[2])}
            for r in cur.execute(
                "SELECT snapshot_label, house, source FROM reference_dashboard_metrics"
            ).fetchall()
        ]
    except sqlite3.Error:
        out["snapshots"] = []
    return out


def _snapshot() -> Dict[str, Any]:
    now = time.time()
    with _CACHE_LOCK:
        if _CACHE.get("ts") and now - float(_CACHE["ts"]) < _TTL:
            return _CACHE
    fresh = _introspect()
    with _CACHE_LOCK:
        _CACHE.clear()
        _CACHE.update(fresh)
    return _CACHE


def invalidate_cache() -> None:
    """Called after an import so newly added columns/tables become visible."""
    with _CACHE_LOCK:
        _CACHE.clear()
        _CACHE["ts"] = 0.0


def tables() -> Dict[str, Dict[str, Any]]:
    return _snapshot()["tables"]


def value_dictionary() -> Dict[str, Any]:
    return _snapshot()["values"]


def table_allowlist(role: Optional[str] = None) -> List[str]:
    """Tables readable at this role. Import bookkeeping is admin-only."""
    from auth import ROLE_RANK  # local import keeps this module import-light

    rank = ROLE_RANK.get(role or "public", 0) if role else 0
    for name, meta in tables().items():
        meta["_rank"] = ROLE_RANK.get(meta["min_role"], 0)
    return [
        name for name, meta in tables().items()
        if rank >= ROLE_RANK.get(meta["min_role"], 0)
    ]


def column_allowlist(role: Optional[str] = None) -> Dict[str, List[str]]:
    allowed = set(table_allowlist(role))
    return {
        name: list(meta["columns"].keys())
        for name, meta in tables().items()
        if name in allowed
    }


def can_read_table(table: str, role: Optional[str] = None) -> bool:
    return table in table_allowlist(role)


# ── relevant-context builder ────────────────────────────────────────────

def relevant_tables(question: str, role: Optional[str] = None, limit: int = 4) -> List[str]:
    import re

    q = (question or "").lower()
    allowed = set(table_allowlist(role))
    picked: List[str] = []
    for pattern, names in _KEYWORD_TABLES:
        if re.search(pattern, q):
            for name in names:
                if name in allowed and name not in picked:
                    picked.append(name)

    # Join targets: a project-derived table is useless without its anchors.
    if "project_rec_info" in picked and "projects" not in picked:
        picked.insert(0, "projects")
    if "risk_scores" in picked and "projects" not in picked:
        picked.insert(0, "projects")
    if not picked:
        picked = [t for t in ("projects", "project_rec_info") if t in allowed]
    return [t for t in picked if t in allowed][:limit]


def _table_block(name: str, meta: Dict[str, Any]) -> str:
    cols = ", ".join(f"{c} {meta['types'].get(c, '')}".strip() for c in meta["columns"])
    rows = meta.get("rows")
    count = f" — {rows:,} rows" if isinstance(rows, int) else ""
    return f"- {name}({cols}){count}\n  {meta['description']}"


def build_context(question: str, role: Optional[str] = None) -> Dict[str, Any]:
    """Compact schema excerpt for the prompt: relevant tables + definitions."""
    names = relevant_tables(question, role)
    all_tables = tables()
    blocks = [_table_block(n, all_tables[n]) for n in names if n in all_tables]
    values = value_dictionary()
    states = ", ".join(s for s, _ in values["states"][:40])
    houses = ", ".join(values["houses"]) or "Lok Sabha, Rajya Sabha"
    statuses = ", ".join(values["statuses"]) or "Completed, Ongoing"
    text = (
        "SQLite (read-only, SELECT only). Dialect notes: use normkey(col) for "
        "normalized work-key joins; LIMIT is enforced; no PRAGMA/ATTACH/writes.\n"
        "Relationships: projects.id = project_rec_info.project_id (1:1); "
        "projects.id = risk_scores.project_id (1:1); expenditures / "
        "recommended_works / completed_works reach a project through the "
        "normalized work key (normkey(work_description) = normkey(project_name) "
        "AND same constituency/state) or work_id = recommended_works.work_id; "
        "import_changes.batch_id = import_batches.id; mp_summaries is keyed by "
        "(mp_name, constituency, house).\n\n"
        "Tables:\n" + "\n".join(blocks) + "\n\n"
        f"Known values — house: {houses}; status: {statuses}; "
        f"risk_level: High, Medium, Low, None; states: {states}.\n\n"
        + BUSINESS_DEFINITIONS
    )
    return {"text": text, "tables": names, "values": values}
