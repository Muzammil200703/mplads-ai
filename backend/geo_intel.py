"""
MPLADS AI — Geospatial intelligence (Phase 2)
=============================================

Feature 14  Recorded-project coverage gaps
Feature 15  Spatial project relationship analysis
Feature 16  Sub-state execution patterns
Feature 17  Geospatial progress monitoring
Feature 18  Geographic outlier detection

Geography reality of this dataset (verified, not assumed):
  • `projects.district` is populated for only 4 of 83,625 rows;
  • `projects.constituency` is populated for all 83,625 rows (540 distinct);
  • no per-project latitude/longitude exists anywhere; only a handful of
    field-verification reports carry GPS.

Therefore every area aggregate uses the finest populated geography —
district where recorded, otherwise constituency — and every payload states
its `unit`, `precision` and the fallback reason. Coordinates are never
invented: only verification-report GPS points are treated as exact.

Coverage findings describe MPLADS *recorded-project* coverage. No population
or external infrastructure dataset exists here, so no infrastructure deficit
is ever claimed.
"""

from __future__ import annotations

import math
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import text
from sqlalchemy.orm import Session

MIN_AREA_PROJECTS = 10
OUTLIER_Z = 3.0
DISTRICT_MIN_RATIO = 0.05
CACHE_TTL = 240.0
_lock = threading.Lock()
_cache: Dict[str, Tuple[float, Any]] = {}


def _num(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
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


def _rows(db: Session, sql: str, params: Optional[Dict[str, Any]] = None) -> List[Any]:
    try:
        return db.execute(text(sql), params or {}).fetchall()
    except Exception:
        return []


def _median(values: List[float]) -> Optional[float]:
    vs = sorted(v for v in values if v is not None)
    if not vs:
        return None
    mid = len(vs) // 2
    return vs[mid] if len(vs) % 2 else (vs[mid - 1] + vs[mid]) / 2


def _mad(values: List[float]) -> Optional[float]:
    m = _median(values)
    if m is None:
        return None
    return _median([abs(v - m) for v in values if v is not None])


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _scope(filters: Dict[str, Any], alias: str = "p") -> Tuple[str, Dict[str, Any]]:
    bits: List[str] = []
    params: Dict[str, Any] = {}
    if filters.get("state"):
        bits.append(f"LOWER(TRIM({alias}.state)) = LOWER(:state)")
        params["state"] = filters["state"]
    if filters.get("district"):
        # District values are mostly absent; when the caller supplies one we
        # still filter strictly so nothing is silently broadened.
        bits.append(f"LOWER(TRIM({alias}.district)) = LOWER(:district)")
        params["district"] = filters["district"]
    if filters.get("constituency"):
        bits.append(f"LOWER(TRIM({alias}.constituency)) = LOWER(:constituency)")
        params["constituency"] = filters["constituency"]
    if filters.get("house") in ("Lok Sabha", "Rajya Sabha"):
        bits.append(f"{alias}.house = :house")
        params["house"] = filters["house"]
    if filters.get("category"):
        bits.append(f"LOWER(TRIM({alias}.project_type)) = LOWER(:category)")
        params["category"] = filters["category"]
    # No leading connector: each caller prefixes its own WHERE/AND.
    return " AND ".join(bits), params


# ══════════════════════════════════════════════════════════════════════
# Geographic unit selection — measured, not assumed
# ══════════════════════════════════════════════════════════════════════

def geo_unit(db: Session) -> Dict[str, Any]:
    def build():
        total = 0
        for r in _rows(db, "SELECT COUNT(*) FROM projects"):
            total = int(_num(r[0]) or 0)
        d = 0
        for r in _rows(db, "SELECT COUNT(*) FROM projects "
                           "WHERE district IS NOT NULL AND TRIM(district) <> ''"):
            d = int(_num(r[0]) or 0)
        c = 0
        for r in _rows(db, "SELECT COUNT(*) FROM projects "
                           "WHERE constituency IS NOT NULL AND TRIM(constituency) <> ''"):
            c = int(_num(r[0]) or 0)
        if total and d / total >= DISTRICT_MIN_RATIO:
            return {
                "unit": "district", "label": "District", "fallback": False,
                "districts_recorded": d, "constituencies_recorded": c, "total_projects": total,
                "explanation": "District is recorded for enough projects to aggregate at district level.",
            }
        return {
            "unit": "constituency", "label": "Constituency", "fallback": True,
            "districts_recorded": d, "constituencies_recorded": c, "total_projects": total,
            "explanation": (
                f"District is recorded for only {d} of {total:,} projects, so constituency "
                "is the finest geography available for aggregation. Constituency boundaries "
                "are not administrative districts and may cross them."),
        }
    return _cached("geo_unit", build, ttl=900)


def _area_expr(unit: str) -> str:
    col = "p.district" if unit == "district" else "p.constituency"
    return f"COALESCE(NULLIF(TRIM({col}), ''), '({unit} not recorded)')"


def _area_rows(db: Session, filters: Dict[str, Any], unit: str) -> List[Dict[str, Any]]:
    where, params = _scope(filters)
    expr = _area_expr(unit)
    rows = _rows(db, f"""
        SELECT {expr} AS area,
               COUNT(*) AS projects,
               AVG(p.completion_percentage) AS avg_completion,
               SUM(CASE WHEN p.completion_percentage IS NOT NULL
                          AND p.completion_percentage < 50 THEN 1 ELSE 0 END) AS under_50,
               SUM(CASE WHEN r.linked_expenditure > 0
                         AND (p.completion_percentage IS NULL OR p.completion_percentage < 1)
                        THEN 1 ELSE 0 END) AS spend_no_progress,
               SUM(r.linked_expenditure) AS expenditure,
               SUM(p.sanctioned_amount) AS sanctioned,
               SUM(CASE WHEN rs.risk_level IN ('High','Critical') THEN 1 ELSE 0 END) AS high_risk,
               SUM(CASE WHEN rs.ml_anomaly = 1 THEN 1 ELSE 0 END) AS anomalies,
               AVG(p.sanctioned_amount) AS avg_sanctioned,
               MIN(p.state) AS state
        FROM projects p
        LEFT JOIN project_rec_info r ON r.project_id = p.id
        LEFT JOIN risk_scores rs ON rs.project_id = p.id
        {"WHERE " + where if where else ""}
        GROUP BY 1
        ORDER BY projects DESC
    """, params)
    out: List[Dict[str, Any]] = []
    for r in rows:
        projects = int(_num(r[1]) or 0)
        sanc, exp = _num(r[6]), _num(r[5])
        out.append({
            "area": r[0],
            "district": r[0] if unit == "district" else None,
            "constituency": r[0] if unit == "constituency" else None,
            "state": r[10],
            "projects": projects,
            "avg_completion": round(_num(r[2]), 2) if _num(r[2]) is not None else None,
            "under_50": int(_num(r[3]) or 0),
            "under_50_pct": round((_num(r[3]) or 0) / projects * 100, 2) if projects else None,
            "spend_no_progress": int(_num(r[4]) or 0),
            "expenditure": exp,
            "sanctioned": sanc,
            "utilization_pct": round(exp / sanc * 100, 2) if exp is not None and sanc else None,
            "high_risk": int(_num(r[7]) or 0),
            "high_risk_pct": round((_num(r[7]) or 0) / projects * 100, 2) if projects else None,
            "anomalies": int(_num(r[8]) or 0),
            "avg_sanctioned": _num(r[9]),
            "small_sample": projects < MIN_AREA_PROJECTS,
        })
    return out


def _categories_by_area(db: Session, filters: Dict[str, Any], areas: List[str], unit: str) -> Dict[str, List[Dict[str, Any]]]:
    if not areas:
        return {}
    where, params = _scope(filters)
    expr = _area_expr(unit)
    placeholders = ", ".join(f":a{i}" for i in range(len(areas)))
    rows = _rows(db, f"""
        SELECT {expr} AS a,
               COALESCE(NULLIF(TRIM(p.project_type), ''), 'Unclassified') AS cat,
               COUNT(*) AS n
        FROM projects p
        {"WHERE " + where + " AND " if where else "WHERE "}
        {expr} IN ({placeholders})
        GROUP BY 1, 2 ORDER BY n DESC
    """, {**params, **{f"a{i}": v for i, v in enumerate(areas)}})
    out: Dict[str, List[Dict[str, Any]]] = {}
    for a, cat, n in rows:
        out.setdefault(a, []).append({"category": cat, "projects": int(_num(n) or 0)})
    return out


def _geo_disclosure(unit: Dict[str, Any], precision: str) -> List[str]:
    out = [
        "Area aggregates use the finest recorded geography; the project catalogue stores no "
        "project coordinates, so no project is placed on the map individually.",
        "Project counts come from the imported MPLADS dataset and may not cover every work on the ground.",
    ]
    if unit.get("fallback"):
        out.append(unit["explanation"])
    if precision == "constituency":
        out.append("Constituency is a parliamentary boundary, not an administrative district.")
    return out


# ══════════════════════════════════════════════════════════════════════
# FEATURE 14 — Recorded-project coverage gaps
# ══════════════════════════════════════════════════════════════════════

def coverage_gaps(db: Session, state: Optional[str] = None) -> Dict[str, Any]:
    return _cached(f"coverage|{state}", lambda: _build_coverage(db, state))


def _build_coverage(db: Session, state: Optional[str]) -> Dict[str, Any]:
    unit = geo_unit(db)
    u = unit["unit"]

    if not state:
        rows = _rows(db, """
            SELECT COALESCE(NULLIF(TRIM(p.state), ''), '(state not recorded)'),
                   COUNT(*), AVG(p.completion_percentage), SUM(p.sanctioned_amount)
            FROM projects p GROUP BY 1 ORDER BY 2 DESC
        """)
        states = [{"state": r[0], "projects": int(_num(r[1]) or 0),
                   "avg_completion": round(_num(r[2]), 2) if _num(r[2]) is not None else None,
                   "sanctioned": _num(r[3])} for r in rows]
        med = _median([float(s["projects"]) for s in states]) or 0
        for s in states:
            s["coverage_index"] = round(s["projects"] / med, 3) if med else None
        return {
            "available": True,
            "mode": "state-ranking",
            "precision": "state",
            "unit": "state",
            "unit_label": "State",
            "geo_unit": unit,
            "states": states,
            "median_projects_per_state": med,
            "notes": ["Select a state to see sub-state recorded-project coverage."],
            "limitations": _geo_disclosure(unit, "state"),
        }

    areas = _area_rows(db, {"state": state}, u)
    if not areas:
        return {"available": False, "reason": f"No projects are recorded for {state}.",
                "precision": u, "geo_unit": unit}

    named = [a for a in areas if a["area"] != f"({u} not recorded)"]
    counts = [float(a["projects"]) for a in named]
    med = _median(counts) or 0
    ordered = sorted(counts)
    p25 = ordered[max(0, int(len(ordered) * 0.25) - 1)] if ordered else None
    cats = _categories_by_area(db, {"state": state}, [a["area"] for a in named][:60], u)

    for a in areas:
        a["coverage_index"] = round(a["projects"] / med, 3) if med else None
        a["low_coverage"] = (a["area"] != f"({u} not recorded)"
                             and med > 0 and a["projects"] < (p25 or med * 0.5))
        if a["area"] in cats:
            top = cats[a["area"]][:3]
            a["top_categories"] = top
            a["category_concentration"] = (round(top[0]["projects"] / a["projects"] * 100, 1)
                                           if top and a["projects"] else None)

    missing = next((a for a in areas if a["area"] == f"({u} not recorded)"), None)
    return {
        "available": True,
        "mode": f"{u}-coverage",
        "state": state,
        "precision": u,
        "unit": u,
        "unit_label": unit["label"],
        "geo_unit": unit,
        "areas": areas,
        "districts": areas if u == "district" else [],
        "low_coverage_areas": [a for a in areas if a.get("low_coverage")],
        "median_projects_per_area": med,
        "p25_projects_per_area": p25,
        "projects_without_area": missing["projects"] if missing else 0,
        "notes": [
            f"Areas are compared at {unit['label'].lower()} level: an area is flagged when its "
            f"recorded project count falls below the 25th percentile of {u}s in the same state.",
            "A low count may reflect record-keeping or boundary differences, not an "
            "infrastructure shortage.",
            "No population or external infrastructure dataset exists in this system, so no "
            "per-capita coverage or infrastructure deficit is computed.",
        ],
        "limitations": _geo_disclosure(unit, u),
    }


# ══════════════════════════════════════════════════════════════════════
# FEATURE 16 — Sub-state execution patterns
# ══════════════════════════════════════════════════════════════════════

def district_patterns(db: Session, filters: Dict[str, Any]) -> Dict[str, Any]:
    return _cached(f"areas|{filters.get('state')}|{filters.get('house')}|{filters.get('category')}",
                   lambda: _build_area_patterns(db, filters))


def _build_area_patterns(db: Session, filters: Dict[str, Any]) -> Dict[str, Any]:
    unit = geo_unit(db)
    u = unit["unit"]
    rows = _area_rows(db, filters, u)
    if not rows:
        return {"available": False, "reason": "No projects match this scope.", "precision": u,
                "geo_unit": unit}

    valid = [r for r in rows if not r["small_sample"] and r["area"] != f"({u} not recorded)"]
    ranked = sorted(valid, key=lambda r: (r["avg_completion"] if r["avg_completion"] is not None else -1))
    for i, r in enumerate(ranked, 1):
        r["completion_rank"] = i
    by_util = sorted([r for r in valid if r["utilization_pct"] is not None],
                     key=lambda r: r["utilization_pct"], reverse=True)
    for i, r in enumerate(by_util, 1):
        r["utilization_rank"] = i
    for r in rows:
        r["interpretation"] = (
            f"{r['projects']} recorded projects — large enough for comparison."
            if not r["small_sample"] else
            f"Only {r['projects']} recorded project(s) — context only, not ranked.")

    return {
        "available": True,
        "precision": u,
        "unit": u,
        "unit_label": unit["label"],
        "geo_unit": unit,
        "state": filters.get("state"),
        "house": filters.get("house"),
        "category": filters.get("category"),
        "areas": rows,
        "districts": rows if u == "district" else [],
        "ranked_by_completion": ranked[:15],
        "ranked_by_utilization": by_util[:15],
        "state_medians": {
            "completion": _median([r["avg_completion"] for r in valid if r["avg_completion"] is not None]),
            "utilization": _median([r["utilization_pct"] for r in valid if r["utilization_pct"] is not None]),
        },
        "metric_definitions": {
            "avg_completion": "Mean recorded physical completion percentage of the area's projects.",
            "utilization_pct": "Sum of linked payments divided by sum of sanctioned amounts.",
            "under_50": "Projects with recorded completion below 50%.",
            "spend_no_progress": "Projects with linked payments and reported completion below 1%.",
            "high_risk_pct": "Share carrying the stored High/Critical risk level.",
        },
        "notes": [
            f"Aggregation unit: {unit['label']} ({unit['explanation']}).",
            "Raw project counts are not performance: portfolio size and category mix differ by area.",
            f"Areas with fewer than {MIN_AREA_PROJECTS} recorded projects are excluded from rankings.",
        ],
        "limitations": _geo_disclosure(unit, u),
    }


# ══════════════════════════════════════════════════════════════════════
# FEATURE 17 — Geospatial progress monitoring
# ══════════════════════════════════════════════════════════════════════

def progress_map(db: Session, filters: Dict[str, Any]) -> Dict[str, Any]:
    return _cached(f"progress|{filters.get('state')}|{filters.get('district')}|"
                   f"{filters.get('house')}|{filters.get('category')}",
                   lambda: _build_progress(db, filters))


def _build_progress(db: Session, filters: Dict[str, Any]) -> Dict[str, Any]:
    unit = geo_unit(db)
    u = unit["unit"]
    where, params = _scope(filters)

    if filters.get("state"):
        expr = _area_expr(u)
        rows = _rows(db, f"""
            SELECT {expr}, COUNT(*), AVG(p.completion_percentage),
                   SUM(r.linked_expenditure), SUM(p.sanctioned_amount),
                   SUM(CASE WHEN rs.risk_level IN ('High','Critical') THEN 1 ELSE 0 END),
                   SUM(CASE WHEN rs.ml_anomaly = 1 THEN 1 ELSE 0 END),
                   SUM(CASE WHEN p.completion_percentage IS NOT NULL
                             AND p.completion_percentage < 50 THEN 1 ELSE 0 END)
            FROM projects p
            LEFT JOIN project_rec_info r ON r.project_id = p.id
            LEFT JOIN risk_scores rs ON rs.project_id = p.id
            {"WHERE " + where if where else ""}
            GROUP BY 1 ORDER BY 1
        """, params)
        precision = u
    else:
        rows = _rows(db, f"""
            SELECT COALESCE(NULLIF(TRIM(p.state), ''), '(state not recorded)'), COUNT(*),
                   AVG(p.completion_percentage), SUM(r.linked_expenditure),
                   SUM(p.sanctioned_amount),
                   SUM(CASE WHEN rs.risk_level IN ('High','Critical') THEN 1 ELSE 0 END),
                   SUM(CASE WHEN rs.ml_anomaly = 1 THEN 1 ELSE 0 END),
                   SUM(CASE WHEN p.completion_percentage IS NOT NULL
                             AND p.completion_percentage < 50 THEN 1 ELSE 0 END)
            FROM projects p
            LEFT JOIN project_rec_info r ON r.project_id = p.id
            LEFT JOIN risk_scores rs ON rs.project_id = p.id
            {"WHERE " + where if where else ""}
            GROUP BY 1 ORDER BY 2 DESC
        """, params)
        precision = "state"

    areas = []
    for r in rows:
        n = int(_num(r[1]) or 0)
        exp, sanc = _num(r[3]), _num(r[4])
        areas.append({
            "area": r[0], "projects": n,
            "avg_completion": round(_num(r[2]), 2) if _num(r[2]) is not None else None,
            "expenditure": exp, "sanctioned": sanc,
            "utilization_pct": round(exp / sanc * 100, 2) if exp is not None and sanc else None,
            "high_risk": int(_num(r[5]) or 0),
            "high_risk_pct": round((_num(r[5]) or 0) / n * 100, 2) if n else None,
            "anomalies": int(_num(r[6]) or 0),
            "under_50": int(_num(r[7]) or 0),
        })

    gps = _rows(db, """
        SELECT v.project_id, v.lat, v.lon, v.status, v.created_at,
               p.project_name, p.state, p.constituency
        FROM verification_reports v LEFT JOIN projects p ON p.id = v.project_id
        WHERE v.lat IS NOT NULL AND v.lon IS NOT NULL
        ORDER BY v.id DESC LIMIT 500
    """)
    points = [{
        "project_id": int(r[0]), "lat": _num(r[1]), "lon": _num(r[2]),
        "verification_status": r[3], "reported_at": str(r[4])[:19],
        "project_name": r[5], "state": r[6], "constituency": r[7],
        "precision": "exact-gps",
    } for r in gps if _num(r[1]) is not None and _num(r[2]) is not None]

    return {
        "available": True,
        "precision": precision,
        "unit": precision if precision != "state" else "state",
        "unit_label": (unit["label"] if precision == u else "State"),
        "geo_unit": unit,
        "areas": areas,
        "gps_points": points,
        "gps_point_count": len(points),
        "filters": filters,
        "notes": [
            f"Area aggregates are mapped at {precision} level. The catalogue stores no project "
            "coordinates, so individual projects are not plotted from area data.",
            "Only field verification submissions carry GPS; those points are exact and shown "
            "separately, never inferred for other projects.",
        ],
        "limitations": _geo_disclosure(unit, precision) + [
            "Progress change across imports is unavailable until imports store per-project snapshots.",
        ],
    }


# ══════════════════════════════════════════════════════════════════════
# FEATURE 15 — Spatial project relationship analysis
# ══════════════════════════════════════════════════════════════════════

def spatial_relations(db: Session, filters: Dict[str, Any], limit: int = 20) -> Dict[str, Any]:
    return _cached(f"spatial|{filters.get('state')}|{filters.get('district')}|{limit}",
                   lambda: _build_spatial(db, filters, limit))


def _build_spatial(db: Session, filters: Dict[str, Any], limit: int) -> Dict[str, Any]:
    unit = geo_unit(db)
    u = unit["unit"]
    where, params = _scope(filters)

    dup = _rows(db, f"""
        SELECT LOWER(TRIM(p.project_name)) AS name, LOWER(TRIM(p.constituency)) AS cons,
               COUNT(*) AS n, MIN(p.id) AS a, MAX(p.id) AS b,
               MIN(p.state) AS st, MIN(p.sanctioned_amount) AS lo, MAX(p.sanctioned_amount) AS hi
        FROM projects p
        WHERE TRIM(COALESCE(p.project_name,'')) <> ''
        {"AND " + where if where else ""}
        GROUP BY 1, 2 HAVING COUNT(*) > 1
        ORDER BY n DESC LIMIT :lim
    """, {**params, "lim": limit})
    groups = [{
        "kind": "same-name-cluster",
        "name": r[0], "constituency": r[1], "state": r[5],
        "project_count": int(_num(r[2]) or 0),
        "sample_project_ids": [int(r[3]), int(r[4])],
        "sanction_range": {"min": _num(r[6]), "max": _num(r[7])},
        "precision": u,
        "explanation": ("Identical normalised project names in one constituency can be split "
                        "works, re-phased sanctions or duplicate records. A shared name is not "
                        "proof of duplication."),
    } for r in dup]

    shared = _rows(db, f"""
        SELECT r.work_key, COUNT(*) AS n, MIN(r.project_id) AS a, MAX(r.project_id) AS b,
               MIN(p.state) AS st
        FROM project_rec_info r JOIN projects p ON p.id = r.project_id
        WHERE TRIM(COALESCE(r.work_key,'')) <> ''
        {"AND " + where if where else ""}
        GROUP BY r.work_key HAVING COUNT(*) > 1
        ORDER BY n DESC LIMIT :lim
    """, {**params, "lim": limit})
    for r in shared:
        groups.append({
            "kind": "shared-linkage-key",
            "work_key": str(r[0])[:80],
            "project_count": int(_num(r[1]) or 0),
            "sample_project_ids": [int(r[2]), int(r[3])],
            "state": r[4],
            "precision": u,
            "explanation": ("Several catalogue rows share one work linkage key, so their payment "
                            "totals describe the same work key."),
        })

    areas = [a for a in _area_rows(db, filters, u) if a["area"] != f"({u} not recorded)"]
    concentration = sorted(areas, key=lambda a: -a["projects"])[:5]

    gps = _rows(db, """
        SELECT v.project_id, v.lat, v.lon, p.project_name, p.state, p.constituency
        FROM verification_reports v JOIN projects p ON p.id = v.project_id
        WHERE v.lat IS NOT NULL AND v.lon IS NOT NULL
        ORDER BY v.id DESC LIMIT 200
    """)
    pts = [(int(r[0]), _num(r[1]), _num(r[2]), r[3], r[4], r[5]) for r in gps
           if _num(r[1]) is not None and _num(r[2]) is not None]
    proximity: List[Dict[str, Any]] = []
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            a, b = pts[i], pts[j]
            if a[0] == b[0]:
                continue
            d = _haversine_m(a[1], a[2], b[1], b[2])
            if d <= 1000:
                proximity.append({
                    "a": {"project_id": a[0], "name": a[3], "lat": a[1], "lon": a[2]},
                    "b": {"project_id": b[0], "name": b[3], "lat": b[1], "lon": b[2]},
                    "distance_m": round(d, 1),
                    "precision": "exact-gps",
                    "explanation": ("Both projects have GPS-verified reports within 1 km. "
                                    "Proximity alone does not imply duplication."),
                })
    proximity = proximity[:20]

    return {
        "available": True,
        "precision": u,
        "unit": u,
        "unit_label": unit["label"],
        "geo_unit": unit,
        "coordinate_basis": ("exact GPS for verification-report points; "
                            f"{u} grouping for everything else"),
        "groups": groups,
        "concentration": concentration,
        "gps_proximity": proximity,
        "gps_points_available": len(pts),
        "distance_available": bool(proximity),
        "notes": [
            "Distance is computed only between projects that have GPS-verified reports.",
            "For all other projects the platform groups by constituency but cannot measure "
            "distance — no coordinates are invented.",
            "Nearby or similarly named projects are surfaced for human review, never merged.",
        ],
        "limitations": [
            f"Only {len(pts)} GPS-verified point(s) exist, so proximity coverage is minimal.",
            "Constituency boundaries are administrative groupings, not a spatial index.",
        ],
    }


# ══════════════════════════════════════════════════════════════════════
# FEATURE 18 — Geographic outlier detection
# ══════════════════════════════════════════════════════════════════════

def geo_outliers(db: Session, filters: Dict[str, Any]) -> Dict[str, Any]:
    return _cached(f"geoout|{filters.get('state')}|{filters.get('house')}|{filters.get('category')}",
                   lambda: _build_geo_outliers(db, filters))


def _build_geo_outliers(db: Session, filters: Dict[str, Any]) -> Dict[str, Any]:
    unit = geo_unit(db)
    u = unit["unit"]
    rows = [r for r in _area_rows(db, filters, u)
            if not r["small_sample"] and r["area"] != f"({u} not recorded)"]
    if len(rows) < 3:
        return {
            "available": False,
            "reason": (f"Only {len(rows)} {u}(s) have at least {MIN_AREA_PROJECTS} recorded projects "
                       "— too few for a geographic comparison."),
            "precision": u, "geo_unit": unit,
        }

    metrics = (
        ("utilization_pct", "Recorded utilization", "percentage points"),
        ("avg_completion", "Average recorded completion", "percentage points"),
        ("avg_sanctioned", "Average sanctioned amount", "₹"),
        ("high_risk_pct", "Share of High-risk projects", "percentage points"),
    )
    flagged: List[Dict[str, Any]] = []
    for key, label, unit_name in metrics:
        values = [r[key] for r in rows if r.get(key) is not None]
        med = _median(values)
        mad = _mad(values)
        if med is None or not mad:
            continue
        peers = sorted(values)
        for r in rows:
            v = r.get(key)
            if v is None:
                continue
            z = (v - med) / (1.4826 * mad)
            if abs(z) >= OUTLIER_Z:
                flagged.append({
                    "area": r["area"],
                    "district": r["area"] if u == "district" else None,
                    "constituency": r["area"] if u == "constituency" else None,
                    "metric": key, "label": label, "value": v, "unit": unit_name,
                    "state_median": med, "robust_z": round(z, 2),
                    "direction": "above" if z > 0 else "below",
                    "projects": r["projects"],
                    "peer_range": {"p10": peers[max(0, int(len(peers) * 0.1) - 1)],
                                   "p90": peers[min(len(peers) - 1, int(len(peers) * 0.9))]},
                    "explanation": (
                        f"{r['area']} records {label.lower()} of {v:.2f} against a state median "
                        f"of {med:.2f} across {len(rows)} comparable {u}s "
                        f"(robust z {z:.1f}, {r['projects']} projects). This is a statistical "
                        "outlier in recorded data, not evidence of irregularity."),
                    "precision": u,
                })

    areas = [r["area"] for r in rows][:60]
    cats = _categories_by_area(db, filters, areas, u)
    category_flags = []
    for a, items in cats.items():
        total = sum(i["projects"] for i in items)
        if total >= MIN_AREA_PROJECTS and items:
            share = items[0]["projects"] / total * 100
            if share >= 60:
                category_flags.append({
                    "area": a, "top_category": items[0]["category"],
                    "share_pct": round(share, 1), "projects": total,
                    "precision": u,
                    "explanation": (f"{round(share, 1)}% of the {total} recorded projects in {a} "
                                    f"fall under '{items[0]['category']}'. Category concentration "
                                    "is a portfolio-composition signal, not an anomaly by itself."),
                })
    category_flags.sort(key=lambda c: -c["share_pct"])

    return {
        "available": True,
        "precision": u,
        "unit": u,
        "unit_label": unit["label"],
        "geo_unit": unit,
        "method": (f"Robust z-score (median and median absolute deviation) across {u}s with at "
                   f"least {MIN_AREA_PROJECTS} recorded projects; flagged at |z| ≥ {OUTLIER_Z}."),
        "areas_compared": len(rows),
        "outliers": flagged[:20],
        "category_concentration": category_flags[:10],
        "notes": [
            "An outlier is a deviation in recorded values — not evidence of irregularity.",
            "Low project counts are excluded rather than flagged, because small portfolios are unstable.",
            "Change in geographic anomaly concentration over time requires stored import history, "
            "which is currently empty.",
        ],
        "limitations": _geo_disclosure(unit, u),
    }
