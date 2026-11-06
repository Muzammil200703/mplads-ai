"""
Authoritative financial-aggregation service
===========================================

Single source of truth for every "Total Expenditure" / utilization /
completed-works number in the application.

Why this exists — the data model has THREE independent datasets:

  projects            — recommended-works catalog (sanctioned amounts,
                        status, FY, completion% as recorded by the
                        recommending authority). Its per-project
                        `expenditure` column is a LEGACY field: only 4
                        seed/test rows carry a value, so aggregating it
                        systematically under-reports spend as ~₹0.
  expenditures        — the payment ledger: 106,263 dated transactions,
                        ₹391.87 Cr avg-scale rows, totaling ₹3,918.73 Cr.
                        This is the ONLY authoritative expenditure source.
  completed_works     — the completions ledger: 43,173 works with final
                        amounts and completion dates. This is the ONLY
                        authoritative completed-works source.

The two ledgers are payment/completion records with generic work
descriptions; they do NOT join reliably to individual project rows
(verified: only 62/6,315 distinct ledger keys match any project key).
So portfolio aggregates are computed FROM THE LEDGERS DIRECTLY, and
`fy`-scoped queries attribute ledger rows to financial years by
payment/completion date using the standard Indian FY (Apr–Mar).
The FY values stored on project rows come from the same recommendation
dataset and are used unchanged for project-scoped metrics (counts,
sanctioned).

Per-project / per-state / per-constituency views keep sanctioned and
project counts from `projects` (they are the recommended-work catalog)
and are not rewritten here; anything labeled "expenditure" or
"completed" anywhere in the app must come from this module.

All functions take a SQLAlchemy Session and are SQL-aggregated
(constant memory; nothing is materialized in Python).
"""

from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, case, text
from sqlalchemy import Integer, Text as SqlText
from sqlalchemy.orm import Session

import models


# Indian financial year from an ISO-like date string 'YYYY-MM-DD...':
# months 04..12 belong to FY <YYYY>-<YYYY+1>; months 01..03 belong to
# the previous year's FY. Emits the short form 'YYYY-YY' to match the
# project catalog's fy values (e.g. '2025-26').
def _fy_expr(col):
    """SQL expression computing the Indian FY (short form 'YYYY-YY')
    from a string date column ('YYYY-MM-DD...')."""
    year = func.substr(col, 1, 4)
    year_int = func.cast(year, Integer)
    return case(
        (
            func.substr(col, 6, 2).in_(("04", "05", "06", "07", "08", "09", "10", "11", "12")),
            year + "-" + func.substr(func.cast(year_int + 1, SqlText), 3, 2),
        ),
        else_=func.cast(year_int - 1, SqlText) + "-" + func.substr(col, 3, 2),
    )


def ledger_totals(db: Session, house: Optional[str] = None) -> Dict[str, Any]:
    """Portfolio-wide expenditure + completed totals from the ledgers,
    optionally scoped to one house (ledgers carry a house column).
    Completed counts/amounts count only snapshot rows (source='esakshi')
    so they match the official metric set; the expenditure ledger is
    already an exact snapshot partition (no legacy rows exist there)."""
    exp_q = db.query(
        func.coalesce(func.sum(models.Expenditure.expenditure_amount), 0.0).label("amount"),
        func.count(models.Expenditure.id).label("transactions"),
    )
    comp_q = db.query(
        func.coalesce(func.sum(models.CompletedWork.final_amount), 0.0).label("amount"),
        func.count(models.CompletedWork.id).label("works"),
    ).filter(models.CompletedWork.source == "esakshi")
    if house:
        exp_q = exp_q.filter(models.Expenditure.house == house)
        comp_q = comp_q.filter(models.CompletedWork.house == house)
    exp = exp_q.one()
    comp = comp_q.one()
    return {
        "total_expenditure": float(exp.amount or 0),
        "expenditure_transactions": int(exp.transactions or 0),
        "completed_works": int(comp.works or 0),
        "completed_works_amount": float(comp.amount or 0),
    }


def ledger_totals_scoped(
    db: Session,
    state: Optional[str] = None,
    fy: Optional[str] = None,
    house: Optional[str] = None,
) -> Dict[str, Any]:
    """Ledger totals, optionally scoped by state (ledger's own state
    column), financial year (payment/completion date, Indian FY) and/or
    house (ledger's own house column)."""
    exp_q = db.query(
        func.coalesce(func.sum(models.Expenditure.expenditure_amount), 0.0).label("amount"),
        func.count(models.Expenditure.id).label("transactions"),
    )
    if state:
        exp_q = exp_q.filter(models.Expenditure.state == state)
    if fy:
        exp_q = exp_q.filter(_fy_expr(models.Expenditure.expenditure_date) == fy)
    if house:
        exp_q = exp_q.filter(models.Expenditure.house == house)
    exp = exp_q.one()

    comp_q = db.query(
        func.coalesce(func.sum(models.CompletedWork.final_amount), 0.0).label("amount"),
        func.count(models.CompletedWork.id).label("works"),
    ).filter(models.CompletedWork.source == "esakshi")
    if state:
        comp_q = comp_q.filter(models.CompletedWork.state == state)
    if fy:
        comp_q = comp_q.filter(_fy_expr(models.CompletedWork.completed_date) == fy)
    if house:
        comp_q = comp_q.filter(models.CompletedWork.house == house)
    comp = comp_q.one()

    return {
        "total_expenditure": float(exp.amount or 0),
        "expenditure_transactions": int(exp.transactions or 0),
        "completed_works": int(comp.works or 0),
        "completed_works_amount": float(comp.amount or 0),
    }


def _total_project_amount(db: Session, house: Optional[str] = None) -> float:
    """Sum of sanctioned amounts across the project catalog (house-scoped;
    None = All). This is the project-monitoring "Total Project Amount" —
    distinct from the official "Total Allocated" (MP entitlements)."""
    q = db.query(func.coalesce(func.sum(models.Project.sanctioned_amount), 0.0))
    if house:
        q = q.filter(models.Project.house == house)
    return float(q.scalar() or 0.0)


def house_dashboard(db: Session, house: str, fy: Optional[str] = None) -> Dict[str, Any]:
    """Official eSAKSHI-style per-house dashboard metrics.

    Reference-first: the `reference_dashboard_metrics` table holds the
    official eSAKSHI values for the snapshot date as backend data (RS
    transcribed from the official dashboard, LS derived from the same
    batch's CSVs). When an FY filter is absent AND the reference row for
    this house exists, those values are returned verbatim with provenance
    so the UI always matches the official snapshot. With an FY filter (or
    a missing reference row) the metrics are COMPUTED from the snapshot
    partitions (source='esakshi' rows only — legacy GitHub-provider and
    dropped works are excluded), with the same definitions:

    - total_allocated       SUM(mp_summaries.allocated_amount) for the house
                            — "total funds allocated to MPs".
    - total_expenditure     SUM(expenditures.expenditure_amount) for the house
                            — official definition: "total vendor payments
                            released against completed/ongoing works".
    - fund_utilization      recommended_amount / allocated — official caption:
                            "share of allocation recommended by MPs".
    - expenditure_rate      expenditure / allocated — official caption:
                            "vendor expenditure recorded as a share of
                            allocation".
    - total_mps             COUNT(mp_summaries rows) for the house.
    - works_completed       COUNT(completed_works rows) for the house.
    - completed_work_value  SUM(completed_works.final_amount).
    - works_pending         recommended_works rows − completed_works rows
                            (the official portal's pending = recommended −
                            completed; our two ledgers share no work keys, so
                            per-work netting is impossible — see README).
    - ongoing_payments      expenditure − completed value — official caption:
                            "vendor expenditure linked to works not yet marked
                            complete".

    FY note: the ledgers carry absolute payment/completion dates, not MP
    tenure; an FY filter scopes payments by Indian FY of the payment date.
    """
    # ── Reference-first path (no FY filter): serve the stored snapshot. ──
    # "All" combines the two per-house reference rows: amounts and counts are
    # summed (the houses are disjoint populations — no double-counting), the
    # two percentage rates are recomputed allocation-weighted (a simple mean
    # would be wrong), and pending/ongoing sums follow their definitions.
    if not fy:
        try:
            if house == "All":
                refs = db.execute(text("SELECT * FROM reference_dashboard_metrics")).mappings().all()
            else:
                ref = db.execute(
                    text("SELECT * FROM reference_dashboard_metrics WHERE house = :h"),
                    {"h": house},
                ).mappings().first()
                refs = [ref] if ref else []
        except Exception:
            refs = []
        if refs:
            alloc = sum(float(r["total_allocated"] or 0) for r in refs)
            exp_amt = sum(float(r["total_expenditure"] or 0) for r in refs)
            return {
                "house": house,
                "total_allocated": alloc,
                "total_expenditure": exp_amt,
                "expenditure_transactions": None,
                "fund_utilization_percentage": (
                    round(sum(float(r["fund_utilization_percentage"] or 0) * float(r["total_allocated"] or 0) for r in refs) / alloc, 2)
                    if alloc > 0 else 0.0
                ),
                "expenditure_rate_percentage": (
                    round(sum(float(r["expenditure_rate_percentage"] or 0) * float(r["total_allocated"] or 0) for r in refs) / alloc, 2)
                    if alloc > 0 else 0.0
                ),
                "total_mps": sum(int(r["total_mps"] or 0) for r in refs),
                "works_completed": sum(int(r["works_completed"] or 0) for r in refs),
                "completed_work_value": sum(float(r["completed_work_value"] or 0) for r in refs),
                "works_recommended": None,
                "recommended_amount": None,
                "works_pending": sum(int(r["works_pending"] or 0) for r in refs),
                "ongoing_work_payments": sum(float(r["ongoing_work_payments"] or 0) for r in refs),
                "total_project_amount": _total_project_amount(db, None if house == "All" else house),
                "data_snapshot": {
                    "last_payment_date": None,
                    "last_completion_date": None,
                    "last_recommendation_date": None,
                    "source": "; ".join(sorted({str(r["source"]) for r in refs})),
                    "label": (
                        "Combined Rajya Sabha + Lok Sabha official eSAKSHI snapshot"
                        if house == "All" else refs[0]["snapshot_label"]
                    ),
                    "reference": True,
                },
            }

    # ── Computed path (FY-filtered or missing reference row). ──
    # house == "All" removes the house constraint instead of filtering for a
    # literal 'All' value; ledgers carry only 'Lok Sabha'/'Rajya Sabha'.
    house_scope = None if house == "All" else house
    alloc_q = db.query(func.coalesce(func.sum(models.MPSummary.allocated_amount), 0.0))
    mps_q = db.query(func.count(models.MPSummary.id))
    if house_scope:
        alloc_q = alloc_q.filter(models.MPSummary.house == house_scope)
        mps_q = mps_q.filter(models.MPSummary.house == house_scope)
    alloc = alloc_q.scalar() or 0.0
    mps = mps_q.scalar() or 0

    exp_q = db.query(
        func.coalesce(func.sum(models.Expenditure.expenditure_amount), 0.0),
        func.count(models.Expenditure.id),
    )
    rec_q = db.query(
        func.coalesce(func.sum(models.RecommendedWork.recommended_amount), 0.0),
        func.count(models.RecommendedWork.id),
    ).filter(models.RecommendedWork.source == "esakshi")
    comp_q = db.query(
        func.coalesce(func.sum(models.CompletedWork.final_amount), 0.0),
        func.count(models.CompletedWork.id),
    ).filter(models.CompletedWork.source == "esakshi")
    if house_scope:
        exp_q = exp_q.filter(models.Expenditure.house == house_scope)
        rec_q = rec_q.filter(models.RecommendedWork.house == house_scope)
        comp_q = comp_q.filter(models.CompletedWork.house == house_scope)

    if fy:
        exp_q = exp_q.filter(_fy_expr(models.Expenditure.expenditure_date) == fy)
        rec_q = rec_q.filter(_fy_expr(models.RecommendedWork.recommendation_date) == fy)
        comp_q = comp_q.filter(_fy_expr(models.CompletedWork.completed_date) == fy)

    exp_amt, exp_n = exp_q.one()
    rec_amt, rec_n = rec_q.one()
    comp_amt, comp_n = comp_q.one()

    exp_amt = float(exp_amt or 0)
    rec_amt = float(rec_amt or 0)
    comp_amt = float(comp_amt or 0)

    fund_utilization = (rec_amt / alloc * 100) if alloc > 0 else 0.0
    expenditure_rate = (exp_amt / alloc * 100) if alloc > 0 else 0.0

    # Dataset snapshot dates (max observed event per ledger, house-scoped).
    last_payment_q = db.query(func.max(models.Expenditure.expenditure_date))
    last_completion_q = db.query(func.max(models.CompletedWork.completed_date))
    last_recommendation_q = db.query(
        func.max(models.RecommendedWork.recommendation_date)
    ).filter(models.RecommendedWork.source == "esakshi")
    if house_scope:
        last_payment_q = last_payment_q.filter(models.Expenditure.house == house_scope)
        last_completion_q = last_completion_q.filter(models.CompletedWork.house == house_scope)
        last_recommendation_q = last_recommendation_q.filter(models.RecommendedWork.house == house_scope)
    last_payment = last_payment_q.scalar()
    last_completion = last_completion_q.scalar()
    last_recommendation = last_recommendation_q.scalar()

    return {
        "house": house,
        "total_allocated": alloc,
        "total_expenditure": exp_amt,
        "expenditure_transactions": int(exp_n or 0),
        "fund_utilization_percentage": round(fund_utilization, 2),
        "expenditure_rate_percentage": round(expenditure_rate, 2),
        "total_mps": int(mps),
        "works_completed": int(comp_n or 0),
        "completed_work_value": comp_amt,
        "works_recommended": int(rec_n or 0),
        "recommended_amount": rec_amt,
        "works_pending": max(int(rec_n or 0) - int(comp_n or 0), 0),
        "ongoing_work_payments": max(exp_amt - comp_amt, 0.0),
        "total_project_amount": _total_project_amount(db, None if house == "All" else house),
        "data_snapshot": {
            "last_payment_date": last_payment,
            "last_completion_date": last_completion,
            "last_recommendation_date": last_recommendation,
            "source": "MPLADS eSAKSHI dataset import (batch snapshot, not a live feed)",
            "reference": False,
        },
    }


def portfolio_overview(
    db: Session, fy: Optional[str] = None, house: Optional[str] = None
) -> Dict[str, Any]:
    """The canonical dashboard-overview payload.

    projects       = project catalog rows (optionally FY/house-filtered by
                     the catalog's own fy/house columns; house NULL rows are
                     excluded only when a specific house is requested)
    sanctioned     = SUM(projects.sanctioned_amount) — real recorded values
    expenditure    = SUM from the payment ledger (house-scoped via the
                     ledger's own house column; FY-scoped by payment date
                     when a FY filter is given)
    completed      = count from the completions ledger (same scoping rule)
    utilization    = expenditure / sanctioned * 100 (unrounded)
    """
    q = db.query(
        func.count(models.Project.id).label("total_projects"),
        func.coalesce(func.sum(models.Project.sanctioned_amount), 0.0).label("total_sanctioned"),
        func.coalesce(func.avg(models.Project.completion_percentage), 0.0).label("avg_completion"),
        func.count(func.distinct(models.Project.state)).label("total_states"),
        func.sum(case((func.lower(models.Project.status) == "completed", 1), else_=0)).label("completed_projects"),
        func.sum(case((func.lower(models.Project.status) == "ongoing", 1), else_=0)).label("ongoing_projects"),
        func.sum(case((func.lower(models.Project.status) == "recommended", 1), else_=0)).label("recommended_works"),
    )
    if fy:
        q = q.filter(models.Project.fy == fy)
    if house in ("Lok Sabha", "Rajya Sabha"):
        q = q.filter(models.Project.house == house)
    stats = q.one()

    mp_q = db.query(func.count(models.MPSummary.id))
    if house in ("Lok Sabha", "Rajya Sabha"):
        mp_q = mp_q.filter(models.MPSummary.house == house)
    total_mps = mp_q.scalar() or 0

    led = ledger_totals_scoped(db, fy=fy, house=house)

    total_sanctioned = float(stats.total_sanctioned or 0)
    total_expenditure = float(led["total_expenditure"])
    utilization = (total_expenditure / total_sanctioned * 100) if total_sanctioned > 0 else 0.0

    return {
        "total_projects": int(stats.total_projects or 0),
        "completed_projects": int(led["completed_works"]),
        "ongoing_projects": int(stats.ongoing_projects or 0),
        "total_allocated_amount": total_sanctioned,
        "total_sanctioned_amount": total_sanctioned,
        "total_expenditure": total_expenditure,
        "expenditure_transactions": led["expenditure_transactions"],
        "completed_works_amount": led["completed_works_amount"],
        "utilization_percentage": utilization,
        "average_completion_percentage": round(float(stats.avg_completion or 0), 2),
        "total_mps": total_mps,
        "total_states": int(stats.total_states or 0),
        "recommended_works": int(stats.recommended_works or 0),
        "expenditure_scope": "ledger_direct",
        "completed_scope": "ledger_direct",
        "house": house if house in ("Lok Sabha", "Rajya Sabha") else "All",
    }


def portfolio_stats(
    db: Session,
    fy: Optional[str] = None,
    state: Optional[str] = None,
    house: Optional[str] = None,
) -> Dict[str, Any]:
    """Catalog counts/sanctioned + ledger expenditure/completions, optionally
    scoped by catalog fy / catalog state / catalog house (ledgers scoped by
    their own state, payment/completion-date FY and house)."""
    q = db.query(
        func.count(models.Project.id).label("total"),
        func.coalesce(func.sum(models.Project.sanctioned_amount), 0.0).label("sanctioned"),
        func.coalesce(func.avg(models.Project.completion_percentage), 0.0).label("avg_completion"),
        func.sum(case((func.lower(models.Project.status) == "ongoing", 1), else_=0)).label("ongoing"),
    )
    if fy:
        q = q.filter(models.Project.fy == fy)
    if state:
        q = q.filter(models.Project.state == state)
    if house in ("Lok Sabha", "Rajya Sabha"):
        q = q.filter(models.Project.house == house)
    s = q.one()
    led = ledger_totals_scoped(db, state=state, fy=fy, house=house)
    return {
        "total_projects": int(s.total or 0),
        "sanctioned": float(s.sanctioned or 0),
        "avg_completion": float(s.avg_completion or 0),
        "ongoing_projects": int(s.ongoing or 0),
        "expenditure": led["total_expenditure"],
        "expenditure_transactions": led["expenditure_transactions"],
        "completed_works": led["completed_works"],
        "completed_works_amount": led["completed_works_amount"],
    }


def resolve_project_expenditure(db: Session, project) -> float:
    """THE authoritative per-project expenditure resolver.

    Every consumer that needs "this project's spend" must go through this
    function (or project_expenditure_map / _rec_info_map which read the
    SAME derived rows): the payment-ledger total linked to the project's
    work key via the conservative MP+IDA-verified linkage, persisted in
    project_rec_info.linked_expenditure by rec_info.rebuild().

    The catalog's projects.expenditure column is a zeroed legacy stamp and
    must NEVER be read for spend. Returns 0.0 when no ledger records are
    linked (an "unlinked / no record" case, not a verified ₹0 — callers
    that need the distinction use expenditure_linkage_summary).
    """
    row = (
        db.query(models.ProjectRecInfo.linked_expenditure)
        .filter(models.ProjectRecInfo.project_id == project.id)
        .one_or_none()
    )
    return float(row[0] or 0.0) if row else 0.0


def project_expenditure_map(db: Session) -> Dict[int, float]:
    """project_id -> SUM(expenditure_amount) for ledger rows linked to the
    project via the conservative MP+IDA-verified linkage.

    Reads the SAME persisted rows as resolve_project_expenditure
    (project_rec_info.linked_expenditure) — one indexed table scan instead
    of recomputing the normkey join over 106k ledger rows per request.
    If the derived table has not been built yet (brand-new database), one
    rebuild is attempted first so the map is never silently empty.

    Bounded by the catalog: only work keys shared between the payment
    ledger and the recommended-works catalog can match (~300 projects in
    this dataset), so this stays a small ID dict, not a dataset copy.

    The catalog's per-project `expenditure` column is a legacy stamp that
    was zeroed by a documented startup repair after it was proven to be
    group-aggregate duplication of this same ledger — never aggregate it.
    """
    rows = db.query(
        models.ProjectRecInfo.project_id,
        models.ProjectRecInfo.linked_expenditure,
    ).filter(models.ProjectRecInfo.linked_expenditure > 0).all()
    if not rows:
        try:
            import rec_info as _recinfo
            if _recinfo.rebuild(db).get("ok"):
                rows = (
                    db.query(
                        models.ProjectRecInfo.project_id,
                        models.ProjectRecInfo.linked_expenditure,
                    )
                    .filter(models.ProjectRecInfo.linked_expenditure > 0)
                    .all()
                )
        except Exception:
            pass  # derived table unavailable; empty map is the honest result
    return {int(pid): float(amt or 0.0) for pid, amt in rows if (amt or 0.0) > 0}


def expenditure_linkage_summary(
    db: Session, project_ids: List[int]
) -> Dict[int, Dict[str, Any]]:
    """project_id -> {linked, transactions, first/latest date, total}.

    Reads the SAME derived rows as resolve_project_expenditure —
    project_rec_info persists the tx count and first/latest payment dates
    of the MP+IDA-verified linkage, so this is one indexed query per page
    with no recomputation. `linked` False means no ledger payments passed
    the conservative verification for this project's work key — the honest
    status is "no verifiably-linked expenditure records", never a claimed
    ₹0 spend.
    """
    out: Dict[int, Dict[str, Any]] = {}
    if not project_ids:
        return out
    rows = (
        db.query(
            models.ProjectRecInfo.project_id,
            models.ProjectRecInfo.has_expenditure,
            models.ProjectRecInfo.linked_expenditure,
            models.ProjectRecInfo.linked_tx_count,
            models.ProjectRecInfo.first_expenditure_date,
            models.ProjectRecInfo.latest_expenditure_date,
            models.ProjectRecInfo.work_key_expenditure,
            models.ProjectRecInfo.work_key_rows,
        )
        .filter(models.ProjectRecInfo.project_id.in_(project_ids))
        .all()
    )
    for r in rows:
        tx = int(r.linked_tx_count or 0)
        wk_rows = int(r.work_key_rows or 1)
        linked_amt = float(r.linked_expenditure or 0.0)
        if linked_amt > 0:
            status = "linked"
        elif r.has_expenditure and tx > 0 and wk_rows > 1:
            status = "ambiguous_shared_work"
        else:
            status = "no_linked_records"
        out[r.project_id] = {
            "linked": status == "linked",
            "status": status,
            "transactions": tx,
            "first_expenditure_date": r.first_expenditure_date or None,
            "latest_expenditure_date": r.latest_expenditure_date or None,
            "total": linked_amt,
            "work_key_expenditure": float(r.work_key_expenditure or 0.0),
            "work_key_rows": wk_rows,
        }
    # projects with no rec row at all (derived table not yet built)
    for pid in project_ids:
        out.setdefault(
            pid,
            {"linked": False, "status": "no_linked_records", "transactions": 0,
             "first_expenditure_date": None, "latest_expenditure_date": None,
             "total": 0.0, "work_key_expenditure": 0.0, "work_key_rows": 1},
        )
    return out


def state_linked_map(db: Session, fy: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """state -> verified project-side expenditure, aggregated per WORK KEY.

    Two tiers, both deduplicated by work key (several catalog rows can
    describe the same work — summing rows would double-count):
      • linked_total          — attributable: unique-key works only
                               (one catalog row per work key)
      • work_verified_total   — all MP+IDA-verified work keys, including
                               shared ones whose per-row split is ambiguous
    Each key's state comes from its first catalog row.
    """
    params: Dict[str, Any] = {}
    fy_join = ""
    if fy:
        fy_join = " JOIN projects pf ON pf.id = pid AND pf.fy = :fy"
        params["fy"] = fy
    sql = f"""
        SELECT p.state AS st,
               SUM(kd.amt)      AS linked_total,
               SUM(kd.wamt)     AS work_verified_total,
               SUM(kd.rows_)    AS linked_rows,
               COUNT(*)         AS linked_keys,
               SUM(CASE WHEN kd.rows_ = 1 THEN 1 ELSE 0 END) AS unique_keys
        FROM (
            SELECT pri.work_key AS k,
                   MAX(pri.linked_expenditure)    AS amt,
                   MAX(pri.work_key_expenditure)  AS wamt,
                   COUNT(*) AS rows_,
                   MIN(pri.project_id) AS pid
            FROM project_rec_info pri
            WHERE pri.work_key_expenditure > 0
            GROUP BY pri.work_key
        ) kd
        JOIN projects p ON p.id = kd.pid{fy_join}
        GROUP BY p.state
    """
    out: Dict[str, Dict[str, Any]] = {}
    for r in db.execute(text(sql), params).fetchall():
        out[r.st or "Unknown"] = {
            "linked_total": float(r.linked_total or 0),
            "work_verified_total": float(r.work_verified_total or 0),
            "linked_rows": int(r.linked_rows or 0),
            "linked_keys": int(r.linked_keys or 0),
            "unique_keys": int(r.unique_keys or 0),
        }
    return out


def state_expenditure_reconciliation(db: Session, fy: Optional[str] = None) -> Dict[str, Any]:
    """Reconcile State Intelligence's per-state ledger totals against the
    SUM of per-project resolved (MP+IDA-verified linked) expenditure.

    These are DIFFERENT quantities by dataset design and must never be
    forced equal:
      • ledger side    — every payment row whose state column says X
      • project side   — only payments whose work key + MP + IDA verifiably
                         match a catalog project of state X
    The gap is unattributable spend (generic work descriptions that do not
    match any catalog row, or verification anchors that fail). This endpoint
    reports the gap so the UI can disclose it instead of hiding it.
    """
    led = state_ledger_map(db, fy=fy)
    linked = state_linked_map(db, fy=fy)
    proj_q = db.query(
        models.Project.state,
        func.count(models.Project.id),
    ).group_by(models.Project.state)
    if fy:
        proj_q = proj_q.filter(models.Project.fy == fy)
    per_state = {
        (s or "Unknown"): {"catalog_projects": int(c or 0)}
        for s, c in proj_q.all()
    }
    states = sorted(set(led.keys()) | set(per_state.keys()) | set(linked.keys()))
    rows = []
    for st in states:
        ledger_total = float(led.get(st, {}).get("expenditure", 0.0))
        lk = linked.get(st, {"linked_total": 0.0, "work_verified_total": 0.0,
                             "linked_rows": 0, "linked_keys": 0, "unique_keys": 0})
        rows.append({
            "state": st,
            "ledger_expenditure": ledger_total,
            "ledger_transactions": int(led.get(st, {}).get("transactions", 0)),
            "project_linked_expenditure": lk["linked_total"],
            "work_verified_expenditure": lk["work_verified_total"],
            "linked_work_keys": lk["linked_keys"],
            "unique_work_keys": lk["unique_keys"],
            "catalog_rows_with_linked_expenditure": lk["linked_rows"],
            "catalog_projects": per_state.get(st, {}).get("catalog_projects", 0),
            "unattributed_expenditure": round(ledger_total - lk["work_verified_total"], 2),
            "ambiguous_shared_work_expenditure": round(lk["work_verified_total"] - lk["linked_total"], 2),
        })
    return {
        "scope": fy or "all",
        "method": (
            "ledger_expenditure = payment ledger grouped by its own state column; "
            "work_verified_expenditure = MP+IDA-verified ledger payments whose work "
            "key matches catalog projects (deduplicated by work key); "
            "project_linked_expenditure = the attributable subset (unique-key works "
            "only). The remainder is spend that cannot be verifiably attributed to "
            "individual catalog projects — a dataset linkage limitation, reported "
            "honestly rather than redistributed."
        ),
        "totals": {
            "ledger_expenditure": round(sum(r["ledger_expenditure"] for r in rows), 2),
            "work_verified_expenditure": round(sum(r["work_verified_expenditure"] for r in rows), 2),
            "project_linked_expenditure": round(sum(r["project_linked_expenditure"] for r in rows), 2),
            "unattributed_expenditure": round(sum(r["unattributed_expenditure"] for r in rows), 2),
        },
        "states": rows,
    }


def state_ledger_map(
    db: Session, fy: Optional[str] = None, house: Optional[str] = None
) -> Dict[str, Dict[str, Any]]:
    """state -> ledger totals (expenditure by the ledger's own state column,
    completions by the completions ledger's state column), optionally
    house-scoped via each ledger's own house column."""
    out: Dict[str, Dict[str, Any]] = {}

    eq = db.query(
        models.Expenditure.state.label("g"),
        func.coalesce(func.sum(models.Expenditure.expenditure_amount), 0.0).label("expenditure"),
        func.count(models.Expenditure.id).label("transactions"),
    )
    if fy:
        eq = eq.filter(_fy_expr(models.Expenditure.expenditure_date) == fy)
    if house in ("Lok Sabha", "Rajya Sabha"):
        eq = eq.filter(models.Expenditure.house == house)
    for r in eq.group_by("g").all():
        entry = out.setdefault(
            r.g or "Unknown",
            {"expenditure": 0.0, "transactions": 0, "completed": 0, "completed_amount": 0.0},
        )
        entry["expenditure"] += float(r.expenditure or 0)
        entry["transactions"] += int(r.transactions or 0)

    cq = db.query(
        models.CompletedWork.state.label("g"),
        func.count(models.CompletedWork.id).label("works"),
        func.coalesce(func.sum(models.CompletedWork.final_amount), 0.0).label("amount"),
    )
    if fy:
        cq = cq.filter(_fy_expr(models.CompletedWork.completed_date) == fy)
    if house in ("Lok Sabha", "Rajya Sabha"):
        cq = cq.filter(models.CompletedWork.house == house)
    for r in cq.group_by("g").all():
        entry = out.setdefault(
            r.g or "Unknown",
            {"expenditure": 0.0, "transactions": 0, "completed": 0, "completed_amount": 0.0},
        )
        entry["completed"] += int(r.works or 0)
        entry["completed_amount"] += float(r.amount or 0)
    return out


def constituency_ledger_map(
    db: Session,
    fy: Optional[str] = None,
    house: Optional[str] = None,
) -> Dict[tuple, Dict[str, Any]]:
    """(state, constituency) -> ledger totals.

    Keyed by the (state, constituency) PAIR: shared labels like
    'Sitting Rajya Sabha' exist in many states, so a constituency-only key
    would double-attribute ledger spend to every state holding the label.
    Verified: 100% of ledger (state, constituency) pairs exist in the
    project catalog with identical strings.
    """
    out: Dict[tuple, Dict[str, Any]] = {}

    eq = db.query(
        models.Expenditure.state.label("s"),
        models.Expenditure.constituency.label("g"),
        func.coalesce(func.sum(models.Expenditure.expenditure_amount), 0.0).label("expenditure"),
        func.count(models.Expenditure.id).label("transactions"),
    )
    if fy:
        eq = eq.filter(_fy_expr(models.Expenditure.expenditure_date) == fy)
    if house in ("Lok Sabha", "Rajya Sabha"):
        eq = eq.filter(models.Expenditure.house == house)
    for r in eq.group_by("s", "g").all():
        entry = out.setdefault(
            ((r.s or "").strip(), (r.g or "Unknown").strip()),
            {"expenditure": 0.0, "transactions": 0, "completed": 0, "completed_amount": 0.0},
        )
        entry["expenditure"] += float(r.expenditure or 0)
        entry["transactions"] += int(r.transactions or 0)

    cq = db.query(
        models.CompletedWork.state.label("s"),
        models.CompletedWork.constituency.label("g"),
        func.count(models.CompletedWork.id).label("works"),
        func.coalesce(func.sum(models.CompletedWork.final_amount), 0.0).label("amount"),
    )
    if fy:
        cq = cq.filter(_fy_expr(models.CompletedWork.completed_date) == fy)
    if house in ("Lok Sabha", "Rajya Sabha"):
        cq = cq.filter(models.CompletedWork.house == house)
    for r in cq.group_by("s", "g").all():
        entry = out.setdefault(
            ((r.s or "").strip(), (r.g or "Unknown").strip()),
            {"expenditure": 0.0, "transactions": 0, "completed": 0, "completed_amount": 0.0},
        )
        entry["completed"] += int(r.works or 0)
        entry["completed_amount"] += float(r.amount or 0)
    return out


def fy_trends(db: Session, house: Optional[str] = None) -> list:
    """Per-FY trends. Project counts + sanctioned come from the project
    catalog grouped by its own fy column. Expenditure and completed
    counts come from the ledgers grouped by payment/completion-date FY.
    `utilization_percentage` uses the catalog sanctioned vs the ledger
    expenditure of the same FY. Catalog FY values with no ledger rows
    still appear (expenditure 0.0). Returned sorted by fy ascending,
    'Unknown' (NULL fy) last. `house` scopes every side to one house
    (catalog's own column; ledgers' own house columns)."""
    # Catalog side: counts + sanctioned by fy
    cat_q = db.query(
        models.Project.fy.label("fy"),
        func.count(models.Project.id).label("projects"),
        func.coalesce(func.sum(models.Project.sanctioned_amount), 0.0).label("sanctioned"),
    )
    if house:
        cat_q = cat_q.filter(models.Project.house == house)
    cat_rows = cat_q.group_by(models.Project.fy).all()
    # Ledger side: expenditure by payment-date FY
    exp_q = db.query(
        _fy_expr(models.Expenditure.expenditure_date).label("fy"),
        func.coalesce(func.sum(models.Expenditure.expenditure_amount), 0.0).label("expenditure"),
        func.count(models.Expenditure.id).label("transactions"),
    )
    if house:
        exp_q = exp_q.filter(models.Expenditure.house == house)
    exp_rows = exp_q.group_by("fy").all()
    # Completions ledger by completion-date FY (snapshot rows only —
    # legacy GitHub rows would inflate the official trend set)
    comp_q = db.query(
        _fy_expr(models.CompletedWork.completed_date).label("fy"),
        func.count(models.CompletedWork.id).label("works"),
        func.coalesce(func.sum(models.CompletedWork.final_amount), 0.0).label("amount"),
    ).filter(models.CompletedWork.source == "esakshi")
    if house:
        comp_q = comp_q.filter(models.CompletedWork.house == house)
    comp_rows = comp_q.group_by("fy").all()

    fy_order: Dict[str, Tuple[int, str]] = {}

    def _rank(v: Optional[str]) -> Tuple[int, str]:
        if not v:
            return (1, "")
        return (0, v)

    by_fy: Dict[str, Dict[str, Any]] = {}
    for r in cat_rows:
        key = r.fy or "Unknown"
        entry = by_fy.setdefault(
            key,
            {"fy": key, "projects": 0, "sanctioned": 0.0, "expenditure": 0.0,
             "transactions": 0, "completed": 0, "avg_risk": None},
        )
        entry["projects"] += int(r.projects or 0)
        entry["sanctioned"] += float(r.sanctioned or 0)
    for r in exp_rows:
        key = (r.fy or "Unknown") if isinstance(r.fy, str) else "Unknown"
        entry = by_fy.setdefault(
            key,
            {"fy": key, "projects": 0, "sanctioned": 0.0, "expenditure": 0.0,
             "transactions": 0, "completed": 0, "avg_risk": None},
        )
        entry["expenditure"] += float(r.expenditure or 0)
        entry["transactions"] += int(r.transactions or 0)
    for r in comp_rows:
        key = (r.fy or "Unknown") if isinstance(r.fy, str) else "Unknown"
        entry = by_fy.setdefault(
            key,
            {"fy": key, "projects": 0, "sanctioned": 0.0, "expenditure": 0.0,
             "transactions": 0, "completed": 0, "avg_risk": None},
        )
        entry["completed"] += int(r.works or 0)
        entry["completed_amount"] = entry.get("completed_amount", 0.0) + float(r.amount or 0)

    # Avg risk per catalog FY (existing risk_scores joined via project fy)
    risk_rows = (
        db.query(
            models.Project.fy.label("fy"),
            func.avg(models.RiskScore.risk_score).label("avg_risk"),
        )
        .join(models.RiskScore, models.RiskScore.project_id == models.Project.id)
        .group_by(models.Project.fy)
        .all()
    )
    for r in risk_rows:
        key = r.fy or "Unknown"
        if key in by_fy:
            by_fy[key]["avg_risk"] = round(float(r.avg_risk), 1) if r.avg_risk is not None else None

    out = []
    for key, e in by_fy.items():
        util = (e["expenditure"] / e["sanctioned"] * 100) if e["sanctioned"] > 0 else None
        out.append({
            "fy": key,
            "projects": e["projects"],
            "sanctioned": e["sanctioned"],
            "expenditure": e["expenditure"],
            "transactions": e["transactions"],
            "completed": e["completed"],
            "completed_amount": e.get("completed_amount", 0.0),
            "utilization_percentage": util,
            "avg_risk": e["avg_risk"],
        })
    out.sort(key=lambda x: (_rank(x["fy"] if x["fy"] != "Unknown" else None), x["fy"]))
    return out
