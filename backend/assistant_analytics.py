"""
MPLADS AI — dataset analytics engine
====================================

Answers portfolio-level analytical questions from targeted SQL. Every branch:

  • declares the metric definition and its data source,
  • applies House / state / FY scope only where the underlying tables support it,
  • returns the records behind a count when the user asked to see them,
  • reports unavailable data as unavailable, never as zero.

No arbitrary SQL is accepted: branches are fixed queries, and the only values
interpolated are validated model columns and bound parameters. Results are
bounded (LIMIT 10) and aggregated in SQL — the dataset is never loaded into
Python wholesale.

Return contract (understood by assistant._answer_body):
    (answer, actions, discussed_pid, ctx_update, meta)
"""

import re
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import case, func, text
from sqlalchemy.orm import Session

import auth
import metrics
import models


# ── helpers ──────────────────────────────────────────────────────────

def _action(**kwargs):
    from assistant import ActionOut
    return ActionOut(**kwargs)


def _money(value) -> str:
    value = float(value or 0)
    if value >= 1e7:
        return f"₹{value / 1e7:,.2f} Cr"
    if value >= 1e5:
        return f"₹{value / 1e5:,.2f} L"
    return f"₹{value:,.0f}"


def _pct(part, whole) -> str:
    try:
        part, whole = float(part or 0), float(whole or 0)
    except (TypeError, ValueError):
        return "—"
    return f"{(part / whole * 100):.1f}%" if whole else "—"


def _house(question: str) -> Optional[str]:
    match = re.search(r"\b(lok\s+sabha|rajya\s+sabha)\b", question or "", re.I)
    if not match:
        return None
    return "Lok Sabha" if match.group(1).lower().startswith("lok") else "Rajya Sabha"


def _state(question: str, db: Session) -> Optional[str]:
    """Resolve a state phrase against the real state list."""
    rows = db.query(models.Project.state).distinct().all()
    states = {str(s).strip().lower(): str(s).strip() for (s,) in rows if s and str(s).strip()}
    candidates: List[str] = []
    match = re.search(
        r"\b(?:in|from|of|for)\s+([a-z][a-z .&']{2,40}?)(?:\?|$|,|\s+(?:show|which|what|how|list|give|tell|projects?|works?|state|constituenc))",
        question or "", re.I,
    )
    if match:
        candidates.append(match.group(1).strip(" ?.,").lower())
    for noun in re.finditer(r"\b(?:projects?|works?|constituenc(?:y|ies)|state)\b", question or "", re.I):
        prefix = (question or "")[:noun.start()].strip(" ,.?!:;-")
        words = re.findall(r"[a-z&'.]+", prefix, re.I)
        if words:
            candidates.append(words[-1].lower())
            if len(words) > 1:
                candidates.append(f"{words[-2].lower()} {words[-1].lower()}")
    for candidate in candidates:
        if candidate in states:
            return states[candidate]
    return None


def _fy(question: str, db: Session) -> Optional[str]:
    match = re.search(r"\b(20\d{2}\s*[-–]\s*\d{2,4})\b", question or "")
    if not match:
        return None
    wanted = re.sub(r"\s+", "", match.group(1)).replace("–", "-").replace("-", "")
    for (value,) in db.query(models.Project.fy).distinct().all():
        if value and str(value).replace("–", "-").replace("-", "") == wanted:
            return value
    return None


def _table(columns: List[str], rows: List[List[Any]], caption: str = "") -> Dict[str, Any]:
    return {
        "columns": columns,
        "rows": [[("" if cell is None else str(cell)) for cell in row] for row in rows],
        "caption": caption,
    }


def _scope_note(house: Optional[str], state: Optional[str], fy: Optional[str]) -> str:
    bits = [b for b in (house, state, f"FY {fy}" if fy else None) if b]
    return (" · " + " · ".join(bits)) if bits else ""


def _guest_response(meta: Optional[Dict] = None):
    # Never echo the computed analytics payload to a caller who is not
    # authorised to see it. Only the denial + its limitation leaves here.
    return (
        "Portfolio analytics are available to analysts, auditors and administrators. "
        "I can still help with MPLADS basics, public project lookups, navigation and "
        "Ground Verification.",
        [_action(label="Browse projects", action="navigate", target="Projects")],
        None,
        None,
        {
            "intent": "dataset_analytics",
            "limitations": ["This analytical answer requires analyst-tier access."],
        },
    )


# ── analytical branches ─────────────────────────────────────────────

def _states_mentioned(question: str, db: Session) -> List[str]:
    """Named states in order of appearance, resolved against the real list."""
    names = {str(s).strip() for (s,) in db.query(models.Project.state).distinct().all() if s}
    hits = []
    for name in sorted(names, key=len, reverse=True):
        for match in re.finditer(rf"\b{re.escape(name)}\b", question or "", re.I):
            hits.append((match.start(), name))
    hits.sort()
    out: List[str] = []
    covered = []
    for pos, name in hits:
        span = (pos, pos + len(name))
        if any(span[0] >= a and span[1] <= b for a, b in covered):
            continue
        covered.append(span)
        if name not in out:
            out.append(name)
    return out


def _state_comparison(ql: str, house, db: Session):
    """Two named states, side by side, using catalogue + ledger aggregates."""
    if not re.search(r"\bcompar(?:e|ison)\b|\bversus\b|\bvs\.?\b", ql):
        return None
    states = _states_mentioned(ql, db)
    if len(states) < 2:
        if re.search(r"\btwo\s+states?\b", ql):
            return (
                "Which two states should I compare? Name them, e.g. \u201ccompare expenditure "
                "between Telangana and Karnataka\u201d.",
                [], None, None,
                {"intent": "comparison", "limitations": ["Two named states are required."]},
            )
        return None
    states = states[:2]
    rows = []
    for state in states:
        project_q = db.query(
            func.count(models.Project.id),
            func.coalesce(func.sum(models.Project.sanctioned_amount), 0.0),
            func.coalesce(func.avg(models.Project.completion_percentage), 0.0),
        ).filter(models.Project.state == state)
        if house:
            project_q = project_q.filter(models.Project.house == house)
        total, sanctioned, avg_completion = project_q.one()
        exp_q = db.query(func.coalesce(func.sum(models.Expenditure.expenditure_amount), 0.0)).filter(
            func.lower(func.trim(models.Expenditure.state)) == state.lower()
        )
        if house:
            exp_q = exp_q.filter(models.Expenditure.house == house)
        expenditure = float(exp_q.scalar() or 0.0)
        high_q = db.query(func.count(models.RiskScore.id)).join(
            models.Project, models.Project.id == models.RiskScore.project_id
        ).filter(models.Project.state == state, models.RiskScore.risk_level.ilike("high"))
        if house:
            high_q = high_q.filter(models.Project.house == house)
        high = int(high_q.scalar() or 0)
        rows.append({
            "state": state, "projects": int(total or 0), "sanctioned": float(sanctioned or 0),
            "expenditure": expenditure, "avg_completion": float(avg_completion or 0),
            "high_risk": high,
        })
    table_rows = [[
        r["state"], f"{r['projects']:,}", _money(r["sanctioned"]), _money(r["expenditure"]),
        _pct(r["expenditure"], r["sanctioned"]), f"{r['avg_completion']:.1f}%", f"{r['high_risk']:,}",
    ] for r in rows]
    first, second = rows
    leader, lagger = (first, second) if first["expenditure"] >= second["expenditure"] else (second, first)
    answer = (
        f"**{leader['state']}** has higher recorded expenditure than **{lagger['state']}**"
        f"{_scope_note(house, None, None)}: {_money(leader['expenditure'])} vs {_money(lagger['expenditure'])} "
        f"(difference {_money(abs(leader['expenditure'] - lagger['expenditure']))}).\n\n"
        "Project counts and sanctioned amounts come from the project catalogue for each named state; "
        "expenditure sums payment-ledger rows whose state label matches (case-insensitive). "
        "The two sources can be scoped differently, so utilization is indicative."
    )
    return (
        answer,
        [
            _action(label=f"State Intelligence \u00b7 {first['state']}", action="navigate",
                    target="State Intelligence", params={"state": first["state"]}),
            _action(label=f"State Intelligence \u00b7 {second['state']}", action="navigate",
                    target="State Intelligence", params={"state": second["state"]}),
        ],
        None, None,
        {
            "intent": "comparison",
            "table": _table(
                ["State", "Projects", "Sanctioned", "Recorded expenditure", "Utilization", "Avg completion", "High-risk"],
                table_rows, f"{first['state']} vs {second['state']} \u2014 recorded aggregates",
            ),
            "limitations": [
                "Expenditure is attributed by the ledger's own state column, which is separate from the catalogue's state field.",
                "Projects with no state attribution are excluded.",
            ],
        },
    )


def _highest_expenditure_state(ql: str, house, fy, db: Session):
    if not re.search(
        r"(?:which|what)\s+states?.{0,45}(?:highest|most|largest|top).{0,30}(?:expenditure|spend|spending)"
        r"|states?.{0,40}(?:highest|most|largest).{0,25}(?:expenditure|spend)"
        r"|highest\s+(?:recorded\s+)?expenditure\s+(?:state|by\s+state)"
        r"|(?:expenditure|spend)\s+by\s+state",
        ql,
    ):
        return None
    query = db.query(
        models.Expenditure.state.label("state"),
        func.coalesce(func.sum(models.Expenditure.expenditure_amount), 0.0).label("amount"),
        func.count(models.Expenditure.id).label("transactions"),
    ).filter(models.Expenditure.state.isnot(None), func.trim(models.Expenditure.state) != "")
    if house:
        query = query.filter(models.Expenditure.house == house)
    if fy:
        query = query.filter(metrics._fy_expr(models.Expenditure.expenditure_date) == fy)
    rows = query.group_by(models.Expenditure.state).order_by(
        func.coalesce(func.sum(models.Expenditure.expenditure_amount), 0.0).desc()
    ).limit(10).all()
    if not rows:
        return ("No state-level expenditure rows are recorded for that scope.", [], None, None,
                {"intent": "dataset_analytics", "limitations": ["The ledger has no matching rows."]})

    sanctioned = {
        (state or "Unknown"): float(amount or 0)
        for state, amount in db.query(
            models.Project.state,
            func.coalesce(func.sum(models.Project.sanctioned_amount), 0.0),
        ).filter(models.Project.state.isnot(None)).group_by(models.Project.state).all()
    }
    total = float(db.query(func.coalesce(func.sum(models.Expenditure.expenditure_amount), 0.0)).scalar() or 0)
    table_rows = []
    for row in rows:
        state = row.state or "Unknown"
        amount = float(row.amount or 0)
        table_rows.append([
            state, _money(amount), _pct(amount, total), f"{int(row.transactions or 0):,}",
            _money(sanctioned.get(state, 0.0)),
            _pct(amount, sanctioned.get(state, 0.0)),
        ])
    top = rows[0]
    answer = (
        f"**{top.state}** has the highest recorded expenditure in the payment ledger{_scope_note(house, None, fy)}: "
        f"**{_money(top.amount)}** across {int(top.transactions or 0):,} transactions "
        f"({_pct(top.amount, total)} of all recorded spend in scope).\n\n"
        "Recorded expenditure sums payment-ledger rows grouped by the ledger's own state column. "
        "Sanctioned amounts come from the project catalogue, so the utilization column compares two "
        "different scopes and is indicative, not a reconciled ratio."
    )
    return (
        answer,
        [
            _action(label=f"State Intelligence · {top.state}", action="navigate",
                    target="State Intelligence", params={"state": top.state}),
            _action(label="Open State Intelligence", action="navigate", target="State Intelligence"),
        ],
        None,
        None,
        {
            "intent": "dataset_analytics",
            "entity": {"state": top.state},
            "table": _table(
                ["State", "Recorded expenditure", "Share", "Transactions", "Sanctioned (catalog)", "Indicative utilization"],
                table_rows,
                "Top states by recorded payment-ledger expenditure",
            ),
            "limitations": [
                "Ledger state labels and catalogue state labels are separate columns; the utilization column mixes them.",
                "Expenditure rows without a state are excluded from the ranking.",
            ],
        },
    )


def _incomplete_constituencies(ql: str, house, state, db: Session):
    if not re.search(
        r"constituenc(?:y|ies).{0,45}(?:most|highest|largest|top).{0,30}(?:incomplete|pending|unfinished|not\s+completed)"
        r"|(?:incomplete|pending|unfinished).{0,40}constituenc(?:y|ies)"
        r"|constituenc(?:y|ies).{0,30}(?:lowest|worst).{0,20}completion",
        ql,
    ):
        return None
    query = db.query(
        models.Project.state.label("state"),
        models.Project.constituency.label("constituency"),
        func.count(models.Project.id).label("total"),
        func.sum(case(
            (func.coalesce(func.lower(models.Project.status), "") != "completed", 1), else_=0
        )).label("incomplete"),
        func.sum(case(
            (func.coalesce(models.Project.completion_percentage, 0) < 100, 1), else_=0
        )).label("below_100"),
        func.coalesce(func.avg(models.Project.completion_percentage), 0.0).label("avg_completion"),
    ).filter(models.Project.constituency.isnot(None), func.trim(models.Project.constituency) != "")
    if house:
        query = query.filter(models.Project.house == house)
    if state:
        query = query.filter(models.Project.state == state)
    rows = query.group_by(models.Project.state, models.Project.constituency).having(
        func.count(models.Project.id) >= 10
    ).order_by(
        func.sum(case(
            (func.coalesce(func.lower(models.Project.status), "") != "completed", 1), else_=0
        )).desc()
    ).limit(10).all()
    if not rows:
        return ("No constituency has enough recorded projects in that scope to rank reliably.", [], None, None,
                {"intent": "dataset_analytics", "limitations": ["Minimum group size is 10 projects."]})

    table_rows = [[
        r.constituency, r.state or "—", f"{int(r.total or 0):,}",
        f"{int(r.incomplete or 0):,}", _pct(r.incomplete, r.total),
        f"{int(r.below_100 or 0):,}", f"{float(r.avg_completion or 0):.1f}%",
    ] for r in rows]
    top = rows[0]
    answer = (
        f"**{top.constituency} ({top.state})** has the most projects not recorded as Completed"
        f"{_scope_note(house, state, None)}: **{int(top.incomplete or 0):,} of {int(top.total or 0):,}** "
        f"({_pct(top.incomplete, top.total)}).\n\n"
        "“Incomplete” means the catalogue status is not “Completed”. The table also shows projects below "
        "100% reported physical completion, because status and completion are separate recorded fields "
        "and can disagree."
    )
    return (
        answer,
        [
            _action(label=f"Projects · {top.state}", action="navigate", target="Projects",
                    params={"state": top.state} if top.state else {}),
            _action(label="Open State Intelligence", action="navigate", target="State Intelligence"),
        ],
        None,
        None,
        {
            "intent": "dataset_analytics",
            "table": _table(
                ["Constituency", "State", "Projects", "Not Completed", "Incomplete share", "Below 100%", "Avg completion"],
                table_rows,
                "Constituencies ranked by projects not recorded as Completed (min. 10 projects)",
            ),
            "limitations": [
                "Status is the catalogue's recorded status; it may lag actual physical completion.",
                "Constituencies with fewer than 10 projects are excluded to avoid unstable rankings.",
            ],
        },
    )


def _high_spend_low_progress(ql: str, house, state, db: Session):
    # A plain "how many have expenditure but zero progress?" is the exact
    # cohort count (completion <= 0), not this screen's "<25% + >= Rs 1 lakh"
    # definition. Keep the stricter cohort handler for that phrasing.
    if re.search(r"\b(zero|no|0%)\s+(physical\s+)?progress\b", ql) \
            and not re.search(r"\b(high|large|significant)\b", ql):
        return None
    if not re.search(
        r"(?:expenditure|spend|spent|payment).{0,40}(?:high|large|significant).{0,30}(?:low|no|zero|0%|little).{0,25}(?:progress|completion)"
        r"|(?:low|no|zero|0%|little).{0,25}(?:progress|completion).{0,40}(?:high|large|significant).{0,25}(?:expenditure|spend|spent|payment)"
        r"|expenditure.{0,20}(?:but|with|and).{0,20}(?:low|no|zero)\s+(?:progress|completion)"
        r"|(?:expenditure|spend|spent|payment).{0,40}(?:high|large|significant).{0,45}(?:progress|completion).{0,25}(?:low|no|zero|0%|little)",
        ql,
    ):
        return None
    query = (
        db.query(
            models.Project.id,
            models.Project.project_name,
            models.Project.state,
            models.Project.constituency,
            models.Project.sanctioned_amount,
            models.Project.completion_percentage,
            models.ProjectRecInfo.linked_expenditure,
            models.ProjectRecInfo.linked_tx_count,
            models.RiskScore.risk_score,
            models.RiskScore.risk_level,
        )
        .join(models.ProjectRecInfo, models.ProjectRecInfo.project_id == models.Project.id)
        .outerjoin(models.RiskScore, models.RiskScore.project_id == models.Project.id)
        .filter(models.ProjectRecInfo.linked_expenditure >= 100000)
        .filter(func.coalesce(models.Project.completion_percentage, 0) < 25)
    )
    if house:
        query = query.filter(models.Project.house == house)
    if state:
        query = query.filter(models.Project.state == state)
    total = query.order_by(None).count()
    rows = query.order_by(models.ProjectRecInfo.linked_expenditure.desc()).limit(10).all()
    if not rows:
        return ("No project matches the screen in that scope: a linked payment of at least ₹1 lakh with reported physical completion below 25%.",
                [], None, None, {"intent": "dataset_analytics", "result_ids": []})

    table_rows = [[
        f"#{r.id}", (r.project_name or "")[:52], r.state or "—", r.constituency or "—",
        _money(r.sanctioned_amount), _money(r.linked_expenditure),
        f"{float(r.completion_percentage or 0):.0f}%",
        f"{r.risk_score} ({r.risk_level})" if r.risk_score is not None else "not scored",
    ] for r in rows]
    answer = (
        f"**{total:,} projects** have at least ₹1 lakh of linked payment recorded while reported physical "
        f"completion is below 25%{_scope_note(house, state, None)}. The {len(rows)} largest linked spends are below.\n\n"
        "“High expenditure” here means ≥ ₹1,00,000 linked through the conservative work-key + MP + IDA "
        "verification; “low progress” means the catalogue's completion field is below 25%. "
        "This is a financial/physical mismatch indicator, not proof of irregularity."
    )
    return (
        answer,
        [
            _action(label="Open Risk Center", action="navigate", target="Risk Center"),
            _action(label="Open Audit Priority", action="navigate", target="Audit Priority"),
        ],
        None,
        None,
        {
            "intent": "dataset_analytics",
            "result_ids": [int(r.id) for r in rows],
            "table": _table(
                ["Project", "Name", "State", "Constituency", "Sanctioned", "Linked expenditure", "Completion", "Risk"],
                table_rows,
                "Projects with ≥ ₹1 lakh linked payment and < 25% reported completion",
            ),
            "limitations": [
                "Only payments that pass the conservative work-key linkage are counted; unattributable spend is excluded.",
                "A low completion value may be an un-updated catalogue field rather than actual physical progress.",
            ],
        },
    )


def _missing_recommendation_dates(ql: str, house, state, db: Session):
    if not re.search(
        r"(?:missing|no|without|absent|blank)\s+(?:recorded\s+)?recommendation\s+date"
        r"|recommendation\s+dates?\s+(?:are\s+)?(?:missing|blank|absent|not\s+recorded)"
        r"|how\s+many\s+projects?.{0,40}recommendation\s+date",
        ql,
    ):
        return None
    base = db.query(models.Project.id).outerjoin(
        models.ProjectRecInfo, models.ProjectRecInfo.project_id == models.Project.id
    ).filter(
        (models.ProjectRecInfo.id.is_(None))
        | (models.ProjectRecInfo.recommendation_date.is_(None))
        | (func.trim(models.ProjectRecInfo.recommendation_date) == "")
    )
    total_q = db.query(func.count(models.Project.id))
    if house:
        base = base.filter(models.Project.house == house)
        total_q = total_q.filter(models.Project.house == house)
    if state:
        base = base.filter(models.Project.state == state)
        total_q = total_q.filter(models.Project.state == state)
    count = base.count()
    total = int(total_q.scalar() or 0)
    if count == 0:
        return ("Every project in this scope has a recorded recommendation date.",
                [], None, None, {"intent": "data_quality", "limitations": []})

    by_house_rows = []
    house_query = db.query(
        models.Project.house,
        func.count(models.Project.id),
        func.sum(case(
            ((models.ProjectRecInfo.id.is_(None))
             | (models.ProjectRecInfo.recommendation_date.is_(None))
             | (func.trim(models.ProjectRecInfo.recommendation_date) == ""), 1), else_=0
        )),
    ).outerjoin(models.ProjectRecInfo, models.ProjectRecInfo.project_id == models.Project.id)
    if state:
        house_query = house_query.filter(models.Project.state == state)
    for label, n_total, n_missing in house_query.group_by(models.Project.house).all():
        by_house_rows.append([label or "Unattributed", f"{int(n_total or 0):,}",
                              f"{int(n_missing or 0):,}", _pct(n_missing, n_total)])

    answer = (
        f"**{count:,} of {total:,} projects** ({_pct(count, total)}) have no recorded recommendation date"
        f"{_scope_note(house, state, None)}.\n\n"
        "The date comes from the recommended-works ledger and is copied into project_rec_info only when a "
        "conservative work-key match exists. A missing date means the timeline cannot be anchored for that "
        "project — it does not mean the work was never recommended."
    )
    return (
        answer,
        [
            _action(label="Open Projects", action="navigate", target="Projects",
                    params={"state": state} if state else {}),
            _action(label="Open Data Management", action="navigate", target="Data Management"),
        ],
        None,
        None,
        {
            "intent": "data_quality",
            "table": _table(
                ["House", "Projects", "Missing recommendation date", "Share"],
                by_house_rows,
                "Recommendation-date coverage by House",
            ),
            "findings": [{
                "title": "Recommendation date unavailable",
                "detail": f"{count:,} projects cannot be timeline-anchored because no recommendation date is linked.",
                "severity": "medium",
                "kind": "data_quality",
                "evidence": "project_rec_info.recommendation_date is NULL/blank for these project ids.",
            }],
            "limitations": [
                "A missing derived date can also mean the source recommendation row could not be conservatively linked, not that the source lacks a date entirely.",
            ],
        },
    )


def _expenditure_before_recommendation(ql: str, house, state, db: Session):
    if not re.search(
        r"(?:expenditure|payment|spend).{0,45}(?:before|earlier\s+than|prior\s+to).{0,35}recommendation"
        r"|recommendation.{0,35}(?:after|later\s+than).{0,30}(?:expenditure|payment)",
        ql,
    ):
        return None
    base = (
        db.query(models.Project)
        .join(models.ProjectRecInfo, models.ProjectRecInfo.project_id == models.Project.id)
        .filter(models.ProjectRecInfo.recommendation_date.isnot(None))
        .filter(func.trim(models.ProjectRecInfo.recommendation_date) != "")
        .filter(models.ProjectRecInfo.first_expenditure_date.isnot(None))
        .filter(func.trim(models.ProjectRecInfo.first_expenditure_date) != "")
        .filter(models.ProjectRecInfo.first_expenditure_date < models.ProjectRecInfo.recommendation_date)
    )
    if house:
        base = base.filter(models.Project.house == house)
    if state:
        base = base.filter(models.Project.state == state)
    count = base.count()
    rows = base.order_by(models.Project.id).limit(10).all()
    if count == 0:
        return ("No record has a first expenditure date earlier than its recorded recommendation date in this scope.",
                [], None, None, {"intent": "data_quality", "result_ids": []})
    table_rows = []
    for project in rows:
        rec = db.query(models.ProjectRecInfo).filter(models.ProjectRecInfo.project_id == project.id).first()
        table_rows.append([
            f"#{project.id}", (project.project_name or "")[:50], project.state or "—",
            rec.recommendation_date if rec else "—",
            rec.first_expenditure_date if rec else "—",
            _money(rec.linked_expenditure if rec else 0),
        ])
    answer = (
        f"**{count:,} projects** have a first recorded payment earlier than their recorded recommendation date"
        f"{_scope_note(house, state, None)}.\n\n"
        "This is a timeline/linkage inconsistency: it may reflect a source date error, a work-key match that "
        "joined the wrong payment, or a recommendation recorded after execution began. It is a data-review "
        "item, not evidence of wrongdoing."
    )
    return (
        answer,
        [
            _action(label="Open Projects", action="navigate", target="Projects",
                    params={"state": state} if state else {}),
            _action(label="Open Risk Center", action="navigate", target="Risk Center"),
        ],
        None,
        None,
        {
            "intent": "data_quality",
            "result_ids": [int(p.id) for p in rows],
            "table": _table(
                ["Project", "Name", "State", "Recommendation date", "First expenditure", "Linked spend"],
                table_rows,
                "Expenditure recorded before the recommendation date (first 10)",
            ),
            "findings": [{
                "title": "Expenditure precedes recommendation",
                "detail": f"{count:,} project timelines place a payment before the recorded recommendation.",
                "severity": "high",
                "kind": "data_quality",
                "evidence": "project_rec_info.first_expenditure_date < project_rec_info.recommendation_date.",
            }],
            "limitations": [
                "Both dates are derived from conservative work-key linkage; a wrong link can create a false sequence.",
            ],
        },
    )


def _house_activity(ql: str, db: Session):
    if not re.search(
        r"(?:compare|comparison|versus|vs\.?)\b.{0,40}(?:lok\s+sabha|rajya\s+sabha|houses?)"
        r"|(?:lok\s+sabha|rajya\s+sabha).{0,30}(?:vs\.?|versus|compared?\s+(?:to|with)|and).{0,30}(?:lok\s+sabha|rajya\s+sabha)"
        r"|house.{0,20}(?:activity|comparison|breakdown)",
        ql,
    ):
        return None
    rows = []
    for house in ("Lok Sabha", "Rajya Sabha"):
        project_stats = db.query(
            func.count(models.Project.id),
            func.coalesce(func.sum(models.Project.sanctioned_amount), 0.0),
            func.coalesce(func.avg(models.Project.completion_percentage), 0.0),
        ).filter(models.Project.house == house).one()
        ledger = metrics.ledger_totals(db, house=house)
        high_risk = (
            db.query(func.count(models.RiskScore.id))
            .join(models.Project, models.Project.id == models.RiskScore.project_id)
            .filter(models.Project.house == house, models.RiskScore.risk_level.ilike("high"))
            .scalar() or 0
        )
        completed_status = db.query(func.count(models.Project.id)).filter(
            models.Project.house == house, func.lower(models.Project.status) == "completed"
        ).scalar() or 0
        rows.append({
            "house": house,
            "projects": int(project_stats[0] or 0),
            "sanctioned": float(project_stats[1] or 0),
            "avg_completion": float(project_stats[2] or 0),
            "expenditure": float(ledger["total_expenditure"] or 0),
            "completed_works": int(ledger["completed_works"] or 0),
            "completed_status": int(completed_status or 0),
            "high_risk": int(high_risk or 0),
        })
    if not any(r["projects"] for r in rows):
        return ("No house-attributed project rows are recorded.", [], None, None,
                {"intent": "comparison", "limitations": ["House attribution is derived; unattributed rows are excluded."]})
    table_rows = [[
        r["house"], f"{r['projects']:,}", _money(r["sanctioned"]), _money(r["expenditure"]),
        _pct(r["expenditure"], r["sanctioned"]), f"{r['avg_completion']:.1f}%",
        f"{r['completed_status']:,}", f"{r['completed_works']:,}", f"{r['high_risk']:,}",
    ] for r in rows]
    ls, rs = rows
    answer = (
        "**House activity comparison** (recorded data; each house uses the same aggregation rules):\n\n"
        f"• **Lok Sabha** — {ls['projects']:,} projects, {_money(ls['sanctioned'])} sanctioned, "
        f"{_money(ls['expenditure'])} recorded expenditure, {ls['avg_completion']:.1f}% average catalogue completion.\n"
        f"• **Rajya Sabha** — {rs['projects']:,} projects, {_money(rs['sanctioned'])} sanctioned, "
        f"{_money(rs['expenditure'])} recorded expenditure, {rs['avg_completion']:.1f}% average catalogue completion.\n\n"
        "Project counts and sanctioned amounts come from the catalogue's house attribution; expenditure and "
        "completed-work counts come from the ledgers' own house columns. Unattributed catalogue rows are excluded."
    )
    return (
        answer,
        [
            _action(label="Open MP Intelligence", action="navigate", target="MP Intelligence"),
            _action(label="Open State Intelligence", action="navigate", target="State Intelligence"),
        ],
        None,
        None,
        {
            "intent": "comparison",
            "table": _table(
                ["House", "Projects", "Sanctioned", "Recorded expenditure", "Utilization",
                 "Avg completion", "Completed status", "Completed works (ledger)", "High-risk flags"],
                table_rows,
                "Lok Sabha vs Rajya Sabha recorded activity",
            ),
            "limitations": [
                "Projects without house attribution are excluded from both sides.",
                "Completed status (catalogue) and completed works (ledger) measure different things.",
            ],
        },
    )


def _high_risk_distribution(ql: str, house, state, db: Session):
    if not re.search(
        r"(?:distribution|breakdown).{0,35}high[- ]risk"
        r"|high[- ]risk.{0,30}(?:distribution|by\s+state|breakdown)"
        r"|which\s+states?.{0,35}(?:most|highest|largest).{0,25}high[- ]risk"
        r"|states?.{0,30}(?:most|highest).{0,25}high[- ]risk",
        ql,
    ):
        return None
    query = (
        db.query(
            models.Project.state,
            func.count(models.RiskScore.id).label("n"),
            func.coalesce(func.sum(models.Project.sanctioned_amount), 0.0).label("sanctioned"),
        )
        .join(models.RiskScore, models.RiskScore.project_id == models.Project.id)
        .filter(models.RiskScore.risk_level.ilike("high"))
        .filter(models.Project.state.isnot(None))
    )
    if house:
        query = query.filter(models.Project.house == house)
    if state:
        query = query.filter(models.Project.state == state)
    rows = query.group_by(models.Project.state).order_by(func.count(models.RiskScore.id).desc()).limit(10).all()
    total_q = db.query(func.count(models.RiskScore.id)).join(
        models.Project, models.Project.id == models.RiskScore.project_id
    ).filter(models.RiskScore.risk_level.ilike("high"))
    if house:
        total_q = total_q.filter(models.Project.house == house)
    if state:
        total_q = total_q.filter(models.Project.state == state)
    total = int(total_q.scalar() or 0)
    if not rows:
        return ("No high-risk projects are currently flagged in the risk data.", [], None, None,
                {"intent": "risk_explanation", "limitations": []})
    table_rows = [[r.state, f"{int(r.n or 0):,}", _pct(r.n, total), _money(r.sanctioned)] for r in rows]
    top = rows[0]
    answer = (
        f"**High-risk flags by state** — {total:,} projects are currently flagged high risk"
        f"{_scope_note(house, state, None)}. **{top.state}** has the most, with {int(top.n or 0):,} "
        f"({_pct(top.n, total)} of all high-risk flags).\n\n"
        "A high-risk flag is a review priority derived from recorded indicators — not a finding of wrongdoing."
    )
    params = {"risk_level": "High"}
    if state:
        params["state"] = state
    return (
        answer,
        [
            _action(label="Risk Center · High risk", action="navigate", target="Risk Center", params=params),
            _action(label="Open State Intelligence", action="navigate", target="State Intelligence"),
        ],
        None,
        None,
        {
            "intent": "risk_explanation",
            "table": _table(
                ["State", "High-risk projects", "Share of high-risk", "Sanctioned exposure"],
                table_rows,
                "Distribution of high-risk flags by state (top 10)",
            ),
            "limitations": [
                "State comes from the catalogue; projects with no state are excluded.",
                "Flags reflect stored risk scores, not new scoring at question time.",
            ],
        },
    )


def _lowest_completion_states(ql: str, house, db: Session):
    if not re.search(
        r"states?.{0,40}(?:lowest|worst|poorest).{0,30}(?:completion|progress)"
        r"|(?:lowest|worst|poorest).{0,25}(?:completion|progress).{0,25}states?"
        r"|lowest\s+completion\s+rates?",
        ql,
    ):
        return None
    query = db.query(
        models.Project.state,
        func.count(models.Project.id).label("total"),
        func.coalesce(func.avg(models.Project.completion_percentage), 0.0).label("avg_completion"),
        func.sum(case((func.lower(models.Project.status) == "completed", 1), else_=0)).label("completed"),
    ).filter(models.Project.state.isnot(None))
    if house:
        query = query.filter(models.Project.house == house)
    rows = query.group_by(models.Project.state).having(
        func.count(models.Project.id) >= 50
    ).order_by(func.coalesce(func.avg(models.Project.completion_percentage), 0.0).asc()).limit(10).all()
    if not rows:
        return ("No state has enough recorded projects to rank completion reliably.", [], None, None,
                {"intent": "dataset_analytics", "limitations": ["Minimum group size is 50 projects."]})
    table_rows = [[
        r.state, f"{int(r.total or 0):,}", f"{float(r.avg_completion or 0):.1f}%",
        f"{int(r.completed or 0):,}", _pct(r.completed, r.total),
    ] for r in rows]
    bottom = rows[0]
    answer = (
        f"**{bottom.state}** has the lowest average recorded physical completion"
        f"{_scope_note(house, None, None)}: **{float(bottom.avg_completion or 0):.1f}%** across "
        f"{int(bottom.total or 0):,} projects, with {_pct(bottom.completed, bottom.total)} recorded as Completed.\n\n"
        "Average completion is the mean of the catalogue's completion_percentage field. States with fewer "
        "than 50 projects are excluded; a low average can reflect un-updated records as well as slower delivery."
    )
    return (
        answer,
        [
            _action(label=f"State Intelligence · {bottom.state}", action="navigate",
                    target="State Intelligence", params={"state": bottom.state}),
            _action(label="Open Reports", action="navigate", target="Reports"),
        ],
        None,
        None,
        {
            "intent": "dataset_analytics",
            "entity": {"state": bottom.state},
            "table": _table(
                ["State", "Projects", "Avg completion", "Completed status", "Completed share"],
                table_rows,
                "States with the lowest average recorded completion (min. 50 projects)",
            ),
            "limitations": [
                "Completion is the catalogue's recorded percentage, which may lag actual progress.",
                "Group-size threshold is 50 projects.",
            ],
        },
    )


def _vendor_work_counts(ql: str, db: Session):
    if not re.search(
        r"vendors?.{0,45}(?:largest|most|highest|top).{0,30}(?:number\s+of\s+)?(?:projects?|works?)"
        r"|(?:most|largest).{0,25}vendors?.{0,25}(?:projects?|works?)",
        ql,
    ):
        return None
    rows = db.execute(
        text("""
            SELECT trim(vendor) AS name,
                   COUNT(DISTINCT lower(trim(work_description))) AS works,
                   COUNT(DISTINCT lower(trim(state))) AS states,
                   COUNT(*) AS transactions,
                   COALESCE(SUM(expenditure_amount), 0) AS total
            FROM expenditures
            WHERE vendor IS NOT NULL AND trim(vendor) <> ''
            GROUP BY normkey(vendor)
            ORDER BY works DESC, total DESC
            LIMIT 10
        """)
    ).fetchall()
    if not rows:
        return ("No vendor expenditure records are available.", [], None, None,
                {"intent": "vendor_investigation", "limitations": []})
    table_rows = [[
        r.name, f"{int(r.works or 0):,}", f"{int(r.states or 0):,}",
        f"{int(r.transactions or 0):,}", _money(r.total),
    ] for r in rows]
    top = rows[0]
    answer = (
        f"**{top.name}** appears against the largest number of distinct works in the expenditure ledger: "
        f"**{int(top.works or 0):,}** distinct work descriptions across {int(top.states or 0):,} state labels, "
        f"with {_money(top.total)} paid over {int(top.transactions or 0):,} transactions.\n\n"
        "The ledger does not publish a vendor-to-catalogue-project identifier, so this counts distinct "
        "expenditure line items (work descriptions), not verified catalogue projects. It is a concentration "
        "signal for review, not proof of any relationship."
    )
    return (
        answer,
        [
            _action(label="Open Vendor Intelligence", action="navigate", target="Vendor Intelligence",
                    params={"vendor": top.name}),
            _action(label="Open Vendor Network", action="navigate", target="Vendor Network"),
        ],
        None,
        None,
        {
            "intent": "vendor_investigation",
            "entity": {"type": "vendor", "name": top.name},
            "table": _table(
                ["Vendor", "Distinct works", "State labels", "Transactions", "Recorded total"],
                table_rows,
                "Vendors associated with the most distinct expenditure works",
            ),
            "limitations": [
                "Vendor records contain work descriptions, not catalogue project ids.",
                "Name variants are grouped with the platform's normalized vendor key.",
            ],
        },
    )


def _fy_comparison(ql: str, house, db: Session):
    if not re.search(
        r"financial\s+year.{0,30}(?:compar|trend|breakdown|distribution)"
        r"|(?:compar|trend|breakdown).{0,30}financial\s+year"
        r"|\bfy\b.{0,25}(?:compar|trend|breakdown)|\byear.on.year\b",
        ql,
    ):
        return None
    rows = metrics.fy_trends(db, house=house)
    if not rows:
        return ("No financial-year totals are available.", [], None, None,
                {"intent": "comparison", "limitations": []})
    table_rows = [[
        r.get("fy") or "Unknown", f"{int(r.get('projects') or 0):,}",
        _money(r.get("sanctioned")), _money(r.get("expenditure")),
        f"{float(r['utilization_percentage']):.1f}%" if r.get("utilization_percentage") is not None else "—",
        f"{int(r.get('completed') or 0):,}",
    ] for r in rows]
    answer = (
        f"**Financial-year comparison{_scope_note(house, None, None)}** — project counts and sanctioned amounts "
        "are grouped by the catalogue's FY; expenditure and completed works are grouped by the ledger date's "
        "Indian financial year (April–March).\n\n"
        "Utilization compares the two scopes and is therefore indicative: sanctioned amounts belong to the "
        "catalogue FY, while payments can arrive in later years."
    )
    return (
        answer,
        [_action(label="Open Reports", action="navigate", target="Reports"),
         _action(label="Open Overview", action="navigate", target="Overview")],
        None,
        None,
        {
            "intent": "comparison",
            "table": _table(
                ["Financial year", "Projects", "Sanctioned", "Recorded expenditure", "Utilization", "Completed works"],
                table_rows,
                "Financial-year comparison",
            ),
            "limitations": ["The two sides are attributed to FY by different date fields."],
        },
    )


def _portfolio_data_quality(ql: str, house, state, db: Session):
    if not re.search(
        r"data\s+qualit|data\s+(?:issues?|problems?|coverage|completeness|gaps?)"
        r"|dataset\s+(?:quality|issues?|coverage)|how\s+complete\s+is\s+the\s+data"
        r"|missing\s+(?:data|fields|records)\b",
        ql,
    ):
        return None
    if re.search(r"\b(this|that)\s+(project|work)\b|project\s*#?\s*\d+", ql):
        return None  # project-level data quality belongs to the investigation intent

    project_query = db.query(
        func.count(models.Project.id),
        func.sum(case((models.Project.completion_percentage.is_(None), 1), else_=0)),
        func.sum(case((models.Project.status.is_(None) | (func.trim(models.Project.status) == ""), 1), else_=0)),
        func.sum(case((models.Project.state.is_(None) | (func.trim(models.Project.state) == ""), 1), else_=0)),
        func.sum(case((models.Project.house.is_(None) | (func.trim(models.Project.house) == ""), 1), else_=0)),
        func.sum(case((
            (func.lower(models.Project.status) == "completed")
            & (func.coalesce(models.Project.completion_percentage, 0) < 90), 1), else_=0)),
        func.sum(case((
            (models.Project.sanctioned_amount.is_(None)) | (models.Project.sanctioned_amount <= 0), 1), else_=0)),
    )
    if house:
        project_query = project_query.filter(models.Project.house == house)
    if state:
        project_query = project_query.filter(models.Project.state == state)
    (total, missing_completion, missing_status, missing_state,
     missing_house, status_conflict, missing_sanction) = project_query.one()
    total = int(total or 0)
    if total == 0:
        return ("No projects match that scope.", [], None, None,
                {"intent": "data_quality", "limitations": []})

    rec_query = db.query(
        func.count(models.ProjectRecInfo.id),
        func.sum(case((
            models.ProjectRecInfo.recommendation_date.is_(None)
            | (func.trim(models.ProjectRecInfo.recommendation_date) == ""), 1), else_=0)),
        func.sum(case((
            models.ProjectRecInfo.first_expenditure_date.isnot(None)
            & (func.trim(models.ProjectRecInfo.first_expenditure_date) != "")
            & models.ProjectRecInfo.recommendation_date.isnot(None)
            & (func.trim(models.ProjectRecInfo.recommendation_date) != "")
            & (models.ProjectRecInfo.first_expenditure_date < models.ProjectRecInfo.recommendation_date), 1), else_=0)),
        func.sum(case((func.coalesce(models.ProjectRecInfo.work_key_rows, 1) > 1, 1), else_=0)),
        func.sum(case((func.coalesce(models.ProjectRecInfo.linked_expenditure, 0) <= 0, 1), else_=0)),
    )
    if house or state:
        rec_query = rec_query.join(models.Project, models.Project.id == models.ProjectRecInfo.project_id)
        if house:
            rec_query = rec_query.filter(models.Project.house == house)
        if state:
            rec_query = rec_query.filter(models.Project.state == state)
    rec_rows, missing_rec, exp_before_rec, shared_work, no_linked = rec_query.one()
    rec_rows = int(rec_rows or 0)
    missing_rec = int(missing_rec or 0) + max(0, total - rec_rows)
    exp_before_rec = int(exp_before_rec or 0)
    shared_work = int(shared_work or 0)
    no_linked = int(no_linked or 0) + max(0, total - rec_rows)

    missing_vendor = db.query(func.count(models.Expenditure.id)).filter(
        (models.Expenditure.vendor.is_(None)) | (func.trim(models.Expenditure.vendor) == "")
    )
    overspend_query = db.query(func.count(models.ProjectRecInfo.id)).join(
        models.Project, models.Project.id == models.ProjectRecInfo.project_id
    ).filter(
        models.Project.sanctioned_amount > 0,
        models.ProjectRecInfo.linked_expenditure > models.Project.sanctioned_amount,
    )
    if house:
        missing_vendor = missing_vendor.filter(models.Expenditure.house == house)
        overspend_query = overspend_query.filter(models.Project.house == house)
    if state:
        missing_vendor = missing_vendor.filter(models.Expenditure.state == state)
        overspend_query = overspend_query.filter(models.Project.state == state)
    missing_vendor = int(missing_vendor.scalar() or 0)
    overspend = int(overspend_query.scalar() or 0)

    rows = [
        ["Recommendation date missing", f"{missing_rec:,}", _pct(missing_rec, total),
         "Timeline cannot be anchored for these projects."],
        ["Completion percentage missing", f"{int(missing_completion or 0):,}", _pct(missing_completion, total),
         "Physical progress cannot be compared with spending."],
        ["Status missing", f"{int(missing_status or 0):,}", _pct(missing_status, total),
         "Project stage is unknown."],
        ["Location missing", f"{int(missing_state or 0):,}", _pct(missing_state, total),
         "State-level aggregation is impossible."],
        ["House attribution missing", f"{int(missing_house or 0):,}", _pct(missing_house, total),
         "Excluded from every house-scoped answer."],
        ["Completed status but <90% progress", f"{int(status_conflict or 0):,}", _pct(status_conflict, total),
         "Status and physical progress disagree; completion evidence needs review."],
        ["Sanctioned amount missing/zero", f"{int(missing_sanction or 0):,}", _pct(missing_sanction, total),
         "Utilization cannot be computed."],
        ["Expenditure before recommendation", f"{exp_before_rec:,}", _pct(exp_before_rec, total),
         "Timeline/linkage inconsistency."],
        ["Expenditure above sanctioned", f"{overspend:,}", _pct(overspend, total),
         "Cost-overrun indicator, not a finding."],
        ["No linked expenditure record", f"{no_linked:,}", _pct(no_linked, total),
         "Spend is unknown — not confirmed zero."],
        ["Shared work-key rows", f"{shared_work:,}", _pct(shared_work, total),
         "Per-project spend attribution is ambiguous."],
        ["Ledger payments without vendor", f"{missing_vendor:,}", "—",
         "Payments exist but vendor identity is unavailable."],
    ]
    findings = [
        {
            "title": "Recommendation-date coverage gap",
            "detail": f"{missing_rec:,} of {total:,} projects lack a usable recommendation date.",
            "severity": "medium",
            "kind": "data_quality",
            "evidence": "project_rec_info.recommendation_date is missing or the project has no derived row.",
        },
        {
            "title": "Status/progress conflict",
            "detail": f"{int(status_conflict or 0):,} projects are marked Completed while recorded progress is below 90%.",
            "severity": "medium",
            "kind": "data_quality",
            "evidence": "projects.status = Completed and projects.completion_percentage < 90.",
        },
        {
            "title": "Timeline inversion",
            "detail": f"{exp_before_rec:,} projects have a first payment earlier than the recommendation date.",
            "severity": "high",
            "kind": "data_quality",
            "evidence": "project_rec_info.first_expenditure_date < recommendation_date.",
        },
    ]
    answer = (
        f"**Portfolio data quality{_scope_note(house, state, None)}** — {total:,} projects reviewed.\n\n"
        "The biggest limitations are coverage, not conclusions: "
        f"{missing_rec:,} projects cannot be timeline-anchored, {no_linked:,} have no linked payment record, "
        f"and {int(status_conflict or 0):,} have status/progress values that disagree.\n\n"
        "Counts are measured directly from the catalog and derived linkage table. Missing values are reported "
        "as unknown; none of them is treated as zero or as evidence of wrongdoing."
    )
    actions = [
        _action(label="Open Projects", action="navigate", target="Projects",
                params={"state": state} if state else {}),
        _action(label="Open Risk Center", action="navigate", target="Risk Center"),
    ]
    return (
        answer,
        actions,
        None,
        None,
        {
            "intent": "data_quality",
            "table": _table(["Check", "Records", "Share", "Why it matters"], rows,
                            "Dataset limitation counts for the selected scope"),
            "findings": findings,
            "limitations": [
                "Coverage counts describe the recorded dataset, not the underlying real-world works.",
                "Missing values can reflect an unlinked source row rather than an absent source fact.",
            ],
        },
    )


def _status_distribution(ql: str, house, state, db: Session):
    if not re.search(
        r"projects?\s+(?:by|per)\s+status|status\s+(?:distribution|breakdown|split)"
        r"|how\s+many\s+projects?.{0,25}(?:completed|ongoing|recommended)",
        ql,
    ):
        return None
    query = db.query(
        func.coalesce(func.nullif(func.trim(models.Project.status), ""), "Not recorded").label("status"),
        func.count(models.Project.id),
        func.coalesce(func.sum(models.Project.sanctioned_amount), 0.0),
    )
    if house:
        query = query.filter(models.Project.house == house)
    if state:
        query = query.filter(models.Project.state == state)
    rows = query.group_by("status").order_by(func.count(models.Project.id).desc()).limit(10).all()
    if not rows:
        return ("No projects match that scope.", [], None, None,
                {"intent": "dataset_analytics", "limitations": []})
    total_q = db.query(func.count(models.Project.id))
    if house:
        total_q = total_q.filter(models.Project.house == house)
    if state:
        total_q = total_q.filter(models.Project.state == state)
    total = int(total_q.scalar() or 0)
    table_rows = [[r[0], f"{int(r[1] or 0):,}", _pct(r[1], total), _money(r[2])] for r in rows]
    answer = (
        f"**Project status distribution{_scope_note(house, state, None)}** — {total:,} projects with a recorded "
        "status group. Counts use the catalogue's status field exactly as recorded; blank values are shown "
        "as “Not recorded” rather than merged into another status."
    )
    return (
        answer,
        [_action(label="Open Projects", action="navigate", target="Projects",
                  params={"state": state} if state else {})],
        None,
        None,
        {
            "intent": "dataset_analytics",
            "table": _table(["Status", "Projects", "Share", "Sanctioned"], table_rows,
                            "Projects by recorded status"),
            "limitations": ["Status is a recorded field and may lag actual delivery."],
        },
    )


# ── entry point ──────────────────────────────────────────────────────

def analytics_intent(q: str, ql: str, user, db: Session):
    """Portfolio analytics intent. Returns the assistant's 5-tuple or None."""
    # Cheap pattern pre-check: every branch starts with a regex, so run them
    # only when the question looks analytical at all.
    if not re.search(
        r"\b(which|how\s+many|how\s+much|highest|lowest|most|least|top|distribution|breakdown"
        r"|compar(?:e|ison)|versus|missing|status|high|low|data\s+quality)\b",
        ql,
    ):
        return None

    house = _house(q)
    state = _state(q, db)
    fy = _fy(q, db)

    checks = (
        lambda: _state_comparison(ql, house, db),
        lambda: _high_spend_low_progress(ql, house, state, db),
        lambda: _missing_recommendation_dates(ql, house, state, db),
        lambda: _expenditure_before_recommendation(ql, house, state, db),
        lambda: _lowest_completion_states(ql, house, db),
        lambda: _incomplete_constituencies(ql, house, state, db),
        lambda: _high_risk_distribution(ql, house, state, db),
        lambda: _house_activity(ql, db),
        lambda: _highest_expenditure_state(ql, house, fy, db),
        lambda: _vendor_work_counts(ql, db),
        lambda: _fy_comparison(ql, house, db),
        lambda: _portfolio_data_quality(ql, house, state, db),
        lambda: _status_distribution(ql, house, state, db),
    )
    for check in checks:
        try:
            result = check()
        except Exception:
            result = None
        if result is None:
            continue
        if user is None or not auth.has_role_rank(user, "analyst"):
            return _guest_response((result[4] if len(result) > 4 else None))
        return result
    return None
