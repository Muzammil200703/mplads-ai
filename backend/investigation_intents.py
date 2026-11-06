"""
MPLADS AI — Investigation intent layer
======================================

Regex intent handlers wiring the investigation service layer
(investigation.py) into the existing assistant chain. Every handler mirrors
the existing convention: return (answer, actions[, discussed_pid, ctx]) or
None when the question isn't its concern. All wording follows the platform
rules: indicators, never accusations; every fact comes from the backend.
"""
import re
from typing import Dict, List, Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

import models


def _AO(**kw):
    """ActionOut constructor — lazily imported to avoid the assistant.py
    import cycle (same pattern as mp_assistant._nav_action)."""
    from assistant import ActionOut
    return ActionOut(**kw)


def _pid(q: str, ql: str, ctx_project_id: Optional[int], session_ctx: Optional[dict]) -> Optional[int]:
    """Project id for the ask: explicit number, page/project context, or the
    session's discussed project (multi-turn "this project")."""
    from assistant import _extract_project_id
    pid = _extract_project_id(q)
    if pid:
        return pid
    # Ordinal follow-ups resolve against the previous answer's result list.
    if session_ctx and session_ctx.get("result_ids"):
        from assistant_nlu import ordinal_index
        index = ordinal_index(ql)
        ids = []
        for value in session_ctx.get("result_ids") or []:
            try:
                ids.append(int(value))
            except (TypeError, ValueError):
                continue
        if index is not None and ids and -len(ids) <= index < len(ids):
            return ids[index]
    pronoun = bool(re.search(r"\b(its?|this\s+(project|one)|that\s+(project|one)|the\s+project)\b", ql))
    if (ctx_project_id or pronoun) and ctx_project_id:
        return ctx_project_id
    if session_ctx and session_ctx.get("project_id"):
        return int(session_ctx["project_id"])
    return None


# ═════════════════════════════════════════════════════════════════════
# Feature 3 — "Why is this risky?" (deep structured reasoning)
# ═════════════════════════════════════════════════════════════════════

_RISKY_RE = re.compile(
    r"why\s+(is|was|are)\s+((this|that|it|one)(\s+(project|work|one))?|project\s*#?\s*\d+)\s+"
    r"((so\s+|very\s+)?(risky|risk\w*|flagged|dangerous|concern\w*)|an?\s+(outlier|anomal\w*)|in\s+(the\s+)?(high|medium)\s+risk)"
    r"|what\s+makes?\s+(this\s+)?(project|it)\s+(risky|risk|outlier|anomal)"
    r"|explain\s+(this\s+)?(project.s|its|the)\s+risk"
    r"|why\s+did\s+(the\s+)?(ai|model|system)\s+flag"
    r"|why\s+is\s+(the\s+)?(first|second|third|fourth|fifth|last)\s+(one|project|work)\s+(so\s+)?(risky|flagged)"
    r"|why\s+risky\b", re.I)


def risk_reasoning_intent(q: str, ql: str, ctx_project_id: Optional[int], user, db: Session,
                          session_ctx: Optional[dict] = None) -> Optional[Tuple[str, list, Optional[int]]]:
    if not _RISKY_RE.search(ql):
        return None
    pid = _pid(q, ql, ctx_project_id, session_ctx)
    if not pid:
        return None
    from investigation import load_project_context, analyze_risk, compose_risk_explanation
    ctx = load_project_context(db, pid)
    if ctx is None:
        return None  # not-found handling belongs to the lookup intent
    analysis = analyze_risk(db, ctx, want_peers=True)
    answer = compose_risk_explanation(ctx, analysis)
    actions = [
        _AO(label="Open Risk Center", action="navigate", target="Risk Center"),
        _AO(label="AI Forensics tab", action="open_project", target=str(pid),
            params={"tab": "forensics"}),
        _AO(label="Open Audit Priority", action="navigate", target="Audit Priority"),
    ]
    # Attach the review engine's structured findings/recommendations so the
    # client can render them separately from the narrative. The narrative is
    # unchanged; this only adds fields old clients ignore.
    try:
        from assistant_review import review_meta
        meta = {"intent": "risk_explanation", "entity": {"type": "project", "id": pid},
                "result_ids": [pid], **review_meta(db, pid, ctx=ctx, analysis=analysis)}
    except Exception:
        meta = {"intent": "risk_explanation", "entity": {"type": "project", "id": pid},
                "result_ids": [pid]}
    return answer, actions, pid, None, meta


# ═════════════════════════════════════════════════════════════════════
# Feature 5 — peer comparison ("compare with similar projects")
# ═════════════════════════════════════════════════════════════════════

_PEER_RE = re.compile(
    r"compare\s+(this|it|that|the project)\b|compar\w*\s+(this\s+)?project\s+(with|to|against)\s+(similar|peers?)"
    r"|(is|does)\s+(this\s+)?(expenditure|spend\w*|utili[sz]ation|cost|progress)\s+(high|low|unusual|normal|typical)\b.{0,40}(similar|peer|compar|other)"
    r"|similar\s+projects?\b|projects?\s+similar\s+to\s+(this|it)"
    r"|what\s+makes?\s+(this\s+)?(project|it)\s+different\b.{0,30}(peer|similar|other)"
    r"|peer\s+(comparison|group|projects?)"
    r"|(high|low|unusual)\s+compared\s+(with|to)\s+(similar|peer)", re.I)


def peer_comparison_intent(q: str, ql: str, ctx_project_id: Optional[int], user, db: Session,
                           session_ctx: Optional[dict] = None) -> Optional[Tuple[str, list, Optional[int]]]:
    if not _PEER_RE.search(ql):
        return None
    # Two explicit IDs belong to the existing two-project comparator.
    if len(re.findall(r"\b\d{1,10}\b", q)) >= 2:
        return None
    pid = _pid(q, ql, ctx_project_id, session_ctx)
    if not pid:
        return None
    from investigation import load_project_context, analyze_peers, compose_peer_comparison
    ctx = load_project_context(db, pid)
    if ctx is None:
        return None
    peers = analyze_peers(db, pid)
    answer = compose_peer_comparison(ctx, peers)
    actions = [
        _AO(label="Open Compare Projects", action="navigate", target="Compare Projects",
            params={"a": str(pid)}),
        _AO(label="Similar tab", action="open_project", target=str(pid),
            params={"tab": "similar"}),
    ]
    return answer, actions, pid


# ═════════════════════════════════════════════════════════════════════
# Feature 6 — data quality / evidence ("can I trust the timeline?")
# ═════════════════════════════════════════════════════════════════════

_DQ_RE = re.compile(
    r"data\s+qualit|can\s+i\s+trust\b.{0,30}(timeline|data|record|project)"
    r"|(any|what)\s+(data\s+)?(issues?|problems?|inconsisten\w*|discrepan\w*).{0,30}(this\s+)?(project|work|it|data)"
    r"|is\s+(the\s+)?(timeline|data)\s+(consistent|reliable|trustworthy)"
    r"|what\s+(information|evidence|fields?|data)\s+(is|are)\s+missing|what.s\s+missing|evidence\s+missing"
    r"|what\s+evidence\s+(do\s+we|is)\s+(have|there|available)|show\s+(me\s+)?(the\s+)?evidence"
    r"|missing\s+(evidence|information|fields|dates|data)|timeline\s+(issues?|problems?|review)", re.I)


def data_quality_intent(q: str, ql: str, ctx_project_id: Optional[int], user, db: Session,
                        session_ctx: Optional[dict] = None) -> Optional[Tuple[str, list, Optional[int]]]:
    if not _DQ_RE.search(ql):
        return None
    # Portfolio-wide data-quality questions are analytics, not project DQ.
    if re.search(r"\bhow many\b|\bportfolio\b|\boverall\b|across\s+(the|all)", ql):
        return None
    pid = _pid(q, ql, ctx_project_id, session_ctx)
    if not pid:
        return None
    from investigation import load_project_context, analyze_data_quality
    ctx = load_project_context(db, pid)
    if ctx is None:
        return None
    dq = analyze_data_quality(db, ctx)
    lines = [f"### Data Quality — #{ctx['project_id']} {ctx['project_name']}", ""]
    lines.append("**DATA QUALITY**")
    for c in dq["checks"][:9]:
        lines.append(f"{'✓' if c['ok'] else '⚠'} {c['text']}")
    if dq["warnings"]:
        lines.append("")
        lines.append("**Review flags**")
        for w in dq["warnings"][:5]:
            lines.append(f"- {w}")
    lines.append("")
    lines.append("Available evidence and missing evidence are listed above; conflicting or unverified items are "
                 "flagged rather than resolved. Missing fields may indicate a source-data gap — this does not "
                 "mean the project itself is invalid.")
    actions = [
        _AO(label="Timeline tab", action="open_project", target=str(pid), params={"tab": "timeline"}),
        _AO(label="AI Forensics tab", action="open_project", target=str(pid), params={"tab": "forensics"}),
    ]
    failed = [
        {
            "title": check.get("label") or "Data-quality check",
            "detail": check.get("text") or "",
            "severity": "medium",
            "kind": "data_quality",
            "evidence": "Derived from the project catalogue and linked ledger record.",
        }
        for check in dq.get("checks") or [] if not check.get("ok")
    ][:8]
    meta = {
        "intent": "data_quality",
        "entity": {"type": "project", "id": pid},
        "result_ids": [pid],
        "findings": failed,
        "limitations": list(dq.get("warnings") or [])[:5] + [
            "Missing fields describe the recorded dataset, not a conclusion about the project."
        ],
    }
    return "\n".join(lines), actions, pid, None, meta


# ═════════════════════════════════════════════════════════════════════
# Feature 7 — audit brief
# ═════════════════════════════════════════════════════════════════════

_BRIEF_RE = re.compile(
    r"audit\s+brief|generate\s+(an\s+)?audit|prepare\s+(this\s+)?(project\s+)?for\s+review"
    r"|audit\s+summar(y|ise|ize)\b.{0,30}(this\s+)?(project|it)"
    r"|give\s+me\s+an?\s+audit\s+(summary|brief)|brief\s+(me|this)\b.{0,30}project", re.I)


def audit_brief_intent(q: str, ql: str, ctx_project_id: Optional[int], user, db: Session,
                       session_ctx: Optional[dict] = None) -> Optional[Tuple[str, list, Optional[int]]]:
    if not _BRIEF_RE.search(ql):
        return None
    pid = _pid(q, ql, ctx_project_id, session_ctx)
    if not pid:
        return ("Which project should I prepare the audit brief for? Open one first, or give me the ID "
                "(e.g. \u201cgenerate an audit brief for project 80545\u201d).",
                [_AO(label="Browse projects", action="navigate", target="Projects")], None)
    from investigation import load_project_context, compose_audit_brief
    ctx = load_project_context(db, pid)
    if ctx is None:
        return None
    answer = compose_audit_brief(db, ctx)
    actions = [
        _AO(label="Open Audit Priority", action="navigate", target="Audit Priority",
            params={"project": str(pid)}),
        _AO(label="Investigate tab", action="open_project", target=str(pid), params={"tab": "audit"}),
        _AO(label="AI Forensics tab", action="open_project", target=str(pid), params={"tab": "forensics"}),
    ]
    return answer, actions, pid


# ═════════════════════════════════════════════════════════════════════
# Multi-turn — "what should I check next?"
# ═════════════════════════════════════════════════════════════════════

_NEXT_RE = re.compile(r"what\s+should\s+i\s+(check|do|look)\s+(next|first)?|next\s+steps?\?*"
                      r"|where\s+should\s+i\s+(start|begin)", re.I)


def next_review_intent(q: str, ql: str, ctx_project_id: Optional[int], user, db: Session,
                       session_ctx: Optional[dict] = None):
    if not _NEXT_RE.search(ql):
        return None
    pid = _pid(q, ql, ctx_project_id, session_ctx)
    if pid:
        from investigation import load_project_context, analyze_risk
        ctx = load_project_context(db, pid)
        if ctx is None:
            return None
        analysis = analyze_risk(db, ctx, want_peers=False)
        lines = [f"**Suggested review — #{ctx['project_id']} {ctx['project_name']}**", ""]
        for i, rp in enumerate(analysis["review_points"][:5], 1):
            lines.append(f"{i}. {rp}")
        lines.append("")
        lines.append("These are factual areas drawn from the recorded data — review items, not findings.")
        return "\n".join(lines), [
            _AO(label="Investigate tab", action="open_project", target=str(pid), params={"tab": "audit"}),
        ], pid
    # No project in context: fall back to the existing investigation queue.
    from assistant import _intent_investigate_next
    return _intent_investigate_next(ql, user, db)


# ═════════════════════════════════════════════════════════════════════
# State context — "give me a summary of Telangana" (page/context scoped)
# ═════════════════════════════════════════════════════════════════════

_STATE_SUMMARY_RE = re.compile(r"(give|show)\s+me\s+(a\s+)?(summary|overview|snapshot)\s+of\b"
                               r"|summar(ise|ize)\s+(the\s+)?state\b", re.I)


def state_summary_intent(q: str, ql: str, user, db: Session,
                         session_ctx: Optional[dict] = None) -> Optional[Tuple[str, list]]:
    m = _STATE_SUMMARY_RE.search(ql)
    if not m:
        return None
    import metrics as _metrics
    from assistant import _canonical_state, _fmt_money, ActionOut as _AO  # noqa: F401

    state = None
    tail = ql.split("of", 1)[1] if " of " in ql else ""
    tail = tail.strip(" ?.,")
    # Resolve against the real state list (bounded query, cached upstream).
    rows = db.query(models.Project.state).distinct().all()
    low_states = {s.strip().lower(): s.strip() for (s,) in rows if s}
    for w in re.findall(r"[a-z][a-z &']{3,40}", tail):
        w = w.strip(" ,.?!&'")
        if w in low_states:
            state = low_states[w]
            break
    if not state and session_ctx and session_ctx.get("state"):
        state = session_ctx["state"]
    if not state:
        return ("Which state? Ask e.g. \u201cgive me a summary of Telangana\u201d.", [])

    st = _metrics.portfolio_stats(db, state=state)
    sanc, expd = float(st["sanctioned"]), float(st["expenditure"])
    util = (expd / sanc * 100) if sanc else 0.0
    hr = (
        db.query(func.count(models.RiskScore.id))
        .join(models.Project, models.Project.id == models.RiskScore.project_id)
        .filter(models.Project.state == state, models.RiskScore.risk_level.ilike("high"))
        .scalar() or 0
    )
    lines = [f"### State Summary — {state}", ""]
    lines.append(f"- Projects: {st['total_projects']:,}")
    lines.append(f"- Sanctioned: {_fmt_money(sanc)}")
    lines.append(f"- Recorded expenditure: {_fmt_money(expd)} ({util:.1f}% utilization)")
    lines.append(f"- Completed works (ledger): {st['completed_works']:,}")
    lines.append(f"- Average catalog completion: {st['avg_completion']:.1f}%")
    lines.append(f"- High-risk flagged projects: {hr:,}")
    lines.append("")
    lines.append("All figures are recorded-data aggregates — factual counts, not judgments about any authority.")
    return "\n".join(lines), [
        _AO(label=f"State Intelligence · {state}", action="navigate", target="State Intelligence",
            params={"state": state}, auto=True),
    ]

# ═════════════════════════════════════════════════════════════════════
# Existing investigation workspace — grounded Q&A and evidence drill-down
# ═════════════════════════════════════════════════════════════════════

_INVESTIGATION_WORKSPACE_RE = re.compile(
    r"\binvestigation\b|\breview\s+this\s+(project|investigation)\b|why\s+(was|is)\s+this\s+project\s+flagged"
    r"|financial\s+discrepan\w*|spend\w*\s+discrepan\w*|expenditure\s+records?\s+correctly\s+linked"
    r"|show\s+(me\s+)?(the\s+)?transactions?\b|linked\s+transactions?|which\s+findings?.*unresolved"
    r"|what\s+(information|evidence)\s+is\s+missing|missing\s+information|site\s+visit|questions?\s+.*(ask|agency)"
    r"|summar\w*\s+(the\s+)?investigation|preliminary\s+audit\s+brief|audit\s+brief"
    r"|unresolved\s+(findings?|questions?|actions?)|recommended\s+actions?|findings?\s+status",
    re.I,
)


def investigation_workspace_intent(q: str, ql: str, ctx_project_id: Optional[int], user, db: Session,
                                   session_ctx: Optional[dict] = None):
    """Answer investigation questions from the live, persisted workspace.

    This is an intent of the one existing assistant, not a second chatbot. It
    reads the same dossier/findings powered by audit_intel, and every answer
    cites the records behind it.
    """
    if not _INVESTIGATION_WORKSPACE_RE.search(ql):
        return None
    try:
        from auth import has_role_rank
        if not user or not has_role_rank(user, "analyst"):
            return None
    except Exception:
        return None
    pid = _pid(q, ql, ctx_project_id, session_ctx)
    if not pid:
        return None
    import audit_intel
    project = db.query(models.Project).filter(models.Project.id == pid).first()
    if project is None:
        return None
    payload = audit_intel.get_investigation(db, project)
    if not payload or not payload.get("investigation"):
        return (
            "No investigation has been started for project #{}. Open its Audit → Investigation tab to start one "
            "from the recorded project data.".format(pid),
            [_AO(label="Open project investigation", action="open_project", target=str(pid), params={"tab": "audit"})],
            pid,
            None,
            {"intent": "investigation_workspace", "entity": {"type": "project", "id": pid},
             "result_ids": [pid], "findings": [], "limitations": ["No investigation record exists for this project."]},
        )

    dossier = payload.get("dossier") or {}
    findings = payload.get("findings") or []
    recommendations = payload.get("recommendations") or []
    summary = payload.get("summary") or {}
    investigation = payload.get("investigation") or {}
    fin = dossier.get("financial") or {}
    risk = dossier.get("risk") or {}
    project_info = dossier.get("project") or {}
    rupees = audit_intel.rupees
    unresolved = [f for f in findings if f.get("status") not in audit_intel.FINAL_FINDING_STATUSES]
    answered = "summary"

    if re.search(r"transaction|payment|financial\s+discrepan|spend|expenditure|linked|over\s*sanction", ql):
        answered = "financial"
        transactions = fin.get("transactions") or []
        lines = [
            "### Financial records — #{} {}".format(pid, project_info.get("project_name") or "").strip(),
            "",
            "- Sanctioned: **{}**".format(fin.get("sanctioned_display") or rupees(0)),
            "- Authoritative linked expenditure: **{}** ({}% utilization)".format(
                fin.get("expenditure_display") or rupees(0),
                fin.get("utilization_pct") if fin.get("utilization_pct") is not None else "not computable",
            ),
            "- Linked payment rows: {}; sum of the bounded rows shown: {}.".format(
                fin.get("transaction_count") or 0, fin.get("transaction_total_display") or rupees(0)
            ),
            "- Work-key linkage: {} catalogue row(s); {}.".format(
                fin.get("work_key_rows") or 1,
                "shared/ambiguous" if int(fin.get("work_key_rows") or 1) > 1 else "one-to-one",
            ),
            "",
        ]
        if transactions:
            lines.append("**Recorded transactions**")
            for tx in transactions[:12]:
                lines.append(
                    "- `{}` · {} · {} · vendor {} · {}".format(
                        tx.get("reference"), tx.get("date") or "date not recorded",
                        tx.get("amount_display") or rupees(tx.get("amount") or 0),
                        tx.get("vendor") or "not recorded", tx.get("payment_status") or "status not recorded",
                    )
                )
            if len(transactions) > 12:
                lines.append("- … {} more rows are in the Financial records tab.".format(len(transactions) - 12))
        else:
            lines.append("No dated linked transaction rows were returned for this work key. The aggregate expenditure is still on record; its payment-level trace is missing.")
        lines += ["", "**Financial findings**"]
        for finding in [f for f in findings if f["key"] in {"expenditure_over_sanction", "expenditure_without_dated_transactions"}]:
            lines.append("- {} · {} · review {}".format(finding["title"], finding["classification_label"], finding["status"]))
        lines += ["", "These are recorded values. A variance is not proof of an irregular payment."]
        answer = "\n".join(lines)

    elif re.search(r"missing|what\s+information|evidence\s+gap|unavailable", ql):
        answered = "missing_information"
        missing = [f for f in findings if f.get("classification") == "missing_information"]
        lines = ["### Missing information / evidence — #{}".format(pid), ""]
        if missing:
            for finding in missing:
                lines.append("- **{}** ({}): {}".format(finding["title"], finding["severity"], finding["explanation"]))
        else:
            lines.append("No generated finding is currently classified as missing information.")
        lines += ["", "**Recorded limitations**"]
        for limitation in (dossier.get("limitations") or [])[:6]:
            lines.append("- {}".format(limitation))
        lines += ["", "Absence of a field is a dataset limitation, not a conclusion that the event did not occur."]
        answer = "\n".join(lines)

    elif re.search(r"unresolved|still\s+open|open\s+findings|which\s+findings|findings?\s+status|status\s+of\s+findings", ql):
        answered = "unresolved_findings"
        lines = ["### Finding review status — #{}".format(pid), ""]
        for finding in findings:
            reviewer = (finding.get("review") or {}).get("reviewer_name")
            lines.append("- **{}** · {} · severity {} · review **{}**{}".format(
                finding["title"], finding["classification_label"], finding["severity"], finding["status"],
                " · reviewer {}".format(reviewer) if reviewer else "",
            ))
        if not findings:
            lines.append("No findings are currently generated for this project.")
        lines += ["", "{} of {} findings remain unresolved. A changed risk score does not change these review states.".format(len(unresolved), len(findings))]
        answer = "\n".join(lines)

    elif re.search(r"site\s+visit|verify|questions?|improv|agency|recommended\s+actions?|what\s+should\s+i\s+do", ql):
        answered = "verification_actions"
        lines = ["### Suggested verification — #{}".format(pid), ""]
        for recommendation in recommendations:
            lines.append("- **{}**".format(recommendation["title"]))
            lines.append("  From: {} · {}".format(
                recommendation["finding_title"], "already an action item" if recommendation["added"] else "not yet added"
            ))
        lines += ["", "**Questions for the implementing agency**"]
        lines.append("- Provide the recommendation/sanction order and any revised sanction for this work.")
        lines.append("- Provide the measurement book / latest progress certificate and current site photographs.")
        lines.append("- Explain the work-key linkage and identify the vendor and payment vouchers for each recorded transaction.")
        lines.append("- Provide the completion certificate if the project is marked completed.")
        lines += ["", "These are verification prompts, not allegations. Convert a recommendation into an action item only after review."]
        answer = "\n".join(lines)

    elif re.search(r"why\s+.*flagged|risk|shap|ml|indicator|anomal", ql):
        answered = "risk_explanation"
        lines = [
            "### Why this project is in review — #{} {}".format(pid, project_info.get("project_name") or "").strip(),
            "",
            "- Stored risk score: **{}** ({}); ML anomaly: {}.".format(
                risk.get("score") if risk.get("score") is not None else "not scored",
                risk.get("level") or "unranked", "yes" if risk.get("ml_anomaly") else "no",
            ),
            "",
        ]
        if risk.get("reasons"):
            lines.append("**Rule indicators recorded by the existing engine**")
            for reason in risk["reasons"][:8]:
                lines.append("- {}".format(reason))
        else:
            lines.append("No rule indicators are stored for this project.")
        if risk.get("shap_explanations"):
            lines += ["", "**SHAP model explanations (not audit findings)**"]
            for factor in risk["shap_explanations"][:6]:
                label = factor.get("label") or factor.get("feature") or "model factor"
                value = factor.get("impact") if factor.get("impact") is not None else factor.get("value")
                lines.append("- {}: {}".format(label, value))
        lines += ["", "**Generated findings**"]
        for finding in findings:
            lines.append("- {} · {} · review {}".format(finding["title"], finding["classification_label"], finding["status"]))
        lines += ["", "Risk indicators and model outputs are review triggers, not confirmed allegations."]
        answer = "\n".join(lines)

    else:
        answer = summary.get("text") or "No investigation summary could be generated."
        if recommendations:
            answer += "\n\n**Priority actions**\n" + "\n".join(
                "- {} (from {})".format(r["title"], r["finding_title"]) for r in recommendations[:5]
            )

    answer += (
        "\n\n_Preliminary analysis from the current database records. "
        "Human reviewers set finding statuses; the assistant cannot update investigation records._"
    )
    meta = {
        "intent": "investigation_workspace",
        "sub_intent": answered,
        "entity": {"type": "investigation", "id": investigation.get("id"), "project_id": pid},
        "result_ids": [pid],
        "findings": [{
            "key": f.get("key"), "title": f.get("title"), "severity": f.get("severity"),
            "detail": f.get("explanation"), "status": f.get("status"),
            "classification": f.get("classification_label"),
        } for f in findings[:10]],
        "recommendations": [{"title": r.get("title"), "finding": r.get("finding_title")} for r in recommendations[:8]],
        "limitations": (dossier.get("limitations") or [])[:4] + [
            "AI conclusions are preliminary and must be checked against the supporting records."
        ],
    }
    actions = [
        _AO(label="Open investigation", action="open_project", target=str(pid), params={"tab": "audit"}),
        _AO(label="Investigate next", action="navigate", target="My Investigations"),
    ]
    return answer, actions, pid, None, meta
