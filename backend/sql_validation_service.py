"""
Strict SQL validation for assistant-generated statements
========================================================

Security is NOT delegated to the language model. Every statement — whether it
came from the deterministic planner or from an LLM — passes through here:

  * exactly one statement (no stacked statements, no trailing payload)
  * SELECT / WITH only; INSERT, UPDATE, DELETE, DROP, ALTER, CREATE,
    TRUNCATE, ATTACH, DETACH, PRAGMA, VACUUM, REINDEX, REPLACE, transactions
    and every other statement class are rejected by keyword AND by SQLite's
    authorizer at execution time
  * comments are rejected so a payload cannot be hidden behind `--`
  * SQLite internals (`sqlite_master`, `sqlite_schema`, …) are unreachable
  * only the allowlisted tables/columns for the caller's role are referenced
  * only allowlisted aggregate/scalar functions are used
  * the result set is capped by wrapping the statement in an outer LIMIT
  * the statement is compiled with EXPLAIN (no rows produced) through the
    read-only connection, so syntax errors and unknown columns fail here

The validator raises nothing to the caller on rejection: it returns a result
object, and the service refuses to execute when `ok` is False.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import read_only_query_executor as executor
import schema_context_service as schema

MAX_SQL_LENGTH = 4000
MAX_ROWS = 200

# Statement classes that can never appear in a read-only query.
_DENIED_KEYWORDS = {
    "insert", "update", "delete", "drop", "alter", "create", "truncate",
    "attach", "detach", "pragma", "vacuum", "reindex", "replace", "grant",
    "revoke", "begin", "commit", "rollback", "savepoint", "release",
    "analyze", "load_extension", "writefile", "readfile", "edit",
    "sqlite_master", "sqlite_schema", "sqlite_temp_master", "sqlite_sequence",
    "sqlite_stat1", "sqlite_stat4", "sqlite_dbpage",
}

# Dangerous scalar helpers even inside an allowed SELECT.
_DENIED_FUNCTIONS = {
    "load_extension", "writefile", "readfile", "edit", "fts3_tokenizer",
    "sqlite_compileoption_get", "sqlite_version", "randomblob", "zeroblob",
}

# SQL syntax words that look like `name(` in a scan but are not functions.
_SYNTAX_WORDS = {
    "select", "from", "where", "group", "order", "by", "having", "limit",
    "offset", "join", "left", "right", "inner", "outer", "cross", "on",
    "using", "and", "or", "not", "in", "is", "null", "like", "glob", "between",
    "case", "when", "then", "else", "end", "as", "asc", "desc", "with",
    "recursive", "union", "all", "exists", "values", "over", "partition",
    "filter", "distinct", "escape", "collate",
}

_TABLE_REF = re.compile(r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_]*)", re.I)
_FUNC_CALL = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", re.I)
_CTE_NAME = re.compile(r"(?:\bwith\b|,)\s*(?:recursive\s+)?([A-Za-z_][A-Za-z0-9_]*)\s+as\s*\(", re.I)


@dataclass
class ValidationResult:
    ok: bool
    sql: str = ""
    execution_sql: str = ""
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    tables: List[str] = field(default_factory=list)
    limit: int = MAX_ROWS

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "sql": self.sql,
            "errors": self.errors,
            "warnings": self.warnings,
            "tables": self.tables,
            "limit": self.limit,
        }


def clean_candidate(raw: str) -> str:
    """Normalise an LLM's SQL string: strip fences, prose and trailing ';'."""
    text = str(raw or "").strip()
    text = re.sub(r"^```(?:sql)?", "", text, flags=re.I).strip()
    text = re.sub(r"```$", "", text).strip()
    text = re.sub(r"^(?:sql\s*:\s*)", "", text, flags=re.I).strip()
    return text.rstrip().rstrip(";").strip()


def _strip_literals(sql: str) -> str:
    """Blank out quoted literals/identifiers; reject comments.

    Keyword scanning on the literal-free text means a value such as
    'drop table projects' inside a string can never trip (or hide behind)
    the keyword checks.
    """
    out: List[str] = []
    quote_char: Optional[str] = None
    bracket = False
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if bracket:
            if ch == "]":
                bracket = False
            out.append(" ")
            i += 1
            continue
        if quote_char:
            if ch == quote_char:
                if i + 1 < n and sql[i + 1] == quote_char:
                    i += 2
                    continue
                quote_char = None
            out.append(" ")
            i += 1
            continue
        if ch in ("'", '"', "`"):
            quote_char = ch
            out.append(" ")
            i += 1
            continue
        if ch == "[":
            bracket = True
            out.append(" ")
            i += 1
            continue
        if sql.startswith("--", i) or sql.startswith("/*", i):
            raise ValueError("SQL comments are not allowed")
        out.append(ch)
        i += 1
    if quote_char or bracket:
        raise ValueError("unterminated quoted literal")
    return "".join(out)


def validate(sql: str, role: Optional[str] = None, *,
             max_rows: int = MAX_ROWS, compile_check: bool = True,
             params: Optional[Sequence[Any]] = None) -> ValidationResult:
    """Validate one statement for this role. Never raises for bad input."""
    original = str(sql or "").strip()
    if not original:
        return ValidationResult(ok=False, errors=["No SQL was produced."])
    if len(original) > MAX_SQL_LENGTH:
        return ValidationResult(ok=False, errors=["The generated query is too long to be safe."])

    # Exactly one statement: a single trailing ';' is tolerated, any other
    # semicolon means statement stacking.
    candidate = original.rstrip()
    while candidate.endswith(";"):
        candidate = candidate[:-1].rstrip()
    if ";" in candidate:
        return ValidationResult(ok=False, sql=candidate,
                                errors=["Only one SQL statement may be executed."])

    try:
        code = _strip_literals(candidate)
    except ValueError as exc:
        return ValidationResult(ok=False, sql=candidate, errors=[str(exc)])

    if not re.match(r"^\s*(select|with)\b", code, re.I):
        return ValidationResult(ok=False, sql=candidate,
                                errors=["Only read-only SELECT queries are allowed."])

    words = set(re.findall(r"[a-z_]+", code.lower()))
    denied = sorted(words & _DENIED_KEYWORDS)
    if denied:
        return ValidationResult(
            ok=False, sql=candidate,
            errors=[f"Prohibited SQL construct: {', '.join(denied)}. "
                    "The assistant can only read data."],
        )

    if re.search(r"\b(?:pragma|attach|detach)\b", code, re.I):
        return ValidationResult(ok=False, sql=candidate,
                                errors=["Database-modifying statements are not allowed."])

    # Functions.
    cte_names = {m.group(1).lower() for m in _CTE_NAME.finditer(code)}
    called = {m.group(1).lower() for m in _FUNC_CALL.finditer(code)}
    called -= _SYNTAX_WORDS | cte_names
    bad_funcs = sorted(called & _DENIED_FUNCTIONS)
    unknown = sorted(called - executor.ALLOWED_FUNCTIONS - cte_names)
    if bad_funcs:
        return ValidationResult(ok=False, sql=candidate,
                                errors=[f"Prohibited function: {', '.join(bad_funcs)}."])
    if unknown:
        return ValidationResult(
            ok=False, sql=candidate,
            errors=[f"Function not permitted: {', '.join(unknown[:4])}."],
        )

    # Tables — role-aware allowlist.
    allowed = set(schema.table_allowlist(role))
    referenced = [t for t in _TABLE_REF.findall(code) if t.lower() not in cte_names]
    tables: List[str] = []
    for name in referenced:
        low = name.lower()
        if low not in {t.lower() for t in schema.tables()}:
            return ValidationResult(ok=False, sql=candidate,
                                    errors=[f"Unknown table: {name}."])
        real = next(t for t in schema.tables() if t.lower() == low)
        if real not in allowed:
            return ValidationResult(
                ok=False, sql=candidate,
                errors=[f"Access to '{real}' is restricted for your role."],
            )
        if real not in tables:
            tables.append(real)
    if not tables:
        return ValidationResult(ok=False, sql=candidate,
                                errors=["The query does not read an allowed table."])

    cap = max(1, min(int(max_rows or MAX_ROWS), executor.MAX_MAX_ROWS))
    execution_sql = f"SELECT * FROM (\n{candidate}\n) LIMIT {cap + 1}"

    if compile_check:
        allowed_columns = schema.column_allowlist(role)
        error = executor.compile_check(execution_sql, sorted(allowed), allowed_columns, params)
        if error:
            return ValidationResult(
                ok=False, sql=candidate, execution_sql=execution_sql, tables=tables,
                errors=[_friendly_compile_error(error)],
            )

    warnings: List[str] = []
    if not re.search(r"\blimit\b", code, re.I):
        warnings.append(f"Result cap of {cap} rows applied automatically.")
    return ValidationResult(ok=True, sql=candidate, execution_sql=execution_sql,
                            tables=tables, warnings=warnings, limit=cap)


def _friendly_compile_error(error: str) -> str:
    text = str(error)
    if "no such column" in text.lower() or "no such table" in text.lower():
        return f"The generated query references data that does not exist: {text[:140]}"
    if "not authorized" in text.lower():
        return "The generated query touches a table or column that is not permitted."
    return f"The generated query is not valid SQL: {text[:160]}"


def render_sql(sql: str, params: Sequence[Any]) -> str:
    """Render bound values into the statement text for display/copy only.

    Execution always uses the bound `params`; this string is never executed.
    """
    out = []
    index = 0
    for ch in sql:
        if ch == "?" and index < len(params):
            out.append(_literal(params[index]))
            index += 1
        else:
            out.append(ch)
    return "".join(out)


def _literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"
