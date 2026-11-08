"""
MPLADS AI — Autonomous Audit Research Agent (Feature 1)
=======================================================

An AI research agent integrated into the EXISTING investigation workflow
(audit_intel dossier + AuditIntelligence InvestigationWorkspace). When an
analyst/auditor triggers a run, the agent autonomously performs a
multi-step investigation over the platform's own data and APIs:

    1. context     — load the project's recorded context (projects,
                     project_rec_info linkage, payments, vendors).
    2. dossier     — generate/refresh the existing evidence-linked dossier
                     and findings (audit_intel engine — no new formulas).
    3. trail       — examine the linked payment trail (dates, vendors).
    4. history     — read stored historical import snapshots / change
                     events for this project (change_tracking).
    5. peers       — benchmark against the same peer engine Risk Center uses.
    6. connections — follow vendor connections to OTHER recorded works
                     (shared vendors across works pay amounts).
    7. gaps        — collect evidence gaps from the existing gap calculators.

Output: an ordered step log, agents' own CONNECTION-LEVEL findings (each with
title, explanation, supporting records, dates/amounts, why it matters,
confidence, recommended verification steps and evidence limitations),
investigation questions, and suggested additional checks. Dossier findings
remain the same regenerated evidence set; the agent never edits them.

Hard safety rules (enforced here, not just by convention):
    • every lookup is READ-ONLY through SQLAlchemy (no writes to project or
      financial data; the agent stores only its own run/findings/notes);
    • evidence is never fabricated — a finding exists only when the
      underlying records support it, and missing data is reported as a
      limitation;
    • risk scores are treated as indicators, never proof;
    • the agent NEVER auto-closes an investigation and never finalises
      findings — final states stay with the auditor via the existing review
      workflow;
    • every endpoint is analyst+ role-gated (server-enforced).
"""

import json
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

import activity
import audit_intel
import auth
import change_tracking
import investigation
import models
from database import SessionLocal

router = APIRouter(prefix="/agent", tags=["AI Operations"])

RUN_STEPS = (
    ("context", "Understand the project context",
     "Loaded recorded project identity, sanction, linked payments, vendors and risk results."),
    ("dossier", "Generate evidence-linked findings",
     "Refreshed the existing dossier findings from projects, payments, risk and verification records."),
    ("trail", "Examine the payment trail",
     "Read the linked expenditure transactions: dates, amounts and vendors around the work key."),
    ("history", "Read historical changes",
     "Checked stored import snapshots and change events for this project."),
    ("peers", "Compare with peer works",
     "Benchmarked the project against comparable works using the existing peer engine."),
    ("connections", "Follow vendor connections",
     "Traced each recorded vendor of this work to other recorded works that share the same vendor."),
    ("gaps", "Identify evidence gaps",
     "Collected missing-information and verification-coverage gaps from the existing calculators."),
    ("questions", "Prepare investigation questions",
     "Turned the findings and gaps into concrete questions an auditor can act on."),
)

DISCLAIMERS = [
    "The research agent only reads existing records. It never modifies project data, risk scores or investigation states.",
    "Findings are evidence-linked review indicators — not proof of wrongdoing. Only an auditor can accept or finalise them.",
]

_MAX_VENDOR_WORKS = 20          # per-vendor related works cap
_MAX_QUESTIONS = 8


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _now() -> str:
    return auth.utc_now().strftime("%Y-%m-%dT%H:%M:%S")


def _num(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _money(v) -> str:
    v = _num(v)
    if v >= 1e7:
        return f"₹{v / 1e7:.2f} Cr"
    if v >= 1e5:
        return f"₹{v / 1e5:.2f} L"
    return f"₹{v:,.0f}"


def _date(d) -> Optional[str]:
    s = str(d or "")[:10]
    return s if len(s) == 10 and s[4] == "-" else None


# ═════════════════════════════════════════════════════════════════════
# Agent research steps (all read-only)
# ═════════════════════════════════════════════════════════════════════

def _payment_trail(db: Session, project: models.Project, rec: Optional[models.ProjectRecInfo]) -> Dict[str, Any]:
    """Linked transactions around the work key — same conservative match
    used by timeline/activity (normalized description + constituency + state)."""
    out: Dict[str, Any] = {"transactions": [], "span_days": None, "vendors": []}
    try:
        span = activity.get_expenditure_span(project)
        out["span_days"] = (span or {}).get("activity_days")
        out["first"] = _date((span or {}).get("first_expenditure_date"))
        out["latest"] = _date((span or {}).get("latest_expenditure_date"))
    except Exception:
        pass
    if rec is None or not rec.has_expenditure:
        return out
    try:
        from database import normkey
        rows = db.execute(text(
            """
            SELECT expenditure_date, expenditure_amount, vendor, payment_status
            FROM expenditures
            WHERE normkey(work_description) = :d
              AND normkey(constituency) = :c
              AND normkey(state) = :s
              AND expenditure_date IS NOT NULL
            ORDER BY expenditure_date ASC
            LIMIT 60
            """
        ), {
            "d": normkey(project.project_name or ""),
            "c": normkey(project.constituency or ""),
            "s": normkey(project.state or ""),
        }).fetchall()
        out["transactions"] = [
            {"date": _date(r[0]), "amount": _num(r[1]), "vendor": (r[2] or "").strip() or None,
             "status": r[3]}
            for r in rows
        ]
    except Exception:
        pass
    return out


def _historical_changes(db: Session, project_id: int) -> Dict[str, Any]:
    try:
        return change_tracking.project_history(db, project_id, limit=120)
    except Exception:
        return {"snapshots": [], "events": [], "batches": {}, "has_history": False, "limitations": []}


def _vendor_connections(db: Session, project: models.Project, vendors: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """For each vendor on this work: other project rows sharing the same
    work_key linkage vendor (amounts + first/last shared date). Bounded and
    read-only — joins the vendor's own payment rows to project_rec_info.
    work_key."""
    out: List[Dict[str, Any]] = []
    for v in vendors[:5]:
        name = str(v.get("vendor") or "").strip()
        if not name:
            continue
        try:
            rows = db.execute(text(
                """
                WITH ve AS (
                  SELECT normkey(work_description) AS wk,
                         state AS st, constituency AS cons,
                         SUM(expenditure_amount) AS total,
                         COUNT(*) AS tx,
                         MIN(expenditure_date) AS first_tx,
                         MAX(expenditure_date) AS last_tx
                  FROM expenditures
                  WHERE vendor = :v
                  GROUP BY normkey(work_description), state, constituency
                )
                SELECT r.project_id, p.project_name, p.state, p.constituency,
                       ve.total, ve.tx, ve.first_tx, ve.last_tx
                FROM ve
                JOIN project_rec_info r ON r.work_key = ve.wk
                JOIN projects p ON p.id = r.project_id
                WHERE r.project_id <> :pid
                ORDER BY ve.total DESC
                LIMIT :lim
                """
            ), {"v": name, "pid": project.id, "lim": _MAX_VENDOR_WORKS}).fetchall()
        except Exception:
            rows = []
        if not rows:
            out.append({
                "vendor": name,
                "this_work_paid": v.get("total_amount"),
                "other_works": 0,
            })
            continue
        works = [
            {
                "project_id": int(r[0]),
                "project_name": r[1],
                "state": r[2],
                "constituency": r[3],
                "vendor_paid_total": _num(r[4]),
                "transactions": int(r[5] or 0),
                "first_shared_date": _date(r[6]),
                "last_shared_date": _date(r[7]),
            }
            for r in rows
        ]
        out.append({
            "vendor": name,
            "this_work_paid": v.get("total_amount"),
            "other_works": len(works),
            "other_work_records": works,
        })
    return out


def _peer_outlier_finding(db: Session, project: models.Project) -> Optional[Dict[str, Any]]:
    """Peer deviation indicator — a potential concern, never a conclusion."""
    try:
        peer = audit_intel.peer_benchmark(project, db, scope="state")
    except Exception:
        return None
    if not peer or peer.get("peer_count", 0) < audit_intel.PEER_MIN_SAMPLE:
        return None
    if peer.get("reliability") == "insufficient":
        return None
    # peer_benchmark returns a metrics list, not flat utilization keys.
    metric = next(
        (m for m in (peer.get("metrics") or []) if m.get("metric") == "utilization_pct"),
        None,
    )
    if not metric:
        return None
    med_util = metric.get("peer_median")
    util = metric.get("project_value")
    gap = metric.get("deviation_vs_median")
    if med_util is None or util is None or gap is None:
        return None
    try:
        gap = float(gap)
    except (TypeError, ValueError):
        return None
    if abs(gap) < 50:
        return None
    direction = "above" if gap > 0 else "below"
    return {
        "key": "peer_utilization_deviation",
        "confidence": "low",
        "explanation": (
            f"Linked payments are {abs(gap):.0f} percentage points {direction} the peer-group median "
            f"({peer['peer_count']} comparable works; this project {util:.1f}% vs peer median "
            f"{float(med_util):.1f}%). A statistical peer deviation is a review indicator, not evidence "
            "of irregularity."),
        "why_it_matters": (
            "Works paying far faster — or far slower — than comparable historical works deserve a "
            "voucher-level look before conclusions are drawn."),
        "suggested_verification": "Compare the payment vouchers with (a) sanctioned scope and (b) reported progress on the ground.",
        "limitations": [
            "Peer medians come from the recorded dataset; small or unrepresentative peer groups reduce reliability.",
            "The peer engine's reliability label is: " + str(peer.get("reliability") or "unknown"),
        ],
        "evidence": {
            "source": "audit_intel.peer_benchmark (existing engine)",
            "records": [{
                "type": "peer_benchmark",
                "reference": f"project {project.id} peer group",
                "fields": {"peer_count": peer.get("peer_count"),
                           "peer_median_utilization": med_util,
                           "this_project_utilization": util},
            }],
        },
    }


def _stalled_activity_finding(trail: Dict[str, Any], dossier: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """No linked payments for a long window while the work is not complete."""
    latest = trail.get("latest")
    first = trail.get("first")
    project_info = dossier.get("project") or {}
    comp = project_info.get("completion_percentage")
    span = trail.get("span_days")
    if not latest or not first or not span or int(span) < 365:
        return None
    if comp is not None and float(comp) >= 90:
        return None
    return {
        "key": "stale_expenditure_activity",
        "confidence": "medium",
        "explanation": (
            f"Recorded payments span {_date(first)} to {_date(latest)} ({int(span)} days) while reported "
            f"physical progress is {comp if comp is not None else 'not recorded'}%. Activity has not been "
            "recorded recently, so the work may be stalled or the record may be incomplete."),
        "why_it_matters": (
            "Stalled payment activity with incomplete reported progress is a standard follow-up trigger."),
        "suggested_verification": "Check the implementing agency's latest progress record and confirm whether work is paused or the data is stale.",
        "limitations": [
            "The expenditure tables are historical official imports; absence of recent rows is a dataset boundary, not proof of inactivity.",
        ],
        "evidence": {
            "source": "project_rec_info + expenditures (dates via activity engine)",
            "records": [{
                "type": "expenditure_span",
                "reference": "activity.get_expenditure_span",
                "fields": {"first_expenditure": first, "latest_expenditure": latest, "span_days": int(span)},
            }],
        },
    }


def _vendor_connection_findings(vconnections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    for v in vconnections:
        works = v.get("other_work_records") or []
        if len(works) < 2:
            continue
        total = sum(w["vendor_paid_total"] for w in works)
        works.sort(key=lambda w: -w["vendor_paid_total"])
        probes = works[:3]
        findings.append({
            "key": f"vendor_connections::{v['vendor']}"[:80],
            "confidence": "low",
            "explanation": (
                f"{v['vendor']} is recorded on this work ({_money(v.get('this_work_paid'))}) and on "
                f"{len(works)} other recorded works totalling {_money(total)} — e.g. #{probes[0]['project_id']} "
                f"{probes[0]['project_name']} ({_money(probes[0]['vendor_paid_total'])}), "
                f"#{probes[1]['project_id']} ({_money(probes[1]['vendor_paid_total'])}), "
                f"#{probes[2]['project_id']} ({_money(probes[2]['vendor_paid_total'])}). "
                "Multiple works per vendor are normal; the connection is surfaced so concentration can be checked."),
        "why_it_matters": (
            "Vendor concentration across several works is a classic follow-up line: consistency of cluster, "
            "contract split patterns and changed amounts."),
            "suggested_verification": "Profile the vendor in Vendor Intelligence and compare scope, rates and completion status across the shared works.",
            "limitations": [
                "Vendor names come from the payment ledger as written; name variants may hide or overstate connections.",
                "No relationship data exists beyond shared payments — the agent cannot see contracts, subcontracts or approvals.",
            ],
            "evidence": {
                "source": "expenditures.vendor + projects (normalized work linkage)",
                "records": [
                    {"type": "vendor", "reference": v["vendor"], "fields": {"other_works": len(works), "total_on_others": total}},
                    *[
                        {"type": "related_project", "reference": f"projects.id={w['project_id']}",
                         "fields": {"project_name": w["project_name"], "vendor_paid_total": w["vendor_paid_total"],
                                    "transactions": w["transactions"], "last_shared_date": w["last_shared_date"]}}
                        for w in probes
                    ],
                ],
            },
        })
    return findings


def _historical_change_findings(history: Dict[str, Any], project_id: int) -> List[Dict[str, Any]]:
    """Regression/regression-type findings only when import snapshots exist."""
    out: List[Dict[str, Any]] = []
    snaps = history.get("snapshots") or []
    if len(snaps) < 2:
        return out
    ordered = sorted(snaps, key=lambda s: (s.get("captured_at") or "", s.get("batch_id") or 0))
    prev = ordered[0]
    latest = ordered[-1]
    for field, label in (("completion_percentage", "reported completion"),
                         ("expenditure", "recorded expenditure")):
        prev_v, new_v = prev.get(field), latest.get(field)
        if prev_v is None or new_v is None:
            continue
        try:
            pf, nf = float(prev_v), float(new_v)
        except (TypeError, ValueError):
            continue
        if pf == nf:
            continue
        direction = "decreased" if nf < pf else "increased"
        if field == "expenditure" and nf >= pf:
            continue  # increases are normal; decreases are review-worthy
        out.append({
            "key": f"historical_{field}_change",
            "confidence": "high" if field == "expenditure" else "medium",
            "explanation": (
                f"Between import batches {prev.get('batch_id')} ({(prev.get('captured_at') or '')[:10]}) and "
                f"{latest.get('batch_id')} ({(latest.get('captured_at') or '')[:10]}), {label} moved from "
                f"{prev_v} to {new_v} ({direction})."),
            "why_it_matters": (
                "A recorded value that moves backwards between imports deserves a source-level check — "
                "it may be a data correction, a revised figure, or an upload error."),
            "suggested_verification": "Open Project Change Detection for this project and confirm the change against the source import files.",
            "limitations": [
                "History covers only imports recorded by this system.",
                "An absent batch value means the project was missing from that import, which is a dataset gap.",
            ],
            "evidence": {
                "source": f"project_snapshots / project_change_events (project {project_id})",
                "records": [
                    {"type": "snapshot", "reference": f"batch {prev.get('batch_id')}", "fields": {field: prev_v}},
                    {"type": "snapshot", "reference": f"batch {latest.get('batch_id')}", "fields": {field: new_v}},
                ],
            },
        })
        break
    return out


def _build_questions(findings: List[Dict[str, Any]], dossier: Dict[str, Any],
                     gaps: Dict[str, Any], connections: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Grounded investigation questions — each references the record(s) that
    raised it. No question is generated without a source."""
    questions: List[Dict[str, Any]] = []
    if not dossier.get("timeline", {}).get("recommendation_date"):
        questions.append({
            "question": "Can the review obtain the official recommendation/sanction order for this work?",
            "raises_from": "projects + project_rec_info records carry no recommendation date.",
        })
    if dossier.get("financial", {}).get("expenditure", 0) > (dossier.get("financial", {}).get("sanctioned_amount") or 0):
        questions.append({
            "question": "Is there a revised sanction or administrative approval covering the payments above the sanctioned amount?",
            "raises_from": ("linked ledger total exceeds the sanctioned amount"
                            f" ({_money(dossier.get('financial', {}).get('expenditure'))} vs"
                            f" {_money(dossier.get('financial', {}).get('sanctioned_amount'))})."),
        })
    for f in findings:
        title = f.get("title") or ""
        if f.get("key", "").startswith("vendor_connections"):
            vendor = str(f.get("key", "")).split("::", 1)[-1]
            questions.append({
                "question": f"Should {vendor}'s shared works be compared for scope, rate and completion consistency?",
                "raises_from": "agent vendor-connection finding.",
            })
        elif f.get("key") == "stale_expenditure_activity":
            questions.append({
                "question": "What is the current status of the work according to the implementing agency?",
                "raises_from": "agent stale-activity finding (no recent recorded payments, incomplete progress).",
            })
        elif f.get("key") == "peer_utilization_deviation":
            questions.append({
                "question": "Voucher-level check: do recorded payments match delivered scope at peer-typical rates?",
                "raises_from": "agent peer-deviation finding.",
            })
        elif f.get("key", "").startswith("historical_"):
            questions.append({
                "question": "Which import carried the value change, and was it a correction or a source-data change?",
                "raises_from": "agent historical-change finding.",
            })
    glist = gaps.get("gaps") or []
    for g in glist[:3]:
        if g.get("summary"):
            questions.append({
                "question": f"How can the missing evidence be obtained — {str(g.get('summary'))[:160]}?",
                "raises_from": "existing evidence-gap calculators.",
            })
    if dossier.get("evidence", {}).get("verification_count", 0) == 0:
        questions.append({
            "question": "Should a field verification be requested to confirm the recorded status on the ground?",
            "raises_from": "no ground-verification reports or photo records are linked to this project.",
        })
    seen = set()
    unique: List[Dict[str, Any]] = []
    for q in questions:
        k = q["question"][:80]
        if k in seen:
            continue
        seen.add(k)
        unique.append(q)
        if len(unique) >= _MAX_QUESTIONS:
            break
    return unique


def _build_suggested_checks(findings: List[Dict[str, Any]], dossier: Dict[str, Any],
                            gaps: Dict[str, Any]) -> List[Dict[str, Any]]:
    suggestions: List[Dict[str, Any]] = []
    for f in findings:
        sv = f.get("suggested_verification")
        if sv and sv not in suggestions:
            suggestions.append({
                "check": sv,
                "basis": f.get("title") or f.get("explanation", "")[:120],
            })
    for f in dossier.get("findings") or []:
        action = f.get("suggested_action")
        if action and action not in {s["check"] for s in suggestions}:
            suggestions.append({"check": action, "basis": f.get("title", "")})
    for item in (dossier.get("checklist") or [])[:0]:
        pass
    for table in (gaps.get("gaps") or [])[:2]:
        if table.get("summary"):
            suggestions.append({
                "check": f"Document the source of the missing record ({str(table.get('summary'))[:140]}).",
                "basis": "evidence-gap analysis",
            })
    return suggestions[:8]


# ═════════════════════════════════════════════════════════════════════
# Run orchestration + persistence
# ═════════════════════════════════════════════════════════════════════

def _new_agent_finding(project_id: int, investigation_id: int, run_id: int,
                       d: Dict[str, Any]) -> models.AgentFinding:
    return models.AgentFinding(
        project_id=project_id,
        investigation_id=investigation_id,
        run_id=run_id,
        finding_key=d["key"],
        title=(d.get("title") or d.get("key", "")).strip()[:240] or d["key"],
        explanation=d.get("explanation") or "",
        evidence_json=json.dumps(d.get("evidence") or {}, ensure_ascii=False),
        confidence=d.get("confidence") or "low",
        suggested_verification=d.get("suggested_verification"),
        limitations_json=json.dumps(d.get("limitations") or [], ensure_ascii=False),
        status="New",
        created_at=_now(),
    )


def run_agent(db: Session, project: models.Project, user: models.User) -> Dict[str, Any]:
    investigation_row = (
        db.query(models.AuditInvestigation)
        .filter(models.AuditInvestigation.project_id == project.id)
        .first()
    )
    if investigation_row is None:
        investigation_row_model = audit_intel.start_investigation(db, project, actor=user)["investigation"]
        # refetch the ORM row
        investigation_row = (
            db.query(models.AuditInvestigation)
            .filter(models.AuditInvestigation.project_id == project.id)
            .first()
        )
    run = models.AgentRun(
        project_id=project.id,
        investigation_id=investigation_row.id,
        status="complete",
        run_by_user_id=user.id if user is not None else None,
        run_by_name=user.name if user is not None else None,
        created_at=_now(),
    )
    db.add(run)
    db.flush()

    steps: List[Dict[str, Any]] = []
    fields_followed: List[str] = []

    # 1. context
    ctx = investigation.load_project_context(db, project.id)
    steps.append({
        "key": "context",
        "title": RUN_STEPS[0][1],
        "detail": RUN_STEPS[0][2],
        "records": [
            {"type": "project", "reference": f"projects.id={project.id}"},
            {"type": "risk_score", "reference": f"risk_scores.project_id={project.id}"},
        ],
    })
    fields_followed.append("project context")

    # 2. dossier findings (regenerated evidence engine)
    dossier_payload = audit_intel.get_investigation(db, project, regenerate=True)
    dossier = (dossier_payload or {}).get("dossier") or {}
    dossier_findings = (dossier_payload or {}).get("findings") or []
    steps.append({
        "key": "dossier",
        "title": RUN_STEPS[1][1],
        "detail": RUN_STEPS[1][2],
        "records": [{"type": "dossier_findings", "reference": "audit_intel engine", "count": len(dossier_findings)}],
    })
    fields_followed.append("dossier findings")

    # 3. payment trail
    rec = db.query(models.ProjectRecInfo).filter(models.ProjectRecInfo.project_id == project.id).first()
    trail = _payment_trail(db, project, rec)
    steps.append({
        "key": "trail",
        "title": RUN_STEPS[2][1],
        "detail": RUN_STEPS[2][2],
        "records": [{"type": "transactions", "reference": "expenditures", "count": len(trail["transactions"])}],
    })
    if trail["transactions"]:
        fields_followed.append("payment trail")

    # 4. historical changes
    history = _historical_changes(db, project.id)
    steps.append({
        "key": "history",
        "title": RUN_STEPS[3][1],
        "detail": (RUN_STEPS[3][2] if history.get("has_history")
                   else "No import snapshots or change events are stored for this project — history is reported as a limitation."),
        "records": [{"type": "change_events", "reference": "project_change_events", "count": len(history.get("events") or [])}],
    })
    if history.get("has_history"):
        fields_followed.append("historical imports")

    # 5. peers
    peer_f = _peer_outlier_finding(db, project)
    steps.append({
        "key": "peers",
        "title": RUN_STEPS[4][1],
        "detail": RUN_STEPS[4][2],
        "records": [{"type": "peer_benchmark", "reference": "audit_intel.peer_benchmark"}],
    })
    fields_followed.append("peer works")

    rec_row = db.query(models.ProjectRecInfo).filter(models.ProjectRecInfo.project_id == project.id).first()

    # 6. vendor connections
    vendor_conns = _vendor_connections(db, project, (ctx or {}).get("vendors") or [])
    steps.append({
        "key": "connections",
        "title": RUN_STEPS[5][1],
        "detail": RUN_STEPS[5][2],
        "records": [
            {"type": "vendor_connection", "reference": v["vendor"], "fields": {"other_works": v["other_works"]}}
            for v in vendor_conns
        ],
    })
    if any(v["other_works"] for v in vendor_conns):
        fields_followed.append("vendor connections")

    # 7. evidence gaps
    try:
        gaps = audit_intel.evidence_gaps(project, timeline=dossier.get("timeline"),
                                         activity=dossier.get("financial"))
    except Exception:
        gaps = {"gaps": [], "summary": []}
    steps.append({
        "key": "gaps",
        "title": RUN_STEPS[6][1],
        "detail": RUN_STEPS[6][2],
        "records": [{"type": "evidence_gap", "reference": "audit_intel.evidence_gaps", "count": len(gaps.get("gaps") or [])}],
    })
    fields_followed.append("evidence coverage")

    # Agent findings: new connection-level ones (stale activity, peer deviation,
    # vendor connections, historical regressions).
    candidate_findings: List[Dict[str, Any]] = []
    stale = _stalled_activity_finding(trail, dossier)
    if stale:
        candidate_findings.append(stale)
    if peer_f:
        candidate_findings.append(peer_f)
    candidate_findings.extend(_vendor_connection_findings(vendor_conns))
    candidate_findings.extend(_historical_change_findings(history, project.id))
    for f in candidate_findings:
        f.setdefault("title", f["key"].replace("_", " ").title())
        f["project_id"] = project.id
        f["run_id"] = run.id

    # Dossier findings already carry confirmed inconsistencies; agent findings
    # must not duplicate them.
    dossier_keys = {f["key"] for f in dossier_findings}
    candidate_findings = [f for f in candidate_findings if f["key"] not in dossier_keys]

    saved: List[models.AgentFinding] = []
    existing_rows = (
        db.query(models.AgentFinding)
        .filter(models.AgentFinding.project_id == project.id,
                models.AgentFinding.investigation_id == investigation_row.id)
        .all()
    )
    existing_by_key = {row.finding_key: row for row in existing_rows}
    for f in candidate_findings:
        row = existing_by_key.get(f["key"])
        if row is None:
            row = _new_agent_finding(project.id, investigation_row.id, run.id, f)
            db.add(row)
            saved.append(row)
        else:
            # refresh evidence text, PRESERVE human review state
            row.explanation = f["explanation"]
            row.evidence_json = json.dumps(f.get("evidence") or {}, ensure_ascii=False)
            row.confidence = f.get("confidence") or row.confidence
            row.suggested_verification = f.get("suggested_verification") or row.suggested_verification
            row.limitations_json = json.dumps(f.get("limitations") or [], ensure_ascii=False)
            row.run_id = run.id
            db.add(row)
            saved.append(row)
    db.commit()
    for row in saved:
        db.refresh(row)

    # 8. questions + suggested checks
    questions = _build_questions(dossier_findings, dossier, gaps, {"vendors": vendor_conns})
    suggested_checks = _build_suggested_checks(candidate_findings, dossier, gaps)
    steps.append({
        "key": "questions",
        "title": RUN_STEPS[7][1],
        "detail": RUN_STEPS[7][2],
        "records": [{"type": "questions", "reference": "agent synthesis", "count": len(questions)}],
    })

    run.steps_json = json.dumps(steps, ensure_ascii=False)
    run.questions_json = json.dumps(questions, ensure_ascii=False)
    run.fields_followed = ", ".join(dict.fromkeys(fields_followed))
    db.commit()

    return run_payload(db, project, run=run)


def run_payload(db: Session, project: models.Project, run: Optional[models.AgentRun] = None) -> Dict[str, Any]:
    """Latest-run payload: steps, agent findings (with review state), dossier
    findings, questions, suggested checks and progress."""
    if run is None:
        run = (db.query(models.AgentRun)
               .filter(models.AgentRun.project_id == project.id)
               .order_by(models.AgentRun.id.desc())
               .first())
    investigation_row = (
        db.query(models.AuditInvestigation)
        .filter(models.AuditInvestigation.project_id == project.id)
        .first()
    )
    if run is None or investigation_row is None:
        return {"agent_run": None, "runner_available": True}

    dossier_payload = audit_intel.get_investigation(db, project)
    dossier = (dossier_payload or {}).get("dossier") or {}

    agent_rows = (
        db.query(models.AgentFinding)
        .filter(models.AgentFinding.investigation_id == investigation_row.id)
        .order_by(
            # highest-confidence first
            models.AgentFinding.id.desc(),
        )
        .all()
    )
    order_rank = {"high": 0, "medium": 1, "low": 2}
    agent_rows.sort(key=lambda r: (order_rank.get(r.confidence, 3), -(r.id or 0)))

    try:
        gaps = audit_intel.evidence_gaps(project, timeline=dossier.get("timeline"),
                                         activity=dossier.get("financial"))
    except Exception:
        gaps = {"gaps": [], "summary": []}

    def _finding_json(row: models.AgentFinding) -> Dict[str, Any]:
        return {
            "id": row.id,
            "finding_key": row.finding_key,
            "title": row.title,
            "explanation": row.explanation,
            "confidence": row.confidence,
            "suggested_verification": row.suggested_verification or None,
            "evidence": json.loads(row.evidence_json) if row.evidence_json else {},
            "limitations": json.loads(row.limitations_json) if row.limitations_json else [],
            "status": row.status,
            "reviewer_note": row.reviewer_note,
            "reviewer_name": row.reviewer_name,
            "reviewed_at": row.reviewed_at,
            "run_id": row.run_id,
        }

    final_statuses = {"Verified", "Not substantiated", "Resolved"}
    dossier_findings = (dossier_payload or {}).get("findings") or []
    unresolved = [f for f in dossier_findings if f.get("status") not in final_statuses]

    accepted = sum(1 for r in agent_rows if r.status == "Accepted")
    dismissed = sum(1 for r in agent_rows if r.status == "Dismissed")
    flagged = sum(1 for r in agent_rows if r.status == "Flagged for Verification")
    questions_payload = json.loads(run.questions_json) if run.questions_json else []

    return {
        "agent_run": {
            "id": run.id,
            "status": run.status,
            "created_at": run.created_at,
            "run_by": run.run_by_name,
            "steps": json.loads(run.steps_json) if run.steps_json else [],
            "fields_followed": (run.fields_followed or "").split(", ") if run.fields_followed else [],
        },
        "agent_findings": [_finding_json(r) for r in agent_rows],
        "dossier_findings": [
            {
                "key": f.get("key"),
                "title": f.get("title"),
                "severity": f.get("severity"),
                "classification": f.get("classification"),
                "classification_label": f.get("classification_label"),
                "explanation": f.get("explanation"),
                "suggested_action": f.get("suggested_action"),
                "limitations": f.get("limitations"),
                "status": f.get("status"),
                "review": f.get("review"),
            }
            for f in dossier_findings
        ],
        "questions": questions_payload,
        "suggested_checks": suggested_checks_cache(db, project, questions_payload),
        "progress": {
            "agent_findings": len(agent_rows),
            "agent_accepted": accepted,
            "agent_dismissed": dismissed,
            "agent_flagged": flagged,
            "dossier_findings": len(dossier_findings),
            "dossier_unresolved": len(unresolved),
            "checklist_pct": (dossier_payload or {}).get("progress_pct") or 0,
        },
        "investigation": {
            "id": investigation_row.id,
            "status": investigation_row.status,
            "note": investigation_row.note,
        },
        "limitations": list((dossier_payload or {}).get("limitations") or [])[:8],
        "disclaimers": DISCLAIMER_NOTE,
    }


def suggested_checks_cache(db: Session, project: models.Project,
                          questions: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Suggested checks for the current dossier — deterministic rebuild."""
    dossier_payload = audit_intel.get_investigation(db, project)
    dossier = (dossier_payload or {}).get("dossier") or {}
    try:
        gaps = audit_intel.evidence_gaps(project, timeline=dossier.get("timeline"),
                                         activity=dossier.get("financial"))
    except Exception:
        gaps = {"gaps": []}
    suggestions: List[Dict[str, Any]] = []
    for f in (dossier_payload or {}).get("findings") or []:
        action = f.get("suggested_action")
        if action and action not in {s["check"] for s in suggestions}:
            suggestions.append({"check": action, "basis": f.get("title", "")})
    for g in (gaps.get("gaps") or [])[:2]:
        if g.get("summary"):
            suggestions.append({
                "check": f"Document the source of the missing record ({str(g.get('summary'))[:140]}).",
                "basis": "evidence-gap analysis",
            })
    for q in (questions or [])[:4]:
        suggestions.append({"check": q.get("question", ""), "basis": q.get("raises_from", "agent questions")})
    return suggestions[:8]


DISCLAIMER_NOTE = [
    "The research agent only reads existing records — it never edits project or financial data.",
    "Findings are review indicators, not conclusions; the auditor's review workflow is unchanged.",
]


# ═════════════════════════════════════════════════════════════════════
# Report export
# ═════════════════════════════════════════════════════════════════════

def agent_report_body(db: Session, project: models.Project) -> Optional[Dict[str, str]]:
    payload = run_payload(db, project)
    if payload.get("agent_run") is None:
        return None
    run = payload["agent_run"]
    lines: List[str] = []
    lines.append("MPLADS-AI — AUTONOMOUS RESEARCH AGENT INVESTIGATION REPORT")
    lines.append("=" * 68)
    lines.append(f"Project: #{project.id} {project.project_name or ''}")
    lines.append(f"State: {project.state or 'not recorded'} · Constituency: {project.constituency or 'not recorded'}")
    lines.append(f"Run: #{run['id']} ({run['created_at']}), by {run['run_by'] or 'unknown'}")
    lines.append("")
    lines.append("Research steps")
    lines.append("-" * 40)
    for i, s in enumerate(run["steps"], 1):
        lines.append(f"{i}. {s['title']} — {s['detail']}")
    lines.append("")
    lines.append("Agent findings (review states set by humans)")
    lines.append("-" * 40)
    if not payload["agent_findings"]:
        lines.append("No agent findings. All explored connections are within expected patterns.")
    for f in payload["agent_findings"]:
        lines.append(f"[{f['confidence'].upper()} · {f['status']}] {f['title']}")
        lines.append(f"    {f['explanation']}")
        if f.get("suggested_verification"):
            lines.append(f"    Suggested verification: {f['suggested_verification']}")
        if f.get("reviewer_note"):
            lines.append(f"    Reviewer note: {f['reviewer_note']}")
        for lim in f.get("limitations") or []:
            lines.append(f"    Limitation: {lim}")
        lines.append("")
    lines.append("Dossier findings (existing evidence engine)")
    lines.append("-" * 40)
    for f in payload["dossier_findings"]:
        lines.append(f"· {f['title']} — {f['status']}")
    lines.append("")
    lines.append("Investigation questions")
    lines.append("-" * 40)
    for q in payload["questions"]:
        lines.append(f"· {q['question']}")
        lines.append(f"  Raises from: {q.get('raises_from', '')}")
    lines.append("")
    lines.append("Suggested additional checks")
    lines.append("-" * 40)
    for s in payload["suggested_checks"]:
        lines.append(f"· {s['check']}")
    lines.append("")
    lines.append("Limitations")
    lines.append("-" * 40)
    for lim in payload.get("limitations") or []:
        lines.append(f"· {lim}")
    lines.append("")
    lines.append("Safety notes")
    lines.append("-" * 40)
    for d in payload.get("disclaimers") or []:
        lines.append(f"· {d}")
    lines.append("")
    lines.append("This report was produced by an autonomous research agent reading the")
    lines.append("existing MPLADS records. It contains no data outside the recorded database.")
    return {
        "filename": f"MPLADS-agent-report-{project.id}-{str(run['created_at'])[:10]}.txt",
        "body": "\n".join(lines),
    }


# ═════════════════════════════════════════════════════════════════════
# API endpoints
# ═════════════════════════════════════════════════════════════════════

class FindingReviewIn(BaseModel):
    status: Optional[str] = Field(None, pattern="^(Accepted|Dismissed|Flagged for Verification|New)$")
    note: Optional[str] = Field(None, max_length=2000)


@router.post("/projects/{project_id}/run")
def run_project_agent(
    project_id: int,
    user: models.User = Depends(auth.require_role("analyst")),
    db: Session = Depends(get_db),
):
    """Run the autonomous research agent on one project (analyst+)."""
    project = db.query(models.Project).filter(models.Project.id == project_id).first()
    if project is None:
        raise HTTPException(404, f"Project {project_id} not found")
    try:
        return run_agent(db, project, user)
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(500, f"Agent run failed: {exc.__class__.__name__}")


@router.get("/projects/{project_id}/latest")
def latest_agent_run(
    project_id: int,
    user: models.User = Depends(auth.require_role("analyst")),
    db: Session = Depends(get_db),
):
    project = db.query(models.Project).filter(models.Project.id == project_id).first()
    if project is None:
        raise HTTPException(404, f"Project {project_id} not found")
    return run_payload(db, project)


@router.patch("/findings/{finding_id}")
def review_agent_finding(
    finding_id: int,
    payload: FindingReviewIn,
    user: models.User = Depends(auth.require_role("analyst")),
    db: Session = Depends(get_db),
):
    row = db.query(models.AgentFinding).filter(models.AgentFinding.id == finding_id).first()
    if row is None:
        raise HTTPException(404, "Agent finding not found")
    if payload.status is not None:
        row.status = payload.status
    if payload.note is not None:
        row.reviewer_note = payload.note[:2000]
    if payload.status is not None or payload.note is not None:
        row.reviewer_user_id = user.id
        row.reviewer_name = user.name
        row.reviewed_at = _now()
    db.commit()
    return {"finding": {
        "id": row.id,
        "status": row.status,
        "reviewer_note": row.reviewer_note,
        "reviewed_at": row.reviewed_at,
    }}


@router.get("/projects/{project_id}/report")
def export_agent_report(
    project_id: int,
    user: models.User = Depends(auth.require_role("analyst")),
    db: Session = Depends(get_db),
):
    project = db.query(models.Project).filter(models.Project.id == project_id).first()
    if project is None:
        raise HTTPException(404, f"Project {project_id} not found")
    body = agent_report_body(db, project)
    if body is None:
        raise HTTPException(404, "No agent run exists for this project yet")
    return body
