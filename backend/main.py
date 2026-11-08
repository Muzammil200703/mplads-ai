import os
import time
import threading
from typing import Optional, List, Dict, Any
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException, Query, Path, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from sqlalchemy.orm import Session
from sqlalchemy import func, distinct, case, text, or_
import io
import csv
import json

from database import engine, Base, SessionLocal
import models
import auth

# SIH 26102 — AI Audit & Verification endpoints (new capability; mounts
# alongside existing routes, modifies nothing)
from ai_audit_api import router as ai_audit_router
from assistant import router as assistant_router
from forensic_api import router as forensic_router
import audit_intel
import metrics as metrics_svc
import metrics
import mp_intelligence as mp_intel
import state_geo
import rec_info as rec_info_mod
from schemas import ProjectCreate
from ml.predictor import predict_risk
from pydantic import BaseModel, Field
from sync import (
    sync_from_csv_upload,
    sync_from_github,
    sync_from_source,
    get_data_freshness,
    get_sync_history,
    get_available_providers,
    invalidate_freshness_cache,
)
from fastapi import UploadFile, File, Form

# Create tables
Base.metadata.create_all(bind=engine)

# Startup column migrations for databases created before the role-system
# expansion (adds users.assigned_district/state, verification_reports.
# user_id/user_role/inquiry_id when missing). Idempotent.
from database import run_lightweight_migrations
run_lightweight_migrations()

# One-time data-integrity repair: a historical backfill stamped aggregate
# expenditure totals onto individual project rows (e.g. every same-named work
# in a constituency sharing one ₹7.76 Cr group total; a second variant used
# per-MP/per-IDA sums). This poisoned risk scores via the overspend /
# disbursement-with-zero-progress rules. Both repair passes (idempotent via
# sync_metadata markers) zero the provable stamps and the affected rows' risk
# scores are recomputed with the existing predictor.
try:
    import logging as _logging

    _repair_logger = _logging.getLogger("mplads.data_repair")
    from database import (
        repair_expenditure_stamps,
        repair_expenditure_stamp_variants,
    )

    _repaired_count = (
        repair_expenditure_stamps() + repair_expenditure_stamp_variants()
    )
    if _repaired_count:
        _repair_logger.warning(
            "data repair: cleared %d fabricated expenditure stamps; "
            "recomputing affected risk scores",
            _repaired_count,
        )
        from models import Project as _Project, RiskScore as _RiskScore
        from ml.predictor import predict_risk as _predict_risk

        _db = SessionLocal()
        try:
            # The affected rows are exactly: zero-expenditure projects whose
            # stored risk reasons still cite an expenditure rule — i.e. rows
            # this repair (and only this repair) changed.
            _rows = (
                _db.query(_Project)
                .join(_RiskScore, _Project.id == _RiskScore.project_id)
                .filter(
                    _Project.expenditure == 0,
                    _RiskScore.reasons.like(
                        "%Expenditure exceeds sanctioned amount%"
                    ),
                )
                .all()
            )
            _rescored = 0
            from ml.predictor import attach_authoritative_expenditure as _attach_exp
            _attach_exp(_rows, _db)
            for _p in _rows:
                _risk = _predict_risk(_p, batch_mode=True)
                _rs = (
                    _db.query(_RiskScore)
                    .filter(_RiskScore.project_id == _p.id)
                    .first()
                )
                if _rs:
                    _rs.ml_anomaly = _risk["ml_anomaly"]
                    _rs.ml_score = _risk["ml_score"]
                    _rs.risk_score = _risk["risk_score"]
                    _rs.risk_level = _risk["risk_level"]
                    _rs.reasons = ", ".join(_risk.get("reasons") or [])
                    _rescored += 1
            _db.commit()
            if _rescored:
                _repair_logger.warning(
                    "data repair: recomputed %d risk scores after clearing "
                    "fabricated expenditure stamps",
                    _rescored,
                )
        except Exception:
            _db.rollback()
            _repair_logger.exception(
                "risk rescore after expenditure repair failed"
            )
        finally:
            _db.close()
except Exception:
    pass  # repair is best-effort; never block startup

# One-time expenditure vendor index (CREATE INDEX IF NOT EXISTS is a no-op
# once it exists). Backs the vendor-intelligence per-vendor profile queries
# and the aggregate GROUP BY; without it every profile open scans 106k rows.
try:
    from sqlalchemy import text as _sql_text
    with engine.begin() as _conn:
        _conn.execute(_sql_text(
            "CREATE INDEX IF NOT EXISTS idx_expenditures_vendor ON expenditures (vendor)"
        ))
        # Expression indexes for the normalized vendor/work keys used by the
        # vendor-intelligence aggregations. Without them the work-key joins
        # against projects / recommended_works / completed_works degrade to
        # full scans with a function call per row (measured: 78s → 0.01s).
        _conn.execute(_sql_text(
            "CREATE INDEX IF NOT EXISTS idx_expenditures_vendor_norm "
            "ON expenditures (normkey(vendor))"
        ))
        _conn.execute(_sql_text(
            "CREATE INDEX IF NOT EXISTS idx_expenditures_vendor_date "
            "ON expenditures (normkey(vendor), expenditure_date)"
        ))
        _conn.execute(_sql_text(
            "CREATE INDEX IF NOT EXISTS idx_projects_workkey "
            "ON projects (normkey(project_name), normkey(constituency), normkey(state))"
        ))
        _conn.execute(_sql_text(
            "CREATE INDEX IF NOT EXISTS idx_recommended_workkey "
            "ON recommended_works (normkey(work_description), normkey(constituency), normkey(state))"
        ))
        _conn.execute(_sql_text(
            "CREATE INDEX IF NOT EXISTS idx_completed_workkey "
            "ON completed_works (normkey(work_description), normkey(constituency), normkey(state))"
        ))
except Exception:
    pass  # index is an optimization, never a startup blocker

# ═══════════════ STALE PROGRESS DETECTION CONFIG ═══════════════
# Projects above this sanctioned-amount threshold with zero progress
# and zero expenditure are flagged as "possibly stale" data rather
# than automatically treated as anomalous.
STALE_PROGRESS_AMOUNT_THRESHOLD = 10 * 100000  # ₹10 lakh

# Known financial years in the dataset (newest first)
_KNOWN_FYS = ["2026-27", "2025-26", "2024-25", "2023-24"]


def _check_stale_progress(project, linked_expenditure: Optional[float] = None):
    """
    Check if a project's zero-progress/zero-expenditure record
    may indicate stale (un-updated) data rather than genuine anomalies.

    `linked_expenditure` is the authoritative payment-ledger total
    (project_rec_info); when not supplied the record's own column is used,
    which is a zeroed legacy stamp for all but a handful of rows.

    Returns a dict:
        flag: "POSSIBLY_STALE" | "INSUFFICIENT_DATA" | "NORMAL"
        reason: human-readable explanation
        sanctioned_amount: float
        expenditure: float
        completion: float
    """
    sanctioned = float(getattr(project, "sanctioned_amount", 0) or 0)
    expenditure = float(linked_expenditure) if linked_expenditure is not None else float(getattr(project, "expenditure", 0) or 0)
    completion = float(getattr(project, "completion_percentage", 0) or 0)
    status_str = str(getattr(project, "status", "") or "").lower()
    fy = str(getattr(project, "fy", "") or "")

    # If any values are missing/None, treat as insufficient data
    if sanctioned is None or expenditure is None or completion is None:
        return {
            "flag": "INSUFFICIENT_DATA",
            "reason": "Insufficient project data to assess progress status.",
            "sanctioned_amount": sanctioned or 0,
            "expenditure": expenditure or 0,
            "completion": completion or 0,
        }

    # Only flag if: high sanctioned amount AND zero progress AND zero expenditure
    if (sanctioned >= STALE_PROGRESS_AMOUNT_THRESHOLD
            and completion == 0
            and expenditure == 0):

        # Build the reason
        sanctioned_display = f"₹{sanctioned / 100000:.1f} L" if sanctioned < 10000000 else f"₹{sanctioned / 10000000:.2f} Cr"
        reason = (
            f"{sanctioned_display} sanctioned but 0% progress and ₹0 expenditure recorded. "
            f"Progress data may require updating."
        )

        # If we have FY info, add a note about age
        if fy and fy in _KNOWN_FYS:
            fy_index = _KNOWN_FYS.index(fy)
            if fy_index >= 2:  # FY 2024-25 or older
                reason += f" (Financial year: {fy})"

        return {
            "flag": "POSSIBLY_STALE",
            "reason": reason,
            "sanctioned_amount": sanctioned,
            "expenditure": expenditure,
            "completion": completion,
        }

    # Normal — not enough conditions to warrant a flag
    return {
        "flag": "NORMAL",
        "reason": "",
        "sanctioned_amount": sanctioned,
        "expenditure": expenditure,
        "completion": completion,
    }

# In-memory cache for heavy aggregations. Bounded: parameterized cache keys
# (per-FY, per-state, search combos) would otherwise accumulate forever on a
# long-lived free-tier process and slowly leak past the 512MB limit. Oldest
# entries are evicted beyond _CACHE_MAX_ENTRIES.
# Memory note: vendor intelligence is NO LONGER cached here — its ~26k-row
# Python payload retained ~40-60MB of heap. It lives in a disk-backed SQLite
# cache table instead (see _vendor_cache_* helpers), so filtering, sorting
# and pagination run in SQL and the process retains nothing between requests.
_CACHE_MAX_ENTRIES = 200
_cache: Dict[str, Any] = {}
_cache_ttl: Dict[str, float] = {}
# Vendor intelligence is derived from a single O(N) pass plus a per-work-key
# project match. The match cache is keyed on the normalized work triple and is
# only meaningful for the current dataset, so data-sync code must clear it.
_match_cache_lock = threading.Lock()
_match_cache: Dict[tuple, list] = {}

# Disk-backed vendor-intelligence cache: rebuilt rows live in a SQLite table
# (meta table stores the build stamp), not in Python memory. Bounded: a rebuild
# DROPs and re-creates the table so it can never grow beyond one dataset build.
_vendor_cache_lock = threading.Lock()
_vendor_cache_table = "vendor_intel_cache"

# Rebuild guards: a single lock ensures only one thread builds the payload at a
# time, and the in-progress flags make concurrent requests reuse the build
# instead of each spawning their own multi-minute computation.
_vendor_build_lock = threading.Lock()
_vendor_build_in_progress = False

def get_cached(key: str, ttl_seconds: int = 300):
    if key in _cache and (time.time() - _cache_ttl.get(key, 0)) < ttl_seconds:
        return _cache[key]
    return None

def set_cached(key: str, value: Any):
    if key not in _cache and len(_cache) >= _CACHE_MAX_ENTRIES:
        oldest = next(iter(_cache))  # dict preserves insertion order
        _cache.pop(oldest, None)
        _cache_ttl.pop(oldest, None)
    _cache[key] = value
    _cache_ttl[key] = time.time()

# Single-flight guard for expensive cached builds. Without it, N concurrent
# requests that all miss the same cache key each run the full aggregate (the
# measured state-intelligence build is multi-second), then all write the same
# value. Stripe locks keep the lock table bounded.
_build_locks = [threading.Lock() for _ in range(64)]


def cached_build(key: str, ttl_seconds: int, builder):
    """Return the cached value, or build it exactly once across threads."""
    cached = get_cached(key, ttl_seconds)
    if cached is not None:
        return cached
    lock = _build_locks[hash(key) % len(_build_locks)]
    with lock:
        cached = get_cached(key, ttl_seconds)
        if cached is not None:
            return cached
        value = builder()
        set_cached(key, value)
        return value


def clear_cache(prefix: Optional[str] = None):
    global _cache, _cache_ttl
    with _match_cache_lock:
        _match_cache.clear()
    # Dataset changed: drop the disk-backed vendor-intelligence cache table so
    # the next request rebuilds against the new data (stamp check also catches
    # this, but explicit drop keeps the stale table from lingering on disk).
    try:
        with engine.begin() as conn:
            conn.execute(text(f"DROP TABLE IF EXISTS {_vendor_cache_table}"))
            conn.execute(text(f"DROP TABLE IF EXISTS {_vendor_cache_table}__meta"))
    except Exception:
        pass  # best-effort; the stamp check below also invalidates
    # SIH 26102: drop the AI-audit category benchmark index too (dataset changed)
    try:
        import ai_audit_api
        ai_audit_api.invalidate_benchmark_index()
    except Exception:
        pass
    # Dataset/model changed: release cached SHAP attributions so repeated
    # explanations are recomputed against the current inputs.
    try:
        from ml.predictor import clear_shap_cache
        clear_shap_cache()
    except Exception:
        pass
    if prefix:
        keys_to_del = [k for k in _cache if k.startswith(prefix)]
        for k in keys_to_del:
            _cache.pop(k, None)
            _cache_ttl.pop(k, None)
    else:
        _cache.clear()
        _cache_ttl.clear()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Setup indices on existing SQLite tables if not present
    with engine.connect() as conn:
        # Expression indexes on the normalized work key — these make the
        # per-key expenditure lookups (timeline/activity/span) O(log n)
        # instead of full-table scans. Built once (persisted in the DB file);
        # normkey is registered as DETERMINISTIC so SQLite can use it here.
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_exp_workkey ON expenditures(normkey(work_description), normkey(constituency), normkey(state));"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_rec_workkey ON recommended_works(normkey(work_description), normkey(constituency), normkey(state));"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_comp_workkey ON completed_works(normkey(work_description), normkey(constituency), normkey(state));"))
        conn.commit()
    with engine.connect() as conn:
        # Project indexes for search/filter performance
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_proj_state ON projects(state);"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_proj_district ON projects(district);"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_proj_status ON projects(status);"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_proj_type ON projects(project_type);"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_proj_constituency ON projects(constituency);"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_proj_fy2 ON projects(fy);"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_proj_sanctioned ON projects(sanctioned_amount);"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_proj_expenditure ON projects(expenditure);"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_proj_completion ON projects(completion_percentage);"))
        # Risk score indexes for anomaly/risk queries
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_risk_project ON risk_scores(project_id);"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_risk_level ON risk_scores(risk_level);"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_risk_score ON risk_scores(risk_score);"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_risk_ml ON risk_scores(ml_anomaly);"))
        # Aggregate-path indexes justified by measured query plans:
        #  • completed_works had no state index, so the per-state completion
        #    rollup scanned all 43k rows and built a temp B-tree.
        #  • state_linked_map groups project_rec_info by work_key; this
        #    covering index answers it from the index (no temp B-tree, no row
        #    lookups) and also backs rec_info's key lookups.
        #  • house-filtered dashboards scan projects/expenditures by house.
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_comp_state ON completed_works(state);"))
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_pri_workkey_cover "
            "ON project_rec_info(work_key, linked_expenditure, work_key_expenditure, project_id);"
        ))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_exp_house_state ON expenditures(house, state);"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_proj_house_fy ON projects(house, fy);"))
        # MP summary indexes for name-based search
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_mp_name ON mp_summaries(mp_name);"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_mp_const_state ON mp_summaries(constituency, state);"))
        conn.commit()

    # Add composite indexes for common query patterns
    with engine.connect() as conn:
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_risk_level_ml ON risk_scores(risk_level, ml_anomaly);"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_risk_level_score ON risk_scores(risk_level, risk_score);"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_proj_state_fy ON projects(state, fy);"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_proj_state_cons ON projects(state, constituency);"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_risk_project_level ON risk_scores(project_id, risk_level);"))
        conn.commit()

    # project_rec_info index for rec-date sorting (table + indexes are created
    # by Base.metadata.create_all above)
    with engine.connect() as conn:
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_rec_info_sort ON project_rec_info(recommendation_date, project_id);"))
        conn.commit()

    # Cache risk table presence at startup (avoids COUNT(*) on every request)
    db = SessionLocal()
    try:
        _cache["_has_risk_table"] = (db.query(func.count(models.RiskScore.id)).scalar() or 0) > 0
        _cache_ttl["_has_risk_table"] = time.time()
    except Exception:
        _cache["_has_risk_table"] = False
        _cache_ttl["_has_risk_table"] = time.time()
    finally:
        db.close()

    # Warm-cache critical endpoints at startup for instant first load
    try:
        db = SessionLocal()
        try:
            # Warm filter_states
            states_rows = db.query(models.Project.state).filter(
                models.Project.state.isnot(None), models.Project.state != ""
            ).distinct().order_by(models.Project.state.asc()).all()
            set_cached("filter_states", [s[0] for s in states_rows if s[0]])

            # Warm filter_fys
            fy_rows = db.query(
                models.Project.fy, func.count(models.Project.id).label("count")
            ).filter(
                models.Project.fy.isnot(None), models.Project.fy != ""
            ).group_by(models.Project.fy).order_by(models.Project.fy.asc()).all()
            set_cached("filter_fys", [{"fy": r.fy, "count": r.count} for r in fy_rows])

            # Warm dashboard_overview (all FY) — same authoritative source as
            # the endpoint (metrics_svc), so warm values match live values.
            set_cached("dashboard_overview_all", metrics_svc.portfolio_overview(db, fy=None, house=None))
            # Vendor directory is expensive to derive from the work-level
            # tables, so warm its compact summary once at startup. Subsequent
            # filter/sort/page requests only slice the in-memory result.
            # Vendor intelligence is built lazily on first visit so startup
            # remains fast with large work-level source tables.
        finally:
            db.close()
    except Exception:
        pass  # Warm-cache is best-effort

    # NOTE: the previous version pre-built the FULL vendor-intelligence
    # payload on a background thread at startup. That build loads tens of
    # thousands of rows into Python objects and permanently caches them —
    # it alone pushed the process past Render's 512MB limit before the
    # first request, causing "Ran out of memory" instance failures. The
    # payload is now built on first request (streamed, compact, bounded)
    # inside _vendor_intelligence_payload(), so startup stays flat.
    # project_rec_info (recommendation facts) — table is created by
    # Base.metadata.create_all; if it's empty (fresh DB or after a data
    # sync), rebuild it in a background thread. Pure SQL inside SQLite;
    # startup returns immediately. Existing rows are served while empty.
    try:
        rec_info_mod.rebuild_if_empty_async()
    except Exception:
        pass  # best-effort; endpoints fall back to absent rec fields

    yield


app = FastAPI(
    title="MPLADS AI API",
    description="High-performance AI-powered MPLADS monitoring, anomaly detection, and analytics system",
    version="2.0.0",
    lifespan=lifespan
)

# SIH 26102 — AI Audit & Verification router (audit queue, image forensics,
# cost anomaly, vendor network, /verify portal, audit actions)
app.include_router(ai_audit_router)
app.include_router(forensic_router)

# Admin data management — CSV upload / validation / import / history.
from admin_data_api import router as admin_data_router
app.include_router(admin_data_router)

# Universal MPLADS AI Assistant (role-aware Q&A over live data + knowledge base)
app.include_router(assistant_router)

# Vendor performance & audit intelligence (per-vendor performance, behaviour,
# risk explanation, relationship network, comparison, grounded analyst).
# Included before the legacy /vendor-intelligence/{vendor_key} route so the
# fixed sub-paths (/compare, /behaviour-scan) are matched first.
from vendor_intel import router as vendor_intel_router
app.include_router(vendor_intel_router)

# =========================================================
# CORS CONFIGURATION (production-safe)
# =========================================================
# A failed CORS preflight (OPTIONS) makes the browser block the actual
# request, so this configuration must answer preflights cheaply and
# reliably:
#   - Explicit localhost origins for development, the exact production
#     frontend origin(s) via the CORS_ORIGINS env var (comma-separated,
#     e.g. "https://mplads-ai.vercel.app"), plus any *.vercel.app
#     deployment (covers Vercel production and preview domains).
#   - allow_credentials is False: the frontend authenticates with a Bearer
#     token in the Authorization header, never cookies. Wildcard origins
#     combined with credentials=True is an invalid CORS combination and
#     must not be restored.
#   - max_age lets browsers cache preflight responses, cutting OPTIONS
#     round-trips dramatically (important on Render's free tier, where
#     cold-start request bursts are throttled by the platform edge).
#
# MIDDLEWARE ORDERING NOTE: Starlette runs middleware outermost-last, so
# CORS must remain the LAST middleware added here. If a rate limiter is
# ever introduced (slowapi / custom middleware), register it BEFORE this
# block so CORSMiddleware answers OPTIONS preflights before the limiter
# can convert them into 429 responses.
_CORS_ORIGINS_ENV = os.environ.get("CORS_ORIGINS", "")
_CORS_EXTRA_ORIGINS = [o.strip() for o in _CORS_ORIGINS_ENV.split(",") if o.strip()]
_CORS_DEFAULT_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:5174",
    "http://127.0.0.1:5174",
    "http://localhost:3000",
    "http://127.0.0.1:3000",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_DEFAULT_ORIGINS + _CORS_EXTRA_ORIGINS,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$|^https://.*\.vercel\.app$",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["Authorization", "Content-Type", "Accept"],
    max_age=3600,
)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ═══════════════ AUTHENTICATION & INVESTIGATOR WORKSPACE ═══════════════
# Mounted after get_db is defined so its dependencies can reuse it.
from workspace import router as workspace_router  # noqa: E402
from global_search import router as global_search_router  # noqa: E402
from change_api import router as change_router  # noqa: E402
from history_api import router as history_router  # noqa: E402
from prediction_api import router as prediction_router  # noqa: E402
from investigation_agent import router as agent_router  # noqa: E402
from intel_api import router as intel_router  # noqa: E402
app.include_router(workspace_router)
app.include_router(global_search_router)
app.include_router(change_router)
app.include_router(history_router)
app.include_router(prediction_router)
app.include_router(agent_router)
app.include_router(intel_router)


# Sortable columns map (avoids SQL injection)
_SORT_COLUMNS = {
    "id": models.Project.id,
    "project_name": models.Project.project_name,
    "state": models.Project.state,
    "district": models.Project.district,
    "constituency": models.Project.constituency,
    "project_type": models.Project.project_type,
    "sanctioned_amount": models.Project.sanctioned_amount,
    # Authoritative per-project spend (payment ledger, MP+IDA-verified via
    # project_rec_info); unlinked projects coalesce to 0 — same value the
    # API rows display.
    "expenditure": func.coalesce(models.ProjectRecInfo.linked_expenditure, 0.0),
    "completion_percentage": models.Project.completion_percentage,
    "status": models.Project.status,
    "fy": models.Project.fy,
}


def _authoritative_expenditure_map(db: Session) -> Dict[int, float]:
    """project_id -> authoritative linked expenditure (payment ledger,
    MP+IDA-verified) for every linked project. Single shared read used by
    the scoring paths; ~300 rows in this dataset."""
    return metrics_svc.project_expenditure_map(db)


def _apply_authoritative_expenditure(projects, db) -> None:
    """Attach authoritative expenditure to project objects (in place) so
    downstream risk scoring reads real spend, not the zeroed catalog stamp.
    Values land on the same `_authoritative_expenditure` attribute the
    predictor reads."""
    if not projects:
        return
    amounts = _authoritative_expenditure_map(db)
    for p in projects:
        pid = getattr(p, "id", None)
        if pid is not None and pid in amounts:
            p._authoritative_expenditure = amounts[pid]


def _eff_exp_of(project, db) -> float:
    """Authoritative expenditure for one project (0.0 when unlinked)."""
    return metrics_svc.resolve_project_expenditure(db, project)


def _rec_info_map(db: Session, project_ids: List[int]) -> Dict[int, Any]:
    """Batched recommendation facts for a page of projects (source of truth:
    the project_rec_info derived table — same linkage as activity.py)."""
    if not project_ids:
        return {}
    try:
        rows = (
            db.query(models.ProjectRecInfo)
            .filter(models.ProjectRecInfo.project_id.in_(project_ids))
            .all()
        )
        return {
            r.project_id: {
                "recommendation_date": r.recommendation_date,
                "recommended_by": r.recommended_by,
                "approx_start_date": r.approx_start_date,
                "has_expenditure": bool(r.has_expenditure),
                "start_status": r.start_status or "no_expenditure",
                "linked_expenditure": float(r.linked_expenditure or 0.0),
                # Work-level verification detail (shared works keep their
                # verified total even though per-project attribution is
                # ambiguous).
                "work_key_expenditure": float(getattr(r, "work_key_expenditure", 0.0) or 0.0),
                "work_key_rows": int(getattr(r, "work_key_rows", 1) or 1),
            }
            for r in rows
        }
    except Exception:
        # Table may not exist yet on a brand-new DB before the first build
        return {}


def _rec_item(rec: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not rec:
        return None
    return {
        "recommendation_date": rec.get("recommendation_date"),
        "recommended_by": rec.get("recommended_by"),
        "approx_start_date": rec.get("approx_start_date"),
        "has_expenditure": rec.get("has_expenditure", False),
        "start_status": rec.get("start_status") or "no_expenditure",
        "linked_expenditure": float(rec.get("linked_expenditure") or 0.0),
        "work_key_expenditure": float(rec.get("work_key_expenditure") or 0.0),
        "work_key_rows": int(rec.get("work_key_rows") or 1),
    }


def apply_sort(query, sort_by: Optional[str], sort_dir: Optional[str], model=None):
    """Apply sorting to a SQLAlchemy query safely."""
    if not sort_by:
        return query
    col_key = sort_by.strip().lower()
    # Recommendation-date sorting — outer join to the 1:1 derived table
    # (unique index on project_id, so no row fan-out and COUNT stays exact).
    # Undated projects always sink to the end of the result regardless of
    # direction; dated rows sort by the chosen direction with a stable
    # project-id tie-break for consistent pagination.
    if col_key in ("recommended_date", "recommendation_date", "recommended"):
        query = query.outerjoin(
            models.ProjectRecInfo,
            models.ProjectRecInfo.project_id == models.Project.id,
        )
        rec_col = models.ProjectRecInfo.recommendation_date
        is_asc = bool(sort_dir and sort_dir.strip().lower() == "asc")
        return query.order_by(
            rec_col.is_(None).asc(),
            rec_col.asc() if is_asc else rec_col.desc(),
            models.Project.id.asc(),
        )
    if col_key not in _SORT_COLUMNS:
        return query
    col = _SORT_COLUMNS[col_key]
    if sort_dir and sort_dir.strip().lower() == "asc":
        return query.order_by(col.asc().nullslast())
    return query.order_by(col.desc().nullslast())


def _enrich_projects_with_risk(projects, db, include_rec: bool = True):
    """Attach risk_scores data to a list of Project objects for API responses.
    This ensures the Projects table, Risk Center, and Project Details all
    use the SAME backend risk score as the single source of truth.

    include_rec also attaches recommendation facts (one batched lookup per
    page) so every consumer reads the same normalized values."""
    if not projects:
        return []
    project_ids = [p.id for p in projects]
    risk_rows = (
        db.query(models.RiskScore)
        .filter(models.RiskScore.project_id.in_(project_ids))
        .all()
    )
    risk_map = {r.project_id: r for r in risk_rows}
    rec_map = _rec_info_map(db, project_ids) if include_rec else {}
    result = []
    for p in projects:
        risk = risk_map.get(p.id)
        # Authoritative per-project spend: linked payment-ledger total from
        # the same project_rec_info batch (catalog column is a zeroed stamp).
        _rec = rec_map.get(p.id) or {}
        _linked = float(_rec.get("linked_expenditure") or 0.0)
        _wk_rows = int(_rec.get("work_key_rows") or 1)
        if _linked > 0:
            _exp_status = "linked"
        elif _rec.get("has_expenditure") and _wk_rows > 1:
            # Verified payments exist for this work key, but the key is shared
            # by multiple catalog rows — the total can't be split per row.
            _exp_status = "ambiguous_shared_work"
        else:
            _exp_status = "no_linked_records"
        item = {
            "id": p.id,
            "project_name": p.project_name,
            "state": p.state,
            "district": p.district,
            "constituency": p.constituency,
            "project_type": p.project_type,
            "sanctioned_amount": p.sanctioned_amount or 0.0,
            "expenditure": _linked,
            # 'linked' = verified payments attributed 1:1; 'ambiguous_shared_work'
            # = payments exist at work level but the work key is shared;
            # 'no_linked_records' = spend unknown (NOT a confirmed ₹0).
            "expenditure_status": _exp_status,
            "work_key_expenditure": float(_rec.get("work_key_expenditure") or 0.0),
            "work_key_rows": _wk_rows,
            "completion_percentage": p.completion_percentage or 0.0,
            "status": p.status,
            "fy": p.fy,
            "house": p.house,
            "risk": {
                "risk_score": risk.risk_score if risk else None,
                "risk_level": risk.risk_level if risk else None,
                "ml_anomaly": risk.ml_anomaly if risk else None,
                "reasons": risk.reasons if risk else None,
            } if risk else None,
            "rec": _rec_item(rec_map.get(p.id)),
        }
        result.append(item)
    return result


# =========================================================
# ROOT & HEALTH CHECK
# =========================================================

@app.get("/", tags=["System"])
def root():
    return {
        "service": "MPLADS AI Backend",
        "version": "2.0.0",
        "status": "online",
        "docs_url": "/docs"
    }


@app.get("/health", tags=["System"])
def health_check(db: Session = Depends(get_db)):
    try:
        # Quick ping to DB
        count = db.query(func.count(models.Project.id)).scalar()
        # A schema-only SQLite file is not a usable audit deployment. This
        # happens when a host checks out the Git-LFS pointer for mplads.db
        # instead of the dataset itself. Returning a degraded response keeps
        # dashboards from presenting empty results as a legitimate finding.
        if not count:
            return JSONResponse(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                content={
                    "status": "degraded",
                    "database": "connected",
                    "data_ready": False,
                    "total_projects": 0,
                    "detail": (
                        "No MPLADS project records are loaded. Verify that the "
                        "deployment fetched the Git-LFS dataset before serving audit results."
                    ),
                },
            )
        return {
            "status": "healthy",
            "database": "connected",
            "data_ready": True,
            "total_projects": count
        }
    except Exception as e:
        return {
            "status": "degraded",
            "database": "error",
            "detail": str(e)
        }


# =========================================================
# FILTERS ENDPOINTS
# =========================================================

@app.get("/filters/states", tags=["Filters"])
def get_filter_states(db: Session = Depends(get_db)):
    cached = get_cached("filter_states", ttl_seconds=600)
    if cached is not None:
        return cached

    states = (
        db.query(models.Project.state)
        .filter(models.Project.state.isnot(None), models.Project.state != "")
        .distinct()
        .order_by(models.Project.state.asc())
        .all()
    )
    result = [s[0] for s in states if s[0]]
    set_cached("filter_states", result)
    return result


@app.get("/filters/districts", tags=["Filters"])
def get_filter_districts(
    state: Optional[str] = Query(None, description="State name to filter districts"),
    db: Session = Depends(get_db)
):
    """Backward-compatible: returns districts if available, else constituencies."""
    if not state:
        return []

    cache_key = f"filter_districts_{state}"
    cached = get_cached(cache_key, ttl_seconds=600)
    if cached is not None:
        return cached

    # The dataset primarily uses the 'constituency' field
    districts = (
        db.query(models.Project.constituency)
        .filter(
            models.Project.state == state,
            models.Project.constituency.isnot(None),
            models.Project.constituency != ""
        )
        .distinct()
        .order_by(models.Project.constituency.asc())
        .all()
    )
    result = [d[0] for d in districts if d[0]]
    set_cached(cache_key, result)
    return result


@app.get("/filters/constituencies", tags=["Filters"])
def get_filter_constituencies(
    state: Optional[str] = Query(None, description="State name to filter constituencies"),
    db: Session = Depends(get_db)
):
    if not state:
        return []

    cache_key = f"filter_constituencies_{state}"
    cached = get_cached(cache_key, ttl_seconds=600)
    if cached is not None:
        return cached

    constituencies = (
        db.query(models.Project.constituency)
        .filter(
            models.Project.state == state,
            models.Project.constituency.isnot(None),
            models.Project.constituency != ""
        )
        .distinct()
        .order_by(models.Project.constituency.asc())
        .all()
    )
    result = [c[0] for c in constituencies if c[0]]
    set_cached(cache_key, result)
    return result
def get_filter_categories(db: Session = Depends(get_db)):
    cached = get_cached("filter_categories", ttl_seconds=600)
    if cached is not None:
        return cached

    categories = (
        db.query(models.Project.project_type)
        .filter(models.Project.project_type.isnot(None), models.Project.project_type != "")
        .distinct()
        .order_by(models.Project.project_type.asc())
        .all()
    )
    result = [c[0] for c in categories if c[0]]
    set_cached("filter_categories", result)
    return result


# =========================================================
# PROJECTS API (OPTIMIZED & PAGINATED)
# =========================================================

@app.post("/projects", tags=["Projects"], status_code=status.HTTP_201_CREATED)
def create_project(
    project: ProjectCreate,
    db: Session = Depends(get_db)
):
    new_project = models.Project(**project.model_dump())
    db.add(new_project)
    db.commit()
    db.refresh(new_project)
    clear_cache()
    return new_project


@app.get("/filters/fys", tags=["Filters"])
def get_filter_fys(db: Session = Depends(get_db)):
    """Return available financial years with project counts."""
    cached = get_cached("filter_fys", ttl_seconds=600)
    if cached is not None:
        return cached

    rows = (
        db.query(
            models.Project.fy,
            func.count(models.Project.id).label("count")
        )
        .filter(models.Project.fy.isnot(None), models.Project.fy != "")
        .group_by(models.Project.fy)
        .order_by(models.Project.fy.asc())
        .all()
    )
    result = [{"fy": r.fy, "count": r.count} for r in rows]
    set_cached("filter_fys", result)
    return result


@app.get("/projects", tags=["Projects"])
def get_projects(
    state: Optional[str] = None,
    district: Optional[str] = None,
    constituency: Optional[str] = None,
    house: Optional[str] = Query(None, description="House: 'Lok Sabha', 'Rajya Sabha', or omitted for All"),
    fy: Optional[str] = Query(None, description="Filter by financial year: 2023-24, 2024-25, 2025-26, 2026-27"),
    project_type: Optional[str] = None,
    status: Optional[str] = None,
    min_sanctioned_amount: Optional[float] = None,
    max_sanctioned_amount: Optional[float] = None,
    min_expenditure: Optional[float] = None,
    max_expenditure: Optional[float] = None,
    min_completion: Optional[float] = None,
    max_completion: Optional[float] = None,
    risk_level: Optional[str] = Query(None, description="Filter by risk level: High, Medium, Low, None"),
    min_risk_score: Optional[int] = Query(None),
    max_risk_score: Optional[int] = Query(None),
    sort_by: Optional[str] = Query(None, description="Sort column"),
    sort_dir: Optional[str] = Query(None, description="Sort direction: asc or desc"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=1000),
    db: Session = Depends(get_db)
):
    needs_risk_join = risk_level or min_risk_score is not None or max_risk_score is not None
    needs_exp_join = (
        min_expenditure is not None
        or max_expenditure is not None
        or sort_by == "expenditure"
    )
    if needs_risk_join or needs_exp_join:
        query = (
            db.query(models.Project)
            .outerjoin(
                models.ProjectRecInfo,
                models.ProjectRecInfo.project_id == models.Project.id,
            )
        )
        if needs_risk_join:
            query = query.join(
                models.RiskScore,
                models.Project.id == models.RiskScore.project_id,
                isouter=True,
            )
    else:
        query = db.query(models.Project)

    if state:
        query = query.filter(models.Project.state == state)
    if district:
        query = query.filter(models.Project.district == district)
    if constituency:
        query = query.filter(models.Project.constituency == constituency)
    # House filter: only exact attributed houses; "All" (omitted) includes
    # unattributed rows. NULL house is never silently counted into a house.
    if house in ("Lok Sabha", "Rajya Sabha"):
        query = query.filter(models.Project.house == house)
    if fy:
        query = query.filter(models.Project.fy == fy)
    if project_type:
        query = query.filter(models.Project.project_type == project_type)
    if status:
        query = query.filter(models.Project.status.ilike(status))
    if risk_level:
        query = query.filter(models.RiskScore.risk_level.ilike(risk_level))
    if min_risk_score is not None:
        query = query.filter(models.RiskScore.risk_score >= min_risk_score)
    if max_risk_score is not None:
        query = query.filter(models.RiskScore.risk_score <= max_risk_score)
    if min_sanctioned_amount is not None:
        query = query.filter(models.Project.sanctioned_amount >= min_sanctioned_amount)
    if max_sanctioned_amount is not None:
        query = query.filter(models.Project.sanctioned_amount <= max_sanctioned_amount)
    if min_expenditure is not None:
        query = query.filter(
            func.coalesce(models.ProjectRecInfo.linked_expenditure, 0.0) >= min_expenditure
        )
    if max_expenditure is not None:
        query = query.filter(
            func.coalesce(models.ProjectRecInfo.linked_expenditure, 0.0) <= max_expenditure
        )
    if min_completion is not None:
        query = query.filter(models.Project.completion_percentage >= min_completion)
    if max_completion is not None:
        query = query.filter(models.Project.completion_percentage <= max_completion)

    if sort_by:
        query = apply_sort(query, sort_by, sort_dir)
    else:
        query = query.order_by(models.Project.id.asc())

    projects = query.offset(skip).limit(limit).all()
    return _enrich_projects_with_risk(projects, db)


@app.get("/search/projects", tags=["Projects"])
def search_projects(
    q: Optional[str] = Query(None, description="Search keyword in project name, type, state, district, constituency, or ID"),
    state: Optional[str] = None,
    district: Optional[str] = None,
    constituency: Optional[str] = None,
    house: Optional[str] = Query(None, description="House: 'Lok Sabha', 'Rajya Sabha', or omitted for All"),
    fy: Optional[str] = Query(None, description="Filter by financial year"),
    project_type: Optional[str] = None,
    status: Optional[str] = None,
    risk_level: Optional[str] = Query(None, description="Filter by risk level: High, Medium, Low, None"),
    min_sanctioned_amount: Optional[float] = Query(None),
    max_sanctioned_amount: Optional[float] = Query(None),
    min_expenditure: Optional[float] = Query(None),
    max_expenditure: Optional[float] = Query(None),
    min_completion: Optional[float] = Query(None),
    max_completion: Optional[float] = Query(None),
    min_risk_score: Optional[int] = Query(None),
    max_risk_score: Optional[int] = Query(None),
    sort_by: Optional[str] = Query(None, description="Sort column: id, project_name, state, sanctioned_amount, expenditure, completion_percentage, status"),
    sort_dir: Optional[str] = Query(None, description="Sort direction: asc or desc"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db)
):
    """Optimized project search.

    Performance optimizations vs. previous implementation:
    - Short-circuits for exact numeric ID match (indexed PK lookup)
    - Uses EXISTS subqueries for MP name matching (avoids full outerjoin)
    - Skips expensive DISTINCT+COUNT; uses LIMIT+1 for hasMore instead
    - Removes redundant DISTINCT on primary-key results
    """
    q_clean = (q or "").strip()

    # ---------------------------------------------------------------
    # FAST PATH: exact numeric ID match (single indexed PK lookup)
    # ---------------------------------------------------------------
    if q_clean:
        try:
            q_id = int(q_clean)
            project = db.query(models.Project).filter(models.Project.id == q_id).first()
            if project:
                # Apply post-filters to the single result
                if state and project.state != state:
                    return {"total": 0, "skip": 0, "limit": limit, "results": [], "hasMore": False}
                if district and project.district != district:
                    return {"total": 0, "skip": 0, "limit": limit, "results": [], "hasMore": False}
                if constituency and project.constituency != constituency:
                    return {"total": 0, "skip": 0, "limit": limit, "results": [], "hasMore": False}
                if house in ("Lok Sabha", "Rajya Sabha") and project.house != house:
                    return {"total": 0, "skip": 0, "limit": limit, "results": [], "hasMore": False}
                if fy and project.fy != fy:
                    return {"total": 0, "skip": 0, "limit": limit, "results": [], "hasMore": False}
                if project_type and project.project_type != project_type:
                    return {"total": 0, "skip": 0, "limit": limit, "results": [], "hasMore": False}
                if min_sanctioned_amount is not None and (project.sanctioned_amount or 0) < min_sanctioned_amount:
                    return {"total": 0, "skip": 0, "limit": limit, "results": [], "hasMore": False}
                if max_sanctioned_amount is not None and (project.sanctioned_amount or 0) > max_sanctioned_amount:
                    return {"total": 0, "skip": 0, "limit": limit, "results": [], "hasMore": False}
                if min_expenditure is not None and metrics_svc.resolve_project_expenditure(db, project) < min_expenditure:
                    return {"total": 0, "skip": 0, "limit": limit, "results": [], "hasMore": False}
                if max_expenditure is not None and metrics_svc.resolve_project_expenditure(db, project) > max_expenditure:
                    return {"total": 0, "skip": 0, "limit": limit, "results": [], "hasMore": False}
                if min_completion is not None and (project.completion_percentage or 0) < min_completion:
                    return {"total": 0, "skip": 0, "limit": limit, "results": [], "hasMore": False}
                if max_completion is not None and (project.completion_percentage or 0) > max_completion:
                    return {"total": 0, "skip": 0, "limit": limit, "results": [], "hasMore": False}
                # Risk filters — need to check risk_scores table
                if risk_level or min_risk_score is not None or max_risk_score is not None:
                    risk_row = db.query(models.RiskScore).filter(models.RiskScore.project_id == project.id).first()
                    if not risk_row:
                        return {"total": 0, "skip": 0, "limit": limit, "results": [], "hasMore": False}
                    if risk_level and (risk_row.risk_level or "").lower() != risk_level.lower():
                        return {"total": 0, "skip": 0, "limit": limit, "results": [], "hasMore": False}
                    if min_risk_score is not None and (risk_row.risk_score or 0) < min_risk_score:
                        return {"total": 0, "skip": 0, "limit": limit, "results": [], "hasMore": False}
                    if max_risk_score is not None and (risk_row.risk_score or 0) > max_risk_score:
                        return {"total": 0, "skip": 0, "limit": limit, "results": [], "hasMore": False}
                return {"total": 1, "skip": 0, "limit": limit, "results": _enrich_projects_with_risk([project], db), "hasMore": False}
            # Exact ID not found — fall through to text search below
        except (ValueError, TypeError):
            pass

    # ---------------------------------------------------------------
    # BUILD MAIN QUERY
    # ---------------------------------------------------------------
    needs_risk_join = risk_level or min_risk_score is not None or max_risk_score is not None
    needs_exp_join = (
        min_expenditure is not None
        or max_expenditure is not None
        or sort_by == "expenditure"
    )
    if needs_risk_join or needs_exp_join:
        query = (
            db.query(models.Project)
            .outerjoin(
                models.ProjectRecInfo,
                models.ProjectRecInfo.project_id == models.Project.id,
            )
        )
        if needs_risk_join:
            query = query.join(
                models.RiskScore,
                models.Project.id == models.RiskScore.project_id,
                isouter=True,
            )
    else:
        query = db.query(models.Project)

    # ---------------------------------------------------------------
    # TEXT SEARCH — use EXISTS subqueries for MP name (avoids full outerjoin)
    # ---------------------------------------------------------------
    if q_clean:
        ilike_pattern = f"%{q_clean}%"
        or_conditions = [
            models.Project.project_name.ilike(ilike_pattern),
            models.Project.project_type.ilike(ilike_pattern),
            models.Project.state.ilike(ilike_pattern),
            models.Project.district.ilike(ilike_pattern),
            models.Project.constituency.ilike(ilike_pattern),
        ]

        # Only add MP-name subquery if the query looks like it could be a name
        # (all alpha characters, or contains spaces typical of names)
        is_likely_name = q_clean.replace(" ", "").replace(".", "").replace("-", "").isalpha()
        if is_likely_name:
            mp_exists = (
                db.query(models.MPSummary.id)
                .filter(
                    models.MPSummary.mp_name.ilike(ilike_pattern),
                    models.MPSummary.constituency == models.Project.constituency,
                    models.MPSummary.state == models.Project.state,
                )
                .correlate(models.Project)
                .exists()
            )
            or_conditions.append(mp_exists)

        query = query.filter(or_(*or_conditions))

    # ---------------------------------------------------------------
    # STRUCTURAL FILTERS (exact match on indexed columns)
    # ---------------------------------------------------------------
    if state:
        query = query.filter(models.Project.state == state)
    if district:
        query = query.filter(models.Project.district == district)
    if constituency:
        query = query.filter(models.Project.constituency == constituency)
    if house in ("Lok Sabha", "Rajya Sabha"):
        query = query.filter(models.Project.house == house)
    if fy:
        query = query.filter(models.Project.fy == fy)
    if project_type:
        query = query.filter(models.Project.project_type == project_type)
    if status:
        query = query.filter(models.Project.status.ilike(status))
    if min_sanctioned_amount is not None:
        query = query.filter(models.Project.sanctioned_amount >= min_sanctioned_amount)
    if max_sanctioned_amount is not None:
        query = query.filter(models.Project.sanctioned_amount <= max_sanctioned_amount)
    if min_expenditure is not None:
        query = query.filter(
            func.coalesce(models.ProjectRecInfo.linked_expenditure, 0.0) >= min_expenditure
        )
    if max_expenditure is not None:
        query = query.filter(
            func.coalesce(models.ProjectRecInfo.linked_expenditure, 0.0) <= max_expenditure
        )
    if min_completion is not None:
        query = query.filter(models.Project.completion_percentage >= min_completion)
    if max_completion is not None:
        query = query.filter(models.Project.completion_percentage <= max_completion)
    if risk_level:
        query = query.filter(models.RiskScore.risk_level.ilike(risk_level))
    if min_risk_score is not None:
        query = query.filter(models.RiskScore.risk_score >= min_risk_score)
    if max_risk_score is not None:
        query = query.filter(models.RiskScore.risk_score <= max_risk_score)

    # ---------------------------------------------------------------
    # SORT + PAGINATE — COUNT the filtered matches (callers rely on `total`
    # for "Showing X to Y of Z" pagination), then fetch one page.
    # The count runs entirely in SQLite; only one page of rows is materialized.
    # ---------------------------------------------------------------
    if sort_by:
        query = apply_sort(query, sort_by, sort_dir)
    else:
        query = query.order_by(models.Project.id.asc())

    total = query.order_by(None).count()
    projects = query.offset(skip).limit(limit).all()

    enriched = _enrich_projects_with_risk(projects, db)
    return {
        "total": total,
        "skip": skip,
        "limit": limit,
        "results": enriched,
        "hasMore": skip + len(projects) < total
    }


@app.get("/projects/{project_id}", tags=["Projects"])
def get_project(
    project_id: int = Path(..., description="The ID of the project"),
    db: Session = Depends(get_db)
):
    project = db.query(models.Project).filter(models.Project.id == project_id).first()
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project with ID {project_id} not found"
        )

    # Use pre-computed risk from risk_scores table (single source of truth)
    # This ensures consistency with the Projects table, Risk Center, etc.
    risk_row = db.query(models.RiskScore).filter(models.RiskScore.project_id == project.id).first()
    if risk_row:
        risk_info = {
            "risk_score": risk_row.risk_score,
            "risk_level": risk_row.risk_level,
            "ml_anomaly": risk_row.ml_anomaly,
            "ml_score": risk_row.ml_score,
            "reasons": risk_row.reasons,
        }
    else:
        risk_info = predict_risk(project)
    _rec_facts = _rec_item(_rec_info_map(db, [project.id]).get(project.id))
    _linked_exp = float((_rec_facts or {}).get("linked_expenditure") or 0.0)
    stale_check = _check_stale_progress(project, linked_expenditure=_linked_exp)
    _wk_exp = float((_rec_facts or {}).get("work_key_expenditure") or 0.0)
    _wk_rows = int((_rec_facts or {}).get("work_key_rows") or 1)
    _linkage = metrics_svc.expenditure_linkage_summary(db, [project.id]).get(project.id, {})
    if _linked_exp > 0:
        _linkage_status = "linked"
    elif (_rec_facts or {}).get("has_expenditure") and _wk_rows > 1:
        _linkage_status = "ambiguous_shared_work"
    else:
        _linkage_status = "no_linked_records"

    return {
        "project": {
            "id": project.id,
            "project_name": project.project_name,
            "state": project.state,
            "district": project.district,
            "constituency": project.constituency,
            "project_type": project.project_type,
            "status": project.status,
            "house": project.house,
            "sanctioned_amount": project.sanctioned_amount or 0.0,
            # Authoritative per-project spend: linked payment-ledger total
            # (project_rec_info). The catalog's column is a zeroed legacy stamp.
            "expenditure": _linked_exp,
            # Status distinction: 'linked' = verified payments attributed 1:1
            # (unique work key); 'ambiguous_shared_work' = verified payments
            # exist at work level but the work key is shared by multiple
            # catalog rows, so the total cannot be split per row;
            # 'no_linked_records' = no ledger payment passed verification —
            # NOT a confirmed ₹0 spend.
            "expenditure_status": _linkage_status,
            "work_key_expenditure": _wk_exp,
            "work_key_rows": _wk_rows,
            "expenditure_linkage": {
                "status": _linkage_status,
                "transactions": int(_linkage.get("transactions", 0) or 0),
                "first_expenditure_date": _linkage.get("first_expenditure_date"),
                "latest_expenditure_date": _linkage.get("latest_expenditure_date"),
                "work_key_expenditure": _wk_exp,
                "work_key_rows": _wk_rows,
                "note": (
                    "Spend comes from payment-ledger records verifiably linked to "
                    "this work (normalized work key + MP + IDA anchor)."
                    if _linkage_status == "linked"
                    else (
                        f"Payments totalling ₹{_wk_exp / 100000:.2f} L are recorded for this "
                        f"shared work description, but {_wk_rows} catalog rows share it, so the "
                        "total cannot be attributed to any single row."
                    )
                    if _linkage_status == "ambiguous_shared_work"
                    else "No ledger payment could be verifiably linked to this work, "
                    "so recorded spend is unknown — not confirmed zero."
                ),
            },
            "completion_percentage": project.completion_percentage or 0.0,
            "data_quality_flag": stale_check["flag"],
            "data_quality_reason": stale_check["reason"],
        },
        # Recommendation facts — same project_rec_info source of truth as the
        # Projects table and Risk Center (identical values everywhere).
        "rec": _rec_facts,
        "risk": risk_info,
        # Audit intelligence summary (same single-source functions used by the
        # Audit Priority list and the audit case, so the UI never recomputes it)
        "audit_intelligence": _audit_intelligence_summary(project, risk_info, linked_expenditure=_linked_exp),
    }


def _audit_intelligence_summary(project, risk_info: Dict[str, Any], linked_expenditure: Optional[float] = None) -> Dict[str, Any]:
    """Lightweight, consistent audit summary attached to the project detail.

    Uses the authoritative per-project spend (payment ledger via
    project_rec_info) — the catalog column is a zeroed legacy stamp."""
    try:
        risk_level = risk_info.get("risk_level")
        risk_score = risk_info.get("risk_score") or 0
        reasons = risk_info.get("reasons")
        if isinstance(reasons, str):
            reasons = [r.strip() for r in reasons.split(",") if r.strip()]
        reasons = reasons or []
        linked_exp = float(linked_expenditure or 0.0)
        stale = _check_stale_progress(project, linked_expenditure=linked_exp)
        checklist = audit_intel.build_checklist(project, reasons, stale["flag"], linked_expenditure=linked_exp)
        return {
            "priority": audit_intel.priority_breakdown(
                project, risk_score, risk_level=risk_level, linked_expenditure=linked_exp
            ),
            "financial_exposure": audit_intel.financial_exposure(project, linked_expenditure=linked_exp),
            "evidence_gap": audit_intel.evidence_gap_summary(project),
            "recommended_actions": audit_intel.recommended_actions(checklist),
            "data_quality": {
                "flag": stale["flag"],
                "reason": stale["reason"],
            },
            "review_level": audit_intel.review_level(
                project,
                int(risk_score or 0),
                bool((project.sanctioned_amount or 0) > 0 and linked_exp > (project.sanctioned_amount or 0)),
            ),
        }
    except Exception as exc:  # never break the detail endpoint
        return {"error": f"Audit summary unavailable: {exc}"}


@app.get("/projects/{project_id}/timeline", tags=["Projects"])
def get_project_timeline_endpoint(
    project_id: int = Path(..., description="The ID of the project"),
):
    """
    Project lifecycle timeline + delay intelligence.

    Events are built by conservatively matching work-level datasets
    (recommended_works, expenditures, completed_works) to the project using
    exact normalized (description, constituency, state) keys. Only events
    supported by actual recorded data are returned — missing lifecycle
    stages are omitted, never estimated.
    """
    from timeline import get_project_timeline

    payload = get_project_timeline(project_id)
    if payload is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project with ID {project_id} not found"
        )
    return payload


@app.get("/projects/{project_id}/expenditure-activity", tags=["Projects"])
def get_project_expenditure_activity_endpoint(
    project_id: int = Path(..., description="The ID of the project"),
    db: Session = Depends(get_db),
):
    """
    Financial activity history + activity-gap intelligence for one project.

    Records are matched using the same conservative matching as the timeline
    (exact normalized description + constituency + state, MP-verified).
    Gap thresholds: >=90 days long_gap, >=180 extended_gap (returned in the
    response `thresholds` block). All dates come from recorded expenditure
    data only.
    """
    from activity import get_expenditure_activity

    project = db.query(models.Project).filter(models.Project.id == project_id).first()
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project with ID {project_id} not found"
        )
    return get_expenditure_activity(project)


# =========================================================
# DASHBOARD AGGREGATIONS (HIGH PERFORMANCE)
# =========================================================

@app.get("/dashboard/overview", tags=["Dashboard"])
def dashboard_overview(
    fy: Optional[str] = Query(None, description="Filter by financial year"),
    house: Optional[str] = Query(None, description="House: 'Lok Sabha', 'Rajya Sabha', or omitted for All"),
    db: Session = Depends(get_db)
):
    house_norm = house if house in ("Lok Sabha", "Rajya Sabha") else None
    cache_key = f"dashboard_overview_{fy or 'all'}_{house_norm or 'all'}"
    cached = get_cached(cache_key, ttl_seconds=60)
    if cached is not None:
        return cached

    # Use warmed cache for the common all-FY/all-house case
    if not fy and not house_norm:
        warmed = get_cached("dashboard_overview_all", ttl_seconds=600)
        if warmed is not None:
            set_cached(cache_key, warmed)
            return warmed

    # Authoritative aggregates (metrics_svc): sanctioned from the project
    # catalog, expenditure/completions from the payment/completions ledgers.
    result = metrics_svc.portfolio_overview(db, fy=fy, house=house_norm)

    set_cached(cache_key, result)
    return result


@app.get("/dashboard/states", tags=["Dashboard"])
def dashboard_states(
    fy: Optional[str] = Query(None, description="Filter by financial year"),
    house: Optional[str] = Query(None, description="House: 'Lok Sabha', 'Rajya Sabha', or omitted for All"),
    db: Session = Depends(get_db)
):
    house_norm = house if house in ("Lok Sabha", "Rajya Sabha") else None
    cache_key = f"dashboard_states_{fy or 'all'}_{house_norm or 'all'}"
    cached = get_cached(cache_key, ttl_seconds=120)
    if cached is not None:
        return cached

    base_q = db.query(models.Project)
    if fy:
        base_q = base_q.filter(models.Project.fy == fy)
    if house_norm:
        base_q = base_q.filter(models.Project.house == house_norm)

    rows = (
        base_q.with_entities(
            models.Project.state,
            func.count(models.Project.id).label("total_projects"),
            func.sum(case((models.Project.status.ilike("completed"), 1), else_=0)).label("completed_projects"),
            func.sum(case((models.Project.status.ilike("ongoing"), 1), else_=0)).label("ongoing_projects"),
            func.coalesce(func.sum(models.Project.sanctioned_amount), 0).label("total_sanctioned_amount"),
            func.coalesce(func.avg(models.Project.completion_percentage), 0).label("avg_completion")
        )
        .filter(models.Project.state.isnot(None), models.Project.state != "")
        .group_by(models.Project.state)
        .order_by(func.count(models.Project.id).desc())
        .all()
    )

    # Authoritative expenditure: payment ledger grouped by its own state column
    # (state labels verified to match the project catalog 1:1). House-scoped
    # via the ledger's own house column when requested.
    ledger_by_state = metrics_svc.state_ledger_map(db, fy=fy, house=house_norm)
    # MP+IDA-verified project linkage, deduplicated by work key — the
    # attributable subset of the ledger (the remainder cannot be verifiably
    # joined to catalog projects; surfaced so the UI can disclose the gap).
    linked_by_state = metrics_svc.state_linked_map(db, fy=fy)

    result = []
    for row in rows:
        sanctioned = float(row.total_sanctioned_amount)
        led = ledger_by_state.get(row.state, {})
        spent = float(led.get("expenditure", 0.0))
        utilization = (spent / sanctioned * 100) if sanctioned > 0 else 0.0
        lk = linked_by_state.get(row.state, {})
        linked_total = float(lk.get("linked_total", 0.0))
        work_verified = float(lk.get("work_verified_total", 0.0))

        result.append({
            "state": row.state,
            "total_projects": int(row.total_projects),
            "completed_projects": int(row.completed_projects),
            "ongoing_projects": int(row.ongoing_projects),
            "total_sanctioned_amount": sanctioned,
            "total_expenditure": spent,
            "utilization_percentage": round(utilization, 2),
            "average_completion_percentage": round(float(row.avg_completion), 2),
            "project_linked_expenditure": linked_total,
            "work_verified_expenditure": work_verified,
            "projects_with_linked_expenditure": int(lk.get("linked_rows", 0)),
            "linked_work_keys": int(lk.get("linked_keys", 0)),
            "unique_work_keys": int(lk.get("unique_keys", 0)),
            "unattributed_expenditure": round(spent - work_verified, 2),
        })

    set_cached(cache_key, result)
    return result


@app.get("/dashboard/state-map", tags=["Dashboard"])
def dashboard_state_map(
    fy: Optional[str] = Query(None, description="Filter by financial year"),
    house: Optional[str] = Query(None, description="House: 'Lok Sabha', 'Rajya Sabha', or omitted for All"),
    db: Session = Depends(get_db),
):
    """State-level aggregates for the Overview India choropleth.

    One row per state/UT (all 36 are returned so the map can distinguish a
    recorded zero from an unavailable value). Metrics reuse the project
    catalog (projects, sanctioned amount, recorded completion status), the
    payment ledger (expenditure; every payment row counted once) and the
    stored risk_scores table (high-risk count). No project-level rows are
    sent to the browser.

    State labels are normalised through state_geo.py and returned with their
    ISO 3166-2 code; the frontend joins that code to the boundary GeoJSON.
    Records whose state label cannot be matched (including NULL/blank) are
    returned separately instead of being silently dropped.
    """
    house_norm = house if house in ("Lok Sabha", "Rajya Sabha") else None
    cache_key = f"state_map_{fy or 'all'}_{house_norm or 'all'}"
    cached = get_cached(cache_key, ttl_seconds=120)
    if cached is not None:
        return cached

    def _bucket(raw_state):
        key = (raw_state or "").strip() or "Unknown"
        bucket = buckets.get(key)
        if bucket is None:
            bucket = {
                "raw": key,
                "total_projects": 0,
                "completed_projects": 0,
                "ongoing_projects": 0,
                "sanctioned_amount": 0.0,
                "completion_sum": 0.0,
                "has_projects": False,
                "total_expenditure": 0.0,
                "transactions": 0,
                "has_expenditure": False,
                "high_risk_projects": 0,
                "risk_scored_projects": 0,
            }
            buckets[key] = bucket
        return bucket

    buckets: Dict[str, Dict[str, Any]] = {}

    # 1. Project catalog — one grouped query. COUNT(id) counts catalog rows
    # once; the completed CASE is over the same rows (no join fan-out).
    project_q = db.query(
        models.Project.state.label("state"),
        func.count(models.Project.id).label("total_projects"),
        func.sum(case(
            (func.lower(models.Project.status) == "completed", 1), else_=0
        )).label("completed_projects"),
        func.sum(case(
            (func.lower(models.Project.status) == "ongoing", 1), else_=0
        )).label("ongoing_projects"),
        func.coalesce(
            func.sum(models.Project.sanctioned_amount), 0.0
        ).label("sanctioned_amount"),
        func.coalesce(
            func.sum(models.Project.completion_percentage), 0.0
        ).label("completion_sum"),
    )
    if fy:
        project_q = project_q.filter(models.Project.fy == fy)
    if house_norm:
        project_q = project_q.filter(models.Project.house == house_norm)
    for row in project_q.group_by(models.Project.state).all():
        bucket = _bucket(row.state)
        bucket["total_projects"] += int(row.total_projects or 0)
        bucket["completed_projects"] += int(row.completed_projects or 0)
        bucket["ongoing_projects"] += int(row.ongoing_projects or 0)
        bucket["sanctioned_amount"] += float(row.sanctioned_amount or 0.0)
        bucket["completion_sum"] += float(row.completion_sum or 0.0)
        bucket["has_projects"] = True

    # 2. Payment ledger — the authoritative expenditure source. Grouped by
    # the ledger's own state label and scoped by its own house column; each
    # payment row appears once in exactly one group.
    ledger_by_state = metrics_svc.state_ledger_map(
        db, fy=fy, house=house_norm
    )
    for raw_state, values in ledger_by_state.items():
        bucket = _bucket(raw_state)
        bucket["total_expenditure"] += float(values.get("expenditure") or 0.0)
        bucket["transactions"] += int(values.get("transactions") or 0)
        bucket["has_expenditure"] = True

    # 3. Stored risk results — reuse the existing risk_scores table. The
    # join is one-to-one on project_id, so project counts cannot fan out.
    risk_q = (
        db.query(
            models.Project.state.label("state"),
            func.count(models.RiskScore.id).label("scored"),
            func.sum(case(
                (func.lower(models.RiskScore.risk_level) == "high", 1),
                else_=0,
            )).label("high_risk"),
        )
        .join(
            models.RiskScore,
            models.RiskScore.project_id == models.Project.id,
        )
    )
    if fy:
        risk_q = risk_q.filter(models.Project.fy == fy)
    if house_norm:
        risk_q = risk_q.filter(models.Project.house == house_norm)
    for row in risk_q.group_by(models.Project.state).all():
        bucket = _bucket(row.state)
        bucket["risk_scored_projects"] += int(row.scored or 0)
        bucket["high_risk_projects"] += int(row.high_risk or 0)

    # 4. Which states have ever appeared in the expenditure ledger? A state
    # that is absent from the ledger entirely has an UNKNOWN spend, while a
    # state present in the ledger but with no rows in the selected FY/house
    # has a genuine recorded zero.
    ledger_codes = set()
    for (raw_state,) in (
        db.query(models.Expenditure.state)
        .filter(
            models.Expenditure.state.isnot(None),
            models.Expenditure.state != "",
        )
        .distinct()
        .all()
    ):
        code, _ = state_geo.normalize_state_label(raw_state)
        if code:
            ledger_codes.add(code)

    states_by_code: Dict[str, Dict[str, Any]] = {}
    for entry in state_geo.STATE_REGISTRY:
        code = str(entry["code"])
        states_by_code[code] = {
            "state_code": code,
            "state": str(entry["name"]),
            "display_name": str(entry["name"]),
            "drill_state": str(entry["name"]),
            "_drill_projects": -1,
            "total_projects": 0,
            "completed_projects": 0,
            "ongoing_projects": 0,
            "sanctioned_amount": 0.0,
            "completion_sum": 0.0,
            "average_completion_percentage": None,
            "completion_rate_percentage": None,
            "has_projects": False,
            "total_expenditure": 0.0,
            "transactions": 0,
            "has_expenditure": False,
            "high_risk_projects": 0,
            "risk_scored_projects": 0,
            "has_risk_scores": False,
        }

    unmatched = []
    for raw_state, bucket in buckets.items():
        code, canonical = state_geo.normalize_state_label(raw_state)
        if not code:
            unmatched.append({
                "state": raw_state,
                "reason": (
                    "missing_state"
                    if raw_state == "Unknown"
                    else "unmatched_state_name"
                ),
                "total_projects": (
                    int(bucket["total_projects"])
                    if bucket["has_projects"] else None
                ),
                "total_expenditure": (
                    round(float(bucket["total_expenditure"]), 2)
                    if bucket["has_expenditure"] else None
                ),
                "completed_projects": (
                    int(bucket["completed_projects"])
                    if bucket["has_projects"] else None
                ),
                "high_risk_projects": (
                    int(bucket["high_risk_projects"])
                    if bucket["risk_scored_projects"] else None
                ),
                "risk_scored_projects": int(bucket["risk_scored_projects"]),
            })
            continue

        state = states_by_code[code]
        state["total_projects"] += int(bucket["total_projects"])
        state["completed_projects"] += int(bucket["completed_projects"])
        state["ongoing_projects"] += int(bucket["ongoing_projects"])
        state["sanctioned_amount"] += float(bucket["sanctioned_amount"])
        state["completion_sum"] += float(bucket["completion_sum"])
        state["total_expenditure"] += float(bucket["total_expenditure"])
        state["transactions"] += int(bucket["transactions"])
        state["high_risk_projects"] += int(bucket["high_risk_projects"])
        state["risk_scored_projects"] += int(bucket["risk_scored_projects"])
        state["has_projects"] = state["has_projects"] or bucket["has_projects"]
        state["has_expenditure"] = (
            state["has_expenditure"] or bucket["has_expenditure"]
        )
        # Prefer the recorded spelling that represents the most catalog rows
        # so State Intelligence's existing state filter receives a label that
        # actually exists in its table.
        if raw_state != "Unknown" and (
            state["_drill_projects"] < 0
            or bucket["total_projects"] > state["_drill_projects"]
        ):
            state["drill_state"] = raw_state
            state["_drill_projects"] = int(bucket["total_projects"])

    for code, state in states_by_code.items():
        # A state present anywhere in the payment ledger gets a real zero
        # when the selected scope has no payments; a state never present in
        # the ledger keeps NULL (unavailable), never a fabricated zero.
        if code in ledger_codes:
            state["has_expenditure"] = True
        state["has_risk_scores"] = state["risk_scored_projects"] > 0

        completion_sum = float(state.pop("completion_sum", 0.0))
        if state["has_projects"]:
            state["total_projects"] = int(state["total_projects"])
            state["completed_projects"] = int(state["completed_projects"])
            state["ongoing_projects"] = int(state["ongoing_projects"])
            state["sanctioned_amount"] = round(
                float(state["sanctioned_amount"]), 2
            )
            state["average_completion_percentage"] = round(
                completion_sum / state["total_projects"], 2
            ) if state["total_projects"] else None
            state["completion_rate_percentage"] = round(
                state["completed_projects"] / state["total_projects"] * 100.0, 2
            ) if state["total_projects"] else None
        else:
            state["total_projects"] = None
            state["completed_projects"] = None
            state["ongoing_projects"] = None
            state["completion_rate_percentage"] = None
            state["sanctioned_amount"] = None

        if state["has_expenditure"]:
            state["total_expenditure"] = round(
                float(state["total_expenditure"]), 2
            )
        else:
            state["total_expenditure"] = None
            state["transactions"] = 0

        state["high_risk_projects"] = (
            int(state["high_risk_projects"])
            if state["has_risk_scores"] else None
        )
        state["risk_coverage_percentage"] = (
            round(
                state["risk_scored_projects"] / state["total_projects"] * 100.0,
                2,
            )
            if state["has_projects"] and state["total_projects"]
            else None
        )

        if (
            state["has_projects"]
            and state["has_expenditure"]
            and state["sanctioned_amount"]
            and state["sanctioned_amount"] > 0
        ):
            state["fund_utilization_percentage"] = round(
                float(state["total_expenditure"])
                / float(state["sanctioned_amount"])
                * 100.0,
                2,
            )
        else:
            state["fund_utilization_percentage"] = None

        state["has_data"] = bool(
            state["has_projects"]
            or state["has_expenditure"]
            or state["has_risk_scores"]
        )
        state.pop("_drill_projects", None)

    unmatched.sort(
        key=lambda item: (item["total_projects"] or 0), reverse=True
    )
    states = list(states_by_code.values())

    result = {
        "scope": {
            "fy": fy or "All",
            "house": house_norm or "All",
        },
        "metric_definitions": {
            "total_projects": (
                "Works in the project catalog grouped by their recorded "
                "state label."
            ),
            "total_expenditure": (
                "Every payment in the expenditure ledger grouped by the "
                "ledger's recorded state label; each payment counted once."
            ),
            "fund_utilization": (
                "Ledger expenditure as a share of catalog sanctioned "
                "amount. Values above 100% reflect ledger payments that "
                "cannot be uniquely attributed to individual catalog works."
            ),
            "completed_projects": (
                "Project-catalog works whose recorded status is Completed."
            ),
            "high_risk_projects": (
                "Project-catalog works with a stored High risk level from "
                "the existing risk engine."
            ),
        },
        "states": states,
        "unmatched": unmatched,
        "meta": {
            "states_total": len(states),
            "states_with_data": sum(1 for s in states if s["has_data"]),
            "unmatched_labels": [u["state"] for u in unmatched],
            "unmatched_project_records": sum(
                int(u["total_projects"] or 0) for u in unmatched
            ),
            "unmatched_expenditure": round(sum(
                float(u["total_expenditure"] or 0.0) for u in unmatched
            ), 2),
            "note": (
                "State-level aggregates from recorded state labels; "
                "not GPS-based project locations."
            ),
        },
    }
    set_cached(cache_key, result)
    return result


@app.get("/dashboard/house", tags=["Dashboard"])
def dashboard_house(
    house: str = Query("Rajya Sabha", description="House: 'Rajya Sabha', 'Lok Sabha', or 'All'"),
    fy: Optional[str] = Query(None, description="Filter by financial year"),
    db: Session = Depends(get_db)
):
    """Official eSAKSHI-style per-house dashboard metrics.

    Mirrors the official MPLADS dashboard's per-house summary (allocated,
    expenditure, utilization, expenditure rate, MPs, completed/pending
    works, ongoing-work payments) from the authoritative ledgers, with the
    metric definitions documented in metrics_svc.house_dashboard.
    house='All' combines the two per-house reference snapshots (disjoint
    populations — amounts summed, rates recomputed allocation-weighted).
    """
    house_norm = house.strip()
    if house_norm not in ("All", "Rajya Sabha", "Lok Sabha"):
        raise HTTPException(status_code=422, detail="house must be 'All', 'Rajya Sabha' or 'Lok Sabha'")

    cache_key = f"dashboard_house_{house_norm}_{fy or 'all'}"
    cached = get_cached(cache_key, ttl_seconds=120)
    if cached is not None:
        return cached

    result = metrics_svc.house_dashboard(db, house=house_norm, fy=fy)
    set_cached(cache_key, result)
    return result


# =========================================================
# MP INTELLIGENCE — MP-level work/financial views (All/LS/RS)
# =========================================================

@app.get("/mp-intelligence", tags=["MP Intelligence"])
def mp_intelligence_list(
    house: Optional[str] = Query(None, description="House: 'Lok Sabha', 'Rajya Sabha', or omitted for All"),
    state: Optional[str] = Query(None, description="Filter by state"),
    q: Optional[str] = Query(None, description="Search by MP name, constituency, or state"),
    sort_by: str = Query("mp_name", description="mp_name | allocated_amount | total_expenditure | utilization_percentage | completed_works | recommended_works | completion_rate_percentage"),
    sort_dir: str = Query("asc", description="asc | desc"),
    limit: int = Query(300, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
):
    """MP list + portfolio summary, reusing the authoritative MPSummary
    fields. Identity is the MPSummary row (MP+House+Constituency) — never a
    name alone — so Lok Sabha and Rajya Sabha MPs can never merge."""
    return mp_intel.list_mp_intelligence(
        db, house=house, state=state, q=q,
        sort_by=sort_by, sort_dir=sort_dir, limit=limit, offset=offset,
    )


@app.get("/mp-intelligence/{mp_id}", tags=["MP Intelligence"])
def mp_intelligence_detail(
    mp_id: int,
    works_limit: int = Query(400, ge=1, le=2000),
    db: Session = Depends(get_db),
):
    """Full MP profile: profile, financial/work/completion summaries, the
    MP's work-level ledger rows (recommendations enriched with completions
    and payments via the platform's normkey work-key linkage), category
    distribution and an honest unlinked-payments data-quality note."""
    detail = mp_intel.mp_detail(db, mp_id, include_works_limit=works_limit)
    if detail is None:
        raise HTTPException(status_code=404, detail="MP not found")
    return detail


@app.get("/dashboard/mps", tags=["Dashboard"])
def dashboard_mps(
    state: Optional[str] = None,
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db)
):
    query = db.query(models.MPSummary)
    if state:
        query = query.filter(models.MPSummary.state == state)

    rows = query.limit(limit).all()
    return [
        {
            "id": row.id,
            "mp_name": row.mp_name,
            "state": row.state,
            "constituency": row.constituency,
            "house": row.house,
            "allocated_amount": row.allocated_amount or 0.0,
            "total_expenditure": row.total_expenditure or 0.0,
            "utilization_percentage": row.utilization_percentage or 0.0,
            "completed_works": row.completed_works or 0,
            "recommended_works": row.recommended_works or 0,
            "completion_rate_percentage": row.completion_rate_percentage or 0.0,
            "unspent_amount": row.unspent_amount or 0.0,
            "transaction_count": row.transaction_count or 0,
            "successful_payments": row.successful_payments or 0,
            "pending_payments": row.pending_payments or 0,
            "average_rating": row.average_rating or 0.0
        }
        for row in rows
    ]


@app.get("/dashboard/anomalies-summary", tags=["Dashboard"])
def anomalies_summary(
    fy: Optional[str] = Query(None, description="Filter by financial year"),
    house: Optional[str] = Query(None, description="House: 'Lok Sabha', 'Rajya Sabha', or omitted for All"),
    db: Session = Depends(get_db)
):
    house_norm = house if house in ("Lok Sabha", "Rajya Sabha") else None
    cache_key = f"anomalies_summary_{fy or 'all'}_{house_norm or 'all'}"
    cached = get_cached(cache_key, ttl_seconds=300)
    if cached is not None:
        return cached

    # Use cached risk table presence (avoids COUNT(*) every call)
    has_risk_table = _cache.get("_has_risk_table", False)
    if has_risk_table:
        # Single consolidated GROUP BY query instead of 3 separate COUNT + IN subquery
        base_q = (
            db.query(models.RiskScore.risk_level, func.count(models.RiskScore.id).label("cnt"))
            .join(models.Project, models.RiskScore.project_id == models.Project.id)
        )
        if fy:
            base_q = base_q.filter(models.Project.fy == fy)
        if house_norm:
            base_q = base_q.filter(models.Project.house == house_norm)
        rows = base_q.group_by(models.RiskScore.risk_level).all()

        level_map = {"high": 0, "medium": 0, "low": 0}
        for row in rows:
            key = (row.risk_level or "").lower()
            if key in level_map:
                level_map[key] = row.cnt

        total_q = db.query(func.count(models.Project.id))
        if fy:
            total_q = total_q.filter(models.Project.fy == fy)
        if house_norm:
            total_q = total_q.filter(models.Project.house == house_norm)
        total_checked = total_q.scalar() or 0

        result = {
            "total_projects_checked": total_checked,
            "high_risk": level_map["high"],
            "medium_risk": level_map["medium"],
            "low_risk": level_map["low"],
            "total_anomalies": level_map["high"] + level_map["medium"] + level_map["low"]
        }
        set_cached(cache_key, result)
        return result

    # Heuristic calculation — consolidated into fewer queries
    base_q = db.query(models.Project)
    if fy:
        base_q = base_q.filter(models.Project.fy == fy)
    if house_norm:
        base_q = base_q.filter(models.Project.house == house_norm)

    # Single pass with CASE WHEN to count all categories at once.
    # Spend conditions use the authoritative linked expenditure
    # (project_rec_info); the catalog's expenditure column is a zeroed
    # legacy stamp and would never trigger these rules.
    stats = (
        base_q.outerjoin(
            models.ProjectRecInfo,
            models.ProjectRecInfo.project_id == models.Project.id,
        )
        .with_entities(
            func.count(models.Project.id).label("total"),
            func.sum(case((
                (func.coalesce(models.ProjectRecInfo.linked_expenditure, 0.0) > models.Project.sanctioned_amount) |
                ((models.Project.sanctioned_amount >= 1000000) & (models.Project.completion_percentage < 25)),
                1), else_=0)).label("high"),
            func.sum(case((
                (func.coalesce(models.ProjectRecInfo.linked_expenditure, 0.0) > 0) & (models.Project.completion_percentage == 0),
                1), else_=0)).label("medium"),
        )
        .one()
    )

    total_projects = int(stats.total or 0)
    high_risk = int(stats.high or 0)
    medium_risk = int(stats.medium or 0)
    low_risk = max(0, int(total_projects * 0.02))

    result = {
        "total_projects_checked": total_projects,
        "high_risk": high_risk,
        "medium_risk": medium_risk,
        "low_risk": low_risk,
        "total_anomalies": high_risk + medium_risk + low_risk
    }
    set_cached(cache_key, result)
    return result


@app.post("/dashboard/anomalies-summary/refresh", tags=["Dashboard"])
def refresh_anomalies_summary():
    clear_cache()
    return {"message": "All dashboard and anomaly caches cleared successfully"}


@app.get("/dashboard/early-warning", tags=["Dashboard"])
def dashboard_early_warning(
    fy: Optional[str] = Query(None, description="Filter by financial year"),
    house: Optional[str] = Query(None, description="House: 'Lok Sabha', 'Rajya Sabha', or omitted for All"),
    db: Session = Depends(get_db),
):
    """Portfolio early-warning summary + FY trends — all SQL-side aggregates.

    Early-warning states are DERIVED from the existing risk-engine outputs
    (risk_scores table) plus directly recorded fields — no separate risk
    calculation:
      🔴 Critical   — risk_level 'High'
      🟠 Early Warning — risk_level 'Medium'
      🟡 Watch      — expenditure recorded with 0% physical completion,
                      or sanctioned ≥ ₹10 L with completion < 25%
      🟢 Normal     — everything else
    Bands overlap by design (watch = pre-risk leading indicator).
    Trends use the fy column and expenditure dates already in the dataset.
    """
    house_norm = house if house in ("Lok Sabha", "Rajya Sabha") else None
    cache_key = f"early_warning_{fy or 'all'}_{house_norm or 'all'}"
    cached = get_cached(cache_key, ttl_seconds=300)
    if cached is not None:
        return cached

    fy_filter = "AND p.fy = :fy" if fy else ""
    house_filter = "AND p.house = :house" if house_norm else ""
    params = {"fy": fy} if fy else {}
    if house_norm:
        params["house"] = house_norm

    # ── 1. Warning bands (single pass over projects LEFT JOIN risk) ──
    # Spend-with-no-progress uses the derived project_rec_info linkage
    # (has_expenditure = ledger payments linked to the work), because the
    # catalog's per-project expenditure column is a zeroed legacy stamp.
    bands = db.execute(text(f"""
        SELECT
          SUM(CASE WHEN rs.risk_level = 'High' THEN 1 ELSE 0 END)                AS critical,
          SUM(CASE WHEN rs.risk_level = 'Medium' THEN 1 ELSE 0 END)              AS early_warning,
          SUM(CASE WHEN (rs.risk_level IS NULL OR rs.risk_level NOT IN ('High','Medium'))
                    AND (
                      (pri.has_expenditure = 1 AND COALESCE(p.completion_percentage, 0) = 0)
                      OR (p.sanctioned_amount >= 1000000 AND COALESCE(p.completion_percentage, 0) < 25)
                    )
                  THEN 1 ELSE 0 END)                                            AS watch,
          COUNT(*)                                                              AS total,
          SUM(CASE WHEN p.status IN ('Completed', 'completed') THEN 1 ELSE 0 END) AS completed,
          SUM(CASE WHEN p.status NOT IN ('Completed', 'completed') OR p.status IS NULL THEN 1 ELSE 0 END) AS ongoing,
          SUM(CASE WHEN rs.risk_level IS NOT NULL THEN 1 ELSE 0 END)             AS scored,
          SUM(CASE WHEN rs.ml_anomaly = 1 THEN 1 ELSE 0 END)                     AS ml_anomalies
        FROM projects p
        LEFT JOIN risk_scores rs ON rs.project_id = p.id
        LEFT JOIN project_rec_info pri ON pri.project_id = p.id
        WHERE 1=1 {fy_filter} {house_filter}
    """), params).one()

    total = int(bands.total or 0)
    critical = int(bands.critical or 0)
    early = int(bands.early_warning or 0)
    watch = int(bands.watch or 0)
    normal = max(0, total - critical - early - watch)

    # ── 2. FY trends — authoritative source (metrics_svc): catalog counts +
    #       sanctioned by catalog fy; expenditure + completions from the
    #       payment/completions ledgers grouped by payment/completion-date FY.
    #       Same numbers power the FY tables everywhere; no other aggregation. ──
    trend_rows = metrics_svc.fy_trends(db, house=house_norm)

    # ── 3. Monthly expenditure trend from the expenditure ledger (dates exist
    #       there; project rows don't carry spend dates). Compact: ≤ 24 rows. ──
    monthly_sql = """
        SELECT substr(e.expenditure_date, 1, 7) AS ym,
               COALESCE(SUM(e.expenditure_amount), 0) AS amount,
               COUNT(*) AS tx
        FROM expenditures e
        WHERE e.expenditure_date IS NOT NULL AND length(e.expenditure_date) >= 7
          {house_filter}
        GROUP BY substr(e.expenditure_date, 1, 7)
        ORDER BY ym
        LIMIT 36
    """.format(house_filter="AND e.house = :house" if house_norm else "")
    monthly = db.execute(text(monthly_sql), params).fetchall()

    # ── 4. Top early-warning states (most critical+early projects) ──
    hot_states = db.execute(text(f"""
        SELECT p.state,
               SUM(CASE WHEN rs.risk_level IN ('High','Medium') THEN 1 ELSE 0 END) AS flagged,
               COUNT(*) AS total
        FROM projects p
        LEFT JOIN risk_scores rs ON rs.project_id = p.id
        WHERE p.state IS NOT NULL {fy_filter} {house_filter}
        GROUP BY p.state
        ORDER BY flagged DESC, total DESC
        LIMIT 8
    """), params).fetchall()

    result = {
        "bands": {
            "normal": normal,
            "watch": watch,
            "early_warning": early,
            "critical": critical,
            "total": total,
        },
        "counts": {
            "completed": int(bands.completed or 0),
            "ongoing": int(bands.ongoing or 0),
            "delayed": watch,  # zero-progress-with-spend / stalled starts
            "high_risk": critical,
            "critical_risk": critical,
            "total_anomalies": int(bands.scored or 0),
            "ml_anomalies": int(bands.ml_anomalies or 0),
        },
        "fy_trends": [
            {
                "fy": r["fy"],
                "projects": int(r["projects"] or 0),
                "sanctioned": float(r["sanctioned"] or 0),
                "expenditure": float(r["expenditure"] or 0),
                "transactions": int(r.get("transactions") or 0),
                "completed": int(r["completed"] or 0),
                "completed_amount": float(r.get("completed_amount") or 0),
                "utilization_percentage": r.get("utilization_percentage"),
                "avg_risk": r.get("avg_risk"),
            }
            for r in trend_rows
        ],
        "monthly_expenditure": [
            {"month": r.ym, "amount": float(r.amount or 0), "transactions": int(r.tx or 0)}
            for r in monthly
        ],
        "hot_states": [
            {"state": r.state, "flagged": int(r.flagged or 0), "total": int(r.total or 0)}
            for r in hot_states
        ],
    }
    set_cached(cache_key, result)
    return result


@app.get("/dashboard/constituencies", tags=["Dashboard"])
def dashboard_constituencies(
    state: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db)
):
    query = (
        db.query(
            models.Project.constituency,
            models.Project.state,
            func.count(models.Project.id).label("total_projects"),
            func.coalesce(func.sum(models.Project.sanctioned_amount), 0).label("total_sanctioned_amount"),
            func.coalesce(func.avg(models.Project.completion_percentage), 0).label("avg_completion")
        )
        .filter(models.Project.constituency.isnot(None), models.Project.constituency != "")
    )
    if state:
        query = query.filter(models.Project.state == state)

    rows = query.group_by(models.Project.constituency, models.Project.state).order_by(func.count(models.Project.id).desc()).limit(limit).all()

    # Authoritative expenditure: the ledgers carry their own (state,
    # constituency) pair, verified to exist in the catalog 1:1 — merge
    # directly on the pair so shared labels never cross states.
    cons_ledger = metrics_svc.constituency_ledger_map(db)

    result = []
    for r in rows:
        sanctioned = float(r.total_sanctioned_amount)
        spent = float(
            cons_ledger.get(((r.state or "").strip(), (r.constituency or "").strip()), {}).get("expenditure", 0.0)
        )
        result.append({
            "constituency": r.constituency,
            "state": r.state,
            "total_projects": int(r.total_projects),
            "total_sanctioned_amount": sanctioned,
            "total_expenditure": spent,
            "utilization_percentage": round((spent / sanctioned * 100) if sanctioned > 0 else 0, 2),
            "average_completion_percentage": round(float(r.avg_completion), 2)
        })
    return result


@app.get("/dashboard/financials", tags=["Dashboard"])
def dashboard_financials(db: Session = Depends(get_db)):
    cached = get_cached("dashboard_financials", ttl_seconds=120)
    if cached is not None:
        return cached

    # Authoritative aggregates (metrics_svc): catalog sanctioned + ledger
    # expenditure/completions. Per-project ledger detail is unavailable (the
    # payment ledger does not join to the catalog), so highest/lowest project
    # spend and the catalog's per-project averages are honestly unavailable.
    led = metrics_svc.ledger_totals(db)
    sanctioned_stats = db.query(
        func.coalesce(func.sum(models.Project.sanctioned_amount), 0).label("total_sanctioned"),
        func.count(models.Project.id).label("total_projects"),
    ).one()

    total_sanctioned = float(sanctioned_stats.total_sanctioned)
    total_expenditure = float(led["total_expenditure"])
    unspent = max(0.0, total_sanctioned - total_expenditure)
    utilization = (total_expenditure / total_sanctioned * 100) if total_sanctioned > 0 else 0.0

    result = {
        "total_sanctioned_amount": total_sanctioned,
        "total_expenditure": total_expenditure,
        "expenditure_transactions": led["expenditure_transactions"],
        "completed_works": led["completed_works"],
        "completed_works_amount": led["completed_works_amount"],
        "unspent_amount": unspent,
        "utilization_percentage": round(utilization, 2),
        "average_project_expenditure": None,
        "highest_expenditure_project": None,
        "lowest_expenditure_project": None,
        "expenditure_scope": "payment_ledger",
        "per_project_note": "Per-project expenditure detail is unavailable: the transaction ledger cannot be reliably joined to individual recommended works.",
    }

    set_cached("dashboard_financials", result)
    return result


@app.get("/dashboard/project-types", tags=["Dashboard"])
def dashboard_project_types(db: Session = Depends(get_db)):
    cached = get_cached("dashboard_project_types", ttl_seconds=180)
    if cached is not None:
        return cached

    rows = (
        db.query(
            models.Project.project_type,
            func.count(models.Project.id).label("total_projects"),
            func.coalesce(func.sum(models.Project.sanctioned_amount), 0).label("total_sanctioned_amount"),
            func.coalesce(func.avg(models.Project.completion_percentage), 0).label("avg_completion")
        )
        .filter(models.Project.project_type.isnot(None), models.Project.project_type != "")
        .group_by(models.Project.project_type)
        .order_by(func.count(models.Project.id).desc())
        .all()
    )

    result = []
    for r in rows:
        sanctioned = float(r.total_sanctioned_amount)
        # Expenditure by work type is unavailable: the payment ledger has no
        # type column and cannot be joined to the catalog. Report None (the
        # frontend renders "Data unavailable"), not zero.
        result.append({
            "project_type": r.project_type,
            "total_projects": int(r.total_projects),
            "total_sanctioned_amount": sanctioned,
            "total_expenditure": None,
            "utilization_percentage": None,
            "average_completion_percentage": round(float(r.avg_completion), 2)
        })

    set_cached("dashboard_project_types", result)
    return result


# =========================================================
# MP SUMMARY, RECOMMENDED, COMPLETED & EXPENDITURES
# =========================================================

@app.get("/mp-summary", tags=["MP Operations"])
def get_mp_summary(
    state: Optional[str] = None,
    mp_name: Optional[str] = None,
    constituency: Optional[str] = None,
    skip: int = 0,
    limit: int = 200,
    db: Session = Depends(get_db)
):
    query = db.query(models.MPSummary)
    if state:
        query = query.filter(models.MPSummary.state == state)
    if mp_name:
        query = query.filter(models.MPSummary.mp_name.ilike(f"%{mp_name}%"))
    if constituency:
        query = query.filter(models.MPSummary.constituency.ilike(f"%{constituency}%"))

    return query.offset(skip).limit(limit).all()


@app.get("/recommended-works", tags=["MP Operations"])
def get_recommended_works(
    state: Optional[str] = None,
    constituency: Optional[str] = None,
    category: Optional[str] = None,
    mp_name: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db)
):
    query = db.query(models.RecommendedWork)
    if state:
        query = query.filter(models.RecommendedWork.state == state)
    if constituency:
        query = query.filter(models.RecommendedWork.constituency == constituency)
    if category:
        query = query.filter(models.RecommendedWork.category == category)
    if mp_name:
        query = query.filter(models.RecommendedWork.mp_name.ilike(f"%{mp_name}%"))

    return query.offset(skip).limit(limit).all()


@app.get("/expenditures", tags=["MP Operations"])
def get_expenditures(
    state: Optional[str] = None,
    constituency: Optional[str] = None,
    mp_name: Optional[str] = None,
    payment_status: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db)
):
    query = db.query(models.Expenditure)
    if state:
        query = query.filter(models.Expenditure.state == state)
    if constituency:
        query = query.filter(models.Expenditure.constituency == constituency)
    if mp_name:
        query = query.filter(models.Expenditure.mp_name.ilike(f"%{mp_name}%"))
    if payment_status:
        query = query.filter(models.Expenditure.payment_status.ilike(payment_status))

    return query.offset(skip).limit(limit).all()


# ═══════════════ VENDOR INTELLIGENCE ═══════════════════════════
# This view is deliberately derived only from the existing expenditure and
# project tables.  Vendor names are normalized conservatively (case, spaces,
# and punctuation only); project linkage uses a cautious location-scoped
# fallback for minor work-description wording differences.
def _vendor_key(value) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _project_work_similarity(left, right) -> float:
    """Score two work descriptions without allowing cross-location matches."""
    from difflib import SequenceMatcher

    left_key = _vendor_key(left)
    right_key = _vendor_key(right)
    if not left_key or not right_key:
        return 0.0
    left_tokens = set(left_key.split())
    right_tokens = set(right_key.split())
    overlap = len(left_tokens & right_tokens) / max(1, min(len(left_tokens), len(right_tokens)))
    sequence = SequenceMatcher(None, left_key, right_key).ratio()
    return max(sequence, (overlap * 0.65) + (sequence * 0.35))


def _match_project_work(description, constituency, state, project_by_key, project_by_location, project_token_index, matcher_cache=None):
    """Resolve a work description to one unambiguous project in its location.

    Semantically identical to scoring every candidate with
    _project_work_similarity, but exact fast paths keep large locations
    tractable:
      - SequenceMatcher caches its lookup table for seq2, so one matcher per
        project (reusing the table) with set_seq1 per query returns the same
        ratio() as constructing a fresh matcher for every pair.
      - real_quick_ratio()/quick_ratio() are documented upper bounds on
        ratio(), so candidates whose best possible score falls below the
        acceptance threshold - or below best - 0.08 once the best is known
        (the margin rule) - can be skipped without changing any outcome.
    """
    from difflib import SequenceMatcher

    key = (_vendor_key(description), _vendor_key(constituency), _vendor_key(state))
    exact = project_by_key.get(key, [])
    if exact:
        return exact
    if not key[0] or not key[1] or not key[2]:
        return []
    location_key = (key[1], key[2])
    candidate_ids = set()
    for token in set(key[0].split()):
        candidate_ids.update(project_token_index.get((location_key, token), set()))
    candidates = [project for project in project_by_location.get(location_key, []) if project["id"] in candidate_ids]
    if not candidates:
        return []

    left_key = key[0]
    left_tokens = set(left_key.split())
    threshold = 0.82

    def matcher_for(project):
        entry = matcher_cache.get(project["id"]) if matcher_cache is not None else None
        if entry is None:
            entry = SequenceMatcher(None, "", _vendor_key(project["project_name"]))
            if matcher_cache is not None:
                matcher_cache[project["id"]] = entry
        entry.set_seq1(left_key)
        return entry

    # Pass 1: cheap upper bounds. score = max(ratio, overlap*0.65 + ratio*0.35),
    # so the best attainable score uses quick_ratio as the ratio bound.
    live = []
    for project in candidates:
        matcher = matcher_for(project)
        right_tokens = set(_vendor_key(project["project_name"]).split())
        overlap = len(left_tokens & right_tokens) / max(1, min(len(left_tokens), len(right_tokens)))
        ub_ratio = matcher.real_quick_ratio()
        if ub_ratio < threshold and (overlap * 0.65 + ub_ratio * 0.35) < threshold:
            continue
        quick = matcher.quick_ratio()
        ub = max(quick, overlap * 0.65 + quick * 0.35)
        live.append((ub, overlap, project))
    if not live:
        return []

    # Pass 2: score candidates in descending upper-bound order, stopping as
    # soon as no remaining candidate can beat the current best or fall within
    # its 0.08 margin — the exact accept/reject decision of scoring every
    # candidate, without the cost.
    live.sort(key=lambda item: item[0], reverse=True)
    best_score = None
    best_project = None
    second_score = 0.0
    scored_count = 0
    for ub, overlap, project in live:
        # Nothing further can become an accepted best once the upper bounds
        # drop below the threshold, and the margin scan below cannot flip a
        # threshold-based rejection, so stop without scoring them.
        if best_score is None and ub < threshold:
            break
        if best_score is not None and best_score >= threshold and ub < best_score - 0.08:
            break
        ratio = matcher_for(project).ratio()
        score = max(ratio, overlap * 0.65 + ratio * 0.35)
        scored_count += 1
        if best_score is None or score > best_score:
            if best_score is not None:
                second_score = best_score
            best_score = score
            best_project = project
        elif score > second_score:
            second_score = score
    if best_score is not None and best_score >= threshold and (scored_count == 1 or best_score - second_score >= 0.08):
        return [best_project]
    return []


def _vendor_type(name: str):
    import re
    raw = str(name or "").strip()
    lower = raw.lower()
    if not raw:
        return "Unknown", "low"
    if re.search(r"\b(panchayat|municipal|municipality|nagarpalika|local body|gram sabha)\b", lower):
        return "Panchayat/Local Body", "medium"
    if re.search(r"\b(government|govt|department|ministry|pwd|rural development|zila parishad)\b", lower):
        return "Government Department", "medium"
    if re.search(r"\b(mr|mrs|ms|shri|smt|dr)\.?\s+", lower) and not re.search(r"\b(ltd|llp|pvt|company|enterprises?)\b", lower):
        return "Individual", "low"
    if re.search(r"\b(contractor|supplier|vendor|works|construction|infrastructure|traders?|agency)\b", lower):
        return "Supplier/Contractor", "medium"
    if re.search(r"\b(ltd|limited|llp|pvt|private|company|industries|enterprise|enterprises|corp|corporation)\b", lower):
        return "Company/Business", "medium"
    return "Other/Unknown", "low"


def _vendor_cache_ready(db: Session) -> bool:
    """True when the disk-backed vendor cache exists and matches the current
    expenditure dataset (row count + max(id) stamp). Rebuild validation is
    a single cheap aggregate — no Python-side data retention."""
    try:
        stamp = db.execute(text(
            f"SELECT stamp FROM {_vendor_cache_table}__meta WHERE id = 1"
        )).scalar()
    except Exception:
        return False
    if stamp is None:
        return False
    current = db.execute(text(
        "SELECT COUNT(*) || ':' || COALESCE(MAX(id), 0) FROM expenditures"
    )).scalar()
    return bool(stamp) and stamp == current


def _vendor_intelligence_payload(db: Session):
    """Ensure the disk-backed vendor cache is populated (build once per
    dataset version, guarded single-flight). Returns nothing — consumers
    query the cache table directly in SQL.

    Memory architecture (512MB Render instance): the original version
    streamed all 106k expenditure rows AND all 83k project rows through
    Python objects on every cold build (~285MB spike, then permanent heap).
    The SQL-aggregate rewrite still retained ~26k Python dicts (~40-60MB)
    in a process-lifetime cache. This version keeps only a bounded build
    batch (≤500 dicts, freed immediately after each INSERT batch); between
    requests the process retains nothing — rows live in the SQLite cache
    table on disk, and filtering/sorting/pagination run in SQL.
    """
    if _vendor_cache_ready(db):
        return
    global _vendor_build_in_progress
    with _vendor_build_lock:
        if _vendor_cache_ready(db):
            return
        _vendor_build_in_progress = True
        try:
            _build_vendor_intelligence_cache(db)
        finally:
            _vendor_build_in_progress = False


def _build_vendor_intelligence_cache(db: Session):
    """Build the vendor-intelligence aggregates into a disk-backed SQLite
    table. The ONLY Python objects materialized are one bounded batch of
    rows at a time (≤500 dicts, freed after each executemany) — the build
    peaks far lower than any in-memory variant and retains nothing.
    """
    import time

    build_started = time.time()

    # Stamp = expenditure dataset version: row count + max(id).
    stamp = db.execute(text(
        "SELECT COUNT(*) || ':' || COALESCE(MAX(id), 0) FROM expenditures"
    )).scalar()
    total = float(db.execute(text(
        "SELECT COALESCE(SUM(expenditure_amount), 0) FROM expenditures "
        "WHERE vendor IS NOT NULL AND trim(vendor) <> ''"
    )).scalar() or 0.0)
    linked_records = int(db.execute(text(
        "SELECT COUNT(*) FROM expenditures WHERE vendor IS NOT NULL AND trim(vendor) <> ''"
    )).scalar() or 0)

    db.execute(text(f"DROP TABLE IF EXISTS {_vendor_cache_table}"))
    db.execute(text(f"DROP TABLE IF EXISTS {_vendor_cache_table}__meta"))
    db.execute(text(f"""
        CREATE TABLE {_vendor_cache_table} (
            vendor_key TEXT PRIMARY KEY,
            vendor_name TEXT NOT NULL,
            vendor_type TEXT NOT NULL,
            type_confidence TEXT NOT NULL,
            project_count INTEGER NOT NULL,
            transaction_count INTEGER NOT NULL,
            total_expenditure REAL NOT NULL,
            expenditure_share REAL NOT NULL,
            linked_project_count INTEGER NOT NULL,
            high_risk_projects INTEGER NOT NULL,
            average_risk REAL NOT NULL,
            highest_risk REAL NOT NULL,
            risk_high INTEGER NOT NULL,
            risk_medium INTEGER NOT NULL,
            risk_low INTEGER NOT NULL,
            states_json TEXT NOT NULL,
            constituencies_json TEXT NOT NULL,
            search_text TEXT NOT NULL
        )
    """))

    # Linked-project/risk context for risk_counts and progress counts.
    # Small result (332 rows on this dataset) — materialized directly.
    linked = db.execute(text("""
        SELECT ve.vk, ve.wk, ve.ck, ve.sk,
               p.id, p.completion_percentage,
               p.sanctioned_amount,
               COALESCE(pri.linked_expenditure, 0) AS expenditure,
               rs.risk_score, rs.risk_level
        FROM (
            SELECT DISTINCT normkey(vendor) vk,
                            normkey(work_description) wk,
                            normkey(constituency) ck,
                            normkey(state) sk
            FROM expenditures
            WHERE vendor IS NOT NULL AND trim(vendor) <> ''
              AND work_description IS NOT NULL AND trim(work_description) <> ''
              AND constituency IS NOT NULL AND trim(constituency) <> ''
              AND state IS NOT NULL AND trim(state) <> ''
        ) ve
        JOIN projects p
          ON normkey(p.project_name) = ve.wk
         AND normkey(p.constituency) = ve.ck
         AND normkey(p.state) = ve.sk
        LEFT JOIN project_rec_info pri ON pri.project_id = p.id
        LEFT JOIN risk_scores rs ON rs.project_id = p.id
    """)).fetchall()
    linked_by_vendor: Dict[str, list] = {}
    for row in linked:
        linked_by_vendor.setdefault(row.vk, []).append(row)

    # Per-vendor aggregates with states/constituencies lists folded in via
    # GROUP_CONCAT(DISTINCT ...) — no per-vendor Python grouping structures.
    agg_sql = """
        SELECT
            normkey(vendor)                        AS vk,
            MAX(vendor)                            AS vendor_name,
            COUNT(*)                               AS transaction_count,
            ROUND(SUM(expenditure_amount), 2)      AS total_expenditure,
            COUNT(DISTINCT CASE WHEN wk <> '' AND ck <> '' AND sk <> ''
                                THEN wk || char(30) || ck || char(30) || sk END)
                                                   AS engagement_count,
            GROUP_CONCAT(DISTINCT CASE WHEN state IS NOT NULL AND trim(state) <> ''
                                       THEN trim(state) END)  AS states_raw,
            GROUP_CONCAT(DISTINCT CASE WHEN constituency IS NOT NULL AND trim(constituency) <> ''
                                       THEN trim(constituency) END) AS cons_raw
        FROM (
            SELECT vendor,
                   expenditure_amount,
                   normkey(work_description) AS wk,
                   normkey(constituency)     AS ck,
                   normkey(state)            AS sk,
                   trim(state)               AS state,
                   trim(constituency)        AS constituency
            FROM expenditures
            WHERE vendor IS NOT NULL AND trim(vendor) <> ''
        )
        GROUP BY vk
    """
    result = db.execute(text(agg_sql))

    batch: list = []
    def flush_batch():
        if batch:
            db.execute(
                text(f"""INSERT INTO {_vendor_cache_table}
                    (vendor_key, vendor_name, vendor_type, type_confidence,
                     project_count, transaction_count, total_expenditure,
                     expenditure_share, linked_project_count, high_risk_projects,
                     average_risk, highest_risk, risk_high, risk_medium, risk_low,
                     states_json, constituencies_json, search_text)
                    VALUES (:vendor_key, :vendor_name, :vendor_type, :type_confidence,
                            :project_count, :transaction_count, :total_expenditure,
                            :expenditure_share, :linked_project_count, :high_risk_projects,
                            :average_risk, :highest_risk, :risk_high, :risk_medium, :risk_low,
                            :states_json, :constituencies_json, :search_text)"""),
                batch,
            )
            batch.clear()

    for row in result.yield_per(2000):
        key = row.vk
        vendor_name = str(row.vendor_name or "").strip()
        vtype, vconf = _vendor_type(vendor_name)

        states_list = sorted({s.strip() for s in (row.states_raw or "").split(",") if s and s.strip()}) if row.states_raw else []
        const_list = sorted({s.strip() for s in (row.cons_raw or "").split(",") if s and s.strip()}) if row.cons_raw else []

        # Risk/progress context from the small linked-project set.
        risk_scores: list = []
        risk_counts = {"High": 0, "Medium": 0, "Low": 0}
        vendor_linked = linked_by_vendor.get(key, [])
        for proj in vendor_linked:
            level = str(proj.risk_level or "Low").title()
            level = level if level in ("High", "Medium", "Low") else "Low"
            risk_counts[level] += 1
            risk_scores.append(int(proj.risk_score or 0))

        total_exp = float(row.total_expenditure or 0)
        share = round((total_exp / total * 100) if total else 0, 2)
        batch.append({
            "vendor_key": key,
            "vendor_name": vendor_name,
            "vendor_type": vtype,
            "type_confidence": vconf,
            "project_count": int(row.engagement_count or 0),
            "transaction_count": int(row.transaction_count or 0),
            "total_expenditure": total_exp,
            "expenditure_share": share,
            "linked_project_count": len(vendor_linked),
            "high_risk_projects": risk_counts["High"],
            "average_risk": round(sum(risk_scores) / len(risk_scores), 1) if risk_scores else 0,
            "highest_risk": max(risk_scores) if risk_scores else 0,
            "risk_high": risk_counts["High"],
            "risk_medium": risk_counts["Medium"],
            "risk_low": risk_counts["Low"],
            "states_json": json.dumps(states_list, ensure_ascii=False),
            "constituencies_json": json.dumps(const_list, ensure_ascii=False),
            "search_text": " " + " ".join([vendor_name.lower(), *[s.lower() for s in states_list], *[c.lower() for c in const_list]]) + " ",
        })
        if len(batch) >= 500:
            flush_batch()

    flush_batch()
    db.execute(text(f"CREATE INDEX idx_vic_type ON {_vendor_cache_table}(vendor_type)"))
    db.execute(text(f"CREATE INDEX idx_vic_spend ON {_vendor_cache_table}(total_expenditure)"))
    db.execute(text(f"CREATE TABLE {_vendor_cache_table}__meta (id INTEGER PRIMARY KEY CHECK (id = 1), stamp TEXT NOT NULL)"))
    db.execute(text(f"INSERT INTO {_vendor_cache_table}__meta (id, stamp) VALUES (1, :stamp)"), {"stamp": stamp})
    db.commit()

    # Stats block: computed fresh from SQL on every page request (cheap
    # aggregates); the build itself persists nothing extra.
    print(
        f"[vendor-intelligence] cache table built for stamp {stamp} "
        f"in {time.time() - build_started:.1f}s",
        flush=True,
    )


def _vendor_profile_payload(db: Session, vendor_key: str):
    """Single-vendor profile: builds ONLY this vendor's data on demand.

    Previously the profile endpoint triggered the full 83k-project build so
    one drawer open cost the entire payload. Now: two bounded SQL queries
    (this vendor's engagement aggregates + this vendor's linked projects),
    one small risk lookup, cached per vendor key in the bounded intel cache
    (cleared on dataset sync). Peak memory: kilobytes per vendor."""
    cache_key = f"vendor_profile_{vendor_key}"
    cached = _intel_cache_get(cache_key)
    if cached is not None:
        return cached

    # Vendor identity + totals (single grouped row, SQL-side).
    identity = db.execute(text("""
        SELECT MAX(vendor) AS vendor_name,
               COUNT(*) AS transaction_count,
               ROUND(SUM(expenditure_amount), 2) AS total_expenditure
        FROM expenditures WHERE normkey(vendor) = :k
    """), {"k": vendor_key}).fetchone()
    if identity is None or not identity.vendor_name:
        raise HTTPException(status_code=404, detail="Vendor not found in the current MPLADS dataset")

    vendor_name = str(identity.vendor_name or "").strip()
    vtype, vconf = _vendor_type(vendor_name)
    total = db.execute(
        text("SELECT COALESCE(SUM(expenditure_amount), 0) FROM expenditures "
             "WHERE vendor IS NOT NULL AND trim(vendor) <> ''")
    ).scalar() or 0.0
    total = float(total)
    vendor_total = float(identity.total_expenditure or 0)
    transaction_count = int(identity.transaction_count or 0)

    # Distinct work engagements for THIS vendor, with per-engagement totals.
    engagements_rows = db.execute(text("""
        SELECT normkey(work_description) wk,
               normkey(constituency) ck,
               normkey(state) sk,
               MAX(trim(work_description)) AS work_description,
               MAX(trim(constituency)) AS constituency,
               MAX(trim(state)) AS state,
               COUNT(*) AS tx,
               ROUND(SUM(expenditure_amount), 2) AS amount
        FROM expenditures
        WHERE normkey(vendor) = :k
          AND work_description IS NOT NULL AND trim(work_description) <> ''
          AND constituency IS NOT NULL AND trim(constituency) <> ''
          AND state IS NOT NULL AND trim(state) <> ''
        GROUP BY wk, ck, sk
        ORDER BY amount DESC
    """), {"k": vendor_key}).fetchall()

    # States/constituencies lists for the drawer header.
    states = [s for (s,) in db.execute(
        text("SELECT DISTINCT trim(state) FROM expenditures "
             "WHERE normkey(vendor) = :k AND state IS NOT NULL AND trim(state) <> ''"), {"k": vendor_key}
    ).fetchall()]
    constituencies = [c for (c,) in db.execute(
        text("SELECT DISTINCT trim(constituency) FROM expenditures "
             "WHERE normkey(vendor) = :k AND constituency IS NOT NULL AND trim(constituency) <> ''"), {"k": vendor_key}
    ).fetchall()]

    # Engagement triples WITHOUT complete location data still count toward
    # expenditure but cannot fabricate an engagement (same rule as the
    # aggregate builder).
    blank_key_totals = db.execute(text("""
        SELECT COUNT(*) AS tx, ROUND(SUM(expenditure_amount), 2) AS amount
        FROM expenditures
        WHERE normkey(vendor) = :k
          AND (work_description IS NULL OR trim(work_description) = ''
               OR constituency IS NULL OR trim(constituency) = ''
               OR state IS NULL OR trim(state) = '')
    """), {"k": vendor_key}).fetchone()

    # Linked real projects: exact work-key match for THIS vendor only.
    linked = db.execute(text("""
        SELECT ve.wk, ve.ck, ve.sk,
               p.id, p.project_name, p.state, p.district, p.constituency,
               p.project_type, p.sanctioned_amount,
               COALESCE(pri.linked_expenditure, 0) AS expenditure,
               p.completion_percentage, p.status,
               rs.risk_score, rs.risk_level
        FROM (
            SELECT DISTINCT normkey(work_description) wk,
                            normkey(constituency) ck,
                            normkey(state) sk
            FROM expenditures
            WHERE normkey(vendor) = :k
              AND work_description IS NOT NULL AND trim(work_description) <> ''
              AND constituency IS NOT NULL AND trim(constituency) <> ''
              AND state IS NOT NULL AND trim(state) <> ''
        ) ve
        JOIN projects p
          ON normkey(p.project_name) = ve.wk
         AND normkey(p.constituency) = ve.ck
         AND normkey(p.state) = ve.sk
        LEFT JOIN project_rec_info pri ON pri.project_id = p.id
        LEFT JOIN risk_scores rs ON rs.project_id = p.id
    """), {"k": vendor_key}).fetchall()

    linked_projects = []
    risk_scores = []
    risk_counts = {"High": 0, "Medium": 0, "Low": 0}
    progress_counts = {"zero": 0, "low": 0, "completed": 0, "mismatch": 0}
    matched_ids: set = set()
    key_to_projects: Dict[tuple, list] = {}
    # Per-project spend from the payment ledger (conservative linkage), not
    # the catalog's zeroed legacy expenditure column.
    _proj_exp = metrics_svc.project_expenditure_map(db)
    for row in linked:
        if row.id in matched_ids:
            continue
        matched_ids.add(row.id)
        score = int(row.risk_score or 0)
        level = str(row.risk_level or "Low").title()
        level = level if level in ("High", "Medium", "Low") else "Low"
        risk_counts[level] += 1
        risk_scores.append(score)
        completion = float(row.completion_percentage or 0)
        expenditure = float(_proj_exp.get(row.id, 0.0))
        sanctioned = float(row.sanctioned_amount or 0)
        if completion <= 0:
            progress_counts["zero"] += 1
        elif completion < 50:
            progress_counts["low"] += 1
        elif completion >= 100:
            progress_counts["completed"] += 1
        if (sanctioned > 0 and expenditure > sanctioned) or (expenditure > 0 and completion <= 0):
            progress_counts["mismatch"] += 1
        linked_projects.append({
            "id": row.id,
            "project_name": row.project_name,
            "state": row.state,
            "district": row.district,
            "constituency": row.constituency,
            "project_type": row.project_type,
            "sanctioned_amount": row.sanctioned_amount or 0,
            "expenditure": _proj_exp.get(row.id, 0.0),
            "completion_percentage": row.completion_percentage or 0,
            "status": row.status,
            "risk": {"score": score, "level": level},
        })
        key_to_projects.setdefault((row.wk, row.ck, row.sk), []).append(row.id)

    engagements = []
    for row in engagements_rows:
        engagements.append({
            "work_description": row.work_description,
            "constituency": row.constituency,
            "state": row.state,
            "transaction_count": int(row.tx or 0),
            "total_amount": round(float(row.amount or 0), 2),
            "project_ids": key_to_projects.get((row.wk, row.ck, row.sk), []),
        })
    if blank_key_totals and int(blank_key_totals.tx or 0) > 0:
        engagements.append({
            "work_description": "(Expenditure records without complete work/location data)",
            "constituency": "—",
            "state": "—",
            "transaction_count": int(blank_key_totals.tx or 0),
            "total_amount": round(float(blank_key_totals.amount or 0), 2),
            "project_ids": [],
        })
    engagement_count = len(engagements_rows)

    payload = {
        "normalized_name": vendor_key,
        "vendor_name": vendor_name,
        "raw_names": [vendor_name],
        "type": vtype,
        "type_confidence": vconf,
        "project_count": engagement_count,
        "engagement_count": engagement_count,
        "linked_project_count": len(matched_ids),
        "transaction_count": transaction_count,
        "total_expenditure": round(vendor_total, 2),
        "states": sorted(states),
        "constituencies": sorted(constituencies),
        "high_risk_projects": risk_counts["High"],
        "risk_counts": risk_counts,
        "average_risk": round(sum(risk_scores) / len(risk_scores), 1) if risk_scores else 0,
        "highest_risk": max(risk_scores) if risk_scores else 0,
        "progress_counts": progress_counts,
        "expenditure_share": round((vendor_total / total * 100) if total else 0, 2),
        "engagements": engagements,
        "projects": linked_projects,
    }
    _intel_cache_set(cache_key, payload)
    return payload


@app.get("/vendor-intelligence", tags=["Vendor Intelligence"])
def get_vendor_intelligence(
    q: Optional[str] = Query(None, description="Search vendor name or state"),
    vendor_type: Optional[str] = Query(None, description="Vendor classification"),
    sort_by: str = Query("total_expenditure"),
    sort_dir: str = Query("desc"),
    skip: int = Query(0, ge=0),
    limit: int = Query(250, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    """Vendor list served from the disk-backed cache table.

    Memory architecture: search (all tokens must match name/states/
    constituencies), vendor-type filtering, sorting and pagination all run
    in SQL against the cache table — the only Python objects materialized
    are the one page of rows returned to the client. Stats are cheap SQL
    aggregates over the full cache (same unfiltered population as before).
    """
    _vendor_intelligence_payload(db)  # build once per dataset version

    where: list = []
    params: Dict[str, Any] = {}
    needle = (q or "").strip().lower()
    if needle:
        # Same contract as before: every token must appear somewhere in
        # vendor name, states or constituencies (precomputed search_text).
        for i, word in enumerate(needle.split()):
            params[f"tok{i}"] = f"% {word} %"
            where.append(f"search_text LIKE :tok{i}")
    if vendor_type:
        params["vt"] = vendor_type
        where.append("vendor_type = :vt")
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    allowed_sort = {"vendor_name", "vendor_type", "project_count", "transaction_count",
                    "total_expenditure", "high_risk_projects", "average_risk", "expenditure_share"}
    # concentration_rank = global expenditure-rank (computed in SQL so the
    # cache table stays rank-free and rebuilds never need a second pass).
    sort_field = sort_by if sort_by in allowed_sort else "total_expenditure"
    if sort_field == "type":
        sort_field = "vendor_type"
    direction = "ASC" if str(sort_dir).lower() == "asc" else "DESC"

    # Identical list/query/sort/page requests are common while an analyst
    # scrolls or reopens the view. Cache the rendered page (bounded by the
    # shared response cache; cleared on every data mutation).
    cache_key = (
        f"vendor_intelligence_{needle}_{vendor_type or ''}_"
        f"{sort_field}_{direction}_{skip}_{limit}"
    )
    cached = get_cached(cache_key, ttl_seconds=120)
    if cached is not None:
        return cached

    total_vendors = int(db.execute(text(
        f"SELECT COUNT(*) FROM {_vendor_cache_table} {where_sql}"
    ), params).scalar() or 0)

    rows = db.execute(text(f"""
        SELECT * FROM (
            SELECT t.*, ROW_NUMBER() OVER (ORDER BY t.total_expenditure DESC) AS concentration_rank
            FROM {_vendor_cache_table} t
        ) ranked
        {where_sql}
        ORDER BY ranked.{sort_field} {direction}, ranked.vendor_key ASC
        LIMIT :lim OFFSET :skip
    """), {**params, "lim": limit, "skip": skip}).mappings().all()

    vendors = []
    for r in rows:
        vendors.append({
            "normalized_name": r["vendor_key"],
            "vendor_name": r["vendor_name"],
            "raw_names": [r["vendor_name"]],
            "type": r["vendor_type"],
            "type_confidence": r["type_confidence"],
            "project_count": r["project_count"],
            "engagement_count": r["project_count"],
            "linked_project_count": r["linked_project_count"],
            "transaction_count": r["transaction_count"],
            "total_expenditure": r["total_expenditure"],
            "states": json.loads(r["states_json"]),
            "constituencies": json.loads(r["constituencies_json"]),
            "high_risk_projects": r["high_risk_projects"],
            "risk_counts": {"High": r["risk_high"], "Medium": r["risk_medium"], "Low": r["risk_low"]},
            "average_risk": r["average_risk"],
            "highest_risk": r["highest_risk"],
            "expenditure_share": r["expenditure_share"],
            "concentration_rank": r["concentration_rank"],
        })

    # Stats over the full (unfiltered) cache population — pure SQL.
    srow = db.execute(text(f"""
        SELECT COUNT(*) AS n,
               COALESCE(SUM(CASE WHEN project_count > 1 THEN 1 ELSE 0 END), 0) AS multi,
               COALESCE(SUM(CASE WHEN high_risk_projects > 0 THEN 1 ELSE 0 END), 0) AS highrisk,
               COALESCE(MAX(expenditure_share), 0) AS max_share,
               COALESCE((SELECT SUM(expenditure_share) FROM (
                   SELECT expenditure_share FROM {_vendor_cache_table}
                   ORDER BY total_expenditure DESC LIMIT 5)), 0) AS top5,
               COALESCE((SELECT SUM(expenditure_share) FROM (
                   SELECT expenditure_share FROM {_vendor_cache_table}
                   ORDER BY total_expenditure DESC LIMIT 10)), 0) AS top10
        FROM {_vendor_cache_table}
    """)).mappings().one()

    stats = {
        "unique_vendors": int(srow["n"]),
        "vendor_linked_records": int(db.execute(text(
            "SELECT COUNT(*) FROM expenditures WHERE vendor IS NOT NULL AND trim(vendor) <> ''"
        )).scalar() or 0),
        "total_vendor_expenditure": round(float(db.execute(text(
            "SELECT COALESCE(SUM(expenditure_amount), 0) FROM expenditures "
            "WHERE vendor IS NOT NULL AND trim(vendor) <> ''"
        )).scalar() or 0.0), 2),
        "multi_project_vendors": int(srow["multi"]),
        "vendors_with_high_risk_projects": int(srow["highrisk"]),
        "unusual_concentration": bool(float(srow["max_share"]) >= 25 or float(srow["top5"]) >= 60),
        "top5_share": round(float(srow["top5"]), 2),
        "top10_share": round(float(srow["top10"]), 2),
    }

    result = {
        "data_source": "Current MPLADS expenditure dataset",
        "generated_from": ["projects", "expenditures", "risk_scores"],
        "normalization": "Vendor names use basic case, space, and punctuation normalization only; raw names are retained.",
        "project_count_method": (
            "The expenditures dataset has no project_id column. project_count "
            "is the number of distinct work engagements in the vendor's own "
            "expenditure records (work_description + constituency + state); "
            "multiple transactions on one work count as one project, so "
            "project_count <= transaction_count always holds. Real project "
            "rows matching the exact work key are attached separately as "
            "linked_project_count. No project links are guessed."
        ),
        "stats": stats,
        "total_vendors": total_vendors,
        "skip": skip,
        "limit": limit,
        "vendors": vendors,
    }
    set_cached(cache_key, result)
    return result


@app.get("/vendor-intelligence/{vendor_key}", tags=["Vendor Intelligence"])
def get_vendor_profile(vendor_key: str, db: Session = Depends(get_db)):
    # On-demand single-vendor build — no full-payload construction, no
    # full-table scans. Bounded per-vendor cache, cleared on data sync.
    return _vendor_profile_payload(db, _vendor_key(vendor_key))


@app.get("/completed-works", tags=["MP Operations"])
def get_completed_works(
    state: Optional[str] = None,
    constituency: Optional[str] = None,
    category: Optional[str] = None,
    mp_name: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db)
):
    query = db.query(models.CompletedWork)
    if state:
        query = query.filter(models.CompletedWork.state == state)
    if constituency:
        query = query.filter(models.CompletedWork.constituency == constituency)
    if category:
        query = query.filter(models.CompletedWork.category == category)
    if mp_name:
        query = query.filter(models.CompletedWork.mp_name.ilike(f"%{mp_name}%"))

    return query.offset(skip).limit(limit).all()


# =========================================================
# ANOMALY DETECTION & AI ENDPOINTS
# =========================================================

@app.get("/anomalies", tags=["Anomaly Detection"])
def detect_anomalies(
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=500),
    risk_level: Optional[str] = None,
    state: Optional[str] = None,
    constituency: Optional[str] = None,
    house: Optional[str] = Query(None, description="House: 'Lok Sabha', 'Rajya Sabha', or omitted for All"),
    fy: Optional[str] = Query(None, description="Filter by financial year"),
    q: Optional[str] = Query(None, description="Search by project name or ID"),
    sort_by: Optional[str] = Query(None, description="Sort: risk_score, sanctioned_amount, expenditure, completion_percentage, project_name, state, id"),
    sort_dir: Optional[str] = Query(None, description="Sort direction: asc or desc"),
    db: Session = Depends(get_db)
):
    # Use cached risk table presence flag (set at startup, avoids COUNT(*) on every request)
    has_risk_table = _cache.get("_has_risk_table", False)

    if has_risk_table:
        query = (
            db.query(models.RiskScore, models.Project)
            .join(models.Project, models.RiskScore.project_id == models.Project.id)
            .outerjoin(
                models.ProjectRecInfo,
                models.ProjectRecInfo.project_id == models.Project.id,
            )
        )
        if state:
            query = query.filter(models.Project.state == state)
        if constituency:
            query = query.filter(models.Project.constituency == constituency)
        if house in ("Lok Sabha", "Rajya Sabha"):
            query = query.filter(models.Project.house == house)
        if fy:
            query = query.filter(models.Project.fy == fy)
        if risk_level:
            query = query.filter(models.RiskScore.risk_level.ilike(risk_level))
        if q:
            q_clean = q.strip()
            try:
                q_id = int(q_clean)
                query = query.filter(
                    (models.Project.id == q_id) |
                    (models.Project.project_name.ilike(f"%{q_clean}%"))
                )
            except (ValueError, TypeError):
                query = query.filter(models.Project.project_name.ilike(f"%{q_clean}%"))

        total_anomalies = query.count()
        # Apply sorting for joined risk+project query
        if sort_by:
            sort_col = sort_by.strip().lower()
            sort_asc = sort_dir and sort_dir.strip().lower() == "asc"
            # Map sort columns to the appropriate model column
            anomaly_sort_map = {
                "risk_score": (models.RiskScore.risk_score, None),
                "sanctioned_amount": (models.Project.sanctioned_amount, None),
                # Authoritative linked spend (project_rec_info) — the catalog
                # column is a zeroed legacy stamp.
                "expenditure": (func.coalesce(models.ProjectRecInfo.linked_expenditure, 0.0), None),
                "completion_percentage": (models.Project.completion_percentage, None),
                "project_name": (models.Project.project_name, None),
                "state": (models.Project.state, None),
                "id": (models.Project.id, None),
            }
            if sort_col in anomaly_sort_map:
                col = anomaly_sort_map[sort_col][0]
                rows = query.order_by(col.desc().nullslast() if not sort_asc else col.asc().nullslast()).offset(skip).limit(limit).all()
            else:
                rows = query.order_by(models.RiskScore.risk_score.desc()).offset(skip).limit(limit).all()
        else:
            rows = query.order_by(models.RiskScore.risk_score.desc()).offset(skip).limit(limit).all()


        anomalies_list = []
        # Expenditure activity span (first/latest recorded expenditure dates)
        # — derived from the same conservative matching as the timeline, so it
        # adds one dict lookup per row, no extra queries.
        from activity import get_expenditure_span
        # Recommendation facts for the page — one batched lookup, same source
        # of truth as the Projects page (project_rec_info table).
        rec_map = _rec_info_map(db, [proj.id for _, proj in rows])
        for risk, proj in rows:
            reasons = [r.strip() for r in (risk.reasons or "").split(",") if r.strip()]
            try:
                span = get_expenditure_span(proj) or {}
            except Exception:
                span = {}
            anomalies_list.append({
                "project_id": proj.id,
                "project_name": proj.project_name,
                "state": proj.state,
                "district": proj.district,
                "constituency": proj.constituency,
                "house": proj.house,
                "project_type": proj.project_type,
                "sanctioned_amount": proj.sanctioned_amount or 0.0,
                # Authoritative linked payment-ledger spend (rec batch below).
                "expenditure": float((rec_map.get(proj.id) or {}).get("linked_expenditure") or 0.0),
                "completion_percentage": proj.completion_percentage or 0.0,
                "status": proj.status,
                "risk_score": risk.risk_score,
                "risk_level": risk.risk_level,
                "ml_anomaly": risk.ml_anomaly,
                "ml_score": risk.ml_score,
                "reasons": reasons,
                "expenditure_first_date": span.get("first_expenditure_date"),
                "expenditure_latest_date": span.get("latest_expenditure_date"),
                "activity_days": span.get("activity_days"),
                "delay_indicator": span.get("delay_indicator"),
                "delay_severity": span.get("delay_severity"),
                "rec": _rec_item(rec_map.get(proj.id)),
            })

        total_checked = db.query(func.count(models.Project.id)).scalar() or 0
        return {
            "total_projects_checked": total_checked,
            "total_anomalies": total_anomalies,
            "skip": skip,
            "limit": limit,
            "anomalies": anomalies_list
        }

    # On-the-fly calculation for paginated candidates
    query = db.query(models.Project)
    if state:
        query = query.filter(models.Project.state == state)

    # Focus candidate scan on projects with potential anomalies.
    # Spend conditions use the authoritative linked expenditure
    # (project_rec_info); the catalog's expenditure column is a zeroed
    # legacy stamp and would never trigger these rules.
    query = query.outerjoin(
        models.ProjectRecInfo,
        models.ProjectRecInfo.project_id == models.Project.id,
    )
    _linked_exp = func.coalesce(models.ProjectRecInfo.linked_expenditure, 0.0)
    candidate_query = query.filter(
        (_linked_exp > models.Project.sanctioned_amount) |
        ((models.Project.sanctioned_amount >= 1000000) & (models.Project.completion_percentage < 25)) |
        ((_linked_exp > 0) & (models.Project.completion_percentage == 0)) |
        (models.Project.status.ilike("completed") & (models.Project.completion_percentage < 90))
    )

    total_candidates = candidate_query.count()
    projects = candidate_query.offset(skip).limit(limit).all()

    anomalies = []
    from activity import get_expenditure_span
    rec_map_fallback = _rec_info_map(db, [p.id for p in projects])
    for proj in projects:
        # Attach authoritative spend before scoring (predictor reads it).
        _rec0 = rec_map_fallback.get(proj.id) or {}
        if _rec0.get("linked_expenditure"):
            proj._authoritative_expenditure = float(_rec0["linked_expenditure"])
        risk_info = predict_risk(proj)
        if risk_info["is_anomaly"]:
            if risk_level and risk_info["risk_level"].lower() != risk_level.lower():
                continue
            try:
                span = get_expenditure_span(proj) or {}
            except Exception:
                span = {}
            anomalies.append({
                "project_id": proj.id,
                "project_name": proj.project_name,
                "state": proj.state,
                "district": proj.district,
                "constituency": proj.constituency,
                "house": proj.house,
                "project_type": proj.project_type,
                "sanctioned_amount": proj.sanctioned_amount or 0.0,
                "expenditure": float((rec_map_fallback.get(proj.id) or {}).get("linked_expenditure") or 0.0),
                "completion_percentage": proj.completion_percentage or 0.0,
                "status": proj.status,
                "risk_score": risk_info["risk_score"],
                "risk_level": risk_info["risk_level"],
                "ml_anomaly": risk_info["ml_anomaly"],
                "ml_score": risk_info["ml_score"],
                "reasons": risk_info["reasons"],
                "expenditure_first_date": span.get("first_expenditure_date"),
                "expenditure_latest_date": span.get("latest_expenditure_date"),
                "activity_days": span.get("activity_days"),
                "delay_indicator": span.get("delay_indicator"),
                "delay_severity": span.get("delay_severity"),
                "rec": _rec_item(rec_map_fallback.get(proj.id)),
            })

    total_checked = db.query(func.count(models.Project.id)).scalar() or 0
    return {
        "total_projects_checked": total_checked,
        "total_anomalies": total_candidates,
        "skip": skip,
        "limit": limit,
        "anomalies": anomalies
    }


@app.get("/ai/risk/{project_id}", tags=["AI Operations"])
def get_project_risk(
    project_id: int = Path(..., description="Project ID"),
    db: Session = Depends(get_db)
):
    project = db.query(models.Project).filter(models.Project.id == project_id).first()
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project {project_id} not found"
        )

    # Score with the authoritative per-project spend (payment ledger via
    # project_rec_info) — never the catalog's zeroed legacy stamp.
    from ml.predictor import attach_authoritative_expenditure
    attach_authoritative_expenditure([project], db)
    _linked = _eff_exp_of(project, db)
    risk_info = predict_risk(project)
    return {
        "project": {
            "id": project.id,
            "project_name": project.project_name,
            "state": project.state,
            "district": project.district,
            "sanctioned_amount": project.sanctioned_amount,
            "expenditure": _linked,
            "completion_percentage": project.completion_percentage,
            "status": project.status
        },
        "risk": risk_info
    }


@app.get("/ai/risky-projects", tags=["AI Operations"])
def get_risky_projects(
    limit: int = Query(10, ge=1, le=100),
    db: Session = Depends(get_db)
):
    has_risk_table = db.query(func.count(models.RiskScore.id)).scalar() or 0

    if has_risk_table > 0:
        risks = (
            db.query(models.RiskScore, models.Project)
            .join(models.Project, models.RiskScore.project_id == models.Project.id)
            .filter(models.RiskScore.risk_score >= 50)
            .order_by(models.RiskScore.risk_score.desc())
            .limit(limit)
            .all()
        )
        projects = []
        _exp_map = _authoritative_expenditure_map(db)
        for risk, project in risks:
            projects.append({
                "id": project.id,
                "project_name": project.project_name,
                "state": project.state,
                "district": project.district,
                "sanctioned_amount": project.sanctioned_amount,
                "expenditure": float(_exp_map.get(project.id, 0.0)),
                "completion_percentage": project.completion_percentage,
                "risk_score": risk.risk_score,
                "risk_level": risk.risk_level,
                "ml_anomaly": risk.ml_anomaly,
                "reasons": [r.strip() for r in (risk.reasons or "").split(",") if r.strip()]
            })

        total = db.query(models.RiskScore).filter(models.RiskScore.risk_score >= 50).count()
        return {
            "total_risky_projects": total,
            "showing": len(projects),
            "projects": projects
        }

    # Fallback to candidate evaluation (authoritative linked spend, not the
    # catalog's zeroed legacy stamp)
    candidates = (
        db.query(models.Project)
        .outerjoin(
            models.ProjectRecInfo,
            models.ProjectRecInfo.project_id == models.Project.id,
        )
        .filter(
            (func.coalesce(models.ProjectRecInfo.linked_expenditure, 0.0) > models.Project.sanctioned_amount) |
            ((models.Project.sanctioned_amount >= 1000000) & (models.Project.completion_percentage < 25))
        )
        .limit(limit * 2)
        .all()
    )

    scored = []
    for p in candidates:
        r = predict_risk(p)
        scored.append({
            "id": p.id,
            "project_name": p.project_name,
            "state": p.state,
            "district": p.district,                "sanctioned_amount": p.sanctioned_amount,
                "expenditure": float(metrics_svc.resolve_project_expenditure(db, p)),
                "completion_percentage": p.completion_percentage,
                "risk_score": r["risk_score"],
                "risk_level": r["risk_level"],
                "ml_anomaly": r["ml_anomaly"],
            "reasons": r["reasons"]
        })

    scored.sort(key=lambda x: x["risk_score"], reverse=True)
    top = scored[:limit]

    return {
        "total_risky_projects": len(scored),
        "showing": len(top),
        "projects": top
    }


@app.get("/ai/insights", tags=["AI Operations"])
def get_ai_insights(db: Session = Depends(get_db)):
    cached = get_cached("ai_insights", ttl_seconds=120)
    if cached is not None:
        return cached

    # Authoritative aggregates (metrics_svc): catalog sanctioned + ledger
    # expenditure/completions.
    stats = metrics_svc.portfolio_overview(db, fy=None)

    total_projects = int(stats["total_projects"] or 0)
    total_sanctioned = float(stats["total_sanctioned_amount"] or 0)
    total_expenditure = float(stats["total_expenditure"] or 0)
    unused_amount = max(0.0, total_sanctioned - total_expenditure)

    # Risk counts
    high_risk_projects = db.query(func.count(models.RiskScore.id)).filter(
        models.RiskScore.risk_level.ilike("high")
    ).scalar() or 0

    if high_risk_projects == 0 and total_projects > 0:
        high_risk_projects = (
            db.query(func.count(models.Project.id))
            .outerjoin(
                models.ProjectRecInfo,
                models.ProjectRecInfo.project_id == models.Project.id,
            )
            .filter(
                func.coalesce(models.ProjectRecInfo.linked_expenditure, 0.0)
                > models.Project.sanctioned_amount
            )
            .scalar() or 0
        )

    top_states = (
        db.query(models.Project.state, func.count(models.Project.id).label("projects"))
        .filter(models.Project.state.isnot(None), models.Project.state != "")
        .group_by(models.Project.state)
        .order_by(func.count(models.Project.id).desc())
        .limit(5)
        .all()
    )

    result = {
        "overview": {
            "total_projects": total_projects,
            "completed_projects": int(stats["completed_projects"] or 0),
            "ongoing_projects": int(stats["ongoing_projects"] or 0)
        },
        "financial_analysis": {
            "total_sanctioned_amount": total_sanctioned,
            "total_expenditure": total_expenditure,
            "unused_amount": unused_amount
        },
        "ai_risk_analysis": {
            "high_risk_projects": high_risk_projects
        },
        "top_states_by_projects": [
            {"state": s[0], "projects": int(s[1])} for s in top_states
        ]
    }

    set_cached("ai_insights", result)
    return result


@app.get("/ai/narrative-insights", tags=["AI Operations"])
def narrative_insights(
    fy: Optional[str] = Query(None, description="Filter by financial year"),
    house: Optional[str] = Query(None, description="House: 'Lok Sabha', 'Rajya Sabha', or omitted for All"),
    db: Session = Depends(get_db),
):
    house_norm = house if house in ("Lok Sabha", "Rajya Sabha") else None
    cache_key = f"narrative_insights_{fy or 'all'}_{house_norm or 'all'}"
    cached = get_cached(cache_key, ttl_seconds=120)
    if cached is not None:
        return cached

    base_q = db.query(models.Project)
    if fy:
        base_q = base_q.filter(models.Project.fy == fy)
    if house_norm:
        base_q = base_q.filter(models.Project.house == house_norm)

    insights = []
    # Authoritative aggregates: sanctioned from the catalog, expenditure and
    # completions from the payment/completions ledgers (metrics_svc).
    stats = metrics_svc.portfolio_overview(db, fy=fy, house=house_norm)

    total_projects = int(stats["total_projects"] or 0)
    sanctioned = float(stats["total_sanctioned_amount"] or 0)
    expenditure = float(stats["total_expenditure"] or 0)
    completed = int(stats["completed_projects"] or 0)

    # 1. High risk insight
    risk_q = db.query(func.count(models.RiskScore.id)).filter(
        models.RiskScore.risk_level.ilike("high")
    )
    if fy:
        risk_project_ids = base_q.with_entities(models.Project.id).subquery()
        risk_q = risk_q.filter(models.RiskScore.project_id.in_(risk_project_ids))
    high_risk = risk_q.scalar() or 0

    if high_risk == 0 and total_projects > 0:
        high_risk = (
            base_q.outerjoin(
                models.ProjectRecInfo,
                models.ProjectRecInfo.project_id == models.Project.id,
            )
            .filter(
                func.coalesce(models.ProjectRecInfo.linked_expenditure, 0.0)
                > models.Project.sanctioned_amount
            )
            .count() or 0
        )

    if total_projects > 0:
        pct = (high_risk / total_projects) * 100
        insights.append({
            "type": "risk",
            "title": "High Risk Discrepancies Detected",
            "message": f"{high_risk:,} projects ({pct:.2f}% of monitored portfolio) exhibit significant cost overruns or execution stagnation requiring audit attention."
        })

    # 2. Financial insight
    if sanctioned > 0:
        utilization = stats["utilization_percentage"]
        insights.append({
            "type": "financial",
            "title": "Portfolio Fund Utilization",
            "message": f"Cumulative fund utilization is {utilization:.2f}% (₹{expenditure/1e7:,.2f} Cr of recorded payments against ₹{sanctioned/1e7:,.2f} Cr sanctioned). Expenditure is summed from the {stats.get('expenditure_transactions', 0):,}-transaction payment ledger; most payments postdate 2023 and payment records cannot be joined to individual recommended works."
        })

    # 3. Completion insight
    if total_projects > 0:
        comp_rate = (completed / total_projects) * 100
        insights.append({
            "type": "completion",
            "title": "Physical Milestone Progress",
            "message": f"{completed:,} completed works recorded in the completions ledger ({comp_rate:.2f}% of the {total_projects:,} recommended works in the catalog)."
        })

    # 4. Top state insight
    top_state_q = (
        base_q.filter(models.Project.state.isnot(None), models.Project.state != "")
        .with_entities(models.Project.state, func.count(models.Project.id).label("count"))
        .group_by(models.Project.state)
        .order_by(func.count(models.Project.id).desc())
        .first()
    )
    if top_state_q:
        insights.append({
            "type": "geographic",
            "title": "Highest Work Allocation Concentration",
            "message": f"{top_state_q[0]} leads with the highest number of sanctioned works ({top_state_q[1]:,} projects)."
        })

    result = {
        "total_insights": len(insights),
        "insights": insights
    }

    set_cached(cache_key, result)
    return result


@app.get("/ai/state-insights/{state}", tags=["AI Operations"])
def state_insights(
    state: str = Path(..., description="State name"),
    db: Session = Depends(get_db)
):
    stats = (
        db.query(
            func.count(models.Project.id).label("total"),
            func.coalesce(func.sum(models.Project.sanctioned_amount), 0).label("sanctioned"),
            func.coalesce(func.avg(models.Project.completion_percentage), 0).label("avg_completion")
        )
        .filter(models.Project.state == state)
        .one()
    )

    if not stats.total or stats.total == 0:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No project data found for state: '{state}'"
        )

    total_projects = int(stats.total)
    sanctioned = float(stats.sanctioned or 0)
    # Authoritative expenditure/completions from the ledgers, scoped by the
    # ledger's own state column (labels match the catalog 1:1).
    led = metrics_svc.ledger_totals_scoped(db, state=state)
    completed = led["completed_works"]
    expenditure = led["total_expenditure"]
    utilization = (expenditure / sanctioned * 100) if sanctioned > 0 else 0.0

    # High risk query via subquery instead of python loop
    high_risk = (
        db.query(func.count(models.RiskScore.id))
        .join(models.Project, models.RiskScore.project_id == models.Project.id)
        .filter(models.Project.state == state, models.RiskScore.risk_level.ilike("high"))
        .scalar() or 0
    )

    top_project_type = (
        db.query(models.Project.project_type, func.count(models.Project.id))
        .filter(models.Project.state == state, models.Project.project_type.isnot(None))
        .group_by(models.Project.project_type)
        .order_by(func.count(models.Project.id).desc())
        .first()
    )

    insights = []
    if utilization < 50:
        insights.append(f"Fund utilization is {utilization:.1f}% (below 50%), requiring heightened financial oversight.")
    elif utilization < 75:
        insights.append(f"Fund utilization is moderate at {utilization:.1f}%.")
    else:
        insights.append(f"Strong fund utilization at {utilization:.1f}%.")

    if total_projects > 0 and (completed / total_projects) < 0.20:
        insights.append(f"Physical completion rate is low ({(completed/total_projects)*100:.1f}%).")

    if high_risk > 0:
        insights.append(f"{high_risk} projects flagged as high risk by AI audit checks.")
    else:
        insights.append("No critical high-risk anomalies detected in this state.")

    return {
        "state": state,
        "summary": {
            "total_projects": total_projects,
            "completed_projects": completed,
            "high_risk_projects": high_risk,
            "average_completion_percentage": round(float(stats.avg_completion), 2)
        },
        "financial": {
            "sanctioned_amount": sanctioned,
            "expenditure": expenditure,
            "utilization_percentage": round(utilization, 2)
        },
        "dominant_project_type": top_project_type[0] if top_project_type else "General",
        "ai_analysis": insights
    }


# =========================================================
# STATE INTELLIGENCE
# =========================================================

@app.get("/dashboard/state-intelligence", tags=["Dashboard"])
def state_intelligence(
    fy: Optional[str] = Query(None, description="Filter by financial year"),
    house: Optional[str] = Query(None, description="House: 'Lok Sabha', 'Rajya Sabha', or omitted for All"),
    db: Session = Depends(get_db)
):
    """Return detailed analytics for all states.

    Single-flight cached: several analysts opening dashboards at once wait for
    ONE build instead of each running the multi-second aggregate. TTL is long
    but clear_cache() runs on every data mutation, so results cannot go stale
    after an import.
    """
    house_norm = house if house in ("Lok Sabha", "Rajya Sabha") else None
    cache_key = f"state_intelligence_{fy or 'all'}_{house_norm or 'all'}"
    return cached_build(
        cache_key,
        600,
        lambda: _compute_state_intelligence(db, fy, house_norm),
    )


def _compute_state_intelligence(db: Session, fy: Optional[str], house_norm: Optional[str]):
    """Build the state-intelligence payload (uncached, DB-facing)."""
    base_q = db.query(models.Project).filter(models.Project.state.isnot(None), models.Project.state != "")
    if fy:
        base_q = base_q.filter(models.Project.fy == fy)
    if house_norm:
        base_q = base_q.filter(models.Project.house == house_norm)

    # Main state stats query (expenditure merged from the payment ledger below)
    rows = (
        base_q.with_entities(
            models.Project.state,
            func.count(models.Project.id).label("total_projects"),
            func.sum(case((models.Project.status.ilike("completed"), 1), else_=0)).label("completed_projects"),
            func.sum(case((models.Project.status.ilike("ongoing"), 1), else_=0)).label("ongoing_projects"),
            func.coalesce(func.sum(models.Project.sanctioned_amount), 0).label("total_sanctioned"),
            func.coalesce(func.avg(models.Project.completion_percentage), 0).label("avg_completion")
        )
        .group_by(models.Project.state)
        .order_by(func.count(models.Project.id).desc())
        .all()
    )

    # High-risk counts per state
    hr_q = (
        db.query(
            models.Project.state,
            func.count(models.RiskScore.id).label("high_risk_count"),
            func.sum(case((models.RiskScore.ml_anomaly == True, 1), else_=0)).label("ml_anomaly_count"),
            func.sum(case((models.RiskScore.risk_level.ilike("high"), 1), else_=0)).label("high_count"),
            func.sum(case((models.RiskScore.risk_level.ilike("medium"), 1), else_=0)).label("medium_count")
        )
        .join(models.RiskScore, models.Project.id == models.RiskScore.project_id)
    )
    if fy:
        hr_q = hr_q.filter(models.Project.fy == fy)
    if house_norm:
        hr_q = hr_q.filter(models.Project.house == house_norm)
    high_risk_rows = hr_q.group_by(models.Project.state).all()
    risk_map = {r.state: r for r in high_risk_rows}

    # Authoritative expenditure: payment ledger grouped by its own state column
    # (state labels verified to match the project catalog 1:1). House-scoped
    # via the ledger's own house column when requested.
    ledger_by_state = metrics_svc.state_ledger_map(db, fy=fy, house=house_norm)
    # MP+IDA-verified project linkage, deduplicated by work key — disclosed
    # alongside so the state total is never confused with the project-attributable
    # subset (the gap is a dataset linkage limitation, not hidden data).
    linked_by_state = metrics_svc.state_linked_map(db, fy=fy)

    result = []
    for row in rows:
        code, canonical = state_geo.normalize_state_label(row.state)
        total_projects = int(row.total_projects)
        sanctioned = float(row.total_sanctioned)
        spent = float(ledger_by_state.get(row.state, {}).get("expenditure", 0.0))
        utilization = (spent / sanctioned * 100) if sanctioned > 0 else 0.0
        risk = risk_map.get(row.state)
        lk = linked_by_state.get(row.state, {})
        linked_total = float(lk.get("linked_total", 0.0))
        work_verified = float(lk.get("work_verified_total", 0.0))
        risk_scored = int(risk.high_risk_count) if risk else 0
        result.append({
            "state": row.state,
            "state_code": code,
            "canonical_state": canonical,
            "display_name": canonical if code else row.state,
            "drill_state": row.state,
            "total_projects": total_projects,
            "completed_projects": int(row.completed_projects),
            "ongoing_projects": int(row.ongoing_projects),
            "completion_rate_percentage": round(
                int(row.completed_projects) / total_projects * 100.0, 2
            ) if total_projects else None,
            "total_sanctioned_amount": sanctioned,
            "total_expenditure": spent,
            "utilization_percentage": round(utilization, 2),
            "average_completion_percentage": round(float(row.avg_completion), 2),
            "high_risk_projects": int(risk.high_count) if risk else 0,
            "medium_risk_projects": int(risk.medium_count) if risk else 0,
            "ml_anomaly_projects": int(risk.ml_anomaly_count) if risk else 0,
            "risk_scored_projects": risk_scored,
            "risk_coverage_percentage": round(
                risk_scored / total_projects * 100.0, 2
            ) if total_projects else None,
            "project_linked_expenditure": linked_total,
            "work_verified_expenditure": work_verified,
            "projects_with_linked_expenditure": int(lk.get("linked_rows", 0)),
            "linked_work_keys": int(lk.get("linked_keys", 0)),
            "unique_work_keys": int(lk.get("unique_keys", 0)),
            "unattributed_expenditure": round(spent - work_verified, 2),
            "expenditure_scope_note": (
                "total_expenditure is every recorded payment in the state; "
                "work_verified_expenditure is the subset verifiably matched to "
                "catalog works; project_linked_expenditure is the uniquely "
                "attributable subset."
            ),
        })

    return result


@app.get("/dashboard/expenditure-reconciliation", tags=["Dashboard"])
def expenditure_reconciliation(
    fy: Optional[str] = Query(None, description="Filter by financial year"),
    db: Session = Depends(get_db),
):
    """Reconcile state-level ledger totals against project-level linked spend.

    Both sides come from the SAME payment ledger; they differ because the
    per-project attribution (normalized work key + MP + IDA verification)
    covers only the subset of ledger payments whose work key verifiably
    matches a catalog project. The unattributed remainder is disclosed per
    state — never redistributed or forced to match.
    """
    cache_key = f"exp_reconciliation_{fy or 'all'}"
    cached = get_cached(cache_key, ttl_seconds=300)
    if cached is not None:
        return cached
    result = metrics_svc.state_expenditure_reconciliation(db, fy=fy)
    set_cached(cache_key, result)
    return result


# =========================================================
# AUDIT PRIORITY QUEUE
# =========================================================

@app.get("/audit-priority/summary", tags=["Anomaly Detection"])
def audit_priority_summary(
    fy: Optional[str] = Query(None),
    house: Optional[str] = Query(None, description="House: 'Lok Sabha', 'Rajya Sabha', or omitted for All"),
    db: Session = Depends(get_db),
):
    """
    Aggregate counts for the Audit Priority KPI cards.

    Includes the composite audit-priority tier distribution computed with the
    same SQL expression used for the priority list, plus the sanctioned value
    of the flagged projects (funds under review).
    """
    base = db.query(models.RiskScore).join(
        models.Project, models.RiskScore.project_id == models.Project.id
    ).filter(models.RiskScore.risk_score > 0)
    if fy:
        base = base.filter(models.Project.fy == fy)
    if house in ("Lok Sabha", "Rajya Sabha"):
        base = base.filter(models.Project.house == house)

    total = base.count()
    high = base.filter(models.RiskScore.risk_level == "High").count()
    medium = base.filter(models.RiskScore.risk_level == "Medium").count()
    critical = base.filter(models.RiskScore.risk_score >= 80).count()
    ml_count = base.filter(models.RiskScore.ml_anomaly == True).count()

    # Tier distribution + exposure (single SQL pass over the composite score).
    # The score expression reads project_rec_info — outer join keeps all rows.
    rank_expr = audit_intel.audit_priority_rank_sql()
    tier_query = (
        base.with_entities(
            rank_expr.label("tier_rank"),
            func.count(models.Project.id).label("n"),
            func.coalesce(func.sum(models.Project.sanctioned_amount), 0.0).label("exposure"),
        )
        .outerjoin(models.ProjectRecInfo, models.ProjectRecInfo.project_id == models.Project.id)
        .group_by(rank_expr)
        .all()
    )
    tiers = {code: {"count": 0, "sanctioned_under_review": 0.0} for code in ("P1", "P2", "P3", "P4")}
    total_exposure = 0.0
    rank_to_code = {1: "P1", 2: "P2", 3: "P3", 4: "P4"}
    for row in tier_query:
        code = rank_to_code.get(int(row.tier_rank or 4), "P4")
        tiers[code] = {
            "count": int(row.n or 0),
            "sanctioned_under_review": round(float(row.exposure or 0), 2),
        }
        total_exposure += float(row.exposure or 0)

    return {
        "total_flagged": total,
        "high_risk": high,
        "medium_risk": medium,
        "critical": critical,
        "ml_anomalies": ml_count,
        # Tier view of the same flagged set (composite audit priority score)
        "tiers": tiers,
        "tier_counts": {code: tiers[code]["count"] for code in tiers},
        "total_sanctioned_under_review": round(total_exposure, 2),
        "tier_labels": {code: meta["tier_label"] for code, meta in audit_intel.TIER_META.items()},
        "score_formula": "risk(0-55) + exposure(0-20) + mismatch(0-15) + evidence_gap(0-10)",
        "priority_note": (
            "Tier = composite audit priority score, with a floor applied from the stored risk level "
            "(High → at least P2, Medium → at least P3)."
        ),
    }


@app.get("/audit-priority", tags=["Anomaly Detection"])
def audit_priority(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    state: Optional[str] = None,
    constituency: Optional[str] = None,
    fy: Optional[str] = Query(None, description="Filter by financial year"),
    risk_level: Optional[str] = None,
    house: Optional[str] = Query(None, description="House: 'Lok Sabha', 'Rajya Sabha', or omitted for All"),
    tier: Optional[str] = Query(None, description="Audit priority tier: P1 | P2 | P3 | P4"),
    q: Optional[str] = None,
    sort_by: Optional[str] = Query(None),
    sort_dir: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    """
    Highest-priority projects for audit, ranked by the composite audit-priority
    score (risk + financial exposure + financial/physical mismatch + evidence gap).

    Each row carries the score breakdown, the P1–P4 tier, financial exposure and
    the generated recommended action so the UI never recomputes them.
    """
    score_expr = audit_intel.audit_priority_score_sql().label("audit_score")
    rank_expr = audit_intel.audit_priority_rank_sql().label("audit_rank")
    query = (
        db.query(models.RiskScore, models.Project, score_expr, rank_expr)
        .join(models.Project, models.RiskScore.project_id == models.Project.id)
        .outerjoin(models.ProjectRecInfo, models.ProjectRecInfo.project_id == models.Project.id)
        .filter(models.RiskScore.risk_score > 0)
    )
    if state:
        query = query.filter(models.Project.state == state)
    if constituency:
        query = query.filter(models.Project.constituency == constituency)
    if fy:
        query = query.filter(models.Project.fy == fy)
    if house in ("Lok Sabha", "Rajya Sabha"):
        query = query.filter(models.Project.house == house)
    if risk_level:
        query = query.filter(models.RiskScore.risk_level.ilike(risk_level))
    if tier:
        tier_ranks = {"P1": 1, "P2": 2, "P3": 3, "P4": 4}
        t = tier.strip().upper()
        if t in tier_ranks:
            query = query.filter(rank_expr == tier_ranks[t])
    if q:
        q_clean = q.strip()
        try:
            q_id = int(q_clean)
            query = query.filter(
                (models.Project.id == q_id) |
                (models.Project.project_name.ilike(f"%{q_clean}%"))
            )
        except (ValueError, TypeError):
            query = query.filter(models.Project.project_name.ilike(f"%{q_clean}%"))

    total = query.count()

    if sort_by:
        sort_col = sort_by.strip().lower()
        sort_asc = sort_dir and sort_dir.strip().lower() == "asc"
        priority_sort_map = {
            "audit_priority": rank_expr,
            "audit_score": score_expr,
            "risk_score": models.RiskScore.risk_score,
            "sanctioned_amount": models.Project.sanctioned_amount,
            "expenditure": func.coalesce(models.ProjectRecInfo.linked_expenditure, 0.0),
            "completion_percentage": models.Project.completion_percentage,
            "project_name": models.Project.project_name,
            "state": models.Project.state,
            "id": models.Project.id,
        }
        if sort_col in priority_sort_map:
            col = priority_sort_map[sort_col]
            query = query.order_by(col.desc().nullslast() if not sort_asc else col.asc().nullslast())
        else:
            query = query.order_by(models.RiskScore.risk_score.desc())
    else:
        query = query.order_by(models.RiskScore.risk_score.desc())

    rows = query.offset(skip).limit(limit).all()
    # Batched linked-spend lookup for the page (project_rec_info).
    _page_rec = _rec_info_map(db, [proj.id for _r, proj, _s, _rk in rows])

    priority_list = []
    for rank, (risk, proj, audit_score, audit_rank) in enumerate(rows, start=skip + 1):
        reasons = [r.strip() for r in (risk.reasons or "").split(",") if r.strip()]
        _linked_exp = float((_page_rec.get(proj.id) or {}).get("linked_expenditure") or 0.0)
        # Determine primary anomaly type
        primary = "Unknown"
        if reasons:
            r_lower = reasons[0].lower()
            if "exceeds sanctioned" in r_lower:
                primary = "Cost Overrun"
            elif "high expenditure" in r_lower:
                primary = "Expenditure-Progress Mismatch"
            elif "0% physical completion" in r_lower:
                primary = "Zero Progress Disbursement"
            elif "completed but physical" in r_lower:
                primary = "Status Inconsistency"
            elif "high-value project" in r_lower:
                primary = "High-Value Stalled Project"
            elif "high completion" in r_lower:
                primary = "Completion-Expenditure Mismatch"
            elif "very low fund" in r_lower:
                primary = "Low Fund Utilization"
            elif "ml" in r_lower:
                primary = "ML Statistical Outlier"

        # Audit-intelligence fields (computed from the same values shown above,
        # using the authoritative linked payment-ledger spend)
        stale = _check_stale_progress(proj, linked_expenditure=_linked_exp)
        breakdown = audit_intel.priority_breakdown(
            proj, risk.risk_score, float(audit_score or 0), risk_level=risk.risk_level,
            linked_expenditure=_linked_exp,
        )
        exposure = audit_intel.financial_exposure(proj, linked_expenditure=_linked_exp)
        checklist = audit_intel.build_checklist(proj, reasons, stale["flag"], linked_expenditure=_linked_exp)
        evidence_gap = audit_intel.evidence_gap_summary(proj)

        priority_list.append({
            "priority_rank": rank,
            "project_id": proj.id,
            "project_name": proj.project_name,
            "state": proj.state,
            "district": proj.district,
            "constituency": proj.constituency,
            "house": proj.house,
            "project_type": proj.project_type,
            "fy": proj.fy,
            "sanctioned_amount": proj.sanctioned_amount or 0.0,
            "expenditure": _linked_exp,
            "completion_percentage": proj.completion_percentage or 0.0,
            "status": proj.status,
            "risk_score": risk.risk_score,
            "risk_level": risk.risk_level,
            "ml_anomaly": risk.ml_anomaly,
            "primary_anomaly": primary,
            "reasons": reasons,
            # ── Audit intelligence additions (all derived from the values above) ──
            **breakdown,
            "financial_exposure": exposure,
            "evidence_gap": evidence_gap,
            "recommended_actions": audit_intel.recommended_actions(checklist, limit=3),
            "recommended_action": audit_intel.recommended_actions(checklist, limit=1)[0],
            "data_quality_flag": stale["flag"],
            "data_quality_reason": stale["reason"],
        })

    return {
        "total": total,
        "skip": skip,
        "limit": limit,
        "priorities": priority_list
    }


# =========================================================
# AUDIT INTELLIGENCE & INVESTIGATION WORKSPACE
# =========================================================
# Evidence-gap detection, anomaly dimension explorer, peer benchmarking,
# deterministic what-if simulation, audit case + escalation draft, and the
# persisted investigation workspace. All values come from the actual project
# record and the existing risk/anomaly results — nothing is invented.


class SimulateRequest(BaseModel):
    sanctioned_amount: Optional[float] = Field(None, ge=0, le=1e15)
    expenditure: Optional[float] = Field(None, ge=0, le=1e15)
    completion_percentage: Optional[float] = Field(None, ge=0, le=100)


class InvestigationUpdateRequest(BaseModel):
    status: Optional[str] = None
    note: Optional[str] = Field(None, max_length=4000)


class EvidenceItemUpdateRequest(BaseModel):
    status: str


class FindingReviewRequest(BaseModel):
    status: Optional[str] = Field(None, max_length=40)
    comment: Optional[str] = Field(None, max_length=4000)
    evidence_reference: Optional[str] = Field(None, max_length=4000)
    document_name: Optional[str] = Field(None, max_length=240)
    document_url: Optional[str] = Field(None, max_length=2000)
    resolution: Optional[str] = Field(None, max_length=4000)


# =========================================================
# DETERMINISTIC-RESPONSE CACHE
# The audit-intel endpoints below are pure functions of the static
# project record and risk_scores table. Caching them removes repeated
# computation on drawer re-opens. The cache is cleared whenever the
# dataset changes (sync/upload) so results can never go stale.
# =========================================================
_intel_cache: Dict[str, Any] = {}
_intel_cache_lock = threading.Lock()
# Audit dossiers are large (evidence + findings + history). 4096 entries could
# retain hundreds of MB of response objects in a 512MB-class process; 512 still
# covers every project an analyst re-opens in a session while bounding growth.
_INTEL_CACHE_MAX = 512


def _intel_cache_get(key: str):
    with _intel_cache_lock:
        return _intel_cache.get(key)


def _intel_cache_set(key: str, value: Any):
    with _intel_cache_lock:
        if len(_intel_cache) >= _INTEL_CACHE_MAX:
            # Simple bounded-growth guard: drop the oldest half.
            for k in list(_intel_cache.keys())[: _INTEL_CACHE_MAX // 2]:
                _intel_cache.pop(k, None)
        _intel_cache[key] = value


def clear_intel_cache():
    with _intel_cache_lock:
        _intel_cache.clear()
    # The vendor-intelligence caches are keyed on the dataset contents and
    # now carry long TTLs, so dataset changes (CSV upload / sync) must drop
    # them as well. clear_cache() is invoked by the other data-mutation
    # endpoints and also clears the vendor match cache.
    clear_cache("vendor_intelligence")
    with _match_cache_lock:
        _match_cache.clear()


def _project_or_404(db: Session, project_id: int):
    project = db.query(models.Project).filter(models.Project.id == project_id).first()
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project with ID {project_id} not found",
        )
    # Attach the authoritative per-project spend (payment ledger via
    # project_rec_info) so audit-intel analyses never read the zeroed
    # catalog stamp.
    _apply_authoritative_expenditure([project], db)
    return project


@app.get("/ai/evidence-gaps/{project_id}", tags=["AI Operations"])
def get_evidence_gaps(
    project_id: int = Path(..., description="Project ID"),
    db: Session = Depends(get_db),
):
    """
    "What evidence is missing?" — evidence availability, data confidence and
    explicit assessment limitations, built from the actual dataset fields and
    the conservatively matched work-level records.
    """
    project = _project_or_404(db, project_id)
    cached = _intel_cache_get(f"eg_{project_id}")
    if cached is not None:
        return cached
    result = audit_intel.evidence_gaps(project)
    _intel_cache_set(f"eg_{project_id}", result)
    return result


@app.get("/ai/anomaly-explorer/{project_id}", tags=["AI Operations"])
def get_anomaly_explorer(
    project_id: int = Path(..., description="Project ID"),
    db: Session = Depends(get_db),
):
    """
    Anomaly dimensions (financial, progress, administrative, peer deviation,
    data completeness) with observed values, why each matters and what should
    be verified. Separate from the raw "why this record was flagged" list.
    """
    project = _project_or_404(db, project_id)
    cached = _intel_cache_get(f"axp_{project_id}")
    if cached is not None:
        return cached
    result = audit_intel.anomaly_explorer(project, db)
    _intel_cache_set(f"axp_{project_id}", result)
    return result


@app.get("/projects/{project_id}/peer-benchmark", tags=["Projects"])
def get_peer_benchmark(
    project_id: int = Path(..., description="Project ID"),
    scope: str = Query("state", description="constituency | state | national"),
    project_type: Optional[str] = Query(None, description="Category to compare against, or 'all'"),
    band: str = Query("default", description="narrow | default | all"),
    status_filter: Optional[str] = Query(None, alias="status"),
    db: Session = Depends(get_db),
):
    """
    Compare this project with comparable projects using SQL aggregation.

    Aggregates are computed over the whole peer group; the median is computed
    over a documented bounded sample. Comparisons are statistical and are not
    a finding of wrongdoing.
    """
    project = _project_or_404(db, project_id)

    scope_norm = (scope or "state").strip().lower()
    if scope_norm not in ("constituency", "state", "national"):
        raise HTTPException(status_code=400, detail="scope must be one of: constituency, state, national")
    band_norm = (band or "default").strip().lower()
    if band_norm not in ("narrow", "default", "all"):
        raise HTTPException(status_code=400, detail="band must be one of: narrow, default, all")
    if project_type and len(project_type) > 120:
        raise HTTPException(status_code=400, detail="project_type is too long")
    if status_filter and len(status_filter) > 60:
        raise HTTPException(status_code=400, detail="status is too long")

    return audit_intel.peer_benchmark(
        project, db,
        scope=scope_norm,
        project_type=project_type,
        band=band_norm,
        status_filter=status_filter,
    )


@app.post("/ai/simulate/{project_id}", tags=["AI Operations"])
def post_risk_simulation(
    project_id: int = Path(..., description="Project ID"),
    payload: SimulateRequest = None,
    db: Session = Depends(get_db),
):
    """
    "What would change the risk?" — a simulation only.

    Hypothetical values are pushed through the same deterministic rule engine
    (and the same loaded anomaly model) that produced the stored score. Nothing
    is written to the database and the response is labelled as a simulation.
    """
    project = _project_or_404(db, project_id)
    payload = payload or SimulateRequest()
    if (payload.sanctioned_amount is None and payload.expenditure is None
            and payload.completion_percentage is None):
        raise HTTPException(status_code=400, detail="Provide at least one value to simulate")
    return audit_intel.simulate(
        project,
        sanctioned_amount=payload.sanctioned_amount,
        expenditure=payload.expenditure,
        completion_percentage=payload.completion_percentage,
    )


@app.get("/ai/audit-case/{project_id}", tags=["AI Operations"])
def get_audit_case(
    project_id: int = Path(..., description="Project ID"),
    db: Session = Depends(get_db),
):
    """
    Structured audit case assembled from the actual project values, risk/anomaly
    results, peer comparison, evidence gaps and the audit priority tier — plus a
    review-ready escalation DRAFT that is explicitly not submitted anywhere.
    """
    project = _project_or_404(db, project_id)
    investigation = None
    try:
        investigation = audit_intel.get_investigation(db, project)
    except Exception:
        investigation = None
    # Only the project-derived portion is cached; the investigation snapshot
    # (mutable workflow state) is fetched fresh every time.
    cached = _intel_cache_get(f"case_{project_id}")
    base = cached
    if base is None:
        base = audit_intel.build_audit_case(project, db, investigation=None)
        _intel_cache_set(f"case_{project_id}", base)
    if investigation is not None:
        try:
            base = dict(base)
            base["investigation"] = investigation.get("investigation")
        except Exception:
            pass
    return base


@app.get("/investigations/active", tags=["Investigations"])
def list_active_investigations(
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
    current: models.User = Depends(auth.require_role("analyst")),
):
    """List audit investigations in progress (workflow state only)."""
    return audit_intel.active_investigations(db, limit=limit)


@app.get("/investigations/{project_id}", tags=["Investigations"])
def get_project_investigation(
    project_id: int = Path(..., description="Project ID"),
    db: Session = Depends(get_db),
    current: models.User = Depends(auth.require_role("analyst")),
):
    """Return the investigation workspace for a project, or a null investigation."""
    project = _project_or_404(db, project_id)
    payload = audit_intel.get_investigation(db, project)
    if payload is None:
        return {
            "project_id": project_id,
            "investigation": None,
            "items": [],
            "total_items": 0,
            "resolved_items": 0,
            "pending_items": 0,
            "discrepancies": 0,
            "progress_pct": 0.0,
            "status_options": list(audit_intel.INVESTIGATION_STATUSES),
            "item_status_options": list(audit_intel.EVIDENCE_STATUSES),
            "workflow": list(audit_intel.INVESTIGATION_STATUSES),
            "note": "No investigation has been started for this project.",
        }
    return payload


@app.post("/investigations/{project_id}/start", tags=["Investigations"])
def start_project_investigation(
    project_id: int = Path(..., description="Project ID"),
    db: Session = Depends(get_db),
    current: models.User = Depends(auth.require_role("analyst")),
):
    """
    Start (or reopen) the audit investigation workspace for a project.

    The checklist is generated from the anomalies actually detected in this
    project's data. Items are recommended verification actions, not allegations.
    """
    project = _project_or_404(db, project_id)
    return audit_intel.start_investigation(db, project, actor=current)


@app.patch("/investigations/{project_id}", tags=["Investigations"])
def patch_project_investigation(
    project_id: int = Path(..., description="Project ID"),
    payload: InvestigationUpdateRequest = None,
    db: Session = Depends(get_db),
    current: models.User = Depends(auth.require_role("analyst")),
):
    """Update investigation workflow status and/or note."""
    project = _project_or_404(db, project_id)
    payload = payload or InvestigationUpdateRequest()
    try:
        return audit_intel.update_investigation(db, project, payload.status, payload.note, actor=current)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.patch("/investigations/{project_id}/items/{item_key}", tags=["Investigations"])
def patch_investigation_item(
    project_id: int = Path(..., description="Project ID"),
    item_key: str = Path(..., description="Checklist item key"),
    payload: EvidenceItemUpdateRequest = None,
    db: Session = Depends(get_db),
    current: models.User = Depends(auth.require_role("analyst")),
):
    """Mark one checklist item Pending / Verified / Discrepancy Found / Not Applicable."""
    project = _project_or_404(db, project_id)
    if payload is None or not payload.status:
        raise HTTPException(status_code=400, detail="status is required")
    if len(item_key) > 80:
        raise HTTPException(status_code=400, detail="item_key is too long")
    try:
        return audit_intel.update_evidence_item(db, project, item_key, payload.status, actor=current)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.patch("/investigations/{project_id}/findings/{finding_key}", tags=["Investigations"])
def patch_investigation_finding(
    project_id: int = Path(..., description="Project ID"),
    finding_key: str = Path(..., description="Evidence-linked finding key"),
    payload: FindingReviewRequest = None,
    db: Session = Depends(get_db),
    current: models.User = Depends(auth.require_role("analyst")),
):
    """Record a reviewer decision for one generated finding.

    The finding itself is recomputed from live records; only this human review
    decision is persisted. A reduced risk score never changes this status.
    """
    project = _project_or_404(db, project_id)
    payload = payload or FindingReviewRequest()
    if len(finding_key) > 80:
        raise HTTPException(status_code=400, detail="finding_key is too long")
    if all(value is None for value in (
        payload.status, payload.comment, payload.evidence_reference,
        payload.document_name, payload.document_url, payload.resolution,
    )):
        raise HTTPException(status_code=400, detail="Provide a review change")
    try:
        return audit_intel.update_finding_review(
            db, project, finding_key, actor=current,
            status_value=payload.status, comment=payload.comment,
            evidence_reference=payload.evidence_reference,
            document_name=payload.document_name, document_url=payload.document_url,
            resolution=payload.resolution,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/investigations/{project_id}/findings/{finding_key}/action", tags=["Investigations"])
def add_investigation_finding_action(
    project_id: int = Path(..., description="Project ID"),
    finding_key: str = Path(..., description="Evidence-linked finding key"),
    db: Session = Depends(get_db),
    current: models.User = Depends(auth.require_role("analyst")),
):
    """Convert a finding recommendation into an existing checklist action item."""
    project = _project_or_404(db, project_id)
    if len(finding_key) > 80:
        raise HTTPException(status_code=400, detail="finding_key is too long")
    try:
        return audit_intel.add_recommendation_action(db, project, finding_key, actor=current)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/investigations/{project_id}/report", tags=["Investigations"])
def get_investigation_report(
    project_id: int = Path(..., description="Project ID"),
    db: Session = Depends(get_db),
    current: models.User = Depends(auth.require_role("analyst")),
):
    """Generate a downloadable preliminary investigation report from live records."""
    project = _project_or_404(db, project_id)
    try:
        return audit_intel.compose_investigation_report(db, project)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


# =========================================================
# SERVER-SIDE REPORT EXPORT
# =========================================================
# SIMILAR PROJECTS
# =========================================================

@app.get("/projects/{project_id}/similar", tags=["Projects"])
def get_similar_projects(
    project_id: int = Path(...),
    limit: int = Query(5, ge=1, le=20),
    db: Session = Depends(get_db)
):
    """Find similar projects based on state, category, and sanctioned amount range."""
    project = db.query(models.Project).filter(models.Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")

    # Build similarity criteria
    sanctioned = project.sanctioned_amount or 0
    low_range = sanctioned * 0.3
    high_range = sanctioned * 3.0 if sanctioned > 0 else 999999999

    query = (
        db.query(models.Project)
        .filter(models.Project.id != project_id)
        .filter(models.Project.state == project.state)
        .filter(models.Project.project_type == project.project_type)
        .filter(models.Project.sanctioned_amount >= low_range)
        .filter(models.Project.sanctioned_amount <= high_range)
    )
    # House-aware "Project Twins": when the anchor's house is known, twins
    # come only from the same house (LS and RS works differ in scale and
    # funding structure). Unattributed anchors keep the wider net.
    if getattr(project, "house", None) in ("Lok Sabha", "Rajya Sabha"):
        query = query.filter(models.Project.house == project.house)

    similar = query.limit(limit).all()

    # Get risk scores for similar projects
    similar_ids = [p.id for p in similar]
    risk_map = {}
    if similar_ids:
        risks = db.query(models.RiskScore).filter(models.RiskScore.project_id.in_(similar_ids)).all()
        risk_map = {r.project_id: r for r in risks}

    # Linked spend for the similar-projects rows (one batched lookup).
    _sim_rec = _rec_info_map(db, [p.id for p in similar])
    result = []
    for p in similar:
        r = risk_map.get(p.id)
        result.append({
            "id": p.id,
            "project_name": p.project_name,
            "state": p.state,
            "district": p.district,
            "constituency": p.constituency,
            "project_type": p.project_type,
            "sanctioned_amount": p.sanctioned_amount or 0,
            "expenditure": float((_sim_rec.get(p.id) or {}).get("linked_expenditure") or 0.0),
            "completion_percentage": p.completion_percentage or 0,
            "status": p.status,
            "risk_score": r.risk_score if r else 0,
            "risk_level": r.risk_level if r else "None",
            "ml_anomaly": r.ml_anomaly if r else False,
        })

    return {
        "criteria": {
            "state": project.state,
            "project_type": project.project_type,
            "sanctioned_range": f"{low_range:,.0f} - {high_range:,.0f}"
        },
        "similar_projects": result
    }


# =========================================================
# ANOMALY DISTRIBUTION ANALYTICS
# =========================================================

@app.get("/anomalies/analytics", tags=["Anomaly Detection"])
def anomaly_analytics(
    state: Optional[str] = Query(None),
    constituency: Optional[str] = Query(None),
    fy: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Return anomaly distribution analytics, optionally filtered."""
    cache_key = f"anomaly_analytics_{state or ''}_{constituency or ''}_{fy or ''}"
    cached = get_cached(cache_key, ttl_seconds=300)
    if cached is not None:
        return cached

    # ── QUERY 1: Risk distribution + total + ml_count in a single GROUP BY ──
    base_filter = []
    if state:
        base_filter.append(models.Project.state == state)
    if constituency:
        base_filter.append(models.Project.constituency == constituency)
    if fy:
        base_filter.append(models.Project.fy == fy)

    # Single query: GROUP BY risk_level + aggregate ml_anomaly count
    dist_q = (
        db.query(
            models.RiskScore.risk_level,
            func.count(models.RiskScore.id).label("cnt"),
            func.sum(case((models.RiskScore.ml_anomaly == True, 1), else_=0)).label("ml_cnt")
        )
        .join(models.Project, models.RiskScore.project_id == models.Project.id)
    )
    if base_filter:
        dist_q = dist_q.filter(*base_filter)
    dist_rows = dist_q.group_by(models.RiskScore.risk_level).all()

    total = 0
    level_map = {"high": 0, "medium": 0, "low": 0, "none": 0}
    ml_count = 0
    for row in dist_rows:
        total += row.cnt
        ml_count += int(row.ml_cnt or 0)
        key = (row.risk_level or "").lower()
        if key in level_map:
            level_map[key] = row.cnt

    # ── QUERY 2: Anomaly reasons categorization (Python-side) ──
    reasons_q = (
        db.query(models.RiskScore.reasons)
        .join(models.Project, models.RiskScore.project_id == models.Project.id)
        .filter(models.RiskScore.reasons.isnot(None), models.RiskScore.reasons != "")
    )
    if base_filter:
        reasons_q = reasons_q.filter(*base_filter)
    all_reasons = reasons_q.all()

    type_counts = {}
    for (reasons_str,) in all_reasons:
        for reason in reasons_str.split(","):
            reason = reason.strip()
            if not reason:
                continue
            r_lower = reason.lower()
            if "exceeds sanctioned" in r_lower:
                cat = "Cost Overrun"
            elif "high expenditure" in r_lower:
                cat = "Expenditure-Progress Mismatch"
            elif "0% physical" in r_lower:
                cat = "Zero Progress Disbursement"
            elif "completed but" in r_lower:
                cat = "Status Inconsistency"
            elif "high-value project" in r_lower:
                cat = "High-Value Stalled Project"
            elif "high completion" in r_lower:
                cat = "Completion-Expenditure Mismatch"
            elif "very low fund" in r_lower:
                cat = "Low Fund Utilization"
            elif "ml" in r_lower:
                cat = "ML Statistical Outlier"
            else:
                cat = "Other"
            type_counts[cat] = type_counts.get(cat, 0) + 1

    # ── QUERY 3: State distribution ──
    state_q = (
        db.query(
            models.Project.state,
            func.count(models.RiskScore.id).label("anomaly_count")
        )
        .join(models.RiskScore, models.Project.id == models.RiskScore.project_id)
        .filter(models.RiskScore.risk_score > 0)
    )
    if state:
        state_q = state_q.filter(models.Project.state == state)
    if constituency:
        state_q = state_q.filter(models.Project.constituency == constituency)
    if fy:
        state_q = state_q.filter(models.Project.fy == fy)
    state_rows = (
        state_q.group_by(models.Project.state)
        .order_by(func.count(models.RiskScore.id).desc())
        .limit(10)
        .all()
    )

    # ── QUERY 4: FY distribution ──
    fy_q = (
        db.query(
            models.Project.fy,
            func.count(models.RiskScore.id).label("anomaly_count")
        )
        .join(models.RiskScore, models.Project.id == models.RiskScore.project_id)
        .filter(models.Project.fy.isnot(None), models.Project.fy != "")
    )
    if state:
        fy_q = fy_q.filter(models.Project.state == state)
    if constituency:
        fy_q = fy_q.filter(models.Project.constituency == constituency)
    fy_rows = (
        fy_q.group_by(models.Project.fy)
        .order_by(models.Project.fy.asc())
        .all()
    )

    result = {
        "total_scored": total,
        "risk_distribution": {
            "high": level_map["high"],
            "medium": level_map["medium"],
            "low": level_map["low"],
            "none": level_map["none"],
        },
        "ml_anomaly_count": ml_count,
        "anomaly_types": [
            {"type": k, "count": v, "percentage": round(v / total * 100, 2) if total > 0 else 0}
            for k, v in sorted(type_counts.items(), key=lambda x: x[1], reverse=True)
        ],
        "state_distribution": [
            {"state": r.state, "count": r.anomaly_count}
            for r in state_rows
        ],
        "fy_distribution": [
            {"fy": r.fy, "count": r.anomaly_count}
            for r in fy_rows
        ],
    }

    set_cached(cache_key, result)
    return result


@app.get("/anomalies/scatter-data", tags=["Anomaly Detection"])
def anomaly_scatter_data(
    state: Optional[str] = Query(None),
    constituency: Optional[str] = Query(None),
    fy: Optional[str] = Query(None),
    limit: int = Query(2000, ge=100, le=5000),
    db: Session = Depends(get_db),
):
    """Return sampled project data for scatter plot: expenditure ratio vs physical progress."""
    cache_key = f"scatter_data_{state or ''}_{constituency or ''}_{fy or ''}_{limit}"
    cached = get_cached(cache_key, ttl_seconds=300)
    if cached is not None:
        return cached

    q = (
        db.query(
            models.Project.id,
            models.Project.project_name,
            models.Project.state,
            models.Project.sanctioned_amount,
            models.Project.expenditure,
            models.Project.completion_percentage,
            models.RiskScore.risk_level,
            models.RiskScore.risk_score,
            models.RiskScore.ml_anomaly,
        )
        .join(models.RiskScore, models.Project.id == models.RiskScore.project_id)
        .filter(models.Project.sanctioned_amount > 0)
    )
    if state:
        q = q.filter(models.Project.state == state)
    if constituency:
        q = q.filter(models.Project.constituency == constituency)
    if fy:
        q = q.filter(models.Project.fy == fy)

    rows = q.limit(limit).all()
    # Per-project spend from the payment ledger (conservative linkage); the
    # catalog's expenditure column is a zeroed legacy stamp.
    _proj_exp = metrics_svc.project_expenditure_map(db)
    result = [
        {
            "id": r.id,
            "name": r.project_name or "",
            "state": r.state or "",
            "expenditure_ratio": round(_proj_exp.get(r.id, 0.0) / r.sanctioned_amount * 100, 2) if r.sanctioned_amount > 0 else 0,
            "progress": r.completion_percentage or 0,
            "sanctioned": r.sanctioned_amount or 0,
            "expenditure": _proj_exp.get(r.id, 0.0),
            "risk_level": r.risk_level or "None",
            "risk_score": r.risk_score or 0,
            "ml_anomaly": bool(r.ml_anomaly),
        }
        for r in rows
    ]

    set_cached(cache_key, result)
    return result


# =========================================================
# SERVER-SIDE REPORT EXPORT
# =========================================================

@app.get("/export/ai-audit-summary", tags=["Export"])
def ai_audit_summary(
    state: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    """Narrative AI Audit Report built from live SQL aggregates.

    Every number comes from a real COUNT/SUM query; nothing is invented.
    Where the dataset cannot support a section, it says so explicitly.
    Indicators are neutral review priorities, never fraud claims.
    """
    from collections import Counter

    pq = db.query(
        func.count(models.Project.id).label("total"),
        func.coalesce(func.sum(models.Project.sanctioned_amount), 0.0).label("sanc"),
        func.sum(case((models.Project.status == "Completed", 1), else_=0)).label("completed"),
        func.sum(case((models.Project.status == "Ongoing", 1), else_=0)).label("ongoing"),
    )
    rq = db.query(models.RiskScore)
    if state:
        pq = pq.filter(models.Project.state == state)
        rq = rq.join(models.Project, models.Project.id == models.RiskScore.project_id).filter(models.Project.state == state)
    total, sanc, _completed_cat, ongoing = pq.one()
    # Authoritative expenditure + completions from the ledgers (metrics_svc).
    led = metrics_svc.ledger_totals_scoped(db, state=state) if state else metrics_svc.ledger_totals(db)
    exp = led["total_expenditure"]
    completed = led["completed_works"]
    total_r = rq.count()
    high = rq.filter(models.RiskScore.risk_level.ilike("high")).count()
    medium = rq.filter(models.RiskScore.risk_level.ilike("medium")).count()
    anomalies = rq.filter(models.RiskScore.ml_anomaly.is_(True)).count()

    flagged = (
        db.query(models.RiskScore.risk_score, models.RiskScore.reasons, models.Project.state)
        .join(models.Project, models.Project.id == models.RiskScore.project_id)
        .filter(models.RiskScore.risk_score >= 60)
        .order_by(models.RiskScore.risk_score.desc())
        .limit(400)
        .all()
    )
    counter = Counter()
    for _score, reasons, _st in flagged:
        for part in (reasons or "").split(";"):
            label = part.strip().split(":")[0].strip()
            if label:
                counter[label] += 1
    top_reasons = counter.most_common(5)

    verifications = db.query(func.count(models.VerificationReport.id)).scalar() or 0
    inquiries_total = db.query(func.count(models.Inquiry.id)).scalar() or 0
    inquiries_open = db.query(func.count(models.Inquiry.id)).filter(models.Inquiry.status == "open").scalar() or 0

    def _money(v):
        if v >= 1e7:
            return f"₹{v / 1e7:,.2f} Cr"
        if v >= 1e5:
            return f"₹{v / 1e5:,.2f} L"
        return f"₹{v:,.0f}"

    lines = [
        "AI AUDIT REPORT — MPLADS PORTFOLIO" + (f" — {state}" if state else ""),
        "Generated — timestamp filled below · figures are recorded dataset values at generation time",
        "",
        "SCOPE",
        f"• Projects: {total:,}" + (f" (state scope: {state})" if state else " (nationwide)"),
        f"• Sanctioned: {_money(sanc)} · Recorded expenditure (payment ledger): {_money(exp)} across {led['expenditure_transactions']:,} transactions",
        f"• Completed works (completions ledger): {completed:,}",
        "• Note: expenditure is aggregated from the transaction-level payment ledger; "
        "payment records cannot be joined to individual recommended works, so "
        "per-project ledger spend is shown only where a work key matches.",
        "",
        "RISK DISTRIBUTION",
        f"• Scored projects: {total_r:,} · High: {high:,} · Medium: {medium:,} · Low: {max(total_r - high - medium, 0):,}",
        f"• ML-flagged statistical anomalies (Isolation Forest): {anomalies:,}",
        "",
        "DOMINANT RISK INDICATORS (top flagged projects, score ≥ 60)",
    ]
    if top_reasons:
        lines += [f"• {label} — {n:,} project(s)" for label, n in top_reasons]
    else:
        lines.append("• No projects currently score at or above the 60-point review threshold.")
    lines += [
        "",
        "GROUND VERIFICATION & INQUIRIES",
        f"• Evidence submissions on record: {verifications:,}",
        f"• Auditor inquiries issued: {inquiries_total:,} (open: {inquiries_open:,})",
        "",
        "STATUS MIX",
        f"• Completed: {completed or 0:,} · Ongoing: {ongoing or 0:,}"
        + (f" · Other/unknown: {max(total - (completed or 0) - (ongoing or 0), 0):,}" if total > (completed or 0) + (ongoing or 0) else ""),
        "",
        "AI-DETECTION NOTICE",
        "• All indicators above are AI/analytical review signals derived from recorded data. "
        "None constitute confirmed fraud; field verification and formal audit remain the "
        "confirmation steps.",
    ]
    import datetime as _dt
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines[1] = f"Generated {stamp} · figures are recorded dataset values at generation time"
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse("\n".join(lines), media_type="text/plain; charset=utf-8")


@app.get("/export/report", tags=["Export"])
def export_report(
    report_type: str = Query("Project Audit Report", description="Report type"),
    state: Optional[str] = Query(None),
    constituency: Optional[str] = Query(None),
    district: Optional[str] = Query(None),
    fy: Optional[str] = Query(None, description="Filter by financial year"),
    status: Optional[str] = Query(None),
    risk_level: Optional[str] = Query(None),
    house: Optional[str] = Query(None, description="House: 'Lok Sabha', 'Rajya Sabha', or omitted for All"),
    sort_by: Optional[str] = Query(None),
    sort_dir: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    """Generate a CSV report server-side using the full dataset.

    Memory architecture (512MB budget): the report is STREAMED row-batch by
    row-batch (SQLAlchemy yield_per + a reused StringIO buffer flushed every
    500 rows). The previous version materialized all 83k Project ORM objects
    at once and triple-buffered the whole CSV — a multi-hundred-MB transient
    spike on an unfiltered export. Peak memory now is one 500-row batch plus
    the small authoritative-expenditure map (~300 entries, catalog-bounded).
    Output bytes are identical to the previous implementation.
    """
    import datetime

    _constituency = constituency or district  # backward compat
    _house = house if house in ("Lok Sabha", "Rajya Sabha") else None

    is_anomaly_report = report_type == "Anomaly Summary Report" and bool(risk_level)

    if is_anomaly_report:
        query = (
            db.query(models.RiskScore, models.Project)
            .join(models.Project, models.RiskScore.project_id == models.Project.id)
        )
        if state:
            query = query.filter(models.Project.state == state)
        if _constituency:
            query = query.filter(models.Project.constituency == _constituency)
        if fy:
            query = query.filter(models.Project.fy == fy)
        if _house:
            query = query.filter(models.Project.house == _house)
        if risk_level:
            query = query.filter(models.RiskScore.risk_level.ilike(risk_level))
        query = query.order_by(models.RiskScore.risk_score.desc())
    else:
        query = db.query(models.Project)
        if state:
            query = query.filter(models.Project.state == state)
        if _constituency:
            query = query.filter(models.Project.constituency == _constituency)
        if fy:
            query = query.filter(models.Project.fy == fy)
        if status:
            query = query.filter(models.Project.status.ilike(status))
        if _house:
            query = query.filter(models.Project.house == _house)
        if sort_by:
            query = apply_sort(query, sort_by, sort_dir)
        else:
            query = query.order_by(models.Project.id.asc())

    # Authoritative linked spend (payment ledger, MP+IDA-verified) — small
    # catalog-bounded map, never the raw 106k-row ledger.
    _exp_map = _authoritative_expenditure_map(db)

    def _generate():
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        if is_anomaly_report:
            writer.writerow([
                "Project ID", "Project Name", "State", "Constituency",
                "Category", "Sanctioned Amount", "Expenditure", "Progress %",
                "Status", "Risk Score", "Risk Level", "ML Anomaly", "Anomaly Reasons"
            ])
        else:
            writer.writerow([
                "Project ID", "Project Name", "State", "Constituency",
                "Category", "Sanctioned Amount", "Expenditure", "Progress %",
                "Status"
            ])
        # BOM for Excel compatibility on the first chunk only.
        yield buffer.getvalue().encode("utf-8-sig")
        buffer.seek(0)
        buffer.truncate(0)

        pending = 0

        def _flush():
            nonlocal pending
            if pending:
                yield_ = buffer.getvalue().encode("utf-8")
                buffer.seek(0)
                buffer.truncate(0)
                pending = 0
                return yield_
            return None

        if is_anomaly_report:
            for risk, proj in query.yield_per(500):
                reasons = [r.strip() for r in (risk.reasons or "").split(",") if r.strip()]
                writer.writerow([
                    proj.id,
                    proj.project_name or "",
                    proj.state or "",
                    proj.constituency or "",
                    proj.project_type or "",
                    proj.sanctioned_amount or 0,
                    _exp_map.get(proj.id, 0.0),
                    proj.completion_percentage or 0,
                    proj.status or "",
                    risk.risk_score,
                    risk.risk_level,
                    "Yes" if risk.ml_anomaly else "No",
                    "; ".join(reasons)
                ])
                pending += 1
                if pending >= 500:
                    chunk = _flush()
                    if chunk:
                        yield chunk
        else:
            for proj in query.yield_per(500):
                writer.writerow([
                    proj.id,
                    proj.project_name or "",
                    proj.state or "",
                    proj.constituency or "",
                    proj.project_type or "",
                    proj.sanctioned_amount or 0,
                    _exp_map.get(proj.id, 0.0),
                    proj.completion_percentage or 0,
                    proj.status or ""
                ])
                pending += 1
                if pending >= 500:
                    chunk = _flush()
                    if chunk:
                        yield chunk

        chunk = _flush()
        if chunk:
            yield chunk

    date_str = datetime.datetime.now().strftime("%Y-%m-%d")
    scope = state or "AllStates"
    filename = f"MPLADS_{report_type.replace(' ', '_')}_{scope}_{date_str}.csv"

    return StreamingResponse(
        _generate(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


@app.get("/export/report-count", tags=["Export"])
def export_report_count(
    report_type: str = Query("Project Audit Report"),
    state: Optional[str] = Query(None),
    constituency: Optional[str] = Query(None),
    district: Optional[str] = Query(None),
    fy: Optional[str] = Query(None, description="Filter by financial year"),
    status: Optional[str] = Query(None),
    risk_level: Optional[str] = Query(None),
    house: Optional[str] = Query(None, description="House: 'Lok Sabha', 'Rajya Sabha', or omitted for All"),
    db: Session = Depends(get_db)
):
    """Return the count of records that would be exported."""
    _constituency = constituency or district  # backward compat
    _house = house if house in ("Lok Sabha", "Rajya Sabha") else None
    if report_type == "Anomaly Summary Report" and risk_level:
        query = (
            db.query(func.count(models.RiskScore.id))
            .join(models.Project, models.RiskScore.project_id == models.Project.id)
        )
        if state:
            query = query.filter(models.Project.state == state)
        if _constituency:
            query = query.filter(models.Project.constituency == _constituency)
        if fy:
            query = query.filter(models.Project.fy == fy)
        if _house:
            query = query.filter(models.Project.house == _house)
        if risk_level:
            query = query.filter(models.RiskScore.risk_level.ilike(risk_level))
        total = query.scalar() or 0
    else:
        query = db.query(func.count(models.Project.id))
        if state:
            query = query.filter(models.Project.state == state)
        if _constituency:
            query = query.filter(models.Project.constituency == _constituency)
        if fy:
            query = query.filter(models.Project.fy == fy)
        if status:
            query = query.filter(models.Project.status.ilike(status))
        if _house:
            query = query.filter(models.Project.house == _house)
        total = query.scalar() or 0

    return {"total_records": total}


# =========================================================
# DATA QUALITY MONITOR
# =========================================================

@app.get("/data-quality", tags=["Data Quality"])
def data_quality_check(db: Session = Depends(get_db)):
    """Analyze the dataset for data quality issues."""
    cached = get_cached("data_quality", ttl_seconds=300)
    if cached is not None:
        return cached

    total = db.query(func.count(models.Project.id)).scalar() or 0
    issues = []

    # 1. Missing project names
    no_name = db.query(func.count(models.Project.id)).filter(
        (models.Project.project_name.is_(None)) | (models.Project.project_name == "")
    ).scalar() or 0
    if no_name > 0:
        issues.append({
            "category": "Missing Data",
            "severity": "Warning",
            "field": "project_name",
            "description": f"{no_name:,} records have missing or empty project names",
            "count": no_name,
            "percentage": round(no_name / total * 100, 2) if total > 0 else 0
        })

    # 2. Missing state
    no_state = db.query(func.count(models.Project.id)).filter(
        (models.Project.state.is_(None)) | (models.Project.state == "")
    ).scalar() or 0
    if no_state > 0:
        issues.append({
            "category": "Missing Data",
            "severity": "Warning",
            "field": "state",
            "description": f"{no_state:,} records have missing or empty state",
            "count": no_state,
            "percentage": round(no_state / total * 100, 2) if total > 0 else 0
        })

    # 3. Missing district
    no_district = db.query(func.count(models.Project.id)).filter(
        (models.Project.district.is_(None)) | (models.Project.district == "")
    ).scalar() or 0
    if no_district > 0:
        issues.append({
            "category": "Missing Data",
            "severity": "Warning",
            "field": "district",
            "description": f"{no_district:,} records have missing or empty district",
            "count": no_district,
            "percentage": round(no_district / total * 100, 2) if total > 0 else 0
        })

    # 4. Missing status
    no_status = db.query(func.count(models.Project.id)).filter(
        (models.Project.status.is_(None)) | (models.Project.status == "")
    ).scalar() or 0
    if no_status > 0:
        issues.append({
            "category": "Missing Data",
            "severity": "Warning",
            "field": "status",
            "description": f"{no_status:,} records have missing or empty status",
            "count": no_status,
            "percentage": round(no_status / total * 100, 2) if total > 0 else 0
        })

    # 5. Null sanctioned amount
    no_sanctioned = db.query(func.count(models.Project.id)).filter(
        (models.Project.sanctioned_amount.is_(None))
    ).scalar() or 0
    if no_sanctioned > 0:
        issues.append({
            "category": "Invalid Financial",
            "severity": "Critical",
            "field": "sanctioned_amount",
            "description": f"{no_sanctioned:,} records have NULL sanctioned amounts",
            "count": no_sanctioned,
            "percentage": round(no_sanctioned / total * 100, 2) if total > 0 else 0
        })

    # 6. Zero sanctioned amount (valid but suspicious)
    zero_sanctioned = db.query(func.count(models.Project.id)).filter(
        models.Project.sanctioned_amount == 0
    ).scalar() or 0
    if zero_sanctioned > 0:
        issues.append({
            "category": "Invalid Financial",
            "severity": "Warning",
            "field": "sanctioned_amount",
            "description": f"{zero_sanctioned:,} records have zero sanctioned amount",
            "count": zero_sanctioned,
            "percentage": round(zero_sanctioned / total * 100, 2) if total > 0 else 0
        })

    # 7. Negative sanctioned amount
    neg_sanctioned = db.query(func.count(models.Project.id)).filter(
        models.Project.sanctioned_amount < 0
    ).scalar() or 0
    if neg_sanctioned > 0:
        issues.append({
            "category": "Invalid Financial",
            "severity": "Critical",
            "field": "sanctioned_amount",
            "description": f"{neg_sanctioned:,} records have negative sanctioned amounts",
            "count": neg_sanctioned,
            "percentage": round(neg_sanctioned / total * 100, 2) if total > 0 else 0
        })

    # 8. Negative expenditure
    neg_expenditure = db.query(func.count(models.Project.id)).filter(
        models.Project.expenditure < 0
    ).scalar() or 0
    if neg_expenditure > 0:
        issues.append({
            "category": "Invalid Financial",
            "severity": "Critical",
            "field": "expenditure",
            "description": f"{neg_expenditure:,} records have negative expenditure values",
            "count": neg_expenditure,
            "percentage": round(neg_expenditure / total * 100, 2) if total > 0 else 0
        })

    # 9. Progress outside 0-100%
    invalid_progress = db.query(func.count(models.Project.id)).filter(
        (
            (models.Project.completion_percentage < 0) |
            (models.Project.completion_percentage > 100)
        ) & (models.Project.completion_percentage.isnot(None))
    ).scalar() or 0
    if invalid_progress > 0:
        issues.append({
            "category": "Invalid Progress",
            "severity": "Critical",
            "field": "completion_percentage",
            "description": f"{invalid_progress:,} records have progress values outside 0-100%",
            "count": invalid_progress,
            "percentage": round(invalid_progress / total * 100, 2) if total > 0 else 0
        })

    # 10. Status/Progress inconsistency (completed + <90%)
    status_progress_mismatch = db.query(func.count(models.Project.id)).filter(
        models.Project.status.ilike("completed"),
        models.Project.completion_percentage < 90
    ).scalar() or 0
    if status_progress_mismatch > 0:
        issues.append({
            "category": "Inconsistency",
            "severity": "Warning",
            "field": "status vs completion_percentage",
            "description": f"{status_progress_mismatch:,} records marked 'Completed' but progress < 90%",
            "count": status_progress_mismatch,
            "percentage": round(status_progress_mismatch / total * 100, 2) if total > 0 else 0
        })

    # 11. Expenditure > Sanctioned (potential overrun) — measured against the
    # authoritative linked payment-ledger spend (project_rec_info), because
    # the catalog's expenditure column is a zeroed legacy stamp.
    overspent = db.query(func.count(models.Project.id)).join(
        models.ProjectRecInfo, models.ProjectRecInfo.project_id == models.Project.id
    ).filter(
        models.ProjectRecInfo.linked_expenditure > models.Project.sanctioned_amount,
        models.Project.sanctioned_amount > 0
    ).scalar() or 0
    if overspent > 0:
        issues.append({
            "category": "Inconsistency",
            "severity": "Critical",
            "field": "expenditure vs sanctioned_amount",
            "description": f"{overspent:,} records have linked payment-ledger expenditure exceeding sanctioned amount",
            "count": overspent,
            "percentage": round(overspent / total * 100, 2) if total > 0 else 0
        })

    # 12. Payments recorded with progress > 0 recorded as zero — now measured
    # against the linked ledger spend (catalog column is a zeroed stamp).
    zero_exp_with_progress = db.query(func.count(models.Project.id)).join(
        models.ProjectRecInfo, models.ProjectRecInfo.project_id == models.Project.id
    ).filter(
        models.ProjectRecInfo.has_expenditure.is_(True),
        models.Project.completion_percentage > 0,
        func.coalesce(models.ProjectRecInfo.linked_expenditure, 0.0) == 0
    ).scalar() or 0

    # 13. Duplicate IDs check
    dup_query = db.query(
        models.Project.id, func.count(models.Project.id).label("cnt")
    ).group_by(models.Project.id).having(func.count(models.Project.id) > 1)
    dup_count = dup_query.count()
    if dup_count > 0:
        issues.append({
            "category": "Duplicate Data",
            "severity": "Critical",
            "field": "id",
            "description": f"{dup_count} duplicate project IDs found",
            "count": dup_count,
            "percentage": round(dup_count / total * 100, 2) if total > 0 else 0
        })

    total_issues = sum(i["count"] for i in issues)
    # Overrun test uses the authoritative linked payment-ledger spend
    # (project_rec_info); the catalog's expenditure column is a zeroed stamp.
    records_with_issues = (
        db.query(func.count(func.distinct(models.Project.id))).outerjoin(
            models.ProjectRecInfo, models.ProjectRecInfo.project_id == models.Project.id
        ).filter(
            (
                (models.Project.project_name.is_(None)) |
                (models.Project.project_name == "") |
                (models.Project.state.is_(None)) |
                (models.Project.state == "") |
                (models.Project.sanctioned_amount.is_(None)) |
                (models.Project.sanctioned_amount < 0) |
                (models.Project.expenditure < 0) |
                (
                    (models.Project.completion_percentage < 0) |
                    (models.Project.completion_percentage > 100)
                ) & (models.Project.completion_percentage.isnot(None)) |
                (
                    func.coalesce(models.ProjectRecInfo.linked_expenditure, 0.0) > models.Project.sanctioned_amount
                ) & (models.Project.sanctioned_amount > 0)
            )
        ).scalar() or 0
    )

    result = {
        "total_records_checked": total,
        "records_with_issues": records_with_issues,
        "total_issues": total_issues,
        "issue_categories": len(issues),
        "percentage_affected": round(records_with_issues / total * 100, 2) if total > 0 else 0,
        "issues": issues
    }

    set_cached("data_quality", result)
    return result


@app.get("/data-quality/records", tags=["Data Quality"])
def data_quality_records(
    field: str = Query(..., description="Issue field identifier"),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """Return project records matching a specific data quality issue type."""
    q = db.query(models.Project)
    flag_reason = ""

    if field == "project_name":
        q = q.filter((models.Project.project_name.is_(None)) | (models.Project.project_name == ""))
        flag_reason = "Project name is missing or empty"
    elif field == "state":
        q = q.filter((models.Project.state.is_(None)) | (models.Project.state == ""))
        flag_reason = "State is missing or empty"
    elif field == "district":
        q = q.filter((models.Project.district.is_(None)) | (models.Project.district == ""))
        flag_reason = "District is missing or empty"
    elif field == "status":
        q = q.filter((models.Project.status.is_(None)) | (models.Project.status == ""))
        flag_reason = "Status is missing or empty"
    elif field == "sanctioned_amount":
        q = q.filter(
            (models.Project.sanctioned_amount.is_(None)) |
            (models.Project.sanctioned_amount == 0) |
            (models.Project.sanctioned_amount < 0)
        )
        flag_reason = "Sanctioned amount is NULL, zero, or negative"
    elif field == "expenditure":
        q = q.filter(models.Project.expenditure < 0)
        flag_reason = "Expenditure is negative"
    elif field == "completion_percentage":
        q = q.filter(
            models.Project.completion_percentage.isnot(None),
            ((models.Project.completion_percentage < 0) | (models.Project.completion_percentage > 100))
        )
        flag_reason = "Progress is outside 0-100% range"
    elif field == "status vs completion_percentage":
        q = q.filter(
            models.Project.status.ilike("completed"),
            models.Project.completion_percentage < 90
        )
        flag_reason = "Status is 'Completed' but progress < 90%"
    elif field == "expenditure vs sanctioned_amount":
        # Linked payment-ledger spend vs sanctioned (catalog column is a
        # zeroed legacy stamp).
        q = q.outerjoin(
            models.ProjectRecInfo, models.ProjectRecInfo.project_id == models.Project.id
        ).filter(
            func.coalesce(models.ProjectRecInfo.linked_expenditure, 0.0) > models.Project.sanctioned_amount,
            models.Project.sanctioned_amount > 0
        )
        flag_reason = "Linked payment-ledger expenditure exceeds sanctioned amount"
    elif field == "expenditure vs completion_percentage":
        q = q.outerjoin(
            models.ProjectRecInfo, models.ProjectRecInfo.project_id == models.Project.id
        ).filter(
            func.coalesce(models.ProjectRecInfo.linked_expenditure, 0.0) == 0,
            models.Project.completion_percentage > 0
        )
        flag_reason = "Progress > 0% but no linked payment-ledger expenditure"
    elif field == "id":
        # Find duplicate IDs
        dup_subq = (
            db.query(models.Project.id)
            .group_by(models.Project.id)
            .having(func.count(models.Project.id) > 1)
            .subquery()
        )
        q = q.filter(models.Project.id.in_(db.query(dup_subq)))
        flag_reason = "Duplicate project ID"
    else:
        # Unknown field — return empty
        return {"records": [], "total": 0, "flag_reason": "Unknown issue type", "skip": skip, "limit": limit}

    total = q.count()
    records = q.order_by(models.Project.id).offset(skip).limit(limit).all()

    # Linked spend for the returned rows (authoritative per-project spend).
    _rec_map = _rec_info_map(db, [p.id for p in records])
    return {
        "records": [
            {
                "id": p.id,
                "project_name": p.project_name or "",
                "state": p.state or "",
                "constituency": p.constituency or "",
                "sanctioned_amount": p.sanctioned_amount or 0,
                "expenditure": float((_rec_map.get(p.id) or {}).get("linked_expenditure") or 0.0),
                "completion_percentage": p.completion_percentage or 0,
                "status": p.status or "",
                "project_type": p.project_type or "",
            }
            for p in records
        ],
        "total": total,
        "flag_reason": flag_reason,
        "skip": skip,
        "limit": limit,
    }


# =========================================================
# FEATURE 1: AI RISK EXPLANATION
# =========================================================

@app.get("/ai/risk-explanation/{project_id}", tags=["AI Operations"])
def get_risk_explanation(
    project_id: int = Path(..., description="Project ID"),
    db: Session = Depends(get_db),
):
    """Return a structured, human-readable explanation of a project's risk score."""
    project = db.query(models.Project).filter(models.Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    from ml.predictor import attach_authoritative_expenditure as _attach
    _attach([project], db)
    risk_info = predict_risk(project)
    sanctioned = float(project.sanctioned_amount or 0)
    expenditure = float(metrics_svc.resolve_project_expenditure(db, project))
    completion = float(project.completion_percentage or 0)
    utilization = (expenditure / sanctioned * 100) if sanctioned > 0 else 0
    status_str = str(project.status or "").lower()

    # Build structured explanation
    contributing_factors = []
    explanation = risk_info.get("explanation", {})
    risk_factors = explanation.get("risk_factors", [])

    for factor in risk_factors:
        contributing_factors.append({
            "factor": factor.get("explanation", ""),
            "source": factor.get("source", "rule"),
            "impact": factor.get("impact", None),
        })

    # If no ML/rule factors, generate transparent data-based explanation
    if not contributing_factors:
        if sanctioned > 0 and expenditure > sanctioned:
            contributing_factors.append({
                "factor": f"Expenditure (₹{expenditure:,.0f}) exceeds sanctioned amount (₹{sanctioned:,.0f})",
                "source": "data",
            })
        if expenditure > 0 and completion == 0:
            contributing_factors.append({
                "factor": f"₹{expenditure:,.0f} spent with 0% physical progress",
                "source": "data",
            })
        if sanctioned > 0 and utilization >= 80 and completion < 50:
            contributing_factors.append({
                "factor": f"Fund utilization at {utilization:.0f}% but physical progress only {completion:.0f}%",
                "source": "data",
            })
        if status_str == "completed" and completion < 90:
            contributing_factors.append({
                "factor": f"Project marked as completed but physical progress is {completion:.0f}%",
                "source": "data",
            })

    # Performance context
    performance_context = {
        "sanctioned_amount": sanctioned,
        "expenditure": expenditure,
        "completion_percentage": completion,
        "fund_utilization_pct": round(utilization, 2),
        "status": project.status or "",
    }

    # Check for stale progress data (separate from ML risk)
    stale_check = _check_stale_progress(project)

    # ── Data Confidence: based on field completeness ──
    fields_present = 0
    fields_total = 4
    if sanctioned > 0: fields_present += 1
    if expenditure > 0: fields_present += 1
    if completion > 0: fields_present += 1
    if project.status and project.status.strip(): fields_present += 1
    data_confidence = "High" if fields_present >= 3 else ("Medium" if fields_present >= 2 else "Low")
    # Zero expenditure + zero progress on sanctioned project → lower confidence
    if sanctioned > 0 and expenditure == 0 and completion == 0:
        data_confidence = "Low"

    # ── Risk Signals breakdown ──
    risk_signals = []
    if sanctioned > 0:
        spend_ratio = (expenditure / sanctioned * 100) if sanctioned > 0 else 0
        risk_signals.append({
            "signal": "Expenditure / Sanction",
            "value": f"{spend_ratio:.1f}%",
            "raw_value": round(spend_ratio, 1),
            "impact": "HIGH" if spend_ratio > 100 else ("MEDIUM" if spend_ratio > 80 else "LOW"),
        })
    else:
        risk_signals.append({"signal": "Expenditure / Sanction", "value": "N/A", "raw_value": None, "impact": "N/A"})

    risk_signals.append({
        "signal": "Reported Physical Progress",
        "value": f"{completion}%",
        "raw_value": completion,
        "impact": "HIGH" if completion == 0 else ("MEDIUM" if completion < 50 else "LOW"),
    })

    if sanctioned > 0:
        risk_signals.append({
            "signal": "Project Value",
            "value": f"₹{sanctioned:,.0f}",
            "raw_value": sanctioned,
            "impact": "MEDIUM" if sanctioned >= 1000000 else "LOW",
        })

    risk_signals.append({
        "signal": "Recorded Expenditure",
        "value": f"₹{expenditure:,.0f}",
        "raw_value": expenditure,
        "impact": "HIGH" if (sanctioned > 0 and expenditure > sanctioned) else ("MEDIUM" if expenditure > 0 else "LOW"),
    })

    # Overspend calculations
    overspend = None
    overspend_pct = None
    if sanctioned > 0 and expenditure > sanctioned:
        overspend = expenditure - sanctioned
        overspend_pct = (overspend / sanctioned) * 100

    # ── Dynamic AI Recommendation ──
    reasons = risk_info.get("reasons", [])
    risk_level = risk_info["risk_level"]
    recommendation = ""
    if risk_level == "High":
        parts = []
        if sanctioned > 0 and expenditure > sanctioned:
            parts.append(f"Expenditure (₹{expenditure:,.0f}) has exceeded the sanctioned amount (₹{sanctioned:,.0f}). Verify expenditure records and check if additional approvals were obtained.")
        if expenditure > 0 and completion == 0:
            parts.append(f"₹{expenditure:,.0f} has been spent but no physical progress is reported. Verify whether work has commenced and obtain the latest progress update.")
        if sanctioned > 0 and completion < 25 and sanctioned >= 1000000:
            parts.append(f"This is a high-value project (₹{sanctioned:,.0f}) with only {completion}% reported progress. Prioritize for field verification.")
        if status_str == "completed" and completion < 90:
            parts.append(f"Project is marked completed but reported progress is {completion}%. Verify completion status against physical verification records.")
        if risk_info.get("ml_anomaly"):
            parts.append("The ML model has flagged this project as a statistical outlier. Cross-reference with peer projects in the same state/constituency.")
        if not parts:
            parts.append(f"Multiple risk indicators are active for this project (score: {risk_info['risk_score']}/100). Prioritize for financial and progress verification.")
        recommendation = " ".join(parts)
        if data_confidence == "Low":
            recommendation += " Note: Data confidence is low — the assessment is based on incomplete project records."
    elif risk_level == "Medium":
        parts = []
        if completion < 50 and expenditure > 0:
            parts.append(f"Physical progress ({completion}%) is below expected levels for the expenditure incurred (₹{expenditure:,.0f}). Monitor for further delays.")
        if risk_info.get("ml_anomaly"):
            parts.append("An ML-detected anomaly is present. Review project context to determine if the deviation is expected.")
        if not parts:
            parts.append(f"Several risk indicators are partially active (score: {risk_info['risk_score']}/100). Schedule periodic review.")
        recommendation = " ".join(parts)
    elif risk_level == "Low":
        recommendation = f"Minor deviations detected (score: {risk_info['risk_score']}/100). No immediate action required; continue routine monitoring."
    else:
        recommendation = "No significant risk indicators detected. Project metrics appear consistent with expected patterns. Continue standard monitoring."

    result = {
        "project_id": project_id,
        "risk_score": risk_info["risk_score"],
        "risk_level": risk_info["risk_level"],
        "summary": explanation.get("summary", f"Risk level: {risk_info['risk_level']}").strip(),
        "contributing_factors": contributing_factors,
        "performance_context": performance_context,
        "data_sufficiency": "sufficient" if (sanctioned > 0 or expenditure > 0) else "limited",
        "ml_anomaly": risk_info.get("ml_anomaly", False),
        "data_quality": {
            "flag": stale_check["flag"],
            "reason": stale_check["reason"],
        },
        # New enhanced fields
        "data_confidence": data_confidence,
        "indicator_count": len(reasons),
        "risk_signals": risk_signals,
        "overspend": overspend,
        "overspend_pct": round(overspend_pct, 1) if overspend_pct else None,
        "ai_recommendation": recommendation,
    }
    return result


# =========================================================
# FEATURE 3: ANOMALY EXPLANATION
# =========================================================

@app.get("/ai/anomaly-explanation/{project_id}", tags=["AI Operations"])
def get_anomaly_explanation(
    project_id: int = Path(..., description="Project ID"),
    db: Session = Depends(get_db),
):
    """Return detailed anomaly explanation for a specific project."""
    project = db.query(models.Project).filter(models.Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    from ml.predictor import attach_authoritative_expenditure as _attach
    _attach([project], db)
    risk_info = predict_risk(project)
    sanctioned = float(project.sanctioned_amount or 0)
    expenditure = float(metrics_svc.resolve_project_expenditure(db, project))
    completion = float(project.completion_percentage or 0)
    utilization = (expenditure / sanctioned * 100) if sanctioned > 0 else 0

    # Get peer stats for expected values
    expected = {}
    explanation = risk_info.get("explanation", {})
    risk_factors = explanation.get("risk_factors", [])

    # Compute expected reference values from peer stats
    import ml.predictor as _predictor_mod
    _predictor_mod._ensure_model()  # make sure the lazy bundle is loaded
    _peer_stats = _predictor_mod.peer_stats
    if _peer_stats and project.state:
        state_stats = _peer_stats.get("states", {}).get(project.state, {})
        if state_stats:
            expected = {
                "state_avg_completion": round(state_stats.get("avg_completion", 0), 1),
                "state_median_expenditure": state_stats.get("median_expenditure", 0),
                "state_avg_utilization": round(state_stats.get("avg_utilization", 0), 1),
            }
        national = _peer_stats.get("national", {})
        if national:
            expected["national_avg_completion"] = round(national.get("avg_completion", 0), 1)
            expected["national_avg_utilization"] = round(national.get("avg_utilization", 0), 1)

    # Build detected anomalies list
    detected_anomalies = []

    # Rule-based anomalies
    if sanctioned > 0 and expenditure > sanctioned:
        detected_anomalies.append({
            "type": "Cost Overrun",
            "what": f"Expenditure (₹{expenditure:,.0f}) exceeds sanctioned amount (₹{sanctioned:,.0f})",
            "observed": {"expenditure": expenditure, "sanctioned_amount": sanctioned},
            "expected": {"max_expenditure": sanctioned},
            "severity": "High",
            "source": "rule",
            "factors": ["Expenditure may include unapproved costs", "Sanction limits may have been exceeded"]
        })

    if expenditure > 0 and completion == 0:
        detected_anomalies.append({
            "type": "Zero Progress Disbursement",
            "what": f"₹{expenditure:,.0f} spent but physical progress remains at 0%",
            "observed": {"expenditure": expenditure, "completion": 0},
            "expected": {"expected_completion_at_spend_level": min(100, expenditure / max(sanctioned, 1) * 100)},
            "severity": "High",
            "source": "rule",
            "factors": ["Work may not have commenced", "Expenditure may be for mobilization only"]
        })

    if sanctioned > 0 and utilization >= 80 and completion < 50:
        detected_anomalies.append({
            "type": "Expenditure-Progress Mismatch",
            "what": f"Fund utilization at {utilization:.0f}% but physical progress only {completion:.0f}%",
            "observed": {"utilization_pct": round(utilization, 1), "completion_pct": completion},
            "expected": {"expected_completion": round(utilization, 1)},
            "severity": "Medium",
            "source": "rule",
            "factors": ["Financial and physical progress are significantly out of sync", "May indicate premature payments or data recording issues"]
        })

    if str(project.status or "").lower() == "completed" and completion < 90:
        detected_anomalies.append({
            "type": "Status Inconsistency",
            "what": f"Project marked as completed but physical progress is {completion:.0f}%",
            "observed": {"status": project.status, "completion": completion},
            "expected": {"expected_completion": 100},
            "severity": "Medium",
            "source": "rule",
            "factors": ["Status may be incorrectly recorded", "Project may be partially complete"]
        })

    # ML-based anomaly
    if risk_info.get("ml_anomaly"):
        ml_factors = []
        for rf in risk_factors:
            if rf.get("source") == "ml" and rf.get("explanation"):
                ml_factors.append(rf["explanation"])
        detected_anomalies.append({
            "type": "ML Statistical Outlier",
            "what": "Project exhibits patterns statistically unusual compared to peer projects",
            "observed": {"ml_score": risk_info.get("ml_score", 0)},
            "expected": {},
            "severity": "High" if risk_info["risk_level"] == "High" else "Medium",
            "source": "ml",
            "factors": ml_factors[:3] if ml_factors else ["Statistical deviation detected by ML model"]
        })

    # If no anomalies detected, note it
    if not detected_anomalies:
        detected_anomalies.append({
            "type": "No Anomaly Detected",
            "what": "This project does not show significant anomalous patterns",
            "observed": {},
            "expected": {},
            "severity": "None",
            "source": "data",
            "factors": ["Project metrics are within expected ranges for similar projects"]
        })

    severity = risk_info["risk_level"]

    return {
        "project_id": project_id,
        "risk_score": risk_info["risk_score"],
        "risk_level": severity,
        "detected_anomalies": detected_anomalies,
        "reference_values": expected,
        "project_metrics": {
            "sanctioned_amount": sanctioned,
            "expenditure": expenditure,
            "completion_percentage": completion,
            "fund_utilization_pct": round(utilization, 2),
            "status": project.status or "",
        },
    }


# =========================================================
# FEATURE 4: STATE / CONSTITUENCY BENCHMARKING
# =========================================================

@app.get("/ai/benchmarking", tags=["AI Operations"])
def get_benchmarking(
    state: Optional[str] = Query(None, description="State to benchmark"),
    constituency: Optional[str] = Query(None, description="Constituency to benchmark"),
    fy: Optional[str] = Query(None, description="Financial year filter"),
    house: Optional[str] = Query(None, description="House: 'Lok Sabha', 'Rajya Sabha', or omitted for All"),
    db: Session = Depends(get_db),
):
    """Return benchmarking comparison for a state/constituency against national or state averages."""
    if not state and not constituency:
        raise HTTPException(status_code=400, detail="Provide at least a state or constituency")

    house_norm = house if house in ("Lok Sabha", "Rajya Sabha") else None
    cache_key = (
        f"benchmark_{state or ''}_{constituency or ''}_{fy or 'all'}_"
        f"{house_norm or 'all'}"
    )
    cached = get_cached(cache_key, ttl_seconds=120)
    if cached is not None:
        return cached

    # --- National average (authoritative: ledger expenditure via metrics_svc) ---
    nat_stats = metrics_svc.portfolio_stats(db, fy=fy, house=house_norm)

    nat_sanctioned = float(nat_stats["sanctioned"])
    nat_expenditure = float(nat_stats["expenditure"])
    national = {
        "total_projects": int(nat_stats["total_projects"] or 0),
        "avg_completion": round(float(nat_stats["avg_completion"] or 0), 2),
        "utilization_pct": round((nat_expenditure / nat_sanctioned * 100) if nat_sanctioned > 0 else 0, 2),
        "total_sanctioned": nat_sanctioned,
        "total_expenditure": nat_expenditure,
        "completed_projects": int(nat_stats["completed_works"]),
        "ongoing_projects": int(nat_stats["ongoing_projects"] or 0),
    }

    # National risk stats
    nat_risk_q = db.query(
        func.count(models.RiskScore.id).label("total_risk"),
        func.sum(case((models.RiskScore.risk_level.ilike("high"), 1), else_=0)).label("high_risk"),
        func.coalesce(func.avg(models.RiskScore.risk_score), 0).label("avg_risk_score"),
    )
    if fy or house_norm:
        nat_risk_q = nat_risk_q.join(
            models.Project, models.RiskScore.project_id == models.Project.id
        )
    if fy:
        nat_risk_q = nat_risk_q.filter(models.Project.fy == fy)
    if house_norm:
        nat_risk_q = nat_risk_q.filter(models.Project.house == house_norm)
    nat_risk = nat_risk_q.one()
    national["high_risk_projects"] = int(nat_risk.high_risk or 0)
    national["avg_risk_score"] = round(float(nat_risk.avg_risk_score), 1)

    # --- State-level stats ---
    state_stats = None
    state_name = state
    if constituency and not state:
        # Infer state from constituency
        state_row = (
            db.query(models.Project.state)
            .filter(models.Project.constituency == constituency, models.Project.state.isnot(None))
        )
        if house_norm:
            state_row = state_row.filter(models.Project.house == house_norm)
        state_row = state_row.first()
        if state_row:
            state_name = state_row[0]

    if state_name:
        s_stats = metrics_svc.portfolio_stats(
            db, fy=fy, state=state_name, house=house_norm
        )
        s_sanctioned = float(s_stats["sanctioned"])
        s_expenditure = float(s_stats["expenditure"])
        state_stats = {
            "total_projects": int(s_stats["total_projects"] or 0),
            "avg_completion": round(float(s_stats["avg_completion"] or 0), 2),
            "utilization_pct": round((s_expenditure / s_sanctioned * 100) if s_sanctioned > 0 else 0, 2),
            "total_sanctioned": s_sanctioned,
            "total_expenditure": s_expenditure,
            "completed_projects": int(s_stats["completed_works"]),
            "ongoing_projects": int(s_stats["ongoing_projects"] or 0),
        }

        # State risk stats
        sr_q = (
            db.query(
                func.sum(case((models.RiskScore.risk_level.ilike("high"), 1), else_=0)).label("high_risk"),
                func.coalesce(func.avg(models.RiskScore.risk_score), 0).label("avg_risk_score"),
            )
            .join(models.Project, models.RiskScore.project_id == models.Project.id)
            .filter(models.Project.state == state_name)
        )
        if fy:
            sr_q = sr_q.filter(models.Project.fy == fy)
        if house_norm:
            sr_q = sr_q.filter(models.Project.house == house_norm)
        sr = sr_q.one()
        state_stats["high_risk_projects"] = int(sr.high_risk or 0)
        state_stats["avg_risk_score"] = round(float(sr.avg_risk_score), 1)

    # --- Constituency-level stats ---
    cons_stats = None
    if constituency and state_name:
        cq = db.query(models.Project).filter(
            models.Project.state == state_name,
            models.Project.constituency == constituency,
        )
        if fy:
            cq = cq.filter(models.Project.fy == fy)
        if house_norm:
            cq = cq.filter(models.Project.house == house_norm)
        c_stats = cq.with_entities(
            func.count(models.Project.id).label("total"),
            func.coalesce(func.avg(models.Project.completion_percentage), 0).label("avg_completion"),
            func.coalesce(func.sum(models.Project.sanctioned_amount), 0).label("total_sanctioned"),
            func.sum(case((models.Project.status.ilike("ongoing"), 1), else_=0)).label("ongoing"),
        ).one()

        # Ledger expenditure for this constituency: the ledgers carry their own
        # (state, constituency) pair, verified to exist in the catalog 1:1.
        # Pair-keyed because shared labels (e.g. 'Sitting Rajya Sabha') span states.
        led_cons = metrics_svc.constituency_ledger_map(
            db, fy=fy, house=house_norm
        ).get(
            ((state_name or "").strip(), (constituency or "").strip()), {}
        )
        c_sanctioned = float(c_stats.total_sanctioned)
        c_expenditure = float(led_cons.get("expenditure", 0.0))
        cons_stats = {
            "total_projects": int(c_stats.total or 0),
            "avg_completion": round(float(c_stats.avg_completion or 0), 2),
            "utilization_pct": round((c_expenditure / c_sanctioned * 100) if c_sanctioned > 0 else 0, 2),
            "total_sanctioned": c_sanctioned,
            "total_expenditure": c_expenditure,
            "completed_projects": int(led_cons.get("completed", 0)),
            "ongoing_projects": int(c_stats.ongoing or 0),
        }

        cr_q = (
            db.query(
                func.sum(case((models.RiskScore.risk_level.ilike("high"), 1), else_=0)).label("high_risk"),
                func.coalesce(func.avg(models.RiskScore.risk_score), 0).label("avg_risk_score"),
            )
            .join(models.Project, models.RiskScore.project_id == models.Project.id)
            .filter(models.Project.state == state_name, models.Project.constituency == constituency)
        )
        if fy:
            cr_q = cr_q.filter(models.Project.fy == fy)
        if house_norm:
            cr_q = cr_q.filter(models.Project.house == house_norm)
        cr = cr_q.one()
        cons_stats["high_risk_projects"] = int(cr.high_risk or 0)
        cons_stats["avg_risk_score"] = round(float(cr.avg_risk_score), 1)

    # Build comparisons
    comparisons = []

    if state_stats:
        # State vs National
        state_vs_national = []
        metrics = [
            ("avg_completion", "Average Completion", "%", True),
            ("utilization_pct", "Fund Utilization", "%", True),
            ("high_risk_projects", "High-Risk Projects", "", False),
            ("avg_risk_score", "Avg Risk Score", "", False),
        ]
        for key, label, unit, higher_is_better in metrics:
            state_val = state_stats.get(key, 0)
            nat_val = national.get(key, 0)
            diff = state_val - nat_val
            state_vs_national.append({
                "metric": label,
                "selected": state_val,
                "benchmark": nat_val,
                "difference": round(diff, 2),
                "unit": unit,
                "better": diff > 0 if higher_is_better else diff < 0,
            })
        comparisons.append({
            "type": "state_vs_national",
            "label": f"{state_name} vs National Average",
            "selected_name": state_name,
            "benchmark_name": "National Average",
            "metrics": state_vs_national,
        })

    if cons_stats and state_stats:
        # Constituency vs State
        cons_vs_state = []
        for key, label, unit, higher_is_better in metrics:
            cons_val = cons_stats.get(key, 0)
            st_val = state_stats.get(key, 0)
            diff = cons_val - st_val
            cons_vs_state.append({
                "metric": label,
                "selected": cons_val,
                "benchmark": st_val,
                "difference": round(diff, 2),
                "unit": unit,
                "better": diff > 0 if higher_is_better else diff < 0,
            })
        comparisons.append({
            "type": "constituency_vs_state",
            "label": f"{constituency} vs {state_name} Average",
            "selected_name": constituency,
            "benchmark_name": f"{state_name} Average",
            "metrics": cons_vs_state,
        })

    result = {
        "state": state_name,
        "constituency": constituency,
        "fy": fy,
        "house": house_norm or "All",
        "national": national,
        "state_stats": state_stats,
        "constituency_stats": cons_stats,
        "comparisons": comparisons,
    }

    set_cached(cache_key, result)
    return result


# =========================================================
# DATA SYNC / FRESHNESS ENDPOINTS
# =========================================================


@app.get("/api/data/freshness")
def data_freshness():
    """
    Get data freshness status.
    Returns sync status, last sync time, and project count.
    """
    return get_data_freshness()


@app.get("/api/data/sync/history")
def data_sync_history(limit: int = Query(10, ge=1, le=50)):
    """
    Get recent sync history.
    """
    return get_sync_history(limit=limit)


@app.get("/api/data/providers")
def list_data_providers():
    """
    List all available data providers and their capabilities.
    Useful for understanding what sources can supply data.
    """
    return get_available_providers()


@app.post("/api/data/sync")
def trigger_sync(source: str = Query("github", description="Data source provider name")):
    """
    Trigger a data synchronization from any registered provider.

    Available providers:
    - 'github': Vonter/india-mplads-works GitHub dataset (recommended works)
    - 'csv_upload': CSV file upload (use /api/data/upload instead)

    New providers can be added by implementing the DataProvider
    interface in providers/ and registering it.
    """
    try:
        result = sync_from_source(source)
    except KeyError as e:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown sync source: {source}. {str(e)}"
        )
    # Dataset changed — all derived intel caches are now stale.
    clear_intel_cache()
    return result


@app.post("/api/data/upload")
def upload_csv(
    file: UploadFile = File(...),
    delimiter: str = Form(","),
):
    """
    Upload a CSV file to update project data.
    Expected columns (flexible mapping):
    - id, project_name, state, district, constituency
    - project_type, sanctioned_amount, expenditure
    - completion_percentage, status, fy
    """
    if not file.filename.endswith((".csv", ".tsv", ".txt")):
        raise HTTPException(
            status_code=400,
            detail="File must be a CSV, TSV, or TXT file"
        )
    if delimiter == "auto":
        content = file.file.read(4096)
        file.file.seek(0)
        try:
            sniffed = csv.Sniffer().sniff(content.decode("utf-8", errors="replace"))
            delimiter = sniffed.delimiter
        except csv.Error:
            delimiter = ","
    try:
        csv_content = file.file.read().decode("utf-8", errors="replace")
    except Exception as e:
        raise HTTPException(
            status_code=400,
            detail=f"Failed to read file: {str(e)}"
        )
    result = sync_from_csv_upload(
        csv_content=csv_content,
        filename=file.filename,
        delimiter=delimiter,
    )
    # Dataset changed — all derived intel caches are now stale.
    clear_intel_cache()
    return result


@app.get("/api/data/stats")
def data_stats():
    """
    Get dataset statistics.
    """
    db = SessionLocal()
    try:
        total_projects = db.query(func.count(models.Project.id)).scalar() or 0
        total_states = db.query(
            func.count(distinct(models.Project.state))
        ).scalar() or 0
        total_constituencies = db.query(
            func.count(distinct(models.Project.constituency))
        ).scalar() or 0
        total_mps = db.query(func.count(models.MPSummary.id)).scalar() or 0
        total_risk_scores = db.query(func.count(models.RiskScore.id)).scalar() or 0
        fy_rows = db.query(
            models.Project.fy, func.count(models.Project.id)
        ).filter(
            models.Project.fy.isnot(None), models.Project.fy != ""
        ).group_by(models.Project.fy).all()
        fy_breakdown = {r[0]: r[1] for r in fy_rows if r[0]}
        return {
            "total_projects": total_projects,
            "total_states": total_states,
            "total_constituencies": total_constituencies,
            "total_mps": total_mps,
            "total_risk_scores": total_risk_scores,
            "fy_breakdown": fy_breakdown,
        }
    finally:
        db.close()
