"""
MPLADS AI — centralized navigation registry
===========================================

ONE source of truth for every page the application shell can render, the
aliases the assistant understands, the parameters each page actually consumes,
and the role guard for each destination.

The frontend page registry (frontend/src/App.jsx) remains the runtime source
of truth for rendering. This module mirrors those page keys exactly and is the
only place the assistant builds navigation actions from, so route names can
never drift or be invented. Any destination not listed here is rejected by
the action validator before it reaches the client.

Guards mirror the sidebar's visibility rules (auth.ROLE_RANK /
ROLE_CAPABILITIES). The backend enforces them when producing actions; the
frontend independently drops actions the signed-in user cannot perform.
"""

from typing import Dict, Optional, Set

import auth


# Project-detail tabs supported by ProjectDetail.jsx. Anything else is
# clamped by the client, but validating here keeps the contract honest.
PROJECT_TABS: Set[str] = {
    "overview", "financial", "risk", "forensics", "audit", "timeline", "similar", "dna",
}

# Nav params the client executor understands at all. Per-page subsets below
# decide what a specific destination will actually consume.
ALL_NAV_PARAMS: Set[str] = {
    "state", "constituency", "status", "risk_level", "tier", "severity", "fy",
    "keyword", "house", "mp_id", "vendor", "a", "b", "project",
}

# Page key -> spec. `aliases` are regex fragments matched against lowercased
# questions. Specific aliases are listed before generic ones by resolution
# order below; the assistant's existing ordered NAV_PAGES still decides which
# page wins, and this registry validates/annotates the result.
PAGES: Dict[str, Dict] = {
    "Overview": {
        "aliases": r"\boverview\b|\bhome\b(?:\s*(?:page|screen))?|\bdashboard\b",
        "guard": None,
        "params": set(),
        "description": "Portfolio dashboard: fund utilization, completion mix, risk insights and AI narrative summaries.",
        "group": "Monitoring",
    },
    "Projects": {
        "aliases": r"\bprojects?\b(?:\s*(?:page|list|tab|explorer))?|\bproject explorer\b",
        "guard": None,
        "params": {"state", "constituency", "status", "fy", "keyword", "house"},
        "description": "Searchable catalogue of every monitored project, with filters and a full detail drawer.",
        "group": "Monitoring",
    },
    "Risk Center": {
        "aliases": r"\brisk\s+center\b|\brisk\s+centre\b",
        "guard": None,
        "params": {"risk_level", "state", "house"},
        "description": "Projects ranked by the 0–100 risk score with contributing factors and expenditure dates.",
        "group": "Monitoring",
    },
    "State Intelligence": {
        "aliases": r"\bstate\s+intelligence\b|\bstates?\s+(?:analysis|performance|comparison)\b",
        "guard": None,
        "params": {"state"},
        "description": "State/UT comparison: projects, sanctioned vs spent, completion, risk distribution.",
        "group": "Monitoring",
    },
    "MP Intelligence": {
        "aliases": r"\bmp\s+intelligence\b|\bmps?\s+(?:intelligence|analysis|performance)\b|\bmember\s+of\s+parliament\b",
        "guard": None,
        "params": {"mp_id", "house", "state"},
        "description": "Member-level view: allocation, expenditure, utilization and completed/recommended works.",
        "group": "Monitoring",
    },
    "Reports": {
        "aliases": r"\breports?\b|\bexport\b",
        "guard": None,
        "params": set(),
        "description": "Exportable portfolio summaries for offline analysis and briefings.",
        "group": "Monitoring",
    },
    "Leaderboard": {
        "aliases": r"\bleaderboard\b|\brankings?\b",
        "guard": None,
        "params": set(),
        "description": "Ranked performance view across states, constituencies and MPs.",
        "group": "Monitoring",
    },
    "Compare Projects": {
        "aliases": r"\bcompare\s+(?:projects?|two\s+projects?)\b|\bproject\s+comparison\b",
        "guard": None,
        "params": {"a", "b"},
        "description": "Side-by-side comparison of two or more projects: money, progress and risk factors.",
        "group": "Monitoring",
    },
    "AI Audit Center": {
        "aliases": r"\bai\s+audit(?:\s+(?:command\s+)?cent(?:er|re))?\b|\baudit\s+(?:command\s+)?cent(?:er|re)\b",
        "guard": "public_or_analyst",
        "params": {"project"},
        "description": "Anomaly screening — photo duplication, cost outliers, GPS mismatches, delayed SLAs — with a 0–100 audit index.",
        "group": "Audit",
    },
    "Audit Priority": {
        "aliases": r"\baudit\s+priority\b|\bpriority\s+(?:list|queue|ranking)\b",
        "guard": "public_or_analyst",
        "params": {"tier", "severity", "state", "project"},
        "description": "P1–P4 review order converting detected risk into an audit queue.",
        "group": "Audit",
    },
    "Vendor Network": {
        "aliases": r"\bvendor\s+network\b|\bvendor\s+cluster\b",
        "guard": "public_or_analyst",
        "params": set(),
        "description": "Vendor cluster graph based on shared offices and name families — investigative leads, not proof.",
        "group": "Audit",
    },
    "Vendor Intelligence": {
        "aliases": r"\bvendor\s+intelligence\b|\bvendor\s+profiles?\b|\bvendors?\b",
        "guard": "public_or_analyst",
        "params": {"vendor", "state"},
        "description": "Vendor profiles from expenditure records: totals, transactions, works and geography.",
        "group": "Audit",
    },
    "Ground Truth Verification": {
        "aliases": r"\bground\s+(?:truth\s+)?verification\b|\bverification\s+portal\b|\bsubmit\s+evidence\b",
        "guard": "verification:submit_or_guest",
        "params": {"project"},
        "description": "Evidence portal: project lookup, photo, GPS, observed status and review queue.",
        "group": "Field",
    },
    "My Verifications": {
        "aliases": r"\bmy\s+(?:evidence|verifications?|submissions?)\b",
        "guard": "verification:read_own",
        "params": set(),
        "description": "Your own field/citizen evidence submissions with review status.",
        "group": "Workspace",
    },
    "My District": {
        "aliases": r"\bmy\s+district\b|\bdistrict\s+(?:portal|workspace)\b",
        "guard": "inquiry:respond",
        "params": set(),
        "description": "District-authority workspace: scoped projects and auditor inquiries awaiting response.",
        "group": "Workspace",
    },
    "Inquiries": {
        "aliases": r"\binquiries\b|\binquiry\s+(?:queue|list|tracker)\b",
        "guard": "inquiry:review",
        "params": set(),
        "description": "Auditor-issued inquiries, authority responses and closure status.",
        "group": "Workspace",
    },
    "Evidence Queue": {
        "aliases": r"\bevidence\s+queue\b|\bverification\s+queue\b",
        "guard": "verification:review",
        "params": set(),
        "description": "Citizen/field evidence awaiting verification.",
        "group": "Workspace",
    },
    "Saved Projects": {
        "aliases": r"\bsaved\s+projects?\b|\bbookmarks?\b",
        "guard": "analyst",
        "params": set(),
        "description": "Your bookmarked projects for follow-up.",
        "group": "Workspace",
    },
    "My Investigations": {
        "aliases": r"\bmy\s+investigations?\b",
        "guard": "analyst",
        "params": set(),
        "description": "Investigations you opened, with priority and status.",
        "group": "Workspace",
    },
    "My Audit Cases": {
        "aliases": r"\bmy\s+audit\s+cases?\b|\baudit\s+cases?\b",
        "guard": "auditor",
        "params": set(),
        "description": "Saved audit cases; content is regenerated from live data when opened.",
        "group": "Workspace",
    },
    "Administration": {
        "aliases": r"\badmin(?:istration)?(?:\s+(?:page|panel|area|console))?\b|\buser\s+management\b",
        "guard": "users:manage",
        "params": set(),
        "description": "User and role administration.",
        "group": "Admin",
    },
    "Data Management": {
        "aliases": r"\bdata\s+management\b|\bcsv\s+(?:upload|import)\b|\bimport\s+(?:data|csv)\b|\bdata\s+imports?\b",
        "guard": "users:manage",
        "params": set(),
        "description": "CSV import pipeline: preview, commit, rollback and dataset refresh.",
        "group": "Admin",
    },
    "Settings": {
        "aliases": r"\bsettings?\b|\bpreferences\b",
        "guard": None,
        "params": set(),
        "description": "Appearance and account preferences.",
        "group": "Help",
    },
    "FAQ": {
        "aliases": r"\bfaq(?:s)?\b|\bhelp\s+page\b",
        "guard": None,
        "params": set(),
        "description": "Frequently asked questions about MPLADS and the portal.",
        "group": "Help",
    },
}

NAV_TARGETS: Set[str] = set(PAGES)

# Action contracts. navigate/filter actions carry page params; the validator
# narrows them to the destination's supported subset.
ACTION_PARAM_KEYS: Dict[str, Set[str]] = {
    "navigate": set(ALL_NAV_PARAMS),
    "filter_projects": {"state", "constituency", "status", "fy", "keyword", "house"},
    "filter_risk": {"risk_level", "state"},
    "open_project": {"tab"},
    "clear_filters": set(),
    "refresh_data": set(),
}

# Destinations that are conceptually pages but are not app shell routes.
# "Support" opens the assistant itself, so it is answered, never navigated to.
NON_ROUTE_DESTINATIONS = {
    "support": "Support is this assistant — I'm already the in-app support surface. I can answer questions, navigate, or open the relevant page for you.",
}


def allowed_params(page: str) -> Set[str]:
    """Params the given page's drill-down handler actually consumes."""
    return set((PAGES.get(page) or {}).get("params") or set())


def validate_params(page: str, params: Optional[Dict]) -> Dict:
    """Keep only values the destination supports, dropping empty/NULL ones."""
    allowed = allowed_params(page) if page in PAGES else ALL_NAV_PARAMS
    clean: Dict = {}
    for key, value in (params or {}).items():
        if key not in allowed or value is None or value == "":
            continue
        if key in ("a", "b", "project") and not str(value).isdigit():
            continue
        if key == "mp_id":
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
        clean[key] = value
    return clean


def _capabilities(user) -> Set[str]:
    return auth.user_capabilities(user) if user else set()


def guard_error(page: str, user) -> Optional[str]:
    """None when the user may open the page, else an honest explanation."""
    spec = PAGES.get(page)
    if not spec:
        return f"I don't have a destination called “{page}” in this application."
    guard = spec.get("guard")
    caps = _capabilities(user)
    if guard is None:
        return None
    if guard == "analyst":
        if user and auth.has_role_rank(user, "analyst"):
            return None
        return (f"The **{page}** workspace is available to analyst-tier accounts "
                "(analysts, auditors and administrators).")
    if guard == "auditor":
        if user and auth.has_role_rank(user, "auditor"):
            return None
        return f"The **{page}** workspace is available to auditors and administrators."
    if guard == "public_or_analyst":
        if user is None or auth.has_role_rank(user, "analyst"):
            return None
        return f"The **{page}** section is available to analysts, auditors and administrators."
    if guard == "verification:submit_or_guest":
        if user is None or "verification:submit" in caps:
            return None
        if "inquiry:respond" in caps:
            return ("Ground Verification submissions are made by citizens, field verifiers and "
                    "auditors. Your role's workspace is **My District**, where auditor inquiries "
                    "await your official response.")
        return "Submitting ground evidence needs a citizen, field verifier, auditor or administrator account."
    if guard in caps:
        return None
    return (f"The **{page}** section needs the “{guard.replace(':', ' ').strip()}” permission, "
            "which your account doesn't hold.")


def can_access(page: str, user) -> bool:
    return guard_error(page, user) is None


def page_description(page: str) -> str:
    return ((PAGES.get(page) or {}).get("description") or "")


def describe_platform() -> str:
    """Grouped destination list for help / feature-discovery answers."""
    groups: Dict[str, list] = {}
    for page, spec in PAGES.items():
        groups.setdefault(spec.get("group") or "Other", []).append(f"{page} — {spec['description']}")
    order = ["Monitoring", "Audit", "Field", "Workspace", "Admin", "Help"]
    lines = ["**What this platform contains**"]
    for group in order:
        if group not in groups:
            continue
        lines.append("")
        lines.append(f"**{group}**")
        for item in groups[group]:
            lines.append(f"• {item}")
    return "\n".join(lines)
