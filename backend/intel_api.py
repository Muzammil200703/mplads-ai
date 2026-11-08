"""
MPLADS AI — Advanced intelligence API
=====================================

One router for the Phase 1 analysis engines and Phase 2 geospatial engines.
Every endpoint is read-only over project/financial data; the only writes are
the human review records (reconciliation decisions, investigation memory) and
the agent run store. Roles are enforced server-side:

    analyst+   read every analytical endpoint, run the multi-agent audit,
               append investigation memory
    auditor+   confirm/remove investigation memory, review reconciliation cases
"""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import auth
import geo_intel
import intel_agents
import intel_analysis
import models
from database import SessionLocal

router = APIRouter(prefix="/intel", tags=["Advanced Intelligence"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _filters(state: Optional[str], district: Optional[str],
             house: Optional[str], category: Optional[str]) -> Dict[str, Any]:
    return {
        "state": (state or "").strip() or None,
        "district": (district or "").strip() or None,
        "house": (house or "").strip() or None,
        "category": (category or "").strip() or None,
    }


# ══════════════════════════════════════════════════════════════════════
# Phase 1
# ══════════════════════════════════════════════════════════════════════

@router.get("/root-cause/{project_id}")
def root_cause_graph(
    project_id: int,
    user: models.User = Depends(auth.require_role("analyst")),
    db: Session = Depends(get_db),
):
    payload = intel_analysis.root_cause(db, project_id)
    if payload is None:
        raise HTTPException(404, f"Project {project_id} not found")
    return payload


@router.get("/emerging-risk")
def emerging_risk(
    state: Optional[str] = None,
    district: Optional[str] = None,
    house: Optional[str] = None,
    category: Optional[str] = None,
    user: models.User = Depends(auth.require_role("analyst")),
    db: Session = Depends(get_db),
):
    return intel_analysis.emerging_risk(
        db, _filters(state, district, house, category))


@router.get("/evolution")
def anomaly_evolution(
    state: Optional[str] = None,
    district: Optional[str] = None,
    house: Optional[str] = None,
    category: Optional[str] = None,
    user: models.User = Depends(auth.require_role("analyst")),
    db: Session = Depends(get_db),
):
    return intel_analysis.anomaly_evolution(
        db, _filters(state, district, house, category))


@router.get("/clusters")
def behaviour_clusters(
    state: Optional[str] = None,
    district: Optional[str] = None,
    house: Optional[str] = None,
    category: Optional[str] = None,
    k: Optional[int] = Query(None, ge=2, le=8),
    user: models.User = Depends(auth.require_role("analyst")),
    db: Session = Depends(get_db),
):
    return intel_analysis.clusters(
        db, _filters(state, district, house, category), k)


@router.get("/clusters/{cluster_id}/projects")
def behaviour_cluster_projects(
    cluster_id: int,
    limit: int = Query(50, ge=1, le=200),
    user: models.User = Depends(auth.require_role("analyst")),
):
    return intel_analysis.cluster_projects(cluster_id, limit)


@router.get("/reconciliation")
def reconciliation(
    state: Optional[str] = None,
    district: Optional[str] = None,
    house: Optional[str] = None,
    limit: int = Query(20, ge=1, le=50),
    user: models.User = Depends(auth.require_role("analyst")),
    db: Session = Depends(get_db),
):
    return intel_analysis.reconciliation(
        db, _filters(state, district, house, None), limit)


class ReconReviewIn(BaseModel):
    decision: str = Field(pattern="^(confirmed|dismissed|needs_review)$")
    note: Optional[str] = Field(None, max_length=2000)


@router.post("/reconciliation/review/{case_key}")
def review_case(
    case_key: str,
    payload: ReconReviewIn,
    user: models.User = Depends(auth.require_role("auditor")),
    db: Session = Depends(get_db),
):
    if len(case_key) > 64 or not case_key.strip():
        raise HTTPException(400, "Invalid case key")
    return intel_analysis.review_reconciliation(
        db, case_key, payload.decision, payload.note, user)


# ── Multi-agent audit ────────────────────────────────────────────────

@router.post("/multiagent/{project_id}/run")
def run_multi_agent(
    project_id: int,
    user: models.User = Depends(auth.require_role("analyst")),
    db: Session = Depends(get_db),
):
    project = db.query(models.Project).filter(models.Project.id == project_id).first()
    if project is None:
        raise HTTPException(404, f"Project {project_id} not found")
    try:
        return intel_agents.run_multi_agent(db, project, user)
    except HTTPException:
        raise
    except Exception as exc:
        db.rollback()
        raise HTTPException(500, f"Multi-agent run failed: {exc.__class__.__name__}")


@router.get("/multiagent/{project_id}/latest")
def multi_agent_latest(
    project_id: int,
    user: models.User = Depends(auth.require_role("analyst")),
    db: Session = Depends(get_db),
):
    project = db.query(models.Project).filter(models.Project.id == project_id).first()
    if project is None:
        raise HTTPException(404, f"Project {project_id} not found")
    return intel_agents.multi_agent_payload(db, project)


# ── Investigation memory ─────────────────────────────────────────────

@router.get("/memory/{project_id}")
def investigation_memory(
    project_id: int,
    user: models.User = Depends(auth.require_role("analyst")),
    db: Session = Depends(get_db),
):
    payload = intel_agents.memory_view(db, project_id)
    if not payload.get("available"):
        raise HTTPException(404, payload.get("reason") or "Not found")
    return payload


class MemoryIn(BaseModel):
    kind: str = Field("note", pattern="^(note|finding|pattern|question|evidence)$")
    title: Optional[str] = Field(None, max_length=240)
    body: Optional[str] = Field(None, max_length=4000)
    source: Optional[str] = Field(None, max_length=240)
    confirmed: bool = False
    related_project_id: Optional[int] = None


@router.post("/memory/{project_id}")
def add_investigation_memory(
    project_id: int,
    payload: MemoryIn,
    user: models.User = Depends(auth.require_role("analyst")),
    db: Session = Depends(get_db),
):
    project = db.query(models.Project).filter(models.Project.id == project_id).first()
    if project is None:
        raise HTTPException(404, f"Project {project_id} not found")
    return intel_agents.add_memory(db, project_id, payload.model_dump(), user)


class MemoryPatchIn(BaseModel):
    title: Optional[str] = Field(None, max_length=240)
    body: Optional[str] = Field(None, max_length=4000)
    confirmed: Optional[bool] = None
    status: Optional[str] = Field(None, pattern="^(active|removed)$")


@router.patch("/memory/entry/{entry_id}")
def update_investigation_memory(
    entry_id: int,
    payload: MemoryPatchIn,
    user: models.User = Depends(auth.require_role("analyst")),
    db: Session = Depends(get_db),
):
    result = intel_agents.update_memory(db, entry_id, payload.model_dump(exclude_unset=True), user)
    if result is None:
        raise HTTPException(404, "Memory entry not found")
    if result.get("forbidden"):
        raise HTTPException(403, "Only the author or an auditor can change this entry")
    return result


# ══════════════════════════════════════════════════════════════════════
# Phase 2 — geospatial
# ══════════════════════════════════════════════════════════════════════

@router.get("/geo/coverage")
def geo_coverage(
    state: Optional[str] = None,
    user: models.User = Depends(auth.require_role("analyst")),
    db: Session = Depends(get_db),
):
    return geo_intel.coverage_gaps(db, (state or "").strip() or None)


@router.get("/geo/districts")
def geo_districts(
    state: Optional[str] = None,
    house: Optional[str] = None,
    category: Optional[str] = None,
    user: models.User = Depends(auth.require_role("analyst")),
    db: Session = Depends(get_db),
):
    return geo_intel.district_patterns(db, _filters(state, None, house, category))


@router.get("/geo/spatial")
def geo_spatial(
    state: Optional[str] = None,
    district: Optional[str] = None,
    limit: int = Query(20, ge=1, le=50),
    user: models.User = Depends(auth.require_role("analyst")),
    db: Session = Depends(get_db),
):
    return geo_intel.spatial_relations(db, _filters(state, district, None, None), limit)


@router.get("/geo/progress")
def geo_progress(
    state: Optional[str] = None,
    district: Optional[str] = None,
    house: Optional[str] = None,
    category: Optional[str] = None,
    user: models.User = Depends(auth.require_role("analyst")),
    db: Session = Depends(get_db),
):
    return geo_intel.progress_map(db, _filters(state, district, house, category))


@router.get("/geo/outliers")
def geo_outliers(
    state: Optional[str] = None,
    house: Optional[str] = None,
    category: Optional[str] = None,
    user: models.User = Depends(auth.require_role("analyst")),
    db: Session = Depends(get_db),
):
    return geo_intel.geo_outliers(db, _filters(state, None, house, category))
