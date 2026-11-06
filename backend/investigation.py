"""
MPLADS AI — Investigation service layer
=======================================

Modular services behind the Assistant's investigation features. Separation of
concerns:

  • retrieve_* / analyze_* functions  → data retrieval + calculation only
  • compose_* functions               → response text + structured actions

Everything is grounded in the SAME authoritative sources the rest of the app
uses (project catalog, project_rec_info payment-ledger linkage, stored risk
scores, audit_intel peer/evidence engines, activity.py expenditure spans,
ml/predictor lazy ML/SHAP). Nothing is invented: missing fields are reported
as limitations, never estimated.

Memory discipline (single uvicorn worker, 512MB budget):
  • every retrieval is a targeted query — no table scans into Python
  • peer medians come from the existing audit_intel peer_benchmark engine
  • ML/SHAP reuse the lazily-loaded singleton in ml/predictor.py
  • text composition is plain string building (no pandas, no big lists)
"""

from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, text
from sqlalchemy.orm import Session

import models

# ── helpers shared with the assistant engine ─────────────────────────


def _money(v) -> str:
    v = float(v or 0)
    if v >= 1e7:
        return f"₹{v / 1e7:.2f} Cr"
    if v >= 1e5:
        return f"₹{v / 1e5:.2f} L"
    return f"₹{v:,.0f}"


def _fmt_date(iso) -> str:
    if not iso:
        return ""
    try:
        from datetime import datetime as _dt
        d = _dt.strptime(str(iso)[:10], "%Y-%m-%d")
        return d.strftime("%d %b %Y").lstrip("0")
    except Exception:
        return str(iso)


def _linked_expenditure(db: Session, project_id: int) -> float:
    """Authoritative per-project spend (project_rec_info.linked_expenditure).
    The catalog expenditure column is a zeroed legacy stamp."""
    row = (
        db.query(models.ProjectRecInfo)
        .filter(models.ProjectRecInfo.project_id == project_id)
        .first()
    )
    return float(row.linked_expenditure or 0.0) if row is not None else 0.0


def _linkage_status(db: Session, project_id: int) -> Dict[str, Any]:
    """How the ledger was linked to this project — drives data-quality flags."""
    row = (
        db.query(
            models.ProjectRecInfo.has_expenditure,
            models.ProjectRecInfo.work_key_rows,
            models.ProjectRecInfo.work_key_expenditure,
        )
        .filter(models.ProjectRecInfo.project_id == project_id)
        .first()
    )
    if row is None:
        return {"has_expenditure": False, "work_key_rows": 0, "work_key_expenditure": 0.0, "rec_row": False}
    return {
        "has_expenditure": bool(row[0]),
        "work_key_rows": int(row[1] or 1),
        "work_key_expenditure": float(row[2] or 0.0),
        "rec_row": True,
    }


# ═════════════════════════════════════════════════════════════════════
# FEATURE 1 — project context loading (targeted queries only)
# ═════════════════════════════════════════════════════════════════════

def load_project_context(db: Session, project_id: int) -> Optional[Dict[str, Any]]:
    """Everything the assistant needs to know about one project, in 4 small
    queries. Returns None when the project doesn't exist."""
    p = db.query(models.Project).filter(models.Project.id == project_id).first()
    if p is None:
        return None

    risk = db.query(models.RiskScore).filter(models.RiskScore.project_id == project_id).first()
    rec = db.query(models.ProjectRecInfo).filter(models.ProjectRecInfo.project_id == project_id).first()
    exp = float(rec.linked_expenditure or 0.0) if rec is not None else 0.0

    # Expenditure span (first/latest payment dates) via the existing activity
    # engine — the same data Risk Center uses.
    span = None
    try:
        import activity as _act
        span = _act.get_expenditure_span(p)
    except Exception:
        span = None

    # Vendors on this work — bounded to the top 5 by paid amount.
    vendors: List[Dict[str, Any]] = []
    if rec is not None and rec.has_expenditure:
        from database import normkey
        rows = db.execute(
            text("""
                SELECT vendor, COUNT(*) AS tx, SUM(expenditure_amount) AS total
                FROM expenditures
                WHERE normkey(work_description) = :d
                  AND normkey(constituency) = :c
                  AND normkey(state) = :s
                  AND vendor IS NOT NULL AND trim(vendor) <> ''
                GROUP BY vendor
                ORDER BY total DESC
                LIMIT 5
            """),
            {
                "d": normkey(p.project_name or ""),
                "c": normkey(p.constituency or ""),
                "s": normkey(p.state or ""),
            },
        ).fetchall()
        vendors = [
            {"vendor": r[0].strip(), "transactions": int(r[1]), "total_amount": float(r[2] or 0)}
            for r in rows
        ]

    reasons = []
    if risk is not None:
        reasons = [r.strip() for r in (risk.reasons or "").split(",") if r.strip()] if isinstance(risk.reasons, str) else list(risk.reasons or [])

    sanctioned = float(p.sanctioned_amount or 0.0)
    completion = p.completion_percentage

    return {
        "project_id": p.id,
        "work_id": getattr(p, "work_id", None),
        "project_name": p.project_name,
        "house": getattr(p, "house", None),
        "state": p.state,
        "district": p.district,
        "constituency": p.constituency,
        "mp": (rec.recommended_by if rec is not None else None),
        "category": p.project_type,
        "fy": p.fy,
        "sanctioned_amount": sanctioned,
        "expenditure": exp,
        "utilization_pct": (exp / sanctioned * 100) if sanctioned > 0 else None,
        "completion_percentage": completion,
        "status": p.status,
        "recommendation_date": (rec.recommendation_date if rec is not None else None),
        "recommended_by": (rec.recommended_by if rec is not None else None),
        "first_expenditure_date": (span or {}).get("first_expenditure_date"),
        "latest_expenditure_date": (span or {}).get("latest_expenditure_date"),
        "completion_date": None,  # no completion-date column exists in the dataset
        "risk_score": (risk.risk_score if risk is not None else None),
        "risk_level": (risk.risk_level if risk is not None else None),
        "risk_reasons": reasons,
        "ml_anomaly": bool(risk.ml_anomaly) if risk is not None else False,
        "vendors": vendors,
        "expenditure_transactions": int((span or {}).get("transaction_count") or 0),
        "activity_days": (span or {}).get("activity_days"),
        "delay_indicator": (span or {}).get("delay_indicator"),
        "delay_severity": (span or {}).get("delay_severity"),
        "linkage": _linkage_status(db, project_id),
    }


def format_context_intro(ctx: Dict[str, Any]) -> str:
    """One compact paragraph the widget shows when a project context loads."""
    bits = [f"**#{ctx['project_id']} {ctx['project_name']}**"]
    loc = ctx.get("state") or ""
    if ctx.get("house"):
        loc = f"{ctx['house']} · {loc}"
    if loc:
        bits.append(loc)
    if ctx.get("constituency"):
        bits.append(ctx["constituency"])
    head = " — ".join([bits[0], " · ".join(b for b in bits[1:])]) if len(bits) > 1 else bits[0]
    money = f"Sanctioned {_money(ctx.get('sanctioned_amount'))}"
    if ctx.get("utilization_pct") is not None:
        money += f", utilization {ctx['utilization_pct']:.0f}%"
    elif ctx.get("expenditure") == 0:
        money += ", no expenditure recorded"
    risk = ""
    if ctx.get("risk_score") is not None:
        risk = f" · risk {ctx['risk_score']} ({ctx['risk_level']})"
    return f"{head}\n{money}{risk}. Ask me anything — risk, spending, timeline, peers, data quality — or say \u201cgenerate an audit brief\u201d."


# ═════════════════════════════════════════════════════════════════════
# FEATURE 3 — deep "why is this risky?" reasoning
# ═════════════════════════════════════════════════════════════════════

def analyze_risk(db: Session, ctx: Dict[str, Any], want_peers: bool = True) -> Dict[str, Any]:
    """Structured risk analysis: financial / physical / timeline / peer / ML
    sections + review points. ML path reuses predict_risk() (lazy, batch off)
    so no second model or explainer is ever created."""
    p = db.query(models.Project).filter(models.Project.id == ctx["project_id"]).first()
    risk = db.query(models.RiskScore).filter(models.RiskScore.project_id == ctx["project_id"]).first()
    reasons = [r.strip() for r in (risk.reasons or "").split(",") if r.strip()] if (risk is not None and isinstance(risk.reasons, str)) else (risk.reasons or [] if risk is not None else [])

    sanc = float(ctx.get("sanctioned_amount") or 0)
    exp = float(ctx.get("expenditure") or 0)
    comp = ctx.get("completion_percentage")
    util = (exp / sanc * 100) if sanc else 0.0

    # Peer medians (same engine the audit workspace uses; house-aware, state
    # scope, default sanction band). Failure is non-fatal.
    peer = None
    if want_peers:
        try:
            import audit_intel as _ai
            peer = _ai.peer_benchmark(p, db, scope="state")
        except Exception:
            peer = None

    # ML — full explainability path (single lazy model/explainer, no batch).
    ml = None
    try:
        from ml.predictor import predict_risk, attach_authoritative_expenditure
        attach_authoritative_expenditure([p], db)
        ml = predict_risk(p)  # full mode → explanation + contributing factors
    except Exception:
        ml = None

    # Review points — factual areas that may warrant verification.
    review_points: List[str] = []
    if sanc > 0 and exp > sanc:
        review_points.append("Verify the expenditure records against sanction orders and payment vouchers.")
    if comp is not None and exp > 0 and comp < 50:
        review_points.append("Review reported physical progress against the recorded spend.")
    if (span_dates := (ctx.get("first_expenditure_date"), ctx.get("latest_expenditure_date")))[0] and ctx.get("activity_days") and ctx.get("activity_days", 0) >= 180 and (comp or 0) < 25:
        review_points.append("Check the timeline: recorded activity spans a long window with low reported progress.")
    if ctx.get("expenditure_transactions", 0) == 0 and (exp or 0) > 0:
        review_points.append("Reconcile the expenditure total — no dated transactions are linked to this work.")
    if not review_points:
        review_points.append("No specific verification needs arise from the recorded data; routine monitoring suffices.")

    return {
        "risk_score": (risk.risk_score if risk is not None else None),
        "risk_level": (risk.risk_level if risk is not None else None),
        "reasons": reasons,
        "financial": {
            "sanctioned": sanc,
            "expenditure": exp,
            "utilization_pct": util,
            "overspend": max(0.0, exp - sanc) if sanc else 0.0,
        },
        "physical": {
            "completion_pct": comp,
            "status": ctx.get("status"),
        },
        "timeline": {
            "recommendation_date": ctx.get("recommendation_date"),
            "first_expenditure_date": ctx.get("first_expenditure_date"),
            "latest_expenditure_date": ctx.get("latest_expenditure_date"),
            "activity_days": ctx.get("activity_days"),
            "delay_indicator": ctx.get("delay_indicator"),
        },
        "peer": {
            "peer_count": (peer or {}).get("peer_count"),
            "reliability": (peer or {}).get("reliability"),
            "median_utilization": _peer_metric(peer, "utilization_pct", "peer_median"),
            "median_completion": _peer_metric(peer, "completion_pct", "peer_median"),
            "median_risk": _peer_metric(peer, "risk_score", "peer_median"),
        },
        "ml": {
            "anomaly": bool(risk.ml_anomaly) if risk is not None else False,
            "score": getattr(risk, "ml_score", None) if risk is not None else None,
            "top_factors": _ml_top_factors(ml),
        },
        "review_points": review_points,
    }


def _peer_metric(peer: Optional[dict], metric: str, field: str):
    try:
        for m in (peer or {}).get("metrics", []):
            if m.get("metric") == metric:
                return m.get(field)
    except Exception:
        return None
    return None


def _ml_top_factors(ml: Optional[dict]) -> List[Dict[str, Any]]:
    """ML/SHAP contributing factors, ML-sourced only (rules are listed
    separately). Empty when the model isn't loaded or no anomaly exists."""
    out = []
    for f in (ml or {}).get("contributing_factors", []):
        if f.get("source") == "ml" and f.get("explanation"):
            out.append({
                "label": f.get("label") or f.get("feature"),
                "explanation": f.get("explanation"),
                "shap_value": f.get("shap_value"),
            })
    return out[:4]


def compose_risk_explanation(ctx: Dict[str, Any], analysis: Dict[str, Any]) -> str:
    """Concise structured markdown for the "why is this risky?" answer.
    Careful wording throughout: indicators, never accusations."""
    sanc = analysis["financial"]["sanctioned"]
    exp = analysis["financial"]["expenditure"]
    comp = analysis["physical"]["completion_pct"]
    util = analysis["financial"]["utilization_pct"]
    reasons = analysis["reasons"]
    ml = analysis["ml"]

    lines = [f"### Project Risk — #{ctx['project_id']}", ""]
    level = analysis.get("risk_level") or "None"
    score = analysis.get("risk_score")
    lines.append(f"**{level} — {score if score is not None else 'not scored'}** (0–100 scale)")
    lines.append("")

    lines.append("**Key indicators**")
    lines.append(f"- Expenditure: {_money(exp)}")
    lines.append(f"- Sanctioned: {_money(sanc)}")
    if comp is not None:
        lines.append(f"- Progress: {comp:.0f}%")
    if sanc:
        lines.append(f"- Utilization: {util:.0f}%")
    lines.append("")

    if reasons:
        lines.append("**Why flagged**")
        for i, r in enumerate(reasons[:5], 1):
            lines.append(f"{i}. {r}")
        lines.append("")

    peer = analysis.get("peer") or {}
    if peer.get("peer_count"):
        plines = []
        if peer.get("median_utilization") is not None:
            plines.append(f"- Utilization {util:.0f}% vs peer median {peer['median_utilization']:.0f}%")
        if peer.get("median_completion") is not None and comp is not None:
            plines.append(f"- Progress {comp:.0f}% vs peer median {peer['median_completion']:.0f}%")
        if peer.get("median_risk") is not None:
            plines.append(f"- Risk score vs peer median {peer['median_risk']:.0f}")
        if plines:
            lines.append(f"**Peer comparison** ({peer['peer_count']:,} comparable projects, {peer.get('reliability') or 'limited'} reliability)")
            lines += plines
            lines.append("")

    if ml.get("anomaly"):
        lines.append("**ML analysis**")
        lines.append("- The Isolation Forest model also flags this project as a statistical outlier vs. similar projects (+25 risk points).")
        for f in ml.get("top_factors", []):
            lines.append(f"- {f['explanation']}")
        lines.append("")

    dq = compose_data_quality(ctx)
    if dq.get("warnings"):
        lines.append("**Data quality**")
        for w in dq["warnings"]:
            lines.append(f"⚠ {w}")
        lines.append("")

    lines.append("**Suggested review**")
    for rp in analysis["review_points"][:4]:
        lines.append(f"- {rp}")
    lines.append("")
    lines.append("These are review indicators derived from recorded data — not proof of wrongdoing.")

    return "\n".join(lines)


# ═════════════════════════════════════════════════════════════════════
# FEATURE 5 — peer comparison
# ═════════════════════════════════════════════════════════════════════

def analyze_peers(db: Session, project_id: int) -> Optional[Dict[str, Any]]:
    """Peer comparison via the existing audit_intel engine (house-aware,
    state scope, default sanction band — NOT name similarity)."""
    p = db.query(models.Project).filter(models.Project.id == project_id).first()
    if p is None:
        return None
    try:
        import audit_intel as _ai
        bench = _ai.peer_benchmark(p, db, scope="state")
    except Exception:
        return None

    def m(metric):
        for x in bench.get("metrics", []):
            if x.get("metric") == metric:
                return x
        return None

    u, c, e, s = m("utilization_pct"), m("completion_pct"), m("expenditure"), m("sanctioned_amount")
    rec = db.query(models.ProjectRecInfo).filter(models.ProjectRecInfo.project_id == project_id).first()
    exp = float(rec.linked_expenditure or 0.0) if rec is not None else 0.0

    comparable = (bench.get("comparable_projects") or [])[:5]
    return {
        "peer_count": bench.get("peer_count"),
        "reliability": bench.get("reliability"),
        "zero_share": bench.get("zero_expenditure_share"),
        "scope": bench.get("scope"),
        "filters": bench.get("filters"),
        "project": {
            "sanctioned": float(p.sanctioned_amount or 0),
            "expenditure": exp,
            "completion": p.completion_percentage,
            "utilization": u.get("project_value") if u else None,
        },
        "medians": {
            "sanctioned": s.get("peer_median") if s else None,
            "expenditure": e.get("peer_median") if e else None,
            "completion": c.get("peer_median") if c else None,
            "utilization": u.get("peer_median") if u else None,
        },
        "deviations": {
            "utilization_vs_median": u.get("deviation_vs_median") if u else None,
            "completion_vs_median": c.get("deviation_vs_median") if c else None,
        },
        "interpretations": bench.get("interpretations") or [],
        "comparable": comparable,
        "methodology": bench.get("methodology"),
    }


def compose_peer_comparison(ctx: Dict[str, Any], peers: Dict[str, Any]) -> str:
    if not peers or not peers.get("peer_count"):
        return ("I couldn't compute a reliable peer comparison for this project — not enough "
                "comparable projects share its house, state and sanction band in the dataset.")
    pr = peers["project"]
    md = peers["medians"]

    lines = [f"### Peer Comparison — #{ctx['project_id']}", ""]
    lines.append("**This project**")
    lines.append(f"- Sanctioned: {_money(pr['sanctioned'])}")
    lines.append(f"- Expenditure: {_money(pr['expenditure'])}")
    if pr.get("completion") is not None:
        lines.append(f"- Progress: {pr['completion']:.0f}%")
    if pr.get("utilization") is not None:
        lines.append(f"- Utilization: {pr['utilization']:.0f}%")
    lines.append("")

    lines.append(f"**Similar projects** ({peers['peer_count']:,} peers — same house, state and sanction band; not name matching)")
    lines.append(f"- Median sanctioned: {_money(md.get('sanctioned'))}")
    lines.append(f"- Median expenditure: {_money(md.get('expenditure'))}")
    if md.get("completion") is not None:
        lines.append(f"- Median progress: {md['completion']:.0f}%")
    if md.get("utilization") is not None:
        lines.append(f"- Median utilization: {md['utilization']:.0f}%")
    lines.append("")

    # Insight — one factual deviation sentence, neutral wording.
    insight = None
    dev_u = (peers["deviations"] or {}).get("utilization_vs_median")
    if dev_u is not None and abs(dev_u) >= 15 and peers.get("zero_share", 0) is not None and peers.get("zero_share", 1) < 0.85:
        direction = "higher" if dev_u > 0 else "lower"
        insight = (f"Compared with similar projects, this project's utilization is {abs(dev_u):.0f} percentage "
                   f"points {direction} than the peer median.")
    if not insight:
        dev_c = (peers["deviations"] or {}).get("completion_vs_median")
        if dev_c is not None and abs(dev_c) >= 15:
            direction = "higher" if dev_c > 0 else "lower"
            insight = (f"Reported progress is {abs(dev_c):.0f} percentage points {direction} than the peer median.")
    if insight:
        lines.append("**Insight**")
        lines.append(insight)
        lines.append("")

    comp_rows = peers.get("comparable") or []
    if comp_rows:
        lines.append("**Closest comparable projects** (closest sanctioned amounts)")
        for r in comp_rows[:4]:
            s = float(r.get("sanctioned_amount") or 0)
            u2 = r.get("utilization_pct")
            lines.append(
                f"- #{r['id']} {str(r.get('project_name') or '')[:48]} — {_money(s)}"
                + (f", {u2:.0f}% utilization" if u2 is not None else "")
                + (f", risk {r['risk_score']}" if r.get("risk_score") is not None else "")
            )
        lines.append("")

    if peers.get("zero_share") is not None and peers["zero_share"] >= 0.5:
        lines.append(f"⚠ Data limitation: {peers['zero_share'] * 100:.0f}% of comparable projects have no recorded "
                     "expenditure, so the peer median partly reflects un-updated records.")
    lines.append("A deviation from peers is a pattern worth reviewing — it is not proof of irregularity.")
    return "\n".join(lines)


# ═════════════════════════════════════════════════════════════════════
# FEATURE 6 — data quality / evidence analysis
# ═════════════════════════════════════════════════════════════════════

def analyze_data_quality(db: Session, ctx: Dict[str, Any]) -> Dict[str, Any]:
    """✓/⚠ data-quality + evidence analysis from the actual record:
    timeline sequence, linkage issues, missing fields, vendor info."""
    p = db.query(models.Project).filter(models.Project.id == ctx["project_id"]).first()
    checks: List[Dict[str, str]] = []
    warnings: List[str] = []

    rec = db.query(models.ProjectRecInfo).filter(models.ProjectRecInfo.project_id == ctx["project_id"]).first()

    # ── availability checks ──────────────────────────────────────
    if ctx.get("recommendation_date"):
        checks.append({"ok": True, "text": f"Recommendation date available ({_fmt_date(ctx['recommendation_date'])})"})
    else:
        checks.append({"ok": False, "text": "Recommendation date unavailable"})
        warnings.append("Recommendation date is missing — the timeline cannot be anchored.")

    if ctx.get("expenditure_transactions", 0) > 0:
        checks.append({"ok": True, "text": f"Expenditure records available ({ctx['expenditure_transactions']} dated transactions)"})
    elif ctx.get("linkage", {}).get("has_expenditure"):
        checks.append({"ok": False, "text": "Expenditure total exists but dated transactions aren't linked"})
        warnings.append("An expenditure total is recorded but no dated transactions could be conservatively linked to this work.")
    else:
        checks.append({"ok": False, "text": "No expenditure records linked"})
        warnings.append("No ledger payment could be conservatively linked to this work — spend is unknown, not confirmed zero.")

    if ctx.get("status"):
        checks.append({"ok": True, "text": f"Execution status recorded ({ctx['status']})"})
    else:
        checks.append({"ok": False, "text": "Execution status missing"})

    if ctx.get("completion_percentage") is not None and ctx["completion_percentage"] > 0:
        checks.append({"ok": True, "text": f"Physical progress recorded ({ctx['completion_percentage']:.0f}%)"})
    else:
        checks.append({"ok": False, "text": "Physical progress recorded as 0%"})

    if ctx.get("vendors"):
        names = ", ".join(v["vendor"] for v in ctx["vendors"][:3])
        checks.append({"ok": True, "text": f"Vendor information available ({names})"})
    else:
        checks.append({"ok": False, "text": "Vendor information missing"})
        warnings.append("No vendor is recorded against this work's payments.")

    # ── timeline-sequence checks (recorded dates, no invention) ──
    rec_d = ctx.get("recommendation_date")
    first_d = ctx.get("first_expenditure_date")
    if rec_d and first_d:
        try:
            before = str(first_d)[:10] < str(rec_d)[:10]
            if before:
                checks.append({"ok": False,
                               "text": f"An expenditure record ({_fmt_date(first_d)}) appears earlier than the recommendation date ({_fmt_date(rec_d)})"})
                warnings.append(
                    f"An expenditure record appears earlier than the recorded recommendation date. "
                    "This may indicate a data/linkage issue and should be reviewed."
                )
            else:
                checks.append({"ok": True, "text": "Timeline sequence consistent (expenditure after recommendation)"})
        except Exception:
            pass

    # Completion evidence
    status_completed = str(ctx.get("status") or "").lower() == "completed"
    comp = ctx.get("completion_percentage")
    if status_completed and (comp or 0) < 90:
        checks.append({"ok": False, "text": f"Marked Completed while progress is {comp or 0:.0f}%"})
        warnings.append(f"Status is Completed but physical progress is recorded as {comp or 0:.0f}% — completion evidence requires review.")
    if ctx.get("linkage", {}).get("work_key_rows", 1) > 1:
        wk = ctx["linkage"]
        checks.append({"ok": False,
                       "text": f"{wk['work_key_rows']} catalog rows share this work description — per-project spend attribution is ambiguous"})
        warnings.append("Multiple catalog rows share this work's description, so ledger amounts cannot be attributed to this specific project alone.")
    if status_completed:
        checks.append({"ok": True, "text": "Completion record present in the completions ledger (status Completed)"})

    return {
        "checks": checks,
        "warnings": warnings,
        "limitation": ("Assessment covers only fields the dataset contains; several fields (work start date, "
                       "expected completion date, sanction documents) are not published in MPLADS data at all."),
    }


def compose_data_quality(ctx: Dict[str, Any]) -> Dict[str, List[str]]:
    """Light warnings-only render used inside the risk answer.
    Re-derives the ⚠ lines from the loaded context without re-querying."""
    warnings: List[str] = []
    rec_d, first_d = ctx.get("recommendation_date"), ctx.get("first_expenditure_date")
    if rec_d and first_d:
        try:
            if str(first_d)[:10] < str(rec_d)[:10]:
                warnings.append("An expenditure record appears earlier than the recorded recommendation date — "
                                "possible data/linkage issue, review before concluding.")
        except Exception:
            pass
    if ctx.get("expenditure_transactions", 0) == 0 and not ctx.get("linkage", {}).get("has_expenditure"):
        warnings.append("No dated expenditure transactions are linked to this work.")
    if ctx.get("linkage", {}).get("work_key_rows", 1) > 1:
        warnings.append("Multiple catalog rows share this work's description — per-project spend attribution is ambiguous.")
    status_completed = str(ctx.get("status") or "").lower() == "completed"
    if status_completed and (ctx.get("completion_percentage") or 0) < 90:
        warnings.append("Marked Completed while physical progress is below 90% — completion evidence requires review.")
    if not ctx.get("vendors") and ctx.get("expenditure_transactions", 0) > 0:
        warnings.append("Payments are recorded without vendor information.")
    if not ctx.get("recommendation_date"):
        warnings.append("Recommendation date is missing — the timeline cannot be anchored.")
    return {"warnings": warnings}


# ═════════════════════════════════════════════════════════════════════
# FEATURE 7 — audit brief
# ═════════════════════════════════════════════════════════════════════

def compose_audit_brief(db: Session, ctx: Dict[str, Any]) -> str:
    """Structured audit brief composed ONLY from loaded context + the peer
    engine + data-quality analysis. No accusations; review points are
    factual areas to verify."""
    p = db.query(models.Project).filter(models.Project.id == ctx["project_id"]).first()

    peers = analyze_peers(db, ctx["project_id"])
    dq = analyze_data_quality(db, ctx)

    sanc = float(ctx.get("sanctioned_amount") or 0)
    exp = float(ctx.get("expenditure") or 0)
    util = (exp / sanc * 100) if sanc else 0.0
    comp = ctx.get("completion_percentage")

    lines = [f"### AUDIT BRIEF", ""]

    lines.append(f"**Project:** #{ctx['project_id']} {ctx['project_name']}")
    lines.append(f"**House:** {ctx.get('house') or 'Not recorded'}")
    lines.append(f"**State:** {ctx.get('state') or 'Not recorded'}")
    lines.append(f"**Constituency:** {ctx.get('constituency') or 'Not recorded'}")
    lines.append(f"**MP:** {ctx.get('mp') or 'Not recorded'}")
    lines.append("")

    lines.append("**FINANCIAL POSITION**")
    lines.append(f"- Sanctioned amount: {_money(sanc)}")
    lines.append(f"- Expenditure (payment ledger): {_money(exp)}")
    lines.append(f"- Utilization: {util:.1f}%" if sanc else "- Utilization: not computable (no sanctioned amount)")
    lines.append(f"- Remaining/unspent: {_money(max(0.0, sanc - exp))}" if exp <= sanc
                 else f"- Overspend beyond sanction: {_money(exp - sanc)}")
    lines.append("")

    lines.append("**PHYSICAL PROGRESS**")
    lines.append(f"- Completion percentage: {comp if comp is not None else 'not recorded'}")
    lines.append(f"- Status: {ctx.get('status') or 'not recorded'}")
    lines.append("")

    lines.append("**TIMELINE**")
    lines.append(f"- Recommendation date: {_fmt_date(ctx.get('recommendation_date')) or 'unavailable'}")
    lines.append(f"- First valid expenditure date: {_fmt_date(ctx.get('first_expenditure_date')) or 'unavailable'}")
    lines.append(f"- Latest expenditure date: {_fmt_date(ctx.get('latest_expenditure_date')) or 'unavailable'}")
    lines.append("- Completion date: not published in the dataset")
    lines.append("")

    lines.append("**RISK**")
    risk = db.query(models.RiskScore).filter(models.RiskScore.project_id == ctx["project_id"]).first()
    score = risk.risk_score if risk is not None else None
    level = risk.risk_level if risk is not None else None
    lines.append(f"- Risk score: {score if score is not None else 'not scored'} ({level or 'n/a'})")
    reasons = [r.strip() for r in (risk.reasons or "").split(",") if r.strip()] if (risk is not None and isinstance(risk.reasons, str)) else []
    if reasons:
        lines.append(f"- Main indicators: {'; '.join(reasons[:3])}")
    if risk is not None and getattr(risk, "ml_anomaly", False):
        lines.append("- ML anomaly: flagged as a statistical outlier vs. similar projects")
    else:
        lines.append("- ML anomaly: not flagged")
    lines.append("")

    lines.append("**PEER COMPARISON**")
    if peers and peers.get("peer_count"):
        md = peers["medians"]
        lines.append(f"- {peers['peer_count']:,} comparable projects ({peers['reliability']} reliability)")
        if md.get("utilization") is not None:
            lines.append(f"- Median utilization: {md['utilization']:.0f}% (this project: {util:.0f}%)")
        if md.get("completion") is not None:
            lines.append(f"- Median progress: {md['completion']:.0f}%" + (f" (this project: {comp:.0f}%)" if comp is not None else ""))
    else:
        lines.append("- No reliable peer group available for this project")
    lines.append("")

    lines.append("**EVIDENCE**")
    if ctx.get("expenditure_transactions", 0) > 0:
        lines.append(f"- Available: {ctx['expenditure_transactions']} dated payment transactions"
                     + (f" ({_fmt_date(ctx.get('first_expenditure_date'))} → {_fmt_date(ctx.get('latest_expenditure_date'))})" if ctx.get("first_expenditure_date") else ""))
    else:
        lines.append("- Available: project record only — no dated payments linked")
    if ctx.get("vendors"):
        lines.append(f"- Available: vendor detail ({', '.join(v['vendor'] for v in ctx['vendors'][:3])})")
    lines.append("- Missing: work start date, expected completion date, sanction document references (not published in MPLADS data)")
    if not any((c.get("ground") for c in [{}] if False)):
        pass
    # Ground-verification evidence — real submissions, if any exist.
    try:
        n_ver = (
            db.query(func.count(models.VerificationReport.id))
            .filter(models.VerificationReport.project_id == ctx["project_id"])
            .scalar() or 0
        )
        lines.append(f"- Ground verification submissions: {n_ver}")
    except Exception:
        pass
    lines.append("")

    lines.append("**DATA QUALITY**")
    if dq["checks"]:
        for c in dq["checks"][:8]:
            lines.append(f"{'✓' if c['ok'] else '⚠'} {c['text']}")
    else:
        lines.append("- No data-quality issues detected in the recorded fields")
    lines.append("")

    lines.append("**REVIEW POINTS**")
    analysis = analyze_risk(db, ctx, want_peers=False)
    for i, rp in enumerate(analysis["review_points"][:5], 1):
        lines.append(f"{i}. {rp}")
    lines.append("")
    lines.append("This brief lists recorded facts and detected indicators that may warrant further review — "
                 "it is not a finding, accusation or conclusion of wrongdoing.")
    return "\n".join(lines)
