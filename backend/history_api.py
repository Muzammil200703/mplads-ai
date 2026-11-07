"""Historical replay endpoints.

Read-only by design: snapshots are written once by the import pipeline and are
never updated here. A checkpoint represents a periodic CSV import, which the
UI states explicitly.

Visibility matches the data: replaying a project is as public as the project
drawer; portfolio-level replay and comparisons are analyst-tier.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

import history_replay as hr
import models
from auth import get_db, require_role

router = APIRouter(prefix="/history", tags=["Historical Replay"])

_analyst = Depends(require_role("analyst"))


@router.get("/snapshots")
def list_snapshots(db: Session = Depends(get_db)):
    """Every replayable checkpoint, oldest first, with its project counts."""
    return hr.list_checkpoints(db)


@router.get("/snapshots/{batch_id}")
def snapshot_detail(batch_id: int, db: Session = Depends(get_db)):
    payload = hr.checkpoint_detail(db, batch_id)
    if payload is None:
        raise HTTPException(status_code=404, detail=f"Import #{batch_id} not found")
    return payload


@router.get("/projects/{project_id}")
def project_at_checkpoint(
    project_id: int,
    batch_id: Optional[int] = Query(None, description="Checkpoint import id; defaults to the latest"),
    db: Session = Depends(get_db),
):
    exists = db.query(models.Project.id).filter(models.Project.id == project_id).first()
    if not exists:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    return hr.project_at(db, project_id, batch_id)


@router.get("/dashboard")
def historical_dashboard(
    batch_id: Optional[int] = Query(None, description="Checkpoint import id; defaults to the latest"),
    house: Optional[str] = Query(None, description="'Lok Sabha' / 'Rajya Sabha' / omitted for All"),
    db: Session = Depends(get_db),
    _user=_analyst,
):
    return hr.dashboard(db, batch_id, house)


@router.get("/compare")
def compare_checkpoints(
    from_batch: int = Query(..., description="Older checkpoint import id"),
    to_batch: int = Query(..., description="Newer checkpoint import id"),
    house: Optional[str] = Query(None, description="'Lok Sabha' / 'Rajya Sabha' / omitted for All"),
    sample_limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _user=_analyst,
):
    if from_batch == to_batch:
        raise HTTPException(status_code=400, detail="Choose two different checkpoints")
    return hr.compare(db, from_batch, to_batch, house, sample_limit=sample_limit)
