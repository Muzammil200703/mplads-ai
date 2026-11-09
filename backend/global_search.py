"""Unified record search for the global command palette.

One bounded, read-only endpoint backs every record lookup in the palette:
projects, MPs, vendors, constituencies, districts, states, investigations,
anomalies, audit findings, recorded changes, imports and report types. Pages
and quick actions are resolved client-side from the application's own page
registry, so this module never duplicates navigation.

Performance / correctness notes
-------------------------------
* Projects are searched in phases: exact ID + indexed name prefix first, then
  a name-substring pass, then constituency/state, and only then a typo
  fallback. Each pass is LIMITed, and a pass is skipped once the palette has
  enough rows, so a common term never pays for every strategy.
* Constituency and state aggregates are the only queries that need a full
  GROUP BY, so they are computed once per process and cached for five
  minutes; afterwards filtering 540 constituencies / 36 states is in-memory.
* Fuzzy matching compares the query against individual name tokens, not just
  whole names: "librery" must find "LIBRARY DEVELOPMENT, UMARDA". Its
  candidate pool is an indexed prefix lookup, never an unbounded scan.
* Destinations are validated against DESTINATIONS — the page keys that exist
  in App.jsx — so the client can never be handed a route that does not exist.
* Investigations are user- and role-scoped (own rows, plus shared audit cases
  for auditor+). Catalogue records follow the visibility of the existing
  /search/projects, /mp-intelligence and /vendor-intelligence endpoints.
"""

from __future__ import annotations

import difflib
import re
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

import auth
from auth import get_db  # same session dependency auth.py declares

router = APIRouter(tags=["Search"])

MAX_LIMIT = 20
CANDIDATE_POOL = 1000

# Page keys that exist in the application shell (App.jsx registry).
DESTINATIONS = {
    "Overview", "Projects", "Risk Center", "AI Audit Center", "Vendor Network",
    "Ground Truth Verification", "Reports", "Leaderboard", "State Intelligence",
    "Audit Priority", "Compare Projects", "FAQ", "Vendor Intelligence",
    "Settings", "Saved Projects", "MP Intelligence", "My Investigations",
    "My Audit Cases", "My Verifications", "My District", "Inquiries",
    "Evidence Queue", "Administration", "Data Management",
}

SEARCH_TYPES = (
    "projects", "mps", "vendors", "constituencies", "districts", "states",
    "investigations", "expenditures", "completed_works", "findings",
    "anomalies", "batches", "changes", "reports",
)

PROJECT_COLUMNS = (
    "id, project_name, state, district, constituency, house, status, "
    "completion_percentage"
)

# Reports are UI capabilities, not database rows. These are the exact report
# types the existing Reports page can export; a palette hit opens that page
# with the type preselected (Reports consumes drillDownParams.report_type).
REPORTS = (
    ("Project Audit Report", "Project-level audit dataset · CSV or JSON"),
    ("Anomaly Summary Report", "Risk-scored anomalies · CSV or JSON"),
    ("Regional Risk Report", "State and constituency risk view · CSV or JSON"),
    ("Financial Utilization Report", "Allocation, expenditure and utilization · CSV or JSON"),
)

# Small response cache: typing then deleting a character replays the same
# query, and the palette is debounced but still chatty.
_CACHE: Dict[tuple, tuple] = {}
_CACHE_TTL = 30.0
_CACHE_MAX = 64

# Geo aggregates are the only full-table GROUP BYs here.
_GEO: Dict[str, Any] = {"at": 0.0, "states": [], "constituencies": [], "districts": []}
_GEO_TTL = 300.0


def _cached(key: tuple) -> Optional[dict]:
    hit = _CACHE.get(key)
    if not hit:
        return None
    ts, payload = hit
    if (time.time() - ts) > _CACHE_TTL:
        _CACHE.pop(key, None)
        return None
    return payload


def _store(key: tuple, payload: dict) -> dict:
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[key] = (time.time(), payload)
    return payload


def _like(term: str) -> str:
    """Escape LIKE wildcards so a user's % or _ is literal text."""
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _money(value: Any) -> Optional[str]:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if amount >= 1e7:
        return f"₹{amount / 1e7:,.2f} Cr"
    if amount >= 1e5:
        return f"₹{amount / 1e5:,.2f} L"
    return f"₹{amount:,.0f}"


def _destination(page: str, **params: Any) -> Optional[dict]:
    if page not in DESTINATIONS:
        return None
    clean = {k: v for k, v in params.items() if v not in (None, "")}
    return {"page": page, "params": clean}


def _best_ratio(query: str, name: Any) -> float:
    """Similarity of the query to a name or any single word inside it.

    Word-level comparison is what makes a one-letter typo in the middle of a
    long project title ("librery" → "LIBRARY DEVELOPMENT…") score high.
    """
    q = (query or "").lower().strip()
    n = str(name or "").lower().strip()
    if not q or not n:
        return 0.0
    best = difflib.SequenceMatcher(None, q, n[: len(q)]).ratio()
    for token in re.split(r"[^a-z0-9]+", n):
        if token and abs(len(token) - len(q)) <= 4:
            ratio = difflib.SequenceMatcher(None, q, token).ratio()
            if ratio > best:
                best = ratio
    return best


def _fuzzy_rows(query: str, rows: List[Dict[str, Any]], key: str, want: int, cutoff: float = 0.8):
    if want <= 0 or len(query) < 4 or not rows:
        return []
    scored = sorted(((_best_ratio(query, row.get(key)), row) for row in rows), key=lambda p: -p[0])
    picked = []
    for ratio, row in scored:
        if ratio < cutoff:
            break
        picked.append(row)
        if len(picked) >= want:
            break
    return picked


def _geo(db: Session) -> Dict[str, Any]:
    if _GEO["states"] and (time.time() - _GEO["at"]) < _GEO_TTL:
        return _GEO
    try:
        states = [
            dict(r._mapping) for r in db.execute(text("""
                SELECT state, COUNT(*) AS n, SUM(sanctioned_amount) AS sanctioned
                FROM projects
                WHERE state IS NOT NULL AND state != ''
                GROUP BY state
            """)).all()
        ]
        constituencies = [
            dict(r._mapping) for r in db.execute(text("""
                SELECT constituency, state, COUNT(*) AS n
                FROM projects
                WHERE constituency IS NOT NULL AND constituency != ''
                GROUP BY constituency, state
            """)).all()
        ]
        districts = [
            dict(r._mapping) for r in db.execute(text("""
                SELECT district, state, COUNT(*) AS n
                FROM projects
                WHERE district IS NOT NULL AND district != ''
                GROUP BY district, state
            """)).all()
        ]
    except Exception:
        states, constituencies, districts = [], [], []
    _GEO.update({
        "at": time.time(),
        "states": states,
        "constituencies": constituencies,
        "districts": districts,
    })
    return _GEO


# ── Projects ─────────────────────────────────────────────────────────────

def _project_row(row) -> Optional[dict]:
    m = row._mapping
    bits = [m["state"], m["district"], m["house"] or "House unattributed"]
    if m["completion_percentage"] is not None:
        bits.append(f"{float(m['completion_percentage']):.0f}% complete")
    if m["status"]:
        bits.append(str(m["status"]))
    return {
        "type": "project",
        "id": f"project:{m['id']}",
        "record_id": m["id"],
        "title": m["project_name"] or f"Project {m['id']}",
        "subtitle": " · ".join([b for b in bits if b]),
        "meta": f"#{m['id']}",
        "destination": _destination("Projects", projectId=m["id"]),
    }


def _search_projects(db: Session, q: str, limit: int, house: Optional[str]) -> List[dict]:
    house_sql = " AND house = :house" if house else ""
    base: Dict[str, Any] = {"raw": q, "lim": limit}
    if house:
        base["house"] = house

    results: List[dict] = []
    seen: set = set()

    def collect(rows) -> None:
        for row in rows:
            built = _project_row(row)
            if built and built["id"] not in seen:
                seen.add(built["id"])
                results.append(built)

    # 1. Three separate indexed lookups — an OR across an indexed prefix and
    #    casted columns would force a full scan of 83k rows on every key.
    if q.isdigit():
        collect(db.execute(text(
            f"SELECT {PROJECT_COLUMNS} FROM projects WHERE id = :pid {house_sql}"
        ), {**base, "pid": int(q)}).all())
        if results:
            # An exact project ID is a complete answer; do not pay for the
            # substring/fuzzy passes just to pad the list.
            return results[:limit]

    if len(results) < limit:
        collect(db.execute(text(f"""
            SELECT {PROJECT_COLUMNS} FROM projects
            WHERE project_name LIKE :pre ESCAPE '\\' {house_sql}
            ORDER BY length(project_name), id
            LIMIT :lim
        """), {**base, "pre": q + "%"}).all())

    if len(results) < limit:
        collect(db.execute(text(f"""
            SELECT {PROJECT_COLUMNS} FROM projects
            WHERE (state = :raw OR district = :raw OR constituency = :raw
                   OR project_type = :raw OR status = :raw OR fy = :raw) {house_sql}
            ORDER BY id
            LIMIT :lim
        """), base).all())

    # 2. State / constituency / district quite often IS the query (“telangana”).
    #    The geo aggregates are already in memory, so resolve the name there and
    #    then hit the indexed equality instead of scanning 83k rows for a LIKE.
    if len(results) < limit:
        geo = _geo(db)
        ql = q.lower()
        state_hits = [r["state"] for r in geo["states"] if ql in str(r["state"]).lower()][:4]
        constituency_hits = [
            r["constituency"] for r in geo["constituencies"]
            if ql in str(r["constituency"]).lower()
        ][:4]
        district_hits = [
            r["district"] for r in geo["districts"]
            if ql in str(r["district"]).lower()
        ][:4]
        if state_hits or constituency_hits or district_hits:
            stmt = text(f"""
                SELECT {PROJECT_COLUMNS} FROM projects
                WHERE (state IN :states OR constituency IN :constituencies
                       OR district IN :districts) {house_sql}
                ORDER BY id
                LIMIT :lim
            """).bindparams(
                bindparam("states", expanding=True),
                bindparam("constituencies", expanding=True),
                bindparam("districts", expanding=True),
            )
            collect(db.execute(stmt, {
                **base,
                "states": state_hits or [""],
                "constituencies": constituency_hits or [""],
                "districts": district_hits or [""],
            }).all())

    # 3. Name substring (single column, bounded by LIMIT).
    if len(results) < limit:
        collect(db.execute(text(f"""
            SELECT {PROJECT_COLUMNS} FROM projects
            WHERE project_name LIKE :sub ESCAPE '\\' {house_sql}
            ORDER BY length(project_name), id
            LIMIT :lim
        """), {**base, "sub": _like(q)}).all())

    # 4. Typo fallback over an indexed prefix candidate pool.
    if not results and len(q) >= 4:
        pool = [
            dict(r._mapping) for r in db.execute(text(f"""
                SELECT {PROJECT_COLUMNS} FROM projects
                WHERE project_name LIKE :head ESCAPE '\\' {house_sql}
                LIMIT {CANDIDATE_POOL}
            """), {**base, "head": q[:4] + "%"}).all()
        ]
        for match in _fuzzy_rows(q, pool, "project_name", limit):
            built = {
                "type": "project",
                "id": f"project:{match['id']}",
                "record_id": match["id"],
                "title": match.get("project_name") or f"Project {match['id']}",
                "subtitle": " · ".join(
                    [b for b in [match.get("state"), match.get("house") or "House unattributed"] if b]
                ),
                "meta": f"#{match['id']}",
                "destination": _destination("Projects", projectId=match["id"]),
            }
            if built["destination"]:
                results.append(built)

    return results[:limit]


# ── MPs ──────────────────────────────────────────────────────────────────

MP_COLUMNS = ("id, mp_name, constituency, state, house, allocated_amount, "
              "total_expenditure, utilization_percentage")


def _mp_row(m) -> dict:
    return {
        "type": "mp",
        "id": f"mp:{m['id']}",
        "record_id": m["id"],
        "title": m["mp_name"] or f"MP {m['id']}",
        "subtitle": " · ".join([b for b in [m["constituency"], m["state"], m["house"]] if b]),
        "meta": _money(m["total_expenditure"]),
        "destination": _destination("MP Intelligence", mp_id=m["id"]),
    }


def _search_mps(db: Session, q: str, limit: int, house: Optional[str]) -> List[dict]:
    house_sql = " AND house = :house" if house else ""
    params: Dict[str, Any] = {"sub": _like(q), "pre": q + "%", "lim": limit}
    if house:
        params["house"] = house
    rows = db.execute(text(f"""
        SELECT {MP_COLUMNS} FROM mp_summaries
        WHERE (mp_name LIKE :sub ESCAPE '\\'
               OR constituency LIKE :sub ESCAPE '\\'
               OR state LIKE :sub ESCAPE '\\')
          {house_sql}
        ORDER BY CASE WHEN mp_name LIKE :pre ESCAPE '\\' THEN 0 ELSE 1 END, mp_name
        LIMIT :lim
    """), params).all()
    results = [r for r in (_mp_row(row._mapping) for row in rows) if r["destination"]]

    if len(results) < limit and len(q) >= 4:
        # mp_summaries is 764 rows: comparing all names is cheaper than a
        # second indexed pass.
        pool = [
            dict(r._mapping) for r in db.execute(text(f"""
                SELECT {MP_COLUMNS} FROM mp_summaries WHERE 1=1 {house_sql}
            """), params).all()
        ]
        seen = {r["id"] for r in results}
        for match in _fuzzy_rows(q, pool, "mp_name", limit):
            built = _mp_row(match)
            if built["destination"] and built["id"] not in seen:
                results.append(built)
                seen.add(built["id"])
    return results[:limit]


# ── Vendors ──────────────────────────────────────────────────────────────

VENDOR_TABLE = "vendor_intel_cache"
VENDOR_COLUMNS = ("vendor_key, vendor_name, vendor_type, project_count, "
                  "transaction_count, total_expenditure")


def _vendor_row(m) -> dict:
    bits = [m["vendor_type"]]
    if m["transaction_count"]:
        bits.append(f"{int(m['transaction_count']):,} payments")
    if m["project_count"]:
        bits.append(f"{int(m['project_count']):,} projects")
    return {
        "type": "vendor",
        "id": f"vendor:{m['vendor_key']}",
        "record_id": m["vendor_key"],
        "title": m["vendor_name"],
        "subtitle": " · ".join([b for b in bits if b]),
        "meta": _money(m["total_expenditure"]),
        "destination": _destination("Vendor Intelligence", vendor=m["vendor_key"]),
    }


def _vendor_cache_exists(db: Session) -> bool:
    return bool(db.execute(text(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=:name"
    ), {"name": VENDOR_TABLE}).first())


def _search_vendors(db: Session, q: str, limit: int) -> List[dict]:
    if not _vendor_cache_exists(db):
        return []

    rows = db.execute(text(f"""
        SELECT {VENDOR_COLUMNS} FROM {VENDOR_TABLE}
        WHERE vendor_name LIKE :sub ESCAPE '\\'
        ORDER BY CASE WHEN vendor_name LIKE :pre ESCAPE '\\' THEN 0 ELSE 1 END,
                 total_expenditure DESC
        LIMIT :lim
    """), {"sub": _like(q), "pre": q + "%", "lim": limit}).all()
    results = [r for r in (_vendor_row(row._mapping) for row in rows) if r["destination"]]

    if len(results) < limit and len(q) >= 4:
        pool = [
            dict(r._mapping) for r in db.execute(text(f"""
                SELECT {VENDOR_COLUMNS} FROM {VENDOR_TABLE}
                WHERE vendor_name LIKE :head ESCAPE '\\'
                ORDER BY total_expenditure DESC
                LIMIT {CANDIDATE_POOL}
            """), {"head": q[:4] + "%"}).all()
        ]
        seen = {r["id"] for r in results}
        for match in _fuzzy_rows(q, pool, "vendor_name", limit):
            built = _vendor_row(match)
            if built["destination"] and built["id"] not in seen:
                results.append(built)
                seen.add(built["id"])
    return results[:limit]


# ── Constituencies / states (cached aggregates) ──────────────────────────

def _search_constituencies(db: Session, q: str, limit: int) -> List[dict]:
    rows = _geo(db)["constituencies"]
    ql = q.lower()

    def build(row) -> dict:
        return {
            "type": "constituency",
            "id": f"constituency:{row['constituency']}|{row.get('state') or ''}",
            "record_id": row["constituency"],
            "title": row["constituency"],
            "subtitle": f"{row.get('state') or 'State unknown'} · {int(row['n']):,} projects",
            "meta": None,
            "destination": _destination(
                "Projects", constituency=row["constituency"], state=row.get("state")
            ),
        }

    hits = [row for row in rows if ql in str(row["constituency"]).lower()]
    hits.sort(key=lambda r: (0 if str(r["constituency"]).lower().startswith(ql) else 1, -int(r["n"] or 0)))

    if len(hits) < limit and len(q) >= 4:
        remaining = [row for row in rows if row not in hits]
        for match in _fuzzy_rows(q, remaining, "constituency", limit - len(hits)):
            hits.append(match)

    return [built for built in (build(row) for row in hits[:limit]) if built["destination"]]


def _search_states(db: Session, q: str, limit: int) -> List[dict]:
    rows = _geo(db)["states"]
    ql = q.lower()

    def build(row) -> dict:
        return {
            "type": "state",
            "id": f"state:{row['state']}",
            "record_id": row["state"],
            "title": row["state"],
            "subtitle": f"{int(row['n']):,} projects · {_money(row['sanctioned']) or '—'} sanctioned",
            "meta": None,
            "destination": _destination("Projects", state=row["state"]),
        }

    hits = [row for row in rows if ql in str(row["state"]).lower()]
    hits.sort(key=lambda r: (0 if str(r["state"]).lower().startswith(ql) else 1, -int(r["n"] or 0)))

    if len(hits) < limit and len(q) >= 4:
        remaining = [row for row in rows if row not in hits]
        for match in _fuzzy_rows(q, remaining, "state", limit - len(hits)):
            hits.append(match)

    return [built for built in (build(row) for row in hits[:limit]) if built["destination"]]


def _search_districts(db: Session, q: str, limit: int) -> List[dict]:
    rows = _geo(db)["districts"]
    ql = q.lower()

    def build(row) -> dict:
        return {
            "type": "district",
            "id": f"district:{row['district']}|{row.get('state') or ''}",
            "record_id": row["district"],
            "title": row["district"],
            "subtitle": f"{row.get('state') or 'State unknown'} · {int(row['n']):,} projects",
            "meta": None,
            "destination": _destination(
                "Projects",
                state=row.get("state"),
                keyword=row["district"],
            ),
        }

    hits = [row for row in rows if ql in str(row["district"]).lower()]
    hits.sort(key=lambda r: (0 if str(r["district"]).lower().startswith(ql) else 1, -int(r["n"] or 0)))

    if len(hits) < limit and len(q) >= 4:
        remaining = [row for row in rows if row not in hits]
        for match in _fuzzy_rows(q, remaining, "district", limit - len(hits)):
            hits.append(match)

    return [built for built in (build(row) for row in hits[:limit]) if built["destination"]]


# ── Investigations (private + auditor-scoped) ────────────────────────────

def _search_investigations(
    db: Session, q: str, limit: int, user, house: Optional[str],
) -> List[dict]:
    if not user:
        return []
    is_analyst = auth.has_role_rank(user, "analyst")
    is_auditor = auth.has_role_rank(user, "auditor")
    if not (is_analyst or is_auditor):
        return []

    results: List[dict] = []
    seen: set = set()
    params: Dict[str, Any] = {"sub": _like(q), "raw": q, "uid": user.id, "lim": limit}
    house_sql = " AND p.house = :house" if house else ""
    if house:
        params["house"] = house

    if is_analyst:
        rows = db.execute(text(f"""
            SELECT ui.id, ui.project_id, ui.status, ui.priority_label,
                   p.project_name, p.state, p.house
            FROM user_investigations ui
            JOIN projects p ON p.id = ui.project_id
            WHERE ui.user_id = :uid
              AND (p.project_name LIKE :sub ESCAPE '\\' OR CAST(ui.project_id AS TEXT) = :raw)
              {house_sql}
            ORDER BY ui.updated_at DESC
            LIMIT :lim
        """), params).all()
        for row in rows:
            m = row._mapping
            seen.add(("own", m["id"]))
            results.append({
                "type": "investigation",
                "id": f"investigation:{m['id']}",
                "record_id": m["id"],
                "title": m["project_name"] or f"Project {m['project_id']}",
                "subtitle": " · ".join(
                    [b for b in ["Your investigation", m["status"], m["priority_label"], m["state"]] if b]
                ),
                "meta": f"#{m['project_id']}",
                "destination": _destination("My Investigations", projectId=m["project_id"]),
            })

    if is_auditor:
        rows = db.execute(text(f"""
            SELECT ai.id, ai.project_id, ai.status, ai.risk_level_at_start,
                   p.project_name, p.state, p.house
            FROM audit_investigations ai
            JOIN projects p ON p.id = ai.project_id
            WHERE (p.project_name LIKE :sub ESCAPE '\\' OR CAST(ai.project_id AS TEXT) = :raw)
              {house_sql}
            ORDER BY ai.updated_at DESC
            LIMIT :lim
        """), params).all()
        for row in rows:
            m = row._mapping
            if ("audit", m["id"]) in seen:
                continue
            seen.add(("audit", m["id"]))
            results.append({
                "type": "investigation",
                "id": f"audit-case:{m['id']}",
                "record_id": m["id"],
                "title": m["project_name"] or f"Project {m['project_id']}",
                "subtitle": " · ".join(
                    [b for b in ["Audit case", m["status"], m["risk_level_at_start"], m["state"]] if b]
                ),
                "meta": f"#{m['project_id']}",
                "destination": _destination("My Audit Cases", projectId=m["project_id"]),
            })
    return results[:limit]


# ── Payments (expenditure ledger) ───────────────────────────────────────

def _search_expenditures(db: Session, q: str, limit: int, user) -> List[dict]:
    """Ledger payments by work description, vendor or MP.

    A payment is not a project: it links to a work through description +
    constituency + state, never a guaranteed project id. The row therefore
    opens Vendor Intelligence when a vendor is recorded, otherwise the
    Projects list scoped to its state/constituency.
    """
    # Individual payments are not part of the public catalogue: signed-in
    # users only, matching the analyst-facing vendor surfaces.
    if not user:
        return []
    try:
        rows = db.execute(text("""
            SELECT id, mp_name, constituency, state, house, work_description,
                   vendor, expenditure_amount, expenditure_date
            FROM expenditures
            WHERE work_description LIKE :sub ESCAPE '\\'
               OR vendor LIKE :sub ESCAPE '\\'
               OR mp_name LIKE :sub ESCAPE '\\'
            ORDER BY expenditure_date DESC
            LIMIT :lim
        """), {"sub": _like(q), "lim": limit}).all()
    except Exception:
        return []

    out: List[dict] = []
    for row in rows:
        m = row._mapping
        vendor = (m["vendor"] or "").strip()
        dest = (
            _destination("Vendor Intelligence", vendor=vendor)
            if vendor else _destination("Projects", state=m["state"], constituency=m["constituency"])
        )
        if not dest:
            continue
        out.append({
            "type": "expenditure",
            "id": f"expenditure:{m['id']}",
            "record_id": m["id"],
            "title": m["work_description"] or (f"Payment to {vendor}" if vendor else f"Payment #{m['id']}"),
            "subtitle": " · ".join(
                [b for b in [vendor or "Vendor not recorded", m["state"], (m["expenditure_date"] or "")[:10] or None] if b]
            ),
            "meta": _money(m["expenditure_amount"]),
            "destination": dest,
        })
    return out


# ── Completed works (completions ledger) ────────────────────────────────

def _search_completed_works(db: Session, q: str, limit: int, house: Optional[str]) -> List[dict]:
    house_sql = " AND house = :house" if house else ""
    params: Dict[str, Any] = {"sub": _like(q), "lim": limit}
    if house:
        params["house"] = house
    try:
        rows = db.execute(text(f"""
            SELECT id, work_id, work_description, category, state, constituency,
                   house, final_amount, completed_date
            FROM completed_works
            WHERE work_description LIKE :sub ESCAPE '\\' {house_sql}
            ORDER BY completed_date DESC
            LIMIT :lim
        """), params).all()
    except Exception:
        return []

    out: List[dict] = []
    for row in rows:
        m = row._mapping
        # completed_works.work_id is an eSAKSHI work identifier, NOT a
        # projects.id. Opening it as a project id produces a 404. The honest
        # destination is the existing Projects list scoped to the recorded
        # state/constituency.
        dest = _destination("Projects", state=m["state"], constituency=m["constituency"])
        if not dest:
            continue
        out.append({
            "type": "completed_work",
            "id": f"completed:{m['id']}",
            "record_id": m["id"],
            "title": m["work_description"] or f"Completed work {m['id']}",
            "subtitle": " · ".join(
                [b for b in ["Completed", m["state"], (m["completed_date"] or "")[:10] or None] if b]
            ),
            "meta": _money(m["final_amount"]),
            "destination": dest,
        })
    return out


# ── Audit findings (analyst / auditor only) ─────────────────────────────

def _search_findings(db: Session, q: str, limit: int, user) -> List[dict]:
    if not user:
        return []
    is_analyst = auth.has_role_rank(user, "analyst")
    is_auditor = auth.has_role_rank(user, "auditor")
    if not (is_analyst or is_auditor):
        return []
    try:
        rows = db.execute(text("""
            SELECT af.id, af.project_id, af.title, af.confidence, af.status,
                   p.project_name, p.state
            FROM agent_findings af
            JOIN projects p ON p.id = af.project_id
            WHERE af.title LIKE :sub ESCAPE '\\'
            ORDER BY af.id DESC
            LIMIT :lim
        """), {"sub": _like(q), "lim": limit}).all()
    except Exception:
        return []

    page = "My Audit Cases" if is_auditor else "My Investigations"
    out: List[dict] = []
    for row in rows:
        m = row._mapping
        out.append({
            "type": "finding",
            "id": f"finding:{m['id']}",
            "record_id": m["id"],
            "title": m["title"],
            "subtitle": " · ".join(
                [b for b in [m["project_name"], m["confidence"], m["status"]] if b]
            ),
            "meta": f"#{m['project_id']}",
            "destination": _destination(page, projectId=m["project_id"]),
        })
    return out


# ── Import batches (admin only) ─────────────────────────────────────────

def _search_batches(db: Session, q: str, limit: int, user) -> List[dict]:
    if not user or not auth.has_role_rank(user, "admin"):
        return []
    try:
        rows = db.execute(text("""
            SELECT id, filename, dataset_type, status, uploaded_at, total_rows
            FROM import_batches
            WHERE filename LIKE :sub ESCAPE '\\'
            ORDER BY id DESC
            LIMIT :lim
        """), {"sub": _like(q), "lim": limit}).all()
    except Exception:
        return []
    return [{
        "type": "batch",
        "id": f"batch:{row._mapping['id']}",
        "record_id": row._mapping["id"],
        "title": row._mapping["filename"],
        "subtitle": " · ".join(
            [b for b in [row._mapping["dataset_type"], row._mapping["status"], row._mapping["uploaded_at"]] if b]
        ),
        "meta": f"{int(row._mapping['total_rows'] or 0):,} rows",
        "destination": _destination("Data Management"),
    } for row in rows]


# ── Recorded change events (analyst+) ───────────────────────────────────

def _search_changes(db: Session, q: str, limit: int, user) -> List[dict]:
    if not user or not auth.has_role_rank(user, "analyst"):
        return []
    try:
        rows = db.execute(text("""
            SELECT ce.id, ce.project_id, ce.change_type, ce.field, ce.changed_at,
                   p.project_name, p.state
            FROM project_change_events ce
            JOIN projects p ON p.id = ce.project_id
            WHERE p.project_name LIKE :sub ESCAPE '\\'
               OR CAST(ce.project_id AS TEXT) = :raw
            ORDER BY ce.id DESC
            LIMIT :lim
        """), {"sub": _like(q), "raw": q, "lim": limit}).all()
    except Exception:
        return []
    out: List[dict] = []
    for row in rows:
        m = row._mapping
        out.append({
            "type": "change",
            "id": f"change:{m['id']}",
            "record_id": m["id"],
            "title": m["project_name"] or f"Project {m['project_id']}",
            "subtitle": " · ".join(
                [b for b in [m["change_type"], m["field"], m["changed_at"]] if b]
            ),
            "meta": f"#{m['project_id']}",
            "destination": _destination("Projects", projectId=m["project_id"]),
        })
    return out


# ── Risk anomalies (Risk Center records) ────────────────────────────────

ANOMALY_SELECT = """
    SELECT p.id, p.project_name, p.state, p.district, p.constituency, p.house,
           rs.risk_level, rs.risk_score, rs.ml_anomaly
    FROM projects p
    JOIN risk_scores rs ON rs.project_id = p.id
"""


def _anomaly_row(m) -> dict:
    bits: List[str] = []
    if m["risk_level"]:
        bits.append(f"{m['risk_level']} risk")
    if m["risk_score"] is not None:
        bits.append(f"{int(m['risk_score'])}/100")
    if m["ml_anomaly"]:
        bits.append("ML anomaly")
    bits.extend([b for b in [m["state"], m["district"], m["house"]] if b])
    return {
        "type": "anomaly",
        "id": f"anomaly:{m['id']}",
        "record_id": m["id"],
        "title": m["project_name"] or f"Project {m['id']}",
        "subtitle": " · ".join(bits),
        "meta": f"#{m['id']}",
        # Open the existing project drawer, which already carries the risk
        # explanation; the record itself is a Risk Center anomaly.
        "destination": _destination("Projects", projectId=m["id"]),
    }


def _search_anomalies(
    db: Session, q: str, limit: int, house: Optional[str],
) -> List[dict]:
    """Anomaly records from the existing risk pipeline.

    Only rows the Risk Center classifies as anomalous are returned: ML
    outliers and High-risk works. Three bounded passes are used (name prefix /
    exact id, exact geography, then a capped top-anomaly pool for partial and
    typo matches) so a common term never becomes a full-table scan.
    """
    house_sql = " AND p.house = :house" if house else ""
    params: Dict[str, Any] = {
        "lim": max(limit * 4, 24),
        "raw": q,
        "pre": q + "%",
    }
    if house:
        params["house"] = house

    results: List[dict] = []
    seen: set = set()

    def add(rows) -> None:
        for row in rows:
            m = row._mapping
            if m["id"] in seen:
                continue
            seen.add(m["id"])
            results.append(_anomaly_row(m))

    condition = "(rs.ml_anomaly = 1 OR rs.risk_level = 'High')"
    try:
        add(db.execute(text(f"""
            {ANOMALY_SELECT}
            WHERE (p.project_name LIKE :pre ESCAPE '\\' OR CAST(p.id AS TEXT) = :raw)
              AND {condition} {house_sql}
            ORDER BY rs.risk_score DESC
            LIMIT :lim
        """), params).all())

        if len(results) < limit:
            add(db.execute(text(f"""
                {ANOMALY_SELECT}
                WHERE (p.state = :raw OR p.district = :raw OR p.constituency = :raw)
                  AND {condition} {house_sql}
                ORDER BY rs.risk_score DESC
                LIMIT :lim
            """), params).all())

        if len(results) < limit and len(q) >= 4:
            pool: List[dict] = []
            pool_params = {"lim": 400}
            if house:
                pool_params["house"] = house
            for risk_condition in ("rs.ml_anomaly = 1", "rs.risk_level = 'High'"):
                pool.extend(
                    dict(row._mapping) for row in db.execute(text(f"""
                        {ANOMALY_SELECT}
                        WHERE {risk_condition} {house_sql}
                        ORDER BY rs.risk_score DESC
                        LIMIT :lim
                    """), pool_params).all()
                )
            ql = q.lower()
            candidates = []
            for match in pool:
                blob = " ".join(
                    str(match.get(key) or "")
                    for key in ("project_name", "state", "district", "constituency")
                ).lower()
                ratio = _best_ratio(q, match.get("project_name"))
                if ql in blob or ratio >= 0.8:
                    candidates.append((1.0 if ql in blob else ratio, match))
            candidates.sort(key=lambda pair: -pair[0])
            for _, match in candidates:
                if match["id"] in seen:
                    continue
                seen.add(match["id"])
                results.append(_anomaly_row(match))
                if len(results) >= limit:
                    break
    except Exception:  # an unavailable risk table must not break the palette
        return results[:limit]

    return results[:limit]


# ── Reports (static capabilities of the existing Reports page) ──────────

def _search_reports(q: str, limit: int) -> List[dict]:
    ql = q.lower()
    scored: List[tuple] = []
    for title, description in REPORTS:
        haystack = f"{title} {description}".lower()
        exact = title.lower().startswith(ql)
        if ql in haystack or exact or _best_ratio(q, title) >= 0.78:
            scored.append((0 if exact else 1, title, description))
    scored.sort(key=lambda row: row[0])
    out: List[dict] = []
    for _, title, description in scored[:limit]:
        destination = _destination("Reports", report_type=title)
        if not destination:
            continue
        out.append({
            "type": "report",
            "id": f"report:{title}",
            "record_id": title,
            "title": title,
            "subtitle": description,
            "meta": "Reports",
            "destination": destination,
        })
    return out


# ── Endpoint ─────────────────────────────────────────────────────────────

@router.get("/global-search")
def global_search(
    q: str = Query("", max_length=120, description="Free-text query: name, ID, constituency, district or state"),
    type: Optional[str] = Query(None, description="projects | mps | vendors | constituencies | districts | states | investigations | anomalies | findings | changes | reports"),
    limit: int = Query(6, ge=1, le=MAX_LIMIT),
    house: Optional[str] = Query(None, description="'Lok Sabha' / 'Rajya Sabha' — omitted for All"),
    user=Depends(auth.get_current_user),
    db: Session = Depends(get_db),
):
    """Categorised records for the command palette.

    ``type`` narrows the response to one category (the palette's "search only
    projects" scopes); omitting it returns a small group per category.
    """
    started = time.perf_counter()
    query = (q or "").strip()
    house_scope = house if house in ("Lok Sabha", "Rajya Sabha") else None
    wanted = type if type in SEARCH_TYPES else None

    payload = {
        "query": query,
        "type": wanted or "all",
        "house": house_scope,
        "counts": {t: 0 for t in SEARCH_TYPES},
        "results": [],
        "fuzzy": False,
        "took_ms": 0,
    }
    if len(query) < 2:
        payload["took_ms"] = int((time.perf_counter() - started) * 1000)
        return payload

    cache_key = (
        query.lower(), wanted, limit, house_scope,
        getattr(user, "id", None), getattr(user, "role", None),
    )
    cached = _cached(cache_key)
    if cached is not None:
        return cached

    per_type = limit if wanted else 3
    searchers = [
        ("projects", lambda: _search_projects(db, query, per_type, house_scope)),
        ("mps", lambda: _search_mps(db, query, per_type, house_scope)),
        ("vendors", lambda: _search_vendors(db, query, per_type)),
        ("constituencies", lambda: _search_constituencies(db, query, per_type)),
        ("districts", lambda: _search_districts(db, query, per_type)),
        ("states", lambda: _search_states(db, query, per_type)),
        ("investigations", lambda: _search_investigations(db, query, per_type, user, house_scope)),
        ("expenditures", lambda: _search_expenditures(db, query, per_type, user)),
        ("completed_works", lambda: _search_completed_works(db, query, per_type, house_scope)),
        ("findings", lambda: _search_findings(db, query, per_type, user)),
        ("anomalies", lambda: _search_anomalies(db, query, per_type, house_scope)),
        ("batches", lambda: _search_batches(db, query, per_type, user)),
        ("changes", lambda: _search_changes(db, query, per_type, user)),
        ("reports", lambda: _search_reports(query, per_type)),
    ]

    for name, run in searchers:
        if wanted and name != wanted:
            continue
        try:
            rows = [row for row in (run() or []) if row and row.get("destination")]
        except Exception:  # one category must never break the palette
            rows = []
        payload["counts"][name] = len(rows)
        payload["results"].extend(rows)

    payload["results"] = payload["results"][:MAX_LIMIT]
    payload["took_ms"] = int((time.perf_counter() - started) * 1000)
    return _store(cache_key, payload)
