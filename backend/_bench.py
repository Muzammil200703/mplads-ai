"""Temporary read-only benchmark of the hot paths (no writes)."""
import os, sys, time, statistics
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sqlalchemy.orm import sessionmaker
from sqlalchemy import text
from database import engine
import models, metrics as metrics_svc, ai_audit_api

SessionLocal = sessionmaker(bind=engine)


def bench(label, fn, n=3):
    times = []
    for _ in range(n):
        db = SessionLocal()
        try:
            t0 = time.perf_counter()
            out = fn(db)
            times.append((time.perf_counter() - t0) * 1000)
        except Exception as e:
            print(f"{label:38s} ERROR {type(e).__name__}: {e}")
            return None
        finally:
            db.close()
    med = statistics.median(times)
    print(f"{label:38s} {med:8.1f} ms   (runs: {', '.join(f'{t:.0f}' for t in times)})")
    return out


def projects_page(db):
    # mirrors GET /projects default paging (count + page)
    total = db.query(models.Project).count()
    rows = (db.query(models.Project)
            .order_by(models.Project.id.desc()).offset(0).limit(50).all())
    return total, len(rows)


def projects_filtered(db):
    total = db.query(models.Project).filter(models.Project.house == "Lok Sabha").count()
    rows = (db.query(models.Project)
            .filter(models.Project.house == "Lok Sabha")
            .order_by(models.Project.id.desc()).offset(0).limit(50).all())
    return total, len(rows)


def projects_deep_page(db):
    total = db.query(models.Project).count()
    rows = (db.query(models.Project)
            .order_by(models.Project.id.desc()).offset(80000).limit(50).all())
    return total, len(rows)


def anomalies(db):
    return db.execute(text("""
        SELECT risk_level, COUNT(*) FROM risk_scores GROUP BY risk_level
    """)).fetchall()


def exp_by_house_fy(db):
    return db.execute(text("""
        SELECT house, COUNT(*), SUM(expenditure_amount)
        FROM expenditures GROUP BY house
    """)).fetchall()


print("=== PRAGMAS ===")
with engine.connect() as c:
    for p in ("journal_mode", "synchronous", "cache_size", "mmap_size",
              "temp_store", "page_size", "busy_timeout"):
        try:
            print(f"  {p:14s}", c.execute(text(f"PRAGMA {p}")).scalar())
        except Exception as e:
            print(f"  {p:14s} ERR {e}")

print("=== INDEXES ===")
with engine.connect() as c:
    for t in ("projects", "expenditures", "completed_works", "recommended_works",
              "risk_scores", "mp_summaries"):
        rows = c.execute(text(f"PRAGMA index_list({t})")).fetchall()
        names = [r[1] for r in rows]
        print(f"  {t:20s} {len(names):2d}: {', '.join(names)}")

print("=== TIMINGS ===")
bench("house_dashboard All", lambda db: metrics_svc.house_dashboard(db, house="All"))
bench("house_dashboard Lok Sabha", lambda db: metrics_svc.house_dashboard(db, house="Lok Sabha"))
bench("house_dashboard Rajya Sabha", lambda db: metrics_svc.house_dashboard(db, house="Rajya Sabha"))
bench("portfolio_overview", lambda db: metrics_svc.portfolio_overview(db))
bench("vendor_network(min_cluster=2,limit=5)", lambda db: ai_audit_api.vendor_network(min_cluster=2, limit=5, db=db), n=1)
bench("projects page (default)", projects_page)
bench("projects page (house filter)", projects_filtered)
bench("projects page (offset 80000)", projects_deep_page)
bench("anomalies group by level", anomalies)
bench("expenditure sums by house", exp_by_house_fy)
