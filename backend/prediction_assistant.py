"""
MPLADS AI — Project Outcome Prediction intent (Feature 2)
=========================================================

Wires the outcome-prediction model (prediction.py) into the EXISTING
assistant answer chain as a curated data intent. Runs AFTER project-context
resolution relies on session context and AFTER interrogation intents, but
BEFORE generic lookups, so questions like:

    "what is the predicted completion outlook for this project?"
    "will project 80545 complete?"
    "what is the probability of delay?"
    "how much longer will this take?"

are answered from the prediction service with the structured payload the
AssistantWidget renders as a prediction card.

Safety: analyst+ role (same gate as the /predict endpoint); the payload is
the service's own bounded structure; predictions are always presented with
their limitations and never modify risk scores.
"""

import re
from typing import Any, Dict, List, Optional, Tuple

import auth
PROJECT_TOKEN_RE = re.compile(r"\b(?:project\s*#?\s*|prd?\s*#?\s*)(\d{1,10})\b", re.I)

_PREDICT_RE = re.compile(
    r"\b(predict|prediction|predicted|forecast|outlook|prognosis)\b"
    r"|\b(complet\w*?|outcome|delays?)\b.{0,30}\b(likelihood|probability|odds|chances?)\b"
    r"|\b(probability|likelihood|odds|chances?)\s+of\s+(complet\w*|delays?|stall\w*)\b"
    r"|\bwill\b.{0,40}\b(complete|finish|get\s+completed)\b"
    r"|\b(how\s+much\s+longer|how\s+long\s+will|when\s+will)\b.{0,50}\b(complete|finish|take|built|done)\b"
    r"|predicted\s+completion\s+outlook"
    r"|\bstalled?\b.{0,30}\b(likelihood|probability|risk|chance)\b",
    re.I,
)

# Questions that must NOT be predicted here even if they mention completion —
# risk reasoning and analytics own those phrasings.
_NEGATIVE_RE = re.compile(
    r"\bwhy\b|\bhow\s+(do|does)\b|\bwhat\s+(is|are)\s+(the\s+)?(risk|methodolog|score)"

    r"|\brisk\s+(methodolog|scor)|\bexplain\b|\bdatasource\b|\bcompare\b|\bversus\b|\bvs\.?\b",
    re.I,
)

ROLE_NOTE = (
    "Outcome predictions are available to signed-in analyst, auditor and "
    "administrator accounts (the same access rule as other AI/analytical surfaces)."
)


def _pid(q: str, ctx_project_id: Optional[int], session_ctx: Optional[dict]) -> Optional[int]:
    m = PROJECT_TOKEN_RE.search(q or "")
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            return None
    if ctx_project_id:
        return int(ctx_project_id)
    if session_ctx and session_ctx.get("project_id"):
        try:
            return int(session_ctx["project_id"])
        except (TypeError, ValueError):
            return None
    return None


def _compose_answer(p: dict) -> str:
    o = p.get("outcome") or {}
    cur = p.get("current") or {}
    cohort = p.get("cohort") or {}
    lines: List[str] = []
    lines.append(f"### Outcome prediction — #{p.get('project_id')} {p.get('project_name') or ''}".strip())
    lines.append("")
    status_txt = "ongoing" if cur.get("status") else "no status recorded"
    if cur.get("status"):
        status_txt = str(cur["status"]).lower() + (f" at {cur['completion_percentage']}% reported progress"
                                                   if cur.get("completion_percentage") is not None else "")
    lines.append(f"Current record: {status_txt}, sanctioned {_fmt_money(cur.get('sanctioned')) or '—'}"
                 f", linked payments {_fmt_money(cur.get('expenditure')) or 'none recorded'}.")

    lines.append("")
    lines.append(f"**Predicted outcome:** {o.get('prediction')}")
    if o.get("probability_complete") is not None:
        lines.append(f"- Completion probability (validated model): **{float(o['probability_complete']) * 100:.0f}%**"
                     f" · confidence {o.get('confidence')} ({o.get('confidence_basis')}).")
    if o.get("expected_duration_months_total"):
        lines.append(f"- Comparable works complete typically by month **{o['expected_duration_months_total']:g}**"
                     " (cohort p90).")
    if o.get("expected_remaining_months"):
        lines.append(f"- Estimated **remaining ≈ {o['expected_remaining_months']:g} months** based on cohort averages.")
    if o.get("delay_probability") is not None:
        lines.append(f"- Cohort delay likelihood at current age: **{float(o['delay_probability']) * 100:.0f}%**.")
    if o.get("stalled_probability") is not None:
        lines.append(f"- Stalled-execution likelihood (no linked payments at this age): **{float(o['stalled_probability']) * 100:.0f}%**.")
    if o.get("expected_final_expenditure") is not None:
        lines.append(f"- Expected final expenditure: **{o['expected_final_expenditure_fmt']}**"
                     " (cohort cost pattern).")
    drivers = p.get("drivers") or []
    if drivers:
        parts = ", ".join(f"{d.get('label', d.get('factor'))}" for d in drivers[:3])
        lines.append(f"- Main factors in this prediction: {parts}.")
    lines.append("")
    lines.append("*Completability is statistical, not guaranteed. Nothing here modifies the risk scores"
                 " or implies wrongdoing; see the limitations below.*")
    return "\n".join(lines)


def _fmt_money(v) -> Optional[str]:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if v >= 1e7:
        return f"₹{v / 1e7:.2f} Cr"
    if v >= 1e5:
        return f"₹{v / 1e5:.2f} L"
    return f"₹{v:,.0f}"


def prediction_intent(q: str, ql: str, ctx_project_id: Optional[int], user, db,
                     session_ctx: Optional[dict] = None) -> Optional[Tuple[str, list, Optional[int], None, dict]]:
    if not _PREDICT_RE.search(ql) or _NEGATIVE_RE.search(ql):
        return None
    if not auth.has_role_rank(user, "analyst"):
        return (
            "Outcome predictions are available to signed-in analyst/auditor/administrator accounts — "
            "the same rule as the other analytical surfaces. Please sign in and ask again.",
            [], None, None,
            {"limitations": ["Role gate — sign in with an analyst+ account to use predictions."]},
        )

    pid = _pid(q, ctx_project_id, session_ctx)
    if not pid:
        return (
            "Which project's outlook should I predict? Open one in the project drawer first, "
            "or give me the ID — for example: \u201cwhat is the predicted completion outlook for project 80545?\u201d",
            [], None, None,
            {"limitations": ["A project must be named before an outcome prediction can be computed."]},
        )

    import models as _models
    import prediction as _prediction
    project = db.query(_models.Project).filter(_models.Project.id == pid).first()
    if project is None:
        return (
            f"I cannot find project #{pid} in the recorded catalogue.",
            [], None, None,
            {"limitations": [f"Project {pid} does not exist."]},
        )

    payload = _prediction.predict_project(db, pid)
    answer = _compose_answer({**payload, "project_name": project.project_name})

    # text with money formatting for the card reuse
    if payload.get("outcome", {}).get("expected_final_expenditure") is not None:
        payload["outcome"]["expected_final_expenditure_fmt"] = _fmt_money(
            payload["outcome"]["expected_final_expenditure"])
    from assistant import ActionOut
    actions = [
        ActionOut(label="Open Project Details", action="open_project", target=str(pid), auto=True),
    ]
    meta = {
        "prediction": payload,
        "intent": "outcome_prediction",
        "limitations": list(payload.get("limitations") or []),
    }
    return answer, actions, pid, None, meta
