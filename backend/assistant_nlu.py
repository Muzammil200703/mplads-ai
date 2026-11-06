"""
MPLADS AI — structured intent & entity understanding
====================================================

A deterministic understanding layer that runs before the answer chain. It
classifies the user's request into one of the platform's intent categories,
extracts explicit entities and filters, and resolves conversational
references (ordinals such as "the first one", pronouns such as "it").

It deliberately does NOT answer questions and does NOT touch the database.
That separation keeps the contract from Part 13:
    LLM/understanding  ->  retrieval  ->  deterministic calculation
    ->  interpretation  ->  validated frontend actions.

The existing intent handlers remain the execution layer; this module gives
them a shared, explicit vocabulary so equivalent phrasings route the same way
and follow-ups can resolve against the previous result set.
"""

import re
from typing import Any, Dict, List, Optional

# Intent categories (Part 3).
INTENTS = (
    "greeting",
    "navigation",
    "project_lookup",
    "project_investigation",
    "risk_explanation",
    "financial_analysis",
    "mp_analysis",
    "state_analysis",
    "vendor_investigation",
    "evidence_analysis",
    "dataset_analytics",
    "comparison",
    "audit_recommendation",
    "data_quality",
    "report_generation",
    "help",
)

_GREETING = re.compile(
    r"^(hello|hi|hey|namaste|namaskar|namaskara|good\s+(morning|afternoon|evening)"
    r"|assalamu.?alaikum|salaam|thanks|thank\s+you)\b", re.I)

_NAV = re.compile(
    r"\b(take\s+me|bring\s+me|go\s*to|goto|jump\s+to|navigate|open|switch\s+to|head\s+to"
    r"|show\s+me\s+the|where\s+(?:is|are|can\s+i))\b", re.I)

_REPORT = re.compile(
    r"\baudit\s+brief\b|\bprepare\s+(?:an?\s+)?(?:audit|report|brief)\b|\breview\s+checklist\b"
    r"|\bgenerate\s+(?:an?\s+)?(?:audit|report|brief)\b|\bsummar(?:y|ise|ize)\s+(?:the\s+)?evidence\b"
    r"|\bprepare\s+(?:this\s+)?(?:project\s+)?for\s+review\b", re.I)

_AUDIT_REVIEW = re.compile(
    r"\breview\s+(this|the|that)?\s*(project|work|record|it)\b|\bproject\s+review\b"
    r"|\bwhat\s+should\s+i\s+(check|investigate|look\s+at)\b|\bnext\s+steps?\b"
    r"|\bidentify\s+(the\s+)?issues?.{0,30}(verify|verification|review)\b"
    r"|\bshould\s+an?\s+auditor\s+(inspect|review|check)\b"
    r"|\bsummar(?:y|ise|ize)\s+(?:the\s+)?(?:financial|physical|position|findings)\b", re.I)

_RISK = re.compile(
    r"\bwhy\b.{0,40}\b(risky|risk|flag|flagged|anomal|high.risk|warning|delayed)\b"
    r"|\bexplain\b.{0,30}\b(risk|score|flag)\b|\bwhat.s\s+wrong\s+with\b"
    r"|\bwhy\s+did\s+(the\s+)?(ai|model|system)\s+flag\b"
    r"|\brisk\s+(score|level|factors?|indicators?)\b", re.I)

_PROJECT_INVESTIGATION = re.compile(
    r"\bwhat\s+is\s+this\s+project\b|\btell\s+me\s+about\s+(this|that)\s+project\b"
    r"|\binvestigate\b|\bdeep\s+dive\b|\bproject\s+details?\b"
    r"|\b(who|when|where)\b.{0,40}\b(recommend|sanction|start|vendor|mp)\b"
    r"|\bwhat\s+(evidence|information|data)\b.{0,20}\b(available|missing|have)\b", re.I)

_COMPARISON = re.compile(
    r"\bcompare\b|\bcomparison\b|\bversus\b|\bvs\.?\b|\bsimilar\s+projects?\b"
    r"|\bpeers?\b|\bdifference\s+between\b", re.I)

_DATA_QUALITY = re.compile(
    r"\bdata\s+qualit|\bdata\s+(issues?|problems?|coverage|completeness|gaps?)\b"
    r"|\bmissing\s+(dates?|fields?|information|data|evidence|vendor)\b"
    r"|\binconsisten|\bdiscrepanc|\bduplicate|\bconflict(?:ing)?\b", re.I)

_VENDOR = re.compile(
    r"\bvendors?\b|\bcontractors?\b|\bsuppliers?\b|\bvendor\s+intelligence\b", re.I)

_MP = re.compile(
    r"\bmps?\b|\bmembers?\s+of\s+parliament\b|\bconstituency\s+mp\b", re.I)

_STATE = re.compile(
    r"\bstates?\b|\bstate\s+intelligence\b|\bconstituenc(?:y|ies)\b|\bdistrict\b", re.I)

_EVIDENCE = re.compile(
    r"\bevidence\b|\bverification\b|\bverify\b|\bgps\b|\bphoto\b|\bfield\s+(?:visit|observation)\b", re.I)

_ANALYTICS = re.compile(
    r"\bwhich\b|\bhow\s+many\b|\bhow\s+much\b|\bhighest\b|\blowest\b|\bmost\b|\bleast\b"
    r"|\btop\b|\bdistribution\b|\bbreakdown\b|\bcompare\b|\btrend\b|\baverage\b"
    r"|\bpercentage\b|\bcount\b|\branking\b", re.I)

_FINANCIAL = re.compile(
    r"\bexpenditure\b|\bspent\b|\bspend(?:ing)?\b|\bsanctioned\b|\butili[sz]ation\b"
    r"|\bfinancial\b|\bfunds?\b|\bunspent\b|\bremaining\b|\boverspend\b|\bpayment\b|\bcost\b", re.I)

_HELP = re.compile(
    r"\bwhat\s+can\s+you\s+do\b|\bwhat\s+can\s+i\s+do\b|\bhelp\b|\bfeatures?\b"
    r"|\bhow\s+does\s+this\s+(site|app|platform)\s+work\b|\bwhat\s+is\s+this\s+site\b", re.I)

_HOUSE = re.compile(r"\b(lok\s+sabha|rajya\s+sabha)\b", re.I)
_TIER = re.compile(r"\bp([1-4])\b", re.I)
_FY = re.compile(r"\b(20\d{2}\s*[-–]\s*\d{2,4})\b")
_STATUS = re.compile(r"\b(completed|complete|finished|ongoing|in\s+progress|recommended|proposed)\b", re.I)
_STATE_NOUN = re.compile(r"\b(projects?|works?|constituencies?|state)\b", re.I)
_ORDINAL = re.compile(r"\b(first|1st|second|2nd|third|3rd|fourth|4th|fifth|5th|last|top)\b", re.I)
_ORDINAL_INDEX = {
    "first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2,
    "fourth": 3, "4th": 3, "fifth": 4, "5th": 4, "last": -1, "top": 0,
}
_PROJECT_ID_PATTERNS = (
    r"#(\d{1,10})\b",
    r"\b(?:project|proj|work)\s*(?:no\.?|number|id)?\s*#?(\d{1,10})\b",
    r"\b(?:show|open|take\s+me\s+to|go\s*to|goto|jump\s+to|find|tell\s+me\s+about)\s+(?:of\s+|for\s+)?#?(\d{1,10})\b",
    r"\b(?:why\s+is|risk|status|details?)\s+(?:of\s+|for\s+)?#?(\d{1,10})\b",
    r"^\s*#?(\d{1,10})\s*\??$",
)


def extract_project_id(question: str) -> Optional[int]:
    """Conservative ID extraction — same anchors as the assistant's lookup."""
    for pattern in _PROJECT_ID_PATTERNS:
        match = re.search(pattern, question, re.I)
        if match:
            return int(match.group(1))
    return None


def ordinal_index(question: str) -> Optional[int]:
    """0-based result index for 'the first one' / 'the second project' etc."""
    match = _ORDINAL.search((question or "").lower())
    if not match:
        return None
    return _ORDINAL_INDEX.get(match.group(1).lower())


def is_reference(question: str) -> bool:
    """True when the message refers back to an existing entity/result set."""
    ql = (question or "").lower()
    return bool(
        ordinal_index(ql) is not None
        or re.search(r"\b(it|its|this|that|these|those|the same|the above|the previous)\b", ql)
    )


def extract_house(question: str) -> Optional[str]:
    match = _HOUSE.search(question or "")
    if not match:
        return None
    return "Lok Sabha" if match.group(1).lower().startswith("lok") else "Rajya Sabha"


def extract_tier(question: str) -> Optional[str]:
    match = _TIER.search(question or "")
    if match:
        return f"P{match.group(1)}"
    if re.search(r"\b(high|top)[- ]priority\b|\burgent\b", (question or ""), re.I):
        return "P1"
    return None


def extract_fy(question: str) -> Optional[str]:
    match = _FY.search(question or "")
    if not match:
        return None
    return re.sub(r"\s+", "", match.group(1)).replace("–", "-")


def extract_status(question: str) -> Optional[str]:
    match = _STATUS.search(question or "")
    if not match:
        return None
    value = match.group(1).lower()
    if value.startswith(("complete", "finished")):
        return "Completed"
    if value in ("ongoing", "in progress"):
        return "Ongoing"
    return "Recommended"


def extract_state_candidate(question: str) -> Optional[str]:
    """Unvalidated state phrase. Callers resolve it against the DB state list."""
    match = re.search(
        r"\b(?:in|from|of|for)\s+([a-z][a-z .&']{2,40}?)(?:\?|$|,|\s+(?:show|which|what|how|list|give|tell|projects?|works?|state|constituenc))",
        question or "", re.I,
    )
    if match:
        return match.group(1).strip(" ?.,")
    match = re.search(r"\b([a-z][a-z .&']{2,40}?)\s+(?:projects?|works?|constituenc(?:y|ies))\b", question or "", re.I)
    if match:
        return " ".join(match.group(1).strip(" ?.,").split()[-2:])
    return None


def _vendor_candidate(question: str) -> Optional[str]:
    match = re.search(
        r"\bvendor[s]?\s+(?:named\s+|called\s+)?([A-Za-z][\w&.'\- ]{1,60}?)(?:\s+in\s+|\s+from\s+|\?|$)",
        question or "", re.I,
    )
    if not match:
        return None
    stop = {"intelligence", "network", "profile", "profiles", "analytics", "spending", "spend",
            "in", "from", "at", "for", "and", "the", "a", "an", "of", "on", "with", "by"}
    words = [word for word in match.group(1).split() if word.lower() not in stop]
    value = " ".join(words).strip(" ?.,!'-")
    return value if len(value) >= 3 else None


def _state_from_noun(question: str) -> Optional[str]:
    for match in _STATE_NOUN.finditer(question or ""):
        prefix = (question or "")[:match.start()].strip(" ,.?!:;-")
        words = re.findall(r"[a-z&'.]+", prefix, re.I)
        if words:
            return words[-1].lower()
    return None


def classify(question: str, page: Optional[str] = None) -> Dict[str, Any]:
    """Classify one user message. Pure, bounded, no I/O.

    Returns `{intent, confidence, entity, filters}`. The entity dict carries
    only explicit ids/values; the answer layer still resolves names against the
    database and refuses to invent anything.
    """
    q = (question or "").strip()
    ql = q.lower()
    entity: Dict[str, Any] = {}
    filters: Dict[str, Any] = {}

    project_id = extract_project_id(q)
    if project_id:
        entity = {"type": "project", "id": project_id}
    house = extract_house(q)
    if house:
        filters["house"] = house
    tier = extract_tier(q)
    if tier:
        filters["tier"] = tier
    fy = extract_fy(q)
    if fy:
        filters["fy"] = fy
    status = extract_status(q)
    if status and re.search(r"\b(projects?|works?|ones?)\b", ql):
        filters["status"] = status

    vendor = _vendor_candidate(q)
    if vendor:
        entity = {**entity, "type": entity.get("type") or "vendor", "name": vendor}

    state_candidate = extract_state_candidate(q) or _state_from_noun(q)
    if state_candidate:
        entity = {**entity, "state_candidate": state_candidate}

    if _GREETING.search(ql) and len(ql.split()) <= 8:
        intent, confidence = "greeting", 0.95
    elif _REPORT.search(ql):
        intent, confidence = "report_generation", 0.9
    elif _AUDIT_REVIEW.search(ql):
        intent, confidence = "audit_recommendation", 0.85
    elif _DATA_QUALITY.search(ql):
        intent, confidence = "data_quality", 0.85
    elif _RISK.search(ql):
        intent, confidence = "risk_explanation", 0.9
    elif vendor and not re.search(r"\bintelligence\s+page\b", ql):
        intent, confidence = "vendor_investigation", 0.85
    elif _COMPARISON.search(ql):
        intent, confidence = "comparison", 0.8
    elif _NAV.search(ql) and re.search(
        r"\b(overview|home|projects?|risk\s+cent(?:er|re)|state\s+intelligence|mp\s+intelligence"
        r"|reports?|leaderboard|compare\s+projects?|audit\s+priority|ai\s+audit"
        r"|vendor\s+(?:network|intelligence)|ground\s+(?:truth\s+)?verification"
        r"|settings|faq|data\s+management|administration|my\s+district|inquiries)\b", ql
    ):
        intent, confidence = "navigation", 0.85
    elif _ANALYTICS.search(ql) and re.search(
        r"\b(which|how\s+many|highest|lowest|most|distribution|breakdown|trend|states?|constituenc"
        r"|vendors?|projects?|risk|expenditure|completion)\b", ql
    ):
        intent, confidence = "dataset_analytics", 0.75
    elif project_id or re.search(r"\bproject\s+(?:id|#)\b", ql):
        intent, confidence = "project_lookup", 0.9
    elif _FINANCIAL.search(ql):
        intent, confidence = "financial_analysis", 0.7
    elif _MP.search(ql):
        intent, confidence = "mp_analysis", 0.8
    elif _EVIDENCE.search(ql):
        intent, confidence = "evidence_analysis", 0.75
    elif _STATE.search(ql):
        intent, confidence = "state_analysis", 0.7
    elif _PROJECT_INVESTIGATION.search(ql) or project_id:
        intent, confidence = "project_investigation", 0.8
    elif _HELP.search(ql):
        intent, confidence = "help", 0.9
    else:
        intent, confidence = "help", 0.35

    # A page-tagged project drawer makes investigation/risk asks project-scoped
    # even when the sentence itself is terse ("review this", "what's wrong?").
    if page and page.startswith("project:"):
        try:
            pid = int(page.split(":", 1)[1])
            entity.setdefault("type", "project")
            entity.setdefault("id", pid)
        except (ValueError, IndexError):
            pass
        if intent in ("help", "project_lookup") and re.search(
            r"\b(review|risk|wrong|flag|audit|check|investigate|expenditure|spent|timeline|vendor|evidence|missing)\b", ql
        ):
            intent = "project_investigation" if not _RISK.search(ql) else "risk_explanation"
            confidence = max(confidence, 0.8)

    return {"intent": intent, "confidence": round(confidence, 2), "entity": entity, "filters": filters}


def result_ids_from_meta(meta: Optional[Dict]) -> List[int]:
    """Pull validated project ids out of an intent's structured meta."""
    ids: List[int] = []
    for value in (meta or {}).get("result_ids") or []:
        try:
            pid = int(value)
        except (TypeError, ValueError):
            continue
        if 0 < pid < 10**10:
            ids.append(pid)
    return ids
