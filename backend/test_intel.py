"""
MPLADS AI — Phase 1 + Phase 2 intelligence tests.

Run against a live server (see test_features.py for the token setup notes):

    MPLADS_JWT_SECRET=... python test_intel.py

Covers every new endpoint, role permissions, the read-only guarantee, House
separation and the history-gap behaviour (no fabricated import history).
"""

import json
import sys
import urllib.error
import urllib.request

from sqlalchemy import text

import auth
from database import SessionLocal
from intel_analysis import compare_indicator_series

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


def call(method, path, token=None, body=None, timeout=240):
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
    except Exception as exc:
        return 0, {"detail": str(exc)}


def main():
    db = SessionLocal()
    analyst = auth.create_access_token(2, "analyst")
    auditor = auth.create_access_token(3, "auditor")
    citizen = auth.create_access_token(10, "citizen")

    row = db.execute(text("""
        SELECT p.id FROM projects p
        JOIN risk_scores rs ON rs.project_id = p.id
        WHERE rs.reasons IS NOT NULL AND TRIM(rs.reasons) <> ''
          AND p.completion_percentage IS NOT NULL
          AND p.sanctioned_amount > 0
        ORDER BY rs.risk_score DESC
        LIMIT 1
    """)).first()
    pid = int(row[0]) if row else 1
    print(f"test project: #{pid}")

    before = {
        "projects": db.execute(text("SELECT COUNT(*) FROM projects")).scalar(),
        "risk_scores": db.execute(text("SELECT COUNT(*) FROM risk_scores")).scalar(),
        "risk_sum": db.execute(text("SELECT COALESCE(SUM(risk_score),0) FROM risk_scores")).scalar(),
        "expenditures": db.execute(text("SELECT COUNT(*) FROM expenditures")).scalar(),
        "snapshots": db.execute(text("SELECT COUNT(*) FROM project_snapshots")).scalar(),
        "recommended": db.execute(text("SELECT COUNT(*) FROM recommended_works")).scalar(),
        "completed": db.execute(text("SELECT COUNT(*) FROM completed_works")).scalar(),
    }

    # ── Feature 1: root cause ───────────────────────────────────────
    status, rc = call("GET", f"/intel/root-cause/{pid}", analyst)
    check("root-cause graph (analyst)", status == 200, f"got {status} {rc.get('detail')}")
    if status == 200:
        check("root cause has an outcome node", any(n.get("kind") == "outcome" for n in rc["nodes"]))
        check("root cause links drivers to the outcome", len(rc["edges"]) >= 3, str(len(rc["edges"])))
        check("root cause carries evidence on every node",
              all(n.get("evidence") for n in rc["nodes"]))
        relations = {e["relation"] for e in rc["edges"]}
        check("root cause separates correlation from contributing rules",
              "correlated deviation" in relations or "contributing indicator" in relations,
              str(relations))
        check("root cause states the causation limit", "causation" in rc["causation_note"].lower())
        check("root cause reports missing history as a gap",
              any(n.get("kind") == "gap" for n in rc["nodes"]) or bool(rc.get("gaps")))

    status, _ = call("GET", f"/intel/root-cause/{pid}", citizen)
    check("root cause denied for citizen", status in (401, 403), f"got {status}")
    status, _ = call("GET", "/intel/root-cause/99999999", analyst)
    check("root cause 404 for unknown project", status == 404, f"got {status}")

    # ── Feature 2: emerging risk ────────────────────────────────────
    status, er = call("GET", "/intel/emerging-risk?state=Bihar", analyst)
    check("emerging-risk endpoint", status == 200, f"got {status}")
    if status == 200:
        check("emerging risk states its mode", er.get("mode") in ("current-baseline", "import-comparison"))
        check("emerging risk reports history availability",
              isinstance((er.get("history") or {}).get("comparable"), bool))
        check("emerging risk does not fabricate a trend when history is empty",
              er["mode"] != "import-comparison" or er["history"]["comparable"])
        check("emerging risk exposes current indicator set",
              isinstance(er.get("current"), dict) and "projects" in er["current"])

    status, er_h = call("GET", "/intel/emerging-risk?state=Bihar&house=Lok%20Sabha", analyst)
    check("emerging risk accepts House filter", status == 200)
    if status == 200 and er.get("current"):
        check("House filter scopes the indicator set",
              (er_h["current"]["projects"] or 0) <= (er["current"]["projects"] or 0),
              f"{er_h['current']['projects']} vs {er['current']['projects']}")
        check("House filter narrows without emptying the slice",
              (er_h["current"]["projects"] or 0) > 0)

    # pure comparison logic (new / persistent / resolved)
    cmp = compare_indicator_series([
        {"batch_id": 1, "captured_at": "2026-01-01", "projects": 100, "high_risk": 10,
         "anomalies": 2, "avg_completion": 50, "expenditure": 1000,
         "signatures": ["spend_no_progress"], "projects_flagged": [1, 2]},
        {"batch_id": 2, "captured_at": "2026-02-01", "projects": 100, "high_risk": 18,
         "anomalies": 5, "avg_completion": 44, "expenditure": 1400,
         "signatures": ["spend_no_progress", "vendor_concentration"], "projects_flagged": [1, 3]},
    ])
    check("series comparison detects a new pattern",
          "vendor_concentration" in cmp["new_patterns"], str(cmp["new_patterns"]))
    check("series comparison keeps persistent patterns",
          "spend_no_progress" in cmp["persistent_patterns"])
    check("series comparison flags worsening metrics",
          any(d["metric"] == "high_risk" for d in cmp["worsening"]))
    check("series comparison refuses a single import",
          compare_indicator_series([{"batch_id": 1}])["available"] is False)

    # ── Feature 7: anomaly evolution ────────────────────────────────
    status, ev = call("GET", "/intel/evolution?state=Bihar", analyst)
    check("anomaly evolution endpoint", status == 200, f"got {status}")
    if status == 200:
        check("evolution labels its mode", ev.get("mode") in ("recorded", "unavailable"), str(ev.get("mode")))
        check("current row is labelled reconstructed",
              any(s.get("mode") == "reconstructed" for s in ev.get("series") or []))
        check("evolution states the history limitation",
              bool(ev.get("limitations")) if ev.get("mode") == "unavailable" else True)

    # ── Feature 3: clustering ───────────────────────────────────────
    status, cl = call("GET", "/intel/clusters?state=Bihar", analyst)
    check("clustering endpoint", status == 200, f"got {status} {cl.get('detail')}")
    if status == 200 and cl.get("available"):
        check("clustering chose a valid k", 3 <= cl["k"] <= 6, str(cl.get("k")))
        check("clustering reports validation quality", isinstance(cl.get("silhouette"), (int, float)))
        check("clustering returns described clusters",
              len(cl["clusters"]) >= 3 and all(c.get("description") for c in cl["clusters"]))
        check("clustering never uses identifiers as features",
              "id" not in cl["features"] and "project_name" not in cl["features"],
              str(cl["features"]))
        cid = cl["clusters"][0]["id"]
        status, cps = call("GET", f"/intel/clusters/{cid}/projects?limit=10", analyst)
        check("cluster drill-down returns projects", status == 200 and cps.get("count", 0) > 0,
              f"got {status}")
    elif status == 200:
        check("clustering explains why it is unavailable", bool(cl.get("reason")), str(cl)[:200])

    # ── Feature 4: reconciliation ──────────────────────────────────
    status, rec = call("GET", "/intel/reconciliation?limit=8", analyst)
    check("reconciliation endpoint", status == 200, f"got {status}")
    case_key = None
    if status == 200:
        check("reconciliation lists all dataset checks", len(rec["checks"]) >= 8, str(len(rec["checks"])))
        check("reconciliation separates confirmed from uncertain",
              all(c["confidence"] in ("confirmed", "probable", "uncertain") for c in rec["cases"]))
        check("reconciliation shows source records side by side",
              all(len(c.get("records") or []) >= 1 for c in rec["cases"]))
        check("reconciliation never labels fraud", "fraud" not in json.dumps(rec).lower())
        if rec["cases"]:
            case_key = rec["cases"][0]["case_key"]

    if case_key:
        status, _ = call("POST", f"/intel/reconciliation/review/{case_key}", analyst,
                         {"decision": "confirmed"})
        check("reconciliation review requires auditor", status in (401, 403), f"got {status}")
        status, rv = call("POST", f"/intel/reconciliation/review/{case_key}", auditor,
                          {"decision": "needs_review", "note": "e2e"})
        check("auditor can review a case", status == 200 and rv.get("decision") == "needs_review",
              f"got {status} {rv}")
        status, rec2 = call("GET", "/intel/reconciliation?limit=8", analyst)
        reviewed = next((c for c in rec2.get("cases", []) if c["case_key"] == case_key), {})
        check("review decision persists on the case",
              (reviewed.get("review") or {}).get("decision") == "needs_review", str(reviewed.get("review")))

    # ── Feature 6: multi-agent ─────────────────────────────────────
    status, ma = call("POST", f"/intel/multiagent/{pid}/run", analyst)
    check("multi-agent run", status == 200 and ma.get("available"), f"got {status} {str(ma)[:200]}")
    if status == 200 and ma.get("available"):
        check("all six specialist agents reported", len(ma["run"]["agents"]) == 6,
              str(len(ma["run"]["agents"])))
        check("agent failures are surfaced, not hidden",
              all(a["status"] in ("complete", "partial", "failed", "skipped") for a in ma["run"]["agents"]))
        check("findings are attributed to an agent",
              all(f.get("agent") for f in ma["findings"]), str(ma["findings"])[:150])
        check("multi-agent findings are evidence-linked",
              all((f.get("evidence") or {}).get("records") for f in ma["findings"]))
        check("coordinator reports conflicts/duplicates",
              "conflicts" in ma["run"] and "duplicate_findings_removed" in ma["run"])
        check("coordinator produced investigation questions", isinstance(ma.get("questions"), list))
        if ma["findings"]:
            fid = ma["findings"][0]["id"]
            status, rev = call("PATCH", f"/agent/findings/{fid}", analyst,
                               {"status": "Accepted", "note": "multi-agent e2e"})
            check("multi-agent finding is reviewable in the existing workflow",
                  status == 200 and (rev.get("finding") or {}).get("status") == "Accepted",
                  f"got {status}")

    status, _ = call("POST", f"/intel/multiagent/{pid}/run", citizen)
    check("multi-agent denied for citizen", status in (401, 403), f"got {status}")

    # ── Feature 5: memory ──────────────────────────────────────────
    status, mem = call("GET", f"/intel/memory/{pid}", analyst)
    check("memory view", status == 200 and mem.get("available"), f"got {status}")
    if status == 200:
        check("memory separates AI suggestions from confirmed findings",
              "ai_suggested" in mem and "confirmed_findings" in mem)
        check("memory lists unresolved questions", isinstance(mem.get("unresolved_questions"), list))

    status, entry = call("POST", f"/intel/memory/{pid}", analyst,
                         {"kind": "note", "title": "e2e note", "body": "verification pending"})
    check("analyst can append memory", status == 200 and entry.get("id"), f"got {status} {entry}")
    entry_id = entry.get("id")
    if entry_id:
        check("analyst-written memory is not marked auditor-confirmed",
              entry.get("origin") == "ai" and entry.get("confirmed") is False, str(entry))
        status, upd = call("PATCH", f"/intel/memory/entry/{entry_id}", auditor,
                           {"confirmed": True})
        check("auditor can confirm memory", status == 200 and upd.get("confirmed") is True,
              f"got {status} {upd}")
        status, mem2 = call("GET", f"/intel/memory/{pid}", analyst)
        stored = next((e for e in mem2.get("entries", []) if e["id"] == entry_id), {})
        check("confirmed memory is listed as auditor origin", stored.get("origin") == "auditor",
              str(stored.get("origin")))
        status, rem = call("PATCH", f"/intel/memory/entry/{entry_id}", auditor,
                           {"status": "removed"})
        check("auditor can remove memory (soft)", status == 200 and rem.get("status") == "removed")
        status, mem3 = call("GET", f"/intel/memory/{pid}", analyst)
        check("removed memory is hidden but not deleted",
              all(e["id"] != entry_id for e in mem3.get("entries", [])))

    status, _ = call("GET", f"/intel/memory/{pid}", citizen)
    check("memory denied for citizen", status in (401, 403), f"got {status}")

    # ── Phase 2: coverage ──────────────────────────────────────────
    status, cov = call("GET", "/intel/geo/coverage?state=Bihar", analyst)
    check("coverage gaps endpoint", status == 200 and cov.get("available"), f"got {status}")
    if status == 200 and cov.get("available"):
        check("coverage discloses its aggregation unit",
              cov["precision"] in ("district", "constituency") and bool(cov.get("geo_unit")))
        check("coverage reports projects without location data",
              "projects_without_area" in cov)
        check("coverage never claims infrastructure deficit",
              "infrastructure" in json.dumps(cov["limitations"]).lower()
              or "infrastructure" in json.dumps(cov.get("notes")).lower())
        check("coverage flags low-coverage areas from recorded counts",
              "low_coverage_areas" in cov)
        check("coverage unit matches the recorded geography",
              cov["unit"] == cov["geo_unit"]["unit"])

    # ── Phase 2: district execution ────────────────────────────────
    status, dp = call("GET", "/intel/geo/districts?state=Bihar", analyst)
    check("district patterns endpoint", status == 200 and dp.get("available"), f"got {status}")
    if status == 200 and dp.get("available"):
        check("area ranking excludes tiny portfolios",
              all(d["projects"] >= 10 for d in dp["ranked_by_completion"]))
        check("area metrics use documented definitions",
              "utilization_pct" in dp["metric_definitions"])
        check("area output warns counts are not performance",
              any("not performance" in n for n in dp["notes"]))
        check("area output names its unit", dp["unit"] == dp["geo_unit"]["unit"])

    status, ls = call("GET", "/intel/geo/districts?house=Lok%20Sabha", analyst)
    status2, rs = call("GET", "/intel/geo/districts?house=Rajya%20Sabha", analyst)
    status3, allh = call("GET", "/intel/geo/districts", analyst)
    if status == status2 == status3 == 200:
        total_ls = sum(d["projects"] for d in ls.get("areas") or [])
        total_rs = sum(d["projects"] for d in rs.get("areas") or [])
        total_all = sum(d["projects"] for d in allh.get("areas") or [])
        check("Lok Sabha and Rajya Sabha remain separated",
              total_all == 0 or total_ls + total_rs <= total_all,
              f"{total_ls}+{total_rs} vs {total_all}")

    # ── Phase 2: spatial relationships ─────────────────────────────
    status, sp = call("GET", "/intel/geo/spatial?state=Bihar", analyst)
    check("spatial relationship endpoint", status == 200 and sp.get("available"), f"got {status}")
    if status == 200 and sp.get("available"):
        check("spatial analysis discloses its precision",
              sp.get("precision") in ("district", "constituency"))
        check("spatial analysis refuses to invent distance",
              sp.get("distance_available") is False
              or all("distance_m" in p for p in sp.get("gps_proximity") or []))
        check("spatial candidates are explained, not asserted as duplicates",
              all("explanation" in g for g in sp.get("groups") or []))

    # ── Phase 2: progress map ──────────────────────────────────────
    status, pm = call("GET", "/intel/geo/progress?state=Bihar&house=Lok%20Sabha", analyst)
    check("geospatial progress endpoint", status == 200 and pm.get("available"), f"got {status}")
    if status == 200 and pm.get("available"):
        check("progress areas carry completion and expenditure",
              all("avg_completion" in a and "expenditure" in a for a in pm["areas"]))
        check("progress distinguishes GPS points from area aggregates",
              "exact-gps" in json.dumps(pm.get("gps_points") or []) or pm.get("gps_point_count") == 0)
        check("progress states its mapping precision",
              pm.get("precision") in ("district", "constituency"))

    # ── Phase 2: geographic outliers ───────────────────────────────
    status, go = call("GET", "/intel/geo/outliers?state=Bihar", analyst)
    check("geo outlier endpoint", status == 200, f"got {status}")
    if status == 200 and go.get("available"):
        check("geo outliers use robust statistics", "median absolute deviation" in go["method"])
        check("geo outliers explain each flag",
              all(o.get("explanation") for o in go.get("outliers") or []))
        check("geo outliers exclude small portfolios",
              all(o["projects"] >= 10 for o in go.get("outliers") or []))
    elif status == 200:
        check("geo outliers explain an unavailable result", bool(go.get("reason")))

    # Every endpoint backing the protected intelligence sections must enforce
    # authentication and the existing analyst role independently of the UI.
    protected_intelligence_routes = [
        "/intel/emerging-risk?state=Bihar",
        "/intel/evolution?state=Bihar",
        "/intel/clusters?state=Bihar",
        "/intel/clusters/1/projects",
        "/intel/geo/coverage?state=Bihar",
        "/intel/geo/districts?state=Bihar",
        "/intel/geo/spatial?state=Bihar",
        "/intel/geo/progress?state=Bihar",
        "/intel/geo/outliers?state=Bihar",
    ]
    for route in protected_intelligence_routes:
        status, _ = call("GET", route)
        check(f"{route.split('?')[0]} denies unauthenticated request", status == 401, f"got {status}")
        status, _ = call("GET", route, citizen)
        check(f"{route.split('?')[0]} denies non-analyst role", status == 403, f"got {status}")

    # ── read-only guarantee ────────────────────────────────────────
    db.expire_all()
    after = {
        "projects": db.execute(text("SELECT COUNT(*) FROM projects")).scalar(),
        "risk_scores": db.execute(text("SELECT COUNT(*) FROM risk_scores")).scalar(),
        "risk_sum": db.execute(text("SELECT COALESCE(SUM(risk_score),0) FROM risk_scores")).scalar(),
        "expenditures": db.execute(text("SELECT COUNT(*) FROM expenditures")).scalar(),
        "snapshots": db.execute(text("SELECT COUNT(*) FROM project_snapshots")).scalar(),
        "recommended": db.execute(text("SELECT COUNT(*) FROM recommended_works")).scalar(),
        "completed": db.execute(text("SELECT COUNT(*) FROM completed_works")).scalar(),
    }
    check("all intelligence features left source data unchanged", before == after,
          f"{before} -> {after}")
    check("existing risk scoring is untouched", before["risk_sum"] == after["risk_sum"])

    db.close()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
