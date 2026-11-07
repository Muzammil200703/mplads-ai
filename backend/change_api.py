"""Change-history endpoints for project records.

Mounted under ``/changes`` so the existing ``/projects/{project_id}`` routes
(which parse an integer id) are never shadowed.

Visibility mirrors the data they describe:
* a single project's history is readable by anyone who can open the project
  drawer (the same public catalogue data);
* the cross-project feed and the summary are analyst-tier;
* a batch's change list is admin-only, like every other import endpoint.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

import auth
import change_tracking as ct
import models
from auth import get_db, require_role

router = APIRouter(prefix="/changes", tags=["Change History"])

_analyst = Depends(require_role("analyst"))
_admin = Depends(require_role("admin"))


@router.get("/projects")
def changes_feed(
    date_from: Optional[str] = Query(None, description="ISO date/time lower bound"),
    date_to: Optional[str] = Query(None, description="ISO date upper bound (inclusive day)"),
    field: Optional[str] = Query(None, description="Changed field, e.g. expenditure"),
    change_type: Optional[str] = Query(None, description="financial_change | completion_change | status_change | new_project | updated_project | missing_from_import | reappeared"),
    house: Optional[str] = Query(None, description="'Lok Sabha' / 'Rajya Sabha'"),
    state: Optional[str] = None,
    status: Optional[str] = Query(None, description="Project status at the time of the change"),
    project_id: Optional[int] = None,
    batch_id: Optional[int] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _user=_analyst,
):
    return ct.feed(
        db, date_from=date_from, date_to=date_to, field=field,
        change_type=change_type, house=house, state=state, status=status,
        project_id=project_id, batch_id=batch_id, limit=limit, offset=offset,
    )


@router.get("/projects/{project_id}")
def project_changes(
    project_id: int,
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    """Every recorded change for one project, newest first."""
    exists = db.query(models.Project.id).filter(models.Project.id == project_id).first()
    if not exists:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    events = ct.project_events(db, project_id, limit=limit)
    highlights = [e for e in events if e.get("impact")][:10]
    return {
        "project_id": project_id,
        "events": events,
        "count": len(events),
        "highlights": highlights,
        "has_history": bool(events),
        "limitations": [
            "Only imports recorded by this system are represented.",
            "Missing-from-import means the project was absent from a file; it is "
            "not a cancellation or deletion.",
        ],
    }


@router.get("/projects/{project_id}/history")
def project_history(
    project_id: int,
    limit: int = Query(500, ge=1, le=2000),
    db: Session = Depends(get_db),
):
    """Chronological snapshots + change events, each traced to its import."""
    exists = db.query(models.Project.id).filter(models.Project.id == project_id).first()
    if not exists:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    return ct.project_history(db, project_id, limit=limit)


@router.get("/summary")
def changes_summary(db: Session = Depends(get_db), _user=_analyst):
    return ct.summary(db)


@router.get("/imports/{batch_id}")
def import_changes(
    batch_id: int,
    limit: int = Query(200, ge=1, le=2000),
    db: Session = Depends(get_db),
    _user=_admin,
):
    payload = ct.batch_changes(db, batch_id, limit=limit)
    if not payload.get("exists"):
        raise HTTPException(status_code=404, detail="Import batch not found")
    return payload
