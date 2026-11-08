"""
Natural-language database querying for the MPLADS AI assistant
==============================================================

One conversational analyst, not a second chatbot: this module is called from
the EXISTING `/assistant/ask` chain (assistant.py) and reuses the existing
session id, role checks and structured response contract.

Pipeline for every question:

    question → intent & entities → relevant schema → SQL
             → strict validator → read-only executor → real rows
             → grounded explanation → table / chart / findings / limitations

SQL is produced in one of two ways:

  * a deterministic planner (works offline, always available) that builds
    parameterised SQL from a closed set of query families over the real
    schema, and
  * an optional LLM (`llm_client`) used ONLY for questions outside those
    families. Its output is validated exactly like the planner's and is
    never trusted.

Hard rules enforced here:
  * no SQL is executed unless `sql_validation_service` accepts it,
  * rows come from the read-only executor (authorizer + timeout + row cap),
  * every number in an answer is read from the returned rows,
  * historical answers come from stored snapshot rows or are declared
    unavailable — never reconstructed,
  * question-scoped House filters and role permissions are applied before
    execution.

Conversation context (family, filters, ordering, last rows) is kept per
session with a TTL, so follow-ups like "show only Telangana" / "sort by
expenditure" / "explain the third project" rebuild the previous query.
"""

import json
import re
import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Sequence, Tuple

import assistant_nlu
import auth
import llm_client
import read_only_query_executor as executor
import schema_context_service as schema
import sql_validation_service as sqlv

MAX_ROWS = 25
STORE_MAX_ROWS = 500
QUERY_TIMEOUT = 4.5
_CTX_TTL = 1800.0
_CTX_MAX = 400
_QUERY_TTL = 1800.0
_QUERY_MAX = 300

_CTX: Dict[str, Dict[str, Any]] = {}
_QUERIES: Dict[str, Dict[str, Any]] = {}
_LOCK = threading.Lock()

_PROJECT_FAMILIES = {
    "top_projects", "overspend", "high_spend_low_completion",
    "constituency_completion", "project_detail", "spend_no_progress",
    "multi_indicator_screening",
}
_BUILDABLE = _PROJECT_FAMILIES | {
    "state_utilisation", "house_comparison", "vendor_ranking", "latest_import",
    "snapshot_totals", "district_risk_ranking", "state_exp_low_completion",
    "state_pair_completion",
}
_PUBLIC_FAMILIES = {"snapshot_totals"}
_IMPORT_FAMILIES = {"latest_import"}

_MONEY_COLS = {"Recorded expenditure", "Sanctioned", "Allocated", "Expenditure", "Excess", "Value"}
_PERCENT_COLS = {"Utilisation %", "Completion %"}
_INT_COLS = {
    "Payments", "Projects", "Works", "Works completed", "Works pending", "Rows",
    "Added", "Updated", "Batch", "ID", "Risk score", "High-risk projects",
    "Indicators", "Other accounts", "Recommended", "Completed",
}

_ORDINALS = {
    "first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2,
    "fourth": 3, "4th": 3, "fifth": 4, "5th": 4, "last": -1,
}


# ══════════════════════════════════════════════════════════════════════
# Formatting helpers
# ══════════════════════════════════════════════════════════════════════

def _money(value: Any) -> str:
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        return "—"
    if abs(v) >= 1e7:
        return f"₹{v / 1e7:.2f} Cr"
    if abs(v) >= 1e5:
        return f"₹{v / 1e5:.2f} L"
    return f"₹{v:,.0f}"


def _fmt(col: str, value: Any) -> str:
    if value is None or value == "":
        return "—"
    if col in _MONEY_COLS:
        return _money(value)
    if col in _PERCENT_COLS:
        try:
            return f"{float(value):.2f}%"
        except (TypeError, ValueError):
            return str(value)
    if col in _INT_COLS:
        try:
            return f"{int(float(value)):,}"
        except (TypeError, ValueError):
            return str(value)
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, float):
        return f"{value:,.2f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _cell(columns: Sequence[str], row: Sequence[Any], name: str) -> Any:
    try:
        return row[list(columns).index(name)]
    except (ValueError, IndexError):
        return None


def _scope(filters: Dict[str, Any]) -> str:
    bits = []
    if filters.get("house"):
        bits.append(str(filters["house"]))
    if filters.get("state"):
        bits.append(str(filters["state"]))
    if filters.get("constituency"):
        bits.append(str(filters["constituency"]))
    if filters.get("fy"):
        bits.append(f"FY {filters['fy']}")
    if filters.get("completion_below") is not None:
        bits.append(f"completion < {filters['completion_below']:g}%")
    return (" · " + " · ".join(bits)) if bits else ""


# ══════════════════════════════════════════════════════════════════════
# Entity resolution — against real values only
# ══════════════════════════════════════════════════════════════════════

def _find_value(question: str, values: Sequence[str]) -> Optional[str]:
    """Longest exact (word-boundary, case-insensitive) value mentioned."""
    best: Optional[str] = None
    for value in sorted({v for v in values if v}, key=len, reverse=True):
        if re.search(rf"(?<![A-Za-z]){re.escape(value)}(?![A-Za-z])", question or "", re.I):
            best = value
            break
    return best


def _state(question: str) -> Optional[str]:
    values = [name for name, _ in schema.value_dictionary().get("states", [])]
    return _find_value(question, values)


def _state_names(question: str) -> list:
    """Every state name that appears in the question — used for two-state
    completion comparisons. Longest names are matched first so compound
    names ('Madhya Pradesh') win and each state is reported once."""
    values = [name for name, _ in schema.value_dictionary().get("states", [])]
    found: List[str] = []
    for name in sorted(values, key=lambda n: -len(str(n or ""))):
        n = str(name or "")
        if len(n) < 4:
            continue
        if re.search(rf"(?<![A-Za-z]){re.escape(n)}(?![A-Za-z])", question or "", re.I) \
                and n.lower() not in [f.lower() for f in found]:
            found.append(n)
        if len(found) >= 2:
            return found
    return found


def _constituency(question: str) -> Optional[str]:
    values = [name for name, _ in schema.value_dictionary().get("constituencies", [])]
    return _find_value(question, values)


def _house(question: str, fallback: Optional[str] = None) -> Optional[str]:
    match = re.search(r"\b(lok|rajya)\s*sabha\b", question or "", re.I)
    if match:
        return "Lok Sabha" if match.group(1).lower() == "lok" else "Rajya Sabha"
    # The app's House filter sends "All" (and may send "") for “no filter”.
    # Anything that is not an actual house must NOT become a WHERE clause —
    # a literal 'All' filter would silently match zero rows.
    if fallback in ("Lok Sabha", "Rajya Sabha"):
        return fallback
    return None


def _fy(question: str) -> Optional[str]:
    match = re.search(r"\b(20\d{2})\s*[-–]\s*(\d{2,4})\b", question or "")
    if not match:
        return None
    return f"{match.group(1)}-{match.group(2)[-2:]}"


def _constituency_phrase(question: str) -> Optional[str]:
    match = re.search(
        r"\b(?:in|from|of|for)\s+(?:the\s+)?([A-Za-z][A-Za-z .&'\-]{2,45}?)\s+constituenc",
        question or "", re.I,
    )
    if not match:
        return None
    value = " ".join(match.group(1).split()).strip(" .,?")
    if len(value) < 3 or value.lower() in {"a", "the", "this", "that", "particular", "each", "every", "any"}:
        return None
    return value


def _constituency_exists(name: str) -> bool:
    sql = "SELECT 1 AS found FROM projects WHERE LOWER(TRIM(constituency)) = LOWER(?) LIMIT 1"
    validation = sqlv.validate(sql, "analyst", max_rows=1, params=[name])
    if not validation.ok:
        return False
    try:
        data = executor.execute(
            validation.execution_sql, [name],
            allowed_tables=schema.table_allowlist("analyst"),
            allowed_columns=schema.column_allowlist("analyst"),
            max_rows=1, timeout=QUERY_TIMEOUT,
        )
    except Exception:
        return False
    return bool(data.get("rows"))


def _limit(question: str, default: int = 10) -> int:
    match = re.search(r"\b(?:top|first|highest|largest|show|list|bottom|lowest)\s+(\d{1,3})\b", question or "", re.I)
    if not match:
        match = re.search(r"\b(\d{1,3})\s+(?:projects?|vendors?|states?|rows?|records?)\b", question or "", re.I)
    if not match:
        return default
    try:
        return max(1, min(50, int(match.group(1))))
    except (TypeError, ValueError):
        return default


def _threshold(question: str, default: float = 50.0) -> float:
    match = re.search(r"(?:below|under|less than|lower than|completion of)\s*(\d{1,3}(?:\.\d+)?)\s*%?", question or "", re.I)
    if match:
        try:
            return max(0.0, min(100.0, float(match.group(1))))
        except ValueError:
            pass
    return default


def _filters(question: str, house: Optional[str], *, completion: bool = False) -> Dict[str, Any]:
    filters: Dict[str, Any] = {}
    state = _state(question)
    if state:
        filters["state"] = state
    constituency = _constituency(question)
    if constituency:
        filters["constituency"] = constituency
    resolved_house = _house(question, house)
    if resolved_house:
        filters["house"] = resolved_house
    fy = _fy(question)
    if fy:
        filters["fy"] = fy
    if completion:
        filters["completion_below"] = _threshold(question)
    return filters


# ══════════════════════════════════════════════════════════════════════
# SQL builders — one per family, all parameterised
# ══════════════════════════════════════════════════════════════════════

_PROJECT_COLUMNS = (
    'p.id AS "ID", p.project_name AS "Project", p.state AS "State", '
    'p.constituency AS "Constituency", p.house AS "House", '
    'ROUND(r.work_key_expenditure, 2) AS "Recorded expenditure", '
    'ROUND(p.sanctioned_amount, 2) AS "Sanctioned", '
    'p.completion_percentage AS "Completion %"'
)


def _where(filters: Dict[str, Any], alias: str = "p") -> Tuple[List[str], List[Any]]:
    clauses: List[str] = []
    params: List[Any] = []
    if filters.get("state"):
        clauses.append(f"LOWER(TRIM({alias}.state)) = LOWER(?)")
        params.append(filters["state"])
    if filters.get("house"):
        clauses.append(f"{alias}.house = ?")
        params.append(filters["house"])
    if filters.get("constituency"):
        clauses.append(f"LOWER(TRIM({alias}.constituency)) = LOWER(?)")
        params.append(filters["constituency"])
    if filters.get("fy"):
        clauses.append(f"{alias}.fy = ?")
        params.append(filters["fy"])
    return clauses, params


def _order(family: str, order: Optional[Dict[str, str]]) -> str:
    key = (order or {}).get("column") or "expenditure"
    direction = "ASC" if str((order or {}).get("dir", "DESC")).upper() == "ASC" else "DESC"
    table = {
        "top_projects": {
            "expenditure": "r.work_key_expenditure", "sanctioned": "p.sanctioned_amount",
            "completion": "p.completion_percentage", "id": "p.id",
        },
        "overspend": {
            "expenditure": '"Excess"', "sanctioned": "p.sanctioned_amount",
            "completion": "p.completion_percentage",
        },
        "high_spend_low_completion": {
            "expenditure": "r.work_key_expenditure", "completion": "p.completion_percentage",
            "sanctioned": "p.sanctioned_amount",
        },
        "constituency_completion": {
            "completion": "p.completion_percentage", "expenditure": "r.work_key_expenditure",
            "sanctioned": "p.sanctioned_amount",
        },
        "state_utilisation": {
            "expenditure": '"Recorded expenditure"', "utilisation": '"Utilisation %"',
            "sanctioned": '"Sanctioned"', "projects": '"Projects"',
        },
        "house_comparison": {"expenditure": '"Recorded expenditure"', "utilisation": '"Utilisation %"'},
        "vendor_ranking": {
            "expenditure": "SUM(expenditure_amount)", "payments": "COUNT(*)",
            "works": "COUNT(DISTINCT work_description)",
        },
        "district_risk_ranking": {
            "high_risk": '"High-risk projects"', "sanctioned": '"Sanctioned"',
            "district": '"District"',
        },
        "multi_indicator_screening": {
            "indicators": '"Indicators"', "risk": "COALESCE(rs.risk_score, 0)",
            "expenditure": "COALESCE(rs.risk_score, 0)",
        },
        "spend_no_progress": {
            "expenditure": "r.work_key_expenditure", "sanctioned": "p.sanctioned_amount",
            "completion": "p.completion_percentage",
        },
        "state_exp_low_completion": {
            "expenditure": '"Recorded expenditure"', "completion": '"Completion %"',
            "sanctioned": '"Sanctioned"', "projects": '"Projects"',
        },
        "state_pair_completion": {
            "expenditure": '"Recorded expenditure"', "completion": '"Completion %"',
            "sanctioned": '"Sanctioned"', "projects": '"Projects"',
        },
        "high_risk_completion": {
            "completion": "p.completion_percentage", "risk": "COALESCE(rs.risk_score, 0)",
            "expenditure": "r.work_key_expenditure", "sanctioned": "p.sanctioned_amount",
        },
    }.get(family, {})
    column = table.get(key, table.get("expenditure", "1"))
    return f"{column} {direction}"


def build(family: str, filters: Dict[str, Any], order: Optional[Dict[str, str]] = None,
          limit: int = 10) -> Tuple[str, List[Any], Dict[str, Any]]:
    """Return (sql, params, meta) for a family. Values are always bound."""
    limit = max(1, min(int(limit or 10), 50))
    clauses, params = _where(filters)
    where = (" AND " + " AND ".join(clauses)) if clauses else ""
    direction = _order(family, order)

    if family == "high_risk_completion":
        threshold = float(filters.get("completion_below", 50.0))
        sql = (
            f"SELECT {_PROJECT_COLUMNS},\n"
            '       COALESCE(rs.risk_level, \'None\') AS "Risk level",\n'
            '       COALESCE(rs.risk_score, 0) AS "Risk score"\n'
            "FROM projects p\n"
            "JOIN risk_scores rs ON rs.project_id = p.id\n"
            "LEFT JOIN project_rec_info r ON r.project_id = p.id\n"
            "WHERE rs.risk_level = 'High'\n"
            "  AND p.completion_percentage < ?"
            f"{where}\n"
            f"ORDER BY {direction}\nLIMIT ?"
        )
        return sql, [threshold] + params + [limit], {"chart": ("Project", "Completion %")}

    if family == "top_projects":
        sql = (
            f"SELECT {_PROJECT_COLUMNS}\n"
            "FROM projects p\n"
            "JOIN project_rec_info r ON r.project_id = p.id\n"
            "WHERE r.work_key_expenditure > 0 AND r.work_key_rows = 1"
            f"{where}\n"
            f"ORDER BY {direction}\nLIMIT ?"
        )
        return sql, params + [limit], {"chart": ("Project", "Recorded expenditure")}

    if family == "overspend":
        sql = (
            f'SELECT {_PROJECT_COLUMNS},\n'
            '       ROUND(r.work_key_expenditure - p.sanctioned_amount, 2) AS "Excess"\n'
            "FROM projects p\n"
            "JOIN project_rec_info r ON r.project_id = p.id\n"
            "WHERE r.work_key_expenditure > p.sanctioned_amount\n"
            "  AND p.sanctioned_amount > 0\n"
            "  AND r.work_key_rows = 1"
            f"{where}\n"
            f"ORDER BY {direction}\nLIMIT ?"
        )
        return sql, params + [limit], {"chart": ("Project", "Recorded expenditure")}

    if family == "high_spend_low_completion":
        threshold = float(filters.get("completion_below", 50.0))
        sql = (
            f"SELECT {_PROJECT_COLUMNS}\n"
            "FROM projects p\n"
            "JOIN project_rec_info r ON r.project_id = p.id\n"
            "WHERE r.work_key_expenditure > 0\n"
            "  AND r.work_key_rows = 1\n"
            "  AND p.completion_percentage < ?"
            f"{where}\n"
            f"ORDER BY {direction}\nLIMIT ?"
        )
        return sql, [threshold] + params + [limit], {"chart": ("Project", "Recorded expenditure")}

    if family == "constituency_completion":
        threshold = float(filters.get("completion_below", 50.0))
        sql = (
            f"SELECT {_PROJECT_COLUMNS}\n"
            "FROM projects p\n"
            "JOIN project_rec_info r ON r.project_id = p.id\n"
            "WHERE p.completion_percentage < ?"
            f"{where}\n"
            f"ORDER BY {direction}\nLIMIT ?"
        )
        return sql, [threshold] + params + [limit], {"chart": ("Project", "Completion %")}

    if family == "project_detail":
        sql = (
            'SELECT p.id AS "ID", p.project_name AS "Project", p.state AS "State", '
            'p.district AS "District", p.constituency AS "Constituency", '
            'p.project_type AS "Type", p.house AS "House", '
            'ROUND(p.sanctioned_amount, 2) AS "Sanctioned", '
            'ROUND(r.work_key_expenditure, 2) AS "Recorded expenditure", '
            'p.completion_percentage AS "Completion %", p.status AS "Status", '
            'p.fy AS "FY", COALESCE(rs.risk_level, \'None\') AS "Risk level", '
            'COALESCE(rs.risk_score, 0) AS "Risk score", '
            'r.recommendation_date AS "Recommended on", '
            'r.recommended_by AS "Recommended by", '
            'r.latest_expenditure_date AS "Last payment"\n'
            "FROM projects p\n"
            "LEFT JOIN project_rec_info r ON r.project_id = p.id\n"
            "LEFT JOIN risk_scores rs ON rs.project_id = p.id\n"
            "WHERE p.id = ?\nLIMIT 1"
        )
        return sql, [int(filters.get("project_id") or 0)], {"chart": None}

    if family == "state_utilisation":
        sanctioned_where = ""
        sanctioned_params: List[Any] = []
        if filters.get("house"):
            sanctioned_where += " AND house = ?"
            sanctioned_params.append(filters["house"])
        if filters.get("fy"):
            sanctioned_where += " AND fy = ?"
            sanctioned_params.append(filters["fy"])
        spent_where = ""
        spent_params: List[Any] = []
        if filters.get("house"):
            spent_where += " AND house = ?"
            spent_params.append(filters["house"])
        state_clause = ""
        state_params: List[Any] = []
        if filters.get("state"):
            state_clause = "WHERE LOWER(TRIM(s.st)) = LOWER(?)"
            state_params = [filters["state"]]
        sql = (
            "WITH sanctioned AS (\n"
            "  SELECT state AS st, COUNT(*) AS projects, SUM(sanctioned_amount) AS sanctioned\n"
            "  FROM projects\n"
            "  WHERE state IS NOT NULL AND TRIM(state) <> ''"
            + sanctioned_where
            + "\n  GROUP BY state\n),\n"
            "spent AS (\n"
            "  SELECT state AS st, SUM(expenditure_amount) AS spend, COUNT(*) AS txns\n"
            "  FROM expenditures\n"
            "  WHERE state IS NOT NULL AND TRIM(state) <> ''"
            + spent_where
            + "\n  GROUP BY state\n)\n"
            'SELECT s.st AS "State", s.projects AS "Projects",\n'
            '       ROUND(s.sanctioned, 2) AS "Sanctioned",\n'
            '       ROUND(COALESCE(e.spend, 0), 2) AS "Recorded expenditure",\n'
            '       ROUND(100.0 * COALESCE(e.spend, 0) / NULLIF(s.sanctioned, 0), 2) AS "Utilisation %"\n'
            "FROM sanctioned s\n"
            "LEFT JOIN spent e ON LOWER(TRIM(e.st)) = LOWER(TRIM(s.st))\n"
            + (state_clause + "\n" if state_clause else "")
            + f"ORDER BY {direction}\nLIMIT ?"
        )
        params = sanctioned_params + spent_params + state_params + [limit]
        return sql, params, {"chart": ("State", "Utilisation %")}

    if family == "house_comparison":
        sql = (
            "WITH s AS (\n"
            "  SELECT house AS h, COUNT(*) AS projects, SUM(sanctioned_amount) AS sanctioned\n"
            "  FROM projects WHERE house IS NOT NULL GROUP BY house\n),\n"
            "e AS (\n"
            "  SELECT house AS h, SUM(expenditure_amount) AS spend, COUNT(*) AS txns\n"
            "  FROM expenditures WHERE house IS NOT NULL GROUP BY house\n)\n"
            'SELECT s.h AS "House", s.projects AS "Projects",\n'
            '       ROUND(s.sanctioned, 2) AS "Sanctioned",\n'
            '       ROUND(COALESCE(e.spend, 0), 2) AS "Recorded expenditure",\n'
            '       ROUND(100.0 * COALESCE(e.spend, 0) / NULLIF(s.sanctioned, 0), 2) AS "Utilisation %",\n'
            '       COALESCE(e.txns, 0) AS "Payments"\n'
            "FROM s LEFT JOIN e ON e.h = s.h\n"
            f"ORDER BY {direction}\nLIMIT ?"
        )
        return sql, [limit], {"chart": ("House", "Recorded expenditure")}

    if family == "vendor_ranking":
        clauses, params = _where(filters, alias="e")
        where = (" AND " + " AND ".join(clauses)) if clauses else ""
        sql = (
            'SELECT vendor AS "Vendor", COUNT(*) AS "Payments",\n'
            '       ROUND(SUM(expenditure_amount), 2) AS "Recorded expenditure",\n'
            '       COUNT(DISTINCT work_description) AS "Works"\n'
            "FROM expenditures\n"
            "WHERE vendor IS NOT NULL AND TRIM(vendor) <> ''"
            f"{where}\n"
            "GROUP BY vendor\n"
            f"ORDER BY {direction}\nLIMIT ?"
        )
        return sql, params + [limit], {"chart": ("Vendor", "Recorded expenditure")}

    if family == "latest_import":
        sql = (
            'SELECT id AS "Batch", filename AS "File", dataset_type AS "Dataset",\n'
            '       status AS "Status", total_rows AS "Rows", new_rows AS "Added",\n'
            '       update_rows AS "Updated", committed_at AS "Committed"\n'
            "FROM import_batches\n"
            "ORDER BY COALESCE(committed_at, uploaded_at) DESC, id DESC\n"
            "LIMIT ?"
        )
        return sql, [limit], {"chart": None}

    if family == "snapshot_totals":
        sql = (
            'SELECT house AS "House", snapshot_label AS "Snapshot",\n'
            '       ROUND(total_allocated, 2) AS "Allocated",\n'
            '       ROUND(total_expenditure, 2) AS "Expenditure",\n'
            '       fund_utilization_percentage AS "Utilisation %",\n'
            '       works_completed AS "Works completed",\n'
            '       works_pending AS "Works pending"\n'
            "FROM reference_dashboard_metrics\n"
            "ORDER BY house\nLIMIT ?"
        )
        return sql, [limit], {"chart": None}

    if family == "district_risk_ranking":
        sql = (
            'SELECT p.district AS "District", p.state AS "State",\n'
            '       COUNT(*) AS "High-risk projects",\n'
            '       ROUND(SUM(p.sanctioned_amount), 2) AS "Sanctioned"\n'
            "FROM projects p\n"
            "JOIN risk_scores rs ON rs.project_id = p.id\n"
            "WHERE rs.risk_level = 'High'\n"
            "  AND p.district IS NOT NULL AND TRIM(p.district) <> ''"
            f"{where}\n"
            "GROUP BY p.district, p.state\n"
            f"ORDER BY {direction}\nLIMIT ?"
        )
        return sql, params + [limit], {"chart": ("District", "High-risk projects")}

    if family == "multi_indicator_screening":
        sql = (
            'SELECT p.id AS "ID", p.project_name AS "Project", p.state AS "State",\n'
            '       p.district AS "District", p.house AS "House",\n'
            "       COALESCE(rs.risk_level, 'None') AS \"Risk level\",\n"
            '       COALESCE(rs.risk_score, 0) AS "Risk score",\n'
            "       (LENGTH(COALESCE(rs.reasons, ''))\n"
            "        - LENGTH(REPLACE(COALESCE(rs.reasons, ''), ',', '')) + 1) AS \"Indicators\",\n"
            "       COALESCE(rs.reasons, '') AS \"Risk reasons\"\n"
            "FROM projects p\n"
            "JOIN risk_scores rs ON rs.project_id = p.id\n"
            "WHERE rs.reasons IS NOT NULL AND TRIM(rs.reasons) <> ''"
            f"{where}\n"
            f"ORDER BY {direction}\nLIMIT ?"
        )
        return sql, params + [limit], {"chart": None}

    if family == "spend_no_progress":
        sql = (
            f"SELECT {_PROJECT_COLUMNS}\n"
            "FROM projects p\n"
            "JOIN project_rec_info r ON r.project_id = p.id\n"
            "WHERE r.work_key_expenditure > 0\n"
            "  AND r.work_key_rows = 1\n"
            "  AND (p.completion_percentage IS NULL OR p.completion_percentage < 1)"
            f"{where}\n"
            f"ORDER BY {direction}\nLIMIT ?"
        )
        return sql, params + [limit], {"chart": ("Project", "Recorded expenditure")}

    if family == "state_exp_low_completion":
        sql = (
            "WITH sanc AS (\n"
            "  SELECT state AS st, COUNT(*) AS projects, SUM(sanctioned_amount) AS sanctioned\n"
            "  FROM projects\n"
            "  WHERE state IS NOT NULL AND TRIM(state) <> ''\n"
            "  GROUP BY state\n),\n"
            "paid AS (\n"
            "  SELECT state AS st, SUM(expenditure_amount) AS spend\n"
            "  FROM expenditures\n"
            "  WHERE state IS NOT NULL AND TRIM(state) <> ''\n"
            "  GROUP BY state\n),\n"
            "rec AS (\n"
            "  SELECT state AS st, COUNT(*) AS recommended\n"
            "  FROM recommended_works\n"
            "  WHERE state IS NOT NULL AND TRIM(state) <> ''\n"
            "  GROUP BY state\n),\n"
            "done AS (\n"
            "  SELECT state AS st, COUNT(*) AS completed\n"
            "  FROM completed_works\n"
            "  WHERE state IS NOT NULL AND TRIM(state) <> ''\n"
            "  GROUP BY state\n)\n"
            'SELECT s.st AS "State", s.projects AS "Projects",\n'
            '       ROUND(s.sanctioned, 2) AS "Sanctioned",\n'
            '       ROUND(COALESCE(p2.spend, 0), 2) AS "Recorded expenditure",\n'
            '       COALESCE(r.recommended, 0) AS "Recommended",\n'
            '       COALESCE(d.completed, 0) AS "Completed",\n'
            '       ROUND(100.0 * COALESCE(d.completed, 0) / NULLIF(r.recommended, 0), 2)\n'
            '         AS "Completion %"\n'
            "FROM sanc s\n"
            "LEFT JOIN paid p2 ON LOWER(TRIM(p2.st)) = LOWER(TRIM(s.st))\n"
            "LEFT JOIN rec r ON LOWER(TRIM(r.st)) = LOWER(TRIM(s.st))\n"
            "LEFT JOIN done d ON LOWER(TRIM(d.st)) = LOWER(TRIM(s.st))\n"
            f"ORDER BY {direction}\nLIMIT ?"
        )
        return sql, [limit], {"chart": ("State", "Recorded expenditure")}

    if family == "state_pair_completion":
        a = str(filters.get("state_a") or "")
        b = str(filters.get("state_b") or "")
        if not a or not b:
            raise ValueError("state_pair_completion requires two states")
        pair = sorted([a, b])
        sql = (
            "WITH picked AS (\n"
            "  SELECT LOWER(?) AS s1, LOWER(?) AS s2\n),\n"
            "sanc AS (\n"
            "  SELECT LOWER(TRIM(state)) AS st, COUNT(*) AS projects,\n"
            "         SUM(sanctioned_amount) AS sanctioned\n"
            "  FROM projects\n"
            "  WHERE state IS NOT NULL AND TRIM(state) <> ''\n"
            "  GROUP BY LOWER(TRIM(state))\n),\n"
            "paid AS (\n"
            "  SELECT LOWER(TRIM(state)) AS st, SUM(expenditure_amount) AS spend\n"
            "  FROM expenditures\n"
            "  WHERE state IS NOT NULL AND TRIM(state) <> ''\n"
            "  GROUP BY LOWER(TRIM(state))\n),\n"
            "rec AS (\n"
            "  SELECT LOWER(TRIM(state)) AS st, COUNT(*) AS recommended\n"
            "  FROM recommended_works\n"
            "  WHERE state IS NOT NULL AND TRIM(state) <> ''\n"
            "  GROUP BY LOWER(TRIM(state))\n),\n"
            "done AS (\n"
            "  SELECT LOWER(TRIM(state)) AS st, COUNT(*) AS completed\n"
            "  FROM completed_works\n"
            "  WHERE state IS NOT NULL AND TRIM(state) <> ''\n"
            "  GROUP BY LOWER(TRIM(state))\n)\n"
            'SELECT s.st AS "State", s.projects AS "Projects",\n'
            '       ROUND(s.sanctioned, 2) AS "Sanctioned",\n'
            '       ROUND(COALESCE(p2.spend, 0), 2) AS "Recorded expenditure",\n'
            '       COALESCE(r.recommended, 0) AS "Recommended",\n'
            '       COALESCE(d.completed, 0) AS "Completed",\n'
            '       ROUND(100.0 * COALESCE(d.completed, 0) / NULLIF(r.recommended, 0), 2)\n'
            '         AS "Completion %"\n'
            "FROM sanc s\n"
            "LEFT JOIN paid p2 ON LOWER(TRIM(p2.st)) = LOWER(TRIM(s.st))\n"
            "LEFT JOIN rec r ON LOWER(TRIM(r.st)) = LOWER(TRIM(s.st))\n"
            "LEFT JOIN done d ON LOWER(TRIM(d.st)) = LOWER(TRIM(s.st))\n"
            "JOIN picked pk ON s.st IN (pk.s1, pk.s2)\n"
            f"ORDER BY {direction}\nLIMIT ?"
        )
        return sql, pair + [limit], {"chart": ("State", "Completion %")}

    raise ValueError(f"unknown query family: {family}")


# ══════════════════════════════════════════════════════════════════════
# Family detection
# ══════════════════════════════════════════════════════════════════════

def _detect(question: str) -> Optional[str]:
    q = (question or "").lower()

    # ── Copilot analytical families (cross-module) ────────────────────
    # Two-state completion comparison first: "compare project completion
    # trends between Telangana and Karnataka".
    if re.search(r"\b(compare|comparison|compar(e|ing))\b.{0,60}\b(between|of|two)\b.{0,40}\b(states?)\b"
                 r"|\bcompare\b.{0,50}\b(states?\b.{0,50})?\bcompletion\b|\btwo\s+states\b"
                 r"|\bcompletion\b.{0,30}\b(between|across)\b.{0,30}\btwo\b|\bcompare\b.{0,40}\bcomplet\w*",
                 q) and not re.search(r"\blok\s*sabha\b.{0,30}rajya", q):
        if re.search(r"\bcompare\b.{0,40}\b(complet\w*|progress|outcome)\b|\bcomplet\w*\b.{0,40}\bcompare\b|\btwo\s+states\b", q):
            return "state_pair_completion"

    # Deteriorating progress / changed outcomes between imports.
    if re.search(r"\b(deteriorat\w*|progress\b.{0,20}\b(decr\w*|declin\w*|dropp\w*|worse)|outcome)\b"
                 r".{0,30}\b(chang\w*|declin\w*|dropp\w*|import\w*|histor\w*)?\b", q) and \
            re.search(r"\bdeteriorat\w*", q):
        return "completion_declines"
    if re.search(r"\bexpenditure.{0,30}(increas|rose|grew|grow)|\b(money|funds?)\b.{0,20}\b(spent|paid)\b.{0,40}\bbut\b.{0,40}\b(no|didn|did\s+not|not)\b.{0,20}\b(impro|progress|complet)"
                 r"|\bspent\b.{0,30}\bbut\b.{0,30}\b(no|zero|didn)\b.{0,25}\b(progress|improvement|completion)\b"
                 r"|\b(increased|rose|grew)\b.{0,40}\bcompletion\b.{0,40}\b(no|not|didn)\b", q):
        return "spend_no_progress"

    if re.search(r"\bdistricts?\b.{0,50}\b(most|highest|top|max\w*)\b.{0,40}\b(high[\s-]*risk|risk|flagged|riskiest)\b"
                 r"|\b(most|top|which)\b.{0,30}\bhigh[\s-]*risk\b.{0,30}\bdistricts?\b"
                 r"|\b(districts?|district-wise)\b.{0,30}\bhigh[\s-]*risk\b", q):
        return "district_risk_ranking"

    if re.search(r"\b(projects?|works?)\b.{0,40}\b(need|requir\w*|merit|warrant|eligible)\b.{0,40}\binvestigat\w*"
                 r"|\binvestigat\w*\b.{0,40}\b(multiple|several|indicators?|riskiest|most)\b"
                 r"|\bwhich\b.{0,30}(projects?|works?)\b.{0,40}\b(multiple|indicator|investigat\w*)\b", q):
        return "multi_indicator_screening"

    # "Which states have the highest expenditure but lowest completion rates?"
    if re.search(r"\b(which|what)\s+states?\b.{0,60}\b(highest|most|top|large(st|est)?)\b.{0,40}\b(expenditure|spending|spent)\b"
                 r".{0,60}\blow\w*\b.{0,40}\bcompletion\b", q):
        return "state_exp_low_completion"

    # "How have project outcomes changed between historical imports?" — the
    # stored official snapshot totals are the only historical aggregate.
    if re.search(r"\b(outcomes?|totals?|figures?)\b.{0,40}\b(chang|compar|differ)\w*\b.{0,40}\b(import|snapshot|histor)", q) \
            and re.search(r"\bsnapshot|snapshot totals\b", q):
        return "snapshot_totals"

    # ── Established families ──────────────────────────────────────────
    if re.search(r"\b(previous|earlier|last)\s+import\b|\bthis\s+project\b.{0,40}\bimport\b", q) and \
            re.search(r"\b(what was|expenditure|spend|value|changed|compare)\b", q):
        return "project_history" if re.search(r"\bproject\b", q) else "expenditure_changes"

    if re.search(r"\b(added|new projects|inserted|created)\b.{0,40}\b(import|batch|upload)\b|"
                 r"\b(import|batch|upload)\b.{0,40}\b(added|new rows|how many)\b|"
                 r"\blatest\s+(import|batch)\b|\blast\s+import\b", q):
        return "latest_import"

    if re.search(r"\bsnapshot|historical|checkpoint\b", q) and \
            re.search(r"\b(compare|comparison|difference|between|across|change|expenditure|totals?)\b", q):
        return "snapshot_totals" if not re.search(r"\blargest\s+expenditure\s+changes?\b", q) else "expenditure_changes"

    if re.search(r"\blargest\s+(?:expenditure\s+)?changes?\b|\bexpenditure\s+changes?\b", q) and \
            re.search(r"\b(projects?|between|snapshots?|imports?)\b", q):
        return "expenditure_changes"

    if re.search(r"\bvendors?|contractors?|suppliers?\b", q) and \
            re.search(r"\b(most|highest|largest|top|maximum)\b", q):
        return "vendor_ranking"

    if re.search(r"\b(exceed|exceeds|exceeded|greater than|more than|higher than|overspend|over spend)\b", q) and \
            re.search(r"\bsanctioned\b", q):
        return "overspend"

    if re.search(r"\bcompar(?:e|ison)\b|\bversus\b|\bvs\.?\b|\bdifference\b", q) and \
            re.search(r"\blok\s*sabha\b", q) and re.search(r"\brajya\s*sabha\b", q):
        return "house_comparison"

    if re.search(r"\b(high|large|most|big)\b.{0,20}\bexpenditure\b", q) and \
            re.search(r"\b(low|poor|slow|incomplete|unfinished)\b.{0,25}\b(completion|progress)\b", q):
        return "high_spend_low_completion"
    if re.search(r"\b(high|large)\s+spend(?:ing)?\b.{0,25}\b(low|poor)\s+(completion|progress)\b", q):
        return "high_spend_low_completion"

    # Multi-condition screening: risk flag + a completion threshold, in an
    # optional state/house/constituency scope. Both conditions are applied to
    # stored columns only — nothing is derived.
    if re.search(r"\bhigh[- ]?risk\b", q) and \
            re.search(r"\b(completion|progress)\b.{0,25}\b(below|under|less|lower)\b|"
                      r"\b(below|under|less|lower)\b.{0,25}\b(completion|progress)\b", q):
        return "high_risk_completion"

    if re.search(r"\b(which|what)\s+states?\b", q) and \
            re.search(r"\b(utilis|utiliz|fund)\b|\bexpenditure\b", q) and \
            re.search(r"\b(highest|most|top|best|largest|ranking)\b", q):
        return "state_utilisation"

    if re.search(r"\bfund\s+utilis|fund\s+utiliz\b", q):
        return "state_utilisation"

    if re.search(r"\b(top|highest|largest|most)\b", q) and \
            re.search(r"\bprojects?\b", q) and \
            re.search(r"\b(expenditure|spend|spending|sanctioned|cost)\b", q):
        return "top_projects"

    if re.search(r"\bconstituenc", q) and \
            re.search(r"\bcompletion\b.{0,20}\b(below|under|less|lower)\b|\b(below|under|less|lower)\b.{0,20}\bcompletion\b", q):
        return "constituency_completion"

    return None


def _plan(question: str, *, user, house: Optional[str]) -> Optional[Dict[str, Any]]:
    text = (question or "").strip()
    # Definitional questions belong to the knowledge base, not to a data query.
    if re.match(r"^(what\s+(is|are|does)|what's|define|explain|meaning\s+of)\b", text, re.I) \
            and not re.search(r"\b(show|list|how\s+many|top|highest|lowest|compare|which|count)\b", text, re.I):
        return None
    family = _detect(question)
    order: Optional[Dict[str, str]] = None
    if family is None:
        return _llm_plan(question, user=user, house=house)
    if family == "project_history":
        return {
            "kind": "limitation",
            "family": family,
            "answer": (
                "Per-project historical values for a previous import are not stored, so I can't "
                "report what this project's expenditure was in an earlier import without inventing it.\n\n"
                "What is stored: official house-level snapshot totals (reference_dashboard_metrics) and, "
                "for imported updates, the previous column values in the import rollback journal "
                "(import_changes). Neither is a per-project time series."
            ),
            "limitations": [
                "There is no per-project historical snapshot table in this database.",
                "Historical values are never reconstructed from current records.",
            ],
        }
    if family == "expenditure_changes":
        return _expenditure_changes_plan(user)
    if family == "completion_declines":
        return {
            "kind": "limitation",
            "family": family,
            "answer": (
                "Which works have deteriorating (regressing) progress can only be told by comparing "
                "each project's values across imports — that history lives in the Project Change "
                "Detection layer, which no import has filled yet.\n\nOnce an import updates existing "
                "projects, every completion decrease appears there; nothing is reconstructed from "
                "current values."),
            "limitations": [
                "No per-project historical values are stored yet (project_snapshots is empty).",
                "Current records are never used as a stand-in for historical values.",
            ],
        }
    if family == "state_pair_completion":
        names = _state_names(question)
        if len(names) >= 2:
            return _sql_plan(
                family, {"state_a": names[0], "state_b": names[1]},
                question=question, title="Completion comparison between two states",
                order={"column": "completion", "dir": "DESC"}, limit=2)
        if len(names) == 1:
            return {
                "kind": "clarify",
                "family": family,
                "question": (
                    f"I found {names[0]}. Name a second state and I'll compare completion "
                    f"records between {names[0]} and that state."),
                "limitations": ["Two named states are required for this comparison."],
            }
        return {
            "kind": "clarify",
            "family": family,
            "question": "Which two states should I compare? Name both and I'll bring their recorded totals, expenditure and completion side by side.",
            "limitations": ["Two named states are required for this comparison."],
        }
    if family == "constituency_completion":
        filters = _filters(question, house, completion=True)
        if not filters.get("constituency"):
            # Names outside the cached top-400 list still resolve, but only if
            # the value really exists in the catalogue (never guessed).
            phrase = _constituency_phrase(question)
            if phrase and _constituency_exists(phrase):
                filters["constituency"] = phrase
        if not filters.get("constituency") and not filters.get("state"):
            values = [name for name, _ in schema.value_dictionary().get("constituencies", [])][:6]
            options = ", ".join(values)
            return {
                "kind": "clarify",
                "family": family,
                "question": (
                    "Which constituency should I check? Name it (for example one of: "
                    f"{options}) and I'll list its projects with completion below "
                    f"{filters.get('completion_below', 50):g}%."
                ),
                "limitations": ["A constituency name is required — the query is not run without one."],
            }
        return _sql_plan(family, filters, question=question, title="Projects below the completion threshold")

    filters = _filters(
        question, house,
        completion=family in ("high_spend_low_completion", "high_risk_completion"),
    )
    if family == "house_comparison":
        # The comparison spans both houses; a single-house filter would be wrong.
        filters.pop("house", None)
    if family == "constituency_completion" and not order:
        order = {"column": "completion", "dir": "ASC"}
    if family == "high_risk_completion" and not order:
        order = {"column": "risk", "dir": "DESC"}
    titles = {
        "top_projects": "Highest recorded expenditure projects",
        "overspend": "Projects whose recorded payments exceed the sanctioned amount",
        "high_spend_low_completion": "High expenditure with low completion",
        "state_utilisation": "Recorded fund utilisation by state",
        "house_comparison": "Lok Sabha vs Rajya Sabha recorded expenditure",
        "vendor_ranking": "Vendors by recorded payments",
        "latest_import": "Latest import batch",
        "snapshot_totals": "Stored historical snapshot totals",
        "district_risk_ranking": "Districts by high-risk project count",
        "multi_indicator_screening": "Projects flagged on multiple indicators",
        "spend_no_progress": "Payments recorded with no reported progress",
        "state_exp_low_completion": "States by expenditure and completion",
        "high_risk_completion": "High-risk projects below the completion threshold",
    }
    if family == "district_risk_ranking":
        filters.pop("completion_below", None)
        return _sql_plan(family, filters, question=question, title=titles[family],
                         order={"column": "high_risk", "dir": "DESC"})
    if family == "multi_indicator_screening":
        filters.pop("completion_below", None)
        return _sql_plan(family, filters, question=question, title=titles[family],
                         order={"column": "indicators", "dir": "DESC"})
    if family == "spend_no_progress":
        filters.pop("completion_below", None)
        return _sql_plan(family, filters, question=question, title=titles[family],
                         order={"column": "expenditure", "dir": "DESC"})
    if family == "state_exp_low_completion":
        filters.pop("completion_below", None)
        return _sql_plan(family, filters, question=question, title=titles[family],
                         order={"column": "expenditure", "dir": "DESC"})
    return _sql_plan(family, filters, question=question, title=titles.get(family, "Database query"),
                     order=order)


def _sql_plan(family: str, filters: Dict[str, Any], *, question: str,
              title: str, order: Optional[Dict[str, str]] = None, 
              limit: Optional[int] = None, llm_note: Optional[str] = None,
              sql: Optional[str] = None, params: Optional[List[Any]] = None) -> Dict[str, Any]:
    plan: Dict[str, Any] = {
        "kind": "sql",
        "family": family,
        "filters": filters,
        "title": title,
        "order": order or {"column": "expenditure", "dir": "DESC"},
        "limit": limit if limit is not None else _limit(question, 10),
        "llm_note": llm_note,
    }
    if sql is None:
        built_sql, built_params, meta = build(family, filters, plan["order"], plan["limit"])
        plan.update({"sql": built_sql, "params": built_params, "meta": meta})
    else:
        plan.update({"sql": sql, "params": params or [], "meta": {"chart": None}})
    return plan


def _expenditure_changes_plan(user) -> Dict[str, Any]:
    """'Largest expenditure changes' — only answerable from stored row history."""
    if not auth.has_role_rank(user, "admin"):
        return {
            "kind": "limitation",
            "family": "expenditure_changes",
            "answer": (
                "Comparing expenditure changes across imports needs the import rollback journal, "
                "which is administrative data. Your role can still compare the stored official "
                "snapshot totals — ask “compare the stored snapshot totals”."
            ),
            "limitations": ["Import history (import_changes / import_batches) is restricted to administrators."],
        }
    rows = _safe_scalar(
        "SELECT COUNT(*) FROM import_changes", allowed_role="admin"
    )
    if not rows:
        return {
            "kind": "limitation",
            "family": "expenditure_changes",
            "answer": (
                "No import has recorded per-row before-states yet, so there are no stored expenditure "
                "changes to rank. Nothing is estimated from current values — the comparison becomes "
                "possible once an import updates existing rows."
            ),
            "limitations": [
                "import_changes is empty: no imported update has captured a previous row state.",
                "Current records are never used as a stand-in for historical values.",
            ],
        }
    return _sql_plan(
        "latest_import", {}, question="", title="Recent imports with captured changes", limit=10,
    )


def _safe_scalar(sql: str, *, allowed_role: str) -> Optional[int]:
    result = sqlv.validate(sql, allowed_role, max_rows=1)
    if not result.ok:
        return None
    try:
        data = executor.execute(
            result.execution_sql, [],
            allowed_tables=schema.table_allowlist(allowed_role),
            allowed_columns=schema.column_allowlist(allowed_role),
            max_rows=1, timeout=QUERY_TIMEOUT,
        )
    except Exception:
        return None
    rows = data.get("rows") or []
    if not rows or not rows[0]:
        return 0
    try:
        return int(rows[0][0] or 0)
    except (TypeError, ValueError):
        return 0


# ══════════════════════════════════════════════════════════════════════
# LLM fallback (advisory only — validated like everything else)
# ══════════════════════════════════════════════════════════════════════

_LLM_SYSTEM = (
    "You generate ONE read-only SQLite SELECT statement for the MPLADS analytics database. "
    "Hard rules: exactly one statement; only SELECT or WITH; no comments; no semicolons; "
    "only the tables and columns in the provided schema; only these functions: "
    "count, sum, avg, total, min, max, round, abs, coalesce, ifnull, nullif, lower, upper, "
    "trim, length, substr, replace, date, strftime, normkey; always end with LIMIT <= 50; "
    "never invent table names, columns, ids or values; never write a statement that modifies "
    "the database or reads sqlite_master. If the question cannot be answered from the schema, "
    "or is ambiguous, ask for clarification instead of guessing. "
    'Reply with JSON only: {"sql": "...", "explanation": "...", "clarification": null or "question"}.'
)


def _llm_plan(question: str, *, user, house: Optional[str]) -> Optional[Dict[str, Any]]:
    if not llm_client.available():
        return None
    role = getattr(user, "role", None) or "public"
    context = schema.build_context(question, role)
    prompt = (
        f"{context['text']}\n\nQuestion: {question}\n"
        f"Application House filter: {house or 'none (all houses)'}\n"
        "Use project_rec_info.work_key_expenditure (with work_key_rows = 1) for per-project spend; "
        "never use projects.expenditure. Return JSON only."
    )
    data = llm_client.complete_json(_LLM_SYSTEM, prompt, max_tokens=650)
    if not data:
        return None
    clarification = str(data.get("clarification") or "").strip()
    if clarification:
        return {"kind": "clarify", "family": "llm", "question": clarification[:280],
                "limitations": ["The question was ambiguous, so no query was executed."]}
    sql = sqlv.clean_candidate(data.get("sql") or "")
    if not sql:
        return None
    return {
        "kind": "sql",
        "family": "llm_generated",
        "filters": {"house": house} if house else {},
        "title": "Database query",
        "order": {"column": "expenditure", "dir": "DESC"},
        "limit": MAX_ROWS,
        "llm_note": str(data.get("explanation") or "")[:400] or None,
        "sql": sql,
        "params": [],
        "meta": {"chart": None},
    }


def _llm_explanation(question: str, sql: str, columns: Sequence[str],
                     rows: Sequence[Sequence[Any]]) -> Optional[str]:
    """Optional prose over the ACTUAL rows, dropped if it introduces numbers."""
    if not llm_client.available() or not rows:
        return None
    sample = [dict(zip(columns, [_fmt(c, v) for c, v in zip(columns, row)])) for row in rows[:12]]
    prompt = (
        f"Question: {question}\nSQL: {sql}\nRows (already formatted, complete for the shown cap):\n"
        f"{json.dumps(sample, ensure_ascii=False)[:4000]}\n\n"
        "Write at most 3 sentences explaining what these rows show. Use ONLY numbers that appear "
        "in the rows. Do not speculate about causes, do not add data, do not mention the SQL. "
        'Reply as JSON: {"explanation": "..."}'
    )
    data = llm_client.complete_json(
        "You summarise database result rows for an auditor. Never introduce numbers that are "
        "not present in the rows. JSON only.",
        prompt, max_tokens=220,
    )
    if not data:
        return None
    text = str(data.get("explanation") or "").strip()
    if not text or len(text) > 700:
        return None
    if not _numbers_grounded(text, columns, rows):
        return None
    return text


def _numbers_grounded(text: str, columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> bool:
    known = set()
    for row in rows:
        for value in row:
            if value is None:
                continue
            raw = str(value)
            known.add(raw)
            known.add(raw.replace(",", ""))
            if isinstance(value, (int, float)):
                known.add(f"{float(value):.2f}")
                known.add(f"{value:,.0f}")
                known.add(f"{value:,.2f}")
    for token in re.findall(r"\d[\d,]*\.?\d*", text):
        clean = token.rstrip(".")
        if clean in known or clean.replace(",", "") in {k.replace(",", "") for k in known}:
            continue
        return False
    return True


# ══════════════════════════════════════════════════════════════════════
# Permissions
# ══════════════════════════════════════════════════════════════════════

def _gate(family: str, user) -> Optional[Dict[str, Any]]:
    if family in _PUBLIC_FAMILIES:
        return None
    if family in _IMPORT_FAMILIES:
        if not auth.has_role_rank(user, "admin"):
            return {
                "kind": "blocked",
                "family": family,
                "answer": (
                    "Import and sync history is administrative data, so I can't expose it for your "
                    "role. I can still answer portfolio questions about projects, expenditure, "
                    "vendors and stored snapshot totals."
                ),
                "limitations": ["Import bookkeeping requires the administrator role."],
            }
    if not auth.has_role_rank(user, "analyst"):
        return {
            "kind": "blocked",
            "family": family,
            "answer": (
                "Database questions about projects, expenditure and vendors are available to "
                "analysts, auditors and administrators. I can still help with MPLADS basics, "
                "public project lookups, navigation and Ground Verification."
            ),
            "limitations": ["This analytical answer requires analyst-tier access."],
        }
    return None


# ══════════════════════════════════════════════════════════════════════
# Explanation — every figure comes from the returned rows
# ══════════════════════════════════════════════════════════════════════

def _explain(family: str, columns: Sequence[str], rows: Sequence[Sequence[Any]],
             filters: Dict[str, Any], limit: int, truncated: bool,
             order: Optional[Dict[str, str]] = None) -> Tuple[str, List[str]]:
    scope = _scope(filters)
    # Scope wording for sentences that already state the completion threshold.
    scope_no_threshold = _scope({k: v for k, v in filters.items() if k != "completion_below"})
    notes: List[str] = []
    if not rows:
        empty = {
            "top_projects": "No project has recorded linked payments in this scope.",
            "overspend": "No project in this scope has recorded payments above its sanctioned amount.",
            "high_spend_low_completion": "No project in this scope matches high recorded spend with completion below the threshold.",
            "constituency_completion": "No project in this scope is below that completion threshold.",
            "state_utilisation": "No state-level rows are recorded for this scope.",
            "house_comparison": "No house-attributed rows are recorded.",
            "vendor_ranking": "No vendor-attributed payments are recorded in this scope.",
            "latest_import": "No import batch is recorded, so the number of projects added in the latest import cannot be determined.",
            "snapshot_totals": "No historical snapshot rows are stored.",
            "district_risk_ranking": "No high-risk projects are recorded in this scope.",
            "multi_indicator_screening": "No project carries multiple stored risk indicators in this scope — or the scope has no stored risk reasons at all.",
            "spend_no_progress": "No project in this scope combines recorded payments with reported progress below 1%.",
            "state_exp_low_completion": "No state-level rows are recorded for this scope.",
            "state_pair_completion": "Neither named state has recorded rows.",
            "high_risk_completion": "No stored High-risk project in this scope is below that completion threshold.",
            "project_detail": "That project id is not in the catalogue.",
            "llm_generated": "The query returned no rows.",
        }.get(family, "The query returned no rows.")
        return empty + " Nothing is estimated in its place.", ["No matching records were found in the database."]

    if family in ("top_projects", "high_spend_low_completion", "constituency_completion"):
        top = rows[0]
        name = _cell(columns, top, "Project")
        pid = _cell(columns, top, "ID")
        spend = _cell(columns, top, "Recorded expenditure")
        sanctioned = _cell(columns, top, "Sanctioned")
        completion = _cell(columns, top, "Completion %")
        state = _cell(columns, top, "State")
        constituency = _cell(columns, top, "Constituency")
        total = sum(float(r[list(columns).index("Recorded expenditure")] or 0)
                    for r in rows if _cell(columns, r, "Recorded expenditure") is not None)
        if family == "constituency_completion":
            head = (
                f"**{len(rows)}{'+' if truncated else ''}** project{'s' if len(rows) != 1 else ''}"
                f"{scope_no_threshold} are below {filters.get('completion_below', 50):g}% completion. "
                f"The first row is **{name}** (#{pid}, {state} · {constituency}) at "
                f"**{_fmt('Completion %', completion)}**."
            )
        else:
            head = (
                f"**{name}** (#{pid}, {state} · {constituency}) has the highest recorded "
                f"expenditure in this result: **{_money(spend)}** against {_money(sanctioned)} "
                f"sanctioned ({_fmt('Completion %', completion)} complete)."
            )
            if family == "high_spend_low_completion":
                head = (
                    f"**{len(rows)}{'+' if truncated else ''}** project"
                    f"{'s' if len(rows) != 1 else ''} combine recorded payments with completion "
                    f"below {filters.get('completion_below', 50):g}%{scope_no_threshold}. The first row is "
                    f"**{name}** (#{pid}) — {_money(spend)} recorded against "
                    f"{_money(sanctioned)} sanctioned at {_fmt('Completion %', completion)} complete."
                )
        tail = f" The {len(rows)} rows shown carry {_money(total)} of recorded payments in total."
        if truncated:
            tail += f" More rows match; the {limit}-row display cap is applied."
        notes.append("Recorded expenditure is the ledger total linked to the project's work key; projects sharing a work key are excluded.")
        return head + tail, notes

    if family == "overspend":
        top = rows[0]
        head = (
            f"**{len(rows)}{'+' if truncated else ''}** project{'s' if len(rows) != 1 else ''}"
            f"{scope} have recorded payments above the sanctioned amount. The largest excess is "
            f"**{_cell(columns, top, 'Project')}** (#{_cell(columns, top, 'ID')}): "
            f"{_money(_cell(columns, top, 'Recorded expenditure'))} paid against "
            f"{_money(_cell(columns, top, 'Sanctioned'))} sanctioned — "
            f"**{_money(_cell(columns, top, 'Excess'))} over**."
        )
        notes.append("Excess is recorded payments minus the catalogue's sanctioned amount for projects whose work key is unambiguous.")
        return head, notes

    if family == "state_utilisation":
        top = rows[0]
        head = (
            f"**{_cell(columns, top, 'State')}** has the highest recorded fund utilisation{scope} at "
            f"**{_fmt('Utilisation %', _cell(columns, top, 'Utilisation %'))}**, on "
            f"{_money(_cell(columns, top, 'Recorded expenditure'))} recorded against "
            f"{_money(_cell(columns, top, 'Sanctioned'))} sanctioned across "
            f"{_fmt('Projects', _cell(columns, top, 'Projects'))} projects."
        )
        if len(rows) > 1:
            last = rows[-1]
            head += (
                f" At the other end of the table, {_cell(columns, last, 'State')} records "
                f"{_fmt('Utilisation %', _cell(columns, last, 'Utilisation %'))}."
            )
        notes.append("Utilisation compares ledger payments labelled with the state against catalogue sanctioned amounts; the two sources are scoped separately, so it is indicative.")
        return head, notes

    if family == "house_comparison":
        parts = []
        for row in rows[:2]:
            parts.append(
                f"**{_cell(columns, row, 'House')}**: "
                f"{_money(_cell(columns, row, 'Recorded expenditure'))} recorded against "
                f"{_money(_cell(columns, row, 'Sanctioned'))} sanctioned "
                f"({_fmt('Utilisation %', _cell(columns, row, 'Utilisation %'))} utilisation, "
                f"{_fmt('Payments', _cell(columns, row, 'Payments'))} payments)"
            )
        head = " · ".join(parts) + "."
        if len(rows) >= 2:
            first, second = rows[0], rows[1]
            diff = abs(float(_cell(columns, first, "Recorded expenditure") or 0)
                       - float(_cell(columns, second, "Recorded expenditure") or 0))
            head += f" The difference in recorded expenditure is {_money(diff)}."
        notes.append("Records with no house attribution are excluded from this comparison — none are guessed into a house.")
        return head, notes

    if family == "vendor_ranking":
        top = rows[0]
        head = (
            f"**{_cell(columns, top, 'Vendor')}** has the highest recorded payments{scope}: "
            f"**{_money(_cell(columns, top, 'Recorded expenditure'))}** across "
            f"{_fmt('Payments', _cell(columns, top, 'Payments'))} payment records covering "
            f"{_fmt('Works', _cell(columns, top, 'Works'))} distinct works."
        )
        notes.append("Vendor totals come from the payment ledger's own vendor field; blank vendor rows are excluded.")
        return head, notes

    if family == "latest_import":
        top = rows[0]
        head = (
            f"The latest recorded import is **{_cell(columns, top, 'File')}** "
            f"({_cell(columns, top, 'Dataset') or 'dataset'}), status "
            f"{_cell(columns, top, 'Status')}, committed {_cell(columns, top, 'Committed') or '—'}: "
            f"{_fmt('Rows', _cell(columns, top, 'Rows'))} rows processed, "
            f"{_fmt('Added', _cell(columns, top, 'Added'))} added and "
            f"{_fmt('Updated', _cell(columns, top, 'Updated'))} updated."
        )
        notes.append("Counts are the import batch's own recorded totals, not a recount of current rows.")
        return head, notes

    if family == "snapshot_totals":
        parts = []
        for row in rows[:4]:
            parts.append(
                f"**{_cell(columns, row, 'House')}** ({_cell(columns, row, 'Snapshot')}): "
                f"{_money(_cell(columns, row, 'Expenditure'))} expenditure against "
                f"{_money(_cell(columns, row, 'Allocated'))} allocated "
                f"({_fmt('Utilisation %', _cell(columns, row, 'Utilisation %'))})"
            )
        head = "Stored snapshot totals — " + "; ".join(parts) + "."
        labels = {_cell(columns, row, "Snapshot") for row in rows}
        if len(labels) == 1:
            head += (
                " These rows are different houses captured in the same official snapshot, not two "
                "points in time."
            )
        notes.append("Values are read from the stored snapshot rows; they are not recomputed from current project data.")
        return head, notes

    if family == "district_risk_ranking":
        top = rows[0]
        head = (
            f"**{_cell(columns, top, 'District')}** ({_cell(columns, top, 'State')}) has the most "
            f"high-risk projects{scope}: **{_fmt('High-risk projects', _cell(columns, top, 'High-risk projects'))}** "
            f"works carrying {_money(_cell(columns, top, 'Sanctioned'))} sanctioned in total."
        )
        if len(rows) > 1:
            head += (
                f" Next: {_cell(columns, rows[1], 'District')} with "
                f"{_fmt('High-risk projects', _cell(columns, rows[1], 'High-risk projects'))}."
            )
        notes.append("Counts use the existing stored risk_levels table — the stored scoring formula is applied, not recomputed.")
        return head, notes

    if family == "high_risk_completion":
        top = rows[0]
        head = (
            f"**{len(rows)}{'+' if truncated else ''}** project{'s' if len(rows) != 1 else ''}{scope} "
            f"carry a stored High risk flag and recorded completion below "
            f"{float(filters.get('completion_below', 50)):g}%. The top row is "
            f"**{_cell(columns, top, 'Project')}** (#{_cell(columns, top, 'ID')}, "
            f"{_cell(columns, top, 'State')}) with risk score {_cell(columns, top, 'Risk score')} "
            f"at {_fmt('Completion %', _cell(columns, top, 'Completion %'))} completion."
        )
        notes.append(
            "Both conditions filter stored values — the existing risk_level flag and the recorded "
            "completion percentage. This is a screening list for review priority, not an audit finding.")
        return head, notes

    if family == "multi_indicator_screening":
        top = rows[0]
        head = (
            f"**{_cell(columns, top, 'Project')}** (#{_cell(columns, top, 'ID')}, "
            f"{_cell(columns, top, 'State')}) carries the most stored risk indicators: "
            f"**{_fmt('Indicators', _cell(columns, top, 'Indicators'))}** reasons — "
            f"{str(_cell(columns, top, 'Risk reasons') or '—')[:200]}."
        )
        notes.append("Indicator counts come from the stored risk reasons; they are screening signals, not audit conclusions.")
        return head, notes

    if family == "spend_no_progress":
        top = rows[0]
        head = (
            f"**{len(rows)}{'+' if truncated else ''}** project{'s' if len(rows) != 1 else ''}"
            f"{scope} have recorded payments with reported physical progress below 1%. The first row is "
            f"**{_cell(columns, top, 'Project')}** (#{_cell(columns, top, 'ID')}): "
            f"{_money(_cell(columns, top, 'Recorded expenditure'))} recorded against "
            f"{_money(_cell(columns, top, 'Sanctioned'))} sanctioned."
        )
        notes.append("'No progress' means the recorded completion percentage is under 1% — the dataset stores one current value, not a trend; money-in-without-progress changes across imports belong to Project Change Detection.")
        return head, notes

    if family in ("state_exp_low_completion", "state_pair_completion"):
        top = rows[0]
        head = ""
        if family == "state_pair_completion" and len(rows) >= 2:
            a, b = rows[0], rows[1]
            head = (
                f"**{_cell(columns, a, 'State')}** vs **{_cell(columns, b, 'State')}**: "
                f"{_fmt('Completion %', _cell(columns, a, 'Completion %'))} vs "
                f"{_fmt('Completion %', _cell(columns, b, 'Completion %'))} completed works "
                f"({_fmt('Completed', _cell(columns, a, 'Completed'))} vs "
                f"{_fmt('Completed', _cell(columns, b, 'Completed'))} of "
                f"{_fmt('Recommended', _cell(columns, a, 'Recommended'))} vs "
                f"{_fmt('Recommended', _cell(columns, b, 'Recommended'))} recommended); recorded expenditure "
                f"{_money(_cell(columns, a, 'Recorded expenditure'))} vs "
                f"{_money(_cell(columns, b, 'Recorded expenditure'))}."
            )
        else:
            head = (
                f"**{_cell(columns, top, 'State')}** leads this table{scope} with "
                f"{_money(_cell(columns, top, 'Recorded expenditure'))} recorded expenditure and "
                f"{_fmt('Completion %', _cell(columns, top, 'Completion %'))} of its recommended works recorded complete."
            )
        notes.append("Completion % compares completed-work records with recommended-work records per state — a work-level ratio, not the catalogue's per-project completion field.")
        return head, notes

    if family == "project_detail":
        row = rows[0]
        head = (
            f"Project **#{_cell(columns, row, 'ID')} — {_cell(columns, row, 'Project')}** "
            f"({_cell(columns, row, 'Type')}, {_cell(columns, row, 'State')} · "
            f"{_cell(columns, row, 'Constituency')}, {_cell(columns, row, 'House') or 'house unattributed'}). "
            f"Sanctioned {_money(_cell(columns, row, 'Sanctioned'))}, recorded payments "
            f"{_money(_cell(columns, row, 'Recorded expenditure'))}, completion "
            f"{_fmt('Completion %', _cell(columns, row, 'Completion %'))}, status "
            f"{_cell(columns, row, 'Status')}. Risk level {_cell(columns, row, 'Risk level')} "
            f"(score {_fmt('Risk score', _cell(columns, row, 'Risk score'))}). "
            f"Recommended on {_cell(columns, row, 'Recommended on') or '—'} by "
            f"{_cell(columns, row, 'Recommended by') or '—'}; last recorded payment "
            f"{_cell(columns, row, 'Last payment') or '—'}."
        )
        return head, ["Every value above is a stored field of that project record."]

    # LLM-generated SQL over an arbitrary (validated) query.
    numeric = [c for c in columns if c in _MONEY_COLS | _PERCENT_COLS]
    head = f"The query returned {len(rows)} row{'s' if len(rows) != 1 else ''}"
    if columns:
        head += f" with columns {', '.join(columns[:6])}"
    head += "."
    if numeric:
        head += f" Numeric columns: {', '.join(numeric[:3])}."
    return head, ["This result came from a generated query; open the SQL to see exactly what was executed."]


# ══════════════════════════════════════════════════════════════════════
# Result shaping / store
# ══════════════════════════════════════════════════════════════════════

def _table(columns: Sequence[str], rows: Sequence[Sequence[Any]], caption: str) -> Dict[str, Any]:
    return {
        "columns": [str(c)[:60] for c in columns][:8],
        "rows": [[_fmt(c, v)[:120] for c, v in zip(columns, row)][:8] for row in rows[:12]],
        "caption": caption[:160],
    }


def _chart(columns: Sequence[str], rows: Sequence[Sequence[Any]],
           spec: Optional[Tuple[str, str]]) -> Optional[Dict[str, Any]]:
    if not spec or not rows:
        return None
    label_col, value_col = spec
    if label_col not in columns or value_col not in columns:
        return None
    li, vi = list(columns).index(label_col), list(columns).index(value_col)
    items = []
    for row in rows[:8]:
        value = row[vi]
        if not isinstance(value, (int, float)):
            continue
        label = str(row[li] or "")
        items.append({"label": label[:38], "value": round(float(value), 2)})
    if not items:
        return None
    return {
        "type": "bar",
        "label_column": label_col,
        "value_column": value_col,
        "money": value_col in _MONEY_COLS,
        "items": items,
    }


def _findings(family: str, columns: Sequence[str], rows: Sequence[Sequence[Any]],
              filters: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if not rows:
        return out
    if family in _PROJECT_FAMILIES:
        for row in rows[:3]:
            out.append({
                "title": f"#{_cell(columns, row, 'ID')} {str(_cell(columns, row, 'Project'))[:70]}",
                "detail": (
                    f"{_cell(columns, row, 'State')} · {_cell(columns, row, 'Constituency')} · "
                    f"{_money(_cell(columns, row, 'Recorded expenditure'))} recorded against "
                    f"{_money(_cell(columns, row, 'Sanctioned'))} sanctioned · "
                    f"{_fmt('Completion %', _cell(columns, row, 'Completion %'))} complete"
                ),
                "kind": "record",
                "evidence": "projects + project_rec_info",
            })
    elif family == "vendor_ranking":
        for row in rows[:3]:
            out.append({
                "title": str(_cell(columns, row, "Vendor"))[:70],
                "detail": (
                    f"{_money(_cell(columns, row, 'Recorded expenditure'))} across "
                    f"{_fmt('Payments', _cell(columns, row, 'Payments'))} payments"
                ),
                "kind": "vendor",
                "evidence": "expenditures",
            })
    elif family in ("state_utilisation", "house_comparison"):
        key = "State" if family == "state_utilisation" else "House"
        for row in rows[:4]:
            out.append({
                "title": str(_cell(columns, row, key)),
                "detail": (
                    f"{_fmt('Utilisation %', _cell(columns, row, 'Utilisation %'))} utilisation · "
                    f"{_money(_cell(columns, row, 'Recorded expenditure'))} recorded · "
                    f"{_money(_cell(columns, row, 'Sanctioned'))} sanctioned"
                ),
                "kind": "aggregate",
                "evidence": "projects + expenditures",
            })
    return out[:4]


def _store(payload: Dict[str, Any], user) -> str:
    qid = uuid.uuid4().hex
    entry = {
        "ts": time.time(),
        "user_id": getattr(user, "id", None),
        "role": getattr(user, "role", None),
        **payload,
    }
    with _LOCK:
        _sweep()
        _QUERIES[qid] = entry
    return qid


def _sweep() -> None:
    now = time.time()
    for key in [k for k, v in _QUERIES.items() if now - v["ts"] > _QUERY_TTL]:
        _QUERIES.pop(key, None)
    while len(_QUERIES) >= _QUERY_MAX:
        _QUERIES.pop(next(iter(_QUERIES)))


def get_results(query_id: str, user) -> Optional[Dict[str, Any]]:
    with _LOCK:
        entry = _QUERIES.get(str(query_id or ""))
    if not entry or time.time() - entry["ts"] > _QUERY_TTL:
        return None
    owner = entry.get("user_id")
    if owner is not None and owner != getattr(user, "id", None) and getattr(user, "role", None) != "admin":
        return None
    return {
        "id": query_id,
        "sql": entry.get("sql_display") or entry.get("sql"),
        "columns": entry.get("columns") or [],
        "rows": entry.get("all_rows") or [],
        "row_count": len(entry.get("all_rows") or []),
        "display_row_count": entry.get("display_row_count"),
        "truncated": entry.get("truncated"),
        "limit": entry.get("limit"),
        "elapsed_ms": entry.get("elapsed_ms"),
        "filters": entry.get("filters") or [],
        "created_at": entry.get("created_at"),
    }


# ══════════════════════════════════════════════════════════════════════
# Conversation context
# ══════════════════════════════════════════════════════════════════════

def has_context(session_id: Optional[str]) -> bool:
    if not session_id:
        return False
    with _LOCK:
        entry = _CTX.get(session_id)
    return bool(entry and time.time() - entry["ts"] <= _CTX_TTL)


def reset_context(session_id: Optional[str]) -> None:
    if not session_id:
        return
    with _LOCK:
        _CTX.pop(session_id, None)


def _remember(session_id: Optional[str], *, family: str, filters: Dict[str, Any],
              order: Dict[str, str], limit: int, columns: Sequence[str],
              rows: Sequence[Sequence[Any]], query_id: Optional[str]) -> None:
    if not session_id:
        return
    now = time.time()
    with _LOCK:
        if len(_CTX) >= _CTX_MAX:
            for key in [k for k, v in _CTX.items() if now - v["ts"] > _CTX_TTL][:50]:
                _CTX.pop(key, None)
        _CTX[session_id] = {
            "ts": now, "family": family, "filters": dict(filters),
            "order": dict(order), "limit": limit,
            "columns": list(columns), "rows": [list(r) for r in rows[:MAX_ROWS]],
            "query_id": query_id,
        }


_FOLLOWUP = re.compile(
    r"^(?:\s*(?:and|now|also|ok|okay)\b)"
    r"|\b(?:show|filter|restrict|narrow)\b.{0,30}\b(?:only|to)\b"
    r"|\bonly\s+[A-Za-z]"
    r"|\b(?:sort|order)\b.{0,20}\bby\b"
    r"|\bexplain\s+(?:the\s+)?(?:first|second|third|fourth|fifth|last|\d+(?:st|nd|rd|th))\b"
    r"|\bcompare\s+(?:those|these|them|it)\b"
    r"|\b(?:more|fewer)\s+(?:rows|results|projects)\b",
    re.I,
)


def is_followup(question: str) -> bool:
    return bool(_FOLLOWUP.search(question or ""))


def _followup(question: str, *, user, house: Optional[str],
              session_id: Optional[str]) -> Optional[Dict[str, Any]]:
    with _LOCK:
        ctx = dict(_CTX.get(session_id) or {})
    if not ctx or time.time() - ctx.get("ts", 0) > _CTX_TTL:
        return None
    q = question or ""
    family = ctx.get("family") or ""
    if family not in _BUILDABLE:
        return None
    filters = dict(ctx.get("filters") or {})
    order = dict(ctx.get("order") or {"column": "expenditure", "dir": "DESC"})
    limit = int(ctx.get("limit") or 10)

    # 1. "explain the third project" — resolve against the rows actually shown.
    ordinal = assistant_nlu.ordinal_index(q)
    if ordinal is not None and re.search(r"\b(explain|tell|details?|what about|why|show)\b", q, re.I) \
            and re.search(r"\b(project|one|record|row|it)\b", q, re.I):
        columns = ctx.get("columns") or []
        rows = ctx.get("rows") or []
        if "ID" in columns and rows:
            index = ordinal if ordinal >= 0 else len(rows) - 1
            if 0 <= index < len(rows):
                pid = rows[index][list(columns).index("ID")]
                plan = _sql_plan(
                    "project_detail", {"project_id": pid}, question=q,
                    title=f"Project #{pid}", limit=1,
                )
                return _run(plan, q, user=user, session_id=session_id, house=house,
                            followup_note=f"Explaining row {index + 1} of the previous result.")
            return {
                "answer": (
                    f"The previous result has only {len(rows)} row"
                    f"{'s' if len(rows) != 1 else ''}, so there is no "
                    f"{q.strip().rstrip('.').split()[1] if len(q.split()) > 1 else 'requested'} row to explain. "
                    "Ask for a wider query (for example more rows, or a different filter) first."
                ),
                "source": "data",
                "actions": [],
                "project_id": None,
                "meta": {
                    "intent": "dataset_analytics",
                    "limitations": ["The requested row is outside the previous result set."],
                },
            }
        return None

    # 2. House filter follow-up ("now filter to Lok Sabha", "only Rajya Sabha").
    house_match = re.search(r"\b(lok|rajya)\s*sabha\b", q, re.I)
    if house_match and re.search(r"\b(only|filter|restrict|now|just|to)\b", q, re.I):
        filters["house"] = "Lok Sabha" if house_match.group(1).lower() == "lok" else "Rajya Sabha"
        plan = _sql_plan(family, filters, question=q, title=ctx.get("title") or "Filtered database query",
                         order=order, limit=limit)
        return _run(plan, q, user=user, session_id=session_id, house=house,
                    followup_note=f"Applied the {filters['house']} filter to the previous result.")

    # 3. State / constituency filter follow-up ("show only Telangana").
    state = _state(q)
    if state and re.search(r"\b(only|just|filter|in|from|now)\b", q, re.I) and len(q.split()) <= 8:
        filters["state"] = state
        plan = _sql_plan(family, filters, question=q, title=ctx.get("title") or "Filtered database query",
                         order=order, limit=limit)
        return _run(plan, q, user=user, session_id=session_id, house=house,
                    followup_note=f"Filtered the previous result to {state}.")

    # 4. Ordering follow-up ("sort them by expenditure", "lowest first").
    if re.search(r"\b(sort|order)\b.{0,20}\bby\b|\b(lowest|highest|ascending|descending)\s+(first|order)?\b", q, re.I):
        column = "expenditure"
        if re.search(r"\bcompletion\b", q, re.I):
            column = "completion"
        elif re.search(r"\bsanctioned\b", q, re.I):
            column = "sanctioned"
        elif re.search(r"\bbenchmark|\bprojects?\b", q, re.I) and family == "state_utilisation":
            column = "projects"
        direction = "ASC" if re.search(r"\bascending|lowest|smallest|increasing\b", q, re.I) else "DESC"
        plan = _sql_plan(family, filters, question=q, title=ctx.get("title") or "Sorted database query",
                         order={"column": column, "dir": direction}, limit=limit)
        return _run(plan, q, user=user, session_id=session_id, house=house,
                    followup_note=f"Re-sorted the previous result by {column} {direction.lower()}.")

    # 5. Row-count follow-up ("show 20 instead").
    count = re.search(r"\b(?:show|top|first|limit)\s+(\d{1,2})\b", q, re.I)
    if count:
        try:
            limit = max(1, min(50, int(count.group(1))))
        except ValueError:
            return None
        plan = _sql_plan(family, filters, question=q, title=ctx.get("title") or "Database query",
                         order=order, limit=limit)
        return _run(plan, q, user=user, session_id=session_id, house=house,
                    followup_note=f"Showing {limit} rows from the previous query.")

    # 6. Historical comparison of the current rows.
    if re.search(r"\b(previous|last|earlier)\s+import\b|\bsnapshots?\b", q, re.I) and \
            re.search(r"\b(compare|comparison|change|difference)\b", q, re.I):
        return {
            "answer": (
                "The projects in that result can't be compared against the previous import: the "
                "database stores no per-project historical snapshot, only the import rollback "
                "journal for updated rows. I can compare the official stored snapshot totals instead."
            ),
            "source": "data",
            "actions": [],
            "project_id": None,
            "meta": {
                "intent": "comparison",
                "limitations": [
                    "No per-project historical snapshots are stored.",
                    "Values are never reconstructed from current records.",
                ],
                "findings": [{
                    "title": "Historical comparison unavailable",
                    "detail": "Use the stored official snapshot totals (reference_dashboard_metrics) for historical figures.",
                    "kind": "limitation",
                    "evidence": "reference_dashboard_metrics",
                }],
            },
        }
    return None


# ══════════════════════════════════════════════════════════════════════
# Execution + response shaping
# ══════════════════════════════════════════════════════════════════════

def _run(plan: Dict[str, Any], question: str, *, user, session_id: Optional[str],
         house: Optional[str], followup_note: Optional[str] = None) -> Dict[str, Any]:
    family = plan["family"]
    filters = plan.get("filters") or {}
    order = plan.get("order") or {"column": "expenditure", "dir": "DESC"}
    limit = int(plan.get("limit") or 10)
    sql = plan["sql"]
    params = plan.get("params") or []

    role = getattr(user, "role", None) or "public"
    validation = sqlv.validate(sql, role, max_rows=max(limit, MAX_ROWS), params=params)
    if not validation.ok:
        return _error_result(
            "I generated a query but the safety validator rejected it, so nothing was executed.\n\n"
            + "\n".join(f"• {e}" for e in validation.errors[:3]),
            extra_limitations=["Query blocked by the read-only SQL validator."],
            sql=sql,
        )

    try:
        data = executor.execute(
            validation.execution_sql, params,
            allowed_tables=schema.table_allowlist(role),
            allowed_columns=schema.column_allowlist(role),
            max_rows=STORE_MAX_ROWS, timeout=QUERY_TIMEOUT,
        )
    except executor.QueryTimeout:
        return _error_result(
            "That query took longer than the execution time limit, so it was cancelled. "
            "Try narrowing it (a state, a house, or fewer rows).",
            extra_limitations=["Query timeout enforced by the read-only executor."],
            sql=sql,
        )
    except executor.QueryRejected as exc:
        return _error_result(
            f"The executor refused the query: {exc}", extra_limitations=["Nothing was executed."], sql=sql,
        )
    except Exception:
        return _error_result(
            "The database could not run that query safely, so nothing was returned.",
            extra_limitations=["Execution error handled by the read-only layer."], sql=sql,
        )

    columns = data.get("columns") or []
    all_rows = data.get("rows") or []
    rows = all_rows[: max(1, min(limit, len(all_rows)))] if all_rows else []
    chart = _chart(columns, rows, (plan.get("meta") or {}).get("chart"))

    truncated = len(all_rows) > len(rows)
    answer, notes = _explain(family, columns, rows, filters, limit, truncated, order)
    if followup_note:
        answer = f"{followup_note}\n\n{answer}"
    if plan.get("llm_note") and family == "llm_generated" and _numbers_grounded(
            str(plan["llm_note"]), columns, rows):
        answer = f"{answer}\n\n{str(plan['llm_note'])[:400]}"
    else:
        prose = _llm_explanation(question, sql, columns, rows)
        if prose:
            answer = f"{answer}\n\n{prose}"

    filters_display = [
        {"label": "House", "value": filters["house"]} if filters.get("house") else None,
        {"label": "State", "value": filters["state"]} if filters.get("state") else None,
        {"label": "Constituency", "value": filters["constituency"]} if filters.get("constituency") else None,
        {"label": "FY", "value": filters["fy"]} if filters.get("fy") else None,
        {"label": "Completion", "value": f"< {filters['completion_below']:g}%"}
        if filters.get("completion_below") is not None else None,
        {"label": "Limit", "value": str(limit)},
    ]
    filters_display = [f for f in filters_display if f]

    sql_display = sqlv.render_sql(validation.sql, params)
    query_id = _store({
        "sql": validation.sql,
        "sql_display": sql_display,
        "columns": columns,
        "all_rows": rows,
        "display_row_count": len(rows),
        "truncated": bool(data.get("truncated")) or len(all_rows) > len(rows),
        "limit": limit,
        "elapsed_ms": data.get("elapsed_ms"),
        "filters": filters_display,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }, user)
    _remember(session_id, family=family, filters=filters, order=order, limit=limit,
              columns=columns, rows=rows, query_id=query_id)

    limitations = list(notes)
    limitations.append(
        f"Read-only query over {len(all_rows)} returned row(s); figures are exactly as recorded."
    )
    if plan.get("llm_note") or family == "llm_generated":
        limitations.append("The SQL was model-generated and passed the read-only validator before execution.")

    caption_bits = [plan.get("title") or "Database query"]
    if _scope(filters):
        caption_bits.append(_scope(filters).lstrip(" · "))
    caption = " · ".join(caption_bits) + f" · {len(rows)} of {max(len(all_rows), len(rows))} rows"

    return {
        "answer": answer,
        "source": "data",
        "actions": [],
        "project_id": (rows[0][list(columns).index("ID")]
                       if family == "project_detail" and columns and rows and "ID" in columns else None),
        "meta": {
            "intent": "dataset_analytics",
            "entity": {"type": "query", "family": family, "filters": filters},
            "table": _table(columns, rows, caption) if rows else None,
            "chart": chart,
            "findings": _findings(family, columns, rows, filters),
            "limitations": limitations[:8],
            "query": {
                "id": query_id,
                "sql": sql_display,
                "status": "ok" if rows else "empty",
                "row_count": len(all_rows),
                "display_row_count": len(rows),
                "truncated": truncated,
                "limit": limit,
                "elapsed_ms": data.get("elapsed_ms"),
                "filters": filters_display,
                "chart": chart,
                "result_url": f"/assistant/query/{query_id}/results",
                "can_export": auth.has_role_rank(user, "analyst"),
                "error": None,
            },
        },
    }



def _error_result(message: str, *, extra_limitations: Sequence[str] = (),
                  sql: Optional[str] = None) -> Dict[str, Any]:
    return {
        "answer": message,
        "source": "data",
        "actions": [],
        "project_id": None,
        "meta": {
            "intent": "dataset_analytics",
            "limitations": list(extra_limitations)[:4] or ["The query was not executed."],
            "query": {
                "id": None, "sql": sql, "status": "error", "row_count": 0,
                "display_row_count": 0, "truncated": False, "limit": 0,
                "elapsed_ms": 0, "filters": [], "chart": None,
                "result_url": None, "can_export": False, "error": message[:300],
            },
        },
    }


# ══════════════════════════════════════════════════════════════════════
# Public entry points
# ══════════════════════════════════════════════════════════════════════

def handle(question: str, *, user=None, house: Optional[str] = None,
           session_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Answer a database question, or return None to let the assistant continue.

    `None` means "this is not a database question" — navigation, knowledge and
    the curated deterministic intents keep ownership of those.
    """
    q = (question or "").strip()
    if not q or len(q) > 500:
        return None
    try:
        if session_id and has_context(session_id) and is_followup(q):
            result = _followup(q, user=user, house=house, session_id=session_id)
            if result is not None:
                return result

        plan = _plan(q, user=user, house=house)
        if plan is None:
            return None

        gate = _gate(plan.get("family") or "", user)
        if gate:
            return _simple({
                "answer": gate["answer"],
                "source": "data",
                "actions": [],
                "project_id": None,
                "meta": {
                    "intent": "dataset_analytics",
                    "limitations": gate.get("limitations") or [],
                },
            })

        if plan["kind"] == "clarify":
            return _simple({
                "answer": plan["question"],
                "source": "data",
                "actions": [],
                "project_id": None,
                "meta": {
                    "intent": "dataset_analytics",
                    "limitations": plan.get("limitations") or [
                        "The question was ambiguous, so no query was run."
                    ],
                },
            })

        if plan["kind"] == "limitation":
            return _simple({
                "answer": plan["answer"],
                "source": "data",
                "actions": [],
                "project_id": None,
                "meta": {
                    "intent": "dataset_analytics",
                    "limitations": plan.get("limitations") or [],
                    "query": {
                        "id": None, "sql": None, "status": "unavailable", "row_count": 0,
                        "display_row_count": 0, "truncated": False, "limit": 0,
                        "elapsed_ms": 0, "filters": [], "chart": None,
                        "result_url": None, "can_export": False,
                        "error": None,
                    },
                },
            })

        return _run(plan, q, user=user, session_id=session_id, house=house)
    except Exception:
        return _error_result(
            "I could not complete that database query safely, so nothing was executed. "
            "Try rephrasing it, or ask for a narrower scope."
        )


def _simple(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "answer": payload["answer"],
        "source": payload.get("source", "data"),
        "actions": payload.get("actions") or [],
        "project_id": payload.get("project_id"),
        "meta": payload.get("meta") or {},
    }


def validate_sql(sql: str, user) -> Dict[str, Any]:
    """Validator-only entry point (POST /assistant/query/validate)."""
    role = getattr(user, "role", None) or "public"
    if not auth.has_role_rank(user, "analyst"):
        return {"ok": False, "errors": ["Query validation requires analyst-tier access."]}
    result = sqlv.validate(sqlv.clean_candidate(sql), role, max_rows=MAX_ROWS)
    return result.as_dict()


def results_for(query_id: str, user) -> Optional[Dict[str, Any]]:
    return get_results(query_id, user)
