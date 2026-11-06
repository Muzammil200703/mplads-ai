"""
MPLADS AI — project review & recommendation engine
==================================================

Turns the project investigation data already loaded elsewhere into:

  • structured findings (recorded facts vs. calculated indicators vs.
    data-quality observations),
  • recommendation objects with a category, a reason, the supporting record,
    the information that is missing, and the action that would resolve it,
  • an explicit limitations list.

Nothing here accuses anyone. Every finding is tied to a recorded value or a
deterministic calculation, and the output is always labelled as an AI-assisted
preliminary review — never a formal audit conclusion.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

import models


# ── formatting helpers ──────────────────────────────────────────────

def _money(value) -> str:
    value = float(value or 0)
    if value >= 1e7:
        return f"₹{value / 1e7:,.2f} Cr"
    if value >= 1e5:
        return f"₹{value / 1e5:,.2f} L"
    return f"₹{value:,.0f}"


def _date(value) -> str:
    if not value:
        return "not recorded"
    try:
        from datetime import datetime
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").strftime("%d %b %Y").lstrip("0")
    except Exception:
        return str(value)


def _finding(title: str, detail: str, severity: str = "info",
             kind: str = "fact", evidence: str = "") -> Dict[str, str]:
    return {"title": title, "detail": detail, "severity": severity,
            "kind": kind, "evidence": evidence}


def _rec(category: str, title: str, why: str, evidence: str,
         missing: str, action: str) -> Dict[str, str]:
    return {"category": category, "title": title, "why": why,
            "evidence": evidence, "missing": missing, "action": action}


# ── review builder ──────────────────────────────────────────────────

def build_project_review(db: Session, project_id: int,
                         ctx: Optional[Dict[str, Any]] = None,
                         analysis: Optional[Dict[str, Any]] = None,
                         dq: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
    """Build a structured review for one project from real records.

    Reuses the investigation service's context/risk/data-quality functions.
    Those functions only read the catalog, the derived ledger linkage, stored
    risk results and the existing peer engine; no value is estimated here.
    """
    from investigation import (analyze_data_quality, analyze_risk,
                               load_project_context)

    ctx = ctx or load_project_context(db, project_id)
    if ctx is None:
        return None
    analysis = analysis or analyze_risk(db, ctx, want_peers=True)
    dq = dq or analyze_data_quality(db, ctx)

    p = db.query(models.Project).filter(models.Project.id == project_id).first()
    if p is None:
        return None

    sanc = float(ctx.get("sanctioned_amount") or 0)
    exp = float(ctx.get("expenditure") or 0)
    util = (exp / sanc * 100) if sanc else None
    comp = ctx.get("completion_percentage")
    status = ctx.get("status")
    rec_date = ctx.get("recommendation_date")
    first_exp = ctx.get("first_expenditure_date")
    last_exp = ctx.get("latest_expenditure_date")
    vendors = ctx.get("vendors") or []
    linkage = ctx.get("linkage") or {}
    risk_score = analysis.get("risk_score")
    risk_level = analysis.get("risk_level")
    reasons = analysis.get("reasons") or []
    peer = analysis.get("peer") or {}
    ml = analysis.get("ml") or {}

    evidence_count = 0
    inquiry_count = 0
    try:
        evidence_count = int(
            db.query(func.count(models.VerificationReport.id))
            .filter(models.VerificationReport.project_id == project_id).scalar() or 0
        )
        inquiry_count = int(
            db.query(func.count(models.Inquiry.id))
            .filter(models.Inquiry.project_id == project_id).scalar() or 0
        )
    except Exception:
        pass

    findings: List[Dict[str, str]] = []
    recommendations: List[Dict[str, str]] = []

    # Recorded facts.
    findings.append(_finding(
        "Project identification",
        f"#{p.id} {p.project_name or '—'} · {ctx.get('house') or 'House not recorded'} · "
        f"{ctx.get('state') or 'State not recorded'} · {ctx.get('constituency') or 'Constituency not recorded'}",
        "info", "fact",
        "projects table (id, name, house, state, constituency).",
    ))
    findings.append(_finding(
        "Financial position",
        f"Sanctioned {_money(sanc)}; linked expenditure {_money(exp)}"
        + (f"; utilization {util:.0f}%." if util is not None else "; utilization not computable (no sanctioned amount)."),
        "info", "fact",
        "projects.sanctioned_amount and project_rec_info.linked_expenditure.",
    ))
    findings.append(_finding(
        "Physical position",
        f"Completion {f'{float(comp):.0f}%' if comp is not None else 'not recorded'}; status {status or 'not recorded'}.",
        "info", "fact",
        "projects.completion_percentage and projects.status.",
    ))
    if risk_score is not None:
        findings.append(_finding(
            "Stored risk assessment",
            f"Risk score {risk_score}/100 ({risk_level or 'n/a'})"
            + (f"; {ml.get('score'):.3f} ML anomaly score" if ml.get("score") is not None else "") + ".",
            "high" if (risk_score or 0) >= 70 else "medium" if (risk_score or 0) >= 40 else "info",
            "fact",
            "risk_scores table (stored result; not recomputed here).",
        ))

    # Indicator findings.
    for reason in reasons[:6]:
        findings.append(_finding(
            "Risk indicator",
            reason,
            "high" if (risk_score or 0) >= 70 else "medium",
            "indicator",
            "risk_scores.reasons (rule/ML explanation generated from this record).",
        ))
    if sanc and exp > sanc:
        findings.append(_finding(
            "Expenditure exceeds sanction",
            f"Linked expenditure {_money(exp)} exceeds the sanctioned amount {_money(sanc)} by {_money(exp - sanc)}.",
            "high", "indicator",
            "project_rec_info.linked_expenditure vs projects.sanctioned_amount.",
        ))
    if exp > 0 and comp is not None and comp < 25:
        findings.append(_finding(
            "Financial / physical mismatch",
            f"{_money(exp)} is linked to this work while recorded physical completion is only {comp:.0f}%.",
            "high", "indicator",
            "linked ledger payments compared with projects.completion_percentage.",
        ))
    if util is not None and peer.get("median_utilization") is not None:
        gap = util - float(peer["median_utilization"])
        if abs(gap) >= 20:
            findings.append(_finding(
                "Utilization differs from peers",
                f"Utilization {util:.0f}% vs peer median {float(peer['median_utilization']):.0f}% "
                f"({abs(gap):.0f} percentage points {'higher' if gap > 0 else 'lower'}).",
                "medium", "indicator",
                f"Peer group: {int(peer.get('peer_count') or 0):,} projects, {peer.get('reliability') or 'limited'} reliability.",
            ))
    if ml.get("anomaly"):
        findings.append(_finding(
            "ML anomaly signal",
            "The Isolation Forest model also flags this record as a statistical outlier against similar projects.",
            "medium", "indicator",
            "risk_scores.ml_anomaly (model output; not a finding of wrongdoing).",
        ))

    # Data-quality findings.
    if not rec_date:
        findings.append(_finding(
            "Recommendation date unavailable",
            "No recommendation date is linked, so the lifecycle cannot be anchored.",
            "medium", "data_quality",
            "project_rec_info.recommendation_date is missing.",
        ))
    if rec_date and first_exp and str(first_exp)[:10] < str(rec_date)[:10]:
        findings.append(_finding(
            "Expenditure precedes recommendation",
            f"The first linked payment ({_date(first_exp)}) is earlier than the recommendation date ({_date(rec_date)}).",
            "high", "data_quality",
            "project_rec_info.first_expenditure_date < recommendation_date.",
        ))
    if str(status or "").lower() == "completed" and (comp or 0) < 90:
        findings.append(_finding(
            "Status / progress conflict",
            f"Status is Completed while recorded physical progress is {float(comp or 0):.0f}%.",
            "medium", "data_quality",
            "projects.status and projects.completion_percentage disagree.",
        ))
    if exp <= 0 and not linkage.get("has_expenditure"):
        findings.append(_finding(
            "Expenditure unknown",
            "No payment passes the conservative work-key linkage for this project. This is an unknown amount, not a confirmed zero.",
            "medium", "data_quality",
            "project_rec_info.linked_expenditure is 0 with has_expenditure false.",
        ))
    if int(linkage.get("work_key_rows") or 1) > 1:
        findings.append(_finding(
            "Shared work key",
            f"{int(linkage.get('work_key_rows') or 1)} catalogue rows share this work description, so per-project spend attribution is ambiguous.",
            "medium", "data_quality",
            "project_rec_info.work_key_rows > 1.",
        ))
    if exp > 0 and not vendors:
        findings.append(_finding(
            "Vendor identity missing",
            "Payments are linked to this work but no vendor is recorded against them.",
            "low", "data_quality",
            "expenditures.vendor is empty for the linked payments.",
        ))

    # Recommendations — each tied to a finding and to what is missing.
    if sanc and exp > sanc:
        recommendations.append(_rec(
            "Financial review",
            "Reconcile the payment ledger with the sanction order",
            f"Recorded payments exceed the sanctioned amount by {_money(exp - sanc)}.",
            f"Linked expenditure {_money(exp)} vs sanctioned {_money(sanc)}.",
            "Sanction order, revised administrative approval (if any) and payment vouchers.",
            "Obtain the sanction/approval documents and confirm whether a revised sanction exists.",
        ))
    if exp > 0 and comp is not None and comp < 25:
        recommendations.append(_rec(
            "Physical verification",
            "Verify physical progress at the site",
            f"{_money(exp)} is recorded against {comp:.0f}% reported completion.",
            f"Linked payments and completion_percentage={comp:.0f}%.",
            "Site measurement, dated progress photographs and the measurement book.",
            "Schedule a field visit and compare measured progress with the recorded percentage.",
        ))
    if rec_date and first_exp and str(first_exp)[:10] < str(rec_date)[:10]:
        recommendations.append(_rec(
            "Timeline investigation",
            "Resolve the date sequence",
            f"A payment ({_date(first_exp)}) predates the recorded recommendation ({_date(rec_date)}).",
            "project_rec_info first-expenditure and recommendation dates.",
            "Original recommendation date and the payment's work reference.",
            "Check the source records for both dates and whether the payment was linked to the right work.",
        ))
    if not rec_date:
        recommendations.append(_rec(
            "Documentation request",
            "Obtain the recommendation record",
            "Without a recommendation date, the project's timeline cannot be established.",
            "project_rec_info has no recommendation_date for this project.",
            "The MP's recommendation letter/date and the matching recommended-works row.",
            "Request the recommendation document and correct or complete the derived date.",
        ))
    if exp > 0 and not vendors:
        recommendations.append(_rec(
            "Vendor clarification",
            "Identify the payee for the linked payments",
            "Payments exist but no vendor identity is recorded, so the expenditure trail stops at the amount.",
            f"{int(ctx.get('expenditure_transactions') or 0)} linked transactions with empty vendor.",
            "Vendor/contractor name, work order and payment advice.",
            "Request the payment advices or work order from the implementing agency.",
        ))
    if str(status or "").lower() == "completed" and (comp or 0) < 90:
        recommendations.append(_rec(
            "Data correction",
            "Reconcile completion status with physical progress",
            f"Status says Completed but progress is {float(comp or 0):.0f}%.",
            "projects.status and completion_percentage conflict.",
            "Completion certificate or updated progress measurement.",
            "Confirm which record is correct and correct the catalogue field through the data-management process.",
        ))
    if int(linkage.get("work_key_rows") or 1) > 1:
        recommendations.append(_rec(
            "Data correction",
            "Disambiguate the shared work description",
            "Several catalogue rows share this work key, so the ledger total cannot be attributed to one project.",
            f"work_key_rows={int(linkage.get('work_key_rows') or 1)}.",
            "A unique work/project identifier linking the catalogue row to the ledger.",
            "Ask the data owner to add the missing identifier or split the work record.",
        ))
    if evidence_count == 0:
        recommendations.append(_rec(
            "Physical verification",
            "Collect ground evidence",
            "No field or citizen evidence submission is recorded for this project.",
            "verification_reports has no row for this project.",
            "A dated site photograph, GPS location and observed status.",
            "Submit evidence through Ground Verification or assign a field verifier.",
        ))
    if (risk_score or 0) >= 70 or (risk_level or "").lower() == "high":
        recommendations.append(_rec(
            "Further audit review",
            "Open a formal audit review",
            f"The stored risk score is {risk_score}/100 ({risk_level or 'n/a'}) with multiple recorded indicators.",
            "; ".join(reasons[:3]) or "Stored risk result.",
            "Auditor's examination of the supporting records.",
            "Create an investigation/audit case and record a disposition after reviewing the documents.",
        ))
    if not recommendations:
        recommendations.append(_rec(
            "Further audit review",
            "Routine monitoring is sufficient on current data",
            "No record-level indicator currently requires a specific verification action.",
            "No overspend, mismatch, timeline inversion or missing core field was detected.",
            "Nothing specific; periodic monitoring.",
            "Keep the project in routine monitoring and re-check after the next data import.",
        ))

    limitations = [
        "This is an AI-assisted preliminary review built from recorded data — not a formal audit finding or conclusion.",
        "The published dataset does not include a work start date, expected completion date or sanction-document references.",
        "Per-project expenditure is the conservatively linked payment-ledger total; unattributable spend is not allocated to this project.",
        "Missing values are reported as unknown, never as zero.",
    ]
    if peer.get("reliability"):
        limitations.append(
            f"Peer comparison uses {int(peer.get('peer_count') or 0):,} comparable projects "
            f"({peer.get('reliability')} reliability)."
        )
    if dq.get("limitation"):
        limitations.append(dq["limitation"])

    return {
        "project_id": int(p.id),
        "project_name": p.project_name,
        "house": ctx.get("house"),
        "state": ctx.get("state"),
        "constituency": ctx.get("constituency"),
        "mp": ctx.get("recommended_by") or ctx.get("mp"),
        "category": ctx.get("category"),
        "fy": ctx.get("fy"),
        "sanctioned": sanc,
        "expenditure": exp,
        "utilization": util,
        "completion": comp,
        "status": status,
        "recommendation_date": rec_date,
        "first_expenditure_date": first_exp,
        "latest_expenditure_date": last_exp,
        "risk_score": risk_score,
        "risk_level": risk_level,
        "reasons": reasons,
        "vendors": vendors,
        "evidence_count": evidence_count,
        "inquiry_count": inquiry_count,
        "peer": peer,
        "ml": ml,
        "findings": findings,
        "recommendations": recommendations[:7],
        "limitations": limitations,
        "dq_checks": dq.get("checks") or [],
        "dq_warnings": dq.get("warnings") or [],
    }


def compose_review(review: Dict[str, Any]) -> str:
    """Render the structured review as a readable, sectioned answer."""
    r = review
    completion_display = f"{float(r['completion']):.0f}%" if r.get("completion") is not None else "not recorded"
    lines = [
        f"### AI-Assisted Preliminary Review — #{r['project_id']} {r['project_name'] or ''}".strip(),
        "",
        "**PROJECT IDENTIFICATION**",
        f"• House: {r.get('house') or 'not recorded'} · State: {r.get('state') or 'not recorded'} · "
        f"Constituency: {r.get('constituency') or 'not recorded'}",
        f"• Recommending MP: {r.get('mp') or 'not recorded'}",
        f"• Category: {r.get('category') or 'not recorded'}",
        f"• Financial year: {r.get('fy') or 'not recorded'}",
        "",
        "**RECORDED FACTS**",
        f"• Sanctioned amount: {_money(r.get('sanctioned'))}",
        f"• Linked expenditure: {_money(r.get('expenditure'))}"
        + (f" ({r['utilization']:.0f}% utilization)" if r.get("utilization") is not None else ""),
        f"• Physical completion: {completion_display}",
        f"• Status: {r.get('status') or 'not recorded'}",
        f"• Recommendation date: {_date(r.get('recommendation_date'))}",
        f"• First / latest linked payment: {_date(r.get('first_expenditure_date'))} / {_date(r.get('latest_expenditure_date'))}",
        f"• Risk: {r.get('risk_score') if r.get('risk_score') is not None else 'not scored'} "
        f"({r.get('risk_level') or 'n/a'})",
        "",
        "**CALCULATED INDICATORS**",
    ]
    if r.get("utilization") is not None:
        lines.append(f"• Utilization = linked expenditure ÷ sanctioned × 100 = {r['utilization']:.1f}%.")
    if r.get("completion") is not None and r.get("utilization") is not None:
        gap = r["utilization"] - float(r["completion"])
        lines.append(f"• Financial/physical gap = {gap:+.0f} percentage points "
                     "(positive means more money recorded than physical progress).")
    peer = r.get("peer") or {}
    if peer.get("median_utilization") is not None and r.get("utilization") is not None:
        lines.append(f"• Utilization vs peer median: {r['utilization']:.0f}% vs "
                     f"{float(peer['median_utilization']):.0f}% "
                     f"({int(peer.get('peer_count') or 0):,} comparable projects).")
    if peer.get("median_completion") is not None and r.get("completion") is not None:
        lines.append(f"• Completion vs peer median: {float(r['completion']):.0f}% vs "
                     f"{float(peer['median_completion']):.0f}%.")
    if not any(line.startswith("• ") for line in lines[lines.index("**CALCULATED INDICATORS**") + 1:]):
        lines.append("• No comparative indicator could be computed from the available fields.")

    lines += ["", "**FINDINGS**"]
    for finding in r.get("findings") or []:
        severity = (finding.get("severity") or "info").upper()
        lines.append(f"• [{severity}] **{finding['title']}** — {finding['detail']}")
        if finding.get("evidence"):
            lines.append(f"  Source: {finding['evidence']}")
    if not r.get("findings"):
        lines.append("• No record-level finding was produced from the available data.")

    lines += ["", "**SUPPORTING EVIDENCE**"]
    if int(r.get("evidence_count") or 0) > 0:
        lines.append(f"• {int(r['evidence_count'])} ground/citizen verification submission(s) are recorded.")
    else:
        lines.append("• No ground-verification submission is recorded for this project.")
    if r.get("vendors"):
        lines.append("• Vendors on linked payments: " + ", ".join(
            f"{v.get('vendor')} ({_money(v.get('total_amount'))})" for v in r["vendors"][:3]
        ) + ".")
    else:
        lines.append("• No vendor is recorded against the linked payments.")
    if r.get("inquiry_count"):
        lines.append(f"• {int(r['inquiry_count'])} inquiry record(s) exist for this project.")
    lines.append("• Source records: project catalogue, derived ledger linkage, stored risk result"
                 + (", peer benchmark" if peer.get("peer_count") else "") + ".")

    lines += ["", "**MISSING INFORMATION**"]
    missing = []
    if not r.get("recommendation_date"):
        missing.append("recommendation date")
    if r.get("completion") is None:
        missing.append("physical completion percentage")
    if not r.get("vendors") and float(r.get("expenditure") or 0) > 0:
        missing.append("vendor identity for linked payments")
    if int(r.get("evidence_count") or 0) == 0:
        missing.append("ground-verification evidence")
    missing.append("work start date, expected completion date and sanction-document references (not published in the dataset)")
    for item in missing:
        lines.append(f"• {item.capitalize() if item[0].islower() else item}")

    lines += ["", "**RECOMMENDED NEXT STEPS**"]
    for index, rec in enumerate(r.get("recommendations") or [], 1):
        lines.append(f"{index}. **{rec['category']} — {rec['title']}**")
        lines.append(f"   Why: {rec['why']}")
        lines.append(f"   Supporting record: {rec['evidence']}")
        lines.append(f"   Missing: {rec['missing']}")
        lines.append(f"   Action: {rec['action']}")

    lines += ["", "**LIMITATIONS**"]
    for limitation in r.get("limitations") or []:
        lines.append(f"• {limitation}")

    lines += ["", "This brief is an AI-assisted preliminary review of recorded data, not a formal audit "
                  "conclusion. It does not establish wrongdoing by any person, vendor or authority."]
    return "\n".join(line for line in lines if line is not None)


# ── intent ──────────────────────────────────────────────────────────

_REVIEW_RE = re.compile(
    r"\breview\s+(this|the|that)?\s*(project|work|record|it)\b|\bproject\s+review\b"
    r"|\breview\s+(?:an?\s+)?(?:audit|brief)\b|\bprepare\s+(?:an?\s+)?audit\s+brief\b"
    r"|\baudit\s+brief\b|\bgenerate\s+(?:an?\s+)?audit\s+brief\b"
    r"|\bprepare\s+(?:this\s+)?(?:project\s+)?for\s+review\b"
    r"|\bidentify\s+(?:the\s+)?issues?.{0,30}(?:verify|verification|review)\b"
    r"|\breview\s+checklist\b|\bshould\s+an?\s+auditor\s+(inspect|review|check)\b"
    r"|\bsummar(?:y|ise|ize)\s+(?:the\s+)?(?:financial|physical|position|evidence|findings)\b"
    r"|\bwhat.s\s+wrong\s+with\s+(this|the|that)?\s*(project|work|it)\b"
    r"|\bwhat\s+should\s+i\s+(check|investigate|look\s+at)\s+(next|first)?\b",
    re.I,
)


def review_intent(q: str, ql: str, ctx_project_id: Optional[int], user, db: Session,
                  session_ctx: Optional[dict] = None):
    """'Review this project', 'prepare an audit brief', 'what should I check next'."""
    if not _REVIEW_RE.search(ql):
        return None
    from investigation_intents import _pid

    pid = _pid(q, ql, ctx_project_id, session_ctx)
    if not pid:
        # Without a project, "what should I investigate next?" is the existing
        # portfolio queue question — let that handler answer it instead.
        if re.search(r"what\s+should\s+i\s+(check|investigate|look\s+at)", ql):
            return None
        from assistant import ActionOut  # lazy — avoids the import cycle
        return (
            "Which project should I review? Open a project's details or give me its ID "
            "(e.g. “review project 80545”) and I'll build the structured review from its record.",
            [ActionOut(label="Browse projects", action="navigate", target="Projects")],
            None,
            None,
            {"intent": "audit_recommendation", "limitations": ["A project id or open project context is required."]},
        )
    review = build_project_review(db, pid)
    if review is None:
        return (
            f"I couldn't find a project with ID {pid} in the current dataset.",
            [],
            None,
            None,
            {"intent": "audit_recommendation", "limitations": ["Project not found."]},
        )

    from assistant import ActionOut  # lazy — avoids the import cycle

    answer = compose_review(review)
    actions = [
        ActionOut(label="Investigation tab", action="open_project", target=str(pid), params={"tab": "audit"}),
        ActionOut(label="Timeline tab", action="open_project", target=str(pid), params={"tab": "timeline"}),
        ActionOut(label="AI Forensics tab", action="open_project", target=str(pid), params={"tab": "forensics"}),
        ActionOut(label="Open Audit Priority", action="navigate", target="Audit Priority"),
    ]
    meta = {
        "intent": "audit_recommendation",
        "entity": {"type": "project", "id": pid},
        "findings": review["findings"],
        "recommendations": review["recommendations"],
        "limitations": review["limitations"],
        "result_ids": [pid],
    }
    return answer, actions, pid, None, meta


def review_meta(db: Session, project_id: int,
                ctx: Optional[Dict[str, Any]] = None,
                analysis: Optional[Dict[str, Any]] = None,
                dq: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Structured findings/recommendations for the risk intent's payload."""
    review = build_project_review(db, project_id, ctx=ctx, analysis=analysis, dq=dq)
    if review is None:
        return {}
    return {
        "findings": review["findings"],
        "recommendations": review["recommendations"],
        "limitations": review["limitations"],
    }
