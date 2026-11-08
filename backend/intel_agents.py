"""
MPLADS AI — Multi-agent audit system + investigation memory
===========================================================

Feature 6  Coordinated multi-agent investigation
Feature 5  Persistent investigation memory

Six specialist agents run over ONE shared project context (loaded once), each
producing evidence-linked findings. A lead coordinator merges them, removes
duplicate findings, surfaces conflicting conclusions and writes a consolidated
summary. Every agent is individually guarded: if one fails or exceeds its
budget the run still returns partial results and records which agent failed.

Nothing here recalculates the risk formula, writes to project/financial data
or auto-closes an investigation. Findings land in the existing AgentFinding
review workflow (accept / dismiss / flag) — the auditor decides.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

import audit_intel
import auth
import investigation
import models
from database import normkey

AGENT_KEYS = (
    ("financial", "Financial audit"),
    ("execution", "Execution analysis"),
    ("vendor", "Vendor intelligence"),
    ("historical", "Historical analysis"),
    ("reconciliation", "Data reconciliation"),
    ("evidence", "Evidence review"),
)
COORDINATOR = ("coordinator", "Lead investigation coordinator")

AGENT_BUDGET_SECONDS = 6.0      # per agent, wall clock
RUN_BUDGET_SECONDS = 25.0

FINAL_REVIEW = {"Verified", "Not substantiated", "Resolved"}


def _now() -> str:
    return auth.utc_now().strftime("%Y-%m-%dT%H:%M:%S")


def _num(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _money(v) -> str:
    f = _num(v)
    if f is None:
        return "—"
    if abs(f) >= 1e7:
        return f"₹{f / 1e7:.2f} Cr"
    if abs(f) >= 1e5:
        return f"₹{f / 1e5:.2f} L"
    return f"₹{f:,.0f}"


def _rows(db: Session, sql: str, params: Optional[Dict[str, Any]] = None) -> List[Any]:
    try:
        return db.execute(text(sql), params or {}).fetchall()
    except Exception:
        return []


def _finding(key: str, title: str, explanation: str, confidence: str,
             evidence: List[Dict[str, Any]], verification: Optional[str] = None,
             limitations: Optional[List[str]] = None,
             tags: Optional[List[str]] = None) -> Dict[str, Any]:
    return {
        "key": key, "title": title, "explanation": explanation,
        "confidence": confidence, "evidence": {"records": evidence},
        "suggested_verification": verification,
        "limitations": limitations or [],
        "tags": tags or [],
    }


# ══════════════════════════════════════════════════════════════════════
# Specialist agents — each returns (findings, notes)
# ══════════════════════════════════════════════════════════════════════

def _agent_financial(db: Session, project, ctx: dict) -> Tuple[List[Dict[str, Any]], List[str]]:
    findings: List[Dict[str, Any]] = []
    notes: List[str] = []
    sanc = _num(project.sanctioned_amount) or 0.0
    rec = ctx.get("rec_row")
    exp = _num(rec.linked_expenditure) if rec is not None else None
    if exp is None:
        notes.append("No linked payment total is stored for this work key.")
        findings.append(_finding(
            "no_linked_payments",
            "No linked payment ledger",
            "The project has no linked expenditure total, so financial consistency cannot be assessed.",
            "high",
            [{"table": "project_rec_info", "reference": f"project_id={project.id}",
              "fields": {"linked_expenditure": None}}],
            limitations=["Absence of linkage is a dataset boundary, not evidence that no money moved."],
            tags=["financial", "evidence_gap"],
        ))
        return findings, notes

    util = (exp / sanc * 100.0) if sanc > 0 else None
    if sanc > 0 and exp > sanc * 1.05:
        findings.append(_finding(
            "payments_above_sanction",
            "Recorded payments exceed the sanctioned amount",
            (f"Linked payments total {_money(exp)} against a sanction of {_money(sanc)} "
             f"({util:.1f}% utilization). A revised sanction may explain it; the records do not show one."),
            "medium",
            [{"table": "projects", "reference": f"id={project.id}", "fields": {"sanctioned_amount": sanc}},
             {"table": "project_rec_info", "reference": f"project_id={project.id}",
              "fields": {"linked_expenditure": exp, "work_key_rows": getattr(rec, "work_key_rows", None)}}],
            verification="Obtain the sanction order or revised administrative approval covering the excess.",
            limitations=["Work-key aggregation can include several catalogue rows for similar descriptions."],
            tags=["financial", "overspend"],
        ))
    elif util is not None and util < 20 and (project.completion_percentage or 0) < 50:
        findings.append(_finding(
            "low_utilization_slow_progress",
            "Low utilization with slow progress",
            (f"Only {util:.1f}% of the sanction is recorded as paid while reported progress is "
             f"{project.completion_percentage if project.completion_percentage is not None else 'not recorded'}%."),
            "low",
            [{"table": "project_rec_info", "reference": f"project_id={project.id}",
              "fields": {"linked_expenditure": exp, "utilization_pct": round(util, 2), "sanctioned": sanc}}],
            verification="Confirm with the implementing agency whether the work has started and whether payments are pending.",
            tags=["financial", "utilization"],
        ))

    wk = getattr(rec, "work_key_expenditure", None)
    if wk is not None and exp and abs(wk - exp) > max(1.0, 0.05 * max(exp, 1)):
        findings.append(_finding(
            "financial_totals_disagree",
            "Two stored expenditure totals disagree",
            (f"Work-key total {_money(wk)} differs from linked project total {_money(exp)}."),
            "high",
            [{"table": "project_rec_info", "reference": f"project_id={project.id}",
              "fields": {"work_key_expenditure": _num(wk), "linked_expenditure": exp}}],
            verification="Reconcile the two totals against the source payment file.",
            tags=["financial", "reconciliation"],
        ))
    return findings, notes


def _agent_execution(db: Session, project, ctx: dict) -> Tuple[List[Dict[str, Any]], List[str]]:
    findings: List[Dict[str, Any]] = []
    notes: List[str] = []
    comp = _num(project.completion_percentage)
    status = (project.status or "").strip()
    rec = ctx.get("rec_row")
    recd = str(getattr(rec, "recommendation_date", "") or "")[:10]
    age = None
    if len(recd) == 10 and recd[4] == "-":
        try:
            from datetime import date
            age = max(0.0, (date.today() - date(int(recd[:4]), int(recd[5:7]), int(recd[8:10]))).days / 30.4375)
        except Exception:
            age = None

    if status.lower() == "completed" and comp is not None and comp < 90:
        findings.append(_finding(
            "status_progress_conflict",
            "Status and recorded progress disagree",
            f"Status is '{status}' but physical progress is {comp}%.",
            "high",
            [{"table": "projects", "reference": f"id={project.id}",
              "fields": {"status": status, "completion_percentage": comp}}],
            verification="Confirm which of the two recorded values is current.",
            tags=["execution", "status"],
        ))
    if age is not None and age > 24 and (comp or 0) < 50 and status.lower() != "completed":
        findings.append(_finding(
            "stalled_by_age",
            "Long-running work with low reported progress",
            (f"The work has been on record for roughly {age:.0f} months with "
             f"{comp if comp is not None else 'no'}% recorded progress."),
            "medium",
            [{"table": "project_rec_info", "reference": f"project_id={project.id}",
              "fields": {"recommendation_date": recd, "age_months": round(age, 1)}},
             {"table": "projects", "reference": f"id={project.id}",
              "fields": {"completion_percentage": comp, "status": status}}],
            verification="Check the latest physical progress report and any revised timeline.",
            limitations=["Age alone is context; only a recorded progress shortfall makes it an indicator."],
            tags=["execution", "delay"],
        ))
    if comp is None:
        notes.append("Completion percentage is not recorded, so progress indicators were skipped.")
    return findings, notes


def _agent_vendor(db: Session, project, ctx: dict) -> Tuple[List[Dict[str, Any]], List[str]]:
    findings: List[Dict[str, Any]] = []
    notes: List[str] = []
    rows = ctx.get("vendors") or []
    if not rows:
        notes.append("No vendor-attributed payments exist for this work key.")
        return findings, notes
    total = sum(_num(r[1]) or 0 for r in rows)
    if total <= 0:
        return findings, notes
    top = rows[0]
    share = (_num(top[1]) or 0) / total
    if share >= 0.8 and total >= 500000:
        findings.append(_finding(
            "vendor_concentration",
            "One vendor accounts for nearly all recorded payments",
            (f"{str(top[0]).strip()} received {_money(_num(top[1]))} of {_money(total)} "
             f"({share * 100:.0f}%) across {int(_num(top[2]) or 0)} transaction(s)."),
            "low",
            [{"table": "expenditures", "reference": f"vendor={str(top[0]).strip()}",
              "fields": {"paid": _num(top[1]), "share_pct": round(share * 100, 1),
                         "transactions": int(_num(top[2]) or 0),
                         "first": str(top[3])[:10], "last": str(top[4])[:10]}}],
            verification="Compare the vendor's other works for scope, rates and completion consistency.",
            limitations=["Vendor names are as recorded; spelling variants can overstate concentration."],
            tags=["vendor", "concentration"],
        ))
    # Shared vendor across other works (bounded, exact name match).
    name = str(top[0]).strip()
    others = _rows(db, """
        SELECT COUNT(DISTINCT r.project_id), SUM(e.expenditure_amount)
        FROM expenditures e
        JOIN project_rec_info r ON r.work_key = normkey(e.work_description)
        WHERE e.vendor = :v AND r.project_id <> :pid
    """, {"v": name, "pid": project.id})
    if others and (_num(others[0][0]) or 0) > 0:
        findings.append(_finding(
            "vendor_shared_across_works",
            "Vendor also appears on other recorded works",
            (f"The same vendor is recorded on {int(_num(others[0][0]) or 0)} other project(s) "
             f"totalling {_money(_num(others[0][1]))}. Sharing vendors is normal; the connection is "
             "surfaced so concentration can be checked."),
            "low",
            [{"dataset": "expenditures + project_rec_info", "reference": f"vendor={name}",
              "fields": {"other_projects": int(_num(others[0][0]) or 0),
                         "paid_on_others": _num(others[0][1])}}],
            verification="Profile the vendor in Vendor Intelligence before drawing conclusions.",
            tags=["vendor", "network"],
        ))
    return findings, notes


def _agent_historical(db: Session, project, ctx: dict) -> Tuple[List[Dict[str, Any]], List[str]]:
    findings: List[Dict[str, Any]] = []
    notes: List[str] = []
    snaps = _rows(db, """
        SELECT batch_id, captured_at, completion_percentage, expenditure
        FROM project_snapshots WHERE project_id = :pid
        ORDER BY captured_at ASC, batch_id ASC
    """, {"pid": project.id})
    events = _rows(db, """
        SELECT field, previous_value, new_value, changed_at
        FROM project_change_events WHERE project_id = :pid
        ORDER BY changed_at ASC LIMIT 40
    """, {"pid": project.id})
    if len(snaps) < 2:
        notes.append(
            f"Only {len(snaps)} stored snapshot(s) and {len(events)} change event(s) exist for this "
            "project, so no trend can be established.")
        return findings, notes
    first, last = snaps[0], snaps[-1]
    for idx, label in ((3, "recorded expenditure"), (2, "reported completion")):
        a, b = _num(first[idx]), _num(last[idx])
        if a is None or b is None or a == b:
            continue
        if idx == 2 and b < a:
            findings.append(_finding(
                "completion_regression",
                "Reported completion moved backwards between imports",
                (f"Completion changed from {a} to {b} between batch {first[0]} "
                 f"({str(first[1])[:10]}) and batch {last[0]} ({str(last[1])[:10]})."),
                "high",
                [{"table": "project_snapshots", "reference": f"batch {first[0]}", "fields": {"completion": a}},
                 {"table": "project_snapshots", "reference": f"batch {last[0]}", "fields": {"completion": b}}],
                verification="Confirm whether the change was a data correction or a revised report.",
                tags=["historical", "regression"],
            ))
        if idx == 3 and b < a:
            findings.append(_finding(
                "expenditure_regression",
                "Recorded expenditure decreased between imports",
                f"Expenditure changed from {_money(a)} to {_money(b)} between the stored snapshots.",
                "high",
                [{"table": "project_snapshots", "reference": f"batch {first[0]}", "fields": {"expenditure": a}},
                 {"table": "project_snapshots", "reference": f"batch {last[0]}", "fields": {"expenditure": b}}],
                verification="Check the import files for a correction or a rollback.",
                tags=["historical", "regression"],
            ))
    return findings, notes


def _agent_reconciliation(db: Session, project, ctx: dict) -> Tuple[List[Dict[str, Any]], List[str]]:
    findings: List[Dict[str, Any]] = []
    notes: List[str] = []
    rec = ctx.get("rec_row")
    wk = str(getattr(rec, "work_key", "") or "")
    if wk:
        same = _rows(db, """
            SELECT COUNT(*) FROM project_rec_info WHERE work_key = :wk
        """, {"wk": wk})
        n = int(_num(same[0][0]) or 0) if same else 0
        if n > 1:
            findings.append(_finding(
                "work_key_shared",
                "Several project rows share one work linkage key",
                (f"{n} project rows carry the same linkage key, so per-project financial values "
                 "may be aggregates rather than row-level amounts."),
                "medium",
                [{"table": "project_rec_info", "reference": f"work_key={wk[:60]}",
                  "fields": {"project_rows": n}}],
                verification="Confirm which catalogue row owns the payment total.",
                tags=["reconciliation", "linkage"],
            ))
    txs = _rows(db, """
        SELECT COUNT(*), SUM(expenditure_amount) FROM expenditures
        WHERE normkey(work_description) = :d AND normkey(constituency) = :c AND normkey(state) = :s
    """, {"d": normkey(project.project_name or ""), "c": normkey(project.constituency or ""),
          "s": normkey(project.state or "")})
    if txs:
        n = int(_num(txs[0][0]) or 0)
        total = _num(txs[0][1]) or 0
        linked = _num(getattr(rec, "linked_expenditure", None)) or 0
        if n and abs(total - linked) > max(1.0, 0.05 * max(linked, 1)):
            findings.append(_finding(
                "ledger_vs_linkage_gap",
                "Payment rows and linked total disagree",
                (f"{n} payment row(s) total {_money(total)} for this work description, while the "
                 f"linked project total is {_money(linked)}."),
                "medium",
                [{"table": "expenditures", "reference": "normalized work description match",
                  "fields": {"rows": n, "sum": total}},
                 {"table": "project_rec_info", "reference": f"project_id={project.id}",
                  "fields": {"linked_expenditure": linked}}],
                verification="Trace both totals to the source payment file.",
                limitations=["Description matching can include similar works in the same constituency."],
                tags=["reconciliation", "amount"],
            ))
    return findings, notes


def _agent_evidence(db: Session, project, ctx: dict) -> Tuple[List[Dict[str, Any]], List[str]]:
    findings: List[Dict[str, Any]] = []
    notes: List[str] = []
    dossier = ctx.get("dossier") or {}
    evidence = dossier.get("evidence") or {}
    if (evidence.get("verification_count") or 0) == 0:
        findings.append(_finding(
            "no_ground_verification",
            "No ground verification is linked to this project",
            ("No citizen, verifier or inspector report is recorded for this project, so the "
             "recorded status and completion have no field corroboration in the system."),
            "high",
            [{"table": "verification_reports", "reference": f"project_id={project.id}",
              "fields": {"count": 0}}],
            verification="Request a field verification and attach photographic evidence.",
            limitations=["Absence of a linked report means no report was recorded here — not that no inspection happened."],
            tags=["evidence", "gap"],
        ))
    gaps = ctx.get("gaps") or {}
    for g in (gaps.get("gaps") or [])[:2]:
        if g.get("summary"):
            findings.append(_finding(
                f"evidence_gap::{str(g.get('key') or 'gap')[:40]}",
                "Missing evidence limits the assessment",
                str(g.get("summary"))[:400],
                "medium",
                [{"source": "audit_intel.evidence_gaps", "fields": {"gap": str(g.get("key") or "")}}],
                verification="Document the source of the missing record.",
                tags=["evidence", "gap"],
            ))
    return findings, notes


AGENT_FUNCS: Dict[str, Callable[..., Tuple[List[Dict[str, Any]], List[str]]]] = {
    "financial": _agent_financial,
    "execution": _agent_execution,
    "vendor": _agent_vendor,
    "historical": _agent_historical,
    "reconciliation": _agent_reconciliation,
    "evidence": _agent_evidence,
}


# ══════════════════════════════════════════════════════════════════════
# Orchestration
# ══════════════════════════════════════════════════════════════════════

def _context(db: Session, project) -> Dict[str, Any]:
    """Load every shared input ONCE and hand it to all agents."""
    ctx: Dict[str, Any] = {}
    try:
        ctx["rec_row"] = (db.query(models.ProjectRecInfo)
                          .filter(models.ProjectRecInfo.project_id == project.id).first())
    except Exception:
        ctx["rec_row"] = None
    ctx["vendors"] = _rows(db, """
        SELECT vendor, SUM(expenditure_amount) AS paid, COUNT(*) AS tx,
               MIN(expenditure_date) AS first_tx, MAX(expenditure_date) AS last_tx
        FROM expenditures
        WHERE normkey(work_description) = :d AND normkey(constituency) = :c AND normkey(state) = :s
          AND vendor IS NOT NULL AND TRIM(vendor) <> ''
        GROUP BY vendor ORDER BY paid DESC LIMIT 5
    """, {"d": normkey(project.project_name or ""),
          "c": normkey(project.constituency or ""),
          "s": normkey(project.state or "")})
    try:
        payload = audit_intel.get_investigation(db, project)
        ctx["dossier"] = (payload or {}).get("dossier") or {}
        ctx["dossier_findings"] = (payload or {}).get("findings") or []
    except Exception:
        ctx["dossier"] = {}
        ctx["dossier_findings"] = []
    try:
        ctx["gaps"] = audit_intel.evidence_gaps(
            project, timeline=ctx["dossier"].get("timeline"),
            activity=ctx["dossier"].get("financial")) or {}
    except Exception:
        ctx["gaps"] = {}
    return ctx


def _ensure_investigation(db: Session, project, user) -> models.AuditInvestigation:
    row = (db.query(models.AuditInvestigation)
           .filter(models.AuditInvestigation.project_id == project.id).first())
    if row is None:
        audit_intel.start_investigation(db, project, actor=user)
        row = (db.query(models.AuditInvestigation)
               .filter(models.AuditInvestigation.project_id == project.id).first())
    return row


def run_multi_agent(db: Session, project, user) -> Dict[str, Any]:
    investigation_row = _ensure_investigation(db, project, user)
    started = time.monotonic()
    ctx = _context(db, project)

    run = models.AgentRun(
        project_id=project.id,
        investigation_id=investigation_row.id,
        status="running",
        run_by_user_id=getattr(user, "id", None),
        run_by_name=getattr(user, "name", None),
        created_at=_now(),
    )
    db.add(run)
    db.flush()

    agents_status: List[Dict[str, Any]] = []
    all_findings: List[Dict[str, Any]] = []
    notes: List[str] = []
    total_budget_exhausted = False

    for key, label in AGENT_KEYS:
        if time.monotonic() - started > RUN_BUDGET_SECONDS:
            total_budget_exhausted = True
            agents_status.append({"key": key, "label": label, "status": "skipped",
                                  "findings": 0, "elapsed_ms": 0,
                                  "error": "run budget exhausted"})
            continue
        t0 = time.monotonic()
        try:
            findings, agent_notes = AGENT_FUNCS[key](db, project, ctx)
            elapsed = (time.monotonic() - t0) * 1000
            if elapsed > AGENT_BUDGET_SECONDS * 1000:
                agents_status.append({"key": key, "label": label, "status": "partial",
                                      "findings": len(findings), "elapsed_ms": round(elapsed, 1),
                                      "error": "agent exceeded its time budget"})
            else:
                agents_status.append({"key": key, "label": label, "status": "complete",
                                      "findings": len(findings), "elapsed_ms": round(elapsed, 1),
                                      "error": None})
            for f in findings:
                f["agent"] = key
                all_findings.append(f)
            notes.extend([f"{label}: {n}" for n in agent_notes])
        except Exception as exc:
            agents_status.append({"key": key, "label": label, "status": "failed",
                                  "findings": 0,
                                  "elapsed_ms": round((time.monotonic() - t0) * 1000, 1),
                                  "error": exc.__class__.__name__})

    # ── Coordinator: dedupe by key, detect conflicting conclusions ──
    seen: Dict[str, Dict[str, Any]] = {}
    duplicates: List[str] = []
    for f in all_findings:
        if f["key"] in seen:
            duplicates.append(f["key"])
            continue
        seen[f["key"]] = f
    merged = list(seen.values())

    conflicts: List[Dict[str, Any]] = []
    by_tag: Dict[str, List[Dict[str, Any]]] = {}
    for f in merged:
        for tag in f.get("tags") or []:
            by_tag.setdefault(tag, []).append(f)
    if by_tag.get("overspend") and by_tag.get("evidence_gap"):
        conflicts.append({
            "between": ["payments_above_sanction", "no_linked_payments"],
            "description": ("The financial agent reports payments above sanction while another finding "
                            "states no linked payment total exists. Both cannot be relied on until the "
                            "linkage is confirmed."),
        })
    if by_tag.get("status") and by_tag.get("regression"):
        conflicts.append({
            "between": ["status_progress_conflict", "completion_regression"],
            "description": ("A status/progress conflict and a historical completion regression both "
                            "concern recorded progress; the import history should decide which is current."),
        })

    failed = [a for a in agents_status if a["status"] in ("failed", "skipped")]
    run.status = "partial" if (failed or total_budget_exhausted) else "complete"

    summary_lines = [
        f"Coordinated multi-agent audit of project #{project.id}.",
        f"{len(merged)} distinct finding(s) from "
        f"{sum(1 for a in agents_status if a['status'] in ('complete', 'partial'))} agent(s); "
        f"{len(failed)} agent(s) unavailable.",
        "The coordinator merged duplicate findings and recorded conflicts; it did not resolve them.",
    ]

    steps = [{
        "key": "multi_agent", "title": "Coordinated multi-agent audit",
        "detail": " ".join(summary_lines),
        "records": [{"type": "agents", "count": len(agents_status)}],
    }]
    questions = _coordinator_questions(merged, ctx)

    run.steps_json = json.dumps({"agents": agents_status, "steps": steps,
                                 "conflicts": conflicts, "duplicates": duplicates,
                                 "notes": notes[:20]}, ensure_ascii=False)
    run.questions_json = json.dumps(questions, ensure_ascii=False)
    run.fields_followed = ", ".join(a["key"] for a in agents_status if a["status"] != "skipped")

    # Persist findings, preserving any human review already recorded.
    existing = {
        row.finding_key: row
        for row in db.query(models.AgentFinding)
        .filter(models.AgentFinding.investigation_id == investigation_row.id).all()
    }
    for f in merged:
        fkey = f"ma::{f['agent']}::{f['key']}"[:80]
        row = existing.get(fkey)
        payload_evidence = dict(f.get("evidence") or {})
        payload_evidence["agent"] = f["agent"]
        if row is None:
            row = models.AgentFinding(
                project_id=project.id, investigation_id=investigation_row.id, run_id=run.id,
                finding_key=fkey, title=f["title"][:240], explanation=f["explanation"],
                evidence_json=json.dumps(payload_evidence, ensure_ascii=False),
                confidence=f["confidence"], suggested_verification=f.get("suggested_verification"),
                limitations_json=json.dumps(f.get("limitations") or [], ensure_ascii=False),
                status="New", created_at=_now(),
            )
            db.add(row)
        else:
            row.explanation = f["explanation"]
            row.evidence_json = json.dumps(payload_evidence, ensure_ascii=False)
            row.confidence = f["confidence"]
            row.suggested_verification = f.get("suggested_verification")
            row.limitations_json = json.dumps(f.get("limitations") or [], ensure_ascii=False)
            row.run_id = run.id
            db.add(row)
    db.commit()

    return multi_agent_payload(db, project, run=run, investigation_row=investigation_row)


def _coordinator_questions(findings: List[Dict[str, Any]], ctx: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for f in findings:
        if f["confidence"] == "high":
            out.append({
                "question": f"What record confirms or contradicts: {f['title']}?",
                "raises_from": f"{f['agent']} agent — {f['key']}",
            })
    if not any(f["key"] == "no_ground_verification" for f in findings):
        out.append({
            "question": "Should a follow-up field verification be scheduled?",
            "raises_from": "evidence agent",
        })
    for g in (ctx.get("gaps") or {}).get("gaps", [])[:2]:
        if g.get("summary"):
            out.append({"question": f"How can this missing evidence be obtained — {str(g['summary'])[:140]}?",
                        "raises_from": "evidence gap analysis"})
    deduped, seen = [], set()
    for q in out:
        if q["question"][:70] in seen:
            continue
        seen.add(q["question"][:70])
        deduped.append(q)
    return deduped[:8]


def multi_agent_payload(db: Session, project, run: Optional[models.AgentRun] = None,
                        investigation_row: Optional[models.AuditInvestigation] = None) -> Dict[str, Any]:
    if investigation_row is None:
        investigation_row = (db.query(models.AuditInvestigation)
                             .filter(models.AuditInvestigation.project_id == project.id).first())
    if run is None:
        run = (db.query(models.AgentRun)
               .filter(models.AgentRun.project_id == project.id,
                       models.AgentRun.investigation_id == (investigation_row.id if investigation_row else -1))
               .order_by(models.AgentRun.id.desc()).first())
    if run is None or investigation_row is None:
        return {"available": False, "reason": "No multi-agent run exists for this project yet."}

    try:
        data = json.loads(run.steps_json or "{}")
    except Exception:
        data = {}

    rows = (db.query(models.AgentFinding)
            .filter(models.AgentFinding.investigation_id == investigation_row.id,
                    models.AgentFinding.finding_key.like("ma::%"))
            .order_by(models.AgentFinding.id.desc()).all())
    rank = {"high": 0, "medium": 1, "low": 2}
    rows.sort(key=lambda r: (rank.get(r.confidence, 3), -(r.id or 0)))

    findings = []
    for r in rows:
        try:
            ev = json.loads(r.evidence_json or "{}")
        except Exception:
            ev = {}
        findings.append({
            "id": r.id, "agent": ev.get("agent") or (r.finding_key.split("::")[1] if "::" in r.finding_key else "unknown"),
            "finding_key": r.finding_key, "title": r.title, "explanation": r.explanation,
            "confidence": r.confidence, "status": r.status,
            "suggested_verification": r.suggested_verification,
            "evidence": ev,
            "limitations": json.loads(r.limitations_json) if r.limitations_json else [],
            "reviewer_note": r.reviewer_note, "reviewer_name": r.reviewer_name,
            "reviewed_at": r.reviewed_at,
        })

    counts = {
        "total": len(findings),
        "accepted": sum(1 for f in findings if f["status"] == "Accepted"),
        "dismissed": sum(1 for f in findings if f["status"] == "Dismissed"),
        "flagged": sum(1 for f in findings if f["status"] == "Flagged for Verification"),
        "new": sum(1 for f in findings if f["status"] == "New"),
    }
    by_agent: Dict[str, int] = {}
    for f in findings:
        by_agent[f["agent"]] = by_agent.get(f["agent"], 0) + 1

    return {
        "available": True,
        "project_id": project.id,
        "run": {
            "id": run.id, "status": run.status, "created_at": run.created_at,
            "run_by": run.run_by_name,
            "agents": data.get("agents") or [],
            "conflicts": data.get("conflicts") or [],
            "duplicate_findings_removed": len(data.get("duplicates") or []),
            "notes": data.get("notes") or [],
        },
        "findings": findings,
        "findings_by_agent": by_agent,
        "counts": counts,
        "questions": json.loads(run.questions_json) if run.questions_json else [],
        "investigation": {"id": investigation_row.id, "status": investigation_row.status},
        "disclaimers": [
            "Specialist agents read existing records; they never modify project or financial data.",
            "Coordinator output is a review aid, not a confirmed audit conclusion.",
            "Agents that failed or exceeded their budget are listed explicitly; partial results are labelled.",
        ],
    }


# ══════════════════════════════════════════════════════════════════════
# FEATURE 5 — Investigation memory
# ══════════════════════════════════════════════════════════════════════

def memory_view(db: Session, project_id: int) -> Dict[str, Any]:
    project = db.query(models.Project).filter(models.Project.id == project_id).first()
    if project is None:
        return {"available": False, "reason": f"Project {project_id} not found."}
    inv = (db.query(models.AuditInvestigation)
           .filter(models.AuditInvestigation.project_id == project_id).first())

    entries = (db.query(models.InvestigationMemory)
               .filter(models.InvestigationMemory.project_id == project_id,
                       models.InvestigationMemory.status == "active")
               .order_by(models.InvestigationMemory.id.desc()).all())

    confirmed: List[Dict[str, Any]] = []
    dismissed: List[Dict[str, Any]] = []
    ai_suggested: List[Dict[str, Any]] = []

    if inv is not None:
        rows = (db.query(models.AgentFinding)
                .filter(models.AgentFinding.investigation_id == inv.id)
                .order_by(models.AgentFinding.id.desc()).all())
        for r in rows:
            item = {
                "id": r.id, "title": r.title, "explanation": r.explanation,
                "status": r.status, "confidence": r.confidence,
                "reviewer": r.reviewer_name, "reviewer_note": r.reviewer_note,
                "reviewed_at": r.reviewed_at, "origin": "ai-suggested",
                "key": r.finding_key,
            }
            if r.status == "Accepted":
                item["origin"] = "auditor-confirmed"
                confirmed.append(item)
            elif r.status == "Dismissed":
                item["origin"] = "auditor-dismissed"
                dismissed.append(item)
            else:
                ai_suggested.append(item)

    unresolved_questions: List[Dict[str, Any]] = []
    run = (db.query(models.AgentRun)
           .filter(models.AgentRun.project_id == project_id)
           .order_by(models.AgentRun.id.desc()).first())
    if run is not None and run.questions_json:
        try:
            unresolved_questions = json.loads(run.questions_json)
        except Exception:
            unresolved_questions = []

    # Related investigations: same state + category, analyst-visible only.
    related = _rows(db, """
        SELECT p.id, p.project_name, p.state, p.status, i.status
        FROM projects p
        JOIN audit_investigations i ON i.project_id = p.id
        WHERE p.id <> :pid AND p.state = :st
          AND COALESCE(p.project_type,'') = COALESCE(:cat,'')
          AND i.status <> 'New'
        ORDER BY i.id DESC LIMIT 8
    """, {"pid": project_id, "st": project.state, "cat": project.project_type})

    memory_entries = [{
        "id": e.id, "kind": e.kind, "title": e.title, "body": e.body,
        "source": e.source, "origin": e.origin, "confirmed": bool(e.confirmed),
        "related_project_id": e.related_project_id,
        "created_by": e.created_by_name, "created_at": e.created_at,
        "updated_at": e.updated_at,
    } for e in entries]

    return {
        "available": True,
        "project_id": project_id,
        "investigation": ({"id": inv.id, "status": inv.status, "note": inv.note}
                          if inv is not None else None),
        "entries": memory_entries,
        "confirmed_findings": confirmed[:40],
        "dismissed_findings": dismissed[:40],
        "ai_suggested": ai_suggested[:40],
        "unresolved_questions": unresolved_questions,
        "related_investigations": [
            {"project_id": int(r[0]), "name": r[1], "state": r[2],
             "project_status": r[3], "investigation_status": r[4]}
            for r in related
        ],
        "counts": {
            "entries": len(memory_entries),
            "confirmed": len(confirmed),
            "dismissed": len(dismissed),
            "ai_suggested": len(ai_suggested),
            "unresolved_questions": len(unresolved_questions),
        },
        "rules": [
            "AI-generated findings stay labelled 'ai-suggested' until a human accepts them.",
            "Only auditor/admin accounts can confirm or remove stored memory.",
            "Related-investigation context is limited to the same state and project category.",
        ],
    }


def add_memory(db: Session, project_id: int, payload: Dict[str, Any], user) -> Dict[str, Any]:
    inv = (db.query(models.AuditInvestigation)
           .filter(models.AuditInvestigation.project_id == project_id).first())
    row = models.InvestigationMemory(
        project_id=project_id,
        investigation_id=inv.id if inv is not None else None,
        kind=(payload.get("kind") or "note")[:32],
        title=(payload.get("title") or "")[:240] or None,
        body=(payload.get("body") or "")[:4000] or None,
        source=(payload.get("source") or "")[:240] or None,
        origin="auditor" if auth.has_role_rank(user, "auditor") else "ai",
        confirmed=bool(payload.get("confirmed")) and auth.has_role_rank(user, "auditor"),
        related_project_id=payload.get("related_project_id"),
        created_by_user_id=getattr(user, "id", None),
        created_by_name=getattr(user, "name", None),
        created_at=_now(),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"id": row.id, "origin": row.origin, "confirmed": bool(row.confirmed),
            "created_at": row.created_at}


def update_memory(db: Session, entry_id: int, payload: Dict[str, Any], user) -> Optional[Dict[str, Any]]:
    row = db.query(models.InvestigationMemory).filter(models.InvestigationMemory.id == entry_id).first()
    if row is None:
        return None
    is_auditor = auth.has_role_rank(user, "auditor")
    owns = row.created_by_user_id == getattr(user, "id", None)
    if not (is_auditor or owns):
        return {"forbidden": True}
    if "body" in payload:
        row.body = (payload.get("body") or "")[:4000] or None
    if "title" in payload:
        row.title = (payload.get("title") or "")[:240] or None
    if "confirmed" in payload and is_auditor:
        row.confirmed = bool(payload.get("confirmed"))
        row.origin = "auditor"
    if payload.get("status") in ("active", "removed") and is_auditor:
        row.status = payload["status"]
    row.updated_at = _now()
    db.commit()
    return {"id": row.id, "status": row.status, "confirmed": bool(row.confirmed),
            "origin": row.origin, "updated_at": row.updated_at}
