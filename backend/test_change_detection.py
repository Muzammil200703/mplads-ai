"""End-to-end tests for project change detection.

Runs the REAL import pipeline (data_management.preview_batch → commit_batch)
against a throwaway SQLite database, so snapshots and change events are
produced exactly the way production produces them.

    cd backend && PYTHONIOENCODING=utf-8 python test_change_detection.py
"""

from __future__ import annotations

import os
import tempfile

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import change_tracking as ct
import data_management as dm
import models

PASSED = 0
FAILED = 0


def check(name: str, condition: bool, detail=None) -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"PASS  {name}")
    else:
        FAILED += 1
        print(f"FAIL  {name}  {detail if detail is not None else ''}")


HEADER = ("id,project_name,state,constituency,sanctioned_amount,expenditure,"
          "completion_percentage,status,project_type,house")


CREATED: list = []


def write_csv(rows) -> str:
    fd, path = tempfile.mkstemp(suffix=".csv")
    CREATED.append(path)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
        fh.write(HEADER + "\n")
        for row in rows:
            fh.write(",".join(str(c) for c in row) + "\n")
    return path


def import_file(db, path, filename, user):
    size = os.path.getsize(path)
    batch = dm.create_batch_from_path(db, filename, path, size, user, dataset_type="projects")
    dm.preview_batch(db, batch)
    return dm.commit_batch(db, batch, user)


def main() -> None:
    tmpdb = tempfile.mktemp(suffix=".db")
    engine = create_engine(f"sqlite:///{tmpdb}", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    TempSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    # Isolate the pipeline: temp sessions, no background refresh thread.
    dm.SessionLocal = TempSession
    dm._spawn_refresh = lambda *a, **k: None

    db = TempSession()
    user = models.User(id=1, name="Test Admin", email="test@example.com",
                       role="admin", password_hash="x", is_active=True,
                       created_at="2026-01-01T00:00:00")
    db.add(user)
    db.commit()

    try:
        # ── 1. First import: two houses ─────────────────────────────────
        path_a = write_csv([
            (1001, "Library building", "Bihar", "Patna Sahib", 1000000, 200000, 10, "Ongoing", "Education", "Lok Sabha"),
            (2001, "Road widening", "Telangana", "Secunderabad", 2000000, 500000, 20, "Ongoing", "Roads", "Rajya Sabha"),
        ])
        payload_a = import_file(db, path_a, "import_a.csv", user)
        batch_a = db.query(models.ImportBatch).get(payload_a["id"])
        events_a = db.query(models.ProjectChangeEvent).filter_by(batch_id=batch_a.id).all()
        check("first import records new projects",
              sorted(e.change_type for e in events_a) == ["new_project", "new_project"],
              [e.change_type for e in events_a])
        check("first import snapshots both houses",
              {s.house for s in db.query(models.ProjectSnapshot).filter_by(batch_id=batch_a.id)} == {"Lok Sabha", "Rajya Sabha"})
        check("batch details carry change summary",
              "changes" in (batch_a.details_json or ""), batch_a.details_json[:120])

        # ── 2. Second import: financial, completion, description, status ─
        path_b = write_csv([
            (1001, "Library building and reading room", "Bihar", "Patna Sahib", 1200000, 300000, 8, "Ongoing", "Education", "Lok Sabha"),
            (2001, "Road widening", "Telangana", "Secunderabad", 2000000, 500000, 65, "Completed", "Roads", "Rajya Sabha"),
            (3001, "New drain", "Bihar", "Patna Sahib", 500000, 0, 0, "Sanctioned", "Sanitation", "Lok Sabha"),
        ])
        payload_b = import_file(db, path_b, "import_b.csv", user)
        batch_b = db.query(models.ImportBatch).get(payload_b["id"])
        events_b = db.query(models.ProjectChangeEvent).filter_by(batch_id=batch_b.id).all()
        by_field = {}
        for e in events_b:
            by_field.setdefault(e.field, []).append(e)

        check("sanctioned amount change recorded",
              any(e.change_type == "financial_change" and e.field == "sanctioned_amount" for e in events_b),
              [e.field for e in events_b])
        check("expenditure change recorded with delta",
              any(e.field == "expenditure" and e.delta_numeric == 100000 for e in events_b))
        check("completion decrease recorded",
              any(e.field == "completion_percentage" and e.new_numeric == 8 for e in events_b))
        check("completion increase recorded",
              any(e.field == "completion_percentage" and e.new_numeric == 65 for e in events_b))
        check("status change recorded",
              any(e.change_type == "status_change" and e.new_value == "Completed" for e in events_b))
        check("description change flagged for review",
              any(e.field == "project_name" and e.confidence == "review" for e in events_b))
        check("new project in second import",
              any(e.change_type == "new_project" and e.project_id == 3001 for e in events_b))
        check("impact explanation uses recorded values",
              any(e.impact == "expenditure_increase" and "₹3.00 L" in (e.impact_detail or "") for e in events_b),
              [e.impact_detail for e in events_b])

        history = ct.project_history(db, 1001)
        check("project history lists snapshots and events",
              len(history["snapshots"]) == 2 and len(history["events"]) >= 3)
        check("history is traceable to a batch",
              all(s["batch_id"] for s in history["snapshots"]) and bool(history["batches"]))

        # ── 3. Third import: missing + reappeared ──────────────────────
        path_c = write_csv([
            (1001, "Library building and reading room", "Bihar", "Patna Sahib", 1200000, 300000, 8, "Ongoing", "Education", "Lok Sabha"),
            (4001, "Reappearing school", "Bihar", "Patna Sahib", 700000, 0, 0, "Sanctioned", "Education", "Lok Sabha"),
        ])
        # 4001 first appears, then is dropped, then returns.
        payload_c1 = import_file(db, path_c, "import_c1.csv", user)
        path_c2 = write_csv([
            (1001, "Library building and reading room", "Bihar", "Patna Sahib", 1200000, 300000, 8, "Ongoing", "Education", "Lok Sabha"),
        ])
        payload_c2 = import_file(db, path_c2, "import_c2.csv", user)
        missing = db.query(models.ProjectChangeEvent).filter(
            models.ProjectChangeEvent.change_type == "missing_from_import",
            models.ProjectChangeEvent.batch_id.in_([payload_c1["id"], payload_c2["id"]]),
        ).all()
        # 2001 + 3001 are absent from c1; 4001 from c2. Each is reported once.
        check("absent projects reported as missing from import",
              {e.project_id for e in missing} == {2001, 3001, 4001},
              [(e.project_id, e.change_type) for e in missing])
        check("a project reported missing once is not repeated every import",
              len(missing) == 3, [(e.batch_id, e.project_id) for e in missing])
        check("missing wording never claims cancellation",
              all("not recorded as cancelled" in (e.impact_detail or "") for e in missing))

        path_c3 = write_csv([
            (1001, "Library building and reading room", "Bihar", "Patna Sahib", 1200000, 300000, 8, "Ongoing", "Education", "Lok Sabha"),
            (4001, "Reappearing school", "Bihar", "Patna Sahib", 700000, 0, 0, "Sanctioned", "Education", "Lok Sabha"),
            (2001, "Road widening", "Telangana", "Secunderabad", 2000000, 500000, 65, "Completed", "Roads", "Rajya Sabha"),
        ])
        payload_c3 = import_file(db, path_c3, "import_c3.csv", user)
        batch_c3 = db.query(models.ImportBatch).get(payload_c3["id"])
        reappeared = db.query(models.ProjectChangeEvent).filter_by(
            batch_id=batch_c3.id, change_type="reappeared").all()
        check("returning project marked reappeared",
              {e.project_id for e in reappeared} == {2001, 4001},
              [(e.project_id, e.change_type) for e in reappeared])

        # ── 4. Duplicate import: recorded, but no change events ────────
        before_events = db.query(models.ProjectChangeEvent).count()
        before_snapshots = db.query(models.ProjectSnapshot).count()
        path_c4 = write_csv([
            (1001, "Library building and reading room", "Bihar", "Patna Sahib", 1200000, 300000, 8, "Ongoing", "Education", "Lok Sabha"),
            (4001, "Reappearing school", "Bihar", "Patna Sahib", 700000, 0, 0, "Sanctioned", "Education", "Lok Sabha"),
            (2001, "Road widening", "Telangana", "Secunderabad", 2000000, 500000, 65, "Completed", "Roads", "Rajya Sabha"),
        ])
        duplicate_payload = import_file(db, path_c4, "import_c4_duplicate.csv", user)
        check("duplicate import records no change events",
              db.query(models.ProjectChangeEvent).count() == before_events)
        check("duplicate import still snapshots the dataset (for later missing checks)",
              db.query(models.ProjectSnapshot).count() > before_snapshots,
              (duplicate_payload.get("details") or {}).get("changes"))

        # ── 5. Failed import leaves no events ──────────────────────────
        path_d = write_csv([
            (1001, "Library building and reading room", "Bihar", "Patna Sahib", 900000, 300000, 8, "Ongoing", "Education", "Lok Sabha"),
        ])
        before_events = db.query(models.ProjectChangeEvent).count()
        before_snapshots = db.query(models.ProjectSnapshot).count()
        original = ct.record_changes_for_batch

        def boom(*args, **kwargs):
            raise RuntimeError("simulated failure after applying rows")

        ct.record_changes_for_batch = boom
        failed_cleanly = False
        try:
            import_file(db, path_d, "import_d_fail.csv", user)
        except Exception:
            failed_cleanly = True
        finally:
            ct.record_changes_for_batch = original

        check("failed import raises", failed_cleanly)
        check("failed import leaves no events",
              db.query(models.ProjectChangeEvent).count() == before_events)
        check("failed import leaves no snapshots",
              db.query(models.ProjectSnapshot).count() == before_snapshots)
        check("failed import did not change the project",
              db.query(models.Project).filter_by(id=1001).first().sanctioned_amount == 1200000)

        # ── 6. Feed filters ────────────────────────────────────────────
        feed = ct.feed(db, change_type="financial_change", house="Lok Sabha")
        check("feed filters by type and house",
              feed["total"] > 0 and all(i["change_type"] == "financial_change" and i["house"] == "Lok Sabha"
                                       for i in feed["items"]))
        summary = ct.summary(db)
        check("summary counts events by type",
              summary["total_events"] > 0 and "new_project" in summary["by_change_type"])
        batch_view = ct.batch_changes(db, batch_b.id)
        check("batch view lists that batch's changes",
              batch_view["exists"] and len(batch_view["items"]) == len(events_b))
    finally:
        db.close()
        try:
            os.remove(tmpdb)
        except OSError:
            pass
        for path in CREATED:
            try:
                os.remove(path)
            except OSError:
                pass

    print(f"\n{PASSED} passed, {FAILED} failed")
    raise SystemExit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
