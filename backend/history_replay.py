"""Historical replay over recorded import checkpoints.

A checkpoint is a *committed project import* that wrote snapshots. Replay reads
those rows and nothing else: no historical value is ever extrapolated from the
current table, and no snapshot is ever rewritten.

Deliberate distinctions
-----------------------
* A checkpoint represents a periodic CSV import, not a daily state. Every
  payload carries the import timestamp so the UI can say exactly that.
* ``NULL`` means "not recorded at that checkpoint" and is returned as null --
  it is never coerced to 0.
* Risk scores are NOT replayed: the model inputs of the version that produced
  them are not stored per checkpoint, so the API returns an explicit
  limitation instead of showing today's score.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy import func, text
from sqlalchemy.orm import Session

import models
from change_tracking import TRACKED_FIELDS, FIELD_LABELS

# A batch becomes replayable only once it committed successfully (the refresh
# thread may still be running, but the dataset itself is written).
REPLAY_STATUSES = ("success", "partial", "refreshing")

BASELINE_LIMITATION = (
    "Checkpoints start with the first project import recorded by this system; "
    "earlier CSV loads were never captured and are not reconstructed."
)
PERIODIC_LIMITATION = (
    "Each checkpoint is the dataset as submitted in a periodic CSV import - not "
    "the project's state on that calendar day."
)
RISK_LIMITATION = (
    "Historical risk scores are not reproduced: the model version and inputs in "
    "force at that import are not stored with the snapshot, so today's score is "
    "never shown as a historical one."
)


def _snapshot_counts(db: Session) -> Dict[int, int]:
    rows = db.query(
        models.ProjectSnapshot.batch_id, func.count(models.ProjectSnapshot.id)
    ).group_by(models.ProjectSnapshot.batch_id).all()
    return {int(bid): int(n) for bid, n in rows if bid is not None}


def replayable(db: Session) -> List[models.ImportBatch]:
    counts = _snapshot_counts(db)
    batches = (
        db.query(models.ImportBatch)
        .filter(
            models.ImportBatch.dataset_type == "projects",
            models.ImportBatch.committed_at.isnot(None),
            models.ImportBatch.rolled_back_at.is_(None),
            models.ImportBatch.status.in_(REPLAY_STATUSES),
        )
        .order_by(models.ImportBatch.committed_at.asc(), models.ImportBatch.id.asc())
        .all()
    )
    return [b for b in batches if counts.get(b.id)]


def _house_counts(db: Session, batch_id: int) -> Dict[str, int]:
    rows = db.execute(text("""
        SELECT COALESCE(house, 'House unattributed') AS h, COUNT(*)
        FROM project_snapshots WHERE batch_id = :b GROUP BY 1 ORDER BY 2 DESC
    """), {"b": batch_id}).all()
    return {str(h): int(n) for h, n in rows}


def _checkpoint_row(batch: models.ImportBatch, index: int, total: int,
                    project_count: int, houses: Dict[str, int]) -> Dict[str, Any]:
    return {
        "batch_id": batch.id,
        "label": f"Import #{batch.id} - {batch.committed_at}",
        "imported_at": batch.committed_at,
        "filename": batch.filename,
        "dataset_version": batch.dataset_version,
        "status": batch.status,
        "house_coverage": batch.house_coverage,
        "project_count": project_count,
        "houses": houses,
        "position": index + 1,
        "is_latest": index == total - 1,
    }


def list_checkpoints(db: Session) -> Dict[str, Any]:
    batches = replayable(db)
    counts = _snapshot_counts(db)
    items = [
        _checkpoint_row(b, i, len(batches), counts.get(b.id, 0), _house_counts(db, b.id))
        for i, b in enumerate(batches)
    ]
    return {
        "items": items,
        "total": len(items),
        "latest_batch_id": items[-1]["batch_id"] if items else None,
        "earliest_batch_id": items[0]["batch_id"] if items else None,
        "earliest_available": items[0]["imported_at"] if items else None,
        "latest_import": items[-1]["imported_at"] if items else None,
        "limitations": [BASELINE_LIMITATION, PERIODIC_LIMITATION],
    }


def checkpoint_detail(db: Session, batch_id: int) -> Optional[Dict[str, Any]]:
    batch = db.query(models.ImportBatch).filter(models.ImportBatch.id == batch_id).first()
    if batch is None:
        return None
    counts = _snapshot_counts(db)
    if not counts.get(batch_id):
        return {
            "batch_id": batch_id,
            "replayable": False,
            "reason": "This import has no project snapshots (it may not be a "
                      "project dataset, or it was not committed).",
            "filename": batch.filename,
            "status": batch.status,
        }
    batches = replayable(db)
    index = next((i for i, b in enumerate(batches) if b.id == batch_id), None)
    fy_rows = db.execute(text("""
        SELECT fy, COUNT(*) FROM project_snapshots
        WHERE batch_id = :b AND fy IS NOT NULL AND fy != ''
        GROUP BY fy ORDER BY fy
    """), {"b": batch_id}).all()
    return {
        "batch_id": batch.id,
        "replayable": True,
        "filename": batch.filename,
        "imported_at": batch.committed_at,
        "dataset_version": batch.dataset_version,
        "status": batch.status,
        "house_coverage": batch.house_coverage,
        "project_count": counts.get(batch_id, 0),
        "houses": _house_counts(db, batch_id),
        "financial_years": [{"fy": fy, "projects": int(n)} for fy, n in fy_rows],
        "position": (index + 1) if index is not None else None,
        "total_checkpoints": len(batches),
        "is_latest": bool(batches) and batches[-1].id == batch_id,
        "fields_captured": [
            {"field": f, "label": FIELD_LABELS.get(f, f)} for f in TRACKED_FIELDS
        ],
        "limitations": [PERIODIC_LIMITATION, RISK_LIMITATION],
    }


def _current_values(db: Session, project_id: int) -> Optional[Dict[str, Any]]:
    row = db.query(models.Project).filter(models.Project.id == project_id).first()
    if row is None:
        return None
    return {f: getattr(row, f, None) for f in TRACKED_FIELDS}


def project_at(db: Session, project_id: int, batch_id: Optional[int] = None) -> Dict[str, Any]:
    """A project exactly as one checkpoint recorded it, plus the current row."""
    if batch_id is None:
        latest = replayable(db)
        if not latest:
            return {"project_id": project_id, "available": False,
                    "reason": "No replayable checkpoints exist yet.",
                    "limitations": [BASELINE_LIMITATION]}
        batch_id = latest[-1].id

    snap = (
        db.query(models.ProjectSnapshot)
        .filter(models.ProjectSnapshot.batch_id == batch_id,
                models.ProjectSnapshot.project_id == project_id)
        .first()
    )
    current = _current_values(db, project_id)
    if snap is None:
        return {
            "project_id": project_id,
            "batch_id": batch_id,
            "available": False,
            "reason": "This project is not present in the selected checkpoint. "
                      "Absence from an import is a dataset gap, not a cancellation.",
            "current": current,
            "limitations": [PERIODIC_LIMITATION],
        }

    batch = db.query(models.ImportBatch).filter(models.ImportBatch.id == batch_id).first()
    historical = {f: getattr(snap, f, None) for f in TRACKED_FIELDS}
    differences = []
    for field in TRACKED_FIELDS:
        old, new = historical.get(field), (current or {}).get(field)
        if old is None and new is None:
            continue
        if old == new:
            continue
        entry = {
            "field": field,
            "label": FIELD_LABELS.get(field, field),
            "historical": old,
            "current": new,
            "historical_missing": old is None,
            "current_missing": new is None,
        }
        if isinstance(old, (int, float)) and isinstance(new, (int, float)):
            entry["delta"] = new - old
            entry["delta_percent"] = ((new - old) / old * 100.0) if old else None
        differences.append(entry)

    return {
        "project_id": project_id,
        "batch_id": batch_id,
        "available": True,
        "imported_at": batch.committed_at if batch else None,
        "filename": batch.filename if batch else None,
        "historical": historical,
        "current": current,
        "differences": differences,
        "rec_date_available": historical.get("rec_date") is not None,
        "completion_date_available": historical.get("completion_date") is not None,
        "limitations": [PERIODIC_LIMITATION],
    }


def dashboard(db: Session, batch_id: Optional[int] = None,
              house: Optional[str] = None) -> Dict[str, Any]:
    """Portfolio metrics computed ONLY from the checkpoint's snapshots."""
    if batch_id is None:
        batches = replayable(db)
        batch_id = batches[-1].id if batches else None
    if batch_id is None:
        return {"available": False, "reason": "No replayable checkpoints exist yet.",
                "limitations": [BASELINE_LIMITATION, RISK_LIMITATION]}

    batch = db.query(models.ImportBatch).filter(models.ImportBatch.id == batch_id).first()
    house_sql = " AND house = :house" if house in ("Lok Sabha", "Rajya Sabha") else ""
    params: Dict[str, Any] = {"b": batch_id}
    if house_sql:
        params["house"] = house

    totals = db.execute(text(f"""
        SELECT COUNT(*) AS projects,
               SUM(sanctioned_amount) AS sanctioned,
               SUM(expenditure) AS expenditure,
               AVG(completion_percentage) AS avg_completion,
               SUM(CASE WHEN expenditure IS NULL THEN 1 ELSE 0 END) AS exp_missing,
               SUM(CASE WHEN sanctioned_amount IS NULL THEN 1 ELSE 0 END) AS san_missing,
               SUM(CASE WHEN completion_percentage IS NULL THEN 1 ELSE 0 END) AS comp_missing,
               SUM(CASE WHEN completion_percentage >= 100 THEN 1 ELSE 0 END) AS completed
        FROM project_snapshots WHERE batch_id = :b {house_sql}
    """), params).one()

    status_rows = db.execute(text(f"""
        SELECT COALESCE(status, 'Not recorded') AS status, COUNT(*) AS n
        FROM project_snapshots WHERE batch_id = :b {house_sql}
        GROUP BY 1 ORDER BY 2 DESC
    """), params).all()

    state_rows = db.execute(text(f"""
        SELECT COALESCE(state, 'Not recorded') AS state, COUNT(*) AS n,
               SUM(sanctioned_amount) AS sanctioned, SUM(expenditure) AS expenditure
        FROM project_snapshots WHERE batch_id = :b {house_sql}
        GROUP BY 1 ORDER BY 2 DESC LIMIT 12
    """), params).all()

    projects = int(totals.projects or 0)
    return {
        "available": projects > 0,
        "batch_id": batch_id,
        "imported_at": batch.committed_at if batch else None,
        "filename": batch.filename if batch else None,
        "dataset_version": batch.dataset_version if batch else None,
        "house": house if house_sql else "All",
        "totals": {
            "projects": projects,
            "sanctioned_amount": float(totals.sanctioned) if totals.sanctioned is not None else None,
            "expenditure": float(totals.expenditure) if totals.expenditure is not None else None,
            "average_completion": round(float(totals.avg_completion), 2) if totals.avg_completion is not None else None,
            "completed_projects": int(totals.completed or 0),
            "records_without_expenditure": int(totals.exp_missing or 0),
            "records_without_sanctioned": int(totals.san_missing or 0),
            "records_without_completion": int(totals.comp_missing or 0),
        },
        "status_distribution": [{"status": s, "projects": int(n)} for s, n in status_rows],
        "state_distribution": [
            {
                "state": s,
                "projects": int(n),
                "sanctioned_amount": float(san) if san is not None else None,
                "expenditure": float(exp) if exp is not None else None,
            }
            for s, n, san, exp in state_rows
        ],
        "risk": {
            "available": False,
            "reason": RISK_LIMITATION,
        },
        "limitations": [PERIODIC_LIMITATION, RISK_LIMITATION],
    }


def compare(db: Session, from_batch: int, to_batch: int,
            house: Optional[str] = None, sample_limit: int = 50) -> Dict[str, Any]:
    """Exact differences between two checkpoints (never against live data)."""
    counts = _snapshot_counts(db)
    for bid in (from_batch, to_batch):
        if not counts.get(bid):
            return {"available": False, "reason": f"Import #{bid} has no snapshots."}

    house_sql = " AND house = :house" if house in ("Lok Sabha", "Rajya Sabha") else ""
    params: Dict[str, Any] = {"a": from_batch, "b": to_batch}
    if house_sql:
        params["house"] = house

    cols = ("project_id, fingerprint, sanctioned_amount, expenditure, "
            "completion_percentage, status, project_name")
    rows = db.execute(text(f"""
        SELECT
          COUNT(*) AS common,
          SUM(CASE WHEN a.fingerprint != b.fingerprint THEN 1 ELSE 0 END) AS changed,
          SUM(CASE WHEN COALESCE(a.sanctioned_amount,-1) != COALESCE(b.sanctioned_amount,-1) THEN 1 ELSE 0 END) AS sanctioned_changed,
          SUM(CASE WHEN COALESCE(a.expenditure,-1) != COALESCE(b.expenditure,-1) THEN 1 ELSE 0 END) AS expenditure_changed,
          SUM(CASE WHEN COALESCE(a.completion_percentage,-1) != COALESCE(b.completion_percentage,-1) THEN 1 ELSE 0 END) AS completion_changed,
          SUM(CASE WHEN COALESCE(a.status,'') != COALESCE(b.status,'') THEN 1 ELSE 0 END) AS status_changed
        FROM (SELECT {cols} FROM project_snapshots WHERE batch_id = :a {house_sql}) a
        JOIN (SELECT {cols} FROM project_snapshots WHERE batch_id = :b {house_sql}) b
          ON a.project_id = b.project_id
    """), params).one()

    added_rows = db.execute(text(f"""
        SELECT COUNT(*) FROM (SELECT project_id FROM project_snapshots WHERE batch_id = :b {house_sql}
        EXCEPT SELECT project_id FROM project_snapshots WHERE batch_id = :a {house_sql})
    """), params).scalar()

    missing_rows = db.execute(text(f"""
        SELECT COUNT(*) FROM (SELECT project_id FROM project_snapshots WHERE batch_id = :a {house_sql}
        EXCEPT SELECT project_id FROM project_snapshots WHERE batch_id = :b {house_sql})
    """), params).scalar()

    samples = db.execute(text(f"""
        SELECT b.project_id, b.project_name AS name,
               a.sanctioned_amount AS old_sanctioned, b.sanctioned_amount AS new_sanctioned,
               a.expenditure AS old_expenditure, b.expenditure AS new_expenditure,
               a.completion_percentage AS old_completion, b.completion_percentage AS new_completion,
               a.status AS old_status, b.status AS new_status
        FROM (SELECT {cols} FROM project_snapshots WHERE batch_id = :a {house_sql}) a
        JOIN (SELECT {cols} FROM project_snapshots WHERE batch_id = :b {house_sql}) b
          ON a.project_id = b.project_id
        WHERE a.fingerprint != b.fingerprint
        ORDER BY b.project_id
        LIMIT :lim
    """), {**params, "lim": sample_limit}).all()

    metrics_from = dashboard(db, from_batch, house)
    metrics_to = dashboard(db, to_batch, house)
    return {
        "available": True,
        "from": {"batch_id": from_batch, "imported_at": metrics_from.get("imported_at"),
                 "projects": metrics_from["totals"]["projects"]},
        "to": {"batch_id": to_batch, "imported_at": metrics_to.get("imported_at"),
               "projects": metrics_to["totals"]["projects"]},
        "house": house or "All",
        "counts": {
            "added": int(added_rows or 0),
            "missing": int(missing_rows or 0),
            "common": int(rows.common or 0),
            "changed": int(rows.changed or 0),
            "sanctioned_changed": int(rows.sanctioned_changed or 0),
            "expenditure_changed": int(rows.expenditure_changed or 0),
            "completion_changed": int(rows.completion_changed or 0),
            "status_changed": int(rows.status_changed or 0),
        },
        "metric_deltas": {
            key: (
                (metrics_to["totals"].get(key) - metrics_from["totals"].get(key))
                if isinstance(metrics_to["totals"].get(key), (int, float))
                and isinstance(metrics_from["totals"].get(key), (int, float))
                else None
            )
            for key in ("projects", "sanctioned_amount", "expenditure",
                        "average_completion", "completed_projects")
        },
        "samples": [
            {
                "project_id": pid,
                "project_name": name,
                "sanctioned": {"from": o_san, "to": n_san},
                "expenditure": {"from": o_exp, "to": n_exp},
                "completion": {"from": o_comp, "to": n_comp},
                "status": {"from": o_st, "to": n_st},
            }
            for pid, name, o_san, n_san, o_exp, n_exp, o_comp, n_comp, o_st, n_st in samples
        ],
        "sample_truncated": len(samples) >= sample_limit,
        "limitations": [PERIODIC_LIMITATION, RISK_LIMITATION],
    }
