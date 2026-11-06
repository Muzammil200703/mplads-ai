"""
MPLADS AI Assistant engine
==========================

A deterministic, role-aware question router — NOT a generative LLM. Every
answer is either:

  1. Static knowledge-base guidance (general MPLADS / website help), or
  2. Composed from REAL database query results (SQL with LIMITs).

Nothing is invented: if no intent matches or data is missing, the assistant
says so. Role enforcement happens here on the server — the same
auth.require_* machinery the rest of the app uses — so a citizen can never
pull audit data regardless of what they type.

Memory discipline (512MB Render): every query is aggregated or LIMIT-ed;
nothing is cached beyond FastAPI's existing bounded cache; no pandas.
"""

import re
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import case, exists, func, or_, text
from sqlalchemy.orm import Session

import auth
import models
import mp_assistant
import investigation_intents
import assistant_nav
import assistant_nlu
import assistant_analytics
import assistant_review

router = APIRouter(prefix="/assistant", tags=["AI Assistant"])

_bearer = HTTPBearer(auto_error=False)


def get_db():
    from database import SessionLocal
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _optional_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    db: Session = Depends(get_db),
) -> Optional[models.User]:
    """Best-effort identity — the assistant works for guests too. A valid
    token attaches the user so role-gated intents can unlock."""
    if credentials is None or not credentials.credentials:
        return None
    uid = auth._decode_token(credentials.credentials)
    if uid is None:
        return None
    return db.query(models.User).filter(models.User.id == uid, models.User.is_active.is_(True)).first()


# ═══════════════════════════════════════════════════════════════════
# Constants / knowledge base
# ═══════════════════════════════════════════════════════════════════

TIER_MEANING = {
    "P1": "P1 — Critical: investigate immediately (highest risk score, biggest financial exposure, strongest audit flags).",
    "P2": "P2 — High: schedule review this week (strong risk indicators or large funds at stake).",
    "P3": "P3 — Medium: routine monitoring (some indicators present).",
    "P4": "P4 — Low: healthy-looking on current data (few or weak indicators).",
}

KNOWLEDGE = [
    # (patterns, answer_key) — answer keys resolved against _knowledge_answer()
    (r"what\s+is\s+mplads|mplads\s+mean|about\s+mplads", "what_is_mplads"),
    (r"purpose\s+of\s+(this\s+)?(website|site|app|platform)|what\s+(does|can)\s+(this|the)\s+(site|website|app)\s+do", "purpose"),
    (r"what\s+can\s+i\s+do|how\s+(does|do)\s+(this|the)\s+(site|website|app)\s+work|sections?\s+of\s+(the\s+)?(website|app)", "sections"),
    (r"how\s+(does|do)\s+mplads\s+work|mplads\s+fund|who\s+funds", "how_mplads_works"),
    (r"ground\s+(truth\s+)?verif", "ground_verification"),
    (r"(submit|upload|report).{0,30}(evidence|photo|project)|how\s+can\s+i\s+report", "submit_evidence"),
    (r"what\s+happens?\s+after\s+i\s+(submit|upload)", "after_submit"),
    (r"who\s+verif", "who_verifies"),
    (r"what\s+is\s+(a\s+)?risk\s+score|risk\s+score\s+mean|risk\s+level", "risk_score"),
    (r"\bp[1-4]\b.{0,20}(mean|priority)|what\s+does\s+p[1-4]|priority\s+tier", "priority_tiers"),
    (r"isolation\s+forest|anomaly\s+detection|anomaly\s+model|how\s+does\s+the\s+ml|machine\s+learning\s+(model|work)|what\s+is\s+ml\b", "ml_anomaly"),
    (r"what\s+is\s+vendor\s+intelligence|vendor\s+intelligence", "vendor_intelligence"),
    (r"what\s+is\s+vendor\s+network|vendor\s+network", "vendor_network"),
    (r"what\s+is\s+ai\s+audit|audit\s+command\s+center|ai\s+audit\s+center", "ai_audit"),
    (r"what\s+happens?.{0,20}inquiry|inquiry\s+work|what\s+is\s+an?\s+inquiry", "inquiry_flow"),
    (r"search\s+for\s+a\s+project|how\s+do\s+i\s+search|find\s+a\s+project|how\s+to\s+filter|use\s+the\s+filters?", "search_help"),
    (r"citizen\s+evidence|evidence\s+submission", "citizen_evidence"),
    # ── Upgrade additions: status vocabulary, reporting, verifying ──
    (r"what\s+(do|does)\s+(the\s+)?status.{0,20}(mean)|what\s+does\s+ongoing|status\s+mean|project\s+statuses?", "status_meanings"),
    (r"report\s+an\s+issue|report\s+a\s+problem|something\s+wrong|report\s+incorrect|report\s+corrupt", "report_issue"),
    (r"how\s+(do|can)\s+i\s+verify\s+a\s+project|verify\s+a\s+project|check\s+a\s+project\s+on?\s*the\s+ground", "verify_project"),
    (r"why\s+is\s+gps|why\s+location|do\s+i\s+need\s+(to\s+)?(share\s+)?gps", "why_gps"),
    (r"what\s+(does|do)\s+.{0,12}(flagged|flag)\s+mean|flagged\s+for\s+review", "flagged_meaning"),
    # not_fraud BEFORE risk_levels_meaning — "does high risk mean fraud?"
    # deserves the careful language answer, not the level definitions.
    (r"mean\s+fraud|means?\s+wrongdoing|does\s+high\s+risk\s+mean\s+fraud|prove\s+(fraud|corruption|wrongdoing)|is\s+(it|that|this)\s+fraud", "not_fraud"),
    (r"what\s+does\s+(high|medium|low)\s+risk\s+mean", "risk_levels_meaning"),
    (r"what\s+is\s+audit\s+priority|audit\s+priority\s+mean|what\s+does\s+audit\s+priority", "priority_tiers"),
    (r"what\s+is\s+an\s+anomaly|anomal(y|ies)\s+mean", "ml_anomaly"),
    (r"what\s+is\s+(a\s+)?project\?*$|^what\s+is\s+a\s+project", "what_is_project"),
    (r"what\s+is\s+(the\s+)?risk\s+center|risk\s+center", "risk_center"),
    (r"why\s+is\s+(this\s+|my\s+)?(project\s+|it\s+)?p[1-4]\b", "priority_tiers"),
    (r"how\s+(do|can)\s+i\s+compare|compare\s+(two|projects)", "compare_help"),
    (r"how\s+(do|can)\s+i\s+check\s+a\s+vendor|check\s+vendors?|vendor\s+.{0,10}search", "vendor_intelligence"),
    (r"how\s+(do|can)\s+i\s+(see|find|view)\s+high.risk|see\s+risky", "search_help"),
    (r"how\s+(do|can)\s+i\s+respond\s+to\s+an?\s+inquiry", "respond_inquiry"),
    (r"how\s+(do|can)\s+i\s+use\s+vendor\s+network", "vendor_network"),
    # ── Intelligence overhaul: methodology, glossary, risk language ──
    (r"\bshap\b|what\s+does\s+shap|shap\s+(value|do|does)", "shap"),
    (r"project\s+dna|how\s+does\s+dna|dna\s+work|dna\s+(is|mean)", "project_dna"),
    (r"project\s+twins?|how\s+are.{0,20}twins|twins?\s+(selected|found|work|mean)", "project_twins"),
    (r"stress\s+test|what.if.{0,20}risk|hypothetical.{0,20}(score|risk)", "stress_test"),
    (r"how\s+does\s+audit\s+priority|audit\s+priority.{0,15}(work|computed|calculated)", "priority_tiers"),
    (r"mean\s+fraud|means?\s+wrongdoing|does\s+high\s+risk\s+mean|prove\s+(fraud|corruption|wrongdoing)|is\s+(it|that)\s+fraud", "not_fraud"),
    (r"current\s+risk.{0,10}predictive|predictive\s+risk|risk.{0,15}(current|future)", "current_vs_predictive"),
    # ── Domain glossary ──
    (r"what\s+(is|does).{0,12}(the\s+)?utilization|utilization\s+(mean|percentage)", "gloss_utilization"),
    (r"what\s+(is|does).{0,12}(the\s+)?sanctioned|sanctioned\s+amount\s+mean", "gloss_sanctioned"),
    (r"what\s+is\s+(a\s+)?sanction(?!ed)|sanction\s+mean", "gloss_sanction"),
    (r"what\s+is\s+(a\s+)?recommendation|recommendation\s+mean", "gloss_recommendation"),
    (r"implementing\s+agency|what\s+is\s+(an\s+)?ida\b|\bida\s+mean", "gloss_ida"),
    (r"what\s+(is|does).{0,12}expenditure|expenditure\s+mean", "gloss_expenditure"),
    (r"what\s+(is|does).{0,12}(physical\s+)?completion|completion\s+(mean|percentage)", "gloss_completion"),
]

KNOWLEDGE_ANSWERS = {
    "what_is_mplads": (
        "**MPLADS** (Member of Parliament Local Area Development Scheme) gives every MP a yearly fund "
        "(₹5 crore) for development works in their constituency — roads, school buildings, water supply, "
        "community halls, and similar public assets. This portal monitors the resulting projects: their "
        "sanctioned amounts, expenditure, physical progress, and risk indicators."
    ),
    "purpose": (
        "This platform helps auditors, analysts, citizens and officials monitor MPLADS projects "
        "transparently: browse the project portfolio, see risk scores and anomalies, verify work on the "
        "ground, and follow up flagged projects through inquiries and audit cases."
    ),
    "sections": (
        "**Main sections**\n"
        "• Overview — portfolio dashboard and AI insights\n"
        "• Projects — search/filter all projects\n"
        "• Risk Center — risk scores and delay indicators\n"
        "• State Intelligence — state-wise analysis\n"
        "• Reports — exportable summaries\n"
        "• AI Audit Center — anomaly detection (auditors)\n"
        "• Audit Priority — P1–P4 review ranking (auditors)\n"
        "• Vendor Network / Vendor Intelligence — vendor analysis\n"
        "• Ground Verification — submit and review field evidence (everyone)"
    ),
    "how_mplads_works": (
        "MPLADS flow: the central government releases funds to each MP's authority → the MP recommends "
        "works to an implementing agency (district authority) → projects are sanctioned and executed → "
        "expenditure is recorded against each project. This portal tracks the recorded projects, money and "
        "progress, and adds risk analytics on top of the published data."
    ),
    "ground_verification": (
        "**Ground Verification** lets anyone strengthen the data with on-the-ground evidence: look up a "
        "project by its ID, take a photo, capture your GPS location, and report what you actually see "
        "(Functional / Non-Functional / Work Not Started). Submissions go into a verification queue — "
        "they are evidence, not official status changes."
    ),
    "submit_evidence": (
        "To submit evidence: open **Ground Verification** in the sidebar, enter the project's numeric ID "
        "(shown as #12345 on any project card), optionally take/upload a photo, capture your location, pick "
        "the status you observe, and submit. Signed-in citizens can see all their own submissions under "
        "**My Verifications**."
    ),
    "after_submit": (
        "After you submit: your report (photo, GPS, status, note, timestamp) enters the verification "
        "queue. A field verifier reviews it and marks it Verified or Rejected with a note. Auditors can "
        "escalate disputed evidence for deeper review. The official project record is never changed by "
        "evidence submissions."
    ),
    "who_verifies": (
        "Citizen evidence is reviewed by **Field Verifiers** — personnel assigned to physically check "
        "projects. If evidence looks suspicious or is disputed, **Auditors** can independently review it "
        "and escalate."
    ),
    "risk_score": (
        "A **risk score** (0–100) flags projects that may deserve a closer look. It combines financial "
        "utilization (e.g. high spend with 0% progress), physical progress vs. money spent, project "
        "status and age, delays, and statistical anomalies vs. similar projects. A high score is a "
        "priority signal — **not** proof of wrongdoing."
    ),
    "ml_anomaly": (
        "**Isolation Forest** is an anomaly-detection model this platform runs alongside its rule-based "
        "risk checks. It learns the statistical profile of similar MPLADS projects (amounts, progress, "
        "utilization patterns) and flags records that stand apart from their peers. An ML flag adds up to "
        "+25 to the rule-based risk score and is one input among several — it indicates \u201cunusual compared "
        "to similar projects\u201d, never wrongdoing by itself."
    ),
    "priority_tiers": (
        "• P1 — Critical: investigate first\n"
        "• P2 — High: review this week\n"
        "• P3 — Medium: routine monitoring\n"
        "• P4 — Low: healthy on current data\n\n"
        "Tiers are computed from the composite priority score (risk index, financial exposure, evidence "
        "gaps, delay indicators)."
    ),
    "vendor_intelligence": (
        "**Vendor Intelligence** (BETA) aggregates every vendor appearing in the expenditure records: "
        "total paid, transaction counts, number of distinct projects, states/constituencies served. "
        "Search a vendor by name, state or constituency and open a profile with its engagements."
    ),
    "vendor_network": (
        "**Vendor Network** (BETA) visualizes possible vendor clusters — groups sharing a registered "
        "office or similar name families — as a graph. Flags are investigative leads for auditors, not "
        "proof of collusion."
    ),
    "ai_audit": (
        "The **AI Audit Command Center** scans all projects for anomaly indicators — photo duplication, "
        "cost outliers vs. category/state benchmarks, GPS mismatches, delayed SLAs — and ranks them on a "
        "0–100 audit risk index. Auditors inspect flagged projects and record dispositions."
    ),
    "inquiry_flow": (
        "Inquiry workflow: an **Auditor** issues an inquiry on a flagged project (from the inspection "
        "modal) → the **District Authority** for that area sees it in My District and posts an official "
        "response → the Auditor reviews the response and closes the inquiry. Everything is timestamped "
        "with the responder's identity."
    ),
    "search_help": (
        "Finding projects: use **Projects** in the sidebar. The search box matches project names/IDs; the "
        "filters narrow by state, constituency, district, status, project type and financial year. The "
        "header search jumps straight to matching projects from any page."
    ),
    "citizen_evidence": (
        "**Citizen Evidence** is the public layer of Ground Verification: any signed-in citizen can report "
        "what a project looks like on the ground with a photo, GPS and status. Each submission is "
        "reviewed by a field verifier before it counts as verified evidence — and it never alters "
        "official project data."
    ),
    # ── Upgrade additions ──
    "what_is_project": (
        "In this portal, a **project** is one sanctioned MPLADS work — a road, school building, water "
        "supply, community hall or similar public asset — identified by a numeric ID (shown as #12345). "
        "Each carries its sanctioned amount, recorded expenditure, physical progress, status and risk "
        "indicators."
    ),
    "status_meanings": (
        "**Project statuses**\n"
        "• Ongoing — work is sanctioned and in progress (or not yet reported complete)\n"
        "• Completed — the work is reported as finished\n"
        "Status comes from the published MPLADS data; evidence you submit does not change it."
    ),
    "report_issue": (
        "To report an issue with a project: open **Ground Verification**, enter the project ID, and "
        "submit what you observe — a photo, your GPS location, and the status you see (e.g. "
        "**Work Not Started** or **Non-Functional**), plus an optional note. Field verifiers review every "
        "submission. It never alters official records, but it creates a timestamped evidence trail."
    ),
    "verify_project": (
        "To verify a project: open **Ground Verification** in the sidebar → enter the project's numeric "
        "ID → take or upload a photo → capture your GPS location → pick the status you actually observe "
        "(Functional / Non-Functional / Work Not Started) → submit. Signed-in users can track their "
        "submissions under **My Verifications**."
    ),
    "why_gps": (
        "GPS tells reviewers **where** the photo was taken. Matching coordinates help a field verifier "
        "confirm the evidence belongs to that project's site — evidence without location is still "
        "accepted, just weaker. Sharing your location is optional."
    ),
    "flagged_meaning": (
        "**Flagged** means the system's checks marked a project as worth a human look — e.g. money spent "
        "with no reported progress, or statistical outliers vs. similar projects. It is a review signal, "
        "**not** a finding of wrongdoing. Auditors verify flagged projects before any conclusion."
    ),
    "risk_levels_meaning": (
        "**Risk levels**\n"
        "• High — multiple strong indicators; review soon\n"
        "• Medium — some indicators; worth monitoring\n"
        "• Low — few weak indicators\n"
        "• None — nothing flagged on current data\n\n"
        "Levels come from the 0–100 risk score. A high level flags a project for review — it is not "
        "proof of wrongdoing."
    ),
    "risk_center": (
        "The **Risk Center** ranks projects by their 0–100 risk score with the contributing factors "
        "(fund utilization, progress mismatch, delays), plus first/latest expenditure dates and the "
        "estimated 'Delayed By' duration. A high score is a review priority, not a finding."
    ),
    "compare_help": (
        "To compare projects: open **Compare Projects**, pick two projects, and you'll see sanctioned "
        "amounts, expenditure, physical progress and risk factors side by side. You can also ask me "
        "about a specific project by ID (e.g. \u201ctell me about project 80649\u201d) and I'll pull its record."
    ),
    "respond_inquiry": (
        "Responding to an inquiry: open **My District** — auditor inquiries for your assigned area are "
        "listed there with the project, the auditor's question and a deadline. Write your official "
        "response and submit; the issuing auditor is notified and reviews it. Every response is "
        "timestamped with your identity and role."
    ),
    # ── Intelligence overhaul: methodology & risk language ──
    "shap": (
        "**SHAP** (SHapley Additive exPlanations) explains *why* the anomaly model flagged a project. "
        "For each project the Isolation Forest scores, SHAP attributes the score to the input features "
        "(sanctioned amount, expenditure, completion, utilization pattern, peer-group size…). This "
        "platform translates those values into plain-language lines such as \u201cexpenditure is high "
        "compared with similar projects\u201d, so an ML flag comes with its reasons — it is an explanation "
        "of a statistical signal, never a finding of wrongdoing."
    ),
    "project_dna": (
        "**Project DNA** builds an analytical fingerprint of a project from the attributes actually "
        "present in the dataset — identity, money, progress, timeline, risk and location dimensions, "
        "each labelled with how it was derived. Dimensions with no recorded data are omitted, never "
        "estimated. Open a project's details and ask me \u201cshow me this project's DNA\u201d to see it."
    ),
    "project_twins": (
        "**Project Twins** are a project's closest analytical peers. Similarity is computed as a "
        "weighted match over attribute dimensions **within the same state and project-type cohort** — "
        "the same indexed filters as the existing similar-projects endpoint, so it is attribute "
        "similarity, not name matching. Twins help answer \u201cis this project normal for its cohort?\u201d "
        "with comparable real records."
    ),
    "stress_test": (
        "**Risk Stress Test** is an interactive what-if on a project's risk score: change hypothetical "
        "values (expenditure, completion, utilization) and the EXISTING rule engine re-scores them. "
        "The re-scoring path is the same rule code as production scoring, the stored ML flag is carried "
        "forward as a constant, nothing is written to the database, and stored scores are untouched — "
        "results are clearly labelled as a simulation."
    ),
    "not_fraud": (
        "**No.** A high risk score means the recorded data triggered review checks — e.g. money spent "
        "with no reported progress, or values that are statistical outliers vs. similar projects. It "
        "is an audit-priority signal that says \u201clook at this project\u201d, not a finding, accusation or "
        "proof of wrongdoing. Unusual values can and do occur in genuine source data; conclusions "
        "require field verification and supporting records."
    ),
    "current_vs_predictive": (
        "Both, clearly separated:\n"
        "• **Current risk** — the 0–100 score from rule checks on the project's own recorded data "
        "(utilization, progress mismatch, status/progress contradictions) plus an ML anomaly signal "
        "comparing it with similar projects today.\n"
        "• **Predictive/what-if** — the **Risk Stress Test**, which re-scores hypothetical values "
        "through the same rule engine as a clearly-labelled simulation. Nothing predictive changes the "
        "stored score."
    ),
    # ── Domain glossary ──
    "gloss_utilization": (
        "**Utilization** = recorded expenditure ÷ sanctioned amount × 100. It shows how much of the "
        "approved money has been spent so far. On this platform expenditure comes from the payment "
        "ledger and sanctioned amounts from the project catalog; both are real recorded values, so "
        "utilization is always calculated — never estimated."
    ),
    "gloss_sanctioned": (
        "**Sanctioned amount** is the money formally approved for a specific work after the MP's "
        "recommendation — the administrative approval that authorizes spending. On project cards it "
        "is the catalog value (e.g. ₹5.00 L = ₹5 lakh); expenditure is tracked against it."
    ),
    "gloss_sanction": (
        "A **sanction** is the formal administrative approval that releases funds for a recommended "
        "work. Flow: the MP recommends a work → the district authority sanctions it → execution starts "
        "→ expenditure is recorded. A recommendation without a sanction has not yet been funded."
    ),
    "gloss_recommendation": (
        "A **recommendation** is an MP proposing a specific work (road, school building, water supply…) "
        "to be funded from their MPLADS allotment. The recommending MP, date and house are recorded "
        "with each recommended work — this portal never guesses them when they're missing."
    ),
    "gloss_ida": (
        "The **implementing agency (IDA)** is the authority that actually executes a work — typically "
        "the district authority/magistrate's office under MPLADS. It implements the sanctioned work "
        "and is recorded separately from the recommending MP, vendors and contractors."
    ),
    "gloss_expenditure": (
        "**Expenditure** is money actually paid out for a work, recorded as transactions in the payment "
        "ledger (with date, amount and vendor). This platform sums the ledger payments linked to each "
        "project's work key; amounts absent from the ledger are reported as \u201cno expenditure "
        "recorded\u201d, never filled in."
    ),
    "gloss_completion": (
        "**Physical completion** is the reported percentage of the work physically done (0–100%), from "
        "the published data. It is distinct from financial utilization — money spent ≠ work finished, "
        "and large gaps between the two are exactly what the rule-based risk checks flag for review."
    ),
}


# ═══════════════════════════════════════════════════════════════════
# Schemas
# ═══════════════════════════════════════════════════════════════════

class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    page: Optional[str] = Field(None, max_length=60)
    session_id: Optional[str] = Field(None, max_length=64)


class ActionOut(BaseModel):
    """Structured action contract.

    The frontend executes actions directly (JARVIS-style "understand →
    execute → confirm") from this structured payload — it never parses the
    answer text. `auto` marks actions the assistant has ALREADY performed
    server-side (navigation/filtering are read-only and reversible), so the
    client runs them without waiting for a click; `auto=False` leaves the
    chip purely as an affordance.

    Validation is a strict allowlist: unknown action types, page targets and
    param keys are dropped by `_valid_actions` before the response leaves the
    server, so assistant output can never drive arbitrary client behavior."""

    label: str
    action: str            # "navigate" | "open_project" (extensible below)
    target: str
    params: Dict = Field(default_factory=dict)   # validated against ACTION_PARAM_KEYS
    auto: bool = False     # executed by the client immediately — no click needed


# Page keys the client can navigate to (must mirror App.jsx page keys).
NAV_TARGETS = {
    "Overview", "Projects", "Risk Center", "AI Audit Center", "Vendor Network",
    "Ground Truth Verification", "Reports", "State Intelligence", "Audit Priority",
    "Compare Projects", "FAQ", "Vendor Intelligence", "Settings", "Saved Projects",
    "MP Intelligence",
    "My Investigations", "My Audit Cases", "My Verifications", "My District",
    "Inquiries", "Evidence Queue", "Administration", "Data Management",
}

# Every param key the client will consume, per action type.
ACTION_PARAM_KEYS = {
    "navigate": {"state", "constituency", "status", "risk_level", "tier", "fy", "keyword", "house", "mp_id"},
    # open_project targets a numeric project id; "tab" deep-links a drawer
    # tab (overview/financial/risk/forensics/audit/timeline/similar/dna) —
    # the client clamps unknown values to its default tab.
    "open_project": {"tab"},
    "filter_projects": {"state", "constituency", "status", "fy", "keyword", "house"},
    "filter_risk": {"risk_level", "state", "constituency"},
    "clear_filters": set(),
    "refresh_data": set(),
}


# Assistant 2.0: the registry is the single source of truth for destinations
# and each destination's supported parameters. The older constants remain as
# the filter-action contract; navigate is narrowed per page below.
NAV_TARGETS = assistant_nav.NAV_TARGETS
ACTION_PARAM_KEYS = {**ACTION_PARAM_KEYS, **assistant_nav.ACTION_PARAM_KEYS}


def _valid_actions(actions: List[ActionOut]) -> List[ActionOut]:
    """Enforce the action allowlist. Anything unrecognized — unknown type,
    unknown page target, unexpected param keys or an open_project target that
    isn't a plain integer — is dropped here, so only safe, known actions ever
    reach the client."""
    out = []
    for a in actions or []:
        if a.action not in ACTION_PARAM_KEYS:
            continue
        if a.action == "open_project":
            if not re.fullmatch(r"\d{1,10}", str(a.target)):
                continue
        elif a.action in ("clear_filters", "refresh_data"):
            pass  # utility actions carry no target — params check below suffices
        elif str(a.target) not in NAV_TARGETS:
            continue
        # (open_project targets are validated below by the integer check.)
        allowed = ACTION_PARAM_KEYS[a.action]
        clean = (assistant_nav.validate_params(str(a.target), a.params or {})
                 if a.action == "navigate"
                 else {k: v for k, v in (a.params or {}).items() if k in allowed and v not in (None, "")})
        # Re-validate label length so a pathological label can't bloat the UI.
        a = a.model_copy(update={"params": clean, "label": str(a.label)[:60]})
        out.append(a)
    return out[:4]


class AskResponse(BaseModel):
    answer: str
    source: str            # "knowledge" | "data" | "help"
    actions: List[ActionOut] = []
    project_id: Optional[int] = None   # project discussed — client keeps it for follow-ups
    # ── Assistant 2.0 structured payload (all optional; old clients ignore it) ──
    intent: str = "help"
    entity: Dict[str, Any] = Field(default_factory=dict)
    findings: List[Dict[str, Any]] = Field(default_factory=list)
    recommendations: List[Dict[str, Any]] = Field(default_factory=list)
    limitations: List[str] = Field(default_factory=list)
    table: Optional[Dict[str, Any]] = None


# ── Conversational session memory ────────────────────────────────────
# Remembers each conversation's last project AND last action context so
# pronoun follow-ups ("why is it risky?", "open it") and context-additive
# filter requests ("only the high risk ones") resolve without re-typing.
# Bounded: TTL expiry + hard cap, values are tiny dicts.

_SESSION_LAST_PROJECT: Dict[str, Dict] = {}
_SESSION_LOCK = threading.Lock()
_SESSION_TTL = 3600.0     # seconds
_SESSION_MAX = 500        # hard cap on remembered sessions


def _session_get(session_id: Optional[str]) -> Optional[Dict]:
    if not session_id:
        return None
    now = time.time()
    with _SESSION_LOCK:
        entry = _SESSION_LAST_PROJECT.get(session_id)
        if not entry or now - entry["ts"] > _SESSION_TTL:
            _SESSION_LAST_PROJECT.pop(session_id, None)
            return None
        entry["ts"] = now
        return entry


def _session_get_project(session_id: Optional[str]) -> Optional[int]:
    entry = _session_get(session_id)
    return entry.get("project_id") if entry else None


def _session_get_ctx(session_id: Optional[str]) -> Dict:
    """Last action context for this session — e.g. {"page": "Projects",
    "filters": {"state": "Telangana"}} — used by follow-up filter requests."""
    entry = _session_get(session_id)
    if not entry:
        return {}
    ctx = entry.get("ctx") or {}
    return dict(ctx) if isinstance(ctx, dict) else {}


def _session_get_state(session_id: Optional[str]) -> Optional[str]:
    """The state named in this session's most recent filtered/summary answer
    ("show Telangana projects" → "Telangana") so follow-ups like "give me a
    summary of it" resolve without re-typing."""
    entry = _session_get(session_id)
    if not entry:
        return None
    v = (entry.get("ctx") or {}).get("state")
    return str(v) if v else None


def _session_get_results(session_id: Optional[str]) -> List[int]:
    """Project ids from the most recent list/analytics answer in this session.
    Used to resolve 'the first one', 'the second project', 'the last one'."""
    entry = _session_get(session_id)
    if not entry:
        return []
    ids = (entry.get("ctx") or {}).get("result_ids") or []
    out: List[int] = []
    for value in ids[:12]:
        try:
            out.append(int(value))
        except (TypeError, ValueError):
            continue
    return out


def _session_set_results(session_id: Optional[str], project_ids) -> None:
    """Remember the bounded project-id list behind the latest answer."""
    if not session_id:
        return
    ids: List[int] = []
    for value in (project_ids or [])[:12]:
        try:
            pid = int(value)
        except (TypeError, ValueError):
            continue
        if 0 < pid < 10 ** 10:
            ids.append(pid)
    if not ids:
        return
    now = time.time()
    with _SESSION_LOCK:
        entry = _SESSION_LAST_PROJECT.get(session_id) or {"ts": now}
        entry.setdefault("ctx", {})["result_ids"] = ids
        entry["ts"] = now
        _SESSION_LAST_PROJECT[session_id] = entry


def _session_set_ctx(session_id: Optional[str], page: Optional[str] = None,
                     filters: Optional[Dict] = None, project_id: Optional[int] = None,
                     state: Optional[str] = None) -> None:
    """Store/merge the session's action context. Bounded values only."""
    if not session_id:
        return
    now = time.time()
    with _SESSION_LOCK:
        # Opportunistic TTL sweep keeps the dict bounded without a job.
        if len(_SESSION_LAST_PROJECT) >= _SESSION_MAX:
            expired = [k for k, v in _SESSION_LAST_PROJECT.items() if now - v["ts"] > _SESSION_TTL]
            for k in expired:
                _SESSION_LAST_PROJECT.pop(k, None)
        while len(_SESSION_LAST_PROJECT) >= _SESSION_MAX:
            _SESSION_LAST_PROJECT.pop(next(iter(_SESSION_LAST_PROJECT)))
        entry = _SESSION_LAST_PROJECT.get(session_id) or {"ts": now}
        if page is not None:
            entry["ctx"] = {**(entry.get("ctx") or {}), "page": page, "filters": filters or {}}
        if project_id:
            entry["project_id"] = int(project_id)
        if state:
            entry.setdefault("ctx", {})["state"] = str(state)[:60]
        entry["ts"] = now
        _SESSION_LAST_PROJECT[session_id] = entry


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════

def _fmt_money(v) -> str:
    v = float(v or 0)
    if v >= 1e7:
        return f"₹{v/1e7:.2f} Cr"
    if v >= 1e5:
        return f"₹{v/1e5:.2f} L"
    return f"₹{v:,.0f}"


def _extract_state(question: str) -> Optional[str]:
    """Match a state name mentioned in the question against the real state list."""
    m = re.search(
        r"(?:in|from|of|for)\s+([a-z][a-z \.&']{2,40}?)(?:\?|$|,|\s+(?:show|which|what|how|list|give|tell))",
        question, re.I,
    )
    if not m:
        return None
    candidate = m.group(1).strip(" ?.,")
    return candidate or None


_STATE_NOUN_RE = re.compile(r"\b(projects?|works?|constituencies?)\b", re.I)

_HOUSE_RE = re.compile(r"\b(lok\s+sabha|rajya\s+sabha)\b", re.I)


def _extract_house(q: str) -> Optional[str]:
    """Explicit house mention in a question — "show Lok Sabha projects in
    Telangana" → "Lok Sabha". Canonical capitalization; None when no house
    is named (the app-wide default is then All, never a guess)."""
    m = _HOUSE_RE.search(q or "")
    if not m:
        return None
    return "Lok Sabha" if m.group(1).lower().startswith("lok") else "Rajya Sabha"


def _state_noun_candidates(question: str) -> List[str]:
    """State-before-noun candidates: for every 'projects/works/constituencies'
    occurrence, the one and two words immediately before it ("show Telangana
    projects" → ["telangana", "show telangana"]). Callers validate each
    against the DB state list, so ordinary phrases ("high risk projects")
    can never resolve."""
    out: List[str] = []
    for m in _STATE_NOUN_RE.finditer(question):
        pre = question[: m.start()].strip(" ,.?!:;-")
        if not pre:
            continue
        words = re.findall(r"[a-z&'\.]+", pre, re.I)
        if not words:
            continue
        out.append(words[-1].lower())
        if len(words) >= 2:
            out.append(f"{words[-2].lower()} {words[-1].lower()}")
    return out


# ── Canonical filter-parameter helpers ───────────────────────────────
# Filter actions must carry values the existing pages actually accept
# (exact state names from the DB, "2025-26" FY strings, capitalized
# statuses/tiers). Raw natural-language fragments would silently filter to
# nothing, so each helper resolves against real data before attaching.

_STATE_CACHE: Dict[str, str] = {}


def _canonical_state(ql: str, db: Session) -> Optional[str]:
    """Resolve a state mentioned in the question to the exact DB name.
    Cached per-process (the state list is static within a deployment)."""
    mentioned = _extract_state(ql)
    candidates = [mentioned] if mentioned else []
    candidates.extend(c for c in _state_noun_candidates(ql) if c not in candidates)
    if not _STATE_CACHE:
        rows = db.query(models.Project.state).distinct().all()
        for (s,) in rows:
            if s:
                _STATE_CACHE[s.strip().lower()] = s.strip()
    for candidate in candidates:
        hit = _STATE_CACHE.get(candidate.strip().lower())
        if hit:
            return hit
    # Direct mention fallback: "Telangana in State Intelligence" has no
    # state-before-noun shape, but still names a real state. Scan the bounded
    # distinct-state list (longest names first so "Andhra Pradesh" wins over
    # any shorter accidental substring).
    for name_lower, name in sorted(_STATE_CACHE.items(), key=lambda item: -len(item[0])):
        if re.search(rf"\b{re.escape(name_lower)}\b", ql):
            return name
    return None


_FY_RE = re.compile(r"\b(20\d{2}\s*[-\u2013]\s*\d{2,4})\b")


def _canonical_fy(ql: str, db: Session) -> Optional[str]:
    """Resolve a mentioned financial year ("2025-26", "2025 - 26") to the FY
    string the catalog actually stores. Returns None when no FY is mentioned
    or the mentioned FY has no projects (an empty filter would look broken)."""
    m = _FY_RE.search(ql)
    if not m:
        return None
    norm = re.sub(r"\s+", "", m.group(1)).replace("\u2013", "-")
    for row in db.query(models.Project.fy).distinct().all():
        if row[0] and row[0].replace("\u2013", "-").replace("-", "") == norm.replace("-", ""):
            return row[0]
    return None


STATUS_MAP = {
    "completed": "Completed", "complete": "Completed", "finished": "Completed",
    "ongoing": "Ongoing", "in progress": "Ongoing", "under way": "Ongoing",
    "recommended": "Recommended", "proposed": "Recommended",
}


def _extract_status(ql: str) -> Optional[str]:
    """Map a status mention to the page's exact option value."""
    if re.search(r"\bcompleted\b|\bfinished\b", ql):
        return STATUS_MAP["completed"]
    if re.search(r"\bongoing\b|\bin progress\b", ql):
        return STATUS_MAP["ongoing"]
    if re.search(r"\brecommended\b|\bproposed\b", ql):
        return STATUS_MAP["recommended"]
    return None


_TIER_RE = re.compile(r"\bp([1-4])\b", re.I)


def _extract_tier(ql: str) -> Optional[str]:
    """"P1".."P4" priority-tier mentions → AuditPriority filter values.
    A generic high-priority ask maps to P1 (the queue's top tier) rather than
    being silently dropped."""
    m = _TIER_RE.search(ql)
    if m:
        return f"P{m.group(1)}"
    if re.search(r"\b(high|top)[- ]priority\b|\burgent\b", ql):
        return "P1"
    return None


_NAV_VERB_RE = re.compile(
    r"^(please\s+)?(can\s+you\s+|could\s+you\s+)?"
    r"(find|search(\s+for)?|show(\s+me)?|open|look\s+up|display|get)\b", re.I)
_STRIP_TOKENS_RE = re.compile(
    r"^(the|a|an|all|for|me|my)\b|(projects?|works?|page|list|tab|explorer)s?\b", re.I)

# Phrasings owned by the dedicated data/screen intents — a bare name search
# must never swallow them ("show high-risk projects" is a screen, not a
# project called "high-risk").
_DATA_SCREEN_RE = re.compile(
    r"high[- ]risk|riskiest|anomal(y|ies)|unusual|suspicious|delayed|mismatch"
    r"|cost deviation|vendor|concentration|\bp[1-4]\b|priorit"
    r"|expenditure.{0,20}(and|with|but).{0,20}(low|no|zero)\s+(progress|completion)"
    r"|(low|no|zero)\s+(progress|completion).{0,20}(and|with|but).{0,20}high expenditure"
    r"|\b(completed|ongoing|recommended)\s+(projects?|works?|ones?)\b", re.I)

# Questions scoped to the WHOLE portfolio rather than the project open in the
# drawer — they must never be answered with the open project's data. Deliberately
# narrow: counting/aggregation phrasings only, so project-scoped asks like "is
# there an overspend?" or "is expenditure above sanctioned?" stay with the
# project-context intent.
_PORTFOLIO_SCOPED_RE = re.compile(
    r"how many\b|what (percentage|percent|share of)\b|\baverage\b|\bavg\b"
    r"|\boverall\b|\bportfolio\b|in total\b"
    r"|total (spent|expenditure|sanctioned|allocation)"
    r"|which (state|states)\b|most projects|highest utilization", re.I)


def _extract_vendor_query(q: str) -> str:
    """Vendor name for a Vendor Intelligence deep-link. Never returns the
    page name itself ("vendor intelligence") as a vendor."""
    m = re.search(
        r"\bvendor[s]?\s+(?:named\s+|called\s+)?([A-Za-z][\w&.'\- ]{1,60}?)(?:\s+in\s+|\s+from\s+|\?|$)",
        q or "", re.I,
    )
    if not m:
        return ""
    stop = {"intelligence", "network", "profile", "profiles", "analytics", "page",
            "vendor", "vendors", "in", "from", "at", "for", "and", "the", "a", "an", "of", "on", "with", "by"}
    words = [w for w in m.group(1).split() if w.lower() not in stop]
    value = " ".join(words).strip(" ?.,!'-”")
    return value if len(value) >= 3 else ""


def _extract_search_term(q: str) -> str:
    """Pull a name-search term out of "find the school project" / "search for
    Anganwadi" — leading verbs and page/placeholder words stripped. Empty
    string when nothing specific remains ("open projects", "show the page")."""
    t = _NAV_VERB_RE.sub("", q.strip()).strip(" ?.,!")
    prev = None
    while prev != t:
        prev = t
        t = _STRIP_TOKENS_RE.sub("", t, count=1).strip(" ?.,!")
    return t if len(t) >= 4 else ""


def _extract_project_id(question: str) -> Optional[int]:
    """Conservative project-ID extraction. Only treats a number as a project
    reference when the phrasing anchors it: a project/proj/work prefix, an
    action verb (show/open/take me to…), a risk/status/details mention, a #
    prefix, or a standalone number message. Prevents false positives like the
    "2024" in \u201chow many projects completed in 2024?\u201d."""
    patterns = (
        r"#(\d{1,10})\b",
        r"\b(?:project|proj|work)\s*(?:no\.?|number|id)?\s*#?(\d{1,10})\b",
        r"\b(?:show|open|take\s+me\s+to|go\s*to|goto|jump\s+to|find|tell\s+me\s+about)\s+(?:of\s+|for\s+)?#?(\d{1,10})\b",
        r"\b(?:why\s+is|risk|status|details?)\s+(?:of\s+|for\s+)?#?(\d{1,10})\b",
        r"^\s*#?(\d{1,10})\s*\??$",
    )
    for pat in patterns:
        m = re.search(pat, question, re.I)
        if m:
            return int(m.group(1))
    return None  # "this project" context resolution handled by the caller


def _fmt_project_line(p, risk=None) -> str:
    line = f"• **#{p.id} {p.project_name}** — {p.state}"
    if p.constituency:
        line += f", {p.constituency}"
    line += f" · sanctioned {_fmt_money(p.sanctioned_amount)}"
    if p.completion_percentage is not None:
        line += f" · {p.completion_percentage:.0f}% complete"
    if risk is not None:
        line += f" · risk {risk.risk_score if risk else '—'} ({risk.risk_level if risk else 'n/a'})"
    return line


# ═══════════════════════════════════════════════════════════════════
# Data intents — each returns (answer, actions) or None. Every query is
# role-gated and LIMIT-ed.
# ═══════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════
# Navigation — "take me to X" / "open X". Targets are the app's REAL page
# keys; guards mirror the sidebar's own visibility rules so the assistant
# never offers a page the user's role cannot open.
# ═══════════════════════════════════════════════════════════════════

NAV_PAGES = [  # (pattern, page key, guard) — specific patterns first
    (r"ground\s+(truth\s+)?verif|verification\s+portal", "Ground Truth Verification", "verification:submit_or_guest"),
    (r"my\s+verifications?", "My Verifications", "verification:read_own"),
    (r"my\s+district", "My District", "inquiry:respond"),
    (r"evidence\s+queue|verification\s+queue", "Evidence Queue", "verification:review"),
    (r"\binquiries?\b", "Inquiries", "inquiry:review"),
    (r"saved\s+projects?", "Saved Projects", "analyst"),
    (r"my\s+investigations?", "My Investigations", "analyst"),
    (r"my\s+audit\s+cases?", "My Audit Cases", "analyst"),
    (r"data\s+management|csv\s+(upload|import)|import\s+(data|csv)\b", "Data Management", "users:manage"),
    (r"admin(istration)?\s*(page|panel|area)?|^\badmin\b$", "Administration", "users:manage"),
    (r"vendor\s+intelligence", "Vendor Intelligence", "public_or_analyst"),
    (r"vendor\s+network", "Vendor Network", "public_or_analyst"),
    (r"ai\s+audit(\s+(command\s+)?center)?|audit\s+(command\s+)?center", "AI Audit Center", "public_or_analyst"),
    (r"audit\s+priority", "Audit Priority", "public_or_analyst"),
    (r"risk\s+center", "Risk Center", None),
    (r"state\s+intelligence", "State Intelligence", None),
    (r"compare\s+(projects?|two\s+projects?)", "Compare Projects", None),
    (r"\breports?\b", "Reports", None),
    (r"\bleaderboard\b|\brankings?\b", "Leaderboard", None),
    (r"\bprojects?\b\s*(page|list|tab|explorer)?", "Projects", None),
    (r"\boverview\b|\bhome\b\s*(page|screen)?", "Overview", None),
    (r"\bsettings?\b", "Settings", None),
    (r"\bfaq\b", "FAQ", None),
]

_NAV_TRIGGER = re.compile(
    r"\btake\s+me\b|\bbring\s+me\b|\bgo\s*to\b|\bgoto\b|\bjump\s+to\b|\bnavigate\b|\bopen\b"
    r"|\bshow\b|\bdisplay\b|\bshow\s+me\b|\bswitch\s+to\b|\bhead\s+to\b|\bwhere\s+(is|are|can\s+i)"
    r"|\bcompare\b", re.I,
)


def _nav_guard_error(page: str, guard: Optional[str], user) -> Optional[str]:
    """None when the user may open the page, else an honest explanation."""
    caps = auth.user_capabilities(user) if user else set()
    if guard is None:
        return None
    if guard == "analyst":
        if user and auth.has_role_rank(user, "analyst"):
            return None
        return f"The **{page}** workspace is available to analyst-tier accounts (analysts, auditors and administrators)."
    if guard == "public_or_analyst":
        if user is None or auth.has_role_rank(user, "analyst"):
            return None
        return f"The **{page}** section is available to analysts, auditors and administrators."
    if guard == "verification:submit_or_guest":
        if user is None or "verification:submit" in caps:
            return None
        if "inquiry:respond" in caps:
            return ("Ground Verification submissions are made by citizens, field verifiers and auditors. "
                    "Your role's workspace is **My District**, where auditor inquiries await your official response.")
        return "Submitting ground evidence needs a citizen, field verifier, auditor or administrator account."
    if guard in caps:
        return None
    return (f"The **{page}** section needs the \u201c{guard.replace(':', ' ').strip()}\u201d permission, "
            "which your account doesn't hold.")


def _intent_navigate(q: str, ql: str, ctx_project_id: Optional[int], user, db: Session,
                     session_id: Optional[str] = None):
    """"Take me to X" / "open X" / "show X" — generates validated structured
    actions the client executes IMMEDIATELY (no button click). Navigation and
    filtering are read-only and reversible, so they ship auto=True. Also
    handles:
      • combined navigation+filtering ("open projects and show high risk from
        telangana") — page target + filter params in one action
      • context-additive follow-ups ("only the high risk ones" after "show
        Telangana projects") via the session's stored filter context
      • clear-filters and refresh-data utility intents
    Project-number navigation is handled by the project lookup intent."""
    # Pronoun navigation follow-up: "open it" / "take me to this project"
    # — but NOT when a target page/tab is named ("show this project in
    # Risk Center" must navigate to Risk Center, not re-open the drawer).
    if re.match(r"^(open|show|go\s*to|goto|take\s+me\s+to|jump\s+to)\s+(it|this\s+(project|one)?|that\s+(project|one)?)\b", ql) \
            and not re.search(r"\b(risk center|audit priority|state intelligence|compare|forensic|dna|timeline|mp intelligence|vendor)\b", ql):
        if ctx_project_id:
            p = db.query(models.Project).filter(models.Project.id == ctx_project_id).first()
            if p:
                return (f"Opening **#{p.id} {p.project_name}**.",
                        [ActionOut(label="Open Project Details", action="open_project", target=str(p.id), auto=True)],
                        p.id)
        return ("Which project? Give me the ID (e.g. \u201copen project 80649\u201d) or open one from the "
                "Projects page first.",
                [ActionOut(label="Browse projects", action="navigate", target="Projects")])

    # Forensic/DNA tab deep-links: "open the forensic view", "show the DNA".
    # With a project in context this opens the drawer directly on that tab.
    if re.search(r"\bforensic\w*\b|\bdna\b", ql) and ctx_project_id:
        tab = "forensics" if re.search(r"forensic", ql) else "dna"
        label = "AI Forensics tab" if tab == "forensics" else "DNA tab"
        view = "AI Forensics" if tab == "forensics" else "DNA"
        return (f"Opening the **{view}** view for #{ctx_project_id}.",
                [ActionOut(label=label, action="open_project", target=str(ctx_project_id),
                           params={"tab": tab}, auto=True)], ctx_project_id)

    # "Show all houses" / "both houses" — per-house counts + Projects open
    # WITHOUT a house filter (= All). House counts are two cheap aggregates.
    if re.search(r"\b(all|both)\s+houses?\b", ql):
        counts = dict(
            db.query(models.Project.house, func.count(models.Project.id))
            .filter(models.Project.house.isnot(None))
            .group_by(models.Project.house)
            .all()
        )
        ls, rs = int(counts.get("Lok Sabha", 0)), int(counts.get("Rajya Sabha", 0))
        return (f"**All houses** — Lok Sabha: {ls:,} projects · Rajya Sabha: {rs:,} projects"
                f" · unattributed: {max(0, int(db.query(func.count(models.Project.id)).scalar() or 0) - ls - rs):,}.",
                [ActionOut(label="Projects · All houses", action="navigate", target="Projects", auto=True)],
                None, {"page": "Projects", "filters": {}})

    # ── Utility intents: clear filters / refresh data ──
    if re.search(r"\b(clear|reset)\b.{0,25}\b(filters?|search(es)?|everything)\b|\bclear\s+all\b", ql):
        return ("Filters cleared.",
                [ActionOut(label="Clear all filters", action="clear_filters", target="", auto=True)])
    if re.search(r"\b(refresh|reload)\b.{0,20}\b(data|numbers|stats|statistics|dashboard|metrics)\b|\brefresh\b\s*$", ql):
        return ("Refreshing data…",
                [ActionOut(label="Refresh data", action="refresh_data", target="", auto=True)])

    # ── Bare status/FY filter asks ("show completed projects", "show 2025-26
    #    projects", "completed ones from 2025-26") — no page named, so
    #    Projects is the target. Stats questions ("how many…") are NOT
    #    navigation and stay with the data intents; risk screens likewise.
    status_only = _extract_status(ql)
    fy_only = _canonical_fy(ql, db)
    house_only = _extract_house(ql)
    # "Who recommended this project?" / "when was it recommended?" are context
    # questions containing a status word — never the Projects status filter.
    if re.search(r"\b(who|whom|whose|when|where|why)\b", ql):
        status_only = None
    if (status_only or fy_only) and re.search(r"\b(projects?|works?|ones?)\b", ql) \
            and not re.search(r"high[- ]risk|riskiest|anomal|unusual|suspicious", ql) \
            and not re.search(r"\bhow many\b|\bwhat (is|are) the\b|\bcount\b"
                              r"|what (percentage|percent|share)|\baverage\b|\bavg\b|\boverall\b"
                              r"|how much (has been )?(sanctioned|spent)", ql):
        params = {}
        if status_only:
            params["status"] = status_only
        if fy_only:
            params["fy"] = fy_only
        state_only = _canonical_state(ql, db)
        if state_only:
            params["state"] = state_only
        if house_only:
            params["house"] = house_only
        scope = " · ".join(params.values())
        return (f"Filtering Projects to {scope}…",
                [ActionOut(label=f"Projects · {scope}", action="navigate", target="Projects",
                           params=params, auto=True)],
                None, {"page": "Projects", "filters": params})

    if not _NAV_TRIGGER.search(ql):
        return None
    # "Support" is not a route — it opens this assistant. Explain that instead
    # of pretending a page exists.
    if re.search(r"\bsupport\b|\bcontact\s+(?:the\s+)?(?:team|us|support)\b", ql):
        return (assistant_nav.NON_ROUTE_DESTINATIONS["support"], [], None, None,
                {"intent": "help", "entity": {"type": "support"}})
    # Interrogative openers are questions, not navigation commands — except
    # "where is…?" which IS a navigation ask (already matched above).
    if re.match(r"^(how|what|which)\b", ql):
        return None
    # A numbered reference ("open project 80649") belongs to project lookup.
    if _extract_project_id(q) is not None:
        return None
    # Project-context questions ("where is this project?", "show me this
    # project's DNA", "who is the vendor?") belong to the context intent —
    # never bare page navigation, even when a page word appears.
    if (ctx_project_id or re.search(r"\b(this|that)\s+(project|one)\b|\bits\b", ql)) \
            and re.search(r"\b(who|whom|whose|when|why)\b"
                          r"|\bwhere\s+(is|are|did)\b"
                          r"|\b(dna|twins?|fingerprint|timeline|stress.?test)\b"
                          r"|\b(compare|peer|peers|similar|unusual)\b", ql) \
            and not re.match(r"^(open|go\s*to|goto|take\s+me|switch\s+to|head\s+to)\b", ql):
        return None
    # A real name-search term ("find the school project") belongs to the
    # lookup intent's name-search — only navigate when the user means a page.
    if re.match(r"^(find|search(\s+for)?|look\s+up)\b", ql) and _extract_search_term(q):
        return None

    # ── Context-additive filter follow-ups: "only the high risk ones",
    #    "now show completed", "and filter to 2025-26". The session remembers
    #    the last filtered page + its filters so the new constraint merges in.
    followup = bool(re.match(
        r"^(only|just|now\s+(only|show|filter)|and\s+(now\s+)?(show|filter|make\s+it)|filter\s+(to|by|for)|add\s+a?.{0,20}filter|make\s+it)\b",
        ql))
    status_f = _extract_status(ql)
    fy_f = _canonical_fy(ql, db)
    risk_f = bool(re.search(r"\b(high|medium|low)\s*(risk)?\b", ql)) and re.search(r"\brisk\b", ql)
    if followup and (status_f or fy_f or risk_f) and session_id:
        ctx = _session_get_ctx(session_id)
        if ctx.get("page") in ("Projects", "Risk Center", "Audit Priority"):
            page = ctx["page"]
            filters = dict(ctx.get("filters") or {})
            if risk_f:
                m = re.search(r"\b(high|medium|low)\b", ql)
                filters["risk_level"] = m.group(1).title()
            if status_f:
                filters["status"] = status_f
            if fy_f:
                filters["fy"] = fy_f
            scope = ", ".join(f"{v}" for v in filters.values()) or "the previous filters"
            label = f"{page} · {scope}"
            return (f"Filtering {page.lower()} to {scope}…",
                    [ActionOut(label=label, action="navigate", target=page, params=filters, auto=True)],
                    None, {"page": page, "filters": filters})

    # ── Combined navigation + filtering: extract everything the user asked
    #    for in one sentence. State names resolve canonically against the DB
    #    so the filter can never silently match nothing.
    state_f = _canonical_state(ql, db)
    house_f = _extract_house(ql)
    tier_f = _extract_tier(ql)
    status_f = _extract_status(ql)
    fy_f = _canonical_fy(ql, db)
    risk_f = re.search(r"\b(high|medium|low)\b", ql) and re.search(r"\brisk\b", ql)
    has_filters = any([state_f, fy_f, status_f, risk_f, tier_f, house_f])

    for pat, page, guard in NAV_PAGES:
        if re.search(pat, ql):
            # The generic Projects pattern must not hijack data-screen asks —
            # "show high-risk/delayed/mismatched projects" are answered by the
            # data intents with real numbers AND auto Risk Center actions.
            # (Specific page patterns — Vendor Intelligence, Risk Center… —
            # always win; only the bare "projects" match is skipped.)
            if page == "Projects" and pat.startswith(r"\bprojects?") \
                    and (risk_f or _DATA_SCREEN_RE.search(ql)):
                continue
            err = _nav_guard_error(page, guard, user)
            if err:
                return (err, [])
            params: Dict = {}
            if has_filters and page in ("Projects", "Risk Center", "Audit Priority", "State Intelligence"):
                if page == "Projects":
                    if state_f:
                        params["state"] = state_f
                    if house_f:
                        params["house"] = house_f
                    if status_f:
                        params["status"] = status_f
                    if fy_f:
                        params["fy"] = fy_f
                elif page == "Risk Center":
                    if risk_f:
                        m = re.search(r"\b(high|medium|low)\b", ql)
                        params["risk_level"] = m.group(1).title()
                    if state_f:
                        params["state"] = state_f
                elif page == "Audit Priority":
                    if tier_f:
                        params["tier"] = tier_f
                    if risk_f:
                        m = re.search(r"\b(high|medium|low)\b risk", ql)
                        params["risk_level"] = m.group(1).title() if m else None
                    params = {k: v for k, v in params.items() if v}
                elif page == "State Intelligence" and state_f:
                    params["state"] = state_f
            elif state_f and page in ("Projects", "Risk Center", "State Intelligence"):
                params = {"state": state_f}
            if page == "Vendor Intelligence":
                vendor_q = _extract_vendor_query(q)
                if vendor_q:
                    params["vendor"] = vendor_q
            blurb = PAGE_HELP.get(page, "")
            fl = "".join(f" · {v}" for v in params.values())
            text_out = f"**{page}**{fl} — opening it now."
            if params:
                text_out = f"**{page}**{fl} — opening it now with those filters."
            return text_out, [ActionOut(label=f"Open {page}", action="navigate", target=page,
                                        params=params, auto=True)], \
                None, ({"page": page, "filters": params, "state": state_f} if (params or state_f) else None)
    return None


def _linked_expenditure(db, project) -> float:
    """Authoritative per-project spend from the payment ledger
    (project_rec_info.linked_expenditure). The catalog column is a zeroed
    legacy stamp."""
    from models import ProjectRecInfo
    row = db.query(ProjectRecInfo).filter(ProjectRecInfo.project_id == project.id).first()
    return float(row.linked_expenditure or 0.0) if row is not None else 0.0


# ═══════════════════════════════════════════════════════════════════
# Project-context intelligence — answers WHY/HOW/WHERE/WHO/WHEN/WHAT
# questions about the project currently open in the details drawer,
# grounded in the same backend sources the UI uses.
# ═══════════════════════════════════════════════════════════════════

_PROJECT_BUNDLE_CACHE: "Dict[int, Tuple[float, Dict]]" = {}
_PROJECT_BUNDLE_TTL = 120.0
_PROJECT_BUNDLE_MAX = 200


def _project_bundle(db: Session, pid: int) -> Optional[Dict[str, Any]]:
    """One bounded resolver for everything the project-context intent needs:
    the project row, stored risk, rec/timeline facts, first/last ledger
    payment and this work's vendors. Cached 2 min per project (entries are
    a few small dicts) so rapid follow-ups never re-query."""
    now = time.time()
    hit = _PROJECT_BUNDLE_CACHE.get(pid)
    if hit and now - hit[0] < _PROJECT_BUNDLE_TTL:
        return hit[1]
    p = db.query(models.Project).filter(models.Project.id == pid).first()
    if not p:
        return None
    risk = db.query(models.RiskScore).filter(models.RiskScore.project_id == pid).first()
    rec_row = db.query(models.ProjectRecInfo).filter(models.ProjectRecInfo.project_id == pid).first()
    from database import normkey
    key_parts = [normkey(p.project_name), normkey(p.constituency), normkey(p.state)]
    vendor_rows: List = []
    first_pay = last_pay = None
    if rec_row is not None and rec_row.has_expenditure:
        # Ledger payments linked to this work key — bounded columns only.
        rows = db.execute(
            text("""
                SELECT expenditure_date AS d, expenditure_amount AS a, vendor AS v
                FROM expenditures
                WHERE normkey(work_description) = :desc
                  AND normkey(constituency) = :con
                  AND normkey(state) = :st
                ORDER BY expenditure_date ASC
            """),
            {"desc": key_parts[0], "con": key_parts[1], "st": key_parts[2]},
        ).fetchall()
        pays = [r for r in rows if r.d]
        if pays:
            first_pay, last_pay = pays[0], pays[-1]
            vend: Dict[str, Dict[str, Any]] = {}
            for r in rows:
                if r.v and str(r.v).strip():
                    e = vend.setdefault(str(r.v).strip(), {"vendor": str(r.v).strip(), "transactions": 0, "total_amount": 0.0})
                    e["transactions"] += 1
                    e["total_amount"] += float(r.a or 0)
            vendor_rows = sorted(vend.values(), key=lambda x: -x["total_amount"])[:8]
    bundle = {
        "p": p, "risk": risk, "rec": rec_row,
        "vendors": vendor_rows, "first_pay": first_pay, "last_pay": last_pay,
    }
    if len(_PROJECT_BUNDLE_CACHE) >= _PROJECT_BUNDLE_MAX:
        _PROJECT_BUNDLE_CACHE.clear()
    _PROJECT_BUNDLE_CACHE[pid] = (now, bundle)
    return bundle


def _reason_list(risk) -> List[str]:
    if not risk:
        return []
    if isinstance(risk.reasons, str):
        return [r.strip() for r in risk.reasons.split(",") if r.strip()]
    return list(risk.reasons or [])


def _fmt_date_human(iso: Optional[str]) -> str:
    if not iso:
        return ""
    try:
        from datetime import datetime as _dt
        d = _dt.strptime(str(iso)[:10], "%Y-%m-%d")
        return d.strftime("%d %b %Y").lstrip("0")
    except Exception:
        return str(iso)


def _peer_anchor_answer(db: Session, p, user) -> Tuple[str, List[ActionOut]]:
    """Compare-with-peers answer via the existing peer-benchmark engine."""
    try:
        import audit_intel as _ai
        bench = _ai.peer_benchmark(p, db, scope="state")
        metrics = {m.get("name"): m for m in bench.get("metrics", []) if isinstance(m, dict)}

        def _fmt_metric(m):
            if not m or m.get("project_value") is None or m.get("peer_median") is None:
                return None
            unit = m.get("unit") or ""
            pv, pm = m["project_value"], m["peer_median"]
            if unit == "percentage points":
                return f"{pv:.0f}% vs peer median {pm:.0f}%"
            if unit == "₹":
                return f"{_fmt_money(pv)} vs peer median {_fmt_money(pm)}"
            return f"{pv} vs peer median {pm}"

        parts = []
        for name in ("utilization_pct", "completion_pct", "risk_score"):
            s = _fmt_metric(metrics.get(name))
            if s:
                parts.append(f"• {s}")
        peer_count = bench.get("peer_count") or 0
        if not parts:
            return ("I couldn't compute a reliable peer comparison for this project — "
                    "not enough comparable projects share its state and category in the dataset."), []
        reliability = bench.get("reliability") or ""
        head = (f"Compared with **{peer_count:,} similar projects** in the same state and category "
                f"(statistical comparison, not a finding):\n" + "\n".join(parts))
        if reliability:
            head += f"\n\nComparison reliability: {reliability}."
        return head, [ActionOut(label="Open Compare Projects", action="navigate", target="Compare Projects")]
    except Exception:
        return ("Peer comparison isn't available for this project right now.", [])


def _intent_compare_metrics(q: str, ql: str, user, db: Session):
    """Numeric comparison of TWO explicit projects by ID — "which has higher
    expenditure between 12345 and 12350?". Factual numbers and differences
    only; no subjective ranking. Pure navigation to Compare Projects (no
    metric named) stays with the lookup intent."""
    if not re.search(r"higher|lower|more|less|difference|differ|which one|which has|whose|utilization", ql):
        return None
    nums = re.findall(r"\b\d{1,10}\b", q)
    if len(nums) < 2:
        return None
    a_id, b_id = int(nums[0]), int(nums[1])
    pa = db.query(models.Project).filter(models.Project.id == a_id).first()
    pb = db.query(models.Project).filter(models.Project.id == b_id).first()
    if not pa or not pb:
        return None  # not-found handling belongs to the lookup intent
    ra = db.query(models.ProjectRecInfo).filter(models.ProjectRecInfo.project_id == a_id).first()
    rb = db.query(models.ProjectRecInfo).filter(models.ProjectRecInfo.project_id == b_id).first()
    risks = {r.project_id: r for r in db.query(models.RiskScore).filter(models.RiskScore.project_id.in_((a_id, b_id))).all()}
    sa, sb = float(pa.sanctioned_amount or 0), float(pb.sanctioned_amount or 0)
    ea = float(ra.linked_expenditure or 0) if ra else 0.0
    eb = float(rb.linked_expenditure or 0) if rb else 0.0
    ua = (ea / sa * 100) if sa else 0.0
    ub = (eb / sb * 100) if sb else 0.0
    ca, cb = pa.completion_percentage, pb.completion_percentage

    def _line(p, e, u, c, r):
        bits = [f"sanctioned {_fmt_money(float(p.sanctioned_amount or 0))}",
                f"expenditure {_fmt_money(e)} ({u:.0f}% utilization)"]
        if c is not None:
            bits.append(f"completion {c:.0f}%")
        if r is not None:
            bits.append(f"risk {r.risk_score} ({r.risk_level})")
        return f"• **#{p.id} {p.project_name}** — " + ", ".join(bits)

    lines = [_line(pa, ea, ua, ca, risks.get(a_id)), _line(pb, eb, ub, cb, risks.get(b_id))]
    verdict = ""
    if re.search(r"expenditure|spent|spend\b|money", ql):
        if ea != eb:
            w = pa if ea > eb else pb
            verdict = f"\n\nHigher recorded expenditure: **#{w.id}** (by {_fmt_money(abs(ea - eb))})."
    elif re.search(r"completion|progress", ql) and ca is not None and cb is not None and ca != cb:
        w = pa if ca > cb else pb
        verdict = f"\n\nHigher physical completion: **#{w.id}** (by {abs(ca - cb):.0f} percentage points)."
    elif re.search(r"\brisk\b", ql) and a_id in risks and b_id in risks:
        ra_, rb_ = risks[a_id], risks[b_id]
        if ra_.risk_score != rb_.risk_score:
            w = pa if ra_.risk_score > rb_.risk_score else pb
            verdict = f"\n\nHigher risk score: **#{w.id}** (by {abs(ra_.risk_score - rb_.risk_score)} points)."
    return ("**Factual comparison** (recorded data):\n" + "\n".join(lines) + verdict
            + "\n\nSame authoritative sources as everywhere else — a data comparison, not a ranking of projects or people.",
            [ActionOut(label="Open Compare Projects", action="navigate", target="Compare Projects")])


def _intent_project_context(q: str, ql: str, ctx_project_id: Optional[int],
                            user, db: Session, session_id: Optional[str] = None):
    """Deep project-context Q&A for the project open in the details drawer
    (or referenced by ID/pronoun). Grounded ONLY in real backend data via
    _project_bundle + the existing timeline/peer-benchmark engines."""
    # Entry: explicit ID already extracted, project-context page tag, or a
    # pronoun referring to the discussed/selected project.
    pronoun = bool(re.search(r"\b(its?|this\s+(project|one)|that\s+(project|one)|the\s+project)\b", ql))
    pid = _extract_project_id(q) or (ctx_project_id if (ctx_project_id or pronoun) else None)
    if not pid:
        return None
    # Two project IDs ("compare project 12 with 34") belong to the lookup
    # intent's comparator; pure navigation ("open project 80649", "show
    # 80649") belongs to lookup as well. Bare numbers that are NOT an ID
    # pair ("why is the audit priority 2/100?") must NOT trigger the bail.
    if len(re.findall(r"\b\d{1,10}\b", q)) >= 2 and re.search(r"\b(compare|comparison|vs\.?|versus|between)\b", ql):
        return None
    if re.match(r"^(open|show|go\s*to|goto|take\s+me\s+to|jump\s+to|display|find|search)\b", ql) \
            and not re.search(r"why|how|what|when|who|where|which|explain|risk|factor|timeline|dna|twins|compare|"
                              r"expenditure|spent|sanctioned|completion|progress|status|vendor|\bmp\b|recommended|"
                              r"audited|missing|unusual|similar|location", ql):
        return None

    bundle = _project_bundle(db, pid)
    if bundle is None:
        return (f"I couldn't find a project with ID {pid} in the current dataset.", [], None)
    # Portfolio-scoped aggregate questions ("how many projects have zero
    # expenditure?") belong to the analytics intent even when a project is
    # open — the open project answers *this* project, not the dataset.
    if _PORTFOLIO_SCOPED_RE.search(ql):
        return None
    p, risk, rec = bundle["p"], bundle["risk"], bundle["rec"]
    reasons = _reason_list(risk)
    exp = float((rec.linked_expenditure or 0.0)) if rec is not None else 0.0
    sanc = float(p.sanctioned_amount or 0.0)
    comp = p.completion_percentage

    # ── intent discrimination ─────────────────────────────────────────
    # Bare follow-ups ("why?" / "what caused it?" / "explain") inherit the
    # previous subject — with a project in context that means its risk
    # explanation.
    bare_why = bool(re.match(r"^why\b[\s?!.]*$|^what caused (it|this|that)[\s?!.]*$|^explain[\s?!.]*$|^reasons?\??[\s!N]*$", ql))
    asks_why_risk = bare_why or bool(re.search(r"\bwhy\b.{0,40}\b(risk|flag|warning|anomal)|why (is|was).{0,30}(risky|high risk|flagged|delayed)|what caused.{0,20}risk|what.s wrong", ql))
    asks_why_expenditure = bool(re.search(r"why.{0,40}(expenditure|spend|spent|cost).{0,25}(high|low|so|this|zero|\b0\b|nothing)|why is (the |expenditure )?(expenditure|spending) zero", ql))
    asks_why_completion = bool(re.search(r"why.{0,40}(completion|progress).{0,25}(low|only|high|so|this|stuck|\d{1,3}\s*%|percent)|why is (the )?(progress|completion)\s*\d", ql))
    asks_why_delay = bool(re.search(r"\bwhy\b.{0,30}\bdelay|why is it (late|stalled|slow)", ql))
    asks_compare = bool(re.search(r"\b(compare|comparison|versus|vs\.?|similar|peers?|unusual\w*|normal|typical)\b", ql))
    asks_dna = bool(re.search(r"\b(dna|twins?|fingerprint|stress.?test)\b", ql))
    asks_timeline = bool(re.search(r"\btimeline\b|what happened first|\bhistory\b.{0,20}project|how long.{0,20}(active|running|going)|when.{0,20}(first|start|begin).{0,20}(expenditure|payment|record)", ql))
    asks_missing = bool(re.search(
        r"what (information|data|evidence|fields?)\s+(is|are)?\s*missing|what.s\s+missing|what is missing|missing (evidence|information|fields)"
        r"|why is (this |the )?(information|date|field|start date)[^.]{0,30}missing|what would i need to verify"
        r"|why.{0,20}start date.{0,20}(missing|unavailable)", ql))
    asks_priority = bool(re.search(r"audit priority|priority (score|tier|rank)|\bp[1-4]\b|why.{0,30}priority", ql))
    asks_auditor = bool(re.search(
        r"why.{0,30}(audit|audited)|what.{0,20}(auditor|should).{0,20}check|what.{0,20}evidence.{0,30}(audit|priority)"
        r"|what.{0,10}(is|are)\s*(the\s+)?(biggest\s+)?(data.quality|discrepanc)|evidence.{0,15}(is\s+)?(available|exists)"
        r"|data.?quality|biggest.{0,15}(discrepanc|issue)|discrepanc"
        r"|what should (be|i) verif|verif\w*.{0,25}\bground\b|on the ground|field verif",
        ql)) or asks_missing or asks_priority
    asks_location = bool(re.search(r"\bwhere\b|which (state|district|constituency)|location", ql))
    asks_who = bool(re.search(r"\bwho\b|who recommended|which mp|\bmp\b|recommending|vendor", ql))
    asks_house = bool(re.search(r"\bhouse\b|lok sabha|rajya sabha", ql))
    asks_fy = bool(re.search(r"\bfy\b|financial year|which (fiscal )?year", ql))
    asks_when = bool(re.search(r"\bwhen\b", ql)) or asks_fy
    asks_how_calc = bool(re.search(r"how (was|is).{0,30}(score|calculated|computed)|how.{0,10}risk.{0,20}(calculated|computed)", ql))
    asks_factors = bool(re.search(r"risk factors?|which factors|what factors|contributed most", ql))
    asks_ml_vs_rule = bool(re.search(r"\bml\b|machine learning|rule.based|isolation forest|anomaly model", ql))
    asks_money = bool(re.search(r"how much|\bamount\b|remaining|spent|expenditure|sanctioned|utilization|overspend|ratio", ql))
    asks_zero_spend = bool(re.search(r"why.{0,40}(expenditure|spend|spent|cost).{0,25}(zero|\b0\b|nothing|nil)|why is (the |expenditure )?(expenditure|spending) zero|why.{0,15}(no|zero) (expenditure|spending|payments)", ql))
    asks_completion = bool(re.search(r"completion|progress|how far (along|has it come)|\bcomplet(ed|ion)\b|has (it|the project) started|is it (done|finished)", ql))
    # "Give me a complete summary" is an OVERVIEW ask — must never be caught
    # by the completion/progress keywords ("complete").
    asks_summary = bool(re.search(r"give me (an overview|a summary|a complete summary|a full summary|a complete overview|a full overview)\b"
                                  r"|complete (summary|overview)\b|full (summary|overview)\b|summarize (this|that) (project|work)", ql))
    if asks_summary:
        asks_completion = False
    asks_status = bool(re.search(r"status|what is this project|tell me about this project", ql))

    # "Explain its risk score" / "what are its risk factors" with the project
    # open: methodology + THIS project's actual triggers and ML signal.
    if (asks_factors or re.search(r"explain.{0,20}(risk|score)|what caused.{0,20}score|what is (its |the |this project.s )?risk score", ql)) \
            and not asks_compare:
        base = _intent_risk_methodology("how is risk calculated", user, pid, db)
        body = base[0] if base else ""
        trig = ""
        if reasons:
            trig = "\n\n**Triggered for this project (#" + str(pid) + "):**\n" + "\n".join(f"• {r}" for r in reasons[:6])
        if risk is not None and getattr(risk, "ml_anomaly", False):
            trig += f"\n• ML anomaly flag: {'yes (+25)' if getattr(risk, 'ml_anomaly', False) else 'no'}"
        return (body + trig) if body else ("I don't have the risk methodology available right now.", [], pid)[0], \
            [ActionOut(label="Open Risk Center", action="navigate", target="Risk Center")], pid

    # HOW-much family (quantitative) — before generic WHY handlers, but
    # never ahead of an explicit WHEN question ("when did expenditure first
    # get recorded?" wants dates, not the financial card).
    if asks_money and not asks_when and not (asks_why_risk or asks_why_expenditure or asks_why_completion):
        util = (exp / sanc * 100) if sanc else 0
        lines = [f"**Financials for #{p.id} {p.project_name}**",
                 f"• Sanctioned: {_fmt_money(sanc)}",
                 f"• Recorded expenditure: {_fmt_money(exp)} ({util:.0f}% utilization)"]
        if sanc and exp <= sanc:
            lines.append(f"• Remaining: {_fmt_money(sanc - exp)}")
        elif sanc and exp > sanc:
            lines.append(f"• Recorded spend exceeds the sanctioned amount by {_fmt_money(exp - sanc)} — a cost-overrun indicator.")
        if comp is not None:
            lines.append(f"• Physical completion: {comp:.0f}% — compare with the {util:.0f}% utilization when judging consistency.")
        if rec is not None and rec.has_expenditure and bundle["first_pay"] is not None:
            lines.append(f"• First recorded payment: {_fmt_date_human(bundle['first_pay'].d)}")
        lines.append("")
        lines.append("Expenditure is the linked payment-ledger total for this work; amounts the ledger doesn't contain are not estimated.")
        return "\n".join(lines), [], pid

    if asks_completion and not (asks_why_risk or asks_why_completion):
        cdisp = f"{comp:.0f}%" if comp is not None else "not recorded"
        extra = ""
        if comp is not None and comp >= 90 and (p.status or "").lower() != "completed":
            extra = " Status hasn't been updated to Completed — a status/progress inconsistency worth reviewing."
        return (f"**#{p.id} {p.project_name}** records **{cdisp} physical completion** "
                f"(status: {p.status or '—'}).{extra}"), [], pid

    # WHY family — grounded explanations from recorded indicators.
    # Zero/missing expenditure "why" first: the generic WHY branch would
    # answer with the risk-flag gate, which is the wrong subject.
    if asks_zero_spend or re.search(r"(progress|completion).{0,40}despite.{0,25}(zero|no|\b0\b)|why.{0,30}(progress|completion).{0,30}(zero|no)\s+(expenditure|spend|payments)", ql):
        util_z = (exp / sanc * 100) if sanc else 0
        parts = [f"**Expenditure for #{p.id} {p.project_name}**",
                 f"• Recorded expenditure: {_fmt_money(exp)}" + (f" ({util_z:.0f}% of the sanctioned {_fmt_money(sanc)})" if sanc else "")]
        if (comp or 0) > 0 and exp <= 0:
            parts.append(f"• Physical progress is recorded as {comp:.0f}% while no expenditure is recorded — "
                         "a money/progress inconsistency worth reviewing.")
        # Distinguish verified ₹0 / linked / shared-work ambiguity / no records.
        if exp <= 0 and rec is not None and getattr(rec, "has_expenditure", False):
            _wk_rows = int(getattr(rec, "work_key_rows", 1) or 1)
            _wk_exp = float(getattr(rec, "work_key_expenditure", 0.0) or 0.0)
            if _wk_rows > 1 and _wk_exp > 0:
                parts.append(f"• Payments totalling {_fmt_money(_wk_exp)} are recorded for this work's "
                             f"description, but {_wk_rows} catalog rows share that description, so the "
                             "amount cannot be attributed to this specific project — treat its "
                             "individual spend as unknown.")
        elif exp <= 0:
            parts.append("• No ledger payment could be verifiably linked to this work "
                         "(normalized work key + MP + implementing-district authority), "
                         "so the spend is currently *unknown — not confirmed zero*.")
        parts.append("")
        parts.append("The dataset records the amounts but contains no recorded reason for the missing payments — "
                     "the cause is not known from this data and should be verified against "
                     "fund-release/payment records rather than assumed.")
        return "\n".join(parts), [], pid

    if asks_why_risk or asks_why_expenditure or asks_why_completion or asks_why_delay:
        if not reasons and not (risk and getattr(risk, "ml_anomaly", False)):
            return (f"No risk indicators are recorded for #{p.id} — its risk score is "
                    f"{risk.risk_score if risk else 0} ({risk.risk_level if risk else 'None'}). "
                    "It hasn't been flagged by the rule engine or the ML model.", [], pid)
        parts = []
        if reasons:
            parts.append("**Why it's flagged** (recorded indicators):\n" + "\n".join(f"• {r}" for r in reasons[:6]))
        if risk and getattr(risk, "ml_anomaly", False):
            parts.append("• The ML model also flags it as a statistical outlier vs. similar projects.")
        if sanc and exp > sanc:
            parts.append(f"• Recorded spend ({_fmt_money(exp)}) exceeds the sanctioned {_fmt_money(sanc)}.")
        if comp is not None and comp < 50 and exp > 0:
            parts.append(f"• {_fmt_money(exp)} is recorded against only {comp:.0f}% physical completion.")
        parts.append("\nThese are review indicators derived from recorded data — not proof of wrongdoing.")
        return "\n".join(parts), [
            ActionOut(label="Open Risk Center", action="navigate", target="Risk Center"),
            ActionOut(label="View Audit Details", action="navigate", target="AI Audit Center"),
        ], pid

    if asks_compare:
        return _peer_anchor_answer(db, p, user) + (pid,)

    if asks_dna:
        # Reuse the forensic DNA/twins/stress-test data — no second calculation.
        try:
            from forensic_api import project_dna as _dna_fn, stress_test as _stress_fn
            from fastapi import Query as _Q
            dna_payload = _dna_fn(pid, db=db)
            dims = (dna_payload.get("dna") or {}).get("dimensions") or []
            twins = dna_payload.get("twins") or []
            lines = [f"**Project DNA — #{p.id} {p.project_name}**"]
            for d in dims[:8]:
                if isinstance(d, dict) and d.get("label"):
                    lines.append(f"• {d['label']}: {d.get('display', d.get('value'))}")
            if twins:
                lines.append("")
                lines.append("**Closest analytical peers (Project Twins):**")
                for t in twins[:4]:
                    if isinstance(t, dict):
                        lines.append(f"• #{t.get('project_id')} {t.get('project_name', '')} — {t.get('similarity_percent', t.get('similarity', ''))}% similar")
            lines.append("")
            lines.append("DNA shows only attributes present in the dataset — missing dimensions are omitted, never estimated.")
            return "\n".join(lines), [], pid
        except Exception:
            return ("I couldn't compute the Project DNA for this project right now.", [], pid)

    if asks_timeline:
        try:
            from timeline import get_project_timeline
            tl = get_project_timeline(pid)
            events = (tl or {}).get("events") or []
            if not events:
                return ("No timeline events are recorded for this project in the source datasets "
                        "(recommendation, expenditure and completion records) — nothing is estimated.", [], pid)
            lines = [f"**Timeline — #{p.id} {p.project_name}**"]
            for e in events[:8]:
                d = _fmt_date_human(e.get("date")) if e.get("date") else "date not recorded"
                amt = f" · {_fmt_money(e['amount'])}" if e.get("amount") else ""
                lines.append(f"• {d} — {e.get('title', e.get('event_type', ''))}{amt}")
            delay = (tl or {}).get("delay_intelligence") or {}
            if isinstance(delay, dict) and delay.get("summary"):
                lines.append("")
                lines.append(str(delay["summary"]))
            lines.append("")
            lines.append("Timeline uses only recorded lifecycle events; missing stages are absent from the source data.")
            return "\n".join(lines), [], pid
        except Exception:
            return ("The timeline for this project couldn't be built right now.", [], pid)

    if asks_auditor:
        try:
            import audit_intel as _ai
            stale = _ai  # noqa: F841
            from main import _check_stale_progress
            linked = exp
            stale_res = _check_stale_progress(p, linked_expenditure=linked)
            checklist = _ai.build_checklist(p, reasons, stale_res["flag"], linked_expenditure=linked)
            actions_rec = _ai.recommended_actions(checklist)
            gap = _ai.evidence_gap_summary(p)

            # "What is missing?" — actual missing evidence fields only.
            if asks_missing:
                labels = (gap or {}).get("missing_labels") or []
                if not labels:
                    return (f"All evidence fields checked for #{p.id} are present in the current dataset — "
                            f"nothing is flagged as missing from the project record.", [], pid)
                lines = [f"**Missing evidence for #{p.id} {p.project_name}** ({len(labels)} item(s) not available):\n"]
                lines += [f"• {lbl}" for lbl in labels[:8]]
                lines.append("")
                lines.append("The dataset doesn't record WHY these are missing — no reason is invented. "
                             "They should be verified against the source registers.")
                return "\n".join(lines), [], pid

            # "Why is the audit priority P2/2?" — explain the EXISTING composite
            # priority breakdown (risk component + exposure + mismatch + gap,
            # floored by risk level); no second calculation.
            if asks_priority:
                pr = _ai.priority_breakdown(p, float(risk.risk_score if risk else 0),
                                            risk_level=(risk.risk_level if risk else None),
                                            linked_expenditure=exp)
                comp = pr.get("components") or {}
                lines = [f"**Audit priority for #{p.id} {p.project_name}**",
                         f"• Composite priority score: {pr.get('audit_priority_score')} → tier **{pr.get('tier')} "
                         f"({pr.get('tier_label')})**"]
                for lbl, key in (("Risk component", "risk"), ("Financial exposure", "financial_exposure"),
                                 ("Financial–physical mismatch", "financial_physical_mismatch"),
                                 ("Evidence/data gap", "evidence_gap")):
                    if key in comp:
                        lines.append(f"• {lbl}: {comp[key]}")
                for r_ in (pr.get("reasons") or [])[:4]:
                    lines.append(f"• {r_}")
                lines.append("")
                lines.append("Risk score ≠ audit priority: the priority tier combines the risk score with "
                             "financial exposure, the utilisation-vs-progress mismatch and the evidence gap, "
                             "with a floor from the risk level (High ⇒ at least P2).")
                return "\n".join(lines), [
                    ActionOut(label="Open Audit Priority", action="navigate", target="Audit Priority"),
                ], pid

            lines = [f"**Why # {p.id} deserves audit attention**"]
            lines.append(f"• Risk {risk.risk_score if risk else 0}/100 ({risk.risk_level if risk else 'None'})" +
                         (f" — {', '.join(reasons[:3])}" if reasons else ""))
            if actions_rec:
                lines.append("")
                lines.append("**What an auditor should check first:**")
                lines += [f"• {a}" for a in actions_rec[:4]]
            missing = (gap or {}).get("missing") or []
            if missing:
                lines.append("")
                lines.append("**Recorded data gaps:** " + ", ".join(str(m) for m in missing[:5]))
            lines.append("")
            lines.append("Flags are review priorities derived from recorded data — not proof of wrongdoing.")
            return "\n".join(lines), [
                ActionOut(label="Open Audit Priority", action="navigate", target="Audit Priority"),
                ActionOut(label="Open AI Audit Center", action="navigate", target="AI Audit Center"),
            ], pid
        except Exception:
            return ("I couldn't assemble the audit view for this project right now.", [], pid)

    if asks_location:
        loc = p.state or ""
        if p.constituency:
            loc += f", {p.constituency}"
        elif p.district:
            loc += f", {p.district}"
        if not loc:
            return "That field isn't recorded for this project in the current dataset.", [], pid
        vendor_note = ""
        if bundle["vendors"] and asks_who:
            vendor_note = f"\nPayment vendors on this work: {', '.join(v['vendor'] for v in bundle['vendors'][:3])}."
        return (f"**#{p.id} {p.project_name}** is recorded in **{loc}**.{vendor_note}", [], pid)

    if asks_who:
        mp = (rec.recommended_by if rec is not None else None) or p.mp_name if hasattr(p, "mp_name") else (rec.recommended_by if rec is not None else None)
        lines = []
        if mp:
            lines.append(f"• Recommended by: **{mp}**")
        else:
            lines.append("• Recommending MP: not recorded in the current dataset.")
        if bundle["vendors"]:
            vs = ", ".join(f"**{v['vendor']}** ({v['transactions']} payments, {_fmt_money(v['total_amount'])})" for v in bundle["vendors"][:3])
            lines.append(f"• Payment vendors on this work: {vs}")
        else:
            lines.append("• Vendors: no vendor records match this work in the expenditure ledger.")
        lines.append("")
        lines.append("Only entities recorded in the source data are named; nothing is inferred.")
        return f"**Who's involved — #{p.id} {p.project_name}**\n" + "\n".join(lines), [], pid

    if asks_when or asks_how_calc or asks_factors or asks_ml_vs_rule or asks_house:
        if asks_house and not (asks_when or asks_who):
            # House comes from the recommended-works record for this work key —
            # looked up with the same conservative normalized linkage.
            from database import normkey as _nk
            house = None
            try:
                house = db.execute(
                    text("SELECT house FROM recommended_works WHERE normkey(work_description) = :d "
                         "AND normkey(constituency) = :c AND normkey(state) = :s AND house IS NOT NULL LIMIT 1"),
                    {"d": _nk(p.project_name or ""), "c": _nk(p.constituency or ""), "s": _nk(p.state or "")},
                ).scalar()
            except Exception:
                house = None
            if house:
                return (f"**#{p.id} {p.project_name}** was recommended in the **{house}**.", [], pid)
            return ("The recommending house isn't recorded for this project in the current dataset.", [], pid)
        if asks_how_calc or asks_factors or asks_ml_vs_rule:
            base = _intent_risk_methodology("how is risk calculated", user, pid, db)
            if base:
                return base[0], base[1], pid
        # WHEN — rec facts + first/last ledger payment, from project_rec_info.
        lines = [f"**Dates for #{p.id} {p.project_name}**"]
        if p.fy:
            lines.append(f"• Financial year: {p.fy}")
        if rec is not None and rec.recommendation_date:
            lines.append(f"• Recommended: {_fmt_date_human(rec.recommendation_date)}")
            if rec.recommended_by:
                lines.append(f"• Recommended by: {rec.recommended_by}")
        else:
            lines.append("• Recommendation date unavailable in the source dataset.")
        if rec is not None and rec.start_status == "valid" and rec.approx_start_date:
            lines.append(f"• Approx. project start: {_fmt_date_human(rec.approx_start_date)} (earliest recorded expenditure)")
        elif rec is not None and rec.has_expenditure:
            lines.append("• Start date unavailable — recorded expenditure predates the recommendation date.")
        else:
            lines.append("• Project not yet started / no expenditure recorded.")
        if bundle["first_pay"] is not None:
            lines.append(f"• First recorded payment: {_fmt_date_human(bundle['first_pay'].d)}")
        if bundle["last_pay"] is not None:
            lines.append(f"• Most recent recorded payment: {_fmt_date_human(bundle['last_pay'].d)}")
        return "\n".join(lines), [], pid

    # "What is this project?" / "tell me about this project" — render the
    # full profile card directly; the profile intent's regex doesn't catch
    # every overview phrasing.
    if asks_summary or re.search(r"what is (this|that) (project|work)|tell me about (this|that)\b|what can you tell me about (this|that)"
                 r"|give me (an overview|a summary) of (this|that)|summarize (this|that) project|project (overview|summary)\b", ql):
        text, actions = _project_profile_answer(p, risk, exp)
        return text, actions, pid

    if asks_status:
        return None  # let the existing profile intent render the full card

    return None


def _project_profile_answer(p, risk, linked_expenditure: float = None) -> Tuple[str, List[ActionOut]]:
    """Full project profile composed ONLY from the real record + risk row.

    Expenditure shown is the authoritative linked payment-ledger total
    (project_rec_info); the catalog column is a zeroed legacy stamp."""
    reasons = [r.strip() for r in (risk.reasons or "").split(",") if r.strip()] if risk else []
    exp = float(linked_expenditure or 0.0)
    util = (exp / p.sanctioned_amount * 100) if p.sanctioned_amount else 0
    lines = [
        "**PROJECT**",
        f"Project ID: #{p.id}",
        f"Name: {p.project_name}",
        f"Location: {p.state}" + (f", {p.constituency}" if p.constituency else ""),
        f"Sanctioned: {_fmt_money(p.sanctioned_amount)} · Expenditure: {_fmt_money(exp)} ({util:.0f}% utilization)",
        f"Physical completion: {p.completion_percentage if p.completion_percentage is not None else '—'}% · Status: {p.status or '—'}",
        "",
        f"**Risk Score:** {risk.risk_score if risk else 'not scored'} ({risk.risk_level if risk else 'n/a'})",
    ]
    if reasons:
        lines.append("")
        lines.append("**KEY FINDINGS**")
        lines += [f"• {r}" for r in reasons[:6]]
    if risk and getattr(risk, "ml_anomaly", False):
        lines.append("• Flagged as a statistical anomaly by the ML model vs. similar projects")
    lines.append("")
    lines.append("**RECOMMENDED NEXT STEP**")
    lines.append("• Open the project's AI Forensics tab for the full evidence picture" if reasons
                 else "• No risk indicators recorded — routine monitoring is enough for now")
    actions = [ActionOut(label="Open Project Details", action="open_project", target=str(p.id))]
    if reasons:
        actions.append(ActionOut(label="View Audit Details", action="navigate", target="AI Audit Center"))
    return "\n".join(lines), actions


def _intent_project_lookup(
    q: str, ql: str, ctx_project_id: Optional[int], user, db: Session
):
    """Every question anchored to a specific project ID — lookup, navigation
    (\u201copen project 80649\u201d, \u201cshow 80649\u201d), and detail asks — plus pronoun
    follow-ups (\u201cwhy is it risky?\u201d) resolved against the selected-project
    context. Returns None when no project reference is present."""
    # Any explicit project-anchored number must be answered (found OR
    # not-found), never silently dropped to the fallback. Status-anchored
    # asks ("show completed projects") also enter for state/status filtering.
    pid = _extract_project_id(q)
    if pid is None and not re.match(r"^(open|show|go\s*to|goto|take\s+me\s+to|jump\s+to|display|find|search(\s+for)?)\b", ql) \
            and not (ctx_project_id and re.search(r"\b(its?|this\s+project|this\s+one|that\s+project|that\s+one)\b", ql)):
        return None
    # ── Compare two projects by ID: "compare project 12 with project 34",
    #    "12 vs 34" → open Compare Projects preloaded with both projects.
    if re.search(r"\bcompar|\bversus\b|\bvs\.?\b", ql) and len(re.findall(r"\b\d{1,10}\b", q)) >= 2:
        nums = re.findall(r"\b\d{1,10}\b", q)
        a_id, b_id = int(nums[0]), int(nums[1])
        pa = db.query(models.Project).filter(models.Project.id == a_id).first()
        pb = db.query(models.Project).filter(models.Project.id == b_id).first()
        if pa and pb:
            return (
                f"Opening **Compare Projects** with **#{pa.id} {pa.project_name}** and "
                f"**#{pb.id} {pb.project_name}**.",
                [ActionOut(label="Open Compare Projects", action="navigate", target="Compare Projects",
                           params={"a": str(a_id), "b": str(b_id)}, auto=True)],
            )
        missing = [n for n, p in ((a_id, pa), (b_id, pb)) if p is None]
        return (f"I couldn't find project{'s' if len(missing) > 1 else ''} "
                f"{', '.join('#' + m for m in missing)} in the current dataset.", [])

    nav_ish = bool(re.match(r"^(open|show|go\s*to|goto|take\s+me\s+to|jump\s+to|display|find)\b", ql))

    # Pronoun/possessive follow-up with a selected project: "why is it risky?",
    # "tell me about this project", "what is its status?"
    if pid is None and ctx_project_id is not None:
        pronoun = re.search(r"\b(its?|this\s+project|this\s+one|that\s+project|that\s+one)\b", ql)
        asks = re.search(r"open|show|risk|anomal|about|tell|status|explain|check|verif", ql)
        if pronoun and asks and not re.search(r"page|section|website|site|workflow", ql):
            pid = ctx_project_id

    if pid is None:
        # ── Name search: no numeric ID but a search term exists ("find the
        #    school project"). One match opens the project directly; several
        #    → concise disambiguation, never a random pick. State mentions
        #    become a filter rather than part of the search term.
        if not (ctx_project_id and re.search(r"\b(its?|this\s+project|this\s+one|that\s+project|that\s+one)\b", ql)) \
                and not _DATA_SCREEN_RE.search(ql):
            state_f = _canonical_state(ql, db)
            house_f = _extract_house(q)
            term = _extract_search_term(q)
            if house_f and term:
                # Strip the house mention from the search term —
                # "lok sabha school projects" searches for "school".
                term = re.sub(r"\b(lok|rajya)\s+sabha\b", "", term, flags=re.I).strip(" ,.-")
                if len(term) < 4:
                    term = ""
            if state_f and term:
                # Strip the resolved state (and its preposition) from the
                # search term — "projects in Telangana" has no name component.
                term = re.sub(re.escape(state_f), "", term, flags=re.I)
                term = re.sub(r"\b(in|from|at|across)\s*$", "", term, flags=re.I).strip(" ,.-")
                if len(term) < 4:
                    term = ""
            nav_filters = {}
            if state_f:
                nav_filters["state"] = state_f
            if house_f:
                nav_filters["house"] = house_f
            if (state_f or house_f) and not term:
                # Pure filter request — "find projects in Telangana",
                # "show Lok Sabha projects", "show Lok Sabha projects in Telangana".
                scope = " ".join(x for x in ([house_f, f"in {state_f}"] if state_f else [house_f]) if x)
                return (f"Showing {scope} projects.".replace("  ", " "),
                        [ActionOut(label=f"Projects · {scope}", action="navigate", target="Projects",
                                   params=nav_filters, auto=True)],
                        None, {"page": "Projects", "filters": nav_filters})
            if term:
                rows_q = (
                    db.query(models.Project)
                    .filter(models.Project.project_name.ilike(f"%{term}%"))
                )
                if house_f:
                    # House named → restrict matches to that house's rows;
                    # unattributed rows are excluded rather than guessed.
                    rows_q = rows_q.filter(models.Project.house == house_f)
                rows = rows_q.order_by(models.Project.id).limit(6).all()
                if len(rows) == 1:
                    p = rows[0]
                    risk = db.query(models.RiskScore).filter(models.RiskScore.project_id == p.id).first()
                    loc = p.state + (f", {p.constituency}" if p.constituency else "")
                    line = (f"**Found 1 project for \u201c{term}\u201d** — opening it.\n"
                            f"**#{p.id} {p.project_name}**\n{loc} · sanctioned {_fmt_money(p.sanctioned_amount)}")
                    if risk:
                        line += f" · risk {risk.risk_score} ({risk.risk_level})"
                    return line, [ActionOut(label="Open Project Details", action="open_project",
                                            target=str(p.id), auto=True)], p.id
                if len(rows) > 1:
                    total = db.query(func.count(models.Project.id)).filter(
                        models.Project.project_name.ilike(f"%{term}%")).scalar() or len(rows)
                    lines = "\n".join(
                        f"• **#{p.id} {p.project_name}** — {p.state}"
                        + (f", {p.constituency}" if p.constituency else "")
                        + f" · {_fmt_money(p.sanctioned_amount)}"
                        for p in rows
                    )
                    return (f"I found {total:,} matching projects for \u201c{term}\u201d — which one do you mean? "
                            f"Top matches:\n{lines}\n\nGive me the **project ID** (e.g. \u201copen project {rows[0].id}\u201d) "
                            "and I'll open it.", [])
                # No match — honest miss; Projects opens pre-filtered so the
                # user can refine rather than restart.
                return (f"I couldn't find any project named \u201c{term}\u201d in the current dataset. "
                        "Opening Projects with that search applied so you can refine it.",
                        [ActionOut(label=f"Search Projects for \u201c{term}\u201d", action="navigate",
                                   target="Projects", params={"keyword": term}, auto=True)])
        return None

    p = db.query(models.Project).filter(models.Project.id == pid).first()
    if not p:
        return (f"❓ I couldn't find a project with ID {pid}. Please check the Project ID and try "
                "again — IDs are the numeric identifiers shown as #12345 on project cards.", [])
    risk = db.query(models.RiskScore).filter(models.RiskScore.project_id == pid).first()

    # Pure navigation ask → compact found-card + AUTO-OPEN action. The
    # client opens Project Details immediately; the chip remains as an
    # affordance. Detail questions get the full profile instead.
    if nav_ish and not re.search(r"why|risk|anomal|status|about|tell|explain|sanction|verif|detail", ql):
        loc = p.state + (f", {p.constituency}" if p.constituency else "")
        line = f"**Opening project #{p.id}** — {p.project_name}\n{loc} · sanctioned {_fmt_money(p.sanctioned_amount)}"
        if p.completion_percentage is not None:
            line += f" · {p.completion_percentage:.0f}% complete"
        if risk:
            line += f" · risk {risk.risk_score} ({risk.risk_level})"
        return line, [ActionOut(label="Open Project Details", action="open_project", target=str(p.id), auto=True)], p.id

    answer, actions = _project_profile_answer(p, risk, linked_expenditure=_linked_expenditure(db, p))
    return answer, actions, p.id


def _intent_risk_methodology(ql: str, user, ctx_project_id: Optional[int], db: Session):
    """Explains the ACTUAL scoring rules from ml/predictor.py — real point
    weights and thresholds, no invented percentages."""
    if not re.search(
        r"how\s+(is|does).{0,30}risk.{0,20}(calculat|comput|determin|work|scored|built|made)"
        r"|how.{0,8}risk.{0,8}score.{0,15}(calculat|work)"
        r"|what\s+factors?.{0,25}(affect|contribute|go\s+into|drive|make).{0,15}risk"
        r"|risk\s+(score|index|methodology).{0,25}(calculat|comput|work|factors|methodology)"
        r"|what\s+makes?.{0,12}(a\s+)?(project|something).{0,10}(suspicious|risky)",
        ql,
    ):
        return None
    answer = (
        "The 0–100 **risk score** combines fixed rule checks on each project's own recorded data "
        "with an ML anomaly signal. Points add up when a check triggers:\n\n"
        "**Rule checks**\n"
        "• Expenditure exceeds the sanctioned amount — **+40**\n"
        "• 80%+ of sanctioned funds spent while physical completion is below 50% — **+35**\n"
        "• Any expenditure recorded with 0% physical completion — **+30**\n"
        "• Marked Completed while physical progress is below 90% — **+25**\n"
        "• Sanctioned ≥ ₹10 lakh with completion below 25% — **+20**\n"
        "• Completion reported ≥ 80% but zero expenditure recorded — **+20**\n"
        "• Sanctioned ≥ ₹5 lakh with under 10% utilization and completion below 30% — **+15**\n\n"
        "**ML anomaly** — flagged by an Isolation Forest model vs. similar projects — **+25**\n\n"
        "The total is capped at 100. **Levels:** High ≥ 60 · Medium ≥ 30 · Low above 0 · None = 0.\n\n"
        "A high score is a review-priority signal — **not** proof of wrongdoing."
    )
    if ctx_project_id:
        risk = db.query(models.RiskScore).filter(models.RiskScore.project_id == ctx_project_id).first()
        if risk:
            answer += f"\n\nThis project (#{ctx_project_id}) currently scores **{risk.risk_score} ({risk.risk_level})**."
    return answer, [
        ActionOut(label="Open Risk Center", action="navigate", target="Risk Center"),
        ActionOut(label="Audit Priority", action="navigate", target="Audit Priority"),
    ]


def _intent_vendor(q: str, ql: str, user, db: Session):
    """Vendor analytics — specific-vendor lookup and concentration ranking.
    Analyst-tier data (guests get the public demo, lateral roles don't)."""
    if not re.search(r"\bvendors?\b", ql):
        return None
    allowed = user is None or auth.has_role_rank(user, "analyst")

    # ── concentration / top vendors (pure SQL aggregate, tiny result) ──
    if re.search(r"concentration|which\s+vendors?.{0,30}(most|top|highest|largest|many)|top\s+vendors?"
                 r"|vendor\s+(spend|spending|totals?)", ql):
        if not allowed:
            return ("Vendor analytics are available to analysts, auditors and administrators. "
                    "I can still help with MPLADS basics, project lookups and Ground Verification.", [])
        rows = db.execute(text("""
            SELECT trim(vendor) AS name, COUNT(*) AS tx,
                   COALESCE(SUM(expenditure_amount), 0) AS total,
                   COUNT(DISTINCT work_description) AS works
            FROM expenditures
            WHERE vendor IS NOT NULL AND trim(vendor) <> ''
            GROUP BY normkey(vendor)
            ORDER BY works DESC, total DESC
            LIMIT 8
        """)).fetchall()
        if not rows:
            return "I don't have vendor expenditure data to answer that.", []
        lines = "\n".join(
            f"• **{r.name}** — {r.works:,} distinct works · {r.tx:,} transactions · {_fmt_money(r.total)}"
            for r in rows
        )
        return (f"**Vendors serving the most distinct works** (potential concentration — "
                f"investigative leads, not proof):\n{lines}", [
                    ActionOut(label="Open Vendor Network", action="navigate", target="Vendor Network"),
                    ActionOut(label="Open Vendor Intelligence", action="navigate", target="Vendor Intelligence"),
                ])

    # ── specific vendor lookup ──
    m = re.search(r"vendor[s]?\s+(?:named\s+|called\s+)?([A-Za-z][\w&\.\'\- ]{1,60}?)(?:\s+in\s+|\s+from\s+|\?|$)", q or "", re.I)
    if not m:
        return None
    candidate = m.group(1).strip(" ?.,!")
    stop = {"in", "from", "at", "for", "and", "the", "a", "an", "of", "on", "with", "by"}
    words = [w for w in candidate.split() if w.lower() not in stop]
    candidate = " ".join(words).strip()
    if len(candidate) < 3:
        return ("Which vendor? Give me the vendor's name (e.g. \u201ctell me about vendor DARSH BUILDCON\u201d).", [])
    if not allowed:
        return (f"Vendor profiles like \u201c{candidate}\u201d are available to analysts, auditors and "
                "administrators. I can still help with MPLADS basics, project lookups and Ground Verification.", [])
    row = db.execute(text("""
        SELECT trim(vendor) AS name, COUNT(*) AS tx,
               COALESCE(SUM(expenditure_amount), 0) AS total,
               COUNT(DISTINCT work_description) AS works
        FROM expenditures
        WHERE normkey(vendor) = normkey(:name)
        GROUP BY trim(vendor)
    """), {"name": candidate}).first()
    alternates = []
    if row is None:
        like = f"%{candidate}%"
        rows = db.execute(text("""
            SELECT trim(vendor) AS name, COUNT(*) AS tx,
                   COALESCE(SUM(expenditure_amount), 0) AS total,
                   COUNT(DISTINCT work_description) AS works
            FROM expenditures
            WHERE vendor LIKE :like
            GROUP BY normkey(vendor)
            ORDER BY total DESC
            LIMIT 5
        """), {"like": like}).fetchall()
        if not rows:
            return (f"I couldn't find a vendor named \u201c{candidate}\u201d in the expenditure records.", [])
        row, alternates = rows[0], [r.name for r in rows[1:]]
    states = [r[0] for r in db.execute(text("""
        SELECT DISTINCT trim(state) FROM expenditures
        WHERE normkey(vendor) = normkey(:name) AND state IS NOT NULL AND trim(state) <> ''
        LIMIT 5
    """), {"name": row.name}).fetchall()]
    lines = [
        f"**VENDOR — {row.name}**",
        f"• Total paid: {_fmt_money(row.total)} across {row.tx:,} transactions",
        f"• Distinct works (expenditure items): {row.works:,}",
    ]
    if states:
        lines.append(f"• States: {', '.join(states)}")
    if alternates:
        lines.append(f"• Similar names also found: {', '.join(alternates[:3])}")
    lines.append("")
    lines.append("Vendor signals are investigative leads — they don't prove wrongdoing.")
    return "\n".join(lines), [
        ActionOut(label="Open Vendor Intelligence", action="navigate", target="Vendor Intelligence"),
        ActionOut(label="Open Vendor Network", action="navigate", target="Vendor Network"),
    ]


def _intent_analytics(q: str, ql: str, user, db: Session):
    """Portfolio analytics Q&A — counts, money, utilization, completion,
    zero-expenditure/overspend/progress-but-no-spend cohorts, and state
    performance. Every figure is one bounded SQL aggregate over the same
    authoritative sources as the dashboard (metrics.py); nothing is loaded
    into Python lists. Analyst-tier data: guests get the concept + chips."""
    if not re.search(
        r"how many|what (percentage|percent|share)|\baverage\b|\bavg\b|overall utilization"
        r"|spent overall|sanctioned overall|overall sanctioned|overall completion|zero expenditure|expenditure above sanctioned"
        r"|above sanctioned|overspend|over.?spend|progress but no|expenditure but zero"
        r"|how.{0,20}performing|how much has been spent|how much has been sanctioned|total.{0,12}(spent|expenditure|sanctioned)"
        r"|zero progress|no progress|0% progress"
        r"|which states?.{0,30}(highest|most|top).{0,30}(expenditure|spent|utilization)"
        r"|states?.{0,30}utilization|compare.{0,40}(state|and)|\bvs\.?\b|\bversus\b",
        ql,
    ):
        return None
    # Data lists/screens belong to the dedicated intents — this answers NUMBERS.
    if _DATA_SCREEN_RE.search(ql) and not re.search(r"how many|what (percentage|percent)", ql):
        return None

    def _guest():
        return (
            "Portfolio analytics are available to analysts, auditors and administrators. "
            "As a guest I can still help with MPLADS basics, project lookups and Ground "
            "Verification.",
            [ActionOut(label="Browse projects", action="navigate", target="Projects")],
        )

    allowed = user is None or auth.has_role_rank(user, "analyst")
    import metrics as _metrics

    # ── cohort counters (zero expenditure / overspend / progress-no-spend / spend-no-progress) ──
    if re.search(r"zero expenditure|no expenditure|expenditure above sanctioned|above sanctioned|overspend|over.?spend|progress but no|expenditure but zero|without expenditure|zero progress|no progress|0% progress", ql):
        if not allowed:
            return _guest()
        # Aggregate in SQL. The old loop pulled every catalogue row into
        # Python just to count four cohorts; this keeps the request bounded to
        # one grouped scan and one row returned.
        linked = func.coalesce(models.ProjectRecInfo.linked_expenditure, 0.0)
        sanction = func.coalesce(models.Project.sanctioned_amount, 0.0)
        completion = func.coalesce(models.Project.completion_percentage, 0.0)
        cohort_q = db.query(
            func.count(models.Project.id),
            func.sum(case((linked <= 0, 1), else_=0)),
            func.sum(case(((sanction > 0) & (linked > sanction), 1), else_=0)),
            func.sum(case(((linked <= 0) & (completion > 0), 1), else_=0)),
            func.sum(case(((completion <= 0) & (linked > 0), 1), else_=0)),
        ).outerjoin(models.ProjectRecInfo, models.ProjectRecInfo.project_id == models.Project.id)
        house_scope = _extract_house(ql)
        if house_scope:
            cohort_q = cohort_q.filter(models.Project.house == house_scope)
        total_n, zero_n, over_n, prog_no_spend, spend_no_prog = cohort_q.one()
        total_n = int(total_n or 0)
        zero_n = int(zero_n or 0)
        over_n = int(over_n or 0)
        prog_no_spend = int(prog_no_spend or 0)
        spend_no_prog = int(spend_no_prog or 0)
        scope_lbl = f" — {house_scope}" if house_scope else ""
        parts = []
        if re.search(r"zero expenditure|no expenditure|without expenditure", ql):
            parts.append(f"• **{zero_n:,} projects** have no expenditure recorded in the payment ledger")
        if re.search(r"expenditure above sanctioned|above sanctioned|overspend|over.?spend", ql):
            parts.append(f"• **{over_n:,} projects** have expenditure above their sanctioned amount (cost-overrun indicator, review-worthy — not a finding of wrongdoing)")
        if re.search(r"progress but no|without expenditure", ql):
            parts.append(f"• **{prog_no_spend:,} projects** report physical progress but have no recorded expenditure — may be a ledger gap or genuine work without recorded payments; flag for review, don't assume either way")
        if re.search(r"expenditure but zero|zero progress|no progress|0% progress", ql):
            parts.append(f"• **{spend_no_prog:,} projects** have recorded expenditure at 0% physical completion")
        if not parts:
            return None
        if re.search(r"what (percentage|percent|share)", ql) and len(parts) == 1:
            base = total_n or 1
            return (parts[0].replace(":", f" — {float(parts[0].split('**')[1].replace(',', '')) / base * 100:.1f}%" + ":", 1) + f"\n(Out of {total_n:,} projects{scope_lbl}.)", [])
        head = f"**Portfolio cohort counts{scope_lbl}** (from the payment ledger and project catalog):"
        return (head + "\n" + "\n".join(parts) + f"\n\nOut of {total_n:,} projects{scope_lbl}.", [
            ActionOut(label="Open Risk Center", action="navigate", target="Risk Center"),
            ActionOut(label="Open State Intelligence", action="navigate", target="State Intelligence"),
        ])

    # ── overall utilization / total spend / average completion ──
    if re.search(r"overall utilization|utilization.{0,15}(portfolio|overall|total)|spent overall|sanctioned overall|overall sanctioned|how much has been spent|how much has been sanctioned|total.{0,12}(spent|expenditure|sanctioned)|overall completion|average completion|\bavg\b.{0,12}completion", ql):
        if not allowed:
            return _guest()
        house = _extract_house(ql)
        ov = _metrics.portfolio_overview(db, house=house)
        lines = [
            f"• Recorded expenditure (payment ledger): {_fmt_money(ov['total_expenditure'])}",
            f"• Sanctioned (project catalog): {_fmt_money(ov['total_sanctioned_amount'])}",
            f"• Utilization: {ov['utilization_percentage']:.1f}%",
            f"• Completed works (completions ledger): {ov['completed_projects']:,}",
            f"• Average catalog completion: {ov['average_completion_percentage']:.1f}%",
        ]
        scope = f" — {house}" if house else ""
        return (f"**Portfolio money & progress{scope}** (same authoritative aggregates the dashboard uses):\n" + "\n".join(lines), [
            ActionOut(label="Open Overview", action="navigate", target="Overview"),
            ActionOut(label="Open State Intelligence", action="navigate", target="State Intelligence"),
        ])

    # ── what percentage of projects are completed ──
    if re.search(r"what (percentage|percent|share).{0,30}(completed|complete)|how many.{0,30}(completed|complete)", ql) \
            and not re.search(r"high.risk|anomal", ql):
        if not allowed:
            return _guest()
        house = _extract_house(ql)
        led_completed = _metrics.ledger_totals(db, house=house)["completed_works"]
        catalog_completed = db.query(models.Project).filter(func.lower(models.Project.status) == "completed")
        total_q = db.query(func.count(models.Project.id))
        if house:
            catalog_completed = catalog_completed.filter(models.Project.house == house)
            total_q = total_q.filter(models.Project.house == house)
        catalog_completed = catalog_completed.count()
        total = total_q.scalar() or 0
        pct = (catalog_completed / total * 100) if total else 0.0
        scope = f" — {house}" if house else ""
        return (f"**Completion{scope}**\n"
                f"• Catalog status = Completed: {catalog_completed:,} of {total:,} projects ({pct:.1f}%)\n"
                f"• Completions ledger: {led_completed:,} works\n"
                f"\nBoth sources are shown because they measure different things — the catalog status is the "
                "project record; the completions ledger counts finished works with a recorded completion.", [])

    # ── two-state comparison: "Compare Telangana and Karnataka" ──
    if re.search(r"\bcompare\b|\bvs\.?\b|\bversus\b", ql):
        rows_states = [s for (s,) in db.query(models.Project.state).distinct().all() if s]
        low_states = {s.lower(): s for s in rows_states}
        # Match canonical state names directly ("compare telangana and
        # karnataka" is one lowercase span, so span-scans can't split it).
        hits = [(ql.find(w), name) for w, name in low_states.items() if re.search(r"\b" + re.escape(w) + r"\b", ql)]
        mentioned: List[str] = [name for _, name in sorted(hits)]
        if len(mentioned) >= 2:
            if not allowed:
                return _guest()
            a, b = mentioned[0], mentioned[1]
            sa, sb = _metrics.portfolio_stats(db, state=a), _metrics.portfolio_stats(db, state=b)
            ua = (float(sa["expenditure"]) / float(sa["sanctioned"]) * 100) if float(sa["sanctioned"]) else 0.0
            ub = (float(sb["expenditure"]) / float(sb["sanctioned"]) * 100) if float(sb["sanctioned"]) else 0.0
            return (f"**{a} vs {b}** (recorded data, same aggregation rules for both):\n\n"
                    f"| | {a} | {b} |\n|---|---|---|\n"
                    f"| Projects | {sa['total_projects']:,} | {sb['total_projects']:,} |\n"
                    f"| Sanctioned | {_fmt_money(sa['sanctioned'])} | {_fmt_money(sb['sanctioned'])} |\n"
                    f"| Expenditure (ledger) | {_fmt_money(sa['expenditure'])} | {_fmt_money(sb['expenditure'])} |\n"
                    f"| Utilization | {ua:.1f}% | {ub:.1f}% |\n"
                    f"| Completed works (ledger) | {sa['completed_works']:,} | {sb['completed_works']:,} |\n"
                    f"| Avg catalog completion | {sa['avg_completion']:.1f}% | {sb['avg_completion']:.1f}% |\n\n"
                    "Factual comparison of recorded data — not a ranking of states or authorities.", [
                        ActionOut(label=f"State Intelligence · {a}", action="navigate", target="State Intelligence",
                                  params={"state": a}, auto=True),
                        ActionOut(label=f"State Intelligence · {b}", action="navigate", target="State Intelligence",
                                  params={"state": b}),
                    ])
        if len(mentioned) == 1:
            state = mentioned[0]
        else:
            return None
    else:
        state = _canonical_state(ql, db)
        if not state:
            # Direct state mention without preposition or trailing noun —
            # "How is Telangana performing?", "Telangana completion rate".
            low_states = {s.strip().lower(): s.strip() for (s,) in db.query(models.Project.state).distinct().all() if s}
            for word in re.findall(r"[a-z][a-z &']{3,40}", ql):
                w = word.strip(" ,.?!&'")
                if w in low_states:
                    state = low_states[w]
                    break

    # ── state performance / state utilization ranking ──
    if re.search(r"how.{0,25}performing|utilization.{0,20}(state|states)|states?.{0,25}(highest|most|best|top).{0,20}(utilization|expenditure|spent)"
                 r"|(expenditure|spent).{0,20}by state|tell me about.{0,25}state|projects? in\b|works? in\b", ql) or (state and re.search(r"how many|how much", ql)):
        if not allowed:
            return _guest()
        if state:
            st = _metrics.portfolio_stats(db, state=state)
            sanc, expd = float(st["sanctioned"]), float(st["expenditure"])
            util = (expd / sanc * 100) if sanc else 0.0
            return (f"**{state}**\n"
                    f"• Projects: {st['total_projects']:,}\n"
                    f"• Sanctioned: {_fmt_money(sanc)}\n"
                    f"• Recorded expenditure: {_fmt_money(expd)} ({util:.1f}% utilization)\n"
                    f"• Completed works (ledger): {st['completed_works']:,}\n"
                    f"• Average catalog completion: {st['avg_completion']:.1f}%", [
                        ActionOut(label=f"State Intelligence · {state}", action="navigate",
                                  target="State Intelligence", params={"state": state}, auto=True),
                    ])
        # Cross-state ranking — expenditure from the ledger's own state
        # column, sanctioned from the catalog. Metric follows the question:
        # expenditure ranking when the user asks for spend, utilization otherwise.
        by_exp = bool(re.search(r"expenditure|spent", ql) and not re.search(r"utili", ql))
        led_by_state = _metrics.state_ledger_map(db)
        sanc_by_state = dict(
            db.query(models.Project.state, func.coalesce(func.sum(models.Project.sanctioned_amount), 0.0))
            .filter(models.Project.state.isnot(None))
            .group_by(models.Project.state)
            .all()
        )
        ranked = []
        for sname, led in led_by_state.items():
            sv = float(sanc_by_state.get(sname) or 0.0)
            e = float(led["expenditure"])
            if sv > 0:
                ranked.append((sname, (e / sv * 100), e, sv))
            elif by_exp and e > 0:
                ranked.append((sname, 0.0, e, 0.0))
        ranked.sort(key=(lambda t: -t[2]) if by_exp else (lambda t: -t[1]))
        if not ranked:
            return "I don't have enough data to rank states by that metric.", []
        top = ranked[:8]
        if by_exp:
            lines = "\n".join(f"• {s}: {_fmt_money(e)}" for s, _u, e, _sv in top)
            return (f"**States ranked by recorded expenditure** (payment ledger):\n{lines}", [
                ActionOut(label="Open State Intelligence", action="navigate", target="State Intelligence"),
            ])
        lines = "\n".join(
            f"• {s}: {u:.1f}% ({_fmt_money(e)} of {_fmt_money(sv)})" for s, u, e, sv in top
        )
        return (f"**States ranked by utilization** (ledger expenditure ÷ catalog sanctioned):\n{lines}", [
            ActionOut(label="Open State Intelligence", action="navigate", target="State Intelligence"),
        ])

    return None


def _intent_portfolio_stats(q: str, user, db: Session):
    ql = q.lower()
    if not re.search(r"how many|total.{0,12}projects|portfolio.{0,12}(size|overview|stats)|project count", ql):
        return None
    state = _extract_state(q)
    house = _extract_house(q)
    base = db.query(models.Project)
    if state:
        # match against the real state list (case-insensitive contains both ways)
        rows = db.query(models.Project.state).distinct().all()
        match = next((s for s in (r[0] for r in rows) if s and state.lower() in s.lower()), None)
        if not match:
            return ("I don't have enough data to answer that — I couldn't find a state "
                    f"named \u201c{state}\u201d in the dataset. Try the exact state name.", [])
        base = base.filter(models.Project.state == match)
        label = match
    else:
        label = "the whole portfolio"
    if house:
        # House named → count and sum only that house's catalog rows.
        base = base.filter(models.Project.house == house)
        label = f"{label} · {house}"
    # Authoritative aggregates (metrics_svc): catalog sanctioned/counts +
    # ledger expenditure/completions. For a state scope the ledgers are
    # filtered by their own state column (labels match the catalog 1:1).
    import metrics as _metrics
    if state:
        led = _metrics.ledger_totals_scoped(db, state=match, house=house)
    elif house:
        led = _metrics.ledger_totals(db, house=house)
    else:
        led = _metrics.ledger_totals(db)
    n, sanc = base.with_entities(
        func.count(models.Project.id),
        func.coalesce(func.sum(models.Project.sanctioned_amount), 0),
    ).one()
    n, sanc = int(n or 0), float(sanc or 0)
    exp = float(led["total_expenditure"])
    completed = int(led["completed_works"])
    if n == 0:
        return f"No projects found for {label}.", []
    answer = (
        f"**{label}**: {n:,} projects\n"
        f"• Sanctioned: {_fmt_money(sanc)}\n"
        f"• Recorded expenditure (payment ledger): {_fmt_money(exp)} ({(exp/sanc*100) if sanc else 0:.1f}% of sanctioned)\n"
        f"• Completed works (completions ledger): {completed:,}"
    )
    actions = [ActionOut(label="Browse projects", action="navigate", target="Projects")]
    if not state:
        actions.append(ActionOut(label="State Intelligence", action="navigate", target="State Intelligence"))
    return answer, actions


def case_completed():
    from sqlalchemy import case
    return case((models.Project.status.ilike("%completed%"), 1), else_=0)


def _intent_state_top(ql: str, user, db: Session):
    if not re.search(r"which states?|states? (with|have|has)|top states|most projects", ql):
        return None
    # Expenditure/utilization rankings belong to the analytics intent —
    # this handler only answers PROJECT-COUNT rankings.
    if re.search(r"expenditure|spent|utili[sz]ation|high.risk|risk", ql):
        return None
    rows = (
        db.query(models.Project.state, func.count(models.Project.id).label("n"))
        .filter(models.Project.state.isnot(None))
        .group_by(models.Project.state)
        .order_by(func.count(models.Project.id).desc())
        .limit(8)
        .all()
    )
    if not rows:
        return "I don't have enough data to answer that.", []
    lines = "\n".join(f"• {s}: {n:,} projects" for s, n in rows)
    return f"**Top states by project count:**\n{lines}", [
        ActionOut(label="Open State Intelligence", action="navigate", target="State Intelligence")
    ]


def _intent_high_risk(ql: str, user, db: Session, session_id: Optional[str] = None):
    # An explicit project id is an investigation, not a portfolio list.
    if re.search(r"\bproject\s*#?\s*\d{1,10}\b|#\d{1,10}", ql):
        return None
    # Concept question ("what does high risk mean?") → knowledge, not data.
    # Only treat as a data request when the user wants actual projects/lists.
    # "Does high risk mean fraud?" is likewise a language question — the
    # careful no-fraud answer, never a project list.
    if re.search(r"what (does|do|is|are).{0,16}(high|risk).{0,8}(risk )?mean|risk level.{0,10}mean", ql):
        return None
    if re.search(r"\bfraud\b|\bwrongdoing\b|\bcorruption\b|\bprove\b|mean that", ql) and re.search(r"high[- ]?risk|score", ql) \
            and not re.search(r"how many|show|list|which", ql):
        return None
    if not re.search(r"high[- ]risk|highest.?risk|risky projects|riskiest", ql):
        return None
    # Risk rankings are internal analysis — citizens/guests get the concept,
    # not the list.
    if user is None or not auth.has_role_rank(user, "analyst"):
        return (
            "Risk scores are internal review indicators used by analysts and auditors. "
            "A high score flags a project for closer review — it is not proof of "
            "wrongdoing. If you're a citizen, you can browse public project information "
            "on the Projects page or strengthen the record through Ground Verification.",
            [ActionOut(label="Open Ground Verification", action="navigate", target="Ground Truth Verification")],
        )
    # State ranking: "which states have the most high-risk projects?" —
    # a factual SQL group-by, neutral language, no allegations.
    if re.search(r"which states?.{0,40}high.risk|states?.{0,20}(most|highest).{0,20}high.risk|high.risk.{0,25}by state", ql) \
            and not re.search(r"\bin\s+", ql):
        rows = (
            db.query(models.Project.state, func.count(models.RiskScore.id).label("n"))
            .join(models.RiskScore, models.RiskScore.project_id == models.Project.id)
            .filter(models.RiskScore.risk_level.ilike("high"))
            .filter(models.Project.state.isnot(None))
            .group_by(models.Project.state)
            .order_by(func.count(models.RiskScore.id).desc())
            .limit(8)
            .all()
        )
        if not rows:
            return "No high-risk projects are currently flagged in the risk data.", []
        total = sum(int(r.n) for r in rows)
        lines = "\n".join(f"• {r[0] or 'Unknown'}: {int(r.n):,}" for r in rows)
        return (f"**States with the most high-risk flags** (top {len(rows)}, {total:,} total flagged):\n{lines}\n\n"
                "High risk is a review priority — not a finding of wrongdoing.", [
                    ActionOut(label="Open Risk Center", action="navigate", target="Risk Center"),
                    ActionOut(label="Open State Intelligence", action="navigate", target="State Intelligence"),
                ], None, None, {
                    "intent": "risk_explanation",
                    "table": {
                        "columns": ["State", "High-risk projects"],
                        "rows": [[r[0] or "Unknown", f"{int(r.n):,}"] for r in rows],
                        "caption": "High-risk flags by state",
                    },
                    "limitations": ["Flags reflect stored risk scores, not new scoring at question time."],
                })
    state = _canonical_state(ql, db)
    house = _extract_house(ql)
    q = (
        db.query(models.Project, models.RiskScore)
        .join(models.RiskScore, models.RiskScore.project_id == models.Project.id)
        .filter(models.RiskScore.risk_level.ilike("high"))
    )
    if state:
        q = q.filter(models.Project.state == state)
    if house:
        q = q.filter(models.Project.house == house)
    rows = q.order_by(models.RiskScore.risk_score.desc()).limit(6).all()
    if not rows:
        scope_parts = [f" in {state.title()}" if state else "", f" ({house})" if house else ""]
        scope = "".join(scope_parts)
        # Even with no flagged rows, hand the user the filtered Risk Center /
        # Audit Priority views — the assistant acts, it doesn't dead-end.
        rc_params = {"risk_level": "High"}
        if state:
            rc_params["state"] = state
        if house:
            rc_params["house"] = house
        return (f"No high-risk projects are currently flagged in the risk data{scope}. "
                "Opening the Risk Center with the High-risk filter so you can double-check.",
                [ActionOut(label="Risk Center · High risk" + (f" · {state}" if state else ""),
                           action="navigate", target="Risk Center", params=rc_params, auto=True),
                 ActionOut(label="Audit Priority", action="navigate", target="Audit Priority")],
                None, {"page": "Risk Center", "filters": rc_params}, {
                    "intent": "risk_explanation",
                    "result_ids": [],
                    "limitations": ["No stored high-risk flag matched the requested scope."],
                })
    lines = "\n".join(_fmt_project_line(p, r) for p, r in rows)
    if session_id:
        _session_set_results(session_id, [p.id for p, _r in rows])
    # ONE join for the count query — joining once per filter produced
    # "ambiguous column name: projects.state" under SQLite whenever a state
    # AND house were both named ("high-risk Lok Sabha projects in Telangana").
    count_q = (
        db.query(func.count(models.RiskScore.id))
        .join(models.Project, models.Project.id == models.RiskScore.project_id)
        .filter(models.RiskScore.risk_level.ilike("high"))
    )
    if state:
        count_q = count_q.filter(models.Project.state == state)
    if house:
        count_q = count_q.filter(models.Project.house == house)
    count = count_q.scalar() or 0
    scope = ((f" in {state.title()}" if state else "") + (f" ({house})" if house else ""))
    answer = (
        f"{count:,} projects{scope} are flagged **high risk**. Top 6 by score:\n{lines}\n\n"
        "A high score is a review priority, not proof of wrongdoing."
    )
    # Auto-execute: Risk Center opens with the High-risk filter applied —
    # read-only, reversible, exactly what "show me high-risk projects" asks for.
    rc_params = {"risk_level": "High"}
    if state:
        rc_params["state"] = state
    if house:
        rc_params["house"] = house
    table_rows = [[
        f"#{p.id}", (p.project_name or "")[:52], p.state or "—",
        _fmt_money(p.sanctioned_amount),
        f"{float(p.completion_percentage):.0f}%" if p.completion_percentage is not None else "—",
        f"{r.risk_score} ({r.risk_level})",
    ] for p, r in rows]
    return answer, [
        ActionOut(label="Risk Center · High risk" + (f" · {state}" if state else ""),
                  action="navigate", target="Risk Center", params=rc_params, auto=True),
        ActionOut(label="Audit Priority", action="navigate", target="Audit Priority"),
    ], None, {"page": "Risk Center", "filters": rc_params}, {
        "intent": "risk_explanation",
        "entity": {"type": "risk", "risk_level": "High"},
        "table": {
            "columns": ["Project", "Name", "State", "Sanctioned", "Completion", "Risk"],
            "rows": table_rows,
            "caption": f"Top high-risk projects{scope}",
        },
        "result_ids": [p.id for p, _r in rows],
        "limitations": [
            "A high score is a review priority, not proof of wrongdoing.",
            "The list shows the highest-scoring records, not every flagged project.",
        ],
    }


def _intent_project_detail(q: str, ql: str, ctx_project_id: Optional[int], user, db: Session):
    """Context/attribute questions about a project ("why is this risky?",
    "what is the sanctioned amount?" with a project open). ID-anchored asks
    are handled by _intent_project_lookup; this catches the residue. Returns
    (answer, actions, discussed_project_id) — a 3-tuple."""
    # Attribute questions only resolve against a *selected* project context
    # ("what is the sanctioned amount?" while a project is open) — without a
    # selected project there is nothing to answer about.
    # Pure definition asks ("what does sanctioned amount mean?") are glossary,
    # never the open-a-project prompt. Bare "what is (the) <term>?" with no
    # project open is likewise a definition, not a missing-project prompt.
    if re.search(r"\b(mean|means|meant)\b|definition\s+of\b", ql):
        return None
    # Portfolio-scoped asks ("which states have the highest expenditure?")
    # belong to the analytics intents — never the open-a-project prompt.
    if _PORTFOLIO_SCOPED_RE.search(ql):
        return None
    if not ctx_project_id and re.search(
        r"^\s*what\s+(is|are|does)\s+(the\s+|a\s+|an\s+)?"
        r"(physical\s+)?(completion|utilization|expenditure|sanctioned|sanction\b|recommendation|implementing agency|anomaly)",
        ql,
    ):
        return None
    asks_attribute = bool(re.search(
        r"sanctioned amount|expenditure|current status|what.*(status|completion)|"
        r"main anomalies|what should (be|we|i) verif|explain this project|about this project",
        ql,
    ))
    if asks_attribute and not ctx_project_id:
        return (
            "Open a project first (click any row on the Projects page), then ask me about "
            "its sanctioned amount, expenditure, status or anomalies — I'll use that "
            "project's actual record.",
            [ActionOut(label="Browse projects", action="navigate", target="Projects")],
            None,
        )
    if not re.search(
        r"why is (this )?(project|it)|risk(y)?\s*(of|for)?\s*project|explain.{0,20}risk|about project|"
        r"tell me about project|details (of|for) project|what.{0,10}anomal|"
        r"sanctioned amount|expenditure|current status|what should (be|we|i) verif|explain this project|about this project",
        ql,
    ):
        return None
    pid = _extract_project_id(q) or ctx_project_id
    if not pid:
        return ("Which project? Give me the numeric ID (e.g. \u201cwhy is project 80649 risky?\u201d) "
                "or open the project first and ask again.", [], None)
    p = db.query(models.Project).filter(models.Project.id == pid).first()
    if not p:
        return (f"❓ I couldn't find a project with ID {pid}. Please check the Project ID and try "
                "again.", [], None)
    risk = db.query(models.RiskScore).filter(models.RiskScore.project_id == pid).first()
    answer, actions = _project_profile_answer(p, risk, linked_expenditure=_linked_expenditure(db, p))
    return answer, actions, pid


def _intent_investigate_next(ql: str, user, db: Session):
    """Auditor/admin only."""
    if not re.search(r"what should i (investigate|review)|investigate (today|next)|inspect today|which projects should i", ql):
        return None
    if user is None or not auth.has_role_rank(user, "auditor"):
        return ("Project triage (\u201cwhat should I investigate\u201d) is available to auditors and "
                "administrators. You can still ask me about MPLADS, Ground Verification, or any "
                "public project.", [])
    rows = (
        db.query(models.Project, models.RiskScore)
        .join(models.RiskScore, models.RiskScore.project_id == models.Project.id)
        .order_by(models.RiskScore.risk_score.desc())
        .limit(5)
        .all()
    )
    pending_evidence = (
        db.query(func.count(models.VerificationReport.id))
        .filter(models.VerificationReport.status.isnot(None))
        .scalar() or 0
    )
    if not rows:
        return "No scored projects available right now.", []
    lines = "\n".join(
        f"• **#{p.id} {p.project_name}** — risk {r.risk_score} ({r.risk_level}), "
        f"{p.state}, {_fmt_money(p.sanctioned_amount)}"
        for p, r in rows
    )
    answer = (
        "**Suggested investigation queue (top risk first):**\n"
        f"{lines}\n\n"
        f"{pending_evidence:,} verification reports exist in the evidence pool. "
        "Open any project's AI Forensics tab before deciding a disposition."
    )
    return answer, [
        ActionOut(label="Open Audit Priority", action="navigate", target="Audit Priority"),
        ActionOut(label="View Audit Details", action="navigate", target="AI Audit Center"),
    ]


def _intent_my_role_data(ql: str, user, db: Session):
    """Role-aware personal data: verifications, inquiries, submissions."""
    if user is None:
        return None
    caps = auth.user_capabilities(user)

    if re.search(r"(my|assigned).{0,20}(submission|verification|report)s?|submissions? (needing|pending|awaiting)", ql):
        if "verification:read_own" in caps:
            mine = (
                db.query(models.VerificationReport)
                .filter(models.VerificationReport.user_id == user.id)
                .order_by(models.VerificationReport.created_at.desc())
                .limit(8)
                .all()
            )
            total = (
                db.query(func.count(models.VerificationReport.id))
                .filter(models.VerificationReport.user_id == user.id)
                .scalar() or 0
            )
            if total == 0:
                role_word = "verifications" if user.role != "citizen" else "evidence submissions"
                return (f"You have no {role_word} yet. Open Ground Verification, look up a project "
                        "ID, and submit your first report.", [
                            ActionOut(label="Open Ground Verification", action="navigate",
                                      target="Ground Truth Verification")])
            lines = "\n".join(
                f"• #{r.project_id} — {r.status} · {r.created_at[:16].replace('T', ' ')}"
                for r in mine
            )
            return (f"You have {total:,} submissions (latest {len(mine)}):\n{lines}", [
                ActionOut(label="My Verifications", action="navigate", target="My Verifications")])
        return None

    if re.search(r"(my|pending).{0,15}inquir|inquiries?.{0,15}(me|my|district)", ql):
        if "inquiry:respond" in caps and (user.assigned_district or user.assigned_state):
            scope_label = user.assigned_district or user.assigned_state
            if user.assigned_district:
                n_open = (
                    db.query(func.count(models.Inquiry.id))
                    .filter(models.Inquiry.district == user.assigned_district,
                            models.Inquiry.status == "open")
                    .scalar() or 0
                )
            else:
                n_open = (
                    db.query(func.count(models.Inquiry.id))
                    .join(models.Project, models.Project.id == models.Inquiry.project_id)
                    .filter(models.Project.state == user.assigned_state,
                            models.Inquiry.status == "open")
                    .scalar() or 0
                )
            return (f"Your area ({scope_label}) has {n_open} open inquiries. "
                    "Respond from My District; the issuing auditor reviews your reply.", [
                        ActionOut(label="Open My District", action="navigate", target="My District")])
        if "inquiry:respond" in caps:
            return ("No district/state is assigned to your account yet, so inquiries can't be "
                    "routed to you. Ask an administrator to set your assignment.", [])
        if "inquiry:review" in caps:
            counts = dict(
                db.query(models.Inquiry.status, func.count(models.Inquiry.id))
                .group_by(models.Inquiry.status).all()
            )
            return (f"Inquiries: {counts.get('open', 0)} open · {counts.get('responded', 0)} awaiting "
                    "your review · "
                    f"{counts.get('closed', 0)} closed. Issue new ones from an inspection modal.", [
                        ActionOut(label="Open Inquiries", action="navigate", target="Inquiries")])
        return ("Inquiries are exchanged between auditors and district authorities. "
                "Ask me about anything else — projects, evidence, or MPLADS basics.", [])
    return None


def _intent_analytical_screening(ql: str, user, db: Session):
    """Analytical screening questions: cost deviation, progress/expenditure
    mismatch, delayed, and ground-verification needs — all answered from a
    single SQL-side query per filter so the 512MB RAM budget is respected.
    Neutral terminology throughout: potential anomalies, never accusations."""
    is_cost = bool(re.search(r"cost (deviation|deviations|overrun|outlier)|unusual cost", ql))
    is_mismatch = bool(re.search(r"progress.{0,25}expenditure.{0,15}mismatch|expenditure.{0,25}progress.{0,15}mismatch|financial.{0,20}physical.{0,15}mismatch|money.{0,20}progress.{0,15}mismatch"
                                 r"|high expenditure.{0,25}(low|no|zero)\s+(progress|completion)|(low|no|zero)\s+(progress|completion).{0,25}high expenditure", ql))
    is_delayed = bool(re.search(r"\bdelayed\b|\bdelays\b|delayed projects|behind schedule", ql))
    is_verify = bool(re.search(r"(require|need|needs|which projects?).{0,20}(ground|field) verification|projects? (that )?need.{0,15}verification|requiring verification", ql))
    if not (is_cost or is_mismatch or is_delayed or is_verify):
        return None
    if user is None or not auth.has_role_rank(user, "analyst"):
        return ("Analytical screening is available to analysts, auditors and administrators. "
                "I can still help with project lookups, Ground Verification and general MPLADS questions.", [])

    # Optional state narrowing — "high cost deviation in Uttar Pradesh"
    state = _extract_state(ql)
    q = (
        db.query(models.Project, models.RiskScore)
        .join(models.RiskScore, models.RiskScore.project_id == models.Project.id)
        .filter(models.Project.sanctioned_amount > 0)
    )
    # Per-project ledger linkage via the SAME conservative normalized-key
    # join used by the timeline (work key + MP + IDA). The catalog's
    # per-project expenditure column is a zeroed legacy stamp and must not
    # drive screens.
    import metrics as _metrics
    exp_map = _metrics.project_expenditure_map(db)
    label = ""
    if is_cost:
        q = q.filter(
            models.Project.id.in_([pid for pid, amt in exp_map.items() if amt > 0])
        )
        label = "payments recorded in the expenditure ledger for their work (per-project cost-deviation screening is unavailable because sanctioned amounts are not recorded against those payment rows)"
    elif is_mismatch:
        q = q.filter(
            models.Project.completion_percentage < 25,
            models.Project.id.in_([pid for pid, amt in exp_map.items() if amt >= 100000])
        )
        label = ("payments of ₹1 lakh or more are recorded for the work in the expenditure ledger "
                 "while recorded physical completion is below 25% (potential progress/expenditure mismatch)")
    elif is_delayed:
        q = q.filter(models.Project.id.in_([pid for pid, amt in exp_map.items() if amt > 0]))
        label = ("payments are recorded in the expenditure ledger while the catalog carries no "
                 "'Not Started' statuses — delayed-work screening by status is unavailable in this dataset")
    else:  # verification needs
        q = q.filter(
            models.RiskScore.risk_score >= 50,
            models.RiskScore.risk_level != "HIGH",
            ~exists().where(models.VerificationReport.project_id == models.Project.id),
            ~exists().where(models.Inquiry.project_id == models.Project.id),
        )
        label = ("risk score 50–99 (MEDIUM band) with no ground-verification evidence and no "
                 "open inquiry — candidates for field verification")
    if state:
        q = q.filter(models.Project.state == state)
    rows = q.order_by(models.RiskScore.risk_score.desc()).limit(6).all()
    count = q.order_by(None).count()
    if count == 0:
        scope = f" in {state.title()}" if state else ""
        return f"No projects currently match that screen{scope}.", []
    lines = "\n".join(_fmt_project_line(p, r) for p, r in rows)
    scope = f" in {state.title()}" if state else " across all states"
    answer = (f"{count:,} projects{scope} have {label}. Top {len(rows)} by risk score:\n\n{lines}\n\n"
              "These are AI/analytical indicators for review prioritisation — not confirmed findings.")
    return answer, [ActionOut(label="Open Risk Center", action="navigate", target="Risk Center")]


def _intent_anomalies(ql: str, user, db: Session):
    if not re.search(r"unusual spending|unusual spend|anomal(y|ies)|suspicious", ql):
        return None
    # Pure concept asks ("what is an anomaly?", "how does the anomaly model "
    # "work?") belong to the knowledge base, not the flagged-projects list.
    if re.search(r"what (is|does)\b.{0,20}anomal|anomal(y|ies)\b.{0,15}\bmean|how does the anomal", ql) \
            and not re.search(r"how many|show|list|which|top", ql):
        return None
    if user is None or not auth.has_role_rank(user, "analyst"):
        return ("Anomaly analysis is available to analysts, auditors and administrators. "
                "I can still help with project lookups, Ground Verification and MPLADS questions.", [])
    rows = (
        db.query(models.Project, models.RiskScore)
        .join(models.RiskScore, models.RiskScore.project_id == models.Project.id)
        .filter(models.RiskScore.ml_anomaly.is_(True))
        .order_by(models.RiskScore.risk_score.desc())
        .limit(6)
        .all()
    )
    total = db.query(func.count(models.RiskScore.id)).filter(models.RiskScore.ml_anomaly.is_(True)).scalar() or 0
    if total == 0:
        return "No ML-flagged anomalies in the current risk data.", []
    lines = "\n".join(_fmt_project_line(p, r) for p, r in rows)
    return (f"{total:,} projects are flagged as statistical anomalies (Isolation Forest vs. similar "
            f"projects). Top 6:\n{lines}", [
                ActionOut(label="Open Risk Center", action="navigate", target="Risk Center")])


# ═══════════════════════════════════════════════════════════════════
# Page-context help
# ═══════════════════════════════════════════════════════════════════

PAGE_HELP = {
    "Overview": "This Overview is the portfolio dashboard: fund utilization, completion mix, risk insights and AI narrative summaries generated from live data.",
    "Projects": "The Projects page lists every monitored project. Use the search box and filters (state, constituency, district, status, type, FY) to narrow the list; click a project to open its full detail drawer.",
    "Risk Center": "The Risk Center ranks projects by their 0–100 risk score with the contributing factors (utilization, progress mismatch, delays). It also shows first/latest expenditure dates and the estimated 'Delayed By' duration.",
    "State Intelligence": "State Intelligence compares states/UTs: project counts, sanctioned vs. spent, completion rates. Click a state to drill into its projects.",
    "MP Intelligence": "MP Intelligence gives a member-level view of MPLADS: sanctioned amounts, expenditure, utilization, completed vs pending works and completion rates for every MP, with Lok Sabha / Rajya Sabha / All scoping. Open an MP for their work-level breakdown.",
    "Reports": "Reports offers exportable summaries of the portfolio for offline analysis and briefings.",
    "AI Audit Center": "The AI Audit Command Center scans for photo fraud, cost outliers, GPS mismatches and delayed SLAs, and ranks projects by a 0–100 audit risk index. Click Inspect on any row for the forensic summary.",
    "Audit Priority": "Audit Priority converts detected risk into a P1–P4 review order so auditors know what to look at first. P1 = investigate immediately.",
    "Compare Projects": "Compare Projects lets you pick two or more projects side by side — money, progress, risk factors — to see relative standing.",
    "Vendor Network": "Vendor Network (BETA) maps vendor clusters sharing offices or name families. Edges are shared-office/name signals; flags are leads, not proof.",
    "Vendor Intelligence": "Vendor Intelligence (BETA) profiles every vendor in the expenditure records — totals, transaction counts, distinct projects, geography. Search by vendor, state or constituency.",
    "Ground Truth Verification": "Ground Verification is the evidence portal: look up a project ID, take a photo, capture GPS, report the observed status. Signed-in users can track their own submissions; field verifiers and auditors review them.",
    "My Verifications": "My Verifications lists every evidence report you personally submitted, with status and timestamps.",
    "My District": "My District is the district-authority portal: scoped projects plus auditor inquiries awaiting your official response.",
    "Inquiries": "Inquiries tracks every auditor-issued inquiry, the district authority's response, and closure status.",
    "Saved Projects": "Saved Projects is your bookmarked list for quick follow-up.",
    "My Investigations": "My Investigations tracks projects you opened an investigation on, with priority tier and recommendations.",
    "My Audit Cases": "My Audit Cases holds the audit cases you saved; case content is regenerated from live data whenever opened.",
    "Settings": "Settings controls appearance (theme, font size) and account preferences.",
    "Leaderboard": "Leaderboard ranks recorded performance across states, constituencies and MPs using the same catalogue and ledger sources as the other pages.",
    "FAQ": "FAQ answers common questions about MPLADS, evidence submission and how the portal's risk signals should be read.",
    "Administration": "Administration is the admin-only surface for users, roles and district assignments.",
    "Data Management": "Data Management is the admin-only CSV import pipeline: preview a file, review detected columns and issues, commit, roll back or refresh derived data.",
}

# Contextual starter chips per page (intersected with role defaults client-side
# via /assistant/suggestions?page=…). Ordered most-specific first.
PAGE_SUGGESTIONS = {
    "Overview": ["What is the purpose of this website?", "What do the risk levels mean?"],
    "Projects": ["How do I find a project?", "What does High Risk mean?", "What is a project?"],
    "Risk Center": ["Why is this risky?", "What does High Risk mean?", "What is a risk score?", "What evidence do we have?", "Generate an audit brief"],
    "State Intelligence": ["Which states have the most projects?", "How many projects are in Karnataka?", "How many high-risk Lok Sabha projects are in Telangana?"],
    "MP Intelligence": [
        "How much has this MP spent?",
        "How many works has this MP completed?",
        "What is this MP's utilization?",
        "Show me this MP's pending works",
        "Show me Rajya Sabha MP statistics",
    ],
    "Reports": ["What reports can I export?", "What is the purpose of this website?"],
    "AI Audit Center": ["Why was this project flagged?", "Explain the risk score", "What should an auditor check?"],
    "Audit Priority": ["What does P1 mean?", "Why is this project prioritized?", "Generate an audit brief", "Are there any data issues with this project?", "Compare this project with similar projects"],
    "Compare Projects": ["How do I compare two projects?", "What does High Risk mean?", "Is this expenditure high compared with similar projects?"],
    "Vendor Network": ["What is Vendor Network?", "What does a vendor cluster mean?"],
    "Vendor Intelligence": ["What is Vendor Intelligence?", "How do I search for a vendor?"],
    "Ground Truth Verification": [
        "How do I submit evidence?",
        "Why is GPS useful?",
        "What happens after submission?",
    ],
    "My Verifications": ["Show my submissions", "What happens after I submit?"],
    "My District": ["Show my pending inquiries", "How do I respond to an inquiry?"],
    "Inquiries": ["What is an inquiry?", "How does the inquiry workflow work?"],
    "Evidence Queue": ["Which submissions need verification?", "How does verification work?"],
    "FAQ": ["What is MPLADS?", "How can I report an issue?"],
}


# ═══════════════════════════════════════════════════════════════════
# Suggestions per role
# ═══════════════════════════════════════════════════════════════════

SUGGESTIONS = {
    "general": [
        "What is MPLADS?",
        "What is the purpose of this website?",
        "How does Ground Verification work?",
        "How do I search for a project?",
    ],
    "citizen": [
        "How do I submit project evidence?",
        "What happens after I upload a photo?",
        "How many projects are in the portfolio?",
        "What is a risk score?",
    ],
    "field_verifier": [
        "Show my submissions",
        "What does a risk score mean?",
        "How does verification work?",
        "Who verifies citizen evidence?",
    ],
    "district_authority": [
        "Show my pending inquiries",
        "How do I respond to an inquiry?",
        "What is an inquiry?",
        "How do I search for a project?",
    ],
    "analyst": [
        "How many projects are in Karnataka?",
        "Which states have the most projects?",
        "Which projects have high risk?",
        "What is a risk score?",
    ],
    "auditor": [
        "What should I investigate today?",
        "Which projects have high risk?",
        "Find unusual spending.",
        "What does P1 mean?",
    ],
    "admin": [
        "What should I investigate today?",
        "Which projects have high risk?",
        "Show unusual spending.",
        "How does the inquiry workflow work?",
    ],
}


# ═══════════════════════════════════════════════════════════════════
# Greetings — pure greetings are conversation, never application commands
# ═══════════════════════════════════════════════════════════════════

_GREET_HEAD = (
    r"(?:hello(?:\s+(?:there|assistant|jarvis))?|hi(?:\s+(?:there|assistant|jarvis))?"
    r"|hey(?:\s+(?:there|assistant|jarvis))?|good\s+(?:morning|afternoon|evening)\s*(?:assistant|jarvis)?"
    r"|namaste(?:\s+ji)?|namaskar|namaskara"
    r"|assalamu.?alaikum|as-?salamu.?alaikum|salam|salaam)"
)
_PURE_GREETING_RE = re.compile(rf"^{_GREET_HEAD}[\s,.!?]*$", re.I)
_GREET_HEAD_RE = re.compile(rf"^{_GREET_HEAD}[\s,.!?]+(?=[a-z])", re.I)


def _greeting_reply(ql: str) -> str:
    """Natural, varied greeting replies — no navigation, no queries."""
    if re.search(r"alaikum|salam|salaam", ql):
        return "Wa Alaikum Assalam! How can I help you?"
    if re.search(r"namaste|namaskar", ql):
        return "Namaste! How can I help you with MPLADS AI?"
    if re.search(r"good\s+morning", ql):
        return "Good morning! What can I do for you?"
    if re.search(r"good\s+afternoon", ql):
        return "Good afternoon! What can I do for you?"
    if re.search(r"good\s+evening", ql):
        return "Good evening! What can I do for you?"
    import random
    return random.choice([
        "Hello! How can I help you with MPLADS AI?",
        "Hi! What would you like me to do — search projects, apply filters, or open a page?",
        "Hey! Ask me to navigate anywhere, filter projects, or look up a project ID.",
    ])


def _pad5(result) -> Tuple[str, str, List[ActionOut], Optional[int], Dict[str, Any]]:
    """Normalise every intent result to the assistant's 5-field contract.
    Older handlers still return 2/3/4-tuples; they must not crash /ask."""
    values = list(result or [])
    while len(values) < 5:
        values.append({} if len(values) == 4 else None)
    return tuple(values[:5])


def _answer(question: str, page: Optional[str], user, db: Session,
            session_id: Optional[str] = None) -> Tuple[str, str, List[ActionOut], Optional[int]]:
    """Entry point: greets pure greetings conversationally (no actions, no
    queries); for mixed messages ("Hi, open Risk Center") strips the greeting,
    executes the request, and prefixes a short acknowledgement."""
    ql = question.lower().strip()
    if _PURE_GREETING_RE.match(ql):
        return _greeting_reply(ql), "help", [], None, {"intent": "greeting"}
    if _GREET_HEAD_RE.match(ql):
        body = _GREET_HEAD_RE.sub("", question, count=1).strip()
        if not body:
            return _greeting_reply(ql), "help", [], None, {"intent": "greeting"}
        answer, source, actions, pid, meta = _pad5(_answer_body(body, page, user, db, session_id))
        greet = _greeting_reply(ql).split("!")[0] + "! "
        return greet + answer, source, actions, pid, {**meta, "intent": meta.get("intent") or "greeting"}
    return _pad5(_answer_body(question, page, user, db, session_id))


def _answer_body(question: str, page: Optional[str], user, db: Session,
                 session_id: Optional[str] = None) -> Tuple[str, str, List[ActionOut], Optional[int]]:
    ql = question.lower().strip()

    ctx_project_id = None
    if page and page.startswith("project:"):
        try:
            ctx_project_id = int(page.split(":", 1)[1])
        except ValueError:
            ctx_project_id = None
    # Conversational memory: the project discussed earlier in this session —
    # used when the user refers to "it" without the page context carrying one.
    if ctx_project_id is None:
        ctx_project_id = _session_get_project(session_id)
    # Ordinal follow-ups ("why is the first one risky?") resolve against the
    # project-id list behind the previous list/analytics answer.
    ordinal = assistant_nlu.ordinal_index(ql)
    if ctx_project_id is None and ordinal is not None:
        result_ids = _session_get_results(session_id)
        if result_ids and -len(result_ids) <= ordinal < len(result_ids):
            ctx_project_id = result_ids[ordinal]
    real_page = page if page and not page.startswith("project:") else None

    # "This MP" support: when the user is on the MP Intelligence page with an
    # MP open, remember it in the session context so questions like "how much
    # has this MP spent?" resolve without re-typing the name.
    if page and page.startswith("mp:"):
        try:
            _session_set_ctx(session_id, page=page)
        except Exception:
            pass

    # 0. Page-context questions FIRST — "how does this work" while sitting on
    #    Ground Verification must explain Ground Verification, not something else.
    asks_page_context = bool(re.search(
        r"what is this|what am i looking at|what does this (page|show)|help.{0,10}this page|"
        r"where am i|about this page|how does this (page )?work",
        ql,
    ))
    if asks_page_context and real_page and real_page in PAGE_HELP:
        return PAGE_HELP[real_page], "help", [], None
    # But with a project drawer open, "what is this (project)?" means the
    # PROJECT — it must reach the project-context intent, not page help.
    if asks_page_context and ctx_project_id and re.search(r"project|work\b|one\b|it\b", ql):
        asks_page_context = False

    # 1. Data intents first (they may fall through with None). Order matters:
    #    navigation, then ID-anchored lookups, then role/data analytics.
    discussed_pid: Optional[int] = None
    results = [
        # Portfolio analytics ahead of MP lookup: "compare expenditure between
        # Telangana and Karnataka" is a state question, not an MP name search.
        # Every branch is pattern-gated; unmatched questions fall through.
        (lambda: assistant_analytics.analytics_intent(question, ql, user, db), "data"),
        # MP Intelligence — MP comparisons, house-scoped MP stats, MPs-by-state
        # lists, per-MP attribute questions. Runs BEFORE generic navigation so
        # "show me Lok Sabha MPs in Telangana" reaches the MP view; its guards
        # defer project-scoped questions (any mention of "project") to the
        # project/context intents below. Identity is MP+House+Constituency.
        (lambda: mp_assistant.mp_intelligence_intent(question, ql, user, db, session_id=session_id), "data"),
        # ── Investigation layer (Features 3/5/6/7 + multi-turn) ──────
        # Session context for this turn: page/project context, session's
        # discussed project/state. Computed once, shared by all intents.
        # Investigation workspace Q&A first when the question is explicitly
        # about an investigation: it must use the persisted findings/history,
        # not a generic project summary. Falls through for every non-match.
        (lambda: investigation_intents.investigation_workspace_intent(
            question, ql, ctx_project_id, user, db, session_ctx=_session_get_ctx(session_id)), "data"),
        # Deep "why is this risky?" reasoning first — with a project in
        # context it must beat the generic navigation/lookup handlers.
        (lambda: assistant_review.review_intent(
            question, ql, ctx_project_id, user, db, session_ctx=_session_get_ctx(session_id)), "data"),
        (lambda: investigation_intents.risk_reasoning_intent(
            question, ql, ctx_project_id, user, db, session_ctx=_session_get_ctx(session_id)), "data"),
        (lambda: investigation_intents.audit_brief_intent(
            question, ql, ctx_project_id, user, db, session_ctx=_session_get_ctx(session_id)), "data"),
        (lambda: _intent_high_risk(ql, user, db, session_id=session_id), "data"),
        (lambda: investigation_intents.peer_comparison_intent(
            question, ql, ctx_project_id, user, db, session_ctx=_session_get_ctx(session_id)), "data"),
        (lambda: investigation_intents.data_quality_intent(
            question, ql, ctx_project_id, user, db, session_ctx=_session_get_ctx(session_id)), "data"),
        (lambda: investigation_intents.next_review_intent(
            question, ql, ctx_project_id, user, db, session_ctx=_session_get_ctx(session_id)), "data"),
        (lambda: investigation_intents.state_summary_intent(
            question, ql, user, db, session_ctx=_session_get_ctx(session_id)), "data"),
        (lambda: _intent_navigate(question, ql, ctx_project_id, user, db, session_id=session_id), "data"),
        # Two-ID metric comparison before project-context (which bails on two
        # numbers but would otherwise let a session-anchored single project
        # swallow "which has higher expenditure between A and B").
        (lambda: _intent_compare_metrics(question, ql, user, db), "data"),
        # Deep project-context Q&A (why/how/where/who/when/compare/timeline/
        # audit/dna) for the project open in the drawer — BEFORE the ID
        # lookup (which would answer everything with the summary card).
        (lambda: _intent_project_context(question, ql, ctx_project_id, user, db, session_id=session_id), "data"),
        (lambda: _intent_project_lookup(question, ql, ctx_project_id, user, db), "data"),
        # List/screening intents BEFORE _intent_project_detail: plural data
        # requests ("show high-risk projects", "progress/expenditure mismatch")
        # must not be swallowed by the project-context attribute regex.
        # List/screening intents before the project-context handler: a genuine
        # portfolio question must not be swallowed by a project discussed in
        # an earlier turn. Analytics/high-risk already ran above.
        (lambda: _intent_analytical_screening(ql, user, db), "data"),
        (lambda: _intent_anomalies(ql, user, db), "data"),
        # Portfolio analytics Q&A — counts/utilization/cohorts. After the
        # project-context intent (project-scoped "how much was sanctioned?"
        # wins) but before the attribute prompt ("how many projects have zero
        # expenditure?" must never be answered with "open a project first").
        (lambda: _intent_analytics(question, ql, user, db), "data"),
        (lambda: _intent_project_detail(q=question, ql=ql, ctx_project_id=ctx_project_id, user=user, db=db), "data"),
        (lambda: _intent_risk_methodology(ql, user, ctx_project_id, db), "data"),
        (lambda: _intent_my_role_data(ql, user, db), "data"),
        (lambda: _intent_investigate_next(ql, user, db), "data"),
        (lambda: _intent_vendor(question, ql, user, db), "data"),
        (lambda: _intent_portfolio_stats(question, user, db), "data"),
        (lambda: _intent_state_top(ql, user, db), "data"),
    ]
    for fn, source in results:
        result = fn()
        if result is None:
            continue
        # Intents return 2-tuples (answer, actions), 3-tuples (+ discussed
        # project id) or 4-tuples (+ action context to remember).
        ctx_update = None
        meta = {}
        if len(result) >= 5:
            answer, actions, discussed_pid, ctx_update, meta = result[:5]
        elif len(result) == 4:
            answer, actions, discussed_pid, ctx_update = result
        elif len(result) == 3:
            answer, actions, discussed_pid = result
        else:
            answer, actions = result
        if discussed_pid:
            _session_set_ctx(session_id, project_id=discussed_pid)
        if isinstance(ctx_update, dict) and ctx_update.get("page"):
            _session_set_ctx(session_id, page=ctx_update["page"],
                             filters=ctx_update.get("filters") or {},
                             state=(ctx_update.get("filters") or {}).get("state") or ctx_update.get("state"))
        meta = dict(meta or {})
        nlu_meta = assistant_nlu.classify(question, page)
        meta.setdefault("intent", nlu_meta.get("intent") or "help")
        entity = dict(meta.get("entity") or nlu_meta.get("entity") or {})
        if discussed_pid:
            entity.setdefault("type", "project")
            entity.setdefault("id", int(discussed_pid))
        meta["entity"] = entity
        result_ids = assistant_nlu.result_ids_from_meta(meta)
        if discussed_pid:
            result_ids = [int(discussed_pid)] + [p for p in result_ids if p != int(discussed_pid)]
        if result_ids:
            _session_set_results(session_id, result_ids)
        # Enforce the action allowlist AND the destination role guard on EVERY
        # outgoing action. Intents that construct actions directly must not be
        # able to hand an analyst an admin-only destination.
        safe_actions = []
        for action in _valid_actions(actions):
            if action.action == "navigate" and not assistant_nav.can_access(str(action.target), user):
                continue
            safe_actions.append(action)
        return answer, source, safe_actions, discussed_pid, meta

    # 2. Knowledge base — answers gain a contextual action chip pointing at
    #    the screen matching the topic asked about.
    for pattern, key in KNOWLEDGE:
        if re.search(pattern, ql):
            answer = KNOWLEDGE_ANSWERS[key]
            actions: List[ActionOut] = []
            nav = {
                "ground_verification": ("Open Ground Verification", "Ground Truth Verification"),
                "submit_evidence": ("Open Ground Verification", "Ground Truth Verification"),
                "citizen_evidence": ("Open Ground Verification", "Ground Truth Verification"),
                "report_issue": ("Open Ground Verification", "Ground Truth Verification"),
                "verify_project": ("Open Ground Verification", "Ground Truth Verification"),
                "search_help": ("Browse projects", "Projects"),
                "vendor_intelligence": ("Open Vendor Intelligence", "Vendor Intelligence"),
                "vendor_network": ("Open Vendor Network", "Vendor Network"),
                "ai_audit": ("Open AI Audit Center", "AI Audit Center"),
                "priority_tiers": ("Open Audit Priority", "Audit Priority"),
                "status_meanings": ("Browse projects", "Projects"),
                "risk_center": ("Open Risk Center", "Risk Center"),
                "compare_help": ("Open Compare Projects", "Compare Projects"),
                "respond_inquiry": ("Open My District", "My District"),
            }.get(key)
            if nav:
                actions.append(ActionOut(label=nav[0], action="navigate", target=nav[1]))
            return answer, "knowledge", actions, None

    # 3. Generic "how does this work / what is this" without a matching page
    #    (unknown or project-context page) — route to the Projects explainer.
    if asks_page_context:
        return PAGE_HELP["Projects"], "help", [], None

    # 3b. Page-scoped how-to questions on a known page — "what should an
    #     auditor check?" on AI Audit Center. Only generic phrasings, so
    #     role/data intents above still win for data questions.
    if real_page and re.search(
        r"what should (an|the) (auditor|verifier|authority) check|what.{0,12}check (here|on this page)|"
        r"how do i use this|what can i do here",
        ql,
    ):
        return PAGE_HELP[real_page], "help", [], None

    # 4. Fallback — honest "can't answer" with pointers
    role_note = ""
    if user is None:
        role_note = " You're browsing as a guest — sign in to submit evidence or use workspace features."
    return (
        "I don't have enough data to answer that. I can help with:\n"
        "• MPLADS basics and how this site works\n"
        "• Ground Verification and evidence submissions\n"
        "• Project lookups and risk explanations (try \u201cwhy is project 80649 risky?\u201d)\n"
        "• Portfolio stats (\u201chow many projects in Karnataka?\u201d)"
        + role_note,
        "help",
        [],
        None,
    )


@router.post("/ask", response_model=AskResponse)
def ask(
    payload: AskRequest,
    user: Optional[models.User] = Depends(_optional_user),
    db: Session = Depends(get_db),
):
    """Ask the MPLADS assistant. Role enforcement is server-side: every data
    intent re-checks auth.has_role_rank / capabilities before answering.
    Returns project_id so clients can keep conversation context for
    follow-ups like "why is it risky?" → "open it"."""
    session_id = payload.session_id or str(uuid.uuid4())
    result = _answer(
        payload.question, payload.page, user, db, session_id=session_id
    )
    answer, source, actions, discussed_pid = result[:4]
    meta = dict(result[4] if len(result) > 4 else {})
    nlu = assistant_nlu.classify(payload.question, payload.page)
    intent = str(meta.get("intent") or nlu.get("intent") or "help")[:40]
    entity = meta.get("entity") or nlu.get("entity") or {}
    findings = list(meta.get("findings") or [])[:8]
    recommendations = list(meta.get("recommendations") or [])[:7]
    limitations = [str(item)[:300] for item in (meta.get("limitations") or [])][:8]
    table = meta.get("table")
    if isinstance(table, dict):
        table = {
            "columns": [str(c)[:60] for c in (table.get("columns") or [])][:8],
            "rows": [
                [str(cell)[:120] for cell in (row or [])][:8]
                for row in (table.get("rows") or [])[:12]
            ],
            "caption": str(table.get("caption") or "")[:160],
        }
    else:
        table = None
    # Single choke point: every action leaving the API is checked against the
    # allowlist, whichever intent produced it.
    return AskResponse(
        answer=answer, source=source, actions=_valid_actions(actions),
        project_id=discussed_pid, intent=intent, entity=entity if isinstance(entity, dict) else {},
        findings=[f for f in findings if isinstance(f, dict)],
        recommendations=[r for r in recommendations if isinstance(r, dict)],
        limitations=limitations, table=table,
    )


@router.get("/suggestions")
def suggestions(page: Optional[str] = None):
    """Suggested questions per role, optionally blended with page-specific
    chips. Page chips come first (they match the user's current context);
    the client keeps the list compact."""
    if not page or page not in PAGE_SUGGESTIONS:
        return SUGGESTIONS
    # Dedupe while preserving order — page chips often overlap role defaults.
    blended = list(dict.fromkeys(PAGE_SUGGESTIONS[page] + SUGGESTIONS["general"][:2]))
    return {"general": blended}


@router.get("/context-help")
def context_help(page: str):
    """One-paragraph explanation of the page currently on screen — used by
    the assistant's 'about this page' context chip."""
    text = PAGE_HELP.get(page)
    if not text:
        raise HTTPException(status_code=404, detail="Unknown page")
    return {"page": page, "help": text}
    return {"page": page, "help": text}
