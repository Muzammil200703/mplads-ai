"""
MPLADS AI — Project Outcome Prediction API (Feature 2)
======================================================

Thin router over prediction.py (the service holds all logic and honesty
rules). Analyst+ role gate matches the platform's other AI/analytical
surfaces; predictions never modify data.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import auth
import models
import prediction
from database import SessionLocal

router = APIRouter(prefix="/predict", tags=["AI Operations"])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("/projects/{project_id}")
def predict_project_outcome(
    project_id: int,
    fresh: int = 0,
    user: models.User = Depends(auth.require_role("analyst")),
    db: Session = Depends(get_db),
):
    """Outcome prediction for one project: completion probability, expected
    timeline/expenditure and delay likelihood, time-aware validation metrics
    attached. Analyst+ (the assistant surfaces the same payload)."""
    project = db.query(models.Project).filter(models.Project.id == project_id).first()
    if project is None:
        raise HTTPException(404, f"Project {project_id} not found")
    payload = prediction.predict_project(
        db, project_id, refreshed=bool(int(fresh or 0)))
    if payload is None:
        raise HTTPException(404, f"Project {project_id} not found")
    return payload
