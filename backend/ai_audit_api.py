"""
AI Audit & Verification API (SIH 26102)
=======================================

Endpoints backing the AI Audit Command Center, forensic inspection modal,
vendor network map, and ground-truth verification portal.

Mounted from main.py via APIRouter — no existing routes are modified.

Memory discipline (512MB Render):
- every multi-row read is streamed (yield_per) or SQL-aggregated
- transient passes fold into small dicts/lists and are released
- the category benchmark index is lazily built ONCE, holds only
  (count, sum) tuples per (category, state) key, and is cleared by the
  existing clear_cache()/data-sync path
- images are hashed and released; only 16-char hex keys are stored
"""

import json
import os
import statistics
import threading
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

import ai_audit
import auth
import models
from auth import require_capability
from database import SessionLocal, normkey


def get_db():
    """Session dependency (mirrors main.get_db; kept local to avoid a
    circular import since main.py imports this router)."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

router = APIRouter(prefix="/ai", tags=["AI Audit"])

_bearer = HTTPBearer(auto_error=False)


def _optional_user(credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
                   db: Session = Depends(get_db)) -> Optional[models.User]:
    """Best-effort identity: attaches the signed-in user when a valid bearer
    token is present, else None. Used to attribute submissions without
    forcing sign-in for legacy anonymous use."""
    if credentials is None or not credentials.credentials:
        return None
    uid = auth._decode_token(credentials.credentials)
    if uid is None:
        return None
    return db.query(models.User).filter(models.User.id == uid, models.User.is_active.is_(True)).first()

# Category benchmark index: {category: {state: (count, sum)}} — tiny.
_bench_lock = threading.Lock()
_bench_index: Optional[Dict[str, Dict[str, tuple]]] = None


# ────────────────────────────────────────────────────────────────────
# Category cost benchmark index (lazy, compact, shared)
# ────────────────────────────────────────────────────────────────────

def _benchmark_index() -> Dict[str, Dict[str, tuple]]:
    global _bench_index
    if _bench_index is not None:
        return _bench_index
    with _bench_lock:
        if _bench_index is not None:
            return _bench_index
        idx: Dict[str, Dict[str, tuple]] = {}
        db = SessionLocal()
        try:
            rows = (
                db.query(
                    models.Project.project_type,
                    models.Project.state,
                    models.Project.sanctioned_amount,
                )
                .filter(
                    models.Project.sanctioned_amount.isnot(None),
                    models.Project.sanctioned_amount > 0,
                )
                .yield_per(500)
            )
            for ptype, state, amount in rows:
                cat = ai_audit.norm_category(ptype)
                state_key = str(state or "").strip() or "All India"
                bucket = idx.setdefault(cat, {})
                count, total = bucket.get(state_key, (0, 0.0))
                bucket[state_key] = (count + 1, total + float(amount))
        finally:
            db.close()
        _bench_index = idx
        return idx


def invalidate_benchmark_index() -> None:
    """Called by the data-sync/clear-cache path when the dataset changes."""
    global _bench_index
    with _bench_lock:
        _bench_index = None


def _benchmark_for(category: str, state: Optional[str]) -> Dict:
    idx = _benchmark_index()
    cat_idx = idx.get(category, {})
    stats_all = cat_idx.get("All India", (0, 0.0))
    stats_state = cat_idx.get(str(state or "").strip(), (0, 0.0))
    n_state, sum_state = stats_state
    n_all, sum_all = stats_all
    if n_state:
        avg = sum_state / n_state
        n, avg_used, basis = n_state, avg, f"state ({n_state} comparable projects)"
    elif n_all:
        avg = sum_all / n_all
        n, avg_used, basis = n_all, avg, f"national ({n_all} comparable projects)"
    else:
        return {"available": False, "reason": "No comparable projects in the dataset for this category."}
    return {
        "available": True,
        "category": category,
        "average": round(avg_used, 0),
        "sample_size": n,
        "basis": basis,
    }


# ────────────────────────────────────────────────────────────────────
# 1. Audit queue — projects ranked by audit risk index (0-100)
# ────────────────────────────────────────────────────────────────────

# Deterministic, explainable flag derivation from recorded fields only.
_FLAG_WEIGHTS = {
    "cost_outlier": 30,
    "overspend": 25,
    "stalled_progress": 25,
    "zero_progress_high_spend": 30,
    "duplicate_photo": 35,
    "gps_mismatch": 20,
    "verification_dispute": 20,
}


def _linked_exp_for(db, project) -> float:
    """Authoritative per-project spend from the payment ledger
    (project_rec_info.linked_expenditure). The catalog column is a zeroed
    legacy stamp."""
    try:
        row = (
            db.query(models.ProjectRecInfo)
            .filter(models.ProjectRecInfo.project_id == project.id)
            .first()
        )
        return float(row.linked_expenditure or 0.0) if row is not None else 0.0
    except Exception:
        return 0.0


def _derive_flags(project, risk_row, extra: Dict) -> List[Dict]:
    """Explainable AI flags with numeric evidence from recorded data."""
    flags: List[Dict] = []
    sanctioned = float(project.sanctioned_amount or 0)
    spent = float(extra.get("linked_expenditure") or 0.0)
    completion = float(project.completion_percentage or 0)

    bench = extra.get("benchmark") or {}
    if bench.get("available") and sanctioned > 0:
        ratio = sanctioned / bench["average"] if bench["average"] else 0
        if ratio >= 2.0:
            flags.append({
                "type": "cost_outlier",
                "label": "Cost Outlier",
                "detail": f"Sanctioned amount is {ratio:.1f}x the {bench['basis']} average for this category.",
                "evidence": {"ratio": round(ratio, 2), "benchmark": bench["average"]},
            })
    if sanctioned > 0 and spent > sanctioned:
        flags.append({
            "type": "overspend",
            "label": "Overspend",
            "detail": f"Expenditure \u20b9{spent:,.0f} exceeds sanctioned \u20b9{sanctioned:,.0f}.",
            "evidence": {"spent": spent, "sanctioned": sanctioned},
        })
    if completion <= 0 and spent > 0:
        flags.append({
            "type": "zero_progress_high_spend",
            "label": "Stalled / Zero Progress",
            "detail": f"\u20b9{spent:,.0f} recorded with 0% physical progress.",
            "evidence": {"spent": spent, "completion": 0},
        })
    elif completion < 25 and spent > 0 and sanctioned > 1_000_000:
        flags.append({
            "type": "stalled_progress",
            "label": "Delayed SLA",
            "detail": f"Only {completion:.0f}% complete despite \u20b9{spent:,.0f} spent.",
            "evidence": {"completion": completion, "spent": spent},
        })
    if extra.get("duplicate_photo_count"):
        flags.append({
            "type": "duplicate_photo",
            "label": "Duplicate Photo Fraud",
            "detail": f"{extra['duplicate_photo_count']} near-duplicate photo submission(s) detected (dHash similarity \u2265 95%).",
            "evidence": {"duplicates": extra["duplicate_photo_count"]},
        })
    if extra.get("gps_mismatch_count"):
        flags.append({
            "type": "gps_mismatch",
            "label": "GPS Mismatch",
            "detail": f"{extra['gps_mismatch_count']} field photo(s) captured >500m from the recorded site.",
            "evidence": {"mismatches": extra["gps_mismatch_count"]},
        })
    if extra.get("verification_nonfunctional"):
        flags.append({
            "type": "verification_dispute",
            "label": "Ground Report Dispute",
            "detail": f"{extra['verification_nonfunctional']} field verification(s) report the work as non-functional / not started.",
            "evidence": {"reports": extra["verification_nonfunctional"]},
        })
    return flags


def _risk_index(flags: List[Dict], ml_score_row) -> int:
    score = sum(_FLAG_WEIGHTS.get(f["type"], 10) for f in flags)
    if ml_score_row is not None:
        # Blend the existing ML risk (already 0-100, audited pipeline) at 40%.
        base = int(ml_score_row.risk_score or 0)
        score = round(0.6 * min(score, 100) + 0.4 * base)
    return int(min(100, max(0, score)))


def _project_extra_context(db: Session, project_ids: List[int]) -> Dict[int, Dict]:
    """One streamed pass per table for the candidate projects only."""
    extra: Dict[int, Dict] = {
        pid: {"duplicate_photo_count": 0, "gps_mismatch_count": 0,
              "verification_nonfunctional": 0, "benchmark": None}
        for pid in project_ids
    }
    if not project_ids:
        return extra
    id_set = set(project_ids)

    # Cross-project duplicate detection: one streamed pass over all stored
    # hashes; a hash seen under ≥2 distinct projects means the same photo
    # was submitted for different works. Compact: dhash -> set(project_ids).
    hash_projects: Dict[str, set] = {}
    for h, pid in db.query(models.PhotoHash.dhash, models.PhotoHash.project_id).yield_per(200):
        hash_projects.setdefault(h, set()).add(pid)
    shared_counts: Dict[int, int] = {}
    for h, pids in hash_projects.items():
        if len(pids) > 1:
            for pid in pids:
                shared_counts[pid] = shared_counts.get(pid, 0) + 1
    for pid, cnt in shared_counts.items():
        if pid in extra:
            extra[pid]["duplicate_photo_count"] = cnt

    # verification reports per project
    rows = (
        db.query(
            models.VerificationReport.project_id,
            models.VerificationReport.status,
            func.count(models.VerificationReport.id),
        )
        .filter(models.VerificationReport.project_id.in_(id_set))
        .group_by(models.VerificationReport.project_id, models.VerificationReport.status)
        .all()
    )
    for pid, status, cnt in rows:
        if pid in extra and str(status).lower() != "functional":
            extra[pid]["verification_nonfunctional"] += cnt

    return extra


@router.get("/audit-queue")
def audit_queue(
    flag: Optional[str] = Query(None, description="Filter pill: photo_fraud|cost_outlier|gps_mismatch|delayed"),
    state: Optional[str] = None,
    fy: Optional[str] = None,
    skip: int = Query(0, ge=0),
    limit: int = Query(25, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """Projects sorted by audit risk index (0-100, descending), with flags."""
    # Candidate set: highest-ML-risk first — the queue is inherently a
    # prioritized slice, never a full-table Python sort of 83k rows.
    base_q = (
        db.query(models.RiskScore, models.Project)
        .join(models.Project, models.RiskScore.project_id == models.Project.id)
    )
    if state:
        base_q = base_q.filter(models.Project.state == state)
    if fy:
        base_q = base_q.filter(models.Project.fy == fy)
    total = base_q.count()
    rows = (
        base_q.order_by(models.RiskScore.risk_score.desc())
        .offset(skip)
        .limit(min(limit * 4, 800))  # superset; flag-filters shrink it
        .all()
    )

    project_rows = []
    project_ids = []
    for risk_row, project in rows:
        project_ids.append(project.id)
        project_rows.append((risk_row, project))
    extra_map = _project_extra_context(db, project_ids)

    items = []
    for risk_row, project in project_rows:
        extra = extra_map.get(project.id, {})
        bench = _benchmark_for(ai_audit.norm_category(project.project_type), project.state)
        extra["benchmark"] = bench
        extra["linked_expenditure"] = _linked_exp_for(db, project)
        flags = _derive_flags(project, risk_row, extra)
        score = _risk_index(flags, risk_row)
        items.append({
            "project_id": project.id,
            "title": project.project_name,
            "district": project.district,
            "state": project.state,
            "constituency": project.constituency,
            "sanctioned_amount": project.sanctioned_amount or 0,
            "expenditure": extra["linked_expenditure"],
            "completion_percentage": project.completion_percentage or 0,
            "risk_index": score,
            "severity": ai_audit.risk_band(score)[0],
            "primary_flag": flags[0]["label"] if flags else ("ML risk signal" if risk_row and risk_row.risk_score and risk_row.risk_score >= 60 else "None"),
            "flags": [{"type": f["type"], "label": f["label"]} for f in flags],
            "ml_risk_score": int(risk_row.risk_score or 0) if risk_row else 0,
        })

    if flag:
        fmap = {
            "photo_fraud": "duplicate_photo",
            "cost_outlier": "cost_outlier",
            "gps_mismatch": "gps_mismatch",
            "delayed": ("stalled_progress", "zero_progress_high_spend"),
        }
        wanted = fmap.get(flag)
        if wanted:
            wanted_set = {wanted} if isinstance(wanted, str) else set(wanted)
            items = [i for i in items if any(f["type"] in wanted_set for f in i["flags"])]

    items.sort(key=lambda x: x["risk_index"], reverse=True)
    return {
        "total_candidates": total,
        "returned": len(items),
        "skip": skip,
        "limit": limit,
        "items": items[:limit],
    }


# ────────────────────────────────────────────────────────────────────
# 2. Image verification — dHash duplicate check + EXIF GPS audit
# ────────────────────────────────────────────────────────────────────

@router.post("/verify-image")
async def verify_image(
    file: UploadFile = File(...),
    project_id: Optional[int] = Query(None, description="Project this photo claims to represent"),
    store: bool = Query(True, description="Store the hash for future duplicate checks"),
    db: Session = Depends(get_db),
):
    """Perceptual-hash duplicate check + EXIF GPS extraction for a photo."""
    raw = await file.read()
    if not raw:
        raise HTTPException(400, "Empty file")
    if len(raw) > 12 * 1024 * 1024:
        raise HTTPException(413, "Image too large (max 12MB)")

    dhash = ai_audit.dhash64(raw)
    if not dhash:
        raise HTTPException(400, "Could not decode image (unsupported or corrupt file)")
    exif = ai_audit.extract_exif_gps(raw)
    del raw  # release the upload buffer immediately

    # Duplicate scan: single streamed pass over stored hashes.
    matches: List[Dict] = []
    stored = (
        db.query(
            models.PhotoHash.id,
            models.PhotoHash.project_id,
            models.PhotoHash.dhash,
            models.PhotoHash.source,
            models.PhotoHash.created_at,
        )
        .yield_per(200)
    )
    for hid, pid, stored_hash, source, created in stored:
        if project_id and pid == project_id:
            continue  # same project's own photos are not "fraud" matches
        distance = ai_audit.hamming_hex(dhash, stored_hash)
        if distance <= 6:  # ≤6/64 bits ⇒ near-duplicate
            match_project = (
                db.query(models.Project.project_name, models.Project.district)
                .filter(models.Project.id == pid)
                .first()
            )
            matches.append({
                "photo_hash_id": hid,
                "project_id": pid,
                "project_name": match_project.project_name if match_project else None,
                "district": match_project.district if match_project else None,
                "similarity_percent": ai_audit.similarity_from_distance(distance),
                "source": source,
                "uploaded_at": created,
            })
    matches.sort(key=lambda m: m["similarity_percent"], reverse=True)
    matches = matches[:5]

    stored_id = None
    if store:
        row = models.PhotoHash(
            project_id=project_id or 0,
            dhash=dhash,
            source="upload",
            created_at=datetime.utcnow().isoformat(),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        stored_id = row.id

    # GPS deviation vs recorded site coordinate (if the project has one)
    gps_check = {"available": False}
    if project_id and exif["has_gps"]:
        site = (
            db.query(models.Project.state, models.Project.district)
            .filter(models.Project.id == project_id)
            .first()
        )
        # projects table has no lat/lon columns; distance is computed when a
        # site coordinate was captured with a previous field verification.
        site_fix = (
            db.query(models.VerificationReport.lat, models.VerificationReport.lon)
            .filter(
                models.VerificationReport.project_id == project_id,
                models.VerificationReport.lat.isnot(None),
            )
            .order_by(models.VerificationReport.created_at.desc())
            .first()
        )
        if site_fix and site_fix.lat is not None:
            dist = ai_audit.haversine_m(exif["lat"], exif["lon"], site_fix.lat, site_fix.lon)
            gps_check = {
                "available": True,
                "photo_lat": exif["lat"], "photo_lon": exif["lon"],
                "reference_lat": site_fix.lat, "reference_lon": site_fix.lon,
                "distance_m": dist,
                "deviation_flag": bool(dist and dist > 500),
                "note": "Distance from the project's last field-verified GPS position.",
            }

    return {
        "dhash": dhash,
        "stored_id": stored_id,
        "exif": exif,
        "duplicate_matches": matches,
        "duplicate_found": bool(matches),
        "gps_check": gps_check,
        "verdict": {
            "duplicate_photo_fraud": bool(matches and matches[0]["similarity_percent"] >= 97),
            "exif_stripped": exif["stripped"],
            "gps_deviation_over_500m": bool(gps_check.get("deviation_flag")),
        },
    }


# ────────────────────────────────────────────────────────────────────
# 3. Cost anomaly detection
# ────────────────────────────────────────────────────────────────────

@router.get("/detect-cost-anomaly")
def detect_cost_anomaly(
    project_id: Optional[int] = None,
    proposed_amount: Optional[float] = None,
    project_type: Optional[str] = None,
    state: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Compare a proposed/sanctioned amount against category benchmarks.

    Either pass project_id (uses the project's sanctioned amount + type +
    state) or explicit proposed_amount + project_type (+ optional state).
    Returns a z-score-style multiple, percentile, and risk classification.
    """
    if project_id:
        row = db.query(models.Project).filter(models.Project.id == project_id).first()
        if not row:
            raise HTTPException(404, f"Project {project_id} not found")
        amount = float(row.sanctioned_amount or 0)
        ptype, state = row.project_type, row.state
        title = row.project_name
    elif proposed_amount is not None and project_type:
        amount = float(proposed_amount)
        ptype, state, title = project_type, state, None
    else:
        raise HTTPException(400, "Provide project_id or proposed_amount + project_type")

    if amount <= 0:
        raise HTTPException(400, "Amount must be positive")

    category = ai_audit.norm_category(ptype)
    bench = _benchmark_for(category, state)
    if not bench.get("available"):
        return {
            "project_id": project_id, "title": title,
            "category": category, "available": False,
            "reason": bench["reason"],
        }

    avg = bench["average"]
    ratio = amount / avg if avg else 0

    # Percentile of this amount within the category (state first, then
    # national) — computed from the compact (count, sum) index via a
    # normal approximation; exact per-row percentiles would need the full
    # distribution, which we deliberately don't keep in RAM.
    import math as _math
    idx = _benchmark_index()
    cat_idx = idx.get(category, {})
    n_state, sum_state = cat_idx.get(str(state or "").strip(), (0, 0.0))
    n_all, sum_all = cat_idx.get("All India", (0, 0.0))
    n, total = (n_state, sum_state) if n_state else (n_all, sum_all)
    if n > 1 and avg and total:
        # derive variance proxy from the multiplicative spread assumption;
        # documented as an estimate, not a measured stddev
        approx_std = max(avg * 0.85, 50_000)
        z = (amount - avg) / approx_std
        percentile = round(50 * (1 + _math.erf(z / _math.sqrt(2))) * 100) / 100
        percentile = min(99.9, max(0.1, percentile))
    else:
        z, percentile = 0.0, 50.0

    if ratio >= 2.5:
        classification, severity = "Severe Cost Outlier", "red"
    elif ratio >= 1.75:
        classification, severity = "High Cost Anomaly", "red"
    elif ratio >= 1.3:
        classification, severity = "Above Regional Norm", "yellow"
    elif ratio >= 0.7:
        classification, severity = "Within Normal Range", "green"
    else:
        classification, severity = "Below Regional Norm", "yellow"

    return {
        "project_id": project_id,
        "title": title,
        "category": category,
        "proposed_amount": amount,
        "regional_average": avg,
        "benchmark_basis": bench["basis"],
        "sample_size": bench["sample_size"],
        "ratio": round(ratio, 2),
        "z_score": round(z, 2),
        "percentile": percentile,
        "classification": classification,
        "severity": severity,
        "explanation": (
            f"Budget is {ratio:.1f}x the {bench['basis']} average "
            f"(\u20b9{amount:,.0f} vs \u20b9{avg:,.0f})."
        ),
        "method": "Category benchmark ratio + normal-approximation percentile (estimated spread). Isolation Forest results remain in the existing risk pipeline.",
    }


# ────────────────────────────────────────────────────────────────────
# 4. Vendor collusion / network map
# ────────────────────────────────────────────────────────────────────

@router.get("/vendor-network")
def vendor_network(
    min_cluster: int = Query(2, ge=2, le=20),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """Vendor entities + suspicious shared-identity clusters.

    The dataset has no PAN/bank columns, so collusion signals are derived
    from what IS recorded: vendors sharing the same registered office
    address (IDA field), and vendor-name families (same normalized base
    name across states/constituencies) that would otherwise dilute
    accountability. Every signal is labeled with its evidence.

    Memory architecture (512MB budget): ALL heavy grouping happens in SQL
    (GROUP BY vendor / ida+family), so Python only ever holds the compact
    per-vendor aggregates (~26k small tuples) — never the 106k expenditure
    rows. The full-detail pass (states lists, raw-name variants) runs as a
    SECOND streamed pass restricted to just the top-`limit` vendors and
    cluster members, so request cost is bounded by the page size.
    """
    from sqlalchemy import func as _func

    # ── 1. SQL-side per-vendor aggregation (one grouped scan in SQLite) ──
    vendor_rows = (
        db.query(
            _func.lower(_func.trim(models.Expenditure.vendor)).label("vkey"),
            _func.min(models.Expenditure.vendor).label("vname"),
            _func.count(models.Expenditure.id).label("contracts"),
            _func.coalesce(_func.sum(models.Expenditure.expenditure_amount), 0.0).label("total"),
        )
        .filter(models.Expenditure.vendor.isnot(None), models.Expenditure.vendor != "")
        .group_by("vkey")
        .all()
    )
    vendor_stats: Dict[str, Dict] = {}
    for vkey, vname, contracts, total in vendor_rows:
        vendor_stats[vkey] = {
            "vendor_name": vname,
            "raw_variant_count": 0,  # filled in pass 2, only for selected vendors
            "contracts": int(contracts),
            "total_value": float(total),
        }
    # release the grouped result immediately
    del vendor_rows

    # ── 2. SQL-side cluster candidate discovery: (ida, vendor) pairs ──
    # A cluster = vendors sharing the same IDA whose names share the same
    # first-two-word family. The family key is computed in Python below to
    # avoid the SQLite "aggregate functions are not allowed in the GROUP BY clause"
    # error that arises when using a complex expr (_fam = trim(substr(...))) in
    # GROUP BY alongside HAVING count(distinct ...).
    # We fetch (ida, vendor) pairs in SQL, then group by (ida, family) in Python.
    vendor_ida_rows = (
        db.query(
            _func.lower(_func.trim(models.Expenditure.ida)).label("ida_key"),
            _func.lower(_func.trim(models.Expenditure.vendor)).label("vkey"),
        )
        .filter(
            models.Expenditure.ida.isnot(None),
            models.Expenditure.ida != "",
            models.Expenditure.vendor.isnot(None),
            models.Expenditure.vendor != "",
            ~_func.lower(_func.trim(models.Expenditure.ida)).notin_(["", "na", "n/a", "-", "not available", "nil"]),
        )
        .all()
    )

    # Compute the "first-two-words" family key for each vendor in Python.
    # This is equivalent to the original SQL _fam = trim(substr(vendor, 1, instr(vendor, ' ', 1) + instr(substr(vendor, instr(vendor, ' ', 1) + 1), ' '))) 
    # but uses Python str.split() which is reliable and avoids the GROUP BY issue.
    def _family_key(vendor_name: str) -> str:
        parts = str(vendor_name or "").strip().lower().split()
        return " ".join(parts[:2]) if len(parts) >= 2 else str(vendor_name or "").strip().lower()

    family_groups: Dict[Tuple[str, str], int] = {}
    for ida_key, vkey in vendor_ida_rows:
        fam = _family_key(vkey)
        key = (ida_key, fam)
        family_groups[key] = family_groups.get(key, 0) + 1

    # Filter groups that meet the minimum cluster size threshold.
    candidates: List[Dict[str, Any]] = []
    for (ida_key, fam), members in family_groups.items():
        if members < min_cluster:
            continue
        total = vendor_stats.get(ida_key, {}).get("total_value", 0.0)
        risk = min(100, 45 + 12 * members + (10 if total > 10_000_000 else 0))
        candidates.append({
            "ida": str(ida_key).title(),
            "name_family": str(fam).strip(),
            "member_count": members,
            "combined_value": round(total, 0),
            "risk_score": risk,
        })
    candidates.sort(key=lambda c: c["risk_score"], reverse=True)
    top_candidates = candidates[:limit]
    cluster_count = len(candidates)
    del candidates

    # ── 3. Resolve cluster membership: one narrow streamed pass fetching
    # (ida, vendor) pairs only for the selected candidates' IDAs. Members
    # are then filtered to the candidate's family in Python — a tiny set.
    wanted_idas = {c["ida"].lower() for c in top_candidates}
    ida_members: Dict[str, set] = {}
    if wanted_idas:
        # Filter in SQL instead of streaming every payment row and discarding
        # it in Python. When cluster discovery yields no candidates (the
        # common case on this dataset) the pass is skipped entirely; when it
        # does, SQLite can serve the predicate from idx_exp_ida_lower.
        pairs = (
            db.query(
                _func.lower(_func.trim(models.Expenditure.ida)),
                _func.lower(_func.trim(models.Expenditure.vendor)),
            )
            .filter(
                models.Expenditure.ida.isnot(None),
                models.Expenditure.vendor.isnot(None),
                models.Expenditure.vendor != "",
                _func.lower(_func.trim(models.Expenditure.ida)).in_(wanted_idas),
            )
            .yield_per(300)
        )
        for ida_key, vkey in pairs:
            ida_members.setdefault(ida_key, set()).add(vkey)
        del pairs

    clusters = []
    for idx, cand in enumerate(top_candidates):
        ida_key = cand["ida"].lower()
        members_all = ida_members.get(ida_key, set())
        members = sorted(m for m in members_all if m.startswith(cand["name_family"].lower()))
        if len(members) < min_cluster:
            continue
        clusters.append({
            "cluster_id": idx + 1,
            "signal": "shared_registered_office_and_name",
            "ida": cand["ida"],
            "name_family": cand["name_family"],
            "members": [vendor_stats[m]["vendor_name"] for m in members if m in vendor_stats][:20],
            "member_count": len(members),
            "combined_works": 0,  # works detail no longer materialized (memory)
            "combined_value": cand["combined_value"],
            "states": [],
            "risk_score": cand["risk_score"],
        })
    del ida_members

    # ── 4. Vendor risk table: rank the compact aggregates (no detail pass
    # needed — flags derive from counts/values already in memory).
    clustered_keys = set()
    for c in clusters:
        for m in c["members"]:
            clustered_keys.add(m.lower())
    vendors_out = []
    for key, v in vendor_stats.items():
        flags = []
        in_cluster = key in clustered_keys
        if in_cluster:
            flags.append("Clustered identity")
        if v["contracts"] >= 20:
            flags.append("High contract count")
        if v.get("raw_variant_count", 0) > 1:
            flags.append("Name variants")
        score = min(100,
                    20
                    + (30 if in_cluster else 0)
                    + (15 if v["contracts"] >= 20 else (5 if v["contracts"] >= 8 else 0))
                    + (10 if v.get("raw_variant_count", 0) > 1 else 0)
                    + (10 if v["total_value"] > 5_000_000 else 0))
        vendors_out.append({
            "vendor_name": v["vendor_name"],
            # Normalized key so the network table can open the shared vendor
            # intelligence panel (same key the /vendor-intelligence routes use).
            "vendor_key": normkey(v["vendor_name"]),
            "raw_names": [],  # variants detail dropped from list view (memory)
            "active_contracts": v["contracts"],
            "total_value": round(v["total_value"], 0),
            "distinct_works": 0,  # per-vendor distinct works no longer materialized
            "states": [],
            "conflict_flags": flags,
            "risk_score": score,
        })
    del vendor_stats
    vendors_out.sort(key=lambda x: (x["risk_score"], x["total_value"]), reverse=True)

    return {
        "total_vendors": len(vendors_out),
        "cluster_count": cluster_count,
        "clusters": clusters,
        "vendors": vendors_out[:limit],
        "method": (
            "Collusion signals derive from recorded fields: shared implementing-agency "
            "(registered office proxy) + shared name family, contract concentration, and "
            "name variants. The dataset contains no PAN/bank-account columns; if those "
            "become available the same clustering logic extends directly."
        ),
    }


# ────────────────────────────────────────────────────────────────────
# 5. Ground-truth verification portal
# ────────────────────────────────────────────────────────────────────

@router.get("/verify/project/{project_id}")
def verify_lookup(project_id: int, db: Session = Depends(get_db)):
    """Public project snapshot for the /verify portal."""
    row = (
        db.query(
            models.Project.id, models.Project.project_name, models.Project.state,
            models.Project.district, models.Project.constituency,
            models.Project.project_type, models.Project.sanctioned_amount,
            models.Project.completion_percentage, models.Project.status,
        )
        .filter(models.Project.id == project_id)
        .first()
    )
    if not row:
        raise HTTPException(404, "Project ID not found")
    recent = (
        db.query(models.VerificationReport)
        .filter(models.VerificationReport.project_id == project_id)
        .order_by(models.VerificationReport.created_at.desc())
        .limit(10)
        .all()
    )
    return {
        "project": {
            "id": row.id, "project_name": row.project_name,
            "state": row.state, "district": row.district,
            "constituency": row.constituency, "project_type": row.project_type,
            "sanctioned_amount": row.sanctioned_amount or 0,
            "completion_percentage": row.completion_percentage or 0,
            "status": row.status,
        },
        "recent_reports": [
            {
                "status": r.status, "reporter": r.reporter_name,
                "distance_m": r.distance_m, "note": (r.note or "")[:200],
                "created_at": r.created_at,
            }
            for r in recent
        ],
    }


@router.post("/verify/report")
async def verify_report(
    project_id: int = Query(...),
    status: str = Query(..., description="Functional | Non-Functional | Work Not Started"),
    lat: Optional[float] = Query(None),
    lon: Optional[float] = Query(None),
    reporter_name: Optional[str] = Query(None),
    note: Optional[str] = Query(None),
    inquiry_id: Optional[int] = Query(None, description="Link this submission to an auditor inquiry"),
    file: Optional[UploadFile] = File(None),
    user: Optional[models.User] = Depends(_optional_user),
    db: Session = Depends(get_db),
):
    """Submit a live field verification (photo + browser GPS + 1-click status).

    Signed-in Field Verifiers / Auditors / Admins get full attribution
    (user_id + role). Anonymous submissions remain possible for the legacy
    public portal but are flagged as unattributed.
    """
    status_clean = status.strip()
    if status_clean not in ("Functional", "Non-Functional", "Work Not Started"):
        raise HTTPException(400, "status must be Functional, Non-Functional, or Work Not Started")
    project = db.query(models.Project.id).filter(models.Project.id == project_id).first()
    if not project:
        raise HTTPException(404, f"Project {project_id} not found")

    # Enquiry linkage: a field verifier responding to an inquiry must target
    # an inquiry that actually exists and is still open.
    inquiry_row = None
    if inquiry_id is not None:
        inquiry_row = db.query(models.Inquiry).filter(models.Inquiry.id == inquiry_id).first()
        if not inquiry_row:
            raise HTTPException(404, f"Inquiry {inquiry_id} not found")
        if inquiry_row.project_id != project_id:
            raise HTTPException(400, "This inquiry belongs to a different project")
        if inquiry_row.status != "open":
            raise HTTPException(400, "This inquiry has already been responded to or closed")

    photo_hash_id = None
    exif = None
    gps_source = "browser" if (lat is not None and lon is not None) else None
    if file is not None:
        raw = await file.read()
        if raw:
            dhash = ai_audit.dhash64(raw)
            exif = ai_audit.extract_exif_gps(raw)
            if dhash:
                row = models.PhotoHash(
                    project_id=project_id,
                    dhash=dhash,
                    source="field_verify",
                    created_at=datetime.utcnow().isoformat(),
                )
                db.add(row)
                db.flush()
                photo_hash_id = row.id
            # If the browser GPS was unavailable but the photo has EXIF GPS, use it.
            if gps_source is None and exif and exif["has_gps"]:
                lat, lon = exif["lat"], exif["lon"]
                gps_source = "exif"
        del raw

    distance_m = None
    if lat is not None and lon is not None:
        # reference: last field-verified position for this project (if any)
        site_fix = (
            db.query(models.VerificationReport.lat, models.VerificationReport.lon)
            .filter(
                models.VerificationReport.project_id == project_id,
                models.VerificationReport.lat.isnot(None),
            )
            .order_by(models.VerificationReport.created_at.desc())
            .first()
        )
        if site_fix and site_fix.lat is not None:
            distance_m = ai_audit.haversine_m(lat, lon, site_fix.lat, site_fix.lon)

    # Identity attribution: signed-in users always take precedence over a
    # self-typed name; anonymous submissions keep the legacy name field.
    if user is not None:
        attrib_name = user.name[:120]
        attrib_role = user.role
        attrib_uid = user.id
    else:
        attrib_name = (reporter_name or "Anonymous")[:120]
        attrib_role = "public"
        attrib_uid = None

    # Citizen submissions are a distinct evidence class — labelled and kept
    # pending until a field verifier dispositions them. Verifier/auditor
    # submissions remain trusted 'field' reports as before.
    submission_kind = "citizen" if (attrib_role in ("citizen", "public")) else "field"

    report = models.VerificationReport(
        project_id=project_id,
        reporter_name=attrib_name,
        status=status_clean,
        lat=lat, lon=lon,
        distance_m=distance_m,
        gps_source=gps_source or "none",
        photo_hash_id=photo_hash_id,
        note=(note or "")[:1000],
        created_at=datetime.utcnow().isoformat(),
        user_id=attrib_uid,
        user_role=attrib_role,
        inquiry_id=inquiry_id,
        submission_kind=submission_kind,
        review_status=("pending" if submission_kind == "citizen" else "verified"),
    )
    db.add(report)
    db.flush()
    db.add(models.EvidenceEvent(
        report_id=report.id,
        event="submitted" if submission_kind == "citizen" else "submitted_field",
        actor_user_id=attrib_uid,
        actor_name=attrib_name,
        actor_role=attrib_role,
        note=(note or "")[:300],
        created_at=datetime.utcnow().isoformat(),
    ))

    # A signed-in response to an inquiry advances the inquiry workflow.
    if inquiry_row is not None and user is not None:
        inquiry_row.status = "responded"
        inquiry_row.response_text = (note or "")[:2000] or status_clean
        inquiry_row.responded_by_user_id = user.id
        inquiry_row.responded_by_name = user.name[:120]
        inquiry_row.responded_at = datetime.utcnow().isoformat()

    db.commit()
    db.refresh(report)

    return {
        "report_id": report.id,
        "project_id": project_id,
        "status_recorded": status_clean,
        "reported_by": attrib_name,
        "reported_by_role": attrib_role,
        "submission_kind": submission_kind,
        "review_status": report.review_status,
        "inquiry_id": inquiry_id,
        "inquiry_status": (inquiry_row.status if inquiry_row is not None else None),
        "gps_captured": bool(lat is not None and lon is not None),
        "gps_source": gps_source or "none",
        "photo_stored": photo_hash_id is not None,
        "exif": exif,
        "distance_from_previous_field_fix_m": distance_m,
        "message": (
            "Citizen evidence submitted — it will be reviewed by a field verifier "
            "before being counted as verified evidence."
            if submission_kind == "citizen"
            else "Verification recorded. Thank you for strengthening ground truth."
        ),
    }


@router.get("/verify/mine")
def my_verifications(
    limit: int = Query(50, ge=1, le=200),
    user: models.User = Depends(require_capability("verification:read_own")),
    db: Session = Depends(get_db),
):
    """Verification reports submitted by the signed-in user (citizen's own
    evidence / verifier's own field reports). Strict ownership filter."""
    rows = (
        db.query(models.VerificationReport, models.Project.project_name)
        .outerjoin(models.Project, models.Project.id == models.VerificationReport.project_id)
        .filter(models.VerificationReport.user_id == user.id)
        .order_by(models.VerificationReport.created_at.desc())
        .limit(limit)
        .all()
    )
    items = [{
        "report_id": r.id,
        "project_id": r.project_id,
        "project_name": name,
        "status": r.status,
        "lat": r.lat, "lon": r.lon,
        "gps_source": r.gps_source,
        "photo_hash_id": r.photo_hash_id,
        "note": r.note,
        "inquiry_id": r.inquiry_id,
        "submission_kind": r.submission_kind or "field",
        "review_status": r.review_status or "verified",
        "review_note": r.review_note,
        "reviewed_by_name": r.reviewed_by_name,
        "reviewed_at": r.reviewed_at,
        "created_at": r.created_at,
    } for r, name in rows]
    return {"items": items, "total": len(items)}


# ────────────────────────────────────────────────────────────────────
# 7b. Citizen evidence review workflow (field verifier / auditor / admin)
# ────────────────────────────────────────────────────────────────────

REVIEW_EVENTS = {
    "verified": "review_verified",
    "rejected": "review_rejected",
    "escalated": "escalated",
}


@router.get("/evidence/queue")
def evidence_queue(
    review_status: str = Query("pending", pattern="^(pending|verified|rejected|escalated|all)$"),
    limit: int = Query(50, ge=1, le=200),
    user: models.User = Depends(require_capability("verification:review")),
    db: Session = Depends(get_db),
):
    """Citizen evidence submissions for review. Field verifiers see pending
    first (their job is triage); auditors/admins see everything. Only
    citizen-kind submissions appear here — verifier field reports are
    trusted by definition."""
    q = (
        db.query(models.VerificationReport, models.Project.project_name,
                 models.Project.state, models.Project.status)
        .outerjoin(models.Project, models.Project.id == models.VerificationReport.project_id)
        .filter(models.VerificationReport.submission_kind == "citizen")
    )
    if review_status != "all":
        q = q.filter(models.VerificationReport.review_status == review_status)
    rows = q.order_by(models.VerificationReport.created_at.desc()).limit(limit).all()
    items = [{
        "report_id": r.id,
        "project_id": r.project_id,
        "project_name": name,
        "project_state": state,
        "project_official_status": official,
        "citizen_status": r.status,
        "reporter": r.reporter_name,
        "lat": r.lat, "lon": r.lon, "gps_source": r.gps_source,
        "photo_hash_id": r.photo_hash_id,
        "note": r.note,
        "review_status": r.review_status,
        "reviewed_by_name": r.reviewed_by_name,
        "review_note": r.review_note,
        "reviewed_at": r.reviewed_at,
        "created_at": r.created_at,
    } for r, name, state, official in rows]
    counts = dict(
        db.query(models.VerificationReport.review_status, func.count(models.VerificationReport.id))
        .filter(models.VerificationReport.submission_kind == "citizen")
        .group_by(models.VerificationReport.review_status)
        .all()
    )
    return {"items": items, "total": len(items), "counts": counts}


@router.post("/evidence/{report_id}/review")
def evidence_review(
    report_id: int,
    decision: str = Query(..., pattern="^(verified|rejected|escalated)$"),
    note: Optional[str] = Query(None),
    user: models.User = Depends(require_capability("verification:review")),
    db: Session = Depends(get_db),
):
    """Disposition a citizen submission. Rejection REQUIRES a note. Escalation
    flags it for auditor attention (auditor/admin only). This records the
    verification outcome — it never touches official project data, risk
    scores, or audit flags."""
    if decision == "escalated" and not auth.has_role_rank(user, "auditor"):
        raise HTTPException(403, "Only auditors and administrators can escalate citizen evidence")
    report = db.query(models.VerificationReport).filter(
        models.VerificationReport.id == report_id,
        models.VerificationReport.submission_kind == "citizen",
    ).first()
    if not report:
        raise HTTPException(404, "Citizen submission not found")
    if report.review_status not in ("pending", "escalated"):
        raise HTTPException(400, f"This submission is already {report.review_status}")
    clean_note = (note or "").strip()
    if decision == "rejected" and not clean_note:
        raise HTTPException(400, "A rejection note is required so the citizen understands the decision")

    now = datetime.utcnow().isoformat()
    report.review_status = decision
    report.reviewed_by_user_id = user.id
    report.reviewed_by_name = user.name[:120]
    report.review_note = clean_note[:1000] or None
    report.reviewed_at = now
    db.add(models.EvidenceEvent(
        report_id=report.id,
        event=REVIEW_EVENTS[decision],
        actor_user_id=user.id,
        actor_name=user.name[:120],
        actor_role=user.role,
        note=clean_note[:300] or None,
        created_at=now,
    ))
    db.commit()
    labels = {
        "verified": "Citizen evidence verified — it now counts as corroborating ground truth.",
        "rejected": "Citizen evidence rejected (note recorded).",
        "escalated": "Citizen evidence escalated for auditor review.",
    }
    return {"report_id": report.id, "review_status": decision,
            "reviewed_by": report.reviewed_by_name, "message": labels[decision]}


@router.get("/evidence/all")
def evidence_all(
    limit: int = Query(100, ge=1, le=500),
    user: models.User = Depends(require_capability("verification:review")),
    db: Session = Depends(get_db),
):
    """Auditor/admin view: every citizen submission incl. verifier decisions
    and the full event trail per report (bounded to the returned page)."""
    rows = (
        db.query(models.VerificationReport, models.Project.project_name)
        .outerjoin(models.Project, models.Project.id == models.VerificationReport.project_id)
        .filter(models.VerificationReport.submission_kind == "citizen")
        .order_by(models.VerificationReport.created_at.desc())
        .limit(limit)
        .all()
    )
    ids = [r.id for r, _ in rows]
    trail: Dict[int, List[dict]] = {}
    if ids:
        events = (
            db.query(models.EvidenceEvent)
            .filter(models.EvidenceEvent.report_id.in_(ids))
            .order_by(models.EvidenceEvent.created_at.asc())
            .all()
        )
        for e in events:
            trail.setdefault(e.report_id, []).append({
                "event": e.event, "actor": e.actor_name, "actor_role": e.actor_role,
                "note": e.note, "created_at": e.created_at,
            })
    items = [{
        "report_id": r.id,
        "project_id": r.project_id,
        "project_name": name,
        "citizen_status": r.status,
        "reporter": r.reporter_name,
        "lat": r.lat, "lon": r.lon, "gps_source": r.gps_source,
        "photo_hash_id": r.photo_hash_id,
        "note": r.note,
        "review_status": r.review_status,
        "reviewed_by_name": r.reviewed_by_name,
        "review_note": r.review_note,
        "reviewed_at": r.reviewed_at,
        "created_at": r.created_at,
        "trail": trail.get(r.id, []),
    } for r, name in rows]
    return {"items": items, "total": len(items)}


# ────────────────────────────────────────────────────────────────────
# 6. Audit action controls
# ────────────────────────────────────────────────────────────────────

@router.post("/audit-action")
def audit_action(
    project_id: int = Query(...),
    action: str = Query(..., description="approve | inquiry | escalate"),
    note: Optional[str] = Query(None),
    actor: Optional[str] = Query(None),
    user: models.User = Depends(require_capability("audit:action")),
    db: Session = Depends(get_db),
):
    """Record an auditor disposition from the inspection modal. When the
    action is an inquiry, an Inquiry row is created and routed to the
    project's district authority."""
    action_clean = action.strip().lower()
    if action_clean not in ("approve", "inquiry", "escalate"):
        raise HTTPException(400, "action must be approve, inquiry, or escalate")
    project = db.query(models.Project).filter(models.Project.id == project_id).first()
    if not project:
        raise HTTPException(404, f"Project {project_id} not found")
    row = models.AuditAction(
        project_id=project_id,
        action=action_clean,
        note=(note or "")[:2000],
        actor=(user.name or user.email)[:120],
        created_at=datetime.utcnow().isoformat(),
    )
    db.add(row)

    inquiry_id = None
    if action_clean == "inquiry":
        if not (note or "").strip():
            raise HTTPException(400, "A question/note is required when issuing an inquiry")
        inq = models.Inquiry(
            project_id=project_id,
            issued_by_user_id=user.id,
            issued_by_name=(user.name or user.email)[:120],
            district=project.district,
            question=(note or "").strip()[:2000],
            status="open",
            created_at=datetime.utcnow().isoformat(),
        )
        db.add(inq)
        db.flush()
        inquiry_id = inq.id

    db.commit()
    db.refresh(row)
    labels = {"approve": "Flag approved & cleared", "inquiry": "Inquiry issued to district authority", "escalate": "Escalated to Auditor General"}
    return {"id": row.id, "project_id": project_id, "action": action_clean, "inquiry_id": inquiry_id, "message": labels[action_clean]}


@router.get("/audit-actions/{project_id}")
def list_audit_actions(project_id: int, db: Session = Depends(get_db)):
    rows = (
        db.query(models.AuditAction)
        .filter(models.AuditAction.project_id == project_id)
        .order_by(models.AuditAction.created_at.desc())
        .limit(20)
        .all()
    )
    return [{"id": r.id, "action": r.action, "note": r.note, "actor": r.actor, "created_at": r.created_at} for r in rows]


# ────────────────────────────────────────────────────────────────────
# Forensic bundle for the inspection modal
# ────────────────────────────────────────────────────────────────────

@router.get("/inspection/{project_id}")
def inspection_bundle(project_id: int, db: Session = Depends(get_db)):
    """Everything the AI Inspection & Forensic Summary modal needs, in one call."""
    project = db.query(models.Project).filter(models.Project.id == project_id).first()
    if not project:
        raise HTTPException(404, f"Project {project_id} not found")
    risk_row = (
        db.query(models.RiskScore)
        .filter(models.RiskScore.project_id == project_id)
        .first()
    )
    extra = _project_extra_context(db, [project_id]).get(project_id, {})
    bench = _benchmark_for(ai_audit.norm_category(project.project_type), project.state)
    extra["benchmark"] = bench
    extra["linked_expenditure"] = _linked_exp_for(db, project)
    flags = _derive_flags(project, risk_row, extra)
    score = _risk_index(flags, risk_row)

    cost = detect_cost_anomaly(project_id=project_id, db=db)

    # duplicate-photo matches between this project's own stored hashes and the rest
    own_hashes = [
        h for (h,) in db.query(models.PhotoHash.dhash)
        .filter(models.PhotoHash.project_id == project_id)
        .limit(50)
        .all()
    ]
    dup_pairs = []
    if own_hashes:
        stored = (
            db.query(models.PhotoHash.project_id, models.PhotoHash.dhash, models.PhotoHash.created_at)
            .filter(models.PhotoHash.project_id != project_id)
            .yield_per(200)
        )
        for pid, h, created in stored:
            for oh in own_hashes:
                distance = ai_audit.hamming_hex(oh, h)
                if distance <= 6:
                    dup_pairs.append({
                        "matched_project_id": pid,
                        "similarity_percent": ai_audit.similarity_from_distance(distance),
                        "uploaded_at": created,
                    })
        # dedupe per matched project, keep best
        best: Dict[int, Dict] = {}
        for pair in dup_pairs:
            pid = pair["matched_project_id"]
            if pid not in best or pair["similarity_percent"] > best[pid]["similarity_percent"]:
                best[pid] = pair
        dup_pairs = sorted(best.values(), key=lambda p: p["similarity_percent"], reverse=True)[:5]
    for pair in dup_pairs:
        name_row = (
            db.query(models.Project.project_name)
            .filter(models.Project.id == pair["matched_project_id"])
            .first()
        )
        pair["matched_project_name"] = name_row.project_name if name_row else None

    verifications = (
        db.query(models.VerificationReport)
        .filter(models.VerificationReport.project_id == project_id)
        .order_by(models.VerificationReport.created_at.desc())
        .limit(10)
        .all()
    )
    actions = list_audit_actions(project_id, db=db)
    inquiries = (
        db.query(models.Inquiry)
        .filter(models.Inquiry.project_id == project_id)
        .order_by(models.Inquiry.created_at.desc())
        .limit(10)
        .all()
    )

    return {
        "project": {
            "id": project.id, "title": project.project_name,
            "district": project.district, "state": project.state,
            "constituency": project.constituency,
            "sanctioned_amount": project.sanctioned_amount or 0,
            "expenditure": extra.get("linked_expenditure") or 0,
            "completion_percentage": project.completion_percentage or 0,
            "status": project.status, "project_type": project.project_type,
        },
        "risk_index": score,
        "severity": ai_audit.risk_band(score)[0],
        "flags": flags,
        "cost_anomaly": cost,
        "duplicate_photos": dup_pairs,
        "verifications": [
            {
                "status": v.status, "reporter": v.reporter_name,
                "reporter_role": v.user_role, "distance_m": v.distance_m,
                "gps_source": v.gps_source,
                "note": (v.note or "")[:200], "created_at": v.created_at,
            }
            for v in verifications
        ],
        "inquiries": [
            {
                "id": i.id, "question": i.question, "status": i.status,
                "issued_by": i.issued_by_name, "created_at": i.created_at,
                "response_text": i.response_text,
                "responded_by": i.responded_by_name, "responded_at": i.responded_at,
            }
            for i in inquiries
        ],
        "actions": actions,
    }


# ────────────────────────────────────────────────────────────────────
# 7. Inquiry workflow: auditor issues → district authority responds
#    → auditor reviews/closes. Enforced via capabilities.
# ────────────────────────────────────────────────────────────────────

@router.get("/inquiries")
def list_inquiries(
    status_filter: Optional[str] = Query(None, alias="status"),
    limit: int = Query(100, ge=1, le=500),
    user: models.User = Depends(require_capability("inquiry:review")),
    db: Session = Depends(get_db),
):
    """All inquiries (auditor/admin view), newest first."""
    q = db.query(models.Inquiry, models.Project.project_name).outerjoin(
        models.Project, models.Project.id == models.Inquiry.project_id
    )
    if status_filter in ("open", "responded", "closed"):
        q = q.filter(models.Inquiry.status == status_filter)
    rows = q.order_by(models.Inquiry.created_at.desc()).limit(limit).all()
    items = [{
        "inquiry_id": i.id, "project_id": i.project_id, "project_name": name,
        "district": i.district, "question": i.question, "status": i.status,
        "issued_by": i.issued_by_name, "created_at": i.created_at,
        "response_text": i.response_text, "responded_by": i.responded_by_name,
        "responded_at": i.responded_at, "closed_at": i.closed_at,
    } for i, name in rows]
    counts = dict(
        db.query(models.Inquiry.status, func.count(models.Inquiry.id))
        .group_by(models.Inquiry.status)
        .all()
    )
    return {"items": items, "total": len(items), "counts": counts}


@router.post("/inquiries/{inquiry_id}/close")
def close_inquiry(
    inquiry_id: int,
    user: models.User = Depends(require_capability("inquiry:review")),
    db: Session = Depends(get_db),
):
    """Auditor reviews the authority's response and closes the inquiry."""
    inq = db.query(models.Inquiry).filter(models.Inquiry.id == inquiry_id).first()
    if not inq:
        raise HTTPException(404, f"Inquiry {inquiry_id} not found")
    if inq.status != "responded":
        raise HTTPException(400, "Only a responded inquiry can be closed")
    inq.status = "closed"
    inq.closed_by_user_id = user.id
    inq.closed_at = datetime.utcnow().isoformat()
    db.commit()
    return {"inquiry_id": inq.id, "status": inq.status, "message": "Inquiry closed"}


@router.get("/verify/photo/{photo_hash_id}")
def serve_photo_hash(
    photo_hash_id: int,
    user: models.User = Depends(require_capability("inquiry:review")),
    db: Session = Depends(get_db),
):
    """Metadata for a stored verification photo (auditor/admin)."""
    row = db.query(models.PhotoHash).filter(models.PhotoHash.id == photo_hash_id).first()
    if not row:
        raise HTTPException(404, "Photo not found")
    return {"id": row.id, "project_id": row.project_id, "dhash": row.dhash,
            "source": row.source, "created_at": row.created_at}
