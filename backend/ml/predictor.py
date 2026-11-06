"""
Context-Aware Risk Predictor with SHAP Explanations (v2)
========================================================

Uses peer-group statistics to compare each project against its state-level
peers. SHAP TreeExplainer provides feature-level attribution for every
prediction, making risk scores fully explainable.

Backward-compatible: all v1 fields are still returned. New fields
add explanations and contributing factors.
"""

import os
import threading
from typing import Dict

import joblib

MODEL_PATH = os.path.join(os.path.dirname(__file__), "anomaly_model.pkl")

model = None
features = []
peer_stats = None
model_version = "1.0"
_shap_explainer = None

# numpy/shap/pandas are imported LAZILY on first ML use. Eagerly importing
# shap at module load cost ~70MB RSS and numpy another ~20MB before the app
# served a single request — significant on 512MB containers. They are pure
# in-process imports, so deferring them changes nothing functionally.
np = None
_shap = None


def _ensure_np():
    """Import numpy only (no shap) — the scoring path needs just numpy.
    shap pulls in numba/llvmlite (~150MB); loading it on every first predict
    (including batch re-scores) was unnecessary: only the explanation path
    uses it."""
    global np
    if np is None:
        import numpy as _np
        np = _np


def _ensure_ml_libs():
    """Import numpy/shap on first use; safe to call repeatedly.
    Used only by the SHAP explanation path."""
    global np, _shap
    _ensure_np()
    if _shap is None:
        import shap as _shap_mod
        _shap = _shap_mod


_model_lock = threading.Lock()
_model_data_cache = None

# Bounded SHAP-result cache. An explanation is a pure function of the
# project's scoring inputs and the loaded model bundle, so repeated opens of
# the same project (drawer re-opens, audit queue → explanation → investigation)
# can reuse it instead of re-running TreeExplainer. Bounded and keyed on the
# input values: changed data or a new model version misses the cache rather
# than serving a stale explanation.
_SHAP_CACHE_MAX = 512
_shap_cache: Dict[tuple, list] = {}
_shap_cache_lock = threading.Lock()


def clear_shap_cache():
    """Drop cached explanations (called after imports/retraining)."""
    with _shap_cache_lock:
        _shap_cache.clear()


def _ensure_model():
    """Load the joblib model bundle on first use (not at import).

    Import-time loading pinned the sklearn model in RSS before the app served
    a single request. Lazy loading is behaviorally identical: model/features/
    peer_stats keep their module-level names, populated on first predict.
    """
    global model, features, peer_stats, model_version, _model_data_cache
    if _model_data_cache is not None:
        return
    with _model_lock:
        if _model_data_cache is not None:
            return
        try:
            if os.path.exists(MODEL_PATH):
                model_data = joblib.load(MODEL_PATH)
                model = model_data.get("model")
                features = model_data.get("features", [])
                peer_stats = model_data.get("peer_stats")
                model_version = model_data.get("version", "1.0")
                _model_data_cache = True
                # NOTE: the SHAP TreeExplainer is created lazily in
                # _compute_feature_contributions() (first explanation request),
                # not at import time — building it eagerly pinned shap+numpy in
                # memory for every request even when ML explanations were never used.
        except Exception as e:
            print(f"Warning: Could not load anomaly ML model: {e}")
            _model_data_cache = True  # don't retry a permanently-broken load every request


# ============================================================
# Human-readable feature labels
# ============================================================

FEATURE_LABELS = {
    "log_sanctioned": "Sanctioned amount",
    "log_expenditure": "Expenditure amount",
    "completion_pct": "Physical completion",
    "utilization_ratio": "Fund utilization rate",
    "unspent_ratio": "Unspent fund proportion",
    "expenditure_per_completion": "Cost per unit completion",
    "remaining_completion": "Remaining work",
    "zero_expenditure": "Zero expenditure recorded",
    "zero_completion": "Zero progress recorded",
    "high_value_project": "High-value project flag",
    "overspent": "Overspending flag",
    "sanctioned_zscore": "Sanctioned amount vs state peers",
    "expenditure_zscore": "Expenditure vs state peers",
    "completion_zscore": "Completion vs state peers",
    "sanctioned_vs_state_median": "Sanctioned vs state median",
    "expenditure_vs_state_median": "Expenditure vs state median",
    "completion_vs_state_median": "Completion vs state median",
    "utilization_vs_state": "Utilization vs state peers",
    "fin_phys_gap_vs_state": "Financial-physical progress gap vs state",
    "sanctioned_national_zscore": "Sanctioned amount vs national",
    "expenditure_national_zscore": "Expenditure vs national",
    "sanctioned_order_of_magnitude": "Sanctioned magnitude rank",
    "peer_group_size": "State peer group size",
}


def _explain_shap_value(feature_name, shap_val, feature_val):
    """
    Generate a human-readable explanation for a single SHAP contribution.

    For IsolationForest via SHAP:
    - Negative SHAP values push toward anomaly (more negative = more anomalous)
    - Positive SHAP values push toward normal
    """
    label = FEATURE_LABELS.get(feature_name, feature_name)

    # Binary flags
    if feature_name == "overspent" and feature_val > 0:
        return f"Expenditure exceeds sanctioned amount (flagged)"
    if feature_name == "zero_expenditure" and feature_val > 0:
        return f"No expenditure recorded despite project being active"
    if feature_name == "zero_completion" and feature_val > 0:
        return f"Zero physical completion percentage"
    if feature_name == "high_value_project" and feature_val > 0:
        return f"High-value project (sanctioned >= 10 lakh)"
    if feature_name == "peer_group_size" and shap_val < -0.3:
        return f"Anomaly is unusual relative to the large state peer group"

    # Peer-relative features: explain the z-score direction
    if feature_name == "sanctioned_zscore":
        if shap_val < -0.15:
            direction = "significantly higher" if feature_val > 0 else "much lower"
            return f"Sanctioned amount is {direction} than typical projects in this state"
        elif shap_val > 0.15:
            return f"Sanctioned amount is consistent with state peers"

    if feature_name == "expenditure_zscore":
        if shap_val < -0.15:
            direction = "much higher" if feature_val > 0 else "much lower"
            return f"Expenditure is {direction} than state peer average"

    if feature_name == "completion_zscore":
        if shap_val < -0.15:
            direction = "much lower" if feature_val < 0 else "much higher"
            return f"Physical completion is {direction} than state peer average"

    if feature_name == "utilization_vs_state":
        if shap_val < -0.15:
            if feature_val > 0:
                return f"Fund utilization is significantly higher than state peers"
            else:
                return f"Fund utilization is much lower than state peers"

    if feature_name == "fin_phys_gap_vs_state":
        if shap_val < -0.15:
            if feature_val > 0:
                return f"Expenditure is much higher than expected for the reported progress"
            else:
                return f"Financial and physical progress are both atypically low"

    if feature_name == "sanctioned_national_zscore":
        if shap_val < -0.15:
            direction = "much higher" if feature_val > 0 else "much lower"
            return f"Sanctioned amount is {direction} than the national median"

    if feature_name == "expenditure_national_zscore":
        if shap_val < -0.15:
            direction = "much higher" if feature_val > 0 else "much lower"
            return f"Expenditure is {direction} compared to national average"

    if feature_name == "sanctioned_vs_state_median":
        if shap_val < -0.1 and feature_val > 3:
            return f"Sanctioned amount is {feature_val:.1f}x the state median (very large)"
        elif shap_val < -0.1 and feature_val < 0.1:
            return f"Sanctioned amount is only {feature_val*100:.0f}% of state median (very small)"

    if feature_name == "expenditure_vs_state_median":
        if shap_val < -0.1 and feature_val > 3:
            return f"Expenditure is {feature_val:.1f}x the state median (very large)"
        elif shap_val < -0.1 and feature_val < 0.1:
            return f"Expenditure is negligible compared to state median"

    if feature_name == "completion_vs_state_median":
        if shap_val < -0.1 and feature_val < 0.3:
            return f"Completion is {feature_val*100:.0f}% of state median (very low progress)"

    # --- Peer-relative feature explanations ---
    if feature_name == "sanctioned_vs_state_median":
        if shap_val < -0.1 and feature_val > 3:
            return f"Sanctioned amount is {feature_val:.1f}x the state median (very large)"
        elif shap_val < -0.1 and feature_val < 0.1:
            return f"Sanctioned amount is only {feature_val*100:.0f}% of state median (very small)"

    if feature_name == "expenditure_vs_state_median":
        if shap_val < -0.1 and feature_val > 3:
            return f"Expenditure is {feature_val:.1f}x the state median (very large)"
        elif shap_val < -0.1 and feature_val < 0.1:
            return f"Expenditure is negligible compared to state median"

    if feature_name == "completion_vs_state_median":
        if shap_val < -0.1 and feature_val < 0.3:
            return f"Completion is {feature_val*100:.0f}% of state median (very low progress)"
        elif shap_val < -0.1 and feature_val > 3:
            return f"Completion is much higher than state peers (may indicate unusual status)"

    if feature_name == "sanctioned_order_of_magnitude":
        if shap_val < -0.15:
            if feature_val > 1:
                return f"Sanctioned amount is orders of magnitude above national median"
            elif feature_val < -1:
                return f"Sanctioned amount is orders of magnitude below national median"

    if feature_name == "log_sanctioned":
        if shap_val < -0.15:
            return f"Log-scale sanctioned amount contributes to unusual profile"

    if feature_name == "log_expenditure":
        if shap_val < -0.15:
            return f"Log-scale expenditure contributes to unusual profile"

    if feature_name == "expenditure_per_completion":
        if shap_val < -0.15 and feature_val > 100000:
            return f"Cost per unit completion is very high (Rs {feature_val:,.0f} per %)"

    # Generic fallback for significant contributions
    if shap_val < -0.3:
        return f"{label} is strongly anomalous (value: {feature_val:.2f})"
    elif shap_val < -0.15:
        return f"{label} is somewhat unusual (value: {feature_val:.2f})"

    return None


# ═══ Authoritative expenditure supply ═══════════════════════════════
# The risk engine must score the SAME spend number every other feature
# uses: the MP+IDA-verified payment-ledger total persisted in
# project_rec_info.linked_expenditure (metrics.resolve_project_expenditure).
# The catalog's projects.expenditure column is a zeroed legacy stamp —
# scoring it would score ₹0 for every project. Callers attach real values
# before calling predict_risk; _eff_expenditure() falls back to the catalog
# column only when nothing was attached (keeps ad-hoc/synthetic objects
# working, e.g. simulations that deliberately carry their own values).


def attach_authoritative_expenditure(projects, db) -> dict:
    """Attach the authoritative linked expenditure to each project object.

    One indexed read of the (tiny) linked rows from project_rec_info; sets
    the private `_authoritative_expenditure` attribute on the projects it
    covers. Returns the {project_id: amount} map for reuse.
    """
    from models import ProjectRecInfo
    rows = (
        db.query(ProjectRecInfo.project_id, ProjectRecInfo.linked_expenditure)
        .filter(ProjectRecInfo.linked_expenditure > 0)
        .all()
    )
    amounts = {int(pid): float(amt or 0.0) for pid, amt in rows}
    for p in projects:
        pid = getattr(p, "id", None)
        if pid is not None and pid in amounts:
            p._authoritative_expenditure = amounts[pid]
    return amounts


def _eff_expenditure(project) -> float:
    """Effective expenditure for scoring: the attached authoritative value
    when present, else the catalog column (legacy stamp → 0 for almost all
    real rows; never a fabricated number)."""
    attached = getattr(project, "_authoritative_expenditure", None)
    if attached is not None:
        return float(attached or 0)
    return float(getattr(project, "expenditure", 0) or 0)


def _compute_feature_contributions(project):
    """
    Compute per-feature contributions using SHAP values.
    Returns list of (feature_name, shap_value, feature_value, explanation)
    sorted by absolute SHAP contribution (most anomalous first).
    """
    _ensure_model()
    sanctioned = float(getattr(project, "sanctioned_amount", 0) or 0)
    expenditure = _eff_expenditure(project)
    completion = float(getattr(project, "completion_percentage", 0) or 0)
    state = getattr(project, "state", "")
    project_type = getattr(project, "project_type", "")

    if not peer_stats or not features:
        return []

    cache_key = (
        getattr(project, "id", None),
        model_version,
        round(sanctioned, 2),
        round(expenditure, 2),
        round(completion, 4),
        state,
        project_type,
    )
    with _shap_cache_lock:
        cached = _shap_cache.get(cache_key)
    if cached is not None:
        # Return copies: callers summarize/sort and must never mutate the
        # shared cached dicts.
        return [dict(item) for item in cached]

    _ensure_ml_libs()
    global _shap_explainer
    if _shap_explainer is None:
        try:
            _shap_explainer = _shap.TreeExplainer(model)
        except Exception:
            return []

    # Compute features
    from ml.train_anomaly_model import create_peer_relative_features
    row = {"state": state, "project_type": project_type,
           "sanctioned_amount": sanctioned, "expenditure": expenditure,
           "completion_percentage": completion}
    feat = create_peer_relative_features(row, peer_stats)

    X = np.array([[feat.get(f, 0) for f in features]])

    try:
        shap_values = _shap_explainer.shap_values(X)[0]
    except Exception:
        return []

    contributions = []
    for i, fname in enumerate(features):
        sv = float(shap_values[i])
        fv = float(X[0][i])
        explanation = _explain_shap_value(fname, sv, fv)
        contributions.append({
            "feature": fname,
            "label": FEATURE_LABELS.get(fname, fname),
            "shap_value": round(sv, 4),
            "feature_value": round(fv, 4),
            "explanation": explanation,
            "contributes_to_anomaly": sv < -0.05,
        })

    # Sort: most anomalous contributions first (most negative SHAP)
    contributions.sort(key=lambda x: x["shap_value"])

    with _shap_cache_lock:
        if len(_shap_cache) >= _SHAP_CACHE_MAX:
            # Bounded-growth guard: drop the oldest half (insertion order).
            for old_key in list(_shap_cache.keys())[: _SHAP_CACHE_MAX // 2]:
                _shap_cache.pop(old_key, None)
        _shap_cache[cache_key] = [dict(item) for item in contributions]

    return contributions


def _summarize_reasons(contributions, rule_reasons, risk_level):
    """
    Generate a concise summary of the main risk factors.
    Combines SHAP-based ML contributions with rule-based reasons.
    """
    factors = []

    # Add rule-based reasons first (these are deterministic and clear)
    for reason in rule_reasons:
        factors.append({
            "source": "rule",
            "explanation": reason,
        })

    # Add top ML contributions that have explanations
    ml_factors = [
        c for c in contributions
        if c["contributes_to_anomaly"] and c["explanation"]
    ]
    for c in ml_factors[:5]:  # Top 5 ML factors
        factors.append({
            "source": "ml",
            "explanation": c["explanation"],
            "feature": c["feature"],
            "impact": round(abs(c["shap_value"]), 4),
        })

    # Generate overall summary
    if risk_level == "High":
        summary = "This project exhibits multiple significant anomalies requiring audit attention."
    elif risk_level == "Medium":
        summary = "This project shows several patterns that deviate from expected norms."
    elif risk_level == "Low":
        summary = "This project has minor deviations from typical patterns."
    else:
        summary = "This project appears consistent with normal project patterns."

    return {
        "summary": summary,
        "risk_factors": factors,
        "ml_contribution_count": sum(1 for f in factors if f["source"] == "ml"),
        "rule_contribution_count": sum(1 for f in factors if f["source"] == "rule"),
    }


def _clean_feature_vector(feat: dict) -> list:
    """features-ordered numeric vector from a feature dict, sanitized the same
    way the previous pandas path did (inf/-inf/nan → 0, missing → 0).
    A single 1×N numpy array replaces the pandas DataFrame — pandas pulled
    ~30MB into the scoring path that never needed a DataFrame."""
    _ensure_np()
    vals = []
    for f in features:
        v = feat.get(f, 0)
        if v is None:
            v = 0
        try:
            v = float(v)
        except (TypeError, ValueError):
            v = 0
        if v != v or v in (float("inf"), float("-inf")):  # nan / inf
            v = 0
        vals.append(v)
    return np.array([vals])


def create_project_features(project):
    """Create features for a single project, using peer stats if available."""
    sanctioned = float(getattr(project, "sanctioned_amount", 0) or 0)
    expenditure = _eff_expenditure(project)
    completion = float(getattr(project, "completion_percentage", 0) or 0)

    if peer_stats:
        from ml.train_anomaly_model import create_peer_relative_features as _crf
        row = {"state": getattr(project, "state", ""),
               "project_type": getattr(project, "project_type", ""),
               "sanctioned_amount": sanctioned, "expenditure": expenditure,
               "completion_percentage": completion}
        feat = _crf(row, peer_stats)
        return _clean_feature_vector(feat)

    # Fallback: v1 absolute features (same formulas as before, numpy-only)
    _ensure_np()
    safe_sanctioned = sanctioned if sanctioned else 1
    safe_completion = completion if completion else 1
    feat = {
        "utilization_ratio": expenditure / safe_sanctioned,
        "unspent_amount": sanctioned - expenditure,
        "expenditure_per_completion": expenditure / safe_completion,
        "remaining_completion": 100 - completion,
        "high_value_project": 1 if sanctioned >= 1000000 else 0,
        "zero_expenditure": 1 if expenditure == 0 else 0,
        "zero_completion": 1 if completion == 0 else 0,
    }
    return _clean_feature_vector(feat)


# ═══ Point-level rule constants (pure, reusable) ════════════════════
# Single source of truth for the rule weights. predict_risk() below reads
# these same literals inline for its production path; the pure scoring
# function exposes them so simulations and explanations can report exact
# point contributions without duplicating the formula anywhere.
RULE_POINTS = [
    # (rule key, reason text, points, trigger predicate on (s, e, c, status))
    ("overspend", "Expenditure exceeds sanctioned amount", 40,
     lambda s, e, c, st: s > 0 and e > s),
    ("high_spend_low_progress", "High expenditure with low physical completion", 35,
     lambda s, e, c, st: s > 0 and (e / s) >= 0.80 and c < 50),
    ("zero_progress_spend", "Disbursements made with 0% physical completion", 30,
     lambda s, e, c, st: e > 0 and c == 0),
    ("high_value_delay", "High-value project with severe progress delay", 20,
     lambda s, e, c, st: s >= 1000000 and c < 25),
    ("completed_below_90", "Project marked completed but physical progress is below 90%", 25,
     lambda s, e, c, st: st == "completed" and c < 90),
    ("high_completion_zero_spend", "High completion recorded with zero expenditure", 20,
     lambda s, e, c, st: c >= 80 and e == 0),
    ("very_low_utilization", "Very low fund utilization and low completion", 15,
     lambda s, e, c, st: s >= 500000 and (e / s) < 0.10 and c < 30),
]
ML_ANOMALY_POINTS = 25


def score_rules_pure(sanctioned, expenditure, completion, status, ml_anomaly=False):
    """Pure point-in-time re-computation of the deterministic rule engine.

    Same formulas, thresholds and point weights as predict_risk(); takes
    plain numbers instead of a Project row so "what-if" simulations can
    re-score hypothetical values with the EXISTING logic — never a second
    formula. Returns (risk_score, risk_level, triggered) where triggered is
    [{key, reason, points}] in rule order.
    """
    s = max(0.0, float(sanctioned or 0))
    e = max(0.0, float(expenditure or 0))
    c = min(100.0, max(0.0, float(completion or 0)))
    st = str(status or "").strip().lower()

    triggered = []
    score = 0
    for key, reason, points, trigger in RULE_POINTS:
        try:
            hit = trigger(s, e, c, st)
        except Exception:
            hit = False
        if hit:
            triggered.append({"key": key, "reason": reason, "points": points})
            score += points
    if ml_anomaly:
        triggered.append({
            "key": "ml_anomaly",
            "reason": "ML anomaly contribution (carried over from the stored result — see note)",
            "points": ML_ANOMALY_POINTS,
        })
        score += ML_ANOMALY_POINTS

    score = min(100, max(0, score))
    if score >= 60:
        level = "High"
    elif score >= 30:
        level = "Medium"
    elif score > 0:
        level = "Low"
    else:
        level = "None"
    return score, level, triggered


def predict_risk(project, batch_mode=False, ml_override=None):
    """
    Predict risk for a project with full explainability.

    Args:
        project: Project ORM object
        batch_mode: If True, skip expensive SHAP explainability (for bulk scoring)

    Returns:
        ml_anomaly: bool
        ml_score: float (decision function value; lower = more anomalous)
        risk_score: int (0-100, rule-based + ML combined)
        risk_level: str (High/Medium/Low/None)
        is_anomaly: bool
        reasons: list[str] (backward-compatible rule-based reasons)
        explanation: dict (full explanation with contributing factors, skipped in batch_mode)
    """
    sanctioned = float(getattr(project, "sanctioned_amount", 0) or 0)
    # Authoritative per-project spend (payment ledger, MP+IDA-verified link)
    # attached by callers via attach_authoritative_expenditure(); the catalog
    # column is a zeroed legacy stamp and must not drive the rules.
    expenditure = _eff_expenditure(project)
    completion = float(getattr(project, "completion_percentage", 0) or 0)
    status_str = str(getattr(project, "status", "") or "").lower()

    ml_anomaly = False
    anomaly_score = 0.0

    if ml_override is not None:
        # Bulk-scoring path: the caller already ran the SAME model over a
        # stacked feature matrix. Rules and thresholds below are unchanged.
        ml_anomaly = bool(ml_override[0])
        anomaly_score = float(ml_override[1])
    else:
        _ensure_model()  # lazy-load the joblib bundle on first prediction
        if model is not None and features:
            try:
                X = create_project_features(project)
                pred = model.predict(X)[0]
                anomaly_score = float(model.decision_function(X)[0])
                ml_anomaly = bool(pred == -1)
            except Exception:
                pass

    risk_score = 0
    reasons = []

    # Rule 1: Overspending
    if sanctioned > 0 and expenditure > sanctioned:
        reasons.append("Expenditure exceeds sanctioned amount")
        risk_score += 40

    # Rule 2: High expenditure with low completion
    if sanctioned > 0 and (expenditure / sanctioned) >= 0.80 and completion < 50:
        reasons.append("High expenditure with low physical completion")
        risk_score += 35

    # Rule 3: Zero progress with disbursements
    if expenditure > 0 and completion == 0:
        reasons.append("Disbursements made with 0% physical completion")
        risk_score += 30

    # Rule 4: High-value project with very low completion
    if sanctioned >= 1000000 and completion < 25:
        reasons.append("High-value project with severe progress delay")
        risk_score += 20

    # Rule 5: Marked completed with low completion percentage
    if status_str == "completed" and completion < 90:
        reasons.append("Project marked completed but physical progress is below 90%")
        risk_score += 25

    # Rule 6: High completion with zero expenditure reported
    if completion >= 80 and expenditure == 0:
        reasons.append("High completion recorded with zero expenditure")
        risk_score += 20

    # Rule 7: Very low fund utilization on major project
    if sanctioned >= 500000 and (expenditure / sanctioned) < 0.10 and completion < 30:
        reasons.append("Very low fund utilization and low completion")
        risk_score += 15

    # ML Anomaly contribution
    ml_reason = None
    if ml_anomaly:
        risk_score += 25
        if peer_stats:
            ml_reason = "Contextual anomaly detected by peer-relative ML model"
        else:
            ml_reason = "Statistical anomaly detected by ML Isolation Forest model"
        reasons.append(ml_reason)

    risk_score = min(100, max(0, risk_score))

    if risk_score >= 60:
        risk_level = "High"
    elif risk_score >= 30:
        risk_level = "Medium"
    elif risk_score > 0:
        risk_level = "Low"
    else:
        risk_level = "None"

    # --- EXPLAINABILITY ---
    if batch_mode:
        return {
            "ml_anomaly": ml_anomaly,
            "ml_score": round(anomaly_score, 4),
            "risk_score": risk_score,
            "risk_level": risk_level,
            "is_anomaly": len(reasons) > 0,
            "reasons": reasons,
        }

    contributions = _compute_feature_contributions(project)
    explanation = _summarize_reasons(contributions, reasons, risk_level)

    # Format contributing factors for API response
    top_factors = []
    for c in contributions:
        if c["contributes_to_anomaly"] and c["explanation"]:
            top_factors.append({
                "source": "ml",
                "feature": c["feature"],
                "label": c["label"],
                "explanation": c["explanation"],
                "shap_value": c["shap_value"],
                "feature_value": c["feature_value"],
            })
    # Add rule-based factors
    for reason in reasons:
        top_factors.append({
            "source": "rule",
            "explanation": reason,
        })

    return {
        "ml_anomaly": ml_anomaly,
        "ml_score": round(anomaly_score, 4),
        "risk_score": risk_score,
        "risk_level": risk_level,
        "is_anomaly": len(reasons) > 0,
        "reasons": reasons,
        "explanation": explanation,
        "contributing_factors": top_factors[:10],
    }
