"""MP Intelligence intent for the MPLADS assistant.

Answers MP-level questions with real backend data and, where useful,
emits navigate actions to the MP Intelligence page. Identity is always
MP+House+Constituency — never a bare name.
"""
import re
from typing import Optional, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

import models
from mp_intelligence import list_mp_intelligence, mp_detail

# House mention helper kept local to avoid an import cycle with assistant.py
_HOUSE_PAT = re.compile(r"\blok\s*sabha\b|\brajya\s*sabha\b", re.I)

_NAV_PARAM_KEYS = ("mp_id", "state", "house")


def _extract_house(q: str) -> Optional[str]:
    ql = (q or "").lower()
    if re.search(r"\blok\s*sabha\b", ql):
        return "Lok Sabha"
    if re.search(r"\brajya\s*sabha\b", ql):
        return "Rajya Sabha"
    return None


def _fmt_cr(amount: float) -> str:
    return f"₹{amount / 1e7:,.2f} Cr"


def _nav_action(mp_row=None, label: str = "Open MP Intelligence", house=None, state=None):
    # ActionOut lives in assistant.py, which imports this module — a lazy
    # import at call time avoids the cycle (assistant is fully initialized
    # before any intent runs).
    from assistant import ActionOut

    params = {}
    if mp_row is not None:
        # Accept either a MPSummary ORM row or an already-serialized dict.
        params["mp_id"] = getattr(mp_row, "id", None) or (mp_row.get("id") if isinstance(mp_row, dict) else None)
    if house:
        params["house"] = house
    if state:
        params["state"] = state
    return ActionOut(
        label=label,
        action="navigate",
        target="MP Intelligence",
        params=params,
        auto=False,
    )


# "This MP" style references — resolved against the session's last viewed
# MP (page context "mp:<id>" set by the MP Intelligence page).
_THIS_MP_RE = re.compile(r"\b(this|that|the|his|her|their)\s+mps?(?:'s)?\b")


def _mp_from_session(db: Session, session_id: Optional[str]):
    """The MP the user is currently viewing ("this MP"), resolved from the
    assistant session context recorded by _answer_body (page "mp:<id>")."""
    if not session_id:
        return None
    try:
        from assistant import _session_get_ctx
        page = (_session_get_ctx(session_id) or {}).get("page") or ""
        if page.startswith("mp:"):
            return (
                db.query(models.MPSummary)
                .filter(models.MPSummary.id == int(page.split(":", 1)[1]))
                .first()
            )
    except Exception:
        return None
    return None


def _mp_answer(r, ql: str) -> Tuple[str, list]:
    """Summary answer for a known MPSummary row, led by the asked-about metric."""
    recommended = int(r.recommended_works or 0)
    completed = int(r.completed_works or 0)
    who = f"**{r.mp_name}** ({r.house}, {r.constituency}, {r.state})"
    if re.search(r"utilization|utilisation", ql):
        lead = (f"{who}: utilization {r.utilization_percentage or 0:.2f}% — "
                f"{_fmt_cr(r.total_expenditure or 0)} spent of {_fmt_cr(r.allocated_amount or 0)} "
                f"allocated (unspent {_fmt_cr(r.unspent_amount or 0)}).")
    elif re.search(r"complet", ql):
        lead = (f"{who}: {completed} of {recommended} recommended works completed "
                f"({r.completion_rate_percentage or 0:.2f}% completion rate).")
    elif re.search(r"pending|ongoing", ql):
        lead = (f"{who}: {recommended - completed} of {recommended} recommended works still "
                f"pending/ongoing (completion rate {r.completion_rate_percentage or 0:.2f}%).")
    else:
        lead = (f"{who}: allocated {_fmt_cr(r.allocated_amount or 0)}, spent {_fmt_cr(r.total_expenditure or 0)}, "
                f"unspent {_fmt_cr(r.unspent_amount or 0)}, utilization {r.utilization_percentage or 0:.2f}%. "
                f"Works: {completed} completed of {recommended} recommended "
                f"({r.completion_rate_percentage or 0:.2f}% completion rate).")
    return lead, [_nav_action(r, label=f"Open {r.mp_name}'s works")]


def _mp_rows(db: Session, house: Optional[str], state: Optional[str], limit: int = 6):
    q = db.query(models.MPSummary)
    if house:
        q = q.filter(models.MPSummary.house == house)
    if state:
        q = q.filter(models.MPSummary.state.ilike(f"%{state}%"))
    return q.order_by(models.MPSummary.total_expenditure.desc().nullslast(), models.MPSummary.id).limit(limit).all()


def mp_intelligence_intent(q: str, ql: str, user, db: Session,
                           session_id: Optional[str] = None) -> Optional[Tuple[str, list]]:
    """Returns (answer, actions) or None if the question isn't MP-level.
    session_id enables "this MP" questions anchored to the MP Intelligence
    page the user is currently viewing."""
    # Defer to the project flows when the question is clearly project-scoped
    # ("which MP recommended this project" must still reach project context),
    # except for explicit MP statistics/compare asks.
    if re.search(r"\bprojects?\b", ql) and not re.search(
        r"\bmps?\b.{0,30}statistic|compare.{0,30}\bmps?\b|\bmps?\b.{0,20}compare", ql
    ):
        return None

    # ── 0. House-vs-house comparison ────────────────────────────
    if re.search(r"compare\b.{0,20}(lok\s*sabha|rajya\s*sabha)\s*(and|with|vs\.?|versus)\s*(lok\s*sabha|rajya\s*sabha)", ql) or (
        re.search(r"\bmps?\b", ql) and re.search(r"(lok\s*sabha).{0,20}(vs\.?|versus|compared? to|and)\s*(rajya\s*sabha)|(rajya\s*sabha).{0,20}(vs\.?|versus|compared? to|and)\s*(lok\s*sabha)", ql)
    ):
        def house_side(h):
            s = list_mp_intelligence(db, house=h, limit=1)["summary"]
            return (f"**{h}** — {s['total_mps']} MPs, {_fmt_cr(s['total_allocated'])} allocated, "
                    f"{_fmt_cr(s['total_expenditure'])} spent (avg utilization {s['average_utilization_percentage']}%), "
                    f"{s['total_completed_works']}/{s['total_recommended_works']} works completed "
                    f"(avg completion rate {s['average_completion_rate_percentage']}%)"), s
        a_line, a_s = house_side("Lok Sabha")
        b_line, b_s = house_side("Rajya Sabha")
        higher = "Lok Sabha" if a_s["total_expenditure"] >= b_s["total_expenditure"] else "Rajya Sabha"
        return (
            f"House comparison (recorded data only):\n\n{a_line}\n\n{b_line}\n\n"
            f"{higher} has the higher recorded expenditure.",
            [_nav_action(label="Open MP Intelligence", house=None)],
        )

    # ── 1. Compare two MPs ──────────────────────────────────────────
    # "Compare X and Y MPs" — resolved by name against mp_summaries.
    m = re.search(
        r"compare\s+(?:mps?\s+)?(.+?)\s+(?:and|with|vs\.?|versus)\s+(.+?)(?:\s+mps?)?\s*[?.]?\s*$",
        ql,
    )
    if m and re.search(r"\bmps?\b|members? of parliament|compare\b", ql):
        a_name, b_name = m.group(1).strip(" '?\""), m.group(2).strip(" '?\"")
        # Two state names without an "MP" mention ("compare Telangana and
        # Karnataka") is a STATE comparison — the analytics intent's two-state
        # table owns it; don't answer with an MP-not-found miss.
        if not re.search(r"\bmps?\b|members? of parliament", ql):
            low_states = {s.strip().lower() for (s,) in db.query(models.Project.state).distinct().all() if s}
            if a_name.lower() in low_states and b_name.lower() in low_states:
                return None

        def find(name):
            rows = (
                db.query(models.MPSummary)
                .filter(models.MPSummary.mp_name.ilike(f"%{name}%"))
                .all()
            )
            # Word-boundary confirmation: a short token like "NDA" must not
            # match inside "Kalyanasundaram" — require a standalone occurrence.
            pat = re.compile(rf"\b{re.escape(name)}\b", re.I)
            rows = [r for r in rows if pat.search(r.mp_name or "")]
            if not rows:
                return None
            if len(rows) > 1:
                # Prefer exact normalized match, then restrict by house if mentioned
                house = _extract_house(ql)
                for r in rows:
                    if house and r.house == house:
                        return r
                exact = [r for r in rows if r.mp_name.lower().strip() == name]
                if exact:
                    return exact[0]
            return rows[0]

        a, b = find(a_name), find(b_name)
        if a is not None and b is not None:
            def side(r):
                recommended = int(r.recommended_works or 0)
                completed = int(r.completed_works or 0)
                return (
                    f"**{r.mp_name}** ({r.house}, {r.constituency}, {r.state}) — "
                    f"allocated {_fmt_cr(r.allocated_amount or 0)}, spent {_fmt_cr(r.total_expenditure or 0)}, "
                    f"utilization {r.utilization_percentage or 0:.2f}%, "
                    f"{completed} of {recommended} works completed "
                    f"({r.completion_rate_percentage or 0:.2f}%)."
                )

            return (
                f"MP comparison (recorded data only):\n\n{side(a)}\n\n{side(b)}\n\n"
                + (
                    f"{a.mp_name} has recorded higher expenditure ({_fmt_cr((a.total_expenditure or 0))} vs "
                    f"{_fmt_cr((b.total_expenditure or 0))})."
                    if (a.total_expenditure or 0) >= (b.total_expenditure or 0)
                    else f"{b.mp_name} has recorded higher expenditure ({_fmt_cr((b.total_expenditure or 0))} vs "
                    f"{_fmt_cr((a.total_expenditure or 0))})."
                ),
                [
                    _nav_action(a, label=f"Open {a.mp_name}"),
                    _nav_action(b, label=f"Open {b.mp_name}"),
                ],
            )
        # names given but not found — fall through with a helpful miss
        if a is None or b is None:
            missing = a_name if a is None else b_name
            return (
                f"I couldn't find an MP matching \"{missing}\" in the dataset. "
                "MP names are matched exactly as recorded in the official snapshot.",
                [],
            )

    # ── 1.5. Session-anchored / possessive per-MP questions ────────
    # "this MP's expenditure" (MP Intelligence page context) and
    # "<Name>'s pending works" — BEFORE the list branch, which would
    # otherwise treat them as generic MP listings.
    if _THIS_MP_RE.search(ql):
        mp = _mp_from_session(db, session_id)
        if mp is None:
            return (
                "I don't have an MP in context. Open one on the MP Intelligence "
                "page — or ask about an MP by name — and I'll pull their numbers.",
                [_nav_action(label="Browse MPs", house=_extract_house(ql))],
            )
        return _mp_answer(mp, ql)
    # Strip leading imperatives so "show me X's pending works" captures the
    # name, not "show me X".
    pos_src = re.sub(r"^(show me|tell me|list|please|what are|what about)\s+", "", ql)
    pm = re.search(
        r"([a-z][a-z\s.'-]{4,60}?)\s*'(?:s)\s+"
        r"(?:(pending|ongoing|completed|recommended)\s+works\b|"
        r"(expenditure|spending|utilization|utilisation|unspent|allocation))",
        pos_src,
    )
    if pm:
        name = re.sub(r"\s+", " ", pm.group(1)).strip(" ?'\"-,.")
        rows = db.query(models.MPSummary).filter(models.MPSummary.mp_name.ilike(f"%{name}%")).all()
        if not rows:
            return (
                f"I couldn't find an MP matching \"{name}\" in the dataset.",
                [],
            )
        if len(rows) > 1:
            listing = "\n".join(
                f"• **{r.mp_name}** ({r.house}, {r.constituency}, {r.state})"
                for r in rows[:6]
            )
            return (
                f"Several MPs match \"{name}\" — which one?\n\n{listing}",
                [_nav_action(label="Browse MPs", house=_extract_house(ql))],
            )
        r0 = rows[0]
        works_kind = pm.group(2)
        if works_kind:
            detail = mp_detail(db, r0.id) or {"works": []}
            want = works_kind
            sel = [
                w for w in detail.get("works", [])
                if (want in ("pending", "ongoing") and w["status"] == "Ongoing")
                or (want == "completed" and w["status"] == "Completed")
                or want == "recommended"
            ]
            sel.sort(key=lambda w: w["amount"], reverse=True)
            title = {"pending": "pending/ongoing", "ongoing": "ongoing",
                     "completed": "completed", "recommended": "recommended"}[want]
            lines = "\n".join(
                f"• {w['work_description'][:70]} — {_fmt_cr(w['amount'])}, {w['status']}"
                + (f" (spent {_fmt_cr(w['expenditure'])}, {w['expenditure_transactions']} payments)"
                   if w.get("expenditure_transactions") else "")
                for w in sel[:8]
            ) or "  (none recorded)"
            more = f"\n\n…and {len(sel) - 8} more — open the MP for the full list." if len(sel) > 8 else ""
            head = (f"**{r0.mp_name}** ({r0.house}, {r0.constituency}) — "
                    f"{len(sel)} {title} works, {_fmt_cr(sum(w['amount'] for w in sel))} total.")
            return (f"{head}\n\n{lines}{more}",
                    [_nav_action(r0, label=f"Open {r0.mp_name}'s works")])
        return _mp_answer(r0, ql)

    # ── 2. House-scoped MP statistics ───────────────────────────────
    if re.search(r"mp.{0,24}statistic|statistics.{0,24}mp|how many (lok|rajya) sabha mps?\b|mp intelligence", ql):
        house = _extract_house(ql)
        s = list_mp_intelligence(db, house=house, limit=1)["summary"]
        scope = house or "both houses combined"
        return (
            f"MP-level snapshot for **{scope}**: {s['total_mps']} MPs, "
            f"{_fmt_cr(s['total_allocated'])} allocated, {_fmt_cr(s['total_expenditure'])} spent, "
            f"average utilization {s['average_utilization_percentage']}%. "
            f"{s['total_completed_works']} of {s['total_recommended_works']} recommended works completed "
            f"(average completion rate {s['average_completion_rate_percentage']}%).",
            [_nav_action(label=f"Open MP Intelligence ({house or 'All'})", house=house)],
        )

    # ── 3. MPs in a state / top-spending MPs ────────────────────────
    # "Show me Lok Sabha MPs in Telangana" / "list MPs from Gujarat" etc.
    if re.search(r"\bmps?\b|members? of parliament", ql) and re.search(
        r"\bshow\b|\blist\b|\bwhich\b|\bhow many\b|\btop\b|\bfrom\b|\bin\b", ql
    ) and not re.search(r"\bprojects?\b", ql) and not _THIS_MP_RE.search(ql):
        house = _extract_house(ql)
        # State extractor: "... in Telangana", "... from Gujarat", or a bare
        # state name preceding "lok/rajya sabha" ("Telangana Lok Sabha MPs").
        state = None
        state_m = re.search(
            r"\b(?:in|from|of)\s+([a-z][a-z\s]{2,30}?)(?:\s+(?:lok|rajya)\s+sabha)?[?.]*\s*$",
            ql,
        ) or re.search(r"\b([a-z][a-z\s]{2,30}?)\s+(?:lok|rajya)\s+sabha\s+(?:mps?|members?)", ql)
        if state_m:
            state = (state_m.group(1) or "").strip()
            state = re.sub(r"\b(lok|rajya)\s+sabha\b|\bmps?\b|\bmembers? of parliament\b", "", state).strip()
        if state and len(state) > 3 and not re.search(r"\b(india|all)\b", state, re.I):
            rows = _mp_rows(db, house, state, limit=6)
            n = (
                db.query(func.count(models.MPSummary.id))
                .filter(models.MPSummary.state.ilike(f"%{state}%"))
                .filter(models.MPSummary.house == house if house else True)
                .scalar()
            )
            if rows:
                lines = "\n".join(
                    f"• **{r.mp_name}** ({r.house}, {r.constituency}) — spent {_fmt_cr(r.total_expenditure or 0)} "
                    f"of {_fmt_cr(r.allocated_amount or 0)} ({r.utilization_percentage or 0:.1f}%), "
                    f"{int(r.completed_works or 0)}/{int(r.recommended_works or 0)} works completed"
                    for r in rows
                )
                return (
                    f"MPs recorded for **{state.title()}**{(' · ' + house) if house else ''} "
                    f"({n} total, top by expenditure):\n\n{lines}",
                    [_nav_action(label=f"Open MP Intelligence ({state.title()})", house=house, state=state)],
                )
            return (
                f"No MPs recorded for \"{state}\" in the current snapshot.",
                [],
            )
        # No state — top MPs overall / for the house
        rows = _mp_rows(db, house, None, limit=6)
        scope = house or "All"
        lines = "\n".join(
            f"• **{r.mp_name}** ({r.house}, {r.constituency}, {r.state}) — spent {_fmt_cr(r.total_expenditure or 0)} "
            f"({r.utilization_percentage or 0:.1f}% utilization), "
            f"{int(r.completed_works or 0)}/{int(r.recommended_works or 0)} works completed"
            for r in rows
        )
        return (
            f"Top MPs by recorded expenditure ({scope}):\n\n{lines}",
            [_nav_action(label="Open MP Intelligence", house=house)],
        )

    # ── 4. Per-MP attribute questions: "How much has <MP> spent?" ───
    # Requires an explicit MP-name anchor ("this mp" without project context
    # is answered by the generic flows, not invented here).
    attr = re.search(
        r"(?:how much (?:has|have|did)|what (?:is|has)|how many works (?:has|have))\s+"
        r"(?:mp\s+)?([a-z][a-z\s.'-]{4,60}?)\s*"
        r"(?:spent|expenditure|utilized|utilisation|completed|recommended)?\s*"
        r"(?:\bso far\b|\buntil now\b)?[?.]*\s*$",
        ql,
    )
    if attr and re.search(r"\bmp\b|member of parliament|constituency|lok sabha|rajya sabha", ql):
        # Drop a trailing "mp"/"mps" token and filler words from the capture
        # ("how much has Mahesh Sharma MP spent" → "Mahesh Sharma").
        name = re.sub(r"\bmps?\b", " ", attr.group(1)).strip(" ?'\"-,.")
        name = re.sub(r"\s+", " ", name)
        if name.lower() in ("this", "that", "the", "a", "an", ""):
            # Session-anchored ("how much has this MP spent?") — resolve from
            # the MP Intelligence page context when available.
            mp = _mp_from_session(db, session_id)
            if mp is not None:
                return _mp_answer(mp, ql)
            return None
        rows = db.query(models.MPSummary).filter(models.MPSummary.mp_name.ilike(f"%{name}%")).all()
        if not rows:
            return (
                f"I couldn't find an MP matching \"{name}\" in the dataset.",
                [],
            )
        if len(rows) > 1:
            listing = "\n".join(
                f"• **{r.mp_name}** ({r.house}, {r.constituency}, {r.state})"
                for r in rows[:6]
            )
            return (
                f"Several MPs match \"{name}\" — which one?\n\n{listing}",
                [_nav_action(label="Browse MPs", house=_extract_house(ql))],
            )
        r = rows[0]
        recommended = int(r.recommended_works or 0)
        completed = int(r.completed_works or 0)
        return (
            f"**{r.mp_name}** ({r.house}, {r.constituency}, {r.state}): "
            f"allocated {_fmt_cr(r.allocated_amount or 0)}, spent {_fmt_cr(r.total_expenditure or 0)}, "
            f"unspent {_fmt_cr(r.unspent_amount or 0)}, utilization {r.utilization_percentage or 0:.2f}%. "
            f"Works: {completed} completed of {recommended} recommended "
            f"({r.completion_rate_percentage or 0:.2f}% completion rate).",
            [_nav_action(r, label=f"Open {r.mp_name}'s works")],
        )

    return None
