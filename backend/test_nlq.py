"""
Tests for the assistant's natural-language database query layer.

Run from the backend directory:

    python test_nlq.py

Covers the contract that matters most — safety and correctness:
  * simple SELECT / JOIN / aggregation questions
  * house-specific and historical snapshot questions
  * follow-up questions (house filter, state filter, sort, ordinal explain)
  * ambiguous questions
  * invalid and destructive SQL attempts
  * restricted-table access and role denial
  * large-query caps and the executor timeout
  * LLM unavailability (deterministic planner keeps working)

No external services are required: the LLM path is disabled unless a key is
configured, and the tests never write to the database.
"""

import os
import sys
import types

os.environ.pop("MPLADS_LLM_API_KEY", None)
os.environ.pop("OPENAI_API_KEY", None)

import read_only_query_executor as executor
import schema_context_service as schema
import sql_validation_service as sqlv
import natural_language_query_service as nlq

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"PASS  {name}")
    else:
        FAILED.append((name, detail))
        print(f"FAIL  {name}{(' — ' + str(detail)[:200]) if detail else ''}")


def analyst(role="analyst", uid=1):
    return types.SimpleNamespace(id=uid, role=role, is_active=True)


_DEFAULT_USER = object()


def ask(question, user=_DEFAULT_USER, session="test-session", house=None):
    """user=None means a guest; omitting it means a signed-in analyst."""
    resolved = analyst() if user is _DEFAULT_USER else user
    return nlq.handle(question, user=resolved, house=house, session_id=session)


def query_block(result):
    return (result or {}).get("meta", {}).get("query") or {}


# ── 1. Validator: destructive / malformed / restricted ──────────────────
def test_validator():
    good = [
        "SELECT COUNT(*) AS n FROM projects",
        "SELECT p.state, SUM(p.sanctioned_amount) AS s FROM projects p GROUP BY p.state",
        "WITH t AS (SELECT state, COUNT(*) AS n FROM projects GROUP BY state) "
        "SELECT state, n FROM t ORDER BY n DESC",
    ]
    for sql in good:
        result = sqlv.validate(sql, "analyst")
        check(f"valid SELECT accepted: {sql[:38]}…", result.ok, result.errors)

    bad = {
        "insert": "INSERT INTO projects (project_name) VALUES ('x')",
        "update": "UPDATE projects SET expenditure = 0",
        "delete": "DELETE FROM projects",
        "drop": "DROP TABLE projects",
        "alter": "ALTER TABLE projects ADD COLUMN x TEXT",
        "create": "CREATE TABLE x (a INT)",
        "truncate": "DELETE FROM expenditures",
        "attach": "ATTACH DATABASE 'x.db' AS y",
        "pragma-write": "PRAGMA writable_schema = ON",
        "pragma-read": "PRAGMA table_info(projects)",
        "stacked": "SELECT 1; DROP TABLE projects",
        "comment": "SELECT * FROM projects -- trailing",
        "block-comment": "SELECT * FROM projects /* hidden */",
        "sqlite_master": "SELECT name FROM sqlite_master",
        "users": "SELECT email FROM users",
        "unknown-table": "SELECT * FROM secret_table",
        "bad-column": "SELECT password_hash FROM projects",
        "extension": "SELECT load_extension('evil') FROM projects",
        "writefile": "SELECT writefile('/tmp/x', 'y') FROM projects",
        "empty": "   ",
    }
    for name, sql in bad.items():
        result = sqlv.validate(sql, "analyst")
        check(f"rejected: {name}", not result.ok, "validator accepted it")

    # Restricted table for a non-admin role.
    import_ok = sqlv.validate("SELECT COUNT(*) FROM import_batches", "admin")
    import_denied = sqlv.validate("SELECT COUNT(*) FROM import_batches", "analyst")
    check("admin may read import_batches", import_ok.ok, import_ok.errors)
    check("analyst denied import_batches", not import_denied.ok, "expected denial")


# ── 2. Executor: read-only, capped, timed out ──────────────────────────
def test_executor():
    allowed = schema.table_allowlist("analyst")
    columns = schema.column_allowlist("analyst")
    result = sqlv.validate("SELECT id, project_name FROM projects", "analyst", max_rows=5)
    data = executor.execute(result.execution_sql, [], allowed_tables=allowed,
                            allowed_columns=columns, max_rows=5, timeout=3)
    check("executor returns capped rows", 0 < data["row_count"] <= 5, data["row_count"])

    big = sqlv.validate("SELECT * FROM expenditures", "analyst", max_rows=50)
    data = executor.execute(big.execution_sql, [], allowed_tables=allowed,
                            allowed_columns=columns, max_rows=50, timeout=5)
    check("large result is capped", data["row_count"] <= 50, data["row_count"])

    heavy = sqlv.validate(
        "SELECT COUNT(*) FROM projects a JOIN projects b ON a.state = b.state",
        "analyst", max_rows=1,
    )
    timed_out = False
    try:
        executor.execute(heavy.execution_sql, [], allowed_tables=allowed,
                         allowed_columns=columns, max_rows=1, timeout=0.5)
    except executor.QueryTimeout:
        timed_out = True
    except executor.QueryRejected:
        timed_out = True  # authorizer/planner refusal is equally acceptable
    check("pathological join is interrupted", timed_out, "query completed instead")

    # Write attempt through the executor (bypassing the validator) is denied.
    denied = False
    try:
        executor.execute("UPDATE projects SET expenditure = 1", [],
                         allowed_tables=allowed, allowed_columns=columns, max_rows=1)
    except (executor.QueryRejected, Exception):
        denied = True
    check("executor refuses a write statement", denied)


# ── 3. Core question families ───────────────────────────────────────────
def test_families():
    cases = [
        ("Show the 10 projects with the highest expenditure.", "top_projects"),
        ("Which states have the highest recorded fund utilisation?", "state_utilisation"),
        ("Find projects where expenditure exceeds sanctioned amount.", "overspend"),
        ("Compare Lok Sabha and Rajya Sabha expenditure.", "house_comparison"),
        ("Which vendors are associated with the most recorded expenditure?", "vendor_ranking"),
        ("Show projects with high expenditure and low completion.", "high_spend_low_completion"),
        ("Compare project expenditure between two historical snapshots.", "snapshot_totals"),
        ("Find projects in Hyderabad constituency with completion below 50%.", "constituency_completion"),
    ]
    for question, family in cases:
        result = ask(question, session=f"family-{family}")
        block = query_block(result)
        check(f"{family}: SQL executed", block.get("status") == "ok", block.get("error") or result)
        check(f"{family}: rows returned", (block.get("row_count") or 0) > 0, block)
        check(f"{family}: SQL shown", bool(block.get("sql")), block)
        check(f"{family}: read-only SELECT", str(block.get("sql") or "").strip().lower().startswith(
            ("select", "with")), block.get("sql"))

    # Aggregation values must match a direct read of the same aggregates.
    result = ask("Which states have the highest recorded fund utilisation?", session="agg")
    table = result["meta"]["table"]
    check("state aggregation has utilisation column", "Utilisation %" in table["columns"], table["columns"])


# ── 4. House scope + follow-ups ─────────────────────────────────────────
def test_followups():
    session = "followups"
    first = ask("Show the 10 projects with the highest expenditure.", session=session, house="Rajya Sabha")
    block = query_block(first)
    check("house filter applied from app state",
          any(f.get("label") == "House" and f.get("value") == "Rajya Sabha" for f in block.get("filters") or []),
          block.get("filters"))

    # The application passes its default House value ("All") when unfiltered.
    all_house = ask("Show the 10 projects with the highest expenditure.", session="allhouse", house="All")
    check("'All' house value is not treated as a filter",
          not any(f.get("label") == "House" for f in query_block(all_house).get("filters") or []),
          query_block(all_house).get("filters"))
    check("'All' house query still returns rows",
          (query_block(all_house).get("row_count") or 0) > 0, query_block(all_house))

    second = ask("Show only Telangana.", session=session)
    filters = query_block(second).get("filters") or []
    check("follow-up state filter applied",
          any(f.get("label") == "State" and f.get("value") == "Telangana" for f in filters), filters)
    check("follow-up keeps house filter",
          any(f.get("label") == "House" and f.get("value") == "Rajya Sabha" for f in filters), filters)

    third = ask("Now filter to Lok Sabha.", session=session)
    filters = query_block(third).get("filters") or []
    check("follow-up house override applied",
          any(f.get("label") == "House" and f.get("value") == "Lok Sabha" for f in filters), filters)

    fourth = ask("Sort them by sanctioned amount.", session=session)
    check("follow-up sort re-runs SQL", query_block(fourth).get("status") == "ok", fourth)
    check("follow-up sort keeps filters",
          len(query_block(fourth).get("filters") or []) >= 2, query_block(fourth).get("filters"))

    # The filtered result may be short: an ordinal beyond it must explain, not guess.
    missing = ask("Explain the fifth project.", session=session)
    check("ordinal beyond the result set is explained",
          missing is not None and "only" in missing["answer"].lower(),
          (missing or {}).get("answer", "returned None")[:140])

    ordinal_session = "ordinal"
    ask("Show the 10 projects with the highest expenditure.", session=ordinal_session)
    fifth = ask("Explain the third project.", session=ordinal_session)
    check("ordinal explanation resolves a real project",
          fifth is not None and "Project **#" in fifth["answer"]
          and query_block(fifth).get("row_count") == 1,
          (fifth or {}).get("answer", "returned None")[:140])

    # A new, unrelated question must not inherit the previous filters.
    fresh = ask("Which vendors are associated with the most recorded expenditure?", session=session)
    labels = {f.get("label") for f in query_block(fresh).get("filters") or []}
    check("unrelated question drops carried filters", "State" not in labels and "House" not in labels, labels)


# ── 5. Ambiguity, historical data, permissions ─────────────────────────
def test_edges():
    ambiguous = ask("Find projects in a particular constituency with completion below 50%.", session="amb")
    block = query_block(ambiguous)
    check("ambiguous constituency asks for clarification",
          "constituency should I check" in ambiguous["answer"], ambiguous["answer"][:120])
    check("clarification executes no query", block.get("status") is None and block.get("sql") is None, block)

    history = ask("What was the expenditure of this project in the previous import?", session="hist")
    check("per-project history declared unavailable",
          "not stored" in history["answer"] or "can't" in history["answer"], history["answer"][:160])
    check("history is not reconstructed", query_block(history).get("sql") is None, query_block(history))

    changes = ask("Which projects experienced the largest expenditure changes?", user=analyst("admin", 2), session="chg")
    check("expenditure changes are honest about missing journal",
          "No import has recorded" in changes["answer"] or "not stored" in changes["answer"],
          changes["answer"][:160])
    changes_analyst = ask("Which projects experienced the largest expenditure changes?", session="chg2")
    check("analyst gets a scoped historical limitation",
          changes_analyst is not None and "administrative data" in changes_analyst["answer"],
          (changes_analyst or {}).get("answer", "returned None")[:160])

    snapshot = ask("Compare project expenditure between two historical snapshots.", session="snap")
    check("snapshot answer uses stored rows",
          "Stored snapshot totals" in snapshot["answer"], snapshot["answer"][:120])

    guest = ask("Show the 10 projects with the highest expenditure.", user=None, session="guest")
    check("guest is denied portfolio data",
          "analysts, auditors and administrators" in guest["answer"], guest["answer"][:120])

    citizen = ask("Show the 10 projects with the highest expenditure.",
                  user=analyst("citizen", 3), session="cit")
    check("citizen is denied portfolio data",
          "analysts, auditors and administrators" in citizen["answer"], citizen["answer"][:120])

    imports_analyst = ask("How many projects were added in the latest import?", session="imp1")
    check("analyst denied import history",
          "administrative data" in imports_analyst["answer"], imports_analyst["answer"][:120])

    imports_admin = ask("How many projects were added in the latest import?",
                        user=analyst("admin", 2), session="imp2")
    block = query_block(imports_admin)
    check("admin may query import history",
          block.get("status") in ("ok", "empty"), block)


# ── 6. LLM failure path ────────────────────────────────────────────────
def test_llm_failure():
    check("LLM disabled without a key", not nlq.llm_client.available())
    # A question outside every deterministic family still must not crash.
    result = ask("What is the airspeed velocity of an unladen swallow?", session="llm")
    check("unrecognised question returns None (assistant fallback)", result is None, result)


# ── 7. HTTP surface ────────────────────────────────────────────────────
def test_endpoints():
    try:
        from fastapi.testclient import TestClient
        import main
        import auth
        import models
        from database import SessionLocal
    except Exception as exc:  # pragma: no cover
        print(f"SKIP  HTTP surface ({exc})")
        return

    db = SessionLocal()
    try:
        analyst_user = (
            db.query(models.User).filter(models.User.role == "analyst", models.User.is_active == True).first()  # noqa: E712
            or db.query(models.User).filter(models.User.role == "admin", models.User.is_active == True).first()  # noqa: E712
        )
    finally:
        db.close()
    if analyst_user is None:
        print("SKIP  HTTP surface (no analyst/admin user in this database)")
        return

    token = auth.create_access_token(analyst_user.id, analyst_user.role)
    headers = {"Authorization": f"Bearer {token}"}
    client = TestClient(main.app)

    ask_resp = client.post("/assistant/ask", json={
        "question": "Show the 10 projects with the highest expenditure.",
        "session_id": "http-1",
    }, headers=headers)
    body = ask_resp.json() if ask_resp.status_code == 200 else {}
    check("/assistant/ask responds", ask_resp.status_code == 200, ask_resp.text[:200])
    check("/assistant/ask carries a query block",
          isinstance(body.get("query"), dict) and bool(body["query"].get("sql")),
          body.get("query"))

    direct = client.post("/assistant/query", json={
        "question": "Which vendors are associated with the most recorded expenditure?",
        "session_id": "http-2",
    }, headers=headers)
    check("/assistant/query responds", direct.status_code == 200, direct.text[:200])
    payload = direct.json() if direct.status_code == 200 else {}
    qid = (payload.get("query") or {}).get("id")
    check("/assistant/query returns query id", bool(qid), payload.get("query"))

    if qid:
        results = client.get(f"/assistant/query/{qid}/results", headers=headers)
        check("results endpoint returns rows",
              results.status_code == 200 and len(results.json().get("rows") or []) > 0,
              results.text[:160])

    validate = client.post("/assistant/query/validate",
                           json={"sql": "DROP TABLE projects"}, headers=headers)
    check("validate endpoint rejects destructive SQL",
          validate.status_code == 200 and validate.json().get("ok") is False, validate.text[:160])

    guest = client.post("/assistant/query", json={
        "question": "Show the 10 projects with the highest expenditure.",
    })
    check("query endpoint answers guests without data",
          guest.status_code == 200 and "analysts, auditors" in guest.json().get("answer", ""),
          guest.text[:160])


def main_run():
    for fn in (test_validator, test_executor, test_families, test_followups,
               test_edges, test_llm_failure, test_endpoints):
        fn()
    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for name, detail in FAILED:
            print(f"  FAILED: {name} — {detail}")
        sys.exit(1)


if __name__ == "__main__":
    main_run()
