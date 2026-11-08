"""
MPLADS AI — Phase 1 analytical engines
======================================

Feature 1  Anomaly root-cause graph        root_cause()
Feature 2  Emerging risk discovery         emerging_risk()
Feature 3  Behaviour clustering            clusters() / cluster_projects()
Feature 4  Cross-dataset reconciliation    reconciliation() / review_reconciliation()
Feature 7  Anomaly pattern evolution       anomaly_evolution()

Design rules enforced here:
  • every number is read from the existing tables — nothing is estimated;
  • observed correlation is labelled differently from a rule-derived
    contributing indicator, and neither is ever called a cause;
  • findings always carry the records they came from;
  • missing data is reported as a gap/limitation, never imputed silently
    (clustering is the one exception, and it reports how many values it had
    to impute);
  • the existing risk formula is never recalculated — stored risk_scores and
    reasons are read as-is;
  • history-dependent features return an explicit "no history recorded"
    payload when project_snapshots / import_batches are empty instead of
    inventing a series.
"""

from __future__ import annotations

import math
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

import audit_intel
import models
from database import normkey

CACHE_TTL = 300.0
_lock = threading.Lock()
_cache: Dict[str, Tuple[float, Any]] = {}
_cluster_state: Dict[str, Any] = {"key": None, "assignments": {}, "meta": {}}

FINAL_STATUSES = {"Verified", "Not substantiated", "Resolved"}


# ── helpers ───────────────────────────────────────────────────────────

def _num(v) -> Optional[float]:
    try:
        if v is None:
            return None
        f = float(v)
        return f if math.isfinite(f) else None
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


def _cached(key: str, builder, ttl: float = CACHE_TTL):
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    value = builder()
    with _lock:
        _cache[key] = (now, value)
    return value


def reset_cache() -> None:
    with _lock:
        _cache.clear()
    _cluster_state.update({"key": None, "assignments": {}, "meta": {}})


def _rows(db: Session, sql: str, params: Optional[Dict[str, Any]] = None) -> List[Sequence[Any]]:
    try:
        return db.execute(text(sql), params or {}).fetchall()
    except Exception:
        return []


def _scalar(db: Session, sql: str, params: Optional[Dict[str, Any]] = None) -> Optional[int]:
    try:
        v = db.execute(text(sql), params or {}).scalar()
        return int(v) if v is not None else None
    except Exception:
        return None


def _scope_clauses(filters: Dict[str, Any], alias: str = "p") -> Tuple[List[str], List[Any]]:
    clauses: List[str] = []
    params: List[Any] = []
    if filters.get("state"):
        clauses.append(f"LOWER(TRIM({alias}.state)) = LOWER(?)")
        params.append(filters["state"])
    if filters.get("district"):
        clauses.append(f"LOWER(TRIM({alias}.district)) = LOWER(?)")
        params.append(filters["district"])
    if filters.get("house") in ("Lok Sabha", "Rajya Sabha"):
        clauses.append(f"{alias}.house = ?")
        params.append(filters["house"])
    if filters.get("category"):
        clauses.append(f"LOWER(TRIM({alias}.project_type)) = LOWER(?)")
        params.append(filters["category"])
    return clauses, params


def _parse_reasons(raw) -> List[str]:
    """Stored risk reasons are comma-joined phrases. Only split on ', '
    followed by a capital letter so phrases such as
    'Very low fund utilization and low completion' stay intact."""
    s = str(raw or "").strip()
    if not s:
        return []
    out: List[str] = []
    buf = ""
    for part in s.split(","):
        candidate = part.strip()
        if not candidate:
            continue
        if buf and not (candidate[:1].isupper()):
            buf = f"{buf}, {candidate}"
            continue
        if buf:
            out.append(buf)
        buf = candidate
    if buf:
        out.append(buf)
    return out[:8]


# ══════════════════════════════════════════════════════════════════════
# FEATURE 1 — Anomaly root-cause graph
# ══════════════════════════════════════════════════════════════════════

ROOT_CAUSE_NOTE = (
    "Edges labelled 'contributing indicator' are the stored rule-based risk "
    "reasons applied by the existing scoring engine. Edges labelled "
    "'correlated deviation' are statistical comparisons only. Neither is "
    "evidence of causation or wrongdoing."
)


def root_cause(db: Session, project_id: int) -> Optional[Dict[str, Any]]:
    project = db.query(models.Project).filter(models.Project.id == project_id).first()
    if project is None:
        return None
    rec = (db.query(models.ProjectRecInfo)
           .filter(models.ProjectRecInfo.project_id == project_id).first())
    risk = (db.query(models.RiskScore)
            .filter(models.RiskScore.project_id == project_id).first())

    sanc = _num(project.sanctioned_amount) or 0.0
    exp = _num(rec.linked_expenditure) if rec is not None else None
    exp = exp if exp is not None else 0.0
    comp = _num(project.completion_percentage)
    reasons = _parse_reasons(risk.reasons if risk is not None else None)
    risk_level = (risk.risk_level if risk is not None else None) or "None"
    ml_anomaly = bool(risk.ml_anomaly) if risk is not None else False

    recd = str(rec.recommendation_date or "")[:10] if rec is not None else ""
    age_months = None
    if len(recd) == 10 and recd[4] == "-":
        try:
            from datetime import date
            age_months = max(0.0, (date.today() - date(int(recd[:4]), int(recd[5:7]), int(recd[8:10]))).days / 30.4375)
        except Exception:
            age_months = None

    # Peer deviation — existing engine, same one Risk Center uses.
    peer_metrics: List[Dict[str, Any]] = []
    peer_reliability = "unavailable"
    peer_count = 0
    try:
        peer = audit_intel.peer_benchmark(project, db, scope="state")
        peer_metrics = peer.get("metrics") or []
        peer_reliability = peer.get("reliability") or "unknown"
        peer_count = int(peer.get("peer_count") or 0)
    except Exception:
        pass
    util_metric = next((m for m in peer_metrics if m.get("metric") == "utilization_pct"), None)
    comp_metric = next((m for m in peer_metrics if m.get("metric") == "completion_pct"), None)

    util = (exp / sanc * 100.0) if sanc > 0 else None

    # Vendor concentration on this work key (linked payment ledger).
    vendor_rows = _rows(db, """
        SELECT vendor, SUM(expenditure_amount) AS paid, COUNT(*) AS tx,
               MIN(expenditure_date) AS first_tx, MAX(expenditure_date) AS last_tx
        FROM expenditures
        WHERE normkey(work_description) = :wk
          AND normkey(constituency) = :cons
          AND normkey(state) = :st
          AND vendor IS NOT NULL AND TRIM(vendor) <> ''
        GROUP BY vendor ORDER BY paid DESC LIMIT 5
    """, {
        "wk": normkey(project.project_name or ""),
        "cons": normkey(project.constituency or ""),
        "st": normkey(project.state or ""),
    })
    vendor_total = sum(_num(r[1]) or 0 for r in vendor_rows)
    top_vendor = None
    top_share = None
    if vendor_rows and vendor_total > 0:
        top_vendor = str(vendor_rows[0][0]).strip()
        top_share = (_num(vendor_rows[0][1]) or 0) / vendor_total

    # Stored historical change events for this project (usually absent).
    change_count = _scalar(db, "SELECT COUNT(*) FROM project_change_events WHERE project_id = :pid",
                           {"pid": project_id}) or 0
    snapshot_count = _scalar(db, "SELECT COUNT(*) FROM project_snapshots WHERE project_id = :pid",
                             {"pid": project_id}) or 0

    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []

    outcome_id = "outcome"
    nodes.append({
        "id": outcome_id,
        "kind": "outcome",
        "label": f"{risk_level} risk · score {risk.risk_score if risk is not None else '—'}",
        "role": "outcome",
        "detail": (
            f"Stored risk level '{risk_level}' with score "
            f"{risk.risk_score if risk is not None else 'not recorded'}."
            + (" Isolation Forest also flagged this row as an outlier." if ml_anomaly else "")
        ),
        "evidence": [{"table": "risk_scores", "reference": f"project_id={project_id}",
                      "fields": {"risk_level": risk_level,
                                 "risk_score": risk.risk_score if risk is not None else None,
                                 "ml_anomaly": ml_anomaly}}],
    })

    def add_node(node_id: str, kind: str, label: str, detail: str,
                 relation: str, evidence: List[Dict[str, Any]],
                 strength: float = 0.5, limitation: Optional[str] = None,
                 value: Any = None) -> None:
        nodes.append({
            "id": node_id, "kind": kind, "label": label, "detail": detail,
            "role": "driver", "value": value, "limitation": limitation,
            "evidence": evidence,
        })
        edges.append({
            "from": node_id, "to": outcome_id, "relation": relation,
            "strength": max(0.0, min(1.0, strength)),
            "explanation": detail,
        })

    # Sanction size — context, not a driver on its own.
    nodes.append({
        "id": "sanction", "kind": "record", "role": "context",
        "label": f"Sanctioned {_money(sanc)}",
        "detail": f"Recorded sanctioned amount {_money(sanc)}; financial year {project.fy or 'not recorded'}.",
        "value": sanc,
        "evidence": [{"table": "projects", "reference": f"id={project_id}",
                      "fields": {"sanctioned_amount": sanc, "fy": project.fy}}],
    })

    # Utilization
    if util is not None:
        peer_med = util_metric.get("peer_median") if util_metric else None
        deviation = util_metric.get("deviation_vs_median") if util_metric else None
        if deviation is not None and abs(float(deviation)) >= 25:
            add_node(
                "utilization", "indicator",
                f"Utilization {util:.1f}%" + (f" vs peer median {peer_med}%" if peer_med is not None else ""),
                (f"Linked payments are {abs(float(deviation)):.1f} percentage points "
                 f"{'above' if float(deviation) > 0 else 'below'} the peer median across "
                 f"{peer_count} comparable works. A peer deviation is a review signal, not a cause."),
                "correlated deviation",
                [{"table": "project_rec_info", "reference": f"project_id={project_id}",
                  "fields": {"linked_expenditure": exp, "work_key": rec.work_key if rec else None}},
                 {"table": "peer_benchmark", "reference": f"state scope, n={peer_count}",
                  "fields": {"peer_median_utilization": peer_med,
                             "this_project_utilization": util}}],
                strength=min(1.0, abs(float(deviation)) / 100.0),
                limitation=(f"Peer reliability is '{peer_reliability}'; peer medians depend on "
                            "how completely comparable records report expenditure."),
                value=util,
            )
        else:
            add_node(
                "utilization", "indicator", f"Utilization {util:.1f}%",
                ("Recorded utilization is within 25 percentage points of its peer median, so it "
                 "does not stand out as a deviation."),
                "context",
                [{"table": "project_rec_info", "reference": f"project_id={project_id}",
                  "fields": {"linked_expenditure": exp}}],
                strength=0.15, value=util,
            )

    # Completion
    if comp is not None:
        peer_med_c = comp_metric.get("peer_median") if comp_metric else None
        dev_c = comp_metric.get("deviation_vs_median") if comp_metric else None
        if dev_c is not None and float(dev_c) <= -25:
            add_node(
                "completion", "indicator",
                f"Progress {comp:.0f}% vs peers {peer_med_c}%",
                (f"Reported completion is {abs(float(dev_c)):.1f} points below the peer median. "
                 "Slow progress is a contributing indicator in the stored risk reasons and a "
                 "correlated deviation against peers — not a proven cause of anything."),
                "correlated deviation",
                [{"table": "projects", "reference": f"id={project_id}",
                  "fields": {"completion_percentage": comp, "status": project.status}},
                 {"table": "peer_benchmark", "reference": f"n={peer_count}",
                  "fields": {"peer_median_completion": peer_med_c}}],
                strength=min(1.0, abs(float(dev_c)) / 100.0), value=comp,
            )
        else:
            add_node("completion", "indicator", f"Progress {comp:.0f}%",
                     "Reported completion is not materially below its peer group.",
                     "context",
                     [{"table": "projects", "reference": f"id={project_id}",
                       "fields": {"completion_percentage": comp}}],
                     strength=0.15, value=comp)

    # Age / recommendation record
    if age_months is not None:
        add_node(
            "age", "record", f"Age {age_months:.0f} months",
            (f"Recommended on {recd}; the work has been on record for about "
             f"{age_months:.0f} months. Age alone is context — it matters only when "
             "progress or payments have not moved."),
            "context",
            [{"table": "project_rec_info", "reference": f"project_id={project_id}",
              "fields": {"recommendation_date": recd, "age_months": round(age_months, 1)}}],
            strength=0.2, value=round(age_months, 1),
        )
    else:
        nodes.append({
            "id": "age", "kind": "gap", "role": "driver",
            "label": "No recommendation date recorded",
            "detail": ("The recommendation date is missing, so project age and any age-based "
                       "delay indicator cannot be evaluated for this work."),
            "evidence": [{"table": "project_rec_info", "reference": f"project_id={project_id}",
                          "fields": {"recommendation_date": None}}],
        })
        edges.append({"from": "age", "to": outcome_id, "relation": "evidence gap",
                      "strength": 0.3,
                      "explanation": "Age-based indicators could not be computed."})

    # Rule-derived reasons — these are the scoring engine's own indicators.
    for idx, reason in enumerate(reasons):
        node_id = f"reason_{idx}"
        add_node(
            node_id, "indicator", reason,
            (f"Stored risk reason applied by the existing scoring engine: \u201c{reason}\u201d. "
             "This is the rule that raised the flag, not an established cause."),
            "contributing indicator",
            [{"table": "risk_scores", "reference": f"project_id={project_id}",
              "fields": {"reasons": reason, "risk_score": risk.risk_score if risk else None}}],
            strength=0.7,
        )

    # Vendor concentration
    if top_vendor:
        strength = min(1.0, (top_share or 0))
        add_node(
            "vendor", "indicator",
            f"Top vendor share {top_share * 100:.0f}%",
            (f"{top_vendor} received {_money(_num(vendor_rows[0][1]))} of "
             f"{_money(vendor_total)} recorded on this work key across "
             f"{int(_num(vendor_rows[0][2]) or 0)} transaction(s). Concentration is a "
             "pattern worth checking, not a finding."),
            "correlated deviation",
            [{"table": "expenditures", "reference": f"vendor={top_vendor}",
              "fields": {"paid": _num(vendor_rows[0][1]),
                         "transactions": int(_num(vendor_rows[0][2]) or 0),
                         "first": str(vendor_rows[0][3])[:10],
                         "last": str(vendor_rows[0][4])[:10]}}],
            strength=strength,
            limitation="Vendor names are recorded as written; variants can overstate or hide concentration.",
        )
    elif exp > 0:
        nodes.append({
            "id": "vendor", "kind": "gap", "role": "driver",
            "label": "Vendor names not recorded",
            "detail": ("Payments exist for this work key but carry no vendor name, so vendor "
                       "concentration cannot be assessed."),
            "evidence": [{"table": "expenditures", "reference": "linked work key",
                          "fields": {"vendor": None}}],
        })
        edges.append({"from": "vendor", "to": outcome_id, "relation": "evidence gap",
                      "strength": 0.25,
                      "explanation": "Vendor pattern analysis is not possible for this work."})

    # History availability
    if snapshot_count or change_count:
        add_node(
            "history", "record",
            f"{snapshot_count} snapshot(s), {change_count} change event(s)",
            ("Stored import history exists for this project; progress and value changes "
             "across imports are available in Project Change Detection."),
            "context",
            [{"table": "project_snapshots", "reference": f"project_id={project_id}",
              "fields": {"snapshots": snapshot_count}},
             {"table": "project_change_events", "reference": f"project_id={project_id}",
              "fields": {"events": change_count}}],
            strength=0.2,
        )
    else:
        nodes.append({
            "id": "history", "kind": "gap", "role": "driver",
            "label": "No import history recorded",
            "detail": ("This project has no stored snapshot or change event, so historical "
                       "progress change cannot contribute to the explanation. The analysis "
                       "uses the current recorded values only."),
            "evidence": [{"table": "project_snapshots", "reference": f"project_id={project_id}",
                          "fields": {"snapshots": 0}}],
        })
        edges.append({"from": "history", "to": outcome_id, "relation": "evidence gap",
                      "strength": 0.35,
                      "explanation": "Trend-based indicators are unavailable for this project."})

    limitations: List[str] = [
        "Stored risk reasons explain what the scoring engine reacted to; they are not "
        "an investigation conclusion.",
        "Peer comparisons are statistical and depend on how complete nearby records are.",
    ]
    if peer_count and peer_count < audit_intel.PEER_MIN_SAMPLE:
        limitations.append(f"Only {peer_count} comparable works were found — the peer comparison is unreliable.")
    if not reasons:
        limitations.append("No stored risk reasons exist for this project, so rule-level attribution is unavailable.")
    if comp is None:
        limitations.append("Completion percentage is not recorded for this project.")

    confidence = "high" if (reasons and comp is not None) else ("medium" if reasons or comp is not None else "low")

    return {
        "project_id": project_id,
        "project": {
            "id": project_id, "name": project.project_name, "state": project.state,
            "district": project.district, "constituency": project.constituency,
            "house": project.house, "category": project.project_type,
            "status": project.status, "sanctioned": sanc, "linked_expenditure": exp,
        },
        "outcome": {"risk_level": risk_level,
                    "risk_score": risk.risk_score if risk is not None else None,
                    "ml_anomaly": ml_anomaly,
                    "flagged": risk_level in ("High", "Critical") or ml_anomaly},
        "nodes": nodes,
        "edges": edges,
        "drivers": [n for n in nodes if n.get("role") == "driver" and n.get("kind") != "gap"],
        "gaps": [n for n in nodes if n.get("kind") == "gap"],
        "confidence": confidence,
        "peer": {"count": peer_count, "reliability": peer_reliability},
        "causation_note": ROOT_CAUSE_NOTE,
        "limitations": limitations,
    }


# ══════════════════════════════════════════════════════════════════════
# FEATURE 2 — Emerging risk discovery
# ══════════════════════════════════════════════════════════════════════

def _current_indicators(db: Session, filters: Dict[str, Any]) -> Dict[str, Any]:
    """Point-in-time indicator set from current recorded values. Labelled
    'baseline' everywhere it is returned — it is NOT a trend."""
    clauses, params = _scope_clauses(filters)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    # SQLAlchemy text() needs named params; build them explicitly.
    named = {f"f{i}": v for i, v in enumerate(params)}
    for i, clause in enumerate(clauses):
        where = where.replace("?", f":f{i}", 1)
    rows = _rows(db, f"""
        SELECT COUNT(*) AS projects,
               SUM(CASE WHEN p.completion_percentage IS NULL OR p.completion_percentage < 1
                        THEN 1 ELSE 0 END) AS no_progress,
               SUM(CASE WHEN r.linked_expenditure > 0
                         AND (p.completion_percentage IS NULL OR p.completion_percentage < 1)
                        THEN 1 ELSE 0 END) AS spend_no_progress,
               AVG(p.completion_percentage) AS avg_completion,
               SUM(CASE WHEN rs.risk_level IN ('High','Critical') THEN 1 ELSE 0 END) AS high_risk,
               SUM(CASE WHEN rs.ml_anomaly = 1 THEN 1 ELSE 0 END) AS anomalies,
               SUM(r.linked_expenditure) AS expenditure,
               SUM(p.sanctioned_amount) AS sanctioned
        FROM projects p
        LEFT JOIN project_rec_info r ON r.project_id = p.id
        LEFT JOIN risk_scores rs ON rs.project_id = p.id
        {where}
    """, named)
    row = rows[0] if rows else None
    projects = int(_num(row[0]) or 0) if row else 0
    out = {
        "projects": projects,
        "spend_no_progress": int(_num(row[2]) or 0) if row else 0,
        "spend_no_progress_pct": round((_num(row[2]) or 0) / projects * 100, 2) if projects else None,
        "avg_completion": round(_num(row[3]) or 0, 2) if row else None,
        "high_risk": int(_num(row[4]) or 0) if row else 0,
        "high_risk_pct": round((_num(row[4]) or 0) / projects * 100, 2) if projects else None,
        "anomalies": int(_num(row[5]) or 0) if row else 0,
        "expenditure": _num(row[6]) if row else None,
        "sanctioned": _num(row[7]) if row else None,
        "utilization_pct": (round((_num(row[6]) or 0) / (_num(row[7]) or 1) * 100, 2)
                            if row and (_num(row[7]) or 0) > 0 else None),
    }
    # Emerging patterns visible in the CURRENT data, stated as such.
    patterns: List[Dict[str, Any]] = []
    if out["spend_no_progress"]:
        patterns.append({
            "key": "spend_no_progress",
            "label": "Payments recorded with no reported progress",
            "value": out["spend_no_progress"],
            "share_pct": out["spend_no_progress_pct"],
            "meaning": ("These works carry linked payments while reported completion stays "
                        "below 1%. This is the strongest current execution-risk indicator "
                        "that needs no history to observe."),
        })
    if out["high_risk_pct"] is not None and out["high_risk_pct"] > 0:
        patterns.append({
            "key": "high_risk_share",
            "label": "Share of portfolio flagged High risk",
            "value": out["high_risk"],
            "share_pct": out["high_risk_pct"],
            "meaning": "Stored risk level, applied by the existing scoring engine.",
        })
    if out["utilization_pct"] is not None and out["utilization_pct"] < 40:
        patterns.append({
            "key": "low_portfolio_utilization",
            "label": "Portfolio utilization below 40%",
            "value": out["utilization_pct"],
            "share_pct": None,
            "meaning": "Linked payments against sanctioned amount for the selected scope.",
        })
    return {"metrics": out, "patterns": patterns}


def compare_indicator_series(series: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Pure comparison of consecutive import indicator sets.

    `series` is ordered oldest → newest with keys:
    batch_id, captured_at, projects, spend_no_progress, high_risk,
    anomalies, avg_completion, expenditure, sanctioned, signatures (set/list
    of anomaly pattern keys), projects_flagged (set/list of project ids).

    Returns emerging / persistent / resolved patterns plus per-metric deltas.
    No data is invented: an indicator missing on either side is skipped and
    reported in `unavailable`.
    """
    if len(series) < 2:
        return {"available": False, "reason": "fewer than two comparable imports"}
    prev, cur = series[-2], series[-1]
    metrics = [
        ("spend_no_progress", "Payments with no progress", False),
        ("high_risk", "High-risk projects", False),
        ("anomalies", "ML anomalies", False),
        ("avg_completion", "Average completion %", True),
        ("expenditure", "Recorded expenditure", False),
        ("sanctioned", "Sanctioned amount", False),
    ]
    deltas: List[Dict[str, Any]] = []
    unavailable: List[str] = []
    for key, label, inverted in metrics:
        a, b = _num(prev.get(key)), _num(cur.get(key))
        if a is None or b is None:
            unavailable.append(key)
            continue
        delta = b - a
        pct = (delta / a * 100) if a else None
        deltas.append({
            "metric": key, "label": label, "previous": a, "current": b,
            "delta": round(delta, 2),
            "delta_pct": round(pct, 2) if pct is not None else None,
            "direction": "up" if delta > 0 else ("down" if delta < 0 else "flat"),
            "adverse": (delta < 0) if inverted else (delta > 0),
        })

    prev_sigs = set(prev.get("signatures") or [])
    cur_sigs = set(cur.get("signatures") or [])
    new_patterns = sorted(cur_sigs - prev_sigs)
    resolved = sorted(prev_sigs - cur_sigs)
    persistent = sorted(cur_sigs & prev_sigs)

    def _projects(key: str) -> List[int]:
        return sorted(set(cur.get("projects_flagged") or []) - set(prev.get("projects_flagged") or []))[:20]

    return {
        "available": True,
        "from_batch": prev.get("batch_id"),
        "to_batch": cur.get("batch_id"),
        "from_at": prev.get("captured_at"),
        "to_at": cur.get("captured_at"),
        "deltas": deltas,
        "new_patterns": new_patterns,
        "persistent_patterns": persistent,
        "resolved_patterns": resolved,
        "newly_flagged_projects": _projects(cur.get("batch_id")),
        "unavailable_metrics": unavailable,
        "worsening": [d for d in deltas if d["adverse"]],
    }


def _history_batches(db: Session) -> List[Dict[str, Any]]:
    rows = _rows(db, """
        SELECT batch_id, MIN(captured_at) AS captured_at, COUNT(*) AS rows,
               AVG(completion_percentage) AS avg_completion,
               SUM(expenditure) AS expenditure,
               COUNT(DISTINCT project_id) AS projects
        FROM project_snapshots
        GROUP BY batch_id ORDER BY captured_at ASC
    """)
    out = []
    for r in rows:
        bid = r[0]
        risk = _rows(db, """
            SELECT COUNT(*) FROM project_change_events
            WHERE batch_id = :b AND field = 'risk_level'
        """, {"b": bid})
        events = _rows(db, """
            SELECT COUNT(*) FROM project_change_events WHERE batch_id = :b
        """, {"b": bid})
        out.append({
            "batch_id": bid, "captured_at": str(r[1])[:19], "rows": int(_num(r[2]) or 0),
            "projects": int(_num(r[5]) or 0),
            "avg_completion": round(_num(r[3]) or 0, 2),
            "expenditure": _num(r[4]),
            "change_events": int(_num(events[0][0]) or 0) if events else 0,
            "risk_level_changes": int(_num(risk[0][0]) or 0) if risk else 0,
        })
    return out


def emerging_risk(db: Session, filters: Dict[str, Any]) -> Dict[str, Any]:
    current = _current_indicators(db, filters)
    batches = _history_batches(db)

    payload: Dict[str, Any] = {
        "mode": "current-baseline",
        "filters": filters,
        "current": current["metrics"],
        "patterns": current["patterns"],
        "history": {
            "batches": len(batches),
            "batch_list": batches[-12:],
            "comparable": len(batches) >= 2,
        },
        "emerging": None,
        "new_vs_persistent": None,
        "notes": [
            "Emerging-risk discovery compares successive dataset imports. Comparison requires "
            "at least two committed imports that captured per-project snapshots.",
            "The indicator set shown is a point-in-time baseline computed from current records — "
            "it is not described as an emerging trend.",
        ],
    }

    if len(batches) >= 2:
        series = []
        for b in batches[-6:]:
            rows = _rows(db, """
                SELECT COUNT(*) AS projects,
                       SUM(CASE WHEN completion_percentage IS NULL OR completion_percentage < 1
                                THEN 1 ELSE 0 END) AS no_progress,
                       AVG(completion_percentage) AS avg_completion,
                       SUM(expenditure) AS expenditure,
                       COUNT(DISTINCT project_id) AS ids
                FROM project_snapshots WHERE batch_id = :b
            """, {"b": b["batch_id"]})
            r = rows[0] if rows else None
            series.append({
                "batch_id": b["batch_id"], "captured_at": b["captured_at"],
                "projects": int(_num(r[2]) or 0) if r else 0,
                "avg_completion": _num(r[3]) if r else None,
                "expenditure": _num(r[4]) if r else None,
                # risk counts are only meaningful where the import captured them
                "signatures": [f"batch:{b['batch_id']}"],
            })
        payload["mode"] = "import-comparison"
        payload["emerging"] = {"available": True, "series": series}
        payload["new_vs_persistent"] = compare_indicator_series(series)
    return payload


# ══════════════════════════════════════════════════════════════════════
# FEATURE 7 — Anomaly pattern evolution
# ══════════════════════════════════════════════════════════════════════

def anomaly_evolution(db: Session, filters: Dict[str, Any]) -> Dict[str, Any]:
    """How stored anomaly patterns change across imports.

    Recorded vs reconstructed is stated explicitly:
      • 'recorded'      — values captured by an import (project_snapshots /
                          project_change_events written at import time);
      • 'reconstructed' — the pattern re-derived now from current records
                          (only used for the baseline row, clearly labelled);
      • 'unavailable'   — no import history exists at all.
    """
    batches = _history_batches(db)
    current = _current_indicators(db, filters)

    rows: List[Dict[str, Any]] = []
    # Baseline: current recorded values, explicitly reconstructed-now.
    rows.append({
        "batch_id": None,
        "label": "Current records",
        "captured_at": None,
        "mode": "reconstructed",
        "projects": current["metrics"]["projects"],
        "avg_completion": current["metrics"]["avg_completion"],
        "high_risk": current["metrics"]["high_risk"],
        "anomalies": current["metrics"]["anomalies"],
        "expenditure": current["metrics"]["expenditure"],
    })
    for b in batches:
        rows.insert(0, {
            "batch_id": b["batch_id"], "label": f"Import {b['batch_id']}",
            "captured_at": b["captured_at"], "mode": "recorded",
            "projects": b["projects"], "avg_completion": b["avg_completion"],
            "high_risk": None, "anomalies": None, "expenditure": b["expenditure"],
        })

    pattern_rows = _rows(db, """
        SELECT batch_id, COUNT(*) FROM project_change_events
        WHERE field IN ('risk_level','risk_score')
        GROUP BY batch_id ORDER BY batch_id
    """)
    signatures_by_batch = {}
    for r in pattern_rows:
        signatures_by_batch[int(r[0])] = {"risk_changes": int(_num(r[1]) or 0)}

    geo_rows = _rows(db, """
        SELECT batch_id, state, COUNT(*) FROM project_change_events
        WHERE change_type IN ('risk_upgrade','anomaly') OR field = 'risk_level'
        GROUP BY batch_id, state ORDER BY COUNT(*) DESC LIMIT 40
    """)
    geo_by_batch: Dict[int, List[Dict[str, Any]]] = {}
    for bid, state, n in geo_rows:
        geo_by_batch.setdefault(int(bid), []).append({"state": state, "changes": int(n)})

    return {
        "mode": "recorded" if len(batches) >= 1 else "unavailable",
        "series": rows,
        "batches": len(batches),
        "patterns_by_batch": [{"batch_id": k, **v} for k, v in sorted(signatures_by_batch.items())],
        "geography_by_batch": [{"batch_id": k, "states": v} for k, v in sorted(geo_by_batch.items())],
        "comparison": compare_indicator_series([
            {"batch_id": r["batch_id"], "captured_at": r["captured_at"],
             "projects": r["projects"], "avg_completion": r["avg_completion"],
             "expenditure": r["expenditure"], "signatures": []}
            for r in rows if r["batch_id"] is not None
        ]) if len(batches) >= 2 else {"available": False, "reason": "fewer than two imports"},
        "limitations": (
            [] if len(batches) >= 2 else [
                "No committed import has stored per-project snapshots yet, so import-to-import "
                "pattern evolution cannot be computed. The current-record row is labelled "
                "'reconstructed' and is not a historical result.",
                "Stored risk results are never re-created backwards in time: earlier anomaly "
                "levels were never recorded, so they are not shown.",
            ]
        ),
        "current": current["metrics"],
    }


# ══════════════════════════════════════════════════════════════════════
# FEATURE 3 — Project behaviour clustering
# ══════════════════════════════════════════════════════════════════════

CLUSTER_FEATURES = (
    "utilization_pct", "completion_pct", "exp_per_progress",
    "log_sanction", "age_months", "payment_activity",
)


def _cluster_dataset(db: Session, filters: Dict[str, Any], limit: int = 12000) -> List[Dict[str, Any]]:
    clauses, params = _scope_clauses(filters)
    where = (" AND " + " AND ".join(clauses)) if clauses else ""
    named = {f"f{i}": v for i, v in enumerate(params)}
    for i in range(len(clauses)):
        where = where.replace("?", f":f{i}", 1)
    rows = _rows(db, f"""
        SELECT p.id, p.state, p.district, p.house, p.project_type, p.project_name,
               p.sanctioned_amount, p.completion_percentage,
               r.linked_expenditure, r.linked_tx_count, r.recommendation_date
        FROM projects p
        JOIN project_rec_info r ON r.project_id = p.id
        WHERE p.sanctioned_amount > 0{where}
        ORDER BY p.id
        LIMIT :lim
    """, {**named, "lim": limit})
    out: List[Dict[str, Any]] = []
    today = None
    for r in rows:
        sanc = _num(r[6]) or 0.0
        exp = _num(r[8]) or 0.0
        comp = _num(r[7])
        util = (exp / sanc * 100.0) if sanc > 0 else None
        recd = str(r[10] or "")[:10]
        age = None
        if len(recd) == 10 and recd[4] == "-":
            try:
                from datetime import date
                if today is None:
                    today = date.today()
                age = max(0.0, (today - date(int(recd[:4]), int(recd[5:7]), int(recd[8:10]))).days / 30.4375)
            except Exception:
                age = None
        out.append({
            "id": int(r[0]), "state": r[1], "district": r[2], "house": r[3],
            "category": r[4], "name": r[5],
            "utilization_pct": util,
            "completion_pct": comp,
            "exp_per_progress": ((util or 0.0) / (comp + 1.0)) if util is not None else None,
            "log_sanction": math.log10(max(sanc, 1.0)),
            "age_months": age,
            "payment_activity": math.log10(1 + int(_num(r[9]) or 0)),
        })
    return out


def _label_cluster(profile: Dict[str, float], medians: Dict[str, float]) -> str:
    """Describe a cluster from its own centroid versus the global median.
    Nothing is hardcoded per cluster id — the text follows the numbers."""
    util, comp = profile.get("utilization_pct"), profile.get("completion_pct")
    util_hi = util is not None and util >= (medians.get("utilization_pct") or 0) * 1.25
    util_lo = util is not None and util <= (medians.get("utilization_pct") or 0) * 0.75
    comp_hi = comp is not None and comp >= (medians.get("completion_pct") or 0) + 20
    comp_lo = comp is not None and comp <= (medians.get("completion_pct") or 0) - 10
    if util_hi and comp_lo:
        return "High utilization · low progress"
    if util_hi and comp_hi:
        return "High utilization · high progress"
    if util_lo and comp_lo:
        return "Low utilization · low progress"
    if util_lo:
        return "Low utilization"
    if comp_lo:
        return "Low reported progress"
    if util_hi:
        return "High utilization"
    return "Consistent execution band"


def clusters(db: Session, filters: Dict[str, Any], k: Optional[int] = None) -> Dict[str, Any]:
    key = f"clusters|{filters.get('state')}|{filters.get('district')}|{filters.get('house')}|{filters.get('category')}|{k}"
    return _cached(key, lambda: _build_clusters(db, filters, k))


def _build_clusters(db: Session, filters: Dict[str, Any], k: Optional[int]) -> Dict[str, Any]:
    data = _cluster_dataset(db, filters)
    if len(data) < 30:
        return {"available": False,
                "reason": f"Only {len(data)} projects with linked payment records match this scope — "
                          "at least 30 are needed for a stable behavioural clustering.",
                "projects_considered": len(data)}

    try:
        import numpy as np
        from sklearn.cluster import KMeans
        from sklearn.metrics import silhouette_score
        from sklearn.preprocessing import StandardScaler
    except Exception:
        return {"available": False,
                "reason": "scikit-learn is unavailable in this deployment, so behaviour clustering is not enabled.",
                "projects_considered": len(data)}

    # Missing values → median imputation, counted and reported.
    imputed = {f: 0 for f in CLUSTER_FEATURES}
    filled: List[List[float]] = []
    columns = {f: [d[f] for d in data] for f in CLUSTER_FEATURES}
    med = {}
    for f in CLUSTER_FEATURES:
        vals = sorted(v for v in columns[f] if v is not None)
        med[f] = float(np.median(vals)) if vals else 0.0
    for d in data:
        row = []
        for f in CLUSTER_FEATURES:
            v = d[f]
            if v is None:
                imputed[f] += 1
                v = med[f]
            row.append(float(v))
        filled.append(row)

    X = np.asarray(filled, dtype=np.float32)
    Xs = StandardScaler().fit_transform(X)

    k_min, k_max = 3, 6
    if len(data) < 200:
        k_max = 3
    candidates = [k] if k else list(range(k_min, k_max + 1))
    sample_idx = np.linspace(0, len(Xs) - 1, min(1200, len(Xs))).astype(int)
    best = None
    for kk in candidates:
        try:
            km = KMeans(n_clusters=kk, n_init=4, random_state=42).fit(Xs)
            score = float(silhouette_score(Xs[sample_idx], km.labels_[sample_idx])) if kk > 1 else -1.0
        except Exception:
            continue
        if best is None or score > best[1]:
            best = (kk, score, km)
    if best is None:
        return {"available": False, "reason": "Clustering did not converge on this dataset."}

    kk, score, model = best
    labels = model.labels_
    medians = {f: float(np.median([d[f] for d in data if d[f] is not None] or [0])) for f in CLUSTER_FEATURES}

    out_clusters: List[Dict[str, Any]] = []
    for cid in range(kk):
        members = [d for d, lab in zip(data, labels) if int(lab) == cid]
        if not members:
            continue
        profile = {f: round(float(np.median([m[f] for m in members if m[f] is not None] or [0])), 2)
                   for f in ("utilization_pct", "completion_pct", "exp_per_progress",
                             "age_months", "log_sanction")}
        profile["payment_activity"] = round(float(np.median([m["payment_activity"] for m in members])), 2)
        label = _label_cluster(profile, medians)
        states: Dict[str, int] = {}
        cats: Dict[str, int] = {}
        for m in members:
            states[m["state"] or "Unknown"] = states.get(m["state"] or "Unknown", 0) + 1
            cats[m["category"] or "Unknown"] = cats.get(m["category"] or "Unknown", 0) + 1
        sanctions = sorted(m["log_sanction"] for m in members)
        out_clusters.append({
            "id": cid,
            "label": label,
            "size": len(members),
            "share_pct": round(len(members) / len(data) * 100, 1),
            "profile": profile,
            "description": (
                f"{len(members):,} projects ({len(members) / len(data) * 100:.1f}% of the clustered "
                f"sample). Median utilization {profile['utilization_pct']:.1f}% against a global median "
                f"of {medians['utilization_pct']:.1f}%, median reported progress "
                f"{profile['completion_pct']:.1f}% (global {medians['completion_pct']:.1f}%), median age "
                f"{profile['age_months']:.0f} months and median sanctioned "
                f"₹{10 ** profile['log_sanction']:,.0f}. The label is derived from these medians, "
                "not assigned in advance."
            ),
            "top_states": [{"state": s, "projects": n} for s, n in sorted(states.items(), key=lambda x: -x[1])[:5]],
            "top_categories": [{"category": c, "projects": n} for c, n in sorted(cats.items(), key=lambda x: -x[1])[:4]],
        })

    with _lock:
        _cluster_state.update({
            "key": "latest",
            "assignments": {d["id"]: int(lab) for d, lab in zip(data, labels)},
            "meta": {d["id"]: d for d in data},
        })

    return {
        "available": True,
        "method": "KMeans on standardised behavioural features; k chosen by silhouette score",
        "k": kk,
        "silhouette": round(score, 3),
        "quality": ("good" if score >= 0.5 else ("moderate" if score >= 0.25 else "weak")),
        "projects_considered": len(data),
        "features": list(CLUSTER_FEATURES),
        "features_used_note": (
            "Features are execution and financial behaviour only — project ids, names and "
            "vendors are never used as clustering inputs."
        ),
        "imputed_values": {k2: v for k2, v in imputed.items() if v},
        "global_medians": {k2: round(v, 2) for k2, v in medians.items()},
        "clusters": sorted(out_clusters, key=lambda c: -c["size"]),
        "limitations": [
            "Clusters describe recorded behaviour in this dataset; they are not performance grades.",
            "Cluster membership is statistical and is recomputed when the data changes.",
        ],
    }


def cluster_projects(cluster_id: int, limit: int = 50) -> Dict[str, Any]:
    with _lock:
        assignments = dict(_cluster_state.get("assignments") or {})
        meta = dict(_cluster_state.get("meta") or {})
    if not assignments:
        return {"available": False, "reason": "No clustering has been computed yet — open the cluster view first."}
    ids = [pid for pid, cid in assignments.items() if cid == int(cluster_id)][: max(1, min(limit, 200))]
    items = []
    for pid in ids:
        d = meta.get(pid) or {}
        items.append({
            "project_id": pid, "name": d.get("name"), "state": d.get("state"),
            "district": d.get("district"), "house": d.get("house"),
            "category": d.get("category"),
            "utilization_pct": round(d["utilization_pct"], 1) if d.get("utilization_pct") is not None else None,
            "completion_pct": d.get("completion_pct"),
            "age_months": round(d["age_months"], 1) if d.get("age_months") is not None else None,
        })
    return {"available": True, "cluster_id": cluster_id, "count": len(items), "projects": items}


# ══════════════════════════════════════════════════════════════════════
# FEATURE 4 — Cross-dataset reconciliation
# ══════════════════════════════════════════════════════════════════════

RECON_CHECKS = (
    ("completed_without_recommendation", "Completed work without a recommendation record"),
    ("recommended_without_completion", "Recommended work with no completion record"),
    ("duplicate_work_identity", "Same work recorded under multiple identities"),
    ("expenditure_without_linkage", "Payment rows with no reliable project linkage"),
    ("financial_conflict", "Conflicting financial amounts between datasets"),
    ("date_conflict", "Completion date earlier than recommendation date"),
    ("status_conflict", "Status says completed but progress is low"),
    ("sanction_variance", "Same work description with materially different sanctions"),
)


def reconciliation(db: Session, filters: Dict[str, Any], limit: int = 25) -> Dict[str, Any]:
    key = f"recon|{filters.get('state')}|{filters.get('district')}|{filters.get('house')}|{limit}"
    return _cached(key, lambda: _build_reconciliation(db, filters, limit))


def _case(check: str, title: str, confidence: str, explanation: str,
          records: List[Dict[str, Any]], *, severity: str = "medium",
          key_hint: str = "", review: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    import hashlib
    raw = f"{check}|{key_hint}".encode("utf-8", "ignore")
    return {
        "case_key": hashlib.sha1(raw).hexdigest()[:24],
        "check": check,
        "title": title,
        "severity": severity,
        "confidence": confidence,   # confirmed | probable | uncertain
        "explanation": explanation,
        "records": records,
        "review": review,
    }


def _review_map(db: Session) -> Dict[str, Dict[str, Any]]:
    rows = []
    try:
        rows = db.query(models.ReconciliationReview).all()
    except Exception:
        rows = []
    return {
        r.case_key: {"decision": r.decision, "note": r.note,
                     "reviewer": r.reviewer_name, "reviewed_at": r.reviewed_at}
        for r in rows
    }


def _build_reconciliation(db: Session, filters: Dict[str, Any], limit: int) -> Dict[str, Any]:
    state = filters.get("state")
    house = filters.get("house") if filters.get("house") in ("Lok Sabha", "Rajya Sabha") else None
    reviews = _review_map(db)
    cases: List[Dict[str, Any]] = []
    counts: Dict[str, int] = {}

    def scope(alias: str) -> Tuple[str, Dict[str, Any]]:
        bits, params = [], {}
        if state:
            bits.append(f"LOWER(TRIM({alias}.state)) = LOWER(:state)")
            params["state"] = state
        if house:
            bits.append(f"{alias}.house = :house")
            params["house"] = house
        return ((" AND " + " AND ".join(bits)) if bits else ""), params

    # 1. Completed works with no matching recommendation (exact work_id).
    w, p = scope("c")
    rows = _rows(db, f"""
        SELECT c.work_id, c.work_description, c.state, c.constituency, c.mp_name,
               c.final_amount, c.completed_date, c.house
        FROM completed_works c
        LEFT JOIN recommended_works r ON r.work_id = c.work_id
        WHERE c.work_id IS NOT NULL AND TRIM(c.work_id) <> ''
          AND r.work_id IS NULL{w}
        LIMIT :lim
    """, {**p, "lim": limit})
    counts["completed_without_recommendation"] = len(rows)
    for r in rows:
        cases.append(_case(
            "completed_without_recommendation",
            "Completed work has no recommendation record",
            "confirmed",
            (f"Work id {r[0]} appears in the completed-works dataset with a final amount of "
             f"{_money(r[5])} but no row with that id exists in recommended works. The identifier "
             "match is exact, so this is a dataset-coverage gap rather than a fuzzy guess."),
            [
                {"dataset": "completed_works", "reference": f"work_id={r[0]}",
                 "fields": {"description": r[1], "state": r[2], "constituency": r[3],
                            "mp": r[4], "final_amount": _num(r[5]),
                            "completed_date": str(r[6])[:10], "house": r[7]}},
                {"dataset": "recommended_works", "reference": f"work_id={r[0]}",
                 "fields": {"present": False}},
            ],
            key_hint=f"{r[0]}|{r[5]}",
        ))

    # 2. Recommended works with no completion record.
    w, p = scope("r")
    rows = _rows(db, f"""
        SELECT r.work_id, r.work_description, r.state, r.constituency, r.mp_name,
               r.recommended_amount, r.recommendation_date, r.house
        FROM recommended_works r
        LEFT JOIN completed_works c ON c.work_id = r.work_id
        WHERE r.work_id IS NOT NULL AND TRIM(r.work_id) <> ''
          AND c.work_id IS NULL{w}
        LIMIT :lim
    """, {**p, "lim": limit})
    counts["recommended_without_completion"] = len(rows)
    for r in rows:
        cases.append(_case(
            "recommended_without_completion",
            "Recommended work has no completion record",
            "probable",
            (f"Work id {r[0]} was recommended for {_money(r[5])} on {str(r[6])[:10]} but appears in "
             "no completed-works row. This normally means the work is still ongoing; it only "
             "indicates an inconsistency once the recorded status says otherwise."),
            [
                {"dataset": "recommended_works", "reference": f"work_id={r[0]}",
                 "fields": {"description": r[1], "recommended_amount": _num(r[5]),
                            "recommendation_date": str(r[6])[:10], "house": r[7]}},
                {"dataset": "completed_works", "reference": f"work_id={r[0]}",
                 "fields": {"present": False}},
            ],
            severity="low", key_hint=f"{r[0]}|{r[5]}",
        ))

    # 3. Duplicate work identity (normalised description + constituency).
    rows = _rows(db, """
        SELECT LOWER(TRIM(work_description)) AS d, LOWER(TRIM(constituency)) AS c,
               COUNT(DISTINCT work_id) AS ids, COUNT(*) AS rows,
               MIN(recommended_amount) AS lo, MAX(recommended_amount) AS hi,
               MIN(work_id) AS sample_id, MIN(state) AS st
        FROM recommended_works
        WHERE work_description IS NOT NULL AND TRIM(work_description) <> ''
        GROUP BY 1, 2 HAVING COUNT(DISTINCT work_id) > 1
        ORDER BY ids DESC LIMIT :lim
    """, {"lim": limit})
    counts["duplicate_work_identity"] = len(rows)
    for r in rows:
        cases.append(_case(
            "duplicate_work_identity",
            "Identical work description under several work ids",
            "probable",
            (f"The same normalised description appears in {int(_num(r[2]) or 0)} work ids in "
             f"{r[1] if r[1] else 'the recorded constituency'}. Recommended amounts range "
             f"{_money(r[4])}–{_money(r[5])}. Re-phasing or split sanctions can legitimately "
             "produce this; the records are shown side by side for a human decision."),
            [{"dataset": "recommended_works", "reference": f"description='{str(r[0])[:80]}'",
              "fields": {"distinct_work_ids": int(_num(r[2]) or 0), "rows": int(_num(r[3]) or 0),
                         "min_amount": _num(r[4]), "max_amount": _num(r[5]), "state": r[7]}}],
            severity="low", key_hint=f"{r[0]}|{r[1]}",
        ))

    # 4. Payments with no project linkage.
    n_unlinked = _scalar(db, """
        SELECT COUNT(*) FROM expenditures e
        WHERE NOT EXISTS (
            SELECT 1 FROM project_rec_info r WHERE r.work_key = normkey(e.work_description)
        )
    """) or 0
    counts["expenditure_without_linkage"] = n_unlinked
    if n_unlinked:
        sample = _rows(db, """
            SELECT e.work_description, e.state, e.constituency, e.vendor,
                   e.expenditure_amount, e.expenditure_date
            FROM expenditures e
            WHERE NOT EXISTS (
                SELECT 1 FROM project_rec_info r WHERE r.work_key = normkey(e.work_description)
            )
            ORDER BY e.expenditure_amount DESC LIMIT :lim
        """, {"lim": min(limit, 10)})
        cases.append(_case(
            "expenditure_without_linkage",
            "Payment rows cannot be linked to a project",
            "confirmed",
            (f"{n_unlinked:,} payment row(s) have no matching project linkage key. Their amounts "
             "therefore cannot be attributed to a specific project record. These rows are left "
             "untouched — no automatic matching is performed."),
            [{"dataset": "expenditures", "reference": "unlinked rows",
              "fields": {"count": n_unlinked,
                         "examples": [
                             {"description": str(s[0])[:90], "state": s[1], "vendor": s[3],
                              "amount": _num(s[4]), "date": str(s[5])[:10]}
                             for s in sample
                         ]}}],
            severity="medium", key_hint="unlinked-expenditure",
        ))

    # 5. Financial conflict: registry total vs linked ledger total.
    rows = _rows(db, """
        SELECT p.id, p.project_name, p.state, p.sanctioned_amount,
               r.linked_expenditure, r.work_key_expenditure, r.work_key_rows
        FROM projects p JOIN project_rec_info r ON r.project_id = p.id
        WHERE r.work_key_expenditure > 0 AND r.linked_expenditure > 0
          AND ABS(r.work_key_expenditure - r.linked_expenditure) > 1
          AND ABS(r.work_key_expenditure - r.linked_expenditure)
              > 0.05 * MAX(r.linked_expenditure, 1)
        ORDER BY ABS(r.work_key_expenditure - r.linked_expenditure) DESC LIMIT :lim
    """, {"lim": min(limit, 15)})
    counts["financial_conflict"] = len(rows)
    for r in rows:
        cases.append(_case(
            "financial_conflict",
            "Two recorded expenditure totals disagree",
            "confirmed",
            (f"The work-key total recorded for project #{r[0]} is {_money(r[5])} while the linked "
             f"project total is {_money(r[4])}. Both are stored values; the difference is reported, "
             "not resolved automatically."),
            [
                {"dataset": "project_rec_info", "reference": f"project_id={r[0]}",
                 "fields": {"work_key_expenditure": _num(r[5]), "work_key_rows": r[6]}},
                {"dataset": "projects", "reference": f"id={r[0]}",
                 "fields": {"sanctioned_amount": _num(r[3]), "linked_expenditure": _num(r[4])}},
            ],
            key_hint=f"{r[0]}|{r[4]}|{r[5]}",
        ))

    # 6. Date conflict: completed before recommended.
    rows = _rows(db, """
        SELECT c.work_id, c.work_description, c.completed_date, r.recommendation_date,
               c.state, c.final_amount
        FROM completed_works c JOIN recommended_works r ON r.work_id = c.work_id
        WHERE c.completed_date IS NOT NULL AND r.recommendation_date IS NOT NULL
          AND date(c.completed_date) < date(r.recommendation_date)
        LIMIT :lim
    """, {"lim": min(limit, 15)})
    counts["date_conflict"] = len(rows)
    for r in rows:
        cases.append(_case(
            "date_conflict",
            "Completion recorded before recommendation",
            "confirmed",
            (f"Work id {r[0]} is recorded as completed on {str(r[2])[:10]} but recommended on "
             f"{str(r[3])[:10]}. At least one date is wrong in source data."),
            [{"dataset": "completed_works", "reference": f"work_id={r[0]}",
              "fields": {"completed_date": str(r[2])[:10], "final_amount": _num(r[5])}},
             {"dataset": "recommended_works", "reference": f"work_id={r[0]}",
              "fields": {"recommendation_date": str(r[3])[:10]}}],
            severity="high", key_hint=f"{r[0]}|date",
        ))

    # 7. Status conflict: completed but low progress.
    w, p = scope("p")
    rows = _rows(db, f"""
        SELECT p.id, p.project_name, p.state, p.status, p.completion_percentage
        FROM projects p
        WHERE p.status = 'Completed'
          AND (p.completion_percentage IS NULL OR p.completion_percentage < 90){w}
        LIMIT :lim
    """, {**p, "lim": min(limit, 15)})
    counts["status_conflict"] = len(rows)
    for r in rows:
        cases.append(_case(
            "status_conflict",
            "Status says completed, progress does not",
            "confirmed",
            (f"Project #{r[0]} is marked '{r[3]}' but its recorded physical progress is "
             f"{r[4] if r[4] is not None else 'not recorded'}%. Either the status or the progress "
             "value is stale."),
            [{"dataset": "projects", "reference": f"id={r[0]}",
              "fields": {"status": r[3], "completion_percentage": _num(r[4]), "state": r[2]}}],
            key_hint=f"{r[0]}|status",
        ))

    # 8. Sanction variance for the same normalised identity.
    rows = _rows(db, """
        SELECT LOWER(TRIM(project_name)) AS d, LOWER(TRIM(constituency)) AS c,
               COUNT(*) AS n, MIN(sanctioned_amount) AS lo, MAX(sanctioned_amount) AS hi,
               MIN(state) AS st
        FROM projects
        WHERE sanctioned_amount > 0 AND project_name IS NOT NULL
        GROUP BY 1, 2
        HAVING COUNT(*) > 1 AND MAX(sanctioned_amount) > 1.2 * MIN(sanctioned_amount)
        ORDER BY (MAX(sanctioned_amount) - MIN(sanctioned_amount)) DESC LIMIT :lim
    """, {"lim": min(limit, 10)})
    counts["sanction_variance"] = len(rows)
    for r in rows:
        cases.append(_case(
            "sanction_variance",
            "Identical project name with materially different sanctions",
            "uncertain",
            (f"{int(_num(r[2]) or 0)} project rows share the normalised name "
             f"'{str(r[0])[:70]}' in {r[1] or 'the same constituency'} with sanctions from "
             f"{_money(r[3])} to {_money(r[4])}. Identical names are not proof of duplicate works — "
             "the rows are presented for review, never merged."),
            [{"dataset": "projects", "reference": f"name='{str(r[0])[:70]}'",
              "fields": {"rows": int(_num(r[2]) or 0), "min": _num(r[3]), "max": _num(r[4]), "state": r[5]}}],
            severity="low", key_hint=f"{r[0]}|{r[1]}",
        ))

    for c in cases:
        c["review"] = reviews.get(c["case_key"])

    return {
        "available": True,
        "scope": {"state": state, "house": house},
        "checks": [{"key": k, "label": v, "found": counts.get(k, 0)} for k, v in RECON_CHECKS],
        "counts": counts,
        "cases": cases,
        "reviewed": sum(1 for c in cases if c.get("review")),
        "notes": [
            "Exact identifiers (work_id) are used first; normalised text is used only to surface "
            "candidates, always labelled 'probable' or 'uncertain'.",
            "No record is merged, edited or deleted. Uncertain matches require human review.",
            "A discrepancy is a data-quality signal, not evidence of financial irregularity.",
        ],
        "limitations": [
            "MP summary totals are house-level and are not reconciled per project here.",
            "Fuzzy text matching is deliberately conservative and can miss spelling variants.",
        ],
    }


def review_reconciliation(db: Session, case_key: str, decision: str,
                          note: Optional[str], user) -> Dict[str, Any]:
    row = (db.query(models.ReconciliationReview)
           .filter(models.ReconciliationReview.case_key == case_key).first())
    if row is None:
        row = models.ReconciliationReview(case_key=case_key, created_at=_now_iso())
        db.add(row)
    row.decision = decision
    row.note = (note or "")[:2000] or None
    row.reviewer_user_id = getattr(user, "id", None)
    row.reviewer_name = getattr(user, "name", None)
    row.reviewed_at = _now_iso()
    db.commit()
    # A decision must be reflected by the next reconciliation read.
    reset_cache()
    return {"case_key": case_key, "decision": row.decision, "note": row.note,
            "reviewer": row.reviewer_name, "reviewed_at": row.reviewed_at}


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
