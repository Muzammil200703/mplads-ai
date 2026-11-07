"""Tests for historical replay over genuine import checkpoints.

Runs the real import pipeline against a throwaway SQLite database, then reads
the recorded snapshots back through ``history_replay``.

    cd backend && PYTHONIOENCODING=utf-8 python test_history_replay.py
"""

from __future__ import annotations

import os
import tempfile

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import data_management as dm
import history_replay as hr
import models

PASSED = 0
FAILED = 0


def check(name, condition, detail=None):
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"PASS  {name}")
    else:
        FAILED += 1
        print(f"FAIL  {name}  {detail if detail is not None else ''}")


HEADER = ("id,project_name,state,constituency,sanctioned_amount,expenditure,"
          "completion_percentage,status,project_type,house")
CREATED = []


def write_csv(rows):
    fd, path = tempfile.mkstemp(suffix=".csv")
    CREATED.append(path)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
        fh.write(HEADER + "\n")
        for row in rows:
            fh.write(",".join(str(c) for c in row) + "\n")
    return path


def import_file(db, path, filename, user):
    batch = dm.create_batch_from_path(db, filename, path, os.path.getsize(path),
                                      user, dataset_type="projects")
    dm.preview_batch(db, batch)
    return dm.commit_batch(db, batch, user)


def main():
    tmpdb = tempfile.mktemp(suffix=".db")
    engine = create_engine(f"sqlite:///{tmpdb}", connect_args={"check_same_thread": False})
    models.Base.metadata.create_all(engine)
    TempSession = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    dm.SessionLocal = TempSession
    dm._spawn_refresh = lambda *a, **k: None

    db = TempSession()
    user = models.User(id=1, name="Test Admin", email="test@example.com",
                       role="admin", password_hash="x", is_active=True,
                       created_at="2026-01-01T00:00:00")
    db.add(user)
    db.commit()

    try:
        check("no checkpoints before any import", hr.list_checkpoints(db)["total"] == 0)
        empty = hr.dashboard(db)
        check("dashboard reports unavailability, not zeros",
              empty["available"] is False and "reason" in empty, empty)

        # ── Import A: two houses ────────────────────────────────────────
        a = import_file(db, write_csv([
            (1001, "Library building", "Bihar", "Patna Sahib", 1000000, 200000, 10, "Ongoing", "Education", "Lok Sabha"),
            (2001, "Road widening", "Telangana", "Secunderabad", 2000000, 500000, 20, "Ongoing", "Roads", "Rajya Sabha"),
        ]), "a.csv", user)["id"]
        listed = hr.list_checkpoints(db)
        check("successful import becomes a checkpoint", listed["total"] == 1, listed)
        check("earliest replay date is the first import",
              listed["earliest_available"] is not None and listed["earliest_batch_id"] == a)
        check("checkpoint records its project count",
              listed["items"][0]["project_count"] == 2, listed["items"])

        # ── Import B: 1001 changed, 2001 absent, 3001 new ──────────────
        b = import_file(db, write_csv([
            (1001, "Library building and reading room", "Bihar", "Patna Sahib", 1200000, 300000, 8, "Ongoing", "Education", "Lok Sabha"),
            (3001, "New drain", "Bihar", "Patna Sahib", 500000, 0, 0, "Sanctioned", "Sanitation", "Lok Sabha"),
        ]), "b.csv", user)["id"]
        listed = hr.list_checkpoints(db)
        check("second import adds a checkpoint in order",
              listed["total"] == 2 and listed["latest_batch_id"] == b
              and listed["items"][0]["batch_id"] == a)

        # ── Reconstruction ─────────────────────────────────────────────
        at_a = hr.project_at(db, 1001, a)
        check("reconstruction returns the stored historical values",
              at_a["available"] and at_a["historical"]["sanctioned_amount"] == 1000000
              and at_a["historical"]["expenditure"] == 200000
              and at_a["historical"]["completion_percentage"] == 10
              and at_a["historical"]["house"] == "Lok Sabha", at_a["historical"])
        check("reconstruction reports the current value separately",
              at_a["current"]["sanctioned_amount"] == 1200000)
        diffs = {d["field"]: d for d in at_a["differences"]}
        check("differences carry exact deltas",
              diffs["sanctioned_amount"]["delta"] == 200000
              and diffs["expenditure"]["delta"] == 100000
              and diffs["completion_percentage"]["delta"] == -2, diffs)
        check("description change is listed",
              "project_name" in diffs)

        later = hr.project_at(db, 3001, a)
        check("project added after an import is absent from the earlier checkpoint",
              later["available"] is False and "not present" in later["reason"])

        gone = hr.project_at(db, 2001, b)
        check("project missing from a later import is reported as missing, not deleted",
              gone["available"] is False and "gap" in gone["reason"])

        # ── Missing fields vs zero ─────────────────────────────────────
        check("unrecorded dates stay null rather than becoming zero",
              at_a["historical"]["rec_date"] is None and at_a["rec_date_available"] is False)

        # ── Dashboard replay ───────────────────────────────────────────
        dash_a = hr.dashboard(db, a)
        check("historical totals come from the checkpoint's snapshots",
              dash_a["totals"]["projects"] == 2
              and dash_a["totals"]["sanctioned_amount"] == 3000000
              and dash_a["totals"]["expenditure"] == 700000
              and dash_a["totals"]["average_completion"] == 15, dash_a["totals"])
        check("historical dashboard is not the current dataset",
              dash_a["totals"]["sanctioned_amount"] != hr.dashboard(db, b)["totals"]["sanctioned_amount"])
        check("status and state distributions are present",
              {r["status"] for r in dash_a["status_distribution"]} == {"Ongoing"}
              and {r["state"] for r in dash_a["state_distribution"]} == {"Bihar", "Telangana"})
        check("risk replay is declared unavailable", dash_a["risk"]["available"] is False)

        rs = hr.dashboard(db, a, "Rajya Sabha")
        ls = hr.dashboard(db, a, "Lok Sabha")
        check("house filter applies to historical metrics",
              rs["totals"]["projects"] == 1 and rs["totals"]["sanctioned_amount"] == 2000000
              and ls["totals"]["projects"] == 1 and ls["totals"]["sanctioned_amount"] == 1000000,
              (rs["totals"], ls["totals"]))

        # ── Comparison ─────────────────────────────────────────────────
        cmp = hr.compare(db, a, b)
        check("comparison counts additions", cmp["counts"]["added"] == 1, cmp["counts"])
        check("comparison counts missing records", cmp["counts"]["missing"] == 1, cmp["counts"])
        check("comparison counts changed records", cmp["counts"]["changed"] == 1, cmp["counts"])
        check("comparison counts field-level changes",
              cmp["counts"]["sanctioned_changed"] == 1
              and cmp["counts"]["expenditure_changed"] == 1
              and cmp["counts"]["completion_changed"] == 1, cmp["counts"])
        # Import B carries 1,200,000 + 500,000 sanctioned (1,700,000), so the
        # portfolio total falls even though one project grew.
        check("comparison reports aggregate metric differences",
              cmp["metric_deltas"]["projects"] == 0
              and cmp["metric_deltas"]["sanctioned_amount"] == -1300000
              and cmp["metric_deltas"]["expenditure"] == -400000
              and cmp["metric_deltas"]["average_completion"] == -11.0, cmp["metric_deltas"])
        check("comparison samples show exact before/after values",
              cmp["samples"] and cmp["samples"][0]["sanctioned"] == {"from": 1000000, "to": 1200000},
              cmp["samples"])
        check("comparison honours the house filter",
              hr.compare(db, a, b, "Rajya Sabha")["counts"]["added"] == 0
              and hr.compare(db, a, b, "Rajya Sabha")["counts"]["missing"] == 1)

        # ── Immutability ───────────────────────────────────────────────
        snap = db.query(models.ProjectSnapshot).filter_by(batch_id=a, project_id=1001).one()
        frozen = (snap.sanctioned_amount, snap.expenditure, snap.captured_at, snap.fingerprint)
        hr.project_at(db, 1001, a)
        hr.compare(db, a, b)
        db.expire_all()
        snap = db.query(models.ProjectSnapshot).filter_by(batch_id=a, project_id=1001).one()
        check("reading history never rewrites a snapshot",
              (snap.sanctioned_amount, snap.expenditure, snap.captured_at, snap.fingerprint) == frozen)
        check("snapshot keeps its source import id", snap.batch_id == a)

        # ── Uncommitted import is not replayable ───────────────────────
        pending_path = write_csv([
            (9001, "Pending work", "Bihar", "Patna Sahib", 1, 0, 0, "Sanctioned", "Roads", "Lok Sabha"),
        ])
        pending = dm.create_batch_from_path(db, "pending.csv", pending_path,
                                            os.path.getsize(pending_path), user,
                                            dataset_type="projects")
        dm.preview_batch(db, pending)
        check("previewed but uncommitted import is not a checkpoint",
              hr.list_checkpoints(db)["total"] == 2)
        detail = hr.checkpoint_detail(db, pending.id)
        check("checkpoint detail explains non-replayable imports",
              detail["replayable"] is False and "no project snapshots" in detail["reason"], detail)
        check("unknown import id yields no checkpoint", hr.checkpoint_detail(db, 999999) is None)

        # ── Latest-by-default behaviour ────────────────────────────────
        latest = hr.dashboard(db)
        check("dashboard defaults to the latest committed checkpoint",
              latest["batch_id"] == b, latest.get("batch_id"))
        default_project = hr.project_at(db, 1001)
        check("project replay defaults to the latest checkpoint",
              default_project["batch_id"] == b
              and default_project["historical"]["sanctioned_amount"] == 1200000)

        # ── Rolled-back import disappears from replay ──────────────────
        c = import_file(db, write_csv([
            (1001, "Library building and reading room", "Bihar", "Patna Sahib", 1500000, 400000, 30, "Ongoing", "Education", "Lok Sabha"),
        ]), "c.csv", user)["id"]
        check("third import is replayable", hr.list_checkpoints(db)["total"] == 3)
        # The refresh worker is stubbed in tests, so mark the batch finished the
        # way that worker would before exercising rollback.
        batch_c = db.get(models.ImportBatch, c)
        batch_c.status = "success"
        db.commit()
        dm.rollback_batch(db, batch_c, user)
        check("rolled-back import leaves replay", hr.list_checkpoints(db)["total"] == 2)
        check("rollback does not disturb earlier checkpoints",
              hr.dashboard(db, a)["totals"]["sanctioned_amount"] == 3000000)
    finally:
        db.close()
        for path in CREATED:
            try:
                os.unlink(path)
            except OSError:
                pass
        try:
            os.unlink(tmpdb)
        except OSError:
            pass

    print(f"\n{PASSED} passed, {FAILED} failed")
    raise SystemExit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
