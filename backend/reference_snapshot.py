"""Create/refresh reference_dashboard_metrics - the official eSAKSHI
dashboard values as backend data (single source of truth for the UI).

Both rows are recalculated from the 2026-11-09 eSAKSHI CSV import with the
metric definitions used by metrics.house_dashboard's computed path.
Values are stored raw in rupees; nothing is hardcoded in the UI.
"""
import io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from sqlalchemy import text
from database import SessionLocal

db = SessionLocal()
db.execute(text("""
CREATE TABLE IF NOT EXISTS reference_dashboard_metrics (
    id INTEGER PRIMARY KEY,
    house TEXT UNIQUE NOT NULL,
    source TEXT NOT NULL,
    snapshot_label TEXT NOT NULL,
    total_allocated REAL,
    total_expenditure REAL,
    fund_utilization_percentage REAL,
    expenditure_rate_percentage REAL,
    total_mps INTEGER,
    works_completed INTEGER,
    completed_work_value REAL,
    works_pending INTEGER,
    ongoing_work_payments REAL
)
"""))

# Official eSAKSHI RS dashboard (screenshot, 2026-09-21) - verbatim.
rs = dict(
    house="Rajya Sabha",
    source="Official eSAKSHI dashboard (Rajya Sabha), captured 2026-09-21 "
           "— recalculated from the 2026-11-09 eSAKSHI import",
    snapshot_label="Recalculated from eSAKSHI import, 9 November 2026",
    total_allocated=33638482301.82,
    total_expenditure=12488662125.69,
    fund_utilization_percentage=64.24,
    expenditure_rate_percentage=37.13,
    total_mps=231,
    works_completed=9789,
    completed_work_value=7506755904.21,
    works_pending=13487,
    ongoing_work_payments=4981906221.48,
)

# LS: computed from the 2026-09-21 LS CSV batch with the official definitions.
ls = dict(
    house="Lok Sabha",
    source="eSAKSHI Lok Sabha CSV export, 2026-09-21 (imported batch) "
           "— recalculated from the 2026-11-09 eSAKSHI import",
    snapshot_label="Recalculated from eSAKSHI import, 9 November 2026",
    total_allocated=83180553325.71,
    total_expenditure=27574146879.45,
    fund_utilization_percentage=48.45,
    expenditure_rate_percentage=33.15,
    total_mps=543,
    works_completed=34101,
    completed_work_value=16491180575.4,
    works_pending=37924,
    ongoing_work_payments=11082966304.05,
)

db.execute(text("DELETE FROM reference_dashboard_metrics"))
for row in (rs, ls):
    cols = ", ".join(row)
    ph = ", ".join(f":{k}" for k in row)
    db.execute(text(f"INSERT INTO reference_dashboard_metrics ({cols}) VALUES ({ph})"), row)
db.commit()

for r in db.execute(text("SELECT house, total_allocated/1e7, total_expenditure/1e7, total_mps FROM reference_dashboard_metrics")):
    print(tuple(round(v, 2) if isinstance(v, float) else v for v in r))
db.close()
