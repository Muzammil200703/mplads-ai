"""Project change detection across dataset imports.

What this module does — and deliberately does not do
----------------------------------------------------
* It reads the before-state the import pipeline ALREADY records
  (``ImportChange.before_json``) plus the dataset snapshot the batch carried
  (``ProjectSnapshot``), and turns them into ``ProjectChangeEvent`` rows.
* A change event exists only when two recorded values differ. Nothing is
  estimated, and no change is described as suspicious: impact flags state the
  arithmetic ("expenditure rose 41.2%"), never a verdict.
* A project with no snapshot in a batch is reported as **missing from this
  import** — never as cancelled or deleted. The wording is enforced in the
  event type itself (``missing_from_import``).
* Matching/identity is decided by the importer's existing key logic (stable
  project id, else description+constituency+state). This module never merges
  two projects because their descriptions look alike; it only flags a changed
  description for review (``confidence="review"``).

Everything runs inside the import's own transaction: a failed or rolled-back
import leaves no events behind.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

import models

logger = logging.getLogger(__name__)

TRACKED_FIELDS: Tuple[str, ...] = (
    "project_name", "state", "district", "constituency", "house",
    "project_type", "status", "fy",
    "sanctioned_amount", "expenditure", "completion_percentage",
    "rec_date", "completion_date",
)

FIELD_LABELS: Dict[str, str] = {
    "project_name": "Description",
    "state": "State",
    "district": "District",
    "constituency": "Constituency",
    "house": "House",
    "project_type": "Category",
    "status": "Status",
    "fy": "Financial year",
    "sanctioned_amount": "Sanctioned amount",
    "expenditure": "Expenditure",
    "completion_percentage": "Completion",
    "rec_date": "Recommendation date",
    "completion_date": "Completion date",
}

MONEY_FIELDS = {"sanctioned_amount", "expenditure"}
PERCENT_FIELDS = {"completion_percentage"}

# A partial upload can legitimately omit most of the portfolio; cap the number
# of "missing" events one import may generate so one file cannot flood the feed.
MISSING_CAP = 2000

COMPLETED_STATUSES = {"completed", "complete", "finished", "closed"}
OPEN_STATUSES = {"ongoing", "in progress", "in-progress", "sanctioned", "started"}


# ─────────────────────────────────────────────────────────────────────
# Value helpers
# ─────────────────────────────────────────────────────────────────────

def _num(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def money_text(value: Any) -> str:
    amount = _num(value)
    if amount is None:
        return "—"
    if abs(amount) >= 1e7:
        return f"₹{amount / 1e7:,.2f} Cr"
    if abs(amount) >= 1e5:
        return f"₹{amount / 1e5:,.2f} L"
    return f"₹{amount:,.0f}"


def value_text(field: str, value: Any) -> Optional[str]:
    """Human display value; None when the field was not recorded."""
    if value is None or value == "":
        return None
    if field in MONEY_FIELDS:
        return money_text(value)
    if field in PERCENT_FIELDS:
        amount = _num(value)
        return f"{amount:,.1f}%" if amount is not None else str(value)
    return str(value)


def _same(field: str, old: Any, new: Any) -> bool:
    if new is None or new == "":
        return True  # the importer never blanks a stored value with a missing one
    if field in MONEY_FIELDS or field in PERCENT_FIELDS:
        old_n, new_n = _num(old), _num(new)
        if old_n is not None and new_n is not None:
            return abs(old_n - new_n) < 0.01
    if field.endswith("_date"):
        return str(old)[:10] == str(new)[:10]
    return old == new


def fingerprint(values: Dict[str, Any]) -> str:
    payload = "|".join(f"{f}={values.get(f)}" for f in TRACKED_FIELDS)
    return hashlib.sha1(payload.encode("utf-8", "replace")).hexdigest()


def snapshot_mapping(
    batch_id: Optional[int],
    project_id: Optional[int],
    values: Dict[str, Any],
    captured_at: Optional[str] = None,
    source: str = "csv_upload",
    dataset_type: str = "projects",
) -> Dict[str, Any]:
    """Row mapping for ``ProjectSnapshot`` — source values, never inferred."""
    return {
        "batch_id": batch_id,
        "project_id": project_id,
        "source": source,
        "dataset_type": dataset_type,
        "captured_at": captured_at or _now(),
        **{f: values.get(f) for f in TRACKED_FIELDS},
        "fingerprint": fingerprint(values),
    }


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ─────────────────────────────────────────────────────────────────────
# Impact explanations (arithmetic, not verdicts)
# ─────────────────────────────────────────────────────────────────────

def impact_for(field: str, old: Any, new: Any) -> Tuple[Optional[str], Optional[str]]:
    if field == "expenditure":
        old_n, new_n = _num(old), _num(new)
        if old_n is not None and new_n is not None and old_n != new_n:
            delta = new_n - old_n
            pct = (delta / old_n * 100.0) if old_n else None
            direction = "rose" if delta > 0 else "fell"
            pct_text = f"{pct:+.1f}%" if pct is not None else "change"
            detail = (
                f"Expenditure {direction} from {money_text(old_n)} to {money_text(new_n)} "
                f"({pct_text} of the previous figure)."
            )
            if delta > 0 and (pct is None or pct >= 25.0) and delta >= 100_000:
                return "expenditure_increase", detail
            return ("expenditure_increase" if delta > 0 else "expenditure_decrease"), detail
        return None, None

    if field == "sanctioned_amount":
        old_n, new_n = _num(old), _num(new)
        if old_n is not None and new_n is not None and old_n != new_n:
            delta = new_n - old_n
            pct = (delta / old_n * 100.0) if old_n else None
            detail = (
                f"Sanctioned amount changed from {money_text(old_n)} to {money_text(new_n)}"
                + (f" ({pct:+.1f}%)." if pct is not None else ".")
            )
            return "sanction_changed", detail
        return None, None

    if field == "completion_percentage":
        old_n, new_n = _num(old), _num(new)
        if old_n is not None and new_n is not None and abs(new_n - old_n) >= 0.01:
            if new_n < old_n:
                return "completion_decreased", (
                    f"Recorded completion fell from {old_n:,.1f}% to {new_n:,.1f}% "
                    f"({new_n - old_n:+.1f} points)."
                )
            return "completion_increased", (
                f"Recorded completion rose from {old_n:,.1f}% to {new_n:,.1f}% "
                f"({new_n - old_n:+.1f} points)."
            )
        return None, None

    if field == "status":
        old_s = str(old or "").strip()
        new_s = str(new or "").strip()
        if old_s and new_s and old_s.lower() != new_s.lower():
            reopened = old_s.lower() in COMPLETED_STATUSES and new_s.lower() in OPEN_STATUSES
            return (
                "status_reopened" if reopened else "status_changed",
                f"Status moved from '{old_s}' to '{new_s}'.",
            )
        return None, None

    if field == "project_name":
        return "description_changed", (
            "The project description changed; the record was matched on its stable "
            "project ID, so identity is unchanged — review the wording before "
            "concluding anything about scope."
        )

    return None, None


# ─────────────────────────────────────────────────────────────────────
# Event construction
# ─────────────────────────────────────────────────────────────────────

def _event(
    batch_id: int,
    project_id: int,
    change_type: str,
    changed_at: str,
    field: Optional[str] = None,
    old: Any = None,
    new: Any = None,
    house: Optional[str] = None,
    state: Optional[str] = None,
    project_status: Optional[str] = None,
    confidence: str = "high",
    source: str = "csv_upload",
) -> models.ProjectChangeEvent:
    impact, detail = impact_for(field, old, new) if field else (None, None)
    old_n, new_n = _num(old), _num(new)
    delta = (new_n - old_n) if (old_n is not None and new_n is not None) else None
    pct = (delta / old_n * 100.0) if (delta is not None and old_n) else None
    return models.ProjectChangeEvent(
        batch_id=batch_id,
        project_id=project_id,
        change_type=change_type,
        field=field,
        previous_value=value_text(field, old) if field else None,
        new_value=value_text(field, new) if field else None,
        previous_numeric=old_n,
        new_numeric=new_n,
        delta_numeric=delta,
        delta_percent=pct,
        changed_at=changed_at,
        house=house,
        state=state,
        project_status=project_status,
        source=source,
        confidence=confidence,
        impact=impact,
        impact_detail=detail,
    )


def _project_values(db: Session, project_id: int) -> Optional[Dict[str, Any]]:
    row = db.query(models.Project).filter(models.Project.id == project_id).first()
    if not row:
        return None
    return {f: getattr(row, f, None) for f in TRACKED_FIELDS}


def _chunks(seq: Sequence[Any], size: int = 400) -> Iterable[Sequence[Any]]:
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _latest_event_types(db: Session, project_ids: Sequence[int], change_type: str) -> set:
    """Projects whose MOST RECENT event is ``change_type``."""
    if not project_ids:
        return set()
    found: set = set()
    for chunk in _chunks(list(project_ids)):
        rows = db.execute(
            models.ProjectChangeEvent.__table__.select().with_only_columns(
                models.ProjectChangeEvent.project_id,
                models.ProjectChangeEvent.change_type,
                models.ProjectChangeEvent.id,
            ).where(models.ProjectChangeEvent.project_id.in_(chunk))
        ).all()
        latest: Dict[int, Tuple[int, str]] = {}
        for pid, ctype, eid in rows:
            if pid not in latest or eid > latest[pid][0]:
                latest[pid] = (eid, ctype)
        found.update(pid for pid, (_eid, ctype) in latest.items() if ctype == change_type)
    return found


def _previous_batch(db: Session, batch: models.ImportBatch) -> Optional[models.ImportBatch]:
    return (
        db.query(models.ImportBatch)
        .filter(
            models.ImportBatch.id < batch.id,
            models.ImportBatch.dataset_type == batch.dataset_type,
            models.ImportBatch.committed_at.isnot(None),
            models.ImportBatch.rolled_back_at.is_(None),
            models.ImportBatch.status.in_(("success", "partial", "refreshing")),
        )
        .order_by(models.ImportBatch.id.desc())
        .first()
    )


def _snapshot_ids(db: Session, batch_id: int) -> List[int]:
    rows = db.execute(
        models.ProjectSnapshot.__table__.select()
        .with_only_columns(models.ProjectSnapshot.project_id)
        .where(models.ProjectSnapshot.batch_id == batch_id)
    ).all()
    return [int(r[0]) for r in rows if r[0] is not None]


def record_changes_for_batch(
    db: Session,
    batch: models.ImportBatch,
    spec: Dict[str, Any],
    preview: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Turn this import's recorded before/after state into change events.

    Must be called inside the import transaction, after the dataset has been
    applied and before commit.
    """
    if spec.get("table") != "projects":
        return {
            "tracked": False,
            "reason": "Change detection covers the project dataset; this import "
                      "updates a linked work/expenditure dataset.",
        }

    batch_id = batch.id
    # Idempotent re-run: clear this batch's EVENTS only. Snapshots were written
    # by the scan that just ran and must survive — they are how "present in this
    # import" is known, and therefore how "missing" is computed.
    db.query(models.ProjectChangeEvent).filter(
        models.ProjectChangeEvent.batch_id == batch_id).delete()
    db.flush()
    changed_at = batch.committed_at or _now()
    source = "csv_upload"

    changes = db.query(models.ImportChange).filter(
        models.ImportChange.batch_id == batch_id,
        models.ImportChange.table_name == "projects",
    ).all()
    updates = [c for c in changes if c.change_type == "update" and c.record_pk]
    inserts = [c for c in changes if c.change_type == "insert" and c.record_pk]

    counts = {"new": 0, "updated": 0, "missing": 0, "reappeared": 0, "field_events": 0}

    # Which projects does THIS dataset carry? Snapshots were written from the
    # file itself, so this is the authoritative "present in this import" set.
    current_ids = set(_snapshot_ids(db, batch_id))

    # ── Reappeared: present now, while the last thing we recorded was absence.
    # A returning project is usually UNCHANGED (its row still exists), so it
    # produces no ImportChange row — absence tracking has to drive this.
    recent_missing = db.query(models.ProjectChangeEvent.project_id).filter(
        models.ProjectChangeEvent.change_type == "missing_from_import"
    ).order_by(models.ProjectChangeEvent.id.desc()).limit(5000).all()
    candidates = {int(pid) for (pid,) in recent_missing if pid is not None} & current_ids
    reappeared_ids = _latest_event_types(db, sorted(candidates), "missing_from_import") & current_ids

    events: List[models.ProjectChangeEvent] = []

    for change in inserts:
        values = _project_values(db, change.record_pk) or {}
        ctype = "reappeared" if change.record_pk in reappeared_ids else "new_project"
        counts["reappeared" if ctype == "reappeared" else "new"] += 1
        events.append(_event(
            batch_id, change.record_pk, ctype, changed_at,
            house=values.get("house"), state=values.get("state"),
            project_status=values.get("status"), source=source,
            field=None,
        ))
        events[-1].impact_detail = (
            "This project was absent from a previous import and is present again "
            "in this one."
            if ctype == "reappeared" else
            "First appearance of this project ID in an imported dataset."
        )

    # Reappeared projects that were merely unchanged in this file (no insert
    # and no field update) still deserve one event: their absence ended.
    inserted_ids = {c.record_pk for c in inserts}
    returning = sorted(reappeared_ids - inserted_ids)
    if returning:
        rows = []
        for chunk in _chunks(returning):
            rows.extend(db.execute(
                models.ProjectSnapshot.__table__.select()
                .where(models.ProjectSnapshot.batch_id == batch_id)
                .where(models.ProjectSnapshot.project_id.in_(chunk))
            ).all())
        for row in rows:
            m = row._mapping
            events.append(_event(
                batch_id, int(m["project_id"]), "reappeared", changed_at,
                house=m.get("house"), state=m.get("state"),
                project_status=m.get("status"), source=source,
            ))
            events[-1].impact_detail = (
                "This project was missing from an earlier import and is present in "
                "this one again."
            )
            counts["reappeared"] += 1

    # ── Field-level changes for updated rows.
    for change in updates:
        try:
            before = json.loads(change.before_json or "{}")
        except (ValueError, TypeError):
            before = {}
        after = _project_values(db, change.record_pk)
        if not after:
            continue
        counts["updated"] += 1
        for field, old in before.items():
            if field not in TRACKED_FIELDS:
                continue
            new = after.get(field)
            if _same(field, old, new):
                continue
            change_type = {
                "sanctioned_amount": "financial_change",
                "expenditure": "financial_change",
                "completion_percentage": "completion_change",
                "status": "status_change",
            }.get(field, "updated_project")
            events.append(_event(
                batch_id, change.record_pk, change_type, changed_at,
                field=field, old=old, new=new,
                house=after.get("house"), state=after.get("state"),
                project_status=after.get("status"), source=source,
                confidence="review" if field == "project_name" else "high",
            ))
            counts["field_events"] += 1

    # ── Missing from this import (never "deleted").
    previous = _previous_batch(db, batch)
    if current_ids and previous is not None:
        previous_ids = set(_snapshot_ids(db, previous.id))
        missing = sorted(previous_ids - current_ids)
        if missing:
            already_missing = _latest_event_types(db, missing, "missing_from_import")
            missing = [pid for pid in missing if pid not in already_missing][:MISSING_CAP]
            if missing:
                rows = []
                for chunk in _chunks(missing):
                    rows.extend(db.execute(
                        models.ProjectSnapshot.__table__.select()
                        .where(models.ProjectSnapshot.batch_id == previous.id)
                        .where(models.ProjectSnapshot.project_id.in_(chunk))
                    ).all())
                meta = {}
                for row in rows:
                    m = row._mapping
                    meta.setdefault(int(m["project_id"]), m)
                for pid in missing:
                    m = meta.get(pid) or {}
                    ev = _event(
                        batch_id, pid, "missing_from_import", changed_at,
                        house=m.get("house"), state=m.get("state"),
                        project_status=m.get("status"), source=source,
                    )
                    ev.impact = "missing_from_import"
                    ev.impact_detail = (
                        f"This project appeared in import batch #{previous.id} "
                        f"({previous.filename}) but is not present in this file. "
                        "It is missing from the import — not recorded as cancelled or deleted."
                    )
                    events.append(ev)
                    counts["missing"] += 1

    if events:
        db.add_all(events)
        db.flush()

    summary = {
        "tracked": True,
        "batch_id": batch_id,
        "events": len(events),
        "missing_capped": counts["missing"] >= MISSING_CAP,
        "previous_batch_id": previous.id if previous is not None else None,
        "current_projects": len(current_ids),
        **counts,
    }
    return summary


def drop_batch_history(db: Session, batch_id: int) -> None:
    """Remove this batch's snapshots + events (rollback / idempotent re-run)."""
    db.query(models.ProjectChangeEvent).filter(
        models.ProjectChangeEvent.batch_id == batch_id).delete()
    db.query(models.ProjectSnapshot).filter(
        models.ProjectSnapshot.batch_id == batch_id).delete()


# ─────────────────────────────────────────────────────────────────────
# Read-side helpers (used by change_api and the assistant)
# ─────────────────────────────────────────────────────────────────────

def event_dict(event: models.ProjectChangeEvent) -> Dict[str, Any]:
    return {
        "id": event.id,
        "batch_id": event.batch_id,
        "project_id": event.project_id,
        "change_type": event.change_type,
        "field": event.field,
        "field_label": FIELD_LABELS.get(event.field or "", event.field),
        "previous_value": event.previous_value,
        "new_value": event.new_value,
        "previous_numeric": event.previous_numeric,
        "new_numeric": event.new_numeric,
        "delta_numeric": event.delta_numeric,
        "delta_percent": event.delta_percent,
        "changed_at": event.changed_at,
        "house": event.house,
        "state": event.state,
        "project_status": event.project_status,
        "source": event.source,
        "confidence": event.confidence,
        "impact": event.impact,
        "impact_detail": event.impact_detail,
    }


def project_events(db: Session, project_id: int, limit: int = 200) -> List[Dict[str, Any]]:
    rows = (
        db.query(models.ProjectChangeEvent)
        .filter(models.ProjectChangeEvent.project_id == project_id)
        .order_by(models.ProjectChangeEvent.changed_at.desc(),
                  models.ProjectChangeEvent.id.desc())
        .limit(limit)
        .all()
    )
    return [event_dict(e) for e in rows]


def project_history(db: Session, project_id: int, limit: int = 500) -> Dict[str, Any]:
    snapshots = (
        db.query(models.ProjectSnapshot)
        .filter(models.ProjectSnapshot.project_id == project_id)
        .order_by(models.ProjectSnapshot.captured_at.desc(),
                  models.ProjectSnapshot.id.desc())
        .limit(limit)
        .all()
    )
    events = project_events(db, project_id, limit=limit)
    batch_ids = sorted({s.batch_id for s in snapshots} | {e["batch_id"] for e in events})
    batches = {}
    if batch_ids:
        for b in db.query(models.ImportBatch).filter(models.ImportBatch.id.in_(batch_ids)).all():
            batches[b.id] = {
                "id": b.id,
                "filename": b.filename,
                "dataset_type": b.dataset_type,
                "house_coverage": b.house_coverage,
                "committed_at": b.committed_at,
                "status": b.status,
                "dataset_version": b.dataset_version,
            }
    return {
        "project_id": project_id,
        "snapshots": [
            {
                "batch_id": s.batch_id,
                "captured_at": s.captured_at,
                "source": s.source,
                **{f: getattr(s, f, None) for f in TRACKED_FIELDS},
            }
            for s in snapshots
        ],
        "events": events,
        "batches": batches,
        "has_history": bool(snapshots or events),
        "limitations": [
            "History begins with the first import recorded by this system; earlier "
            "CSV loads were not captured.",
            "Absence from an import is a dataset gap, never proof that a project "
            "was cancelled.",
        ],
    }


def feed(
    db: Session,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    field: Optional[str] = None,
    change_type: Optional[str] = None,
    house: Optional[str] = None,
    state: Optional[str] = None,
    status: Optional[str] = None,
    project_id: Optional[int] = None,
    batch_id: Optional[int] = None,
    limit: int = 50,
    offset: int = 0,
) -> Dict[str, Any]:
    query = db.query(models.ProjectChangeEvent)
    if date_from:
        query = query.filter(models.ProjectChangeEvent.changed_at >= date_from)
    if date_to:
        query = query.filter(models.ProjectChangeEvent.changed_at <= date_to + "T23:59:59")
    if field:
        query = query.filter(models.ProjectChangeEvent.field == field)
    if change_type:
        query = query.filter(models.ProjectChangeEvent.change_type == change_type)
    if house in ("Lok Sabha", "Rajya Sabha"):
        query = query.filter(models.ProjectChangeEvent.house == house)
    if state:
        query = query.filter(models.ProjectChangeEvent.state == state)
    if status:
        query = query.filter(models.ProjectChangeEvent.project_status == status)
    if project_id:
        query = query.filter(models.ProjectChangeEvent.project_id == project_id)
    if batch_id:
        query = query.filter(models.ProjectChangeEvent.batch_id == batch_id)

    total = query.count()
    rows = (
        query.order_by(models.ProjectChangeEvent.changed_at.desc(),
                       models.ProjectChangeEvent.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    project_ids = sorted({e.project_id for e in rows})
    names = {}
    if project_ids:
        for pid, name in db.query(models.Project.id, models.Project.project_name).filter(
            models.Project.id.in_(project_ids)
        ).all():
            names[pid] = name
    items = []
    for e in rows:
        payload = event_dict(e)
        payload["project_name"] = names.get(e.project_id)
        items.append(payload)
    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": items,
        "filters": {
            "date_from": date_from, "date_to": date_to, "field": field,
            "change_type": change_type, "house": house, "state": state,
            "status": status, "project_id": project_id, "batch_id": batch_id,
        },
    }


def summary(db: Session) -> Dict[str, Any]:
    from sqlalchemy import func

    by_type = {
        str(t): int(n) for t, n in db.query(
            models.ProjectChangeEvent.change_type,
            func.count(models.ProjectChangeEvent.id),
        ).group_by(models.ProjectChangeEvent.change_type).all()
    }
    by_field = {
        str(f): int(n) for f, n in db.query(
            models.ProjectChangeEvent.field,
            func.count(models.ProjectChangeEvent.id),
        ).filter(models.ProjectChangeEvent.field.isnot(None))
        .group_by(models.ProjectChangeEvent.field).all()
    }
    by_impact = {
        str(i): int(n) for i, n in db.query(
            models.ProjectChangeEvent.impact,
            func.count(models.ProjectChangeEvent.id),
        ).filter(models.ProjectChangeEvent.impact.isnot(None))
        .group_by(models.ProjectChangeEvent.impact).all()
    }
    # One row per committed project import and its event count.
    joins = db.query(
        models.ImportBatch.id,
        models.ImportBatch.filename,
        models.ImportBatch.committed_at,
        func.count(models.ProjectChangeEvent.id),
    ).outerjoin(
        models.ProjectChangeEvent,
        models.ProjectChangeEvent.batch_id == models.ImportBatch.id,
    ).filter(
        models.ImportBatch.dataset_type == "projects",
        models.ImportBatch.committed_at.isnot(None),
        models.ImportBatch.rolled_back_at.is_(None),
    ).group_by(
        models.ImportBatch.id, models.ImportBatch.filename, models.ImportBatch.committed_at,
    ).order_by(models.ImportBatch.id.desc()).limit(10).all()
    recent_batches = [
        {"batch_id": bid, "filename": fname, "committed_at": at, "events": int(n or 0)}
        for bid, fname, at, n in joins
    ]
    total_events = db.query(func.count(models.ProjectChangeEvent.id)).scalar() or 0
    return {
        "total_events": int(total_events),
        "by_change_type": by_type,
        "by_field": by_field,
        "by_impact": by_impact,
        "recent_batches": recent_batches,
        "field_labels": FIELD_LABELS,
        "limitations": [
            "Counts cover imports recorded by this system only.",
            "A missing project is a dataset absence, not a cancellation.",
        ],
    }


def batch_changes(db: Session, batch_id: int, limit: int = 200) -> Dict[str, Any]:
    batch = db.query(models.ImportBatch).filter(models.ImportBatch.id == batch_id).first()
    if batch is None:
        return {"batch_id": batch_id, "exists": False, "items": [], "counts": {}}
    rows = (
        db.query(models.ProjectChangeEvent)
        .filter(models.ProjectChangeEvent.batch_id == batch_id)
        .order_by(models.ProjectChangeEvent.id.asc())
        .limit(limit)
        .all()
    )
    counts: Dict[str, int] = {}
    for e in rows:
        counts[e.change_type] = counts.get(e.change_type, 0) + 1
    project_ids = sorted({e.project_id for e in rows})
    names = {}
    if project_ids:
        for pid, name in db.query(models.Project.id, models.Project.project_name).filter(
            models.Project.id.in_(project_ids)
        ).all():
            names[pid] = name
    items = []
    for e in rows:
        payload = event_dict(e)
        payload["project_name"] = names.get(e.project_id)
        items.append(payload)
    return {
        "batch_id": batch_id,
        "exists": True,
        "filename": batch.filename,
        "committed_at": batch.committed_at,
        "status": batch.status,
        "counts": counts,
        "items": items,
        "truncated": len(rows) >= limit,
    }
