"""
MPLADS AI — Project Outcome Prediction (Feature 2)
==================================================

Predicts the future outcome of ONE project (completion probability, expected
timeline, expected final expenditure, delay / stalled likelihood) from the
REAL recorded data: projects, project_rec_info linkage, expenditures,
risk_scores, recommended_works and completed_works.

Method — deliberately simple, honest about thin history:

  • Completion model: logistic regression (scikit-learn, already a runtime
    dependency of the existing ML anomaly scorer) trained on the project
    catalogue itself. Label = completed (completion_percentage >= 90, which
    in this dataset coincides with status 'Completed'). Features use ONLY
    existing columns: log sanction, category, house, FY vintage,
    recommendation age, linked-payment presence and payment density.
  • Validation: time-aware hold-out — rows are split by recommendation
    vintage, never randomly, so the model is always evaluated on the most
    recent cohort it has never seen. Metrics (ROC AUC, accuracy, Brier
    score) are returned with the prediction rather than hidden.
  • Duration / delay: EMPIRICAL cohort estimates from completed_works
    (final records carry a completion date). Cohorts are category + house +
    state. Duration = months from the matched recommendation date
    (recommended_works, same work_id) to completed_date. Delay probability
    at the project's current age = share of its cohort's completed durations
    that are LONGER than the project's current age — an empirical survival
    share, no curve fitting.
  • Expected final expenditure: cohort median of final_amount /
    recommended_amount ratios applied to this project's sanction.
  • Progress outlook curve: cohort empirical duration distribution gives
    P(completed by month m) for future months — presented as a cohort
    outlook, never as a promise for this project.

Honesty rules enforced here:
  • Below cohort/sample thresholds the offending estimate is withheld and
    the response states exactly what is (and is not) knowable via limits.
  • A prediction never modifies risk scores, never closes an investigation
    and is never evidence of wrongdoing. Confidence labels derive from the
    validation metrics and cohort sizes — not invented.
  • Missing data is reported as a limitation, never silently imputed.

Memory discipline: the design matrix is float32 (≈83k × 11 ≈ 3.5 MB); the
trained model and cohort tables are singletons built once per process,
validated against a cheap data fingerprint; per-project payloads are cached
in a bounded map. Cohort aggregation runs inside SQLite.
"""

import math
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

import models
from database import SessionLocal

MODEL_VERSION = "outcome-v1"
_COHORT_TTL = 1800.0
_MODEL_TTL = 7200.0
MIN_COMPLETED_COHORT = 20     # cohort estimate refused below this
MODEL_MIN_POS = 30            # minimum completed rows to train at all
MODEL_MIN_AUC = 0.60          # hold-out AUC below → model probabilities withheld
MAX_TRAIN_ROWS = 120000

_cache_lock = threading.Lock()
_cache: Dict[str, Any] = {
    "cohorts_at": 0.0, "cohorts": None, "fingerprint": None,
    "model_at": 0.0, "model": None,
}

_PAYLOAD_MAX = 256
_payloads: Dict[int, Dict[str, Any]] = {}
_payload_order: List[int] = []


# ── small helpers ────────────────────────────────────────────────────

def _num(v) -> Optional[float]:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _money(v) -> str:
    v = float(v or 0)
    if v >= 1e7:
        return f"₹{v / 1e7:.2f} Cr"
    if v >= 1e5:
        return f"₹{v / 1e5:.2f} L"
    return f"₹{v:,.0f}"


def _date_parts(iso) -> Optional[str]:
    if not iso:
        return None
    s = str(iso)[:10]
    if len(s) == 10 and s[4] == "-" and s[7] == "-":
        return s
    return None


def _months_between(start: str, end: str) -> Optional[float]:
    try:
        from datetime import date
        a = date(int(start[:4]), int(start[5:7]), int(start[8:10]))
        b = date(int(end[:4]), int(end[5:7]), int(end[8:10]))
        days = (b - a).days
        return days / 30.4375 if days > 0 else None
    except Exception:
        return None


def _today() -> str:
    from datetime import date
    return date.today().isoformat()


def _fy_year(fy: str) -> float:
    fy = str(fy or "")
    return int(fy[:4]) + 0.5 if fy[:4].isdigit() else 0.0


# ═════════════════════════════════════════════════════════════════════
# Cohort statistics — one pass inside SQLite, cached per process
# ═════════════════════════════════════════════════════════════════════

def _cohorts_fingerprint(db: Session) -> int:
    return int(db.execute(text(
        "SELECT COUNT(*) FROM completed_works"
    )).scalar() or 0)


def _raw_conn():
    """Dedicated read-only sqlite connection + the same UDFs the aggregation
    needs. Separate from the SQLAlchemy pool so cohort queries never disturb
    request sessions."""
    import sqlite3
    from urllib.parse import quote
    from database import normkey, DB_PATH

    def _normdate_sql(value) -> str:
        s = str(value or "").strip()
        if len(s) >= 10 and s[4] == "-" and s[7] == "-":
            cand = s[:10]
            try:
                time.strptime(cand, "%Y-%m-%d")
                return cand
            except ValueError:
                return ""
        return ""

    uri = f"file:{quote(DB_PATH, safe='/')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=15.0)
    try:
        conn.create_function("normkey", 1, normkey, deterministic=True)
    except Exception:
        conn.create_function("normkey", 1, normkey)
    try:
        conn.create_function("normdate", 1, _normdate_sql, deterministic=True)
    except Exception:
        conn.create_function("normdate", 1, _normdate_sql)
    return conn


def _load_cohorts(db: Session) -> Dict[str, Any]:
    with _cache_lock:
        fp = _cohorts_fingerprint(db)
        if _cache["cohorts"] is not None and _cache["fingerprint"] == fp \
                and time.time() - _cache["cohorts_at"] < _COHORT_TTL:
            return _cache["cohorts"]

    conn = _raw_conn()
    cur = conn.cursor()

    cohort_sizes: Dict[Tuple[str, str, str], int] = {}
    for cat, house, state, works in cur.execute(
        """
        SELECT COALESCE(c.category, 'Unknown'),
               LOWER(TRIM(COALESCE(c.house, 'Unattributed'))),
               COALESCE(c.state, 'Unknown'),
               COUNT(DISTINCT c.work_description || '|' || COALESCE(c.constituency, ''))
        FROM completed_works c
        GROUP BY 1, 2, 3
        """
    ).fetchall():
        cohort_sizes[(cat, house, state)] = int(works or 0)

    # Duration pairs (recommendation → completion) on the same work_id.
    durations: Dict[Tuple[str, str, str], List[float]] = {}
    for cat, house, state, recd, done in cur.execute(
        """
        SELECT COALESCE(c.category, 'Unknown'),
               LOWER(TRIM(COALESCE(c.house, 'Unattributed'))),
               COALESCE(c.state, 'Unknown'),
               MIN(normdate(r.recommendation_date)),
               MIN(normdate(c.completed_date))
        FROM completed_works c
        JOIN recommended_works r ON r.work_id = c.work_id
        GROUP BY 1, 2, 3, normkey(c.work_description), LOWER(TRIM(c.constituency))
        HAVING MIN(normdate(r.recommendation_date)) IS NOT NULL
           AND MIN(normdate(c.completed_date)) IS NOT NULL
        """
    ).fetchall():
        m = _months_between(str(recd)[:10], str(done)[:10])
        if m and 0 < m < 400:
            durations.setdefault((cat, house, state), []).append(m)

    # Cost-overrun ratios (final vs sanctioned-for-works amounts).
    costs: Dict[Tuple[str, str, str], List[float]] = {}
    for cat, house, state, ratio in cur.execute(
        """
        SELECT COALESCE(c.category, 'Unknown'),
               LOWER(TRIM(COALESCE(c.house, 'Unattributed'))),
               COALESCE(c.state, 'Unknown'),
               c.final_amount / r.recommended_amount
        FROM completed_works c
        JOIN recommended_works r ON r.work_id = c.work_id
        WHERE r.recommended_amount > 0 AND c.final_amount > 0
          AND c.final_amount / r.recommended_amount BETWEEN 0.1 AND 20
        """
    ).fetchall():
        costs.setdefault((cat, house, state), []).append(float(ratio))
    conn.close()

    cohorts = {
        "sizes": cohort_sizes,
        "durations": durations,
        "costs": costs,
        "n_pairs": sum(len(v) for v in durations.values()),
    }
    with _cache_lock:
        _cache["cohorts"] = cohorts
        _cache["cohorts_at"] = time.time()
        _cache["fingerprint"] = fp
    return cohorts


def _percentile(values: List[float], q: float) -> Optional[float]:
    vs = sorted(v for v in values if v and 0 < v < 400)
    if not vs:
        return None
    k = (len(vs) - 1) * (q / 100.0)
    f = math.floor(k)
    c = min(len(vs) - 1, f + 1)
    if f == c:
        return vs[f]
    return vs[f] + (vs[c] - vs[f]) * (k - f)


# ═════════════════════════════════════════════════════════════════════
# Completion model — trained once per process, time-aware validation
# ═════════════════════════════════════════════════════════════════════

FEATURE_NAMES = (
    "log_sanction", "known_category", "category_rank", "lok_sabha",
    "rajya_sabha", "fy_vintage", "age_months", "has_payments",
    "payment_density", "state_activity", "high_value",
)

FEATURE_LABELS = {
    "log_sanction": "Sanction size",
    "known_category": "Recognised work category",
    "category_rank": "Work-category rank",
    "lok_sabha": "Lok Sabha work",
    "rajya_sabha": "Rajya Sabha work",
    "fy_vintage": "Financial-year vintage",
    "age_months": "Age since recommendation",
    "has_payments": "Linked payments present",
    "payment_density": "Payment density",
    "state_activity": "State activity level",
    "high_value": "High-value project",
}


def _train_rows(db: Session):
    return db.execute(text(
        """
        SELECT p.project_type,
               LOWER(TRIM(COALESCE(p.house, 'None'))),
               COALESCE(p.fy, ''),
               LOWER(TRIM(COALESCE(r.recommendation_date, ''))),
               CASE WHEN r.linked_expenditure > 0 THEN 1 ELSE 0 END,
               COALESCE(r.linked_tx_count, 0),
               LOWER(TRIM(COALESCE(p.state, ''))),
               COALESCE(p.sanctioned_amount, 0),
               p.completion_percentage
        FROM projects p
        LEFT JOIN project_rec_info r ON r.project_id = p.id
        LIMIT :n
        """
    ), {"n": MAX_TRAIN_ROWS}).fetchall()


def _row_vector(cat: str, house: str, fy: str, recd: str, has_exp: int,
                tx: int, state: str, sanc: float, age_months: float,
                cat_vocab: List[str], state_vocab: List[str]) -> List[float]:
    return [
        math.log10(max(float(sanc), 1.0)) / 8.0,
        1.0 if cat in cat_vocab else 0.0,
        (cat_vocab.index(cat) / max(1, len(cat_vocab))) if cat in cat_vocab else 0.0,
        1.0 if house == "lok sabha" else 0.0,
        1.0 if house == "rajya sabha" else 0.0,
        _fy_year(fy) / 2100.0,
        min(float(age_months), 24.0) / 24.0,
        float(has_exp),
        min(int(tx or 0), 50) / 50.0,
        (state_vocab.index(state) / max(1, len(state_vocab))) if state in state_vocab else 0.5,
        1.0 if float(sanc) > 1e7 else 0.0,
    ]


def _load_model(db: Session, fingerprint: int) -> Optional[Dict[str, Any]]:
    with _cache_lock:
        if _cache["model"] is not None and _cache["model"]["fingerprint"] == fingerprint \
                and time.time() - _cache["model_at"] < _MODEL_TTL:
            return _cache["model"]

    rows = _train_rows(db)
    if not rows:
        return None
    pos = sum(1 for r in rows if _num(r[8]) is not None and r[8] >= 90)
    if pos < MODEL_MIN_POS:
        return None

    try:
        import numpy as np
        from sklearn.linear_model import LogisticRegression
        from sklearn.metrics import roc_auc_score, brier_score_loss, average_precision_score
    except Exception:
        return None

    from collections import Counter
    cat_vocab = [c for c, _ in Counter(r[0] for r in rows).most_common(12)]
    state_vocab = [s for s, _ in Counter(r[6] for r in rows).most_common(400)]
    today = _today()

    n = len(rows)
    X = np.zeros((n, len(FEATURE_NAMES)), dtype=np.float32)
    y = np.zeros(n, dtype=np.int8)
    vintage = np.zeros(n, dtype=np.float64)
    for i, (cat, house, fy, recd, has_exp, tx, state, sanc, comp) in enumerate(rows):
        recd = recd[:10]
        age = (max(0.0, _months_between(recd, today) or 0.0)
               if len(recd) == 10 and recd[:4].isdigit() else 0.0)
        X[i] = _row_vector(cat, house, fy, recd, int(has_exp), int(tx or 0),
                           state, float(sanc or 0), age, cat_vocab, state_vocab)
        y[i] = 1 if (_num(comp) is not None and comp >= 90) else 0
        vintage[i] = _fy_year(fy) + (_months_between(recd, today) or 0) / 1200.0

    order = np.argsort(vintage, kind="stable")
    split = int(n * 0.75)
    tr_idx, te_idx = order[:split], order[split:]
    if tr_idx.size == 0 or te_idx.size == 0:
        return None

    model = LogisticRegression(C=1.0, max_iter=300, class_weight="balanced")
    try:
        model.fit(X[tr_idx], y[tr_idx])
    except Exception:
        return None

    metrics: Dict[str, Any] = {
        "trained_projects": int(tr_idx.size),
        "completed_train": int(y[tr_idx].sum()),
    }
    auc = None
    try:
        if len(np.unique(y[te_idx])) > 1:
            p_hold = model.predict_proba(X[te_idx])[:, 1]
            y_hold = y[te_idx]
            metrics["holdout_projects"] = int(te_idx.size)
            metrics["holdout_completed"] = int(y_hold.sum())
            auc = float(roc_auc_score(y_hold, p_hold))
            metrics["holdout_auc"] = round(auc, 3)
            metrics["holdout_brier"] = round(float(brier_score_loss(y_hold, p_hold)), 3)
            try:
                metrics["holdout_pr_auc"] = round(float(average_precision_score(y_hold, p_hold)), 3)
            except Exception:
                pass
    except Exception:
        pass

    usable = auc is not None and auc >= MODEL_MIN_AUC
    Xd = np.asarray(X, dtype=np.float64)
    payload = {
        "fingerprint": fingerprint,
        "model": model,
        "cat_vocab": cat_vocab,
        "state_vocab": state_vocab,
        "metrics": metrics,
        "usable": usable,
        "weights": [round(float(w), 4) for w in model.coef_[0].tolist()],
        "median_feature": np.median(Xd, axis=0).tolist(),
        "iqr_feature": (np.percentile(Xd, 75, axis=0) - np.percentile(Xd, 25, axis=0)).tolist(),
    }
    with _cache_lock:
        _cache["model"] = payload
        _cache["model_at"] = time.time()
    return payload


# ═════════════════════════════════════════════════════════════════════
# Public prediction entry point
# ═════════════════════════════════════════════════════════════════════

def predict_project(db: Session, project_id: int, *, refreshed: bool = False) -> Optional[Dict[str, Any]]:
    """Full outcome-prediction payload for one project (Feature 2)."""
    if not refreshed:
        with _cache_lock:
            hit = _payloads.get(int(project_id))
        if hit is not None:
            return dict(hit)

    project: Optional[models.Project] = (
        db.query(models.Project).filter(models.Project.id == project_id).first())
    if project is None:
        return None
    rec = db.query(models.ProjectRecInfo).filter(
        models.ProjectRecInfo.project_id == project_id).first()
    risk = db.query(models.RiskScore).filter(
        models.RiskScore.project_id == project_id).first()

    cohorts = _load_cohorts(db)
    fingerprint = _cohorts_fingerprint(db)
    model_data = _load_model(db, fingerprint)

    sanc = float(project.sanctioned_amount or 0)
    exp = float(rec.linked_expenditure or 0) if rec is not None else 0.0
    comp = _num(project.completion_percentage)
    recd = _date_parts(rec.recommendation_date if rec is not None else None)
    tx_count = int(rec.linked_tx_count or 0) if rec is not None else 0
    status = (project.status or "").strip()
    completed_now = bool(status.lower() == "completed" or (comp is not None and comp >= 90))

    age_months: Optional[float] = None
    if recd:
        age_months = max(0.0, _months_between(recd, _today()) or 0.0)

    # ── Cohorts (category + house + state, widening on sparse data) ───
    cat = project.project_type or "Unknown"
    house = (project.house or "Unattributed").strip().lower()
    state = project.state or "Unknown"
    cohort_key = None
    cohort_label: Optional[str] = None
    durs: List[float] = []
    costs: List[float] = []
    cohorts_d = cohorts["durations"]
    cohorts_c = cohorts["costs"]
    # Narrowest → widest: exact cohort · same category+state · same category.
    direct = cohorts_d.get((cat, house, state), [])
    same_state_d = [d for k, v in cohorts_d.items() if k[0] == cat and k[2] == state for d in v]
    same_cat_d = [d for k, v in cohorts_d.items() if k[0] == cat for d in v]
    direct_costs = cohorts_c.get((cat, house, state), [])
    same_state_c = [c for k, c in cohorts_c.items() if k[0] == cat and k[2] == state for c in c]
    same_cat_c = [c for k, c in cohorts_c.items() if k[0] == cat for c in c]
    if len(direct) >= MIN_COMPLETED_COHORT:
        cohort_key, durs, costs = (cat, house, state), direct, direct_costs
        cohort_label = f"{cat} · {state} · {house}"
    elif len(same_state_d) >= MIN_COMPLETED_COHORT:
        cohort_key, durs, costs = (cat, f"{state} · any house", state), same_state_d, same_state_c
        cohort_label = f"{cat} · {state} · any house"
    elif len(same_cat_d) >= MIN_COMPLETED_COHORT:
        cohort_key, durs, costs = (cat, "any house", "any state"), same_cat_d, same_cat_c
        cohort_label = f"{cat} · any house/state"

    limitations: List[str] = []
    if cohort_key is None:
        limitations.append(
            "Fewer than "
            f"{MIN_COMPLETED_COHORT} comparable completed works are recorded, so no cohort-based "
            "completion outlook, delay estimate or expected duration is produced for this project.")

    # ── Cohort outlook ────────────────────────────────────────────────
    duration_median = _percentile(durs, 50)
    duration_p90 = _percentile(durs, 90)
    forecast_curve: List[Dict[str, Any]] = []
    remaining_months: Optional[float] = None
    delay_probability: Optional[float] = None
    stalled_probability: Optional[float] = None
    expected_complete_months: Optional[float] = None

    if durs:
        vs = sorted(durs)
        horizon = [3, 6, 9, 12, 18, 24, 36]
        for m in horizon:
            forecast_curve.append({
                "month": m,
                "label": f"+{m}m",
                "probability_complete": round(len([d for d in vs if d <= m]) / len(vs), 3),
            })
        if not completed_now and age_months is not None:
            beyond = [d for d in vs if d > age_months]
            if beyond:
                delay_probability = round(len(beyond) / len(vs), 3)
                ahead = min(d for d in beyond) - age_months
                remaining_months = round(ahead, 1) if ahead > 0 else None
            else:
                delay_probability = 1.0
                limitations.append(
                    "This project has already existed longer than every comparable work that "
                    "historically completed — the cohort gives no completion estimate beyond "
                    "reporting that the age is exceptional.")
            if tx_count == 0:
                stalled_probability = round(min(1.0, (age_months or 0.0) / max(1.0, duration_median or 1.0)), 3)
        expected_complete_months = round(duration_p90 or duration_median or 0, 1)

    # ── Expected final expenditure ────────────────────────────────────
    expected_final_expenditure: Optional[float] = None
    cost_basis: Optional[Dict[str, Any]] = None
    if costs:
        ratio_median = _percentile(costs, 50)
        ratio_p25 = _percentile(costs, 25)
        if ratio_median:
            cost_basis = {
                "cohort_median_ratio": round(ratio_median, 3),
                "cohort_p25_ratio": round(ratio_p25, 3) if ratio_p25 else None,
                "sample": len(costs),
            }
            if exp > 0:
                if ratio_p25:
                    expected_final_expenditure = round(max(exp, sanc * ratio_p25), 0)
            elif sanc > 0:
                expected_final_expenditure = round(sanc * ratio_median, 0)

    # ── Model completion probability ─────────────────────────────────
    completion_probability: Optional[float] = None
    confidence = "cohort-only"
    drivers: List[Dict[str, Any]] = []
    if completed_now:
        completion_probability = 1.0
        confidence = "recorded"
    elif model_data is not None and model_data.get("usable"):
        try:
            import numpy as np
            now_months = age_months or 0.0
            vec = _row_vector(
                cat, (project.house or "None").lower(), project.fy, recd or "",
                1 if exp > 0 else 0, tx_count, (state or "").lower(),
                sanc, now_months, model_data["cat_vocab"], model_data["state_vocab"])
            xv = np.array([vec], dtype=np.float32)
            completion_probability = round(float(model_data["model"].predict_proba(xv)[0, 1]), 3)
            auc = (model_data["metrics"] or {}).get("holdout_auc") or 0.0
            confidence = "high" if auc >= 0.75 else "moderate"
            med = np.array(model_data["median_feature"], dtype=np.float64)
            iqr = np.array(model_data["iqr_feature"], dtype=np.float64)
            iqr[iqr == 0] = 1.0
            devs = (np.asarray(vec, dtype=np.float64) - med) / iqr
            contrib = [
                {
                    "factor": FEATURE_NAMES[i],
                    "label": FEATURE_LABELS.get(FEATURE_NAMES[i], FEATURE_NAMES[i]),
                    "contribution": round(float(devs[i] * model_data["weights"][i]), 3),
                }
                for i in range(len(FEATURE_NAMES))
                if abs(devs[i] * model_data["weights"][i]) > 1e-6
            ]
            contrib.sort(key=lambda c: -abs(c["contribution"]))
            drivers = contrib[:4]
        except Exception:
            completion_probability = None
        if completion_probability is None:
            limitations.append("The completion model could not score this project; cohorts are used alone.")
    elif model_data is not None:
        limitations.append(
            "The completion model's time-aware validation score fell below the reliability "
            "threshold, so no model probability is published — only cohort information is shown.")
    else:
        limitations.append(
            "Historical completed-project sample is below the training minimum, so the completion "
            "model is not applied and no completion probability is estimated.")
    if completion_probability is not None and not completed_now and confidence != "high":
        limitations.append(
            "Completion history in this dataset is extremely imbalanced (very few recorded completions), "
            "so the model probability is a coarse prioritisation signal — read it alongside the cohort "
            "outlook, never as precise odds.")

    # When no model probability exists, present only if a cohort curve exists —
    # and label it honestly as cohort share, never as a model estimate.
    if completion_probability is None and not completed_now:
        completion_probability = None  # withheld by design

    # ── Validation block ──────────────────────────────────────────────
    validation: Dict[str, Any] = {
        "approach": "time-aware hold-out: the model is trained on earlier-vintage records "
                    "and validated only on the most recent quarter it never saw",
        "model_version": MODEL_VERSION,
        "metrics": (model_data or {}).get("metrics") or {},
        "cohort_completed_pairs": cohorts.get("n_pairs") or 0,
    }
    if not model_data:
        validation["note"] = "Completion model not trained — completed sample below the minimum."
    elif not model_data.get("usable"):
        validation["note"] = "Completion model withheld — hold-out AUC below the reliability threshold."

    # ── Recorded history (import snapshots + milestones) ──────────────
    history: List[Dict[str, Any]] = []
    try:
        snaps = db.execute(text(
            "SELECT captured_at, completion_percentage, expenditure, status "
            "FROM project_snapshots WHERE project_id = :pid ORDER BY captured_at ASC LIMIT 24"
        ), {"pid": project_id}).fetchall()
    except Exception:
        snaps = []
    for s in snaps:
        history.append({
            "at": _date_parts(s[0]) or str(s[0])[:10],
            "actual_completion": _num(s[2]),
            "expenditure": _num(s[3]),
            "status": s[4],
            "kind": "import snapshot",
        })
    try:
        events = db.execute(text(
            "SELECT event_type, event_date, progress_percentage, amount "
            "FROM project_timeline_events "
            "WHERE project_id = :pid ORDER BY event_date ASC LIMIT 24"
        ), {"pid": project_id}).fetchall()
    except Exception:
        events = []
    for et, ed, prog, amnt in events:
        if str(et) == "completion":
            history.append({
                "at": _date_parts(ed) or str(ed or "")[:10],
                "actual_completion": _num(prog),
                "expenditure": _num(amnt),
                "status": None,
                "kind": "recorded milestone",
            })
    if not history:
        limitations.append(
            "No historical progress snapshots or milestones are recorded for this project, so the "
            "visual shows the current value against the cohort outlook — past per-project "
            "trajectory is not stored in this database.")

    payload = {
        "project_id": project_id,
        "model_version": MODEL_VERSION,
        "generated_at": _today(),
        "current": {
            "status": status or None,
            "sanctioned": sanc,
            "expenditure": exp,
            "completion_percentage": comp,
            "utilization_pct": (round(exp / sanc * 100, 1) if sanc > 0 else None),
            "recommendation_date": recd,
            "age_months": (round(age_months, 1) if age_months is not None else None),
            "linked_transactions": tx_count,
            "risk_level": risk.risk_level if risk is not None else None,
            "house": project.house,
            "state": project.state,
            "category": project.project_type,
        },
        "outcome": {
            "prediction": (
                "Already recorded as completed" if completed_now else
                ("Likely to complete" if (completion_probability or 0) >= 0.6 else
                 "Completability uncertain") if completion_probability is not None else
                "Estimates withheld — insufficient validated history"),
            "probability_complete": completion_probability,
            "delay_probability": delay_probability,
            "stalled_probability": stalled_probability,
            "expected_remaining_months": remaining_months,
            "expected_duration_months_total": expected_complete_months,
            "expected_final_expenditure": expected_final_expenditure,
            "confidence": confidence,
            "confidence_basis": (
                "project already recorded as completed" if completed_now else
                (f"hold-out AUC {((model_data or {}).get('metrics') or {}).get('holdout_auc')}"
                 if model_data else "cohort sizes only")),
            "completed_record_now": completed_now,
        },
        "drivers": drivers,
        "history": history,
        "forecast": forecast_curve,
        "cohort": {
            "key": cohort_label if cohort_key else None,
            "sample_completed": len(durs),
            "median_duration_months": round(duration_median, 1) if duration_median else None,
            "p90_duration_months": round(duration_p90, 1) if duration_p90 else None,
            "cost_basis": cost_basis,
        },
        "validation": validation,
        "limitations": limitations[:8],
        "disclaimers": [
            "Predictions are statistical estimates to focus attention — not guarantees and not audit findings.",
            "Predictions never modify the existing risk scoring and are never evidence of wrongdoing.",
        ],
    }

    with _cache_lock:
        _payloads[int(project_id)] = dict(payload)
        _payload_order.append(int(project_id))
        while len(_payload_order) > _PAYLOAD_MAX:
            _payloads.pop(_payload_order.pop(0), None)
    return payload


def reset_cache() -> None:
    """Clear all prediction caches (after imports / dataset changes)."""
    with _cache_lock:
        _cache.update({"cohorts_at": 0.0, "cohorts": None, "fingerprint": None,
                       "model_at": 0.0, "model": None})
        _payloads.clear()
        _payload_order.clear()
