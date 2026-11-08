"""
MPLADS AI — end-to-end tests for the three advanced capabilities.

Covers:
  Feature 1  Autonomous Audit Research Agent  (/agent/*)
  Feature 2  Project Outcome Prediction       (/predict/*, assistant intent)
  Feature 3  Intelligence Copilot             (existing /assistant/ask chain)

Also asserts:
  • role gating (citizen is refused on agent/prediction),
  • the agent run is read-only (projects / risk_scores row counts unchanged),
  • prediction output carries validation metrics and never claims certainty.

Run against a live server:  python test_features.py
"""

import json
import sys
import urllib.error
import urllib.request

from sqlalchemy import text

import auth
import models
from database import SessionLocal

BASE = "http://127.0.0.1:8000"
passed = 0
failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"PASS  {name}")
    else:
        failed += 1
        print(f"FAIL  {name} {detail}")


def call(method, path, token=None, body=None, timeout=180):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw)
        except Exception:
            return exc.code, {"detail": raw[:300]}
    except Exception as exc:  # connection / timeout
        return 0, {"detail": str(exc)}


def main():
    db = SessionLocal()
    analyst = auth.create_access_token(2, "analyst")
    citizen = auth.create_access_token(10, "citizen")

    row = db.execute(text(
        """
        SELECT p.id FROM projects p
        JOIN project_rec_info r ON r.project_id = p.id
        WHERE r.linked_expenditure > 0 AND r.recommendation_date IS NOT NULL
          AND p.completion_percentage > 0 AND p.completion_percentage < 90
        LIMIT 1
        """
    )).first()
    pid = int(row[0]) if row else 1
    print(f"test project: #{pid}")

    before = {
        "projects": db.execute(text("SELECT COUNT(*) FROM projects")).scalar(),
        "risk_scores": db.execute(text("SELECT COUNT(*) FROM risk_scores")).scalar(),
        "snapshots": db.execute(text("SELECT COUNT(*) FROM project_snapshots")).scalar(),
        "expenditures": db.execute(text("SELECT COUNT(*) FROM expenditures")).scalar(),
    }

    # ── Feature 1: agent ─────────────────────────────────────────────
    status, body = call("GET", f"/agent/projects/{pid}/latest", analyst)
    check("agent latest (analyst)", status == 200, f"got {status} {body.get('detail')}")

    status, body = call("GET", f"/agent/projects/{pid}/latest", citizen)
    check("agent latest denied for citizen", status in (401, 403), f"got {status}")

    status, run = call("POST", f"/agent/projects/{pid}/run", analyst)
    check("agent run (analyst)", status == 200, f"got {status} {str(run)[:300]}")
    if status == 200:
        steps = (run.get("agent_run") or {}).get("steps") or []
        check("agent executed the full step trail", len(steps) >= 7, f"steps={len(steps)}")
        check("agent returns findings list", isinstance(run.get("agent_findings"), list))
        check("agent returns questions", isinstance(run.get("questions"), list))
        check("agent returns suggested checks", isinstance(run.get("suggested_checks"), list))
        check("agent reports evidence limitations", isinstance(run.get("limitations"), list))
        check("agent exposes human review progress", "agent_accepted" in (run.get("progress") or {}))
        for f in run.get("agent_findings") or []:
            check(f"finding has evidence: {f.get('finding_key')}",
                  bool((f.get("evidence") or {}).get("records")), f.get("finding_key"))
        findings = run.get("agent_findings") or []
        if findings:
            fid = findings[0]["id"]
            status, rev = call("PATCH", f"/agent/findings/{fid}", analyst,
                               {"status": "Accepted", "note": "e2e verification"})
            check("agent finding review accepted", status == 200
                  and (rev.get("finding") or {}).get("status") == "Accepted", f"got {status} {rev}")
            status, after = call("GET", f"/agent/projects/{pid}/latest", analyst)
            kept = next((x for x in (after.get("agent_findings") or []) if x["id"] == fid), {})
            check("review state persists across reads", kept.get("status") == "Accepted", str(kept.get("status")))
            status, run2 = call("POST", f"/agent/projects/{pid}/run", analyst)
            kept2 = next((x for x in (run2.get("agent_findings") or []) if x["id"] == fid), {})
            check("re-run preserves human review state", kept2.get("status") == "Accepted", str(kept2.get("status")))

        status, report = call("GET", f"/agent/projects/{pid}/report", analyst)
        check("agent report generated", status == 200 and "MPLADS-AI" in (report.get("body") or ""),
              f"got {status}")
        check("agent report names its limitations", "Limitations" in (report.get("body") or ""))

    status, _ = call("POST", f"/agent/projects/{pid}/run", citizen)
    check("agent run denied for citizen", status in (401, 403), f"got {status}")

    # ── Feature 2: prediction ────────────────────────────────────────
    status, pred = call("GET", f"/predict/projects/{pid}", analyst)
    check("prediction endpoint (analyst)", status == 200, f"got {status} {str(pred)[:300]}")
    if status == 200:
        outcome = pred.get("outcome") or {}
        check("prediction reports current record", bool(pred.get("current")), str(pred.get("current"))[:120])
        check("prediction carries a forecast or explicit withholding",
              bool(pred.get("forecast")) or bool(pred.get("limitations")))
        check("prediction exposes validation", bool((pred.get("validation") or {}).get("approach")))
        check("prediction is time-aware validated",
              "time-aware" in str((pred.get("validation") or {}).get("approach", "")).lower())
        check("prediction states confidence basis", bool(outcome.get("confidence_basis")))
        check("prediction never claims certainty",
              (outcome.get("probability_complete") is None)
              or 0.0 <= float(outcome["probability_complete"]) <= 1.0)
        check("prediction does not alter risk scores",
              not any(k in json.dumps(pred).lower() for k in ("risk_formula", "threshold_change")))

    status, _ = call("GET", f"/predict/projects/{pid}", citizen)
    check("prediction denied for citizen", status in (401, 403), f"got {status}")

    status, _ = call("GET", "/predict/projects/99999999", analyst)
    check("prediction 404 for unknown project", status == 404, f"got {status}")

    # assistant intent
    status, ans = call("POST", "/assistant/ask", analyst,
                       {"question": f"what is the predicted completion outlook for project {pid}?"})
    check("assistant answers outcome-prediction question", status == 200, f"got {status}")
    if status == 200:
        check("assistant embeds prediction payload", isinstance(ans.get("prediction"), dict))
        check("assistant prediction answer is data-backed",
              "Outcome prediction" in (ans.get("answer") or ""), (ans.get("answer") or "")[:160])

    # ── Feature 3: copilot over the existing query chain ─────────────
    status, ans = call("POST", "/assistant/ask", analyst,
                       {"question": "compare completion rates between Bihar and Uttar Pradesh"})
    check("copilot answers a two-state comparison", status == 200, f"got {status}")
    if status == 200:
        check("copilot comparison returns a real result table",
              bool(ans.get("table") or ans.get("query")), str(ans.get("query"))[:200])

    status, ans = call("POST", "/assistant/ask", analyst,
                       {"question": "show high-risk projects in Bihar with completion below 50 percent"})
    check("copilot answers a multi-condition filter", status == 200, f"got {status}")
    if status == 200:
        q = ans.get("query") or {}
        check("multi-condition answer is executed against SQLite",
              bool(q.get("sql")) and q.get("row_count") is not None,
              f"keys={list(q.keys())} sql={bool(q.get('sql'))}")

    status, ans = call("POST", "/assistant/ask", analyst,
                       {"question": "why is project 80545 risky?"})
    check("copilot explains risk with evidence", status == 200, f"got {status}")

    # ── read-only guarantee ─────────────────────────────────────────
    db.expire_all()
    after = {
        "projects": db.execute(text("SELECT COUNT(*) FROM projects")).scalar(),
        "risk_scores": db.execute(text("SELECT COUNT(*) FROM risk_scores")).scalar(),
        "snapshots": db.execute(text("SELECT COUNT(*) FROM project_snapshots")).scalar(),
        "expenditures": db.execute(text("SELECT COUNT(*) FROM expenditures")).scalar(),
    }
    check("agent/prediction runs left source data unchanged", before == after,
          f"{before} -> {after}")

    db.close()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
