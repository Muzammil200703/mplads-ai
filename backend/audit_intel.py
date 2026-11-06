"""
MPLADS Audit Intelligence & Investigation Platform — backend logic
==================================================================

This module turns an existing anomaly/risk result into an actionable,
evidence-driven audit workflow. It deliberately does NOT introduce a second
risk engine: the risk score/level always comes from risk_scores (or
predict_risk as a fallback), and matching to work-level datasets reuses the
already-verified conservative matching in timeline.py / activity.py.

GROUNDING RULES (non-negotiable, enforced throughout):

  * Every number, date and label returned here is either read from
    projects / risk_scores / the matched work-level datasets, or is a
    clearly labelled CALCULATION of those values (never an estimate of a
    value that does not exist).
  * The source datasets contain NO start date, expected-completion date,
    sanction date or project-level delay reason. Those are reported as
    "not available" and are never invented.
  * Statistical comparisons are labelled as statistical comparisons and
    are never stated as proof of wrongdoing.
  * Simulated (what-if) values are labelled SIMULATION, are recomputed with
    the existing rule engine, and are never persisted.

Explicit labelling vocabulary used in responses:
    source        → actual project / dataset values
    model         → ML model output (Isolation Forest)
    rule          → deterministic risk-rule output
    calculated    → arithmetic on actual values
    statistical   → peer-group comparison
    simulation    → what-if result (not project data)
    missing       → not present in the available datasets
    recommendation→ system suggestion, not a finding
"""

from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import case, func, text
from sqlalchemy.orm import Session

import models
from activity import get_expenditure_activity
from ml.predictor import predict_risk
from timeline import get_project_timeline

# ── Documented thresholds ───────────────────────────────────────
# Same threshold as the data-quality (stale progress) check in main.py.
STALE_PROGRESS_AMOUNT_THRESHOLD = 10 * 100000  # ₹10 lakh

# Audit priority tier bands, applied to the composite audit_priority_score.
TIER_BANDS = (
    (80.0, "P1", "Immediate Review"),
    (60.0, "P2", "High Priority"),
    (40.0, "P3", "Review"),
    (0.0, "P4", "Monitor"),
)

# Composite audit-priority score weights (documented; see audit_priority_score_sql).
#   risk              0–55  = risk_score × 0.55
#   exposure          0–20  = min(sanctioned / ₹50 lakh, 1) × 20
#   mismatch          0–15  = min(max(utilization − completion, 0) / 100, 1) × 15
#   evidence gap      0–10  = 10 if expenditure = 0 and completion = 0, 5 if completion = 0
#
# A risk-level FLOOR is then applied so the tier can never contradict the stored
# risk classification (see audit_priority_rank_sql):
#   risk_level High   → at least P2
#   risk_level Medium → at least P3
EXPOSURE_FULL_SANCTION = 50 * 10**5  # ₹50 lakh (full exposure weight)

# Peer-group defaults (mirror the existing /projects/{id}/similar bands).
PEER_BAND_DEFAULT = (0.3, 3.0)
PEER_BAND_NARROW = (0.7, 1.4)
PEER_MIN_SAMPLE = 5          # below this the comparison is reported as unreliable
PEER_MEDIAN_SAMPLE = 1000    # rows used for the median (documented in the response)
PEER_TABLE_LIMIT = 10        # comparable projects returned for display

# Deviation thresholds used only for severity labelling of peer comparisons.
PEER_PCT_HIGH = 30.0
PEER_PCT_MEDIUM = 15.0

INVESTIGATION_STATUSES = ("New", "Evidence Required", "Under Review", "Resolved")
EVIDENCE_STATUSES = ("Pending", "Verified", "Discrepancy Found", "Not Applicable")

# Human-reviewed finding states. These are deliberately separate from the
# investigation-level workflow: a finding can remain unresolved even if the
# underlying risk score later falls.
FINDING_REVIEW_STATUSES = (
    "Open",
    "Under review",
    "Clarification requested",
    "Evidence received",
    "Verified",
    "Not substantiated",
    "Resolved",
)
FINAL_FINDING_STATUSES = {"Verified", "Not substantiated", "Resolved"}
FINDING_CLASSIFICATIONS = {
    "confirmed_data_inconsistency": "Confirmed data inconsistency",
    "potential_concern": "Potential concern",
    "missing_information": "Missing information",
}

CASE_ID_PREFIX = "MPLADS-AUD"

# Stable identifiers for the deterministic risk rules (single source: ml/predictor.py).
RULE_LABELS = {
    "expenditure exceeds sanctioned amount": "Overspending — expenditure above sanction",
    "high expenditure with low physical completion": "High expenditure with low reported physical completion",
    "disbursements made with 0% physical completion": "Disbursements recorded with 0% reported physical completion",
    "high-value project with severe progress delay": "High-value project with very low reported completion",
    "project marked completed but physical progress is below 90%": "Status marked Completed below 90% reported progress",
    "high completion recorded with zero expenditure": "High reported completion with zero expenditure",
    "very low fund utilization and low completion": "Very low fund utilization with low completion",
}

_lock = threading.Lock()


# ═══════════════ formatting helpers ═══════════════

def rupees(value: Any) -> str:
    """Format a numeric amount in Indian units (₹ Cr / ₹ L / ₹)."""
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        return "N/A"
    if v >= 10**7:
        return f"₹{v / 10**7:.2f} Cr"
    if v >= 10**5:
        return f"₹{v / 10**5:.2f} L"
    return f"₹{v:,.0f}"


def pct(value: Any, digits: int = 1) -> Optional[float]:
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _slug(text: str, max_len: int = 60) -> str:
    """Stable key for a checklist label so stored statuses survive regeneration."""
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:max_len]


# ═══════════════ audit priority score ═══════════════

def audit_priority_score_sql():
    """
    Composite audit-priority score as a SQLAlchemy expression (0–100).

    Defined once in SQL so that sorting, aggregation and the returned value
    all use exactly the same formula:

        score = risk (0–55)
              + financial exposure (0–20)
              + financial/physical mismatch (0–15)
              + evidence/data gap (0–10)

    Utilization/mismatch use the authoritative linked payment-ledger spend
    (project_rec_info.linked_expenditure, LEFT JOINed by the callers); the
    catalog's per-project expenditure column is a zeroed legacy stamp.
    Callers MUST join ProjectRecInfo on project_id (outer join keeps rows).
    """
    linked_exp = func.coalesce(models.ProjectRecInfo.linked_expenditure, 0.0)
    utilization = case(
        (models.Project.sanctioned_amount > 0,
         linked_exp * 100.0 / models.Project.sanctioned_amount),
        else_=0.0,
    )
    risk_component = func.min(func.coalesce(models.RiskScore.risk_score, 0), 100.0) * 0.55
    exposure_component = func.min(
        func.coalesce(models.Project.sanctioned_amount, 0) / float(EXPOSURE_FULL_SANCTION), 1.0
    ) * 20.0
    mismatch_component = func.min(
        func.max(utilization - func.coalesce(models.Project.completion_percentage, 0), 0.0) / 100.0,
        1.0,
    ) * 15.0
    gap_component = case(
        (linked_exp == 0, case(
            (func.coalesce(models.Project.completion_percentage, 0) == 0, 10.0), else_=0.0)),
        (func.coalesce(models.Project.completion_percentage, 0) == 0, 5.0),
        else_=0.0,
    )
    return risk_component + exposure_component + mismatch_component + gap_component


def tier_for_score(score: float) -> Dict[str, str]:
    for floor, code, label in TIER_BANDS:
        if score >= floor:
            return {"tier": code, "tier_label": label}
    return {"tier": "P4", "tier_label": "Monitor"}


TIER_META = {
    "P1": {"tier": "P1", "tier_label": "Immediate Review", "rank": 1},
    "P2": {"tier": "P2", "tier_label": "High Priority", "rank": 2},
    "P3": {"tier": "P3", "tier_label": "Review", "rank": 3},
    "P4": {"tier": "P4", "tier_label": "Monitor", "rank": 4},
}


def tier_from_rank(rank: int) -> Dict[str, Any]:
    code = {1: "P1", 2: "P2", 3: "P3"}.get(rank, "P4")
    return dict(TIER_META[code])


def audit_priority_rank_sql():
    """
    Priority rank as a single SQL expression: 1 (P1) … 4 (P4).

    rank = MIN(score-based rank, risk-level floor)
    so a High-risk project can never be displayed below P2, keeping the tier
    consistent with the risk classification shown elsewhere in the app.
    """
    score = audit_priority_score_sql()
    score_rank = case(
        (score >= 80.0, 1),
        (score >= 60.0, 2),
        (score >= 40.0, 3),
        else_=4,
    )
    risk_floor = case(
        (models.RiskScore.risk_level == "High", 2),
        (models.RiskScore.risk_level == "Medium", 3),
        else_=4,
    )
    return func.min(score_rank, risk_floor)


def priority_breakdown(
    project,
    risk_score: float,
    audit_score: Optional[float] = None,
    risk_level: Optional[str] = None,
    linked_expenditure: Optional[float] = None,
) -> Dict[str, Any]:
    """Return the composite score, its components and a plain-language explanation.

    `linked_expenditure` is the authoritative per-project spend from the
    payment ledger (project_rec_info.linked_expenditure). The catalog's
    per-project `expenditure` column is a zeroed legacy stamp and must not
    drive scoring; when the override is absent, spend-derived components
    treat expenditure as 0 (evidence-gap behaviour), never as fabricated.
    """
    sanctioned = _num(project.sanctioned_amount)
    expenditure = _num(linked_expenditure) if linked_expenditure is not None else 0.0
    completion = _num(project.completion_percentage)
    utilization = (expenditure / sanctioned * 100.0) if sanctioned > 0 else 0.0

    risk_component = min(max(_num(risk_score), 0.0), 100.0) * 0.55
    exposure_component = min(max(sanctioned, 0.0) / float(EXPOSURE_FULL_SANCTION), 1.0) * 20.0
    mismatch_component = min(max(utilization - completion, 0.0) / 100.0, 1.0) * 15.0
    if expenditure == 0 and completion == 0:
        gap_component = 10.0
    elif completion == 0:
        gap_component = 5.0
    else:
        gap_component = 0.0

    total = risk_component + exposure_component + mismatch_component + gap_component
    if audit_score is not None:
        total = _num(audit_score, total)

    reasons = []
    risk_level = str(risk_level or getattr(project, "risk_level", "") or "")
    reasons.append(f"Risk score contributes {risk_component:.0f}/55 (risk_score {int(_num(risk_score))}/100).")
    reasons.append(
        f"Financial exposure contributes {exposure_component:.0f}/20 "
        f"(sanctioned {rupees(sanctioned)}; full weight at {rupees(EXPOSURE_FULL_SANCTION)})."
    )
    if mismatch_component > 0:
        reasons.append(
            f"Financial–physical mismatch contributes {mismatch_component:.0f}/15 "
            f"({utilization:.1f}% utilization vs {completion}% reported progress)."
        )
    if gap_component > 0:
        reasons.append(
            f"Evidence/data gap contributes {gap_component:.0f}/10 "
            f"({'expenditure and progress both recorded as zero' if gap_component == 10 else 'no physical progress reported'})."
        )

    score_tier = tier_for_score(total)
    floor_rank = 4
    if risk_level == "High":
        floor_rank = 2
    elif risk_level == "Medium":
        floor_rank = 3
    score_rank = TIER_META[score_tier["tier"]]["rank"]
    final_rank = min(score_rank, floor_rank)
    tier = tier_from_rank(final_rank)
    if floor_rank < score_rank:
        reasons.append(
            f"Risk level is {risk_level or 'not classified'}, so this work is placed at least at "
            f"{tier['tier']} ({tier['tier_label']}) regardless of the composite score."
        )
    return {
        "audit_priority_score": round(total, 1),
        "tier": tier["tier"],
        "tier_label": tier["tier_label"],
        "tier_rank": final_rank,
        "score_based_tier": score_tier["tier"],
        "components": {
            "risk": round(risk_component, 1),
            "financial_exposure": round(exposure_component, 1),
            "financial_physical_mismatch": round(mismatch_component, 1),
            "evidence_gap": round(gap_component, 1),
        },
        "explanation": reasons,
        "formula": "risk(0-55) + exposure(0-20) + mismatch(0-15) + evidence_gap(0-10)",
        "tier_bands": {code: label for _, code, label in TIER_BANDS},
    }


def financial_exposure(
    project, linked_expenditure: Optional[float] = None
) -> Dict[str, Any]:
    """
    Financial exposure indicators.

    `funds_under_review` is the sanctioned amount of a flagged work (a
    definition, not a claim of loss). `overspend` and `unverified_spend` are
    arithmetic on actual recorded values and are labelled as calculated.
    `linked_expenditure` (payment ledger) is the authoritative spend; the
    catalog column is a zeroed legacy stamp.
    """
    sanctioned = _num(project.sanctioned_amount)
    expenditure = _num(linked_expenditure) if linked_expenditure is not None else 0.0
    completion = _num(project.completion_percentage)

    overspend = (expenditure - sanctioned) if (sanctioned > 0 and expenditure > sanctioned) else 0.0
    unverified_spend = expenditure * max(0.0, 1 - completion / 100.0)

    return {
        "sanctioned_amount": sanctioned,
        "recorded_expenditure": expenditure,
        "funds_under_review": sanctioned,
        "funds_under_review_display": rupees(sanctioned),
        "overspend_amount": round(overspend, 2),
        "overspend_display": rupees(overspend) if overspend > 0 else None,
        "unverified_spend": round(unverified_spend, 2),
        "unverified_spend_display": rupees(unverified_spend) if expenditure > 0 else None,
        "unverified_spend_formula": "recorded expenditure × (1 − reported completion ÷ 100) [calculated]",
        "note": (
            "Financial exposure figures are arithmetic on recorded values. They indicate the "
            "amounts whose documentation may warrant verification; they are not evidence of loss."
        ),
    }


# ═══════════════ evidence gaps ═══════════════

def _status_label(status: str) -> str:
    return {
        "available": "Available",
        "reported_zero": "Recorded as zero",
        "not_available": "Not available",
        "partial": "Partial",
    }.get(status, status)


def _evidence_item(key, label, status, value=None, source=None, note=None) -> Dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "status": status,
        "status_label": _status_label(status),
        "value": value,
        "source": source,
        "note": note,
    }


def _eff_exp(project) -> float:
    """Authoritative per-project spend for analysis text.

    Reads the value attached by callers (_authoritative_expenditure — the
    MP+IDA-verified payment-ledger total from project_rec_info); falls back
    to the catalog column, which is a zeroed legacy stamp for almost all
    rows. Never fabricates: an unlinked project simply yields 0 and the
    evidence/status text treats it as "no linked records".
    """
    attached = getattr(project, "_authoritative_expenditure", None)
    if attached is not None:
        return float(attached or 0)
    return _num(project.expenditure)


def _core_evidence_items(project) -> List[Dict[str, Any]]:
    """Evidence items that come from the project record columns only."""
    sanctioned = _num(project.sanctioned_amount)
    expenditure = _eff_exp(project)
    completion = _num(project.completion_percentage)
    status_str = (project.status or "").strip()

    return [
        _evidence_item(
            "project_record", "Project record", "available",
            value=f"#{project.id}", source="projects",
            note="Canonical project row used by every page of this application.",
        ),
        _evidence_item(
            "sanction_amount", "Sanctioned amount",
            "available" if sanctioned > 0 else "reported_zero",
            value=rupees(sanctioned), source="projects.sanctioned_amount",
            note=None if sanctioned > 0 else "Recorded as ₹0 — the sanction value may be missing or not yet updated.",
        ),
        _evidence_item(
            "expenditure", "Recorded expenditure",
            "available" if expenditure > 0 else "reported_zero",
            value=rupees(expenditure), source="payment ledger (MP+IDA-linked)",
            note=None if expenditure > 0 else "No verifiably linked expenditure records — spend cannot be confirmed from the current dataset.",
        ),
        _evidence_item(
            "physical_progress", "Reported physical progress",
            "available" if completion > 0 else "reported_zero",
            value=f"{completion}%", source="projects.completion_percentage",
            note=None if completion > 0 else "Recorded as 0% — the progress record may be missing or not yet updated.",
        ),
        _evidence_item(
            "execution_status", "Execution status",
            "available" if status_str else "not_available",
            value=status_str or None, source="projects.status",
            note=None if status_str else "No execution status recorded for this project.",
        ),
        _evidence_item(
            "financial_year", "Financial year",
            "available" if (project.fy or "").strip() else "not_available",
            value=(project.fy or "").strip() or None, source="projects.fy",
        ),
        _evidence_item(
            "location", "State / constituency mapping",
            "available" if (project.state or "").strip() and (project.constituency or "").strip() else "partial",
            value=f"{project.state or 'N/A'} — {project.constituency or 'N/A'}",
            source="projects.state, projects.constituency",
        ),
    ]


def _dataset_gap_items() -> List[Dict[str, Any]]:
    """Fields the available MPLADS datasets genuinely do not contain."""
    return [
        _evidence_item(
            "work_start_date", "Work start / commencement date", "not_available", source=None,
            note="No commencement-date field exists in any available MPLADS dataset.",
        ),
        _evidence_item(
            "expected_completion_date", "Expected completion date", "not_available", source=None,
            note="No expected-completion-date field exists in any available MPLADS dataset.",
        ),
        _evidence_item(
            "sanction_document", "Sanction / approval document reference", "not_available", source=None,
            note="The dataset records amounts only; no document reference is published with it.",
        ),
        _evidence_item(
            "delay_reason", "Official delay / non-progress reason", "not_available", source=None,
            note="No project-level reason field exists in the available datasets.",
        ),
    ]


def evidence_gap_summary(project, timeline: Optional[dict] = None, activity: Optional[dict] = None) -> Dict[str, Any]:
    """
    Compact evidence-gap summary used by list views.

    When the matched work-level datasets are supplied the result is identical
    to the full evidence check. Without them (list mode) only the project-record
    fields plus the dataset-wide gaps are evaluated, and that scope is stated.
    """
    if timeline is not None or activity is not None:
        full = evidence_gaps(project, timeline=timeline, activity=activity)
        return {
            "confidence": full["confidence"],
            "missing_count": full["missing_count"],
            "missing_labels": [i["label"] for i in full["missing_items"]],
            "scope": "full",
            "summary": full["summary"],
        }

    items = _core_evidence_items(project) + _dataset_gap_items()
    missing = [i for i in items if i["status"] == "not_available"]
    return {
        "confidence": None,
        "missing_count": len(missing),
        "missing_labels": [i["label"] for i in missing],
        "scope": "project_record_fields_only",
        "summary": (
            f"{len(missing)} evidence item(s) are not available for this work "
            "(project-record field check; matched-dataset evidence is evaluated in the project workspace)."
        ),
        "note": (
            "Matched-dataset evidence (recommendation date, expenditure transactions, completion record, "
            "vendor detail) is evaluated in the project audit workspace."
        ),
    }


def evidence_gaps(project, timeline: Optional[dict] = None, activity: Optional[dict] = None) -> Dict[str, Any]:
    """
    "WHAT EVIDENCE IS MISSING?" — evidence availability built from the actual
    dataset fields plus the conservatively matched work-level records.
    """
    if timeline is None:
        timeline = get_project_timeline(project.id)
    if activity is None:
        activity = get_expenditure_activity(project)

    sanctioned = _num(project.sanctioned_amount)
    expenditure = _eff_exp(project)
    completion = _num(project.completion_percentage)
    status_str = (project.status or "").strip()
    matched = (timeline or {}).get("meta", {}).get("matching", {}) or {}
    events = (timeline or {}).get("events", []) or []

    rec_event = next((e for e in events if e.get("event_type") == "recommendation"), None)
    comp_event = next((e for e in events if e.get("event_type") == "completion"), None)

    items: List[Dict[str, Any]] = []

    items.extend(_core_evidence_items(project))
    items.append(_evidence_item(
        "recommendation_date", "Recommendation date",
        "available" if (matched.get("recommended") and rec_event) else "not_available",
        value=rec_event.get("date") if rec_event else None,
        source="recommended_works.recommendation_date",
        note=None if (matched.get("recommended") and rec_event) else
             "No recommended-work record matched this project in the available dataset.",
    ))

    # Expenditure transaction detail
    tx_count = int(activity.get("transaction_count") or 0)
    items.append(_evidence_item(
        "expenditure_transactions", "Dated expenditure transactions",
        "available" if tx_count > 0 else "not_available",
        value=f"{tx_count} record(s) • {activity.get('first_activity_date') or 'N/A'} → {activity.get('latest_activity_date') or 'N/A'}"
        if tx_count > 0 else None,
        source="expenditures.expenditure_date",
        note=None if tx_count > 0 else
             "No expenditure transaction could be conservatively matched to this work; only the project-level expenditure total is available.",
    ))

    payment_statuses = []
    for m in activity.get("monthly_activity", []) or []:
        for st in (m.get("payment_statuses") or []):
            if st and st not in payment_statuses:
                payment_statuses.append(st)
    items.append(_evidence_item(
        "payment_status", "Payment status detail",
        "available" if payment_statuses else "not_available",
        value=", ".join(payment_statuses[:4]) if payment_statuses else None,
        source="expenditures.payment_status",
    ))

    # Actual completion record
    items.append(_evidence_item(
        "completion_date", "Completion record",
        "available" if (matched.get("completed") and comp_event) else "not_available",
        value=comp_event.get("date") if comp_event else None,
        source="completed_works.completed_date",
        note=None if (matched.get("completed") and comp_event) else (
            "Project is marked Completed but no completion record was matched in the completion dataset."
            if status_str.lower() == "completed" else
            "No completion record matched this work (expected for projects still under execution)."
        ),
    ))

    # Fields the datasets genuinely do not contain.
    items.extend(_dataset_gap_items())
    items.append(_evidence_item(
        "vendor_detail", "Executing agency / vendor reference", "not_available",
        source="expenditures.vendor",
        note="A vendor column exists at transaction level but is not tied to this work in the matched records.",
    ))

    # ── Data confidence (transparent, from field completeness ──
    core = [
        ("sanction_amount", sanctioned > 0),
        ("expenditure", expenditure > 0),
        ("physical_progress", completion > 0),
        ("execution_status", bool(status_str)),
        ("location", bool((project.state or "").strip() and (project.constituency or "").strip())),
        ("project_type", bool((project.project_type or "").strip())),
    ]
    core_available = sum(1 for _, present in core if present)
    matched_records = sum(1 for k in ("recommended", "expenditures", "completed") if matched.get(k))

    basis: List[str] = [
        f"{core_available} of {len(core)} core project fields populated "
        f"({', '.join(lbl for lbl, present in core if present) or 'none'}).",
        f"{matched_records} of 3 work-level datasets matched "
        f"(recommended works, expenditure transactions, completion records).",
    ]

    if core_available >= 5 and matched_records >= 1:
        confidence = "High"
    elif core_available >= 4:
        confidence = "Medium"
    else:
        confidence = "Low"

    stale_risk = (sanctioned >= STALE_PROGRESS_AMOUNT_THRESHOLD and expenditure == 0 and completion == 0)
    if stale_risk:
        confidence = "Low"
        basis.append(
            f"Progress and expenditure are both recorded as zero for a work sanctioned at {rupees(sanctioned)}, "
            "so the reported record may be stale."
        )
    if core_available <= 2:
        basis.append("Fewer than half of the core fields are populated, so the assessment rests on limited data.")

    # ── Assessment limitations (explicit) ──
    limitations: List[str] = []
    limitations.append(
        "Formal delay analysis cannot be performed: no start date or expected-completion date exists "
        "in the available MPLADS datasets."
    )
    if expenditure == 0 or completion == 0:
        limitations.append(
            "Reported progress and/or expenditure are recorded as zero. This may indicate that project records "
            "have not been updated; it does not by itself establish that work has not started or that the project is irregular."
        )
    if status_str.lower() == "completed" and not (matched.get("completed") and comp_event):
        limitations.append(
            "The project is recorded as Completed, but no matching completion record was found in the completion dataset, "
            "so the completion date cannot be stated."
        )
    if tx_count == 0:
        limitations.append(
            "No dated expenditure transactions were matched to this work, so no financial activity timeline "
            "can be produced for it."
        )
    if not matched.get("recommended"):
        limitations.append(
            "No recommended-work record matched this project, so the recommendation date is unavailable "
            "and any per-work MP/constituency cross-check is limited."
        )

    missing_material = [i for i in items if i["status"] == "not_available"]
    summary = (
        f"{len(missing_material)} of {len(items)} evidence items are not available in the current datasets."
    )

    return {
        "confidence": confidence,
        "confidence_basis": basis,
        "core_fields_available": core_available,
        "core_fields_total": len(core),
        "core_fields": [{"field": lbl, "available": present} for lbl, present in core],
        "matched_datasets": matched_records,
        "datasets_matched": {k: bool(matched.get(k)) for k in ("recommended", "expenditures", "completed")},
        "items": items,
        "available_count": sum(1 for i in items if i["status"] == "available"),
        "missing_count": len(missing_material),
        "missing_items": [{"key": i["key"], "label": i["label"], "note": i["note"]} for i in missing_material],
        "summary": summary,
        "limitations": limitations,
        "distinction_note": (
            "Evidence availability describes what data exists. It is not a judgement about the project or "
            "the implementing agency."
        ),
    }


# ═══════════════ peer benchmarking ═══════════════

def _median(values: List[float]) -> Optional[float]:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    n = len(vals)
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2.0


def peer_benchmark(
    project,
    db: Session,
    scope: str = "state",
    project_type: Optional[str] = None,
    band: str = "default",
    status_filter: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Compare a project with comparable projects using SQL aggregates.

    `scope`: constituency | state | national
    `project_type`: explicit type, or "all" to ignore the category.
    `band`: narrow (0.7–1.4×) | default (0.3–3.0×) | all

    Expenditure everywhere is the authoritative linked payment-ledger total
    (project_rec_info.linked_expenditure); the catalog column is a zeroed
    legacy stamp.
    """
    sanctioned = _num(project.sanctioned_amount)
    _rec_row = (
        db.query(models.ProjectRecInfo)
        .filter(models.ProjectRecInfo.project_id == project.id)
        .first()
    )
    expenditure = _num(_rec_row.linked_expenditure) if _rec_row is not None and _rec_row.linked_expenditure is not None else 0.0
    completion = _num(project.completion_percentage)

    scope_norm = (scope or "state").strip().lower()
    if scope_norm not in ("constituency", "state", "national"):
        scope_norm = "state"

    if band == "all":
        low, high = 0.0, float("inf")
    elif band == "narrow":
        low, high = sanctioned * PEER_BAND_NARROW[0], sanctioned * PEER_BAND_NARROW[1]
    else:
        low, high = sanctioned * PEER_BAND_DEFAULT[0], sanctioned * PEER_BAND_DEFAULT[1]
    if high <= 0:
        high = float("inf")

    type_filter = (project_type or project.project_type or "").strip()
    if type_filter.lower() in ("all", "any", ""):
        type_filter = None

    base = (
        db.query(models.Project, models.RiskScore)
        .outerjoin(models.RiskScore, models.RiskScore.project_id == models.Project.id)
        .filter(models.Project.id != project.id)
        .filter(models.Project.sanctioned_amount > 0)
    )
    if scope_norm == "constituency" and (project.constituency or "").strip():
        base = base.filter(models.Project.constituency == project.constituency)
    elif scope_norm == "state" and (project.state or "").strip():
        base = base.filter(models.Project.state == project.state)
    # House-aware peer groups: when the anchor project's house is known,
    # compare only against same-house peers (LS and RS works differ in scale
    # and structure — cross-house averages would distort benchmarks). When
    # the anchor's house is unattributed, keep the previous behavior so
    # unknown rows still get a peer group.
    if getattr(project, "house", None) in ("Lok Sabha", "Rajya Sabha"):
        base = base.filter(models.Project.house == project.house)
    if type_filter:
        base = base.filter(models.Project.project_type == type_filter)
    if band != "all":
        base = base.filter(models.Project.sanctioned_amount >= low)
        base = base.filter(models.Project.sanctioned_amount <= high)
    if status_filter and status_filter.strip() and status_filter.lower() != "all":
        base = base.filter(models.Project.status == status_filter.strip())

    # Per-project spend from the payment ledger (project_rec_info linkage);
    # the catalog's expenditure column is a zeroed legacy stamp. Peers with
    # no linked spend count as zero (no expenditure recorded). One joined
    # query of light tuples, ordered by id so the median slice is the same
    # deterministic bounded sample as before.
    _peer_rows = (
        base.with_entities(
            models.Project.id,
            models.Project.completion_percentage,
            models.Project.sanctioned_amount,
            models.RiskScore.risk_score,
            models.ProjectRecInfo.linked_expenditure,
        )
        .outerjoin(models.ProjectRecInfo, models.ProjectRecInfo.project_id == models.Project.id)
        .order_by(models.Project.id.asc())
        .all()
    )
    _n = len(_peer_rows)
    _util_list, _comp_list, _exp_list, _sanc_list, _risk_list = [], [], [], [], []
    for _pid, _c, _s, _r, _le in _peer_rows:
        _exp = _num(_le) if _le is not None else 0.0
        _sanc = _num(_s)
        if _sanc > 0:
            _util_list.append(_exp * 100.0 / _sanc)
        _comp_list.append(_num(_c))
        _exp_list.append(_exp)
        _sanc_list.append(_sanc)
        if _r is not None:
            _risk_list.append(_num(_r))
    agg = type("Agg", (), {
        "n": _n,
        "avg_util": (sum(_util_list) / _n) if _n else 0.0,
        "avg_completion": (sum(_comp_list) / _n) if _n else 0.0,
        "avg_expenditure": (sum(_exp_list) / _n) if _n else 0.0,
        "avg_sanctioned": (sum(_sanc_list) / _n) if _n else 0.0,
        "avg_risk": (sum(_risk_list) / len(_risk_list)) if _risk_list else 0.0,
    })()

    peer_count = int(agg.n or 0)

    # Deterministic bounded sample for the median (documented in the response).
    util_vals = _util_list[:PEER_MEDIAN_SAMPLE]
    comp_vals = _comp_list[:PEER_MEDIAN_SAMPLE]
    exp_vals = _exp_list[:PEER_MEDIAN_SAMPLE]
    sanc_vals = _sanc_list[:PEER_MEDIAN_SAMPLE]
    risk_vals = _risk_list[:PEER_MEDIAN_SAMPLE]

    median_util = _median(util_vals)

    median_util = _median(util_vals)
    median_comp = _median(comp_vals)
    median_exp = _median(exp_vals)
    median_sanc = _median(sanc_vals)
    median_risk = _median(risk_vals)

    project_risk = None
    try:
        _row = db.query(models.RiskScore).filter(models.RiskScore.project_id == project.id).first()
        project_risk = _row.risk_score if _row else None
    except Exception:
        project_risk = None

    utilization = (expenditure / sanctioned * 100.0) if sanctioned > 0 else None

    def _metric(name, label, project_value, peer_avg, peer_median, unit) -> Dict[str, Any]:
        dev_median = None
        dev_avg = None
        if project_value is not None and peer_median is not None:
            dev_median = round(project_value - peer_median, 1)
        if project_value is not None and peer_avg is not None:
            dev_avg = round(project_value - peer_avg, 1)
        return {
            "metric": name,
            "label": label,
            "project_value": round(project_value, 1) if project_value is not None else None,
            "peer_median": round(peer_median, 1) if peer_median is not None else None,
            "peer_average": round(peer_avg, 1) if peer_avg is not None else None,
            "deviation_vs_median": dev_median,
            "deviation_vs_average": dev_avg,
            "unit": unit,
        }

    metrics = [
        _metric("utilization_pct", "Expenditure / Sanction",
                utilization, agg.avg_util, median_util, "percentage points"),
        _metric("completion_pct", "Reported physical progress",
                completion, agg.avg_completion, median_comp, "percentage points"),
        _metric("expenditure", "Recorded expenditure",
                expenditure, agg.avg_expenditure, median_exp, "₹"),
        _metric("sanctioned_amount", "Sanctioned amount",
                sanctioned, agg.avg_sanctioned, median_sanc, "₹"),
        # Risk score is populated for every project, so this comparison stays
        # meaningful even where expenditure/progress records are un-updated.
        _metric("risk_score", "Risk score (0-100)",
                _num(project_risk) if project_risk is not None else None,
                agg.avg_risk, median_risk, "points"),
    ]

    # Share of peer records with no recorded expenditure — when most comparable
    # records report zero expenditure the comparison is not meaningful below.
    zero_share = None
    if exp_vals:
        zero_share = round(sum(1 for v in exp_vals if not v) / len(exp_vals), 3)

    reliability = "good" if peer_count >= 50 else ("limited" if peer_count >= PEER_MIN_SAMPLE else "insufficient")
    if zero_share is not None and zero_share >= 0.85:
        # Almost every comparable record reports no expenditure, so a
        # deviation against them cannot support a comparison.
        reliability = "insufficient"
    elif zero_share is not None and zero_share >= 0.5:
        reliability = "limited"

    interpretations: List[str] = []
    if peer_count < PEER_MIN_SAMPLE:
        interpretations.append(
            f"Only {peer_count} comparable project(s) were found for the selected comparison filters, "
            "which is too few for a reliable statistical comparison."
        )
    elif zero_share is not None and zero_share >= 0.5:
        interpretations.append(
            f"{zero_share * 100:.0f}% of comparable projects have no recorded expenditure"
            + (", so no reliable deviation can be computed from them."
               if zero_share >= 0.85 else
               ", so the peer median is dominated by records that may not have been updated.")
        )
        u = metrics[0]
        if zero_share < 0.85 and u["deviation_vs_median"] is not None and abs(u["deviation_vs_median"]) >= PEER_PCT_MEDIUM:
            interpretations.append(
                f"This project shows {u['project_value']}% expenditure against sanction versus a peer median of "
                f"{u['peer_median']}% (n={peer_count}). The difference is large, but it partly reflects how many "
                "comparable records report zero expenditure."
            )
    else:
        u = metrics[0]
        if u["deviation_vs_median"] is not None:
            direction = "above" if u["deviation_vs_median"] > 0 else "below"
            magnitude = abs(u["deviation_vs_median"])
            if magnitude >= PEER_PCT_MEDIUM:
                interpretations.append(
                    f"Expenditure relative to sanction is {magnitude:.1f} percentage points {direction} the median "
                    f"of comparable projects (median {u['peer_median']}%, n={peer_count}). "
                    "Statistical deviation requiring review."
                )
            else:
                interpretations.append(
                    f"Expenditure relative to sanction is {magnitude:.1f} percentage points {direction} the median "
                    f"of comparable projects (median {u['peer_median']}%, n={peer_count}). This is broadly in line with comparable works."
                )
        c = metrics[1]
        if c["deviation_vs_median"] is not None and abs(c["deviation_vs_median"]) >= PEER_PCT_MEDIUM:
            direction = "above" if c["deviation_vs_median"] > 0 else "below"
            interpretations.append(
                f"Reported physical progress is {abs(c['deviation_vs_median']):.1f} percentage points {direction} "
                f"the median of comparable projects (median {c['peer_median']}%)."
            )

    # Comparable projects for display (closest sanctioned amounts).
    # Linked-spend lookup for the displayed rows (authoritative per-project
    # spend from the ledger; the catalog column is a zeroed legacy stamp).
    _linked_by_id = {row[0]: (row[4] if row[4] is not None else 0.0) for row in _peer_rows}
    comparable = []
    if sanctioned > 0:
        closest = (
            base.with_entities(models.Project, models.RiskScore)
            .order_by(func.abs(models.Project.sanctioned_amount - sanctioned).asc())
            .limit(PEER_TABLE_LIMIT)
            .all()
        )
    else:
        closest = (
            base.with_entities(models.Project, models.RiskScore)
            .order_by(models.Project.id.asc())
            .limit(PEER_TABLE_LIMIT)
            .all()
        )
    for p, r in closest:
        s = _num(p.sanctioned_amount)
        _pexp = float(_linked_by_id.get(p.id, 0.0))
        comparable.append({
            "id": p.id,
            "project_name": p.project_name,
            "state": p.state,
            "constituency": p.constituency,
            "project_type": p.project_type,
            "status": p.status,
            "sanctioned_amount": s,
            "expenditure": _pexp,
            "completion_percentage": _num(p.completion_percentage),
            "utilization_pct": round(_pexp / s * 100, 1) if s > 0 else None,
            "risk_score": r.risk_score if r else None,
            "risk_level": r.risk_level if r else None,
        })

    return {
        "scope": scope_norm,
        "peer_count": peer_count,
        "reliability": reliability,
        "zero_expenditure_share": zero_share,
        "sample_size_for_median": min(peer_count, PEER_MEDIAN_SAMPLE),
        "filters": {
            "scope": scope_norm,
            "project_type": type_filter or "all (same category not applied)",
            "sanction_band": "all" if band == "all" else f"{low:,.0f} – {high:,.0f}",
            "sanction_band_preset": band,
            "status": status_filter or "all",
        },
        "metrics": metrics,
        "interpretations": interpretations,
        "comparable_projects": comparable,
        "methodology": (
            "Peer group = projects excluding this one, filtered by the selected scope, category, sanction band "
            f"and status. Averages are computed in SQL across all {peer_count} peer project(s); the median is computed "
            f"over a deterministic sample of up to {PEER_MEDIAN_SAMPLE} peer rows ({min(peer_count, PEER_MEDIAN_SAMPLE)} rows used). "
            "Comparison is statistical only and is not a finding of wrongdoing."
        ),
        "labels": {
            "statistical": "Statistical comparison against comparable projects.",
            "no_claim": "A deviation from peers indicates a pattern worth reviewing; it is not proof of irregularity.",
        },
        "severity_band": {
            "high": f"deviation ≥ {PEER_PCT_HIGH:.0f} percentage points",
            "medium": f"deviation ≥ {PEER_PCT_MEDIUM:.0f} percentage points",
            "low": "below medium band",
        },
    }


# ═══════════════ anomaly explorer ═══════════════

def anomaly_explorer(project, db: Session, peer: Optional[dict] = None) -> Dict[str, Any]:
    """
    Break the project into anomaly dimensions. Each dimension reports what was
    observed (actual values), why it matters, and what should be verified.
    """
    if peer is None:
        peer = peer_benchmark(project, db)

    sanctioned = _num(project.sanctioned_amount)
    expenditure = _eff_exp(project)
    completion = _num(project.completion_percentage)
    status_str = (project.status or "").strip()
    utilization = (expenditure / sanctioned * 100.0) if sanctioned > 0 else 0.0
    gap = utilization - completion

    evidence = evidence_gaps(project)

    dimensions: List[Dict[str, Any]] = []

    # 1. Financial
    fin_observed = [
        {"label": "Sanctioned amount", "value": rupees(sanctioned)},
        {"label": "Recorded expenditure", "value": rupees(expenditure)},
        {"label": "Expenditure / sanction", "value": f"{utilization:.1f}%"},
    ]
    if sanctioned > 0 and expenditure > sanctioned:
        fin_sev = "high"
        fin_finding = "Recorded expenditure exceeds the sanctioned amount."
    elif sanctioned > 0 and utilization >= 80 and completion < 50:
        fin_sev = "high"
        fin_finding = "Fund utilization is high while reported physical progress is low."
    elif sanctioned > 0 and utilization >= 80:
        fin_sev = "medium"
        fin_finding = "Fund utilization is at or above 80% of the sanctioned amount."
    elif sanctioned > 0 and utilization < 10 and completion < 30:
        fin_sev = "medium"
        fin_finding = "Fund utilization is very low with low reported progress."
    else:
        fin_sev = "low"
        fin_finding = "No financial anomaly was detected from the recorded values."
    dimensions.append({
        "dimension": "financial",
        "title": "Financial anomaly",
        "severity": fin_sev,
        "status": "flagged" if fin_sev != "low" else "no_issue",
        "finding": fin_finding,
        "observed": fin_observed,
        "override": None,
        "why_it_matters": (
            "Expenditure that is high relative to sanction, or high relative to reported progress, "
            "indicates that payments should be reconciled with physical execution records."
        ),
        "what_to_verify": [
            "Verify the sanction/approval amount and any revised sanction for this work.",
            "Verify expenditure/payment records against the recorded total.",
            "Check supporting bills and utilization documentation.",
        ],
        "basis": "rule",
    })

    # 2. Progress
    prog_observed = [
        {"label": "Reported physical progress", "value": f"{completion}%"},
        {"label": "Recorded expenditure", "value": rupees(expenditure)},
        {"label": "Financial–physical gap", "value": f"{gap:.1f} percentage points"},
    ]
    if expenditure > 0 and completion == 0:
        prog_sev = "high"
        prog_finding = "Disbursements are recorded while reported physical progress is 0%."
    elif completion < 25 and sanctioned >= 10**6:
        prog_sev = "high"
        prog_finding = "Reported progress is below 25% on a high-value work."
    elif completion == 0 and sanctioned > 0:
        prog_sev = "medium"
        prog_finding = "No physical progress is reported for this work."
    elif gap >= 20:
        prog_sev = "medium"
        prog_finding = "Reported progress lags expenditure by a wide margin."
    else:
        prog_sev = "low"
        prog_finding = "Reported progress is broadly consistent with recorded expenditure."
    dimensions.append({
        "dimension": "progress",
        "title": "Progress anomaly",
        "severity": prog_sev,
        "status": "flagged" if prog_sev != "low" else "no_issue",
        "finding": prog_finding,
        "observed": prog_observed,
        "why_it_matters": (
            "Physical progress is the execution evidence that should accompany expenditure. "
            "A wide gap means the reported progress record may need confirmation before conclusions are drawn."
        ),
        "what_to_verify": [
            "Request the latest physical progress update from the implementing agency.",
            "Verify site status and whether the work has commenced.",
            "Check whether the progress record has simply not been updated.",
        ],
        "data_caveat": (
            "A 0% or missing progress value may itself be an un-updated record; see the evidence section."
        ),
        "basis": "rule",
    })

    # 3. Administrative / status
    admin_observed = [{"label": "Recorded status", "value": status_str or "Not recorded"}]
    if status_str.lower() == "completed" and completion < 90:
        admin_sev = "high"
        admin_finding = "Status is recorded as Completed while reported progress is below 90%."
    elif status_str.lower() == "ongoing" and completion >= 100:
        admin_sev = "medium"
        admin_finding = "Status is Ongoing although reported progress is 100%."
    elif not status_str:
        admin_sev = "medium"
        admin_finding = "No execution status is recorded for this work."
    else:
        admin_sev = "low"
        admin_finding = "Recorded status is consistent with the reported progress."
    dimensions.append({
        "dimension": "administrative",
        "title": "Administrative / status anomaly",
        "severity": admin_sev,
        "status": "flagged" if admin_sev != "low" else "no_issue",
        "finding": admin_finding,
        "observed": admin_observed,
        "why_it_matters": (
            "Status drives how a work is reported and closed. A mismatch between status and progress "
            "usually indicates a records-update issue that should be confirmed with the agency."
        ),
        "what_to_verify": [
            "Confirm the current execution status with the implementing agency.",
            "If the work is closed, verify the completion certificate / final record.",
            "If the work is ongoing, confirm the expected reporting cycle.",
        ],
        "basis": "rule",
    })

    # 4. Peer deviation
    util_metric = next((m for m in peer["metrics"] if m["metric"] == "utilization_pct"), None)
    comp_metric = next((m for m in peer["metrics"] if m["metric"] == "completion_pct"), None)
    peer_observed = [
        {"label": "Comparable projects", "value": f"{peer['peer_count']} (scope: {peer['scope']})"},
        {"label": "This project — expenditure/sanction", "value": f"{utilization:.1f}%" if util_metric and util_metric["project_value"] is not None else "N/A"},
        {"label": "Peer median — expenditure/sanction", "value": f"{util_metric['peer_median']}%" if util_metric and util_metric["peer_median"] is not None else "N/A"},
        {"label": "This project — reported progress", "value": f"{completion}%"},
        {"label": "Peer median — reported progress", "value": f"{comp_metric['peer_median']}%" if comp_metric and comp_metric["peer_median"] is not None else "N/A"},
    ]
    dev = util_metric["deviation_vs_median"] if util_metric else None
    zero_share = peer.get("zero_expenditure_share")
    if peer["reliability"] == "insufficient":
        peer_sev = "unknown"
        peer_finding = "Too few comparable projects to make a statistical comparison."
    elif zero_share is not None and zero_share >= 0.85:
        peer_sev = "unknown"
        peer_finding = (
            f"{zero_share * 100:.0f}% of comparable records report no expenditure, so no reliable peer comparison "
            "can be made for this work. This is a data-coverage limitation, not a project finding."
        )
    elif zero_share is not None and zero_share >= 0.5 and dev is not None and abs(dev) >= PEER_PCT_MEDIUM:
        peer_sev = "medium"
        peer_finding = (
            f"This project's expenditure/sanction ({utilization:.1f}%) is far above the peer median "
            f"({util_metric['peer_median']}%), but {zero_share * 100:.0f}% of comparable records report zero expenditure, "
            "so the comparison is indicative only."
        )
    elif dev is None:
        peer_sev = "unknown"
        peer_finding = "Peer deviation could not be computed from the available values."
    elif abs(dev) >= PEER_PCT_HIGH:
        peer_sev = "high"
        peer_finding = f"Expenditure/sanction is {abs(dev):.1f} percentage points {'above' if dev > 0 else 'below'} the peer median."
    elif abs(dev) >= PEER_PCT_MEDIUM:
        peer_sev = "medium"
        peer_finding = f"Expenditure/sanction is {abs(dev):.1f} percentage points {'above' if dev > 0 else 'below'} the peer median."
    else:
        peer_sev = "low"
        peer_finding = "Expenditure/sanction is in line with comparable projects."
    dimensions.append({
        "dimension": "peer",
        "title": "Peer deviation",
        "severity": peer_sev,
        "status": "flagged" if peer_sev in ("high", "medium") else ("unknown" if peer_sev == "unknown" else "no_issue"),
        "finding": peer_finding,
        "observed": peer_observed,
        "why_it_matters": (
            "Comparable works set the expected range for spending relative to sanction. A large deviation is a "
            "statistical signal for review and is not by itself a finding against the project."
        ),
        "what_to_verify": [
            "Compare this work with the listed comparable projects in the same category.",
            "Confirm whether scope or cost was revised for this work.",
        ],
        "basis": "statistical",
    })

    # 5. Data completeness / evidence gap
    data_observed = [
        {"label": "Data confidence", "value": evidence["confidence"]},
        {"label": "Core fields populated", "value": f"{evidence['core_fields_available']} of {evidence['core_fields_total']}"},
        {"label": "Work-level datasets matched", "value": f"{evidence['matched_datasets']} of 3"},
        {"label": "Evidence items unavailable", "value": f"{evidence['missing_count']} of {len(evidence['items'])}"},
    ]
    conf = evidence["confidence"]
    data_sev = "high" if conf == "Low" else ("medium" if conf == "Medium" else "low")
    dimensions.append({
        "dimension": "data_completeness",
        "title": "Data completeness",
        "severity": data_sev,
        "status": "flagged" if data_sev != "low" else "no_issue",
        "finding": f"Data confidence is {conf}. {evidence['summary']}",
        "observed": data_observed,
        "why_it_matters": (
            "An assessment can only be as reliable as the records behind it. Knowing which evidence is missing "
            "prevents treating a data gap as a project finding."
        ),
        "what_to_verify": [
            "Obtain the missing records listed in the evidence section before concluding on the project.",
        ],
        "missing_items": evidence["missing_items"],
        "basis": "calculated",
    })

    flagged = [d for d in dimensions if d["status"] == "flagged"]
    order = {"high": 0, "medium": 1, "low": 2, "unknown": 3}
    flagged.sort(key=lambda d: order.get(d["severity"], 9))

    return {
        "dimensions": dimensions,
        "flagged_dimensions": len(flagged),
        "highest_severity": flagged[0]["severity"] if flagged else "low",
        "summary": (
            f"{len(flagged)} of {len(dimensions)} anomaly dimensions flagged from the recorded values."
            if flagged else
            "No anomaly dimensions were flagged from the recorded values."
        ),
        "labels": {
            "observed": "Actual values from the project record.",
            "why": "Rule-based interpretation; not an accusation.",
            "verify": "Recommended verification step — a system recommendation, not a finding.",
        },
    }


# ═══════════════ investigation checklist ═══════════════

def build_checklist(
    project, reasons: Optional[List[str]] = None, stale_flag: str = "NORMAL",
    linked_expenditure: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """
    Build the evidence checklist from the anomalies actually present.
    Every item is a recommended verification action, never a claim.
    `linked_expenditure` is the authoritative per-project spend from the
    payment ledger (the catalog column is a zeroed legacy stamp).
    """
    sanctioned = _num(project.sanctioned_amount)
    expenditure = _num(linked_expenditure) if linked_expenditure is not None else 0.0
    completion = _num(project.completion_percentage)
    status_str = (project.status or "").strip().lower()
    utilization = (expenditure / sanctioned * 100.0) if sanctioned > 0 else 0.0
    ml_anomaly = any("ml" in (r or "").lower() for r in (reasons or []))
    reason_text = " ".join((reasons or [])).lower()

    items: List[Dict[str, Any]] = []

    def add(label: str, category: str, severity: str, rationale: str):
        key = _slug(label)
        if any(i["item_key"] == key for i in items):
            return
        items.append({
            "item_key": key,
            "label": label,
            "category": category,
            "severity": severity,
            "rationale": rationale,
        })

    if sanctioned > 0 and expenditure > sanctioned:
        var = expenditure - sanctioned
        add("Verify sanction/approval documentation", "financial", "high",
            f"Recorded expenditure {rupees(expenditure)} exceeds the sanctioned amount {rupees(sanctioned)} "
            f"by {rupees(var)} ({utilization:.1f}% utilization). Confirm whether a revised sanction was issued.")
        add("Verify expenditure / payment records", "financial", "high",
            f"Reconcile the recorded expenditure of {rupees(expenditure)} with payment records.")
        add("Check supporting bills and vouchers", "financial", "high",
            "Confirm that supporting bills exist for the recorded payments.")
        add("Verify utilization documentation", "financial", "medium",
            "Confirm that utilization certificates match the recorded expenditure.")

    if expenditure > 0 and completion == 0:
        add("Request latest physical progress update", "progress", "high",
            f"{rupees(expenditure)} is recorded as spent while reported physical progress is 0%. "
            "Obtain the current progress report.")
        add("Verify site status", "progress", "high",
            "Confirm on site whether the work has been executed and how much of it is complete.")
        add("Check whether the work has actually commenced", "progress", "high",
            "Confirm the commencement position since no progress is reported.")
        add("Verify latest supporting documentation", "progress", "medium",
            "Request photographs, measurement books or inspection notes for the reported stage of work.")

    elif utilization >= 80 and completion < 50:
        add("Reconcile expenditure with reported progress", "progress", "high",
            f"Utilization is {utilization:.1f}% while reported progress is {completion}%. "
            "Confirm the progress recorded against the payments made.")
        add("Request measurement book / inspection note", "progress", "medium",
            "Confirm that physical measurements support the recorded progress.")

    if sanctioned >= 10**6 and completion < 25 and completion > 0:
        add("Review execution schedule for this high-value work", "progress", "medium",
            f"Sanctioned at {rupees(sanctioned)} with only {completion}% reported progress.")

    if status_str == "completed" and completion < 90:
        add("Verify completion certificate", "administrative", "high",
            f"Status is Completed while reported progress is {completion}%. Confirm the completion record.")
        add("Confirm final status with implementing agency", "administrative", "medium",
            "Confirm whether the work is closed or still under execution.")

    if sanctioned > 0 and utilization < 10 and expenditure == 0:
        add("Verify fund release and utilization position", "financial", "medium",
            f"Only {utilization:.1f}% of the sanctioned {rupees(sanctioned)} is recorded as utilized.")
        add("Review whether the work has been taken up", "progress", "medium",
            "Confirm whether the work has been started and whether funds have been released.")

    if ml_anomaly:
        add("Cross-check with comparable projects", "analytical", "medium",
            "The ML model flagged this project as a statistical outlier. Compare it with comparable projects "
            "to establish whether the pattern is explained by scope or cost revisions.")
        add("Review recorded attributes for data entry errors", "analytical", "medium",
            "Confirm that the sanctioned amount, expenditure and progress values are recorded correctly.")

    if stale_flag == "POSSIBLY_STALE":
        add("Request updated progress and expenditure records", "data_quality", "medium",
            f"{rupees(sanctioned)} is sanctioned with 0% progress and ₹0 expenditure recorded. "
            "This may indicate that the records have not been updated; request the current position.")
    if stale_flag == "INSUFFICIENT_DATA":
        add("Obtain complete project records", "data_quality", "medium",
            "Key financial or progress values are missing, so the position cannot be assessed from the current record.")

    if not items:
        add("Verify recorded financial values", "financial", "low",
            f"Confirm that the sanctioned amount ({rupees(sanctioned)}) and recorded expenditure "
            f"({rupees(expenditure)}) are current.")
        add("Verify reported progress", "progress", "low",
            f"Confirm that the reported physical progress of {completion}% is current.")

    # Rank by severity for display
    order = {"high": 0, "medium": 1, "low": 2}
    items.sort(key=lambda i: order.get(i["severity"], 3))
    if reason_text:
        pass  # reasons already inform the items above
    return items


def recommended_actions(checklist: List[Dict[str, Any]], limit: int = 5) -> List[str]:
    """Concise audit actions derived from the checklist (highest severity first)."""
    actions = []
    for item in checklist:
        actions.append(item["label"])
        if len(actions) >= limit:
            break
    if not actions:
        actions.append("No specific verification action was generated from the recorded values.")
    return actions


# ═══════════════ what-if simulation ═══════════════

class _SimStub:
    """Minimal attribute holder so the existing rule engine can be reused as-is."""

    def __init__(self, sanctioned, expenditure, completion_percentage, status, state, project_type):
        self.sanctioned_amount = sanctioned
        self.expenditure = expenditure
        self.completion_percentage = completion_percentage
        self.status = status
        self.state = state
        self.project_type = project_type
        self.id = -1


def _rule_key(reason: str) -> str:
    rl = (reason or "").lower()
    for needle, label in RULE_LABELS.items():
        if needle in rl:
            return label
    if "ml" in rl:
        return "ML anomaly flag (model re-evaluation)"
    return reason


def _model_available() -> bool:
    try:
        from ml import predictor
        predictor._ensure_model()  # model is lazy-loaded; trigger the load
        return getattr(predictor, "model", None) is not None
    except Exception:
        return False


def simulate(
    project,
    sanctioned_amount: Optional[float] = None,
    expenditure: Optional[float] = None,
    completion_percentage: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Re-run the EXISTING deterministic rule engine (plus the loaded ML model)
    on hypothetical values. Nothing is persisted and the result is clearly
    labelled as a simulation.
    """
    cur_sanctioned = _num(project.sanctioned_amount)
    cur_expenditure = _eff_exp(project)
    cur_completion = _num(project.completion_percentage)

    sim_sanctioned = cur_sanctioned if sanctioned_amount is None else max(0.0, float(sanctioned_amount))
    sim_expenditure = cur_expenditure if expenditure is None else max(0.0, float(expenditure))
    sim_completion = cur_completion if completion_percentage is None else min(100.0, max(0.0, float(completion_percentage)))

    changed_inputs: List[str] = []
    if sanctioned_amount is not None and abs(sim_sanctioned - cur_sanctioned) > 1e-6:
        changed_inputs.append(f"sanctioned amount → {rupees(sim_sanctioned)}")
    if expenditure is not None and abs(sim_expenditure - cur_expenditure) > 1e-6:
        changed_inputs.append(f"expenditure → {rupees(sim_expenditure)}")
    if completion_percentage is not None and abs(sim_completion - cur_completion) > 1e-6:
        changed_inputs.append(f"reported progress → {sim_completion}%")

    current = predict_risk(project, batch_mode=True)
    stub = _SimStub(
        sim_sanctioned, sim_expenditure, sim_completion,
        project.status, project.state, project.project_type,
    )
    simulated = predict_risk(stub, batch_mode=True)

    cur_labels = {_rule_key(r) for r in current.get("reasons", [])}
    sim_labels = {_rule_key(r) for r in simulated.get("reasons", [])}

    resolved = sorted(cur_labels - sim_labels)
    triggered = sorted(sim_labels - cur_labels)

    notes: List[str] = []
    if resolved:
        notes.append("No longer triggered: " + "; ".join(resolved) + ".")
    if triggered:
        notes.append("Newly triggered by the simulated values: " + "; ".join(triggered) + ".")
    if not resolved and not triggered:
        notes.append("The simulated values do not change which risk conditions are triggered.")

    delta = int(simulated.get("risk_score", 0)) - int(current.get("risk_score", 0))
    if delta == 0:
        notes.append("Risk score is unchanged by the simulated values.")
    else:
        notes.append(f"Risk score moves by {delta:+d} point(s).")

    return {
        "simulation": True,
        "notice": "SIMULATION — NOT ACTUAL PROJECT DATA. Nothing here is written to the project record.",
        "unchanged_underlying_data": True,
        "changed_inputs": changed_inputs,
        "method": (
            "Hypothetical values are passed through the same deterministic risk rules "
            "(and the same loaded anomaly model) that produced the stored risk score."
        ),
        "model_available": _model_available(),
        "current": {
            "sanctioned_amount": cur_sanctioned,
            "expenditure": cur_expenditure,
            "completion_percentage": cur_completion,
            "utilization_pct": round(cur_expenditure / cur_sanctioned * 100, 1) if cur_sanctioned > 0 else None,
            "risk_score": current.get("risk_score", 0),
            "risk_level": current.get("risk_level", "None"),
            "ml_anomaly": current.get("ml_anomaly", False),
            "conditions": sorted(_rule_key(r) for r in current.get("reasons", [])),
        },
        "simulated": {
            "sanctioned_amount": sim_sanctioned,
            "expenditure": sim_expenditure,
            "completion_percentage": sim_completion,
            "utilization_pct": round(sim_expenditure / sim_sanctioned * 100, 1) if sim_sanctioned > 0 else None,
            "risk_score": simulated.get("risk_score", 0),
            "risk_level": simulated.get("risk_level", "None"),
            "ml_anomaly": simulated.get("ml_anomaly", False),
            "conditions": sorted(_rule_key(r) for r in simulated.get("reasons", [])),
        },
        "score_delta": delta,
        "conditions_resolved": resolved,
        "conditions_newly_triggered": triggered,
        "explanation": notes,
    }


# ═══════════════ audit case + escalation draft ═══════════════

def review_level(project, risk_score: int, overspend: bool) -> Dict[str, str]:
    """
    Suggest a review level from the sanctioned amount and the detected severity.
    This is a system recommendation about which administrative level should
    look at the case. It is not a claim that anything was submitted.
    """
    sanctioned = _num(project.sanctioned_amount)
    if overspend or sanctioned >= 10**7 or risk_score >= 80:
        return {
            "level": "Competent authority review",
            "basis": (
                f"Suggested because {'recorded expenditure exceeds the sanction' if overspend else ''}"
                f"{' and ' if overspend and sanctioned >= 10**7 else ''}"
                f"{'the sanctioned amount is ' + rupees(sanctioned) if sanctioned >= 10**7 else 'the risk score is ' + str(risk_score)}."
            ).replace("  ", " "),
        }
    if sanctioned >= 10**6:
        return {
            "level": "District-level verification",
            "basis": f"Suggested because the sanctioned amount is {rupees(sanctioned)}.",
        }
    return {
        "level": "Project-level review",
        "basis": f"Suggested because the sanctioned amount is {rupees(sanctioned)}.",
    }


def build_audit_case(project, db: Session, investigation: Optional[dict] = None) -> Dict[str, Any]:
    """Assemble a structured audit case from actual values only."""
    risk_row = db.query(models.RiskScore).filter(models.RiskScore.project_id == project.id).first()
    if risk_row:
        risk = {
            "risk_score": risk_row.risk_score or 0,
            "risk_level": risk_row.risk_level or "None",
            "ml_anomaly": bool(risk_row.ml_anomaly),
            "ml_score": risk_row.ml_score,
            "reasons": [r.strip() for r in (risk_row.reasons or "").split(",") if r.strip()],
        }
    else:
        pr = predict_risk(project, batch_mode=True)
        risk = {
            "risk_score": pr.get("risk_score", 0),
            "risk_level": pr.get("risk_level", "None"),
            "ml_anomaly": bool(pr.get("ml_anomaly")),
            "ml_score": pr.get("ml_score"),
            "reasons": pr.get("reasons", []),
        }

    peer = peer_benchmark(project, db)
    explorer = anomaly_explorer(project, db, peer=peer)
    evidence = evidence_gaps(project)

    # Authoritative per-project spend from the payment ledger (project_rec_info);
    # the catalog column is a zeroed legacy stamp.
    _rec_row = (
        db.query(models.ProjectRecInfo)
        .filter(models.ProjectRecInfo.project_id == project.id)
        .first()
    )
    linked_exp = _num(_rec_row.linked_expenditure) if _rec_row is not None and _rec_row.linked_expenditure is not None else 0.0

    sanctioned = _num(project.sanctioned_amount)
    expenditure = linked_exp
    completion = _num(project.completion_percentage)
    utilization = (expenditure / sanctioned * 100.0) if sanctioned > 0 else None
    overspend = sanctioned > 0 and expenditure > sanctioned

    priority = priority_breakdown(
        project, risk["risk_score"], risk_level=risk.get("risk_level"),
        linked_expenditure=linked_exp,
    )
    exposure = financial_exposure(project, linked_expenditure=linked_exp)
    checklist = build_checklist(project, risk["reasons"], evidence_stale_flag(project), linked_expenditure=linked_exp)
    level = review_level(project, int(risk["risk_score"] or 0), overspend)

    triggered = [d for d in explorer["dimensions"] if d["status"] == "flagged"]
    main_anomaly = triggered[0]["title"] if triggered else "No anomaly dimension flagged"

    case = {
        "case_id": f"{CASE_ID_PREFIX}-{project.id:06d}",
        "generated_at": _now_iso(),
        "project": {
            "project_id": project.id,
            "project_name": project.project_name,
            "state": project.state,
            "district": project.district,
            "constituency": project.constituency,
            "project_type": project.project_type,
            "status": project.status,
            "fy": project.fy,
        },
        "financials": {
            "sanctioned_amount": sanctioned,
            "sanctioned_display": rupees(sanctioned),
            "expenditure": expenditure,
            "expenditure_display": rupees(expenditure),
            "utilization_pct": round(utilization, 1) if utilization is not None else None,
            "completion_percentage": completion,
            "overspend_amount": exposure["overspend_amount"],
            "overspend_display": exposure["overspend_display"],
            "unverified_spend_display": exposure["unverified_spend_display"],
        },
        "risk": risk,
        "priority": {
            "audit_priority_score": priority["audit_priority_score"],
            "tier": priority["tier"],
            "tier_label": priority["tier_label"],
            "explanation": priority["explanation"],
            "formula": priority["formula"],
        },
        "main_anomaly": main_anomaly,
        "anomaly_dimensions": explorer["dimensions"],
        "peer_comparison": {
            "peer_count": peer["peer_count"],
            "reliability": peer["reliability"],
            "scope": peer["scope"],
            "interpretations": peer["interpretations"],
            "metrics": peer["metrics"],
            "comparable_projects": peer["comparable_projects"][:5],
        },
        "evidence": {
            "confidence": evidence["confidence"],
            "summary": evidence["summary"],
            "missing_items": evidence["missing_items"],
            "limitations": evidence["limitations"],
            "items": evidence["items"],
        },
        "recommended_verification_actions": recommended_actions(checklist),
        "checklist": checklist,
        "review_level": level,
        "financial_exposure": exposure,
        "investigation": investigation,
        "disclaimers": [
            "This case is generated from the recorded project data and the existing risk/anomaly results.",
            "A flagged anomaly indicates a project for review. It does not establish delay, irregularity or wrongdoing.",
            "No start date, completion date, expected-completion date or official delay reason exists in the source "
            "datasets, so no such value appears in this case.",
        ],
    }

    case["escalation_draft"] = build_escalation_draft(case)
    return case


def evidence_stale_flag(project) -> str:
    """Mirror of the data-quality heuristic in main.py (single rule, reused)."""
    sanctioned = _num(project.sanctioned_amount)
    expenditure = _eff_exp(project)
    completion = _num(project.completion_percentage)
    if sanctioned >= STALE_PROGRESS_AMOUNT_THRESHOLD and expenditure == 0 and completion == 0:
        return "POSSIBLY_STALE"
    if sanctioned == 0 and expenditure == 0 and completion == 0:
        return "INSUFFICIENT_DATA"
    return "NORMAL"


def build_escalation_draft(case: Dict[str, Any]) -> Dict[str, Any]:
    """
    A review-ready DRAFT. Nothing is submitted anywhere by this system, and the
    draft says so explicitly.
    """
    p = case["project"]
    f = case["financials"]
    r = case["risk"]
    pr = case["priority"]

    lines: List[str] = []
    lines.append(f"Subject: MPLADS work #{p['project_id']} — "
                 f"{pr['tier']} ({pr['tier_label']}) review of recorded financial and progress data")
    lines.append("")
    lines.append("SYSTEM-GENERATED DRAFT — NOT YET SUBMITTED")
    lines.append("")
    lines.append("1. Work details")
    lines.append(f"   Work ID: {p['project_id']}")
    lines.append(f"   Work: {p['project_name'] or 'Not recorded'}")
    lines.append(f"   Location: {p['constituency'] or 'N/A'}, {p['state'] or 'N/A'}"
                 + (f" (district: {p['district']})" if p.get("district") else ""))
    lines.append(f"   Category: {p['project_type'] or 'Not recorded'}   Status: {p['status'] or 'Not recorded'}"
                 + (f"   FY: {p['fy']}" if p.get("fy") else ""))
    lines.append("")
    lines.append("2. Recorded financial position")
    lines.append(f"   Sanctioned amount: {f['sanctioned_display']}")
    lines.append(f"   Recorded expenditure: {f['expenditure_display']}"
                 + (f" (utilization {f['utilization_pct']}%)" if f["utilization_pct"] is not None else ""))
    lines.append(f"   Reported physical progress: {f['completion_percentage']}%")
    if f.get("overspend_display"):
        lines.append(f"   Expenditure above sanction: {f['overspend_display']}")
    lines.append("")
    lines.append("3. Detected indicators")
    lines.append(f"   Risk score: {r['risk_score']}/100 ({r['risk_level']})   Audit priority: {pr['tier']} — {pr['tier_label']}")
    for reason in r["reasons"][:6]:
        lines.append(f"   - {reason}")
    if r.get("ml_anomaly"):
        lines.append("   - ML model flagged this work as a statistical outlier (model finding).")
    lines.append("")
    lines.append("4. Evidence available")
    available = [i["label"] for i in case["evidence"]["items"] if i["status"] == "available"]
    lines.append("   " + ("; ".join(available) if available else "None beyond the project record."))
    lines.append("")
    lines.append("5. Evidence missing")
    missing = case["evidence"]["missing_items"]
    lines.append("   " + ("; ".join(i["label"] for i in missing) if missing else "None identified."))
    lines.append("")
    lines.append("6. Recommended verification")
    for action in case["recommended_verification_actions"]:
        lines.append(f"   - {action}")
    lines.append("")
    lines.append("7. Suggested review level")
    lines.append(f"   {case['review_level']['level']} ({case['review_level']['basis']})")
    lines.append("")
    lines.append("Note: Anomalies are indicators identified from recorded data and do not by themselves "
                 "establish delay, irregularity or wrongdoing. No start date, completion date, expected "
                 "completion date or official delay reason is available in the source datasets for this work.")

    return {
        "subject": lines[0].replace("Subject: ", ""),
        "body": "\n".join(lines),
        "review_level": case["review_level"]["level"],
        "review_level_basis": case["review_level"]["basis"],
        "status": "DRAFT",
        "status_label": "SYSTEM-GENERATED DRAFT — NOT YET SUBMITTED",
        "submission_note": (
            "This system does not submit anything to any authority. Use the copy or download options to "
            "take this draft into your own review process."
        ),
    }


# ═══════════════ investigation workspace persistence ═══════════════

# The dossier/finding layer below is read-only. It derives everything from
# the same projects / project_rec_info / expenditures / risk_scores /
# verification_reports records already used by the rest of the platform, and
# stores only human review state. No project value is copied into a review row.

_HISTORY_LABELS = {
    "investigation_started": "Investigation started",
    "status_changed": "Investigation status changed",
    "note_updated": "Investigation note updated",
    "checklist_updated": "Evidence checklist item updated",
    "finding_reviewed": "Finding review updated",
    "action_added": "Recommendation converted to action item",
}


def _actor_triplet(actor) -> tuple:
    """Identity comes from the authenticated user, never from request JSON."""
    if actor is None:
        return None, "System", "system"
    return (
        getattr(actor, "id", None),
        getattr(actor, "name", None) or getattr(actor, "email", None) or "Authorized user",
        getattr(actor, "role", None) or "unknown",
    )


def _record_history(db: Session, investigation: models.AuditInvestigation, event: str, actor=None, detail: str = "") -> None:
    uid, name, role = _actor_triplet(actor)
    db.add(models.InvestigationHistory(
        investigation_id=investigation.id,
        event=event,
        actor_user_id=uid,
        actor_name=name,
        actor_role=role,
        detail=(detail or "")[:4000],
        created_at=_now_iso(),
    ))


def _history_payload(db: Session, investigation: models.AuditInvestigation) -> List[Dict[str, Any]]:
    rows = (
        db.query(models.InvestigationHistory)
        .filter(models.InvestigationHistory.investigation_id == investigation.id)
        .order_by(models.InvestigationHistory.created_at.desc(), models.InvestigationHistory.id.desc())
        .limit(100)
        .all()
    )
    if not rows:
        # Legacy investigations predate the append-only log. Show the real
        # creation timestamp without pretending an actor exists.
        return [{
            "id": None,
            "event": "investigation_started",
            "label": _HISTORY_LABELS["investigation_started"],
            "detail": "Workflow record created from the recorded project data.",
            "actor_name": "System",
            "actor_role": "system",
            "created_at": investigation.created_at,
        }]
    return [{
        "id": row.id,
        "event": row.event,
        "label": _HISTORY_LABELS.get(row.event, row.event.replace("_", " ").title()),
        "detail": row.detail or "",
        "actor_name": row.actor_name or "Authorized user",
        "actor_role": row.actor_role or "",
        "created_at": row.created_at,
    } for row in rows]


def _review_map(db: Session, investigation_id: int) -> Dict[str, models.InvestigationFindingReview]:
    rows = (
        db.query(models.InvestigationFindingReview)
        .filter(models.InvestigationFindingReview.investigation_id == investigation_id)
        .all()
    )
    return {row.finding_key: row for row in rows}


def _review_payload(row: Optional[models.InvestigationFindingReview]) -> Dict[str, Any]:
    if row is None:
        return {
            "status": "Open",
            "comment": "",
            "evidence_reference": "",
            "document_name": "",
            "document_url": "",
            "resolution": "",
            "reviewer_name": "",
            "reviewer_role": "",
            "updated_at": None,
            "status_options": list(FINDING_REVIEW_STATUSES),
        }
    return {
        "status": row.status,
        "comment": row.comment or "",
        "evidence_reference": row.evidence_reference or "",
        "document_name": row.document_name or "",
        "document_url": row.document_url or "",
        "resolution": row.resolution or "",
        "reviewer_name": row.reviewer_name or "",
        "reviewer_role": row.reviewer_role or "",
        "updated_at": row.updated_at,
        "status_options": list(FINDING_REVIEW_STATUSES),
    }


def _linked_transactions(db: Session, project) -> Dict[str, Any]:
    """Bounded project-level ledger history using the existing work-key join.

    This is the exact linkage behind project_rec_info.linked_expenditure; the
    house condition only prevents an explicitly House-tagged row from being
    mixed with a different House. Legacy NULL-house rows remain visible.
    """
    rec = (
        db.query(models.ProjectRecInfo)
        .filter(models.ProjectRecInfo.project_id == project.id)
        .first()
    )
    if rec is None:
        return {
            "transactions": [],
            "transaction_count": 0,
            "transaction_total": 0.0,
            "transaction_total_display": rupees(0),
            "linked_expenditure": 0.0,
            "linked_expenditure_display": rupees(0),
            "truncated": False,
            "has_expenditure": False,
        }
    from database import normkey
    params = {
        "d": normkey(project.project_name or ""),
        "c": normkey(project.constituency or ""),
        "s": normkey(project.state or ""),
        "house": project.house or "",
    }
    rows = db.execute(text("""
        SELECT id, vendor, expenditure_amount, expenditure_date, payment_status,
               work_description, constituency, state, mp_name, house, ida
        FROM expenditures
        WHERE normkey(work_description) = :d
          AND normkey(constituency) = :c
          AND normkey(state) = :s
          AND (:house = '' OR house IS NULL OR house = :house)
        ORDER BY (expenditure_date IS NULL), expenditure_date ASC, id ASC
        LIMIT 1001
    """), params).mappings().all()
    truncated = len(rows) > 1000
    rows = rows[:1000]
    transactions = []
    payment_status: Dict[str, int] = {}
    for row in rows:
        status = (row["payment_status"] or "Not recorded").strip()
        payment_status[status] = payment_status.get(status, 0) + 1
        transactions.append({
            "id": row["id"],
            "reference": f"expenditures.id={row['id']}",
            "date": row["expenditure_date"],
            "amount": float(row["expenditure_amount"] or 0),
            "amount_display": rupees(row["expenditure_amount"]),
            "vendor": (row["vendor"] or "").strip() or None,
            "payment_status": status,
            "work_description": row["work_description"],
            "constituency": row["constituency"],
            "state": row["state"],
            "mp_name": row["mp_name"],
            "house": row["house"],
            "ida": row["ida"],
        })
    total = sum(t["amount"] for t in transactions)
    return {
        "transactions": transactions,
        "transaction_count": int(rec.linked_tx_count or len(transactions)),
        "transaction_total": round(total, 2),
        "transaction_total_display": rupees(total),
        "linked_expenditure": float(rec.linked_expenditure or 0),
        "linked_expenditure_display": rupees(rec.linked_expenditure or 0),
        "truncated": truncated,
        "has_expenditure": bool(rec.has_expenditure),
        "payment_status_summary": payment_status,
        "work_key": rec.work_key,
        "work_key_expenditure": float(rec.work_key_expenditure or 0),
        "work_key_rows": int(rec.work_key_rows or 1),
        "start_status": rec.start_status,
        "approx_start_date": rec.approx_start_date,
        "recommendation_date": rec.recommendation_date,
        "recommended_by": rec.recommended_by,
    }


def _verification_evidence(db: Session, project) -> Dict[str, Any]:
    reports = (
        db.query(models.VerificationReport)
        .filter(models.VerificationReport.project_id == project.id)
        .order_by(models.VerificationReport.created_at.desc())
        .limit(50)
        .all()
    )
    photo_count = (
        db.query(func.count(models.PhotoHash.id))
        .filter(models.PhotoHash.project_id == project.id)
        .scalar() or 0
    )
    return {
        "verification_reports": [{
            "id": r.id,
            "reference": f"verification_reports.id={r.id}",
            "date": r.created_at,
            "reporter_name": r.reporter_name,
            "status": r.status,
            "review_status": r.review_status,
            "submission_kind": r.submission_kind,
            "note": r.note,
            "lat": r.lat,
            "lon": r.lon,
            "distance_m": r.distance_m,
            "photo_hash_id": r.photo_hash_id,
            "review_note": r.review_note,
        } for r in reports],
        "verification_count": len(reports),
        "photo_records": int(photo_count),
        "documents": [],
        "document_note": (
            "The source dataset contains no project-level document store. "
            "Ground-verification reports and linked photo fingerprints are shown when present; "
            "sanction orders, bills and completion certificates are not published in the dataset."
        ),
    }


def _build_investigation_dossier(db: Session, project) -> Dict[str, Any]:
    """Targeted project dossier for the existing investigation workspace."""
    from investigation import load_project_context, analyze_data_quality, analyze_risk

    ctx = load_project_context(db, project.id) or {}
    rec = (
        db.query(models.ProjectRecInfo)
        .filter(models.ProjectRecInfo.project_id == project.id)
        .first()
    )
    risk = db.query(models.RiskScore).filter(models.RiskScore.project_id == project.id).first()
    transactions = _linked_transactions(db, project)
    verification = _verification_evidence(db, project)
    dq = analyze_data_quality(db, ctx) if ctx else {"checks": [], "warnings": [], "limitation": "Project records could not be loaded."}

    peer = None
    try:
        peer = peer_benchmark(project, db)
    except Exception:
        peer = None

    shap_factors: List[Dict[str, Any]] = []
    if risk is not None and getattr(risk, "ml_anomaly", False):
        try:
            analysis = analyze_risk(db, ctx, want_peers=False)
            shap_factors = list((analysis.get("ml") or {}).get("top_factors") or [])
        except Exception:
            shap_factors = []

    reasons = []
    if risk is not None:
        raw = risk.reasons or ""
        reasons = [r.strip() for r in raw.split(",") if r.strip()] if isinstance(raw, str) else list(raw or [])

    financial = {
        "sanctioned_amount": float(project.sanctioned_amount or 0),
        "sanctioned_display": rupees(project.sanctioned_amount or 0),
        "expenditure": transactions["linked_expenditure"],
        "expenditure_display": transactions["linked_expenditure_display"],
        "utilization_pct": round((transactions["linked_expenditure"] / float(project.sanctioned_amount)) * 100, 1) if float(project.sanctioned_amount or 0) > 0 else None,
        "transaction_count": transactions["transaction_count"],
        "transaction_total": transactions["transaction_total"],
        "transaction_total_display": transactions["transaction_total_display"],
        "transactions": transactions["transactions"],
        "transactions_truncated": transactions["truncated"],
        "payment_status_summary": transactions.get("payment_status_summary") or {},
        "has_expenditure": transactions["has_expenditure"],
        "linkage": ctx.get("linkage") or {},
        "work_key": transactions.get("work_key"),
        "work_key_expenditure": transactions.get("work_key_expenditure", 0),
        "work_key_rows": transactions.get("work_key_rows", 1),
    }
    if financial["has_expenditure"] and financial["transaction_count"] and abs(financial["expenditure"] - financial["transaction_total"]) > 1:
        financial["reconciliation_note"] = (
            "The linked expenditure total differs from the sum of the bounded transaction history; "
            "the authoritative project_rec_info total is used for the headline figure."
        )

    timeline = {
        "recommendation_date": ctx.get("recommendation_date"),
        "recommendation_date_display": _date_display(ctx.get("recommendation_date")),
        "recommended_by": ctx.get("recommended_by"),
        "approx_start_date": (rec.approx_start_date if rec is not None else None),
        "approx_start_date_display": _date_display(rec.approx_start_date if rec is not None else None),
        "start_status": (rec.start_status if rec is not None else None),
        "first_expenditure_date": ctx.get("first_expenditure_date"),
        "first_expenditure_date_display": _date_display(ctx.get("first_expenditure_date")),
        "latest_expenditure_date": ctx.get("latest_expenditure_date"),
        "latest_expenditure_date_display": _date_display(ctx.get("latest_expenditure_date")),
        "activity_days": ctx.get("activity_days"),
        "delay_indicator": ctx.get("delay_indicator"),
        "completion_date": None,
        "completion_date_note": "No completion date exists in the available project dataset.",
    }

    evidence = {
        **verification,
        "data_quality": dq,
    }
    provenance = {
        "project": "projects",
        "recommendation": "project_rec_info (derived from recommended_works)",
        "expenditure": "expenditures via the existing normalized work-key linkage",
        "risk": "risk_scores + the existing rule/ML engines",
        "ground_verification": "verification_reports / photo_hashes",
    }
    limitations = [
        "No narrative project description, sanction date, sanction document, work start date, expected completion date or completion certificate is present in the source datasets.",
        "Expenditure rows are linked by the existing normalized name + constituency + state work key; they are not a legal payment ledger.",
        "A detected pattern is a review indicator, not a finding of wrongdoing or a confirmed allegation.",
        dq.get("limitation") or "Data-quality assessment is limited to published fields.",
    ]
    if transactions["truncated"]:
        limitations.append("The linked transaction history is bounded to 1,000 records in this view; the project-level total remains the authoritative aggregate.")

    return {
        "generated_at": _now_iso(),
        "project": {
            "project_id": project.id,
            "project_name": project.project_name,
            "description": None,
            "description_note": "The source catalogue has no narrative description field.",
            "project_type": project.project_type,
            "house": project.house,
            "mp": ctx.get("recommended_by"),
            "state": project.state,
            "district": project.district,
            "constituency": project.constituency,
            "fy": project.fy,
            "status": project.status,
            "completion_percentage": project.completion_percentage,
        },
        "sanction": {
            "sanctioned_amount": financial["sanctioned_amount"],
            "sanctioned_display": financial["sanctioned_display"],
            "sanction_date": None,
            "sanction_reference": None,
            "note": "Sanction date and sanction-document reference are not published in the available dataset.",
        },
        "financial": financial,
        "timeline": timeline,
        "risk": {
            "score": risk.risk_score if risk is not None else None,
            "level": risk.risk_level if risk is not None else None,
            "ml_anomaly": bool(risk.ml_anomaly) if risk is not None else False,
            "ml_score": risk.ml_score if risk is not None else None,
            "reasons": reasons,
            "shap_explanations": shap_factors,
        },
        "vendors": ctx.get("vendors") or [],
        "evidence": evidence,
        "data_quality": dq,
        "similar_projects": peer or {},
        "limitations": limitations,
        "provenance": provenance,
    }


def _date_display(value) -> Optional[str]:
    if not value:
        return None
    try:
        d = datetime.strptime(str(value)[:10], "%Y-%m-%d")
        return d.strftime("%d %b %Y").lstrip("0")
    except Exception:
        return str(value)


def _transaction_evidence(transactions: List[Dict[str, Any]], limit: int = 30) -> List[Dict[str, Any]]:
    return [{
        "type": "expenditure",
        "reference": t["reference"],
        "fields": {
            "date": t.get("date"),
            "amount": t.get("amount"),
            "amount_display": t.get("amount_display"),
            "vendor": t.get("vendor"),
            "payment_status": t.get("payment_status"),
        },
    } for t in (transactions or [])[:limit]]


def _build_findings(db: Session, project, dossier: Dict[str, Any], investigation: models.AuditInvestigation) -> List[Dict[str, Any]]:
    """Generate evidence-linked findings from the current dossier.

    `confirmed_data_inconsistency` is reserved for measurable contradictions in
    the recorded fields or linkage. Risk-model and peer signals remain
    `potential_concern`; absent source fields are `missing_information`.
    """
    reviews = _review_map(db, investigation.id)
    findings: List[Dict[str, Any]] = []
    financial = dossier.get("financial") or {}
    timeline = dossier.get("timeline") or {}
    risk = dossier.get("risk") or {}
    project_info = dossier.get("project") or {}
    verification = (dossier.get("evidence") or {})
    transactions = financial.get("transactions") or []
    sanc = float(financial.get("sanctioned_amount") or 0)
    exp = float(financial.get("expenditure") or 0)
    util = financial.get("utilization_pct")
    comp = project_info.get("completion_percentage")
    status = str(project_info.get("status") or "").lower()
    recommendation_date = timeline.get("recommendation_date")
    first_date = timeline.get("first_expenditure_date")

    def add(key, title, severity, classification, explanation, records, source, limitations, action, amounts=None, dates=None, percentages=None):
        review = _review_payload(reviews.get(key))
        findings.append({
            "key": key,
            "title": title,
            "severity": severity,
            "classification": classification,
            "classification_label": FINDING_CLASSIFICATIONS[classification],
            "explanation": explanation,
            "evidence": {
                "summary": explanation,
                "records": records,
                "source": source,
            },
            "amounts": amounts or {},
            "dates": dates or {},
            "percentages": percentages or {},
            "source": source,
            "limitations": limitations,
            "suggested_action": action,
            "investigation_status": investigation.status,
            "status": review["status"],
            "review": review,
        })

    project_record = {
        "type": "project",
        "reference": f"projects.id={project.id}",
        "fields": {
            "sanctioned_amount": sanc,
            "expenditure": exp,
            "completion_percentage": comp,
            "status": project_info.get("status"),
            "house": project_info.get("house"),
        },
    }

    if sanc > 0 and exp > sanc:
        variance = exp - sanc
        add(
            "expenditure_over_sanction",
            "Recorded expenditure exceeds the sanctioned amount",
            "high",
            "confirmed_data_inconsistency",
            f"The authoritative linked ledger total is {rupees(exp)}, which exceeds the recorded sanction of {rupees(sanc)} by {rupees(variance)} ({util:.1f}% utilization).",
            [project_record] + _transaction_evidence(transactions),
            "projects.sanctioned_amount + project_rec_info.linked_expenditure + expenditures",
            ["This is a recorded-value inconsistency, not a conclusion that an irregular payment occurred. Revised sanctions may exist outside the dataset."],
            "Verify the sanction order and any revised sanction, then reconcile the linked payment vouchers against the sanctioned scope.",
            {"sanctioned": sanc, "expenditure": exp, "variance": variance},
            {"first_expenditure": first_date, "latest_expenditure": timeline.get("latest_expenditure_date")},
            {"utilization": util},
        )

    if exp > 0 and comp is not None and ((util is not None and util >= 80 and comp < 50) or abs(float(util or 0) - float(comp)) >= 25):
        add(
            "expenditure_progress_mismatch",
            "Financial utilization and reported physical progress diverge",
            "high" if (util or 0) >= 80 and comp < 50 else "medium",
            "potential_concern",
            f"Linked expenditure is {rupees(exp)} ({(util if util is not None else 0):.1f}% of sanction) while reported physical progress is {comp}%. The gap is a review indicator, not proof that the work is incomplete.",
            [project_record] + _transaction_evidence(transactions, 15),
            "projects + project_rec_info + expenditures",
            ["Physical progress is a reported percentage; no independent measurement or completion certificate is included in the dataset."],
            "Request the latest measurement book, inspection note and current site photographs, then reconcile them with the recorded payments.",
            {"sanctioned": sanc, "expenditure": exp},
            {"first_expenditure": first_date, "latest_expenditure": timeline.get("latest_expenditure_date")},
            {"utilization": util, "completion": comp, "gap": round(abs(float(util or 0) - float(comp)), 1)},
        )

    if recommendation_date and first_date and str(first_date)[:10] < str(recommendation_date)[:10]:
        add(
            "expenditure_before_recommendation",
            "An expenditure date precedes the recorded recommendation date",
            "high",
            "confirmed_data_inconsistency",
            f"The earliest linked payment is dated {_date_display(first_date)}, before the recorded recommendation date of {_date_display(recommendation_date)}. This may indicate a linkage or source-date error.",
            [
                {"type": "project", "reference": f"projects.id={project.id}", "fields": {"recommendation_date": recommendation_date}},
                {"type": "expenditure", "reference": transactions[0]["reference"] if transactions else "project_rec_info.linked_expenditure", "fields": {"first_expenditure_date": first_date}},
            ],
            "project_rec_info.recommendation_date + expenditures.expenditure_date",
            ["The date relation is factual, but the cause could be a source-data or work-key linkage error."],
            "Inspect the recommendation record and the earliest payment voucher; verify the project-to-payment relationship before drawing a conclusion.",
            {"expenditure": exp},
            {"recommendation_date": recommendation_date, "first_expenditure_date": first_date},
            {},
        )

    if not recommendation_date or sanc <= 0:
        missing = []
        if not recommendation_date:
            missing.append("recommendation date")
        if sanc <= 0:
            missing.append("sanctioned amount")
        add(
            "missing_sanction_or_recommendation",
            "Sanction or recommendation anchor is missing",
            "medium",
            "missing_information",
            f"The investigation dossier could not establish the following mandatory timeline/financial anchors: {', '.join(missing)}. No value has been estimated.",
            [project_record],
            "projects + project_rec_info",
            ["Absence of a field is a dataset limitation; it is not evidence that the work was not sanctioned."],
            "Obtain the recommendation and sanction records from the implementing authority and record the missing reference against this finding.",
            {"sanctioned": sanc}, {"recommendation_date": recommendation_date}, {},
        )

    if status == "completed" and (comp is None or float(comp) < 90):
        add(
            "completion_evidence_gap",
            "Completed status is not supported by recorded physical progress",
            "high",
            "confirmed_data_inconsistency",
            f"The project is marked Completed while reported physical progress is {comp if comp is not None else 'not recorded'}%. The completion date and certificate are not present in the source dataset.",
            [project_record],
            "projects.status + projects.completion_percentage",
            ["The recorded completion percentage may be stale; the dataset has no completion date or certificate to validate it."],
            "Obtain the completion certificate and final measurement record, then verify the reported progress before relying on the Completed status.",
            {}, {}, {"completion": comp},
        )

    if exp > 0 and not dossier.get("vendors"):
        add(
            "vendor_missing",
            "Payments are recorded without vendor information",
            "medium",
            "missing_information",
            f"{rupees(exp)} of linked expenditure is recorded, but no vendor name is present in the linked payment rows. Payment responsibilities cannot be traced to a vendor from this record.",
            [project_record] + _transaction_evidence(transactions, 10),
            "project_rec_info.linked_expenditure + expenditures.vendor",
            ["A missing vendor name may be a source-data gap rather than a payment outside the recorded process."],
            "Request the payment vouchers and vendor identifiers, then update the evidence reference for this finding.",
            {"expenditure": exp}, {}, {},
        )

    if int(financial.get("work_key_rows") or 1) > 1:
        add(
            "ambiguous_work_linkage",
            "Multiple catalogue rows share this work key",
            "medium",
            "confirmed_data_inconsistency",
            f"{financial.get('work_key_rows')} catalogue rows share the normalized work key, so the work-level payment total of {rupees(financial.get('work_key_expenditure') or 0)} cannot be attributed to this project alone. Headline per-project expenditure is therefore not asserted from the shared total.",
            [project_record, {"type": "project_rec_info", "reference": f"project_rec_info.project_id={project.id}", "fields": {"work_key_rows": financial.get("work_key_rows"), "work_key_expenditure": financial.get("work_key_expenditure")}}],
            "project_rec_info.work_key_rows + work_key_expenditure",
            ["This is a linkage ambiguity in the source records; it does not establish duplicate payments."],
            "Compare the catalogue rows and confirm the intended project-to-work relationship with the implementing agency.",
            {"work_key_expenditure": financial.get("work_key_expenditure")}, {}, {},
        )

    if exp > 0 and not transactions:
        add(
            "expenditure_without_dated_transactions",
            "Expenditure total has no linked dated transactions",
            "medium",
            "missing_information",
            f"A linked expenditure total of {rupees(exp)} exists, but the current linkage returned no dated expenditure rows. The payment dates and individual amounts cannot be verified from this view.",
            [project_record],
            "project_rec_info.linked_expenditure + expenditures",
            ["The total may be preserved from a prior linkage while the transaction rows are unavailable or not matched."],
            "Reconcile project_rec_info.linked_expenditure against the payment ledger and record the source reference used.",
            {"expenditure": exp}, {}, {},
        )

    if risk.get("ml_anomaly"):
        add(
            "ml_anomaly",
            "ML model flagged a statistical outlier",
            "medium",
            "potential_concern",
            f"The existing Isolation Forest model flagged this project as a statistical outlier (score {risk.get('ml_score') if risk.get('ml_score') is not None else 'not recorded'}). The model result is an indicator for review, not an allegation.",
            [{"type": "risk_scores", "reference": f"risk_scores.project_id={project.id}", "fields": {"risk_score": risk.get("score"), "ml_score": risk.get("ml_score"), "reasons": risk.get("reasons")}}],
            "risk_scores.ml_anomaly / ml_score; existing ML predictor",
            ["A statistical outlier can be explained by scale, scope or source-data patterns and is not proof of irregularity.", *(["SHAP factors are model explanations, not audit findings."] if risk.get("shap_explanations") else [])],
            "Review the model factors and compare the project with the existing peer group before recording a conclusion.",
            {}, {}, {},
        )

    if risk.get("reasons"):
        add(
            "rule_risk_indicators",
            "Rule-based risk indicators are present",
            "high" if str(risk.get("level") or "").lower() == "high" else "medium",
            "potential_concern",
            "The existing deterministic risk engine recorded the following indicators: " + "; ".join(risk.get("reasons") or []) + ".",
            [{"type": "risk_scores", "reference": f"risk_scores.project_id={project.id}", "fields": {"risk_score": risk.get("score"), "risk_level": risk.get("level"), "reasons": risk.get("reasons")}}],
            "risk_scores.reasons (existing rule engine)",
            ["Rule-based risk indicators are review triggers, not confirmed irregularities."],
            "Map each rule indicator to the underlying financial or progress record and record which one is supported by evidence.",
            {}, {}, {},
        )

    if verification.get("verification_count", 0) == 0 and verification.get("photo_records", 0) == 0:
        add(
            "no_ground_evidence",
            "No ground-verification or photo evidence is linked",
            "low",
            "missing_information",
            "The dossier found no ground-verification report and no linked photo fingerprint for this project. The absence is a coverage gap in the available evidence.",
            [{"type": "evidence_coverage", "reference": f"projects.id={project.id}", "fields": {"verification_reports": 0, "photo_records": 0}}],
            "verification_reports + photo_hashes",
            ["No linked evidence is not proof that no site visit occurred; it means the dataset has no recorded evidence."],
            "Request current site photographs or a field verification and record the resulting reference in this finding.",
            {}, {}, {},
        )

    return findings


def _build_recommendations(db: Session, investigation: models.AuditInvestigation, findings: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    existing = {
        c.item_key for c in db.query(models.AuditEvidenceCheck)
        .filter(models.AuditEvidenceCheck.investigation_id == investigation.id)
        .all()
    }
    out = []
    for finding in findings:
        key = f"rec:{finding['key']}"
        out.append({
            "key": key,
            "finding_key": finding["key"],
            "finding_title": finding["title"],
            "title": finding.get("suggested_action") or "Verify the recorded finding",
            "why": finding.get("explanation"),
            "severity": finding.get("severity"),
            "classification": finding.get("classification"),
            "action_item_key": key,
            "added": key in existing,
        })
    return out


def _build_summary(dossier: Dict[str, Any], findings: List[Dict[str, Any]]) -> Dict[str, Any]:
    project = dossier.get("project") or {}
    financial = dossier.get("financial") or {}
    timeline = dossier.get("timeline") or {}
    risk = dossier.get("risk") or {}
    unresolved = [f for f in findings if f.get("status") not in FINAL_FINDING_STATUSES]
    by_class = {k: 0 for k in FINDING_CLASSIFICATIONS}
    for finding in findings:
        by_class[finding.get("classification")] = by_class.get(finding.get("classification"), 0) + 1
    lines = [
        "PRELIMINARY ANALYSIS — generated from recorded project data. It is not a finding of wrongdoing or a final audit conclusion.",
        "",
        f"Project #{project.get('project_id')} — {project.get('project_name') or 'Unnamed project'}",
        f"{project.get('house') or 'House not recorded'} · {project.get('state') or 'State not recorded'} · {project.get('constituency') or 'Constituency not recorded'}",
        "",
        f"Financial position: sanction {financial.get('sanctioned_display')}, linked expenditure {financial.get('expenditure_display')}, utilization {financial.get('utilization_pct') if financial.get('utilization_pct') is not None else 'not computable'}.",
        f"Physical progress: {project.get('completion_percentage') if project.get('completion_percentage') is not None else 'not recorded'}% · status {project.get('status') or 'not recorded'}.",
        f"Timeline: recommendation {timeline.get('recommendation_date_display') or 'not recorded'}; first linked expenditure {timeline.get('first_expenditure_date_display') or 'not recorded'}; latest {timeline.get('latest_expenditure_date_display') or 'not recorded'}.",
        f"Risk: {risk.get('score') if risk.get('score') is not None else 'not scored'} ({risk.get('level') or 'not scored'}); ML anomaly: {'yes' if risk.get('ml_anomaly') else 'no'}.",
        "",
        f"Findings: {len(findings)} total · {len(unresolved)} unresolved · confirmed inconsistencies {by_class.get('confirmed_data_inconsistency', 0)} · potential concerns {by_class.get('potential_concern', 0)} · missing information {by_class.get('missing_information', 0)}.",
    ]
    if unresolved:
        lines.append("Unresolved findings: " + "; ".join(f["title"] for f in unresolved[:6]) + ".")
    if dossier.get("limitations"):
        lines.append("Data limitations: " + " ".join(dossier["limitations"][:2]))
    return {
        "text": "\n".join(lines),
        "preliminary": True,
        "finding_count": len(findings),
        "unresolved_count": len(unresolved),
        "by_classification": by_class,
        "unresolved_findings": [f["key"] for f in unresolved],
        "data_limitations": dossier.get("limitations") or [],
    }


def _build_workspace_extras(db: Session, project, investigation: models.AuditInvestigation) -> Dict[str, Any]:
    dossier = _build_investigation_dossier(db, project)
    findings = _build_findings(db, project, dossier, investigation)
    recommendations = _build_recommendations(db, investigation, findings)
    return {
        "dossier": dossier,
        "findings": findings,
        "recommendations": recommendations,
        "summary": _build_summary(dossier, findings),
        "history": _history_payload(db, investigation),
        "finding_status_options": list(FINDING_REVIEW_STATUSES),
        "finding_classifications": FINDING_CLASSIFICATIONS,
    }


def update_finding_review(db: Session, project, finding_key: str, actor=None, status_value: Optional[str] = None,
                         comment: Optional[str] = None, evidence_reference: Optional[str] = None,
                         document_name: Optional[str] = None, document_url: Optional[str] = None,
                         resolution: Optional[str] = None) -> Dict[str, Any]:
    investigation = (
        db.query(models.AuditInvestigation)
        .filter(models.AuditInvestigation.project_id == project.id)
        .first()
    )
    if not investigation:
        raise ValueError("Investigation has not been started for this project")
    dossier = _build_investigation_dossier(db, project)
    findings = _build_findings(db, project, dossier, investigation)
    finding = next((f for f in findings if f["key"] == finding_key), None)
    if finding is None:
        raise ValueError("Finding not found for this investigation")
    if status_value is not None and status_value not in FINDING_REVIEW_STATUSES:
        raise ValueError(f"Invalid finding status: {status_value}")
    row = (
        db.query(models.InvestigationFindingReview)
        .filter(
            models.InvestigationFindingReview.investigation_id == investigation.id,
            models.InvestigationFindingReview.finding_key == finding_key,
        )
        .first()
    )
    now = _now_iso()
    if row is None:
        row = models.InvestigationFindingReview(
            investigation_id=investigation.id,
            finding_key=finding_key,
            status="Open",
            created_at=now,
            updated_at=now,
        )
        db.add(row)
    old_status = row.status
    if status_value is not None:
        if status_value in FINAL_FINDING_STATUSES and not ((resolution or row.resolution or "").strip()):
            raise ValueError("A resolution explanation is required before a finding can be verified or closed")
        row.status = status_value
    if comment is not None:
        row.comment = comment[:4000]
    if evidence_reference is not None:
        row.evidence_reference = evidence_reference[:4000]
    if document_name is not None:
        row.document_name = document_name[:240]
    if document_url is not None:
        row.document_url = document_url[:2000]
    if resolution is not None:
        row.resolution = resolution[:4000]
    uid, name, role = _actor_triplet(actor)
    row.reviewer_user_id = uid
    row.reviewer_name = name
    row.reviewer_role = role
    row.updated_at = now
    investigation.updated_at = now
    detail = f"Finding {finding_key}: {old_status} → {row.status}"
    if comment is not None:
        detail += " · comment updated"
    if evidence_reference is not None:
        detail += " · evidence reference updated"
    if resolution is not None:
        detail += " · resolution recorded"
    _record_history(db, investigation, "finding_reviewed", actor=actor, detail=detail)
    db.commit()
    return _investigation_payload(db, project, investigation)


def add_recommendation_action(db: Session, project, finding_key: str, actor=None) -> Dict[str, Any]:
    investigation = (
        db.query(models.AuditInvestigation)
        .filter(models.AuditInvestigation.project_id == project.id)
        .first()
    )
    if not investigation:
        raise ValueError("Investigation has not been started for this project")
    dossier = _build_investigation_dossier(db, project)
    findings = _build_findings(db, project, dossier, investigation)
    finding = next((f for f in findings if f["key"] == finding_key), None)
    if finding is None:
        raise ValueError("Finding not found for this investigation")
    item_key = f"rec:{finding_key}"
    existing = (
        db.query(models.AuditEvidenceCheck)
        .filter(
            models.AuditEvidenceCheck.investigation_id == investigation.id,
            models.AuditEvidenceCheck.item_key == item_key,
        )
        .first()
    )
    if existing is None:
        now = _now_iso()
        db.add(models.AuditEvidenceCheck(
            investigation_id=investigation.id,
            item_key=item_key,
            label=(finding.get("suggested_action") or "Verify the finding")[:500],
            category="finding_recommendation",
            severity=finding.get("severity") or "medium",
            rationale=(f"Recommended from finding '{finding['title']}'. {finding.get('explanation') or ''}")[:4000],
            status="Pending",
            updated_at=now,
        ))
        investigation.updated_at = now
        _record_history(db, investigation, "action_added", actor=actor, detail=f"Action item added from finding: {finding['title']}")
        db.commit()
    return _investigation_payload(db, project, investigation)


def compose_investigation_report(db: Session, project) -> Dict[str, Any]:
    investigation = (
        db.query(models.AuditInvestigation)
        .filter(models.AuditInvestigation.project_id == project.id)
        .first()
    )
    if not investigation:
        raise ValueError("Investigation has not been started for this project")
    extras = _build_workspace_extras(db, project, investigation)
    dossier = extras["dossier"]
    project_info = dossier["project"]
    fin = dossier["financial"]
    timeline = dossier["timeline"]
    risk = dossier["risk"]
    summary = extras["summary"]
    lines = [
        f"MPLADS INVESTIGATION REPORT — PROJECT #{project.id}",
        "PRELIMINARY ANALYSIS — generated from recorded data; not a final finding of wrongdoing.",
        f"Investigation status: {investigation.status} · generated {_now_iso()}",
        "",
        "1. PROJECT OVERVIEW",
        f"Work: {project_info.get('project_name') or 'Not recorded'}",
        f"House: {project_info.get('house') or 'Not recorded'}",
        f"Location: {project_info.get('constituency') or 'N/A'}, {project_info.get('state') or 'N/A'}" + (f" (district: {project_info.get('district')})" if project_info.get('district') else ""),
        f"MP: {project_info.get('mp') or 'Not recorded'} · FY: {project_info.get('fy') or 'Not recorded'} · Type: {project_info.get('project_type') or 'Not recorded'}",
        f"Description: {project_info.get('description') or 'Not available in the source dataset.'}",
        "",
        "2. FINANCIAL POSITION",
        f"Sanctioned: {fin.get('sanctioned_display')} · Linked expenditure: {fin.get('expenditure_display')} · Utilisation: {fin.get('utilization_pct') if fin.get('utilization_pct') is not None else 'not computable'}.",
        f"Linked transactions: {fin.get('transaction_count')} · first {timeline.get('first_expenditure_date_display') or 'not recorded'} · latest {timeline.get('latest_expenditure_date_display') or 'not recorded'}.",
        "",
        "3. PHYSICAL PROGRESS AND TIMELINE",
        f"Completion: {project_info.get('completion_percentage') if project_info.get('completion_percentage') is not None else 'not recorded'}% · status: {project_info.get('status') or 'not recorded'}.",
        f"Recommendation: {timeline.get('recommendation_date_display') or 'not recorded'} · approx start: {timeline.get('approx_start_date_display') or 'not recorded'} · completion date: not available.",
        "",
        "4. RISK INDICATORS",
        f"Risk score: {risk.get('score') if risk.get('score') is not None else 'not scored'} ({risk.get('level') or 'not scored'}) · ML anomaly: {'yes' if risk.get('ml_anomaly') else 'no'}.",
    ]
    for reason in (risk.get("reasons") or [])[:8]:
        lines.append(f"- {reason}")
    lines += ["", "5. FINDINGS (recorded facts and review indicators)"]
    for i, finding in enumerate(extras["findings"], 1):
        lines.append(f"{i}. {finding['title']} [{finding['classification_label']}; severity {finding['severity']}; review {finding['status']}]")
        lines.append(f"   {finding['explanation']}")
        lines.append(f"   Source: {finding['source']}")
        for record in (finding.get("evidence") or {}).get("records", [])[:8]:
            fields = record.get("fields") or {}
            lines.append(f"   Evidence: {record.get('reference')} {fields}")
    lines += ["", "6. REVIEW AND ACTIONS"]
    for rec in extras["recommendations"]:
        lines.append(f"- {rec['title']} (from {rec['finding_title']}; {'added as action item' if rec['added'] else 'not yet an action item'})")
    lines += ["", "7. DATA LIMITATIONS"]
    for limitation in dossier.get("limitations") or []:
        lines.append(f"- {limitation}")
    lines += ["", "8. HISTORY"]
    for event in extras["history"][:30]:
        lines.append(f"- {event.get('created_at')} · {event.get('label')} · {event.get('actor_name')} · {event.get('detail')}")
    lines += ["", summary["text"], "", "END OF PRELIMINARY REPORT"]
    return {
        "filename": f"MPLADS-investigation-{project.id}.txt",
        "body": "\n".join(lines),
        "generated_at": _now_iso(),
        "preliminary": True,
        "investigation_status": investigation.status,
    }


# ═══════════════ investigation workspace persistence ═══════════════

def _investigation_payload(db: Session, project, investigation: models.AuditInvestigation) -> Dict[str, Any]:
    checks = (
        db.query(models.AuditEvidenceCheck)
        .filter(models.AuditEvidenceCheck.investigation_id == investigation.id)
        .order_by(models.AuditEvidenceCheck.id.asc())
        .all()
    )
    items = [
        {
            "item_key": c.item_key,
            "label": c.label,
            "category": c.category,
            "severity": c.severity,
            "rationale": c.rationale,
            "status": c.status,
            "updated_at": c.updated_at,
        }
        for c in checks
    ]
    total = len(items)
    resolved = sum(1 for i in items if i["status"] != "Pending")
    discrepancies = sum(1 for i in items if i["status"] == "Discrepancy Found")
    extras = _build_workspace_extras(db, project, investigation)
    return {
        "project_id": project.id,
        "investigation": {
            "id": investigation.id,
            "status": investigation.status,
            "note": investigation.note,
            "created_at": investigation.created_at,
            "updated_at": investigation.updated_at,
            "risk_score_at_start": investigation.risk_score_at_start,
            "risk_level_at_start": investigation.risk_level_at_start,
            "priority_tier_at_start": investigation.priority_tier_at_start,
        },
        "items": items,
        "total_items": total,
        "resolved_items": resolved,
        "pending_items": total - resolved,
        "discrepancies": discrepancies,
        "progress_pct": round(resolved / total * 100, 1) if total else 0.0,
        "dossier": extras["dossier"],
        "findings": extras["findings"],
        "recommendations": extras["recommendations"],
        "summary": extras["summary"],
        "history": extras["history"],
        "finding_status_options": extras["finding_status_options"],
        "finding_classifications": extras["finding_classifications"],
        "status_options": list(INVESTIGATION_STATUSES),
        "item_status_options": list(EVIDENCE_STATUSES),
        "workflow": list(INVESTIGATION_STATUSES),
        "note": (
            "Checklist items are recommended verification actions generated from this project's detected anomalies. "
            "They are not allegations."
        ),
    }


def get_investigation(db: Session, project, regenerate: bool = False) -> Optional[Dict[str, Any]]:
    investigation = (
        db.query(models.AuditInvestigation)
        .filter(models.AuditInvestigation.project_id == project.id)
        .first()
    )
    if not investigation:
        return None
    if regenerate:
        _sync_checklist(db, project, investigation)
    return _investigation_payload(db, project, investigation)


def _sync_checklist(db: Session, project, investigation: models.AuditInvestigation) -> None:
    """Create any missing checklist items, preserving the status of existing ones."""
    risk_row = db.query(models.RiskScore).filter(models.RiskScore.project_id == project.id).first()
    reasons = [r.strip() for r in (risk_row.reasons or "").split(",")] if risk_row else []
    _rec_row = (
        db.query(models.ProjectRecInfo)
        .filter(models.ProjectRecInfo.project_id == project.id)
        .first()
    )
    linked_exp = _num(_rec_row.linked_expenditure) if _rec_row is not None and _rec_row.linked_expenditure is not None else 0.0
    checklist = build_checklist(project, reasons, evidence_stale_flag(project), linked_expenditure=linked_exp)

    existing = {
        c.item_key: c
        for c in db.query(models.AuditEvidenceCheck)
        .filter(models.AuditEvidenceCheck.investigation_id == investigation.id)
        .all()
    }
    now = _now_iso()
    for item in checklist:
        if item["item_key"] in existing:
            row = existing[item["item_key"]]
            row.label = item["label"]
            row.category = item["category"]
            row.severity = item["severity"]
            row.rationale = item["rationale"]
            continue
        db.add(models.AuditEvidenceCheck(
            investigation_id=investigation.id,
            item_key=item["item_key"],
            label=item["label"],
            category=item["category"],
            severity=item["severity"],
            rationale=item["rationale"],
            status="Pending",
            updated_at=now,
        ))
    db.commit()


def start_investigation(db: Session, project, actor=None) -> Dict[str, Any]:
    """Create the workspace if needed, then return it."""
    created = False
    investigation = (
        db.query(models.AuditInvestigation)
        .filter(models.AuditInvestigation.project_id == project.id)
        .first()
    )
    _rec_row = (
        db.query(models.ProjectRecInfo)
        .filter(models.ProjectRecInfo.project_id == project.id)
        .first()
    )
    linked_exp = _num(_rec_row.linked_expenditure) if _rec_row is not None and _rec_row.linked_expenditure is not None else 0.0
    now = _now_iso()
    if not investigation:
        risk_row = db.query(models.RiskScore).filter(models.RiskScore.project_id == project.id).first()
        risk_score = int(risk_row.risk_score or 0) if risk_row else int(predict_risk(project, batch_mode=True).get("risk_score", 0))
        tier = priority_breakdown(
            project, risk_score,
            risk_level=(risk_row.risk_level if risk_row else None),
            linked_expenditure=linked_exp,
        )
        investigation = models.AuditInvestigation(
            project_id=project.id,
            status="New",
            risk_score_at_start=risk_score,
            risk_level_at_start=(risk_row.risk_level if risk_row else None),
            priority_tier_at_start=tier["tier"],
            created_at=now,
            updated_at=now,
        )
        db.add(investigation)
        db.commit()
        db.refresh(investigation)
        created = True
    _sync_checklist(db, project, investigation)
    if created:
        _record_history(
            db, investigation, "investigation_started", actor=actor,
            detail="Investigation workspace created from the recorded project data and existing risk results.",
        )
        db.commit()
    return _investigation_payload(db, project, investigation)


def update_investigation(db: Session, project, status_value: Optional[str], note: Optional[str], actor=None) -> Dict[str, Any]:
    investigation = (
        db.query(models.AuditInvestigation)
        .filter(models.AuditInvestigation.project_id == project.id)
        .first()
    )
    if not investigation:
        raise ValueError("Investigation has not been started for this project")
    old_status = investigation.status
    old_note = investigation.note
    if status_value is not None:
        if status_value not in INVESTIGATION_STATUSES:
            raise ValueError(f"Invalid investigation status: {status_value}")
        investigation.status = status_value
    if note is not None:
        investigation.note = note[:4000]
    investigation.updated_at = _now_iso()
    if old_status != investigation.status:
        _record_history(db, investigation, "status_changed", actor=actor,
                        detail=f"Investigation status: {old_status} → {investigation.status}")
    if old_note != investigation.note:
        _record_history(db, investigation, "note_updated", actor=actor,
                        detail="Investigation note updated.")
    db.commit()
    return _investigation_payload(db, project, investigation)


def update_evidence_item(db: Session, project, item_key: str, status_value: str, actor=None) -> Dict[str, Any]:
    if status_value not in EVIDENCE_STATUSES:
        raise ValueError(f"Invalid evidence status: {status_value}")
    investigation = (
        db.query(models.AuditInvestigation)
        .filter(models.AuditInvestigation.project_id == project.id)
        .first()
    )
    if not investigation:
        raise ValueError("Investigation has not been started for this project")
    row = (
        db.query(models.AuditEvidenceCheck)
        .filter(
            models.AuditEvidenceCheck.investigation_id == investigation.id,
            models.AuditEvidenceCheck.item_key == item_key,
        )
        .first()
    )
    if not row:
        raise ValueError("Checklist item not found")
    old_status = row.status
    row.status = status_value
    row.updated_at = _now_iso()
    investigation.updated_at = row.updated_at
    _record_history(db, investigation, "checklist_updated", actor=actor,
                    detail=f"{row.label}: {old_status} → {status_value}")
    db.commit()
    return _investigation_payload(db, project, investigation)


def active_investigations(db: Session, limit: int = 200) -> Dict[str, Any]:
    rows = (
        db.query(models.AuditInvestigation, models.Project)
        .outerjoin(models.Project, models.Project.id == models.AuditInvestigation.project_id)
        .order_by(models.AuditInvestigation.updated_at.desc())
        .limit(limit)
        .all()
    )
    results = []
    for inv, project in rows:
        checks = (
            db.query(models.AuditEvidenceCheck)
            .filter(models.AuditEvidenceCheck.investigation_id == inv.id)
            .all()
        )
        total = len(checks)
        resolved = sum(1 for c in checks if c.status != "Pending")
        results.append({
            "project_id": inv.project_id,
            "project_name": project.project_name if project else None,
            "status": inv.status,
            "tier": inv.priority_tier_at_start,
            "created_at": inv.created_at,
            "updated_at": inv.updated_at,
            "total_items": total,
            "resolved_items": resolved,
            "discrepancies": sum(1 for c in checks if c.status == "Discrepancy Found"),
            "progress_pct": round(resolved / total * 100, 1) if total else 0.0,
        })
    by_status = {s: 0 for s in INVESTIGATION_STATUSES}
    for r in results:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    return {
        "total": len(results),
        "by_status": by_status,
        "investigations": results,
    }
