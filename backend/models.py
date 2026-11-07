from sqlalchemy import Column, Integer, String, Float, Boolean, Index, DateTime, Text
from database import Base


class User(Base):
    """Registered portal user (field verifier, district authority, analyst,
    auditor, admin)."""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    email = Column(String, unique=True, index=True, nullable=False)
    password_hash = Column(String, nullable=False)
    # 'public' | 'field_verifier' | 'district_authority' | 'analyst'
    #   | 'auditor' | 'admin'
    role = Column(String, index=True, nullable=False, default="analyst")
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(String, nullable=False)
    # Optional territorial assignment (used by district_authority users for
    # district-scoped views; admins manage these via the admin panel).
    assigned_district = Column(String, index=True)
    assigned_state = Column(String, index=True)


class PasswordReset(Base):
    """Single-use password reset token (short-lived)."""

    __tablename__ = "password_resets"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True, nullable=False)
    token = Column(String, index=True, nullable=False)
    expires_at = Column(String, nullable=False)
    used = Column(Boolean, nullable=False, default=False)
    created_at = Column(String, nullable=False)


class UserInvestigation(Base):
    """
    Per-user investigation record.

    Complements the shared per-project investigation workspace: the shared
    workspace holds the evidence checklist (derived from real project data),
    while this table records WHICH user opened an investigation, its workflow
    status and the recommendation snapshot generated from actual risk results.
    """

    __tablename__ = "user_investigations"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True, nullable=False)
    project_id = Column(Integer, index=True, nullable=False)
    # 'Open' | 'Under Review' | 'Resolved'
    status = Column(String, index=True, nullable=False, default="Open")
    priority_tier = Column(String, index=True)   # P1..P4 at creation (from audit_intel)
    priority_label = Column(String)
    risk_score_at_start = Column(Integer)
    recommendations = Column(Text)  # JSON list — generated from recorded data
    created_at = Column(String, nullable=False)
    updated_at = Column(String, nullable=False)

    __table_args__ = (
        Index("idx_userinv_user_project", "user_id", "project_id", unique=True),
    )


class SavedProject(Base):
    """A project bookmarked by a user for their investigation workspace."""

    __tablename__ = "saved_projects"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True, nullable=False)
    project_id = Column(Integer, index=True, nullable=False)
    saved_at = Column(String, nullable=False)

    __table_args__ = (
        Index("idx_saved_user_project", "user_id", "project_id", unique=True),
    )


class AuditCase(Base):
    """An audit case saved from the generated case for one project."""

    __tablename__ = "audit_cases"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, index=True, nullable=False)
    project_id = Column(Integer, index=True, nullable=False)
    case_id = Column(String, index=True, nullable=False)  # from audit_intel generator
    priority_tier = Column(String, index=True)
    priority_label = Column(String)
    risk_score = Column(Integer)
    risk_level = Column(String)
    # 'Open' | 'Under Review' | 'Resolved' — mirrors the linked investigation
    status = Column(String, index=True, nullable=False, default="Open")
    created_at = Column(String, nullable=False)

    __table_args__ = (
        Index("idx_case_user_project", "user_id", "project_id"),
    )

class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, index=True)
    project_name = Column(String, index=True)
    state = Column(String, index=True)
    district = Column(String, index=True)
    constituency = Column(String, index=True)
    project_type = Column(String, index=True)
    sanctioned_amount = Column(Float, index=True)
    expenditure = Column(Float, index=True)
    completion_percentage = Column(Float, index=True)
    status = Column(String, index=True)
    fy = Column(String, index=True)
    # House attribution, derived from recommended_works name matches (only
    # when all matching works agree on one house). NULL = unattributed —
    # aggregations must treat NULL as unknown, never guess a house.
    house = Column(String, index=True)

    __table_args__ = (
        Index("idx_project_state_dist", "state", "district"),
        Index("idx_project_state_status", "state", "status"),
        Index("idx_proj_fy", "fy"),
        Index("idx_proj_house", "house"),
    )


class MPSummary(Base):
    __tablename__ = "mp_summaries"

    id = Column(Integer, primary_key=True, index=True)
    mp_name = Column(String, index=True)
    constituency = Column(String, index=True)
    state = Column(String, index=True)
    house = Column(String)
    allocated_amount = Column(Float)
    total_expenditure = Column(Float)
    utilization_percentage = Column(Float)
    completed_works = Column(Integer)
    recommended_works = Column(Integer)
    completion_rate_percentage = Column(Float)
    unspent_amount = Column(Float)
    transaction_count = Column(Integer)
    successful_payments = Column(Integer)
    pending_payments = Column(Integer)
    average_rating = Column(Float)


class RecommendedWork(Base):
    __tablename__ = "recommended_works"

    id = Column(Integer, primary_key=True, index=True)
    work_id = Column(Integer, index=True)
    work_description = Column(String)
    category = Column(String, index=True)
    mp_name = Column(String, index=True)
    constituency = Column(String, index=True)
    state = Column(String, index=True)
    house = Column(String)
    recommended_amount = Column(Float)
    recommendation_date = Column(String)
    has_images = Column(Boolean)
    ida = Column(String)
    # Row provenance: 'esakshi' = part of the official snapshot batch,
    # 'github_legacy' = supplementary GitHub-provider row (NULL work_id) or
    # a work dropped from the current eSAKSHI view. Official-metric
    # aggregations count only 'esakshi' rows.
    source = Column(String)


class Expenditure(Base):
    __tablename__ = "expenditures"

    id = Column(Integer, primary_key=True, index=True)
    mp_name = Column(String, index=True)
    constituency = Column(String, index=True)
    state = Column(String, index=True)
    house = Column(String)
    work_description = Column(String)
    vendor = Column(String)
    ida = Column(String)
    expenditure_amount = Column(Float)
    expenditure_date = Column(String)
    payment_status = Column(String, index=True)


class CompletedWork(Base):
    __tablename__ = "completed_works"

    id = Column(Integer, primary_key=True, index=True)
    work_id = Column(Integer, index=True)
    work_description = Column(String)
    category = Column(String, index=True)
    mp_name = Column(String, index=True)
    constituency = Column(String, index=True)
    state = Column(String, index=True)
    house = Column(String)
    final_amount = Column(Float)
    completed_date = Column(String)
    has_images = Column(Boolean)
    average_rating = Column(Float)
    ida = Column(String)
    # Row provenance — see RecommendedWork.source.
    source = Column(String)


class RiskScore(Base):
    __tablename__ = "risk_scores"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, index=True, unique=True)
    ml_anomaly = Column(Boolean, index=True)
    ml_score = Column(Float)
    risk_score = Column(Integer, index=True)
    risk_level = Column(String, index=True)
    reasons = Column(String)

    __table_args__ = (
        Index("idx_risk_level_score", "risk_level", "risk_score"),
    )


class ProjectTimelineEvent(Base):
    """
    Normalized project lifecycle event.

    Built by matching the existing work-level datasets
    (recommended_works, expenditures, completed_works) to projects using
    exact normalized (name, constituency, state) keys. Events are only
    created where the underlying data actually supports them — no dates
    are ever invented.
    """

    __tablename__ = "project_timeline_events"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, index=True, nullable=False)
    # 'recommendation' | 'expenditure' | 'completion' | 'status'
    event_type = Column(String, index=True, nullable=False)
    event_date = Column(String)  # ISO date (YYYY-MM-DD) or empty if unknown
    title = Column(String)
    description = Column(Text)
    amount = Column(Float)              # nullable — not meaningful for all events
    progress_percentage = Column(Float)  # nullable
    source_dataset = Column(String)      # 'recommended_works' | 'expenditures' | 'completed_works' | 'projects'
    source_record_id = Column(String)    # original row id in the source table
    match_confidence = Column(String, index=True)  # 'exact' | 'mp_verified'

    __table_args__ = (
        Index("idx_pte_project_date", "project_id", "event_date"),
    )


class AuditInvestigation(Base):
    """
    An audit investigation workspace for one flagged project.

    Only workflow state created by the user is stored (status, note).
    No project values are copied here — they always come from the
    projects / risk_scores tables so nothing can drift or be fabricated.
    """

    __tablename__ = "audit_investigations"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, index=True, unique=True, nullable=False)
    # 'New' | 'Evidence Required' | 'Under Review' | 'Resolved'
    status = Column(String, index=True, nullable=False, default="New")
    note = Column(Text)
    risk_score_at_start = Column(Integer)
    risk_level_at_start = Column(String)
    priority_tier_at_start = Column(String, index=True)
    created_at = Column(String, nullable=False)
    updated_at = Column(String, nullable=False)


class AuditEvidenceCheck(Base):
    """One evidence-checklist item inside an audit investigation."""

    __tablename__ = "audit_evidence_checks"

    id = Column(Integer, primary_key=True, index=True)
    investigation_id = Column(Integer, index=True, nullable=False)
    item_key = Column(String, index=True, nullable=False)
    label = Column(String, nullable=False)
    category = Column(String, index=True)
    severity = Column(String, index=True)  # 'high' | 'medium' | 'low'
    rationale = Column(Text)  # grounded, generated from actual project values
    # 'Pending' | 'Verified' | 'Discrepancy Found' | 'Not Applicable'
    status = Column(String, index=True, nullable=False, default="Pending")
    updated_at = Column(String, nullable=False)

    __table_args__ = (
        Index("idx_evidence_inv_item", "investigation_id", "item_key"),
    )


class InvestigationFindingReview(Base):
    """Persisted reviewer workflow for one generated investigation finding.

    Findings themselves are regenerated from the current project records (so
    they cannot drift from the data); only the human review state is stored.
    The unique key keeps one review row per finding per investigation.
    """

    __tablename__ = "investigation_finding_reviews"

    id = Column(Integer, primary_key=True, index=True)
    investigation_id = Column(Integer, index=True, nullable=False)
    finding_key = Column(String(80), index=True, nullable=False)
    status = Column(String(40), index=True, nullable=False, default="Open")
    comment = Column(Text)
    evidence_reference = Column(Text)
    document_name = Column(String(240))
    document_url = Column(Text)
    resolution = Column(Text)
    reviewer_user_id = Column(Integer, index=True)
    reviewer_name = Column(String(160))
    reviewer_role = Column(String(40))
    created_at = Column(String, nullable=False)
    updated_at = Column(String, nullable=False)

    __table_args__ = (
        Index("idx_finding_review_inv_key", "investigation_id", "finding_key", unique=True),
    )


class InvestigationHistory(Base):
    """Append-only audit trail for investigation workflow changes.

    Project-derived findings are not copied here; `detail` contains only the
    workflow event and references needed to understand who changed what.
    """

    __tablename__ = "investigation_history"

    id = Column(Integer, primary_key=True, index=True)
    investigation_id = Column(Integer, index=True, nullable=False)
    event = Column(String(80), index=True, nullable=False)
    actor_user_id = Column(Integer, index=True)
    actor_name = Column(String(160))
    actor_role = Column(String(40))
    detail = Column(Text)
    created_at = Column(String, nullable=False)

    __table_args__ = (
        Index("idx_investigation_history_inv", "investigation_id", "created_at"),
    )


class SyncMetadata(Base):
    __tablename__ = "sync_metadata"

    id = Column(Integer, primary_key=True, index=True)
    source = Column(String, nullable=False)  # 'csv_upload', 'github', 'api', 'manual'
    status = Column(String, nullable=False)  # 'success', 'partial', 'failed'
    records_fetched = Column(Integer, default=0)
    records_inserted = Column(Integer, default=0)
    records_updated = Column(Integer, default=0)
    records_unchanged = Column(Integer, default=0)
    records_failed = Column(Integer, default=0)
    source_updated_at = Column(String)  # When the source data was last updated (if known)
    synced_at = Column(String, nullable=False)  # When this sync was performed
    error_message = Column(Text)


# ──────────────────────────────────────────────────────────────────────
# AI Audit & Verification (SIH 26102) — new tables only, existing data
# is never touched. Created via Base.metadata.create_all at startup.
# ──────────────────────────────────────────────────────────────────────


class PhotoHash(Base):
    """Perceptual (dHash) fingerprint of an uploaded progress/field photo.

    Used to detect duplicate photo submission across projects — the "photo
    fraud" signal. A 64-bit dHash is stored as a 16-char hex string.
    """
    __tablename__ = "photo_hashes"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, index=True, nullable=False)
    dhash = Column(String(16), index=True, nullable=False)
    source = Column(String, default="upload")  # upload | field_verify
    created_at = Column(String, nullable=False)

    __table_args__ = (
        Index("idx_photo_hash", "dhash"),
        Index("idx_photo_project", "project_id"),
    )


class VerificationReport(Base):
    """A citizen/ground-inspector field verification submission (/verify).

    Stores the live geolocation captured at submit time (browser GPS API),
    the submitted status, an optional note, and the distance in metres from
    the project's recorded site coordinate (if that coordinate exists).
    """
    __tablename__ = "verification_reports"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, index=True, nullable=False)
    reporter_name = Column(String)
    status = Column(String, nullable=False)  # Functional | Non-Functional | Work Not Started
    lat = Column(Float)
    lon = Column(Float)
    distance_m = Column(Float)  # distance from recorded site coords, if known
    gps_source = Column(String, default="browser")  # browser | exif
    photo_hash_id = Column(Integer, index=True)  # optional link to stored photo
    note = Column(Text)
    created_at = Column(String, nullable=False)
    # Submitting user's identity (set for signed-in field verifiers; legacy
    # rows keep reporter_name only).
    user_id = Column(Integer, index=True)
    user_role = Column(String)
    # Set when the report responds to an auditor inquiry.
    inquiry_id = Column(Integer, index=True)
    # Citizen-evidence lifecycle. submission_kind: 'field' (verifier) |
    # 'citizen'. review_status: pending | verified | rejected | escalated.
    submission_kind = Column(String, index=True, default="field")
    review_status = Column(String, index=True, default="pending")
    # Review outcome — verifier identity/note/timestamp when dispositioned.
    reviewed_by_user_id = Column(Integer, index=True)
    reviewed_by_name = Column(String)
    review_note = Column(Text)
    reviewed_at = Column(String)
    # Optional routing: verifier assigned to review this submission.
    assigned_verifier_id = Column(Integer, index=True)

    __table_args__ = (
        Index("idx_verification_project", "project_id"),
        Index("idx_verification_status", "status"),
        Index("idx_verification_review", "review_status"),
        Index("idx_verification_kind", "submission_kind"),
    )


class EvidenceEvent(Base):
    """Append-only audit trail for a verification report's lifecycle
    (submitted → review_verified / review_rejected / escalated → …).
    One row per status change; never updated or deleted."""

    __tablename__ = "evidence_events"

    id = Column(Integer, primary_key=True, index=True)
    report_id = Column(Integer, index=True, nullable=False)
    event = Column(String, nullable=False)      # submitted | review_verified | review_rejected | escalated
    actor_user_id = Column(Integer, index=True)
    actor_name = Column(String)
    actor_role = Column(String)
    note = Column(Text)
    created_at = Column(String, nullable=False)

    __table_args__ = (
        Index("idx_evidence_event_report", "report_id"),
    )


class AuditAction(Base):
    """An auditor's disposition recorded from the inspection modal."""
    __tablename__ = "audit_actions"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, index=True, nullable=False)
    action = Column(String, nullable=False)  # approve | inquiry | escalate
    note = Column(Text)
    actor = Column(String)  # current user email/name if available
    created_at = Column(String, nullable=False)

    __table_args__ = (
        Index("idx_audit_action_project", "project_id"),
    )
    details = Column(Text)  # JSON string with additional sync details


class Inquiry(Base):
    """Auditor inquiry on a project, routed to the project's district
    authority. Workflow: auditor issues -> authority responds -> auditor
    reviews/closes."""

    __tablename__ = "inquiries"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, index=True, nullable=False)
    issued_by_user_id = Column(Integer, index=True)
    issued_by_name = Column(String)
    district = Column(String, index=True)  # routing key (project district)
    question = Column(Text, nullable=False)
    status = Column(String, index=True, nullable=False, default="open")
    # open | responded | closed
    response_text = Column(Text)
    responded_by_user_id = Column(Integer, index=True)
    responded_by_name = Column(String)
    responded_at = Column(String)
    closed_by_user_id = Column(Integer, index=True)
    closed_at = Column(String)
    created_at = Column(String, nullable=False)

    __table_args__ = (
        Index("idx_inquiry_status", "status"),
        Index("idx_inquiry_district", "district"),
    )


class ProjectRecInfo(Base):
    """
    Derived, rebuildable recommendation/timeline facts per project.

    Built by SQL joins from the work-level source tables using the SAME
    conservative linkage as activity.py (normalized name+constituency+state
    work key, MP-verified when a recommended work matches). Nothing here is
    invented: dates come from recommended_works.recommendation_date and the
    earliest matched expenditure_date; MP comes from recommended_works.

    Rebuild after data syncs via rec_info.rebuild(db).
    """

    __tablename__ = "project_rec_info"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, index=True, nullable=False)
    recommendation_date = Column(String)   # ISO YYYY-MM-DD or NULL
    recommended_by = Column(String)        # MP name from recommended_works
    approx_start_date = Column(String)     # ISO, earliest VALID (on/after rec) expenditure
    has_expenditure = Column(Boolean, default=False)
    # 'valid' | 'pre_recommendation' | 'no_expenditure' — drives UI wording;
    # pre_recommendation means every linked expenditure predates the
    # recommendation date, so no start proxy is shown.
    start_status = Column(String)
    # SUM(expenditure_amount) of ledger payments linked to this project's
    # work key (same conservative linkage). The authoritative per-project
    # spend; the catalog's `expenditure` column is a zeroed legacy stamp.
    linked_expenditure = Column(Float)
    # Linkage detail (all from the same MP+IDA-verified ledger join):
    linked_tx_count = Column(Integer)          # number of linked payment records
    first_expenditure_date = Column(String)    # earliest linked payment date
    latest_expenditure_date = Column(String)   # most recent linked payment date
    # normalized work key ('name|constituency|state') — lets consumers detect
    # catalog rows sharing one work key (assignment ambiguity) and lets
    # state reconciliation group by actual work instead of duplicating rows.
    work_key = Column(String)
    # MP+IDA-verified work-level payment total regardless of key uniqueness,
    # and the number of catalog rows sharing the key. When work_key_rows > 1
    # the total cannot be split per row: linked_expenditure stays 0 and the
    # status is 'ambiguous_shared_work' (never assign every row the full
    # work total).
    work_key_expenditure = Column(Float)
    work_key_rows = Column(Integer)

    __table_args__ = (
        Index("idx_rec_info_project", "project_id", unique=True),
        Index("idx_rec_info_rec_date", "recommendation_date"),
    )


# ──────────────────────────────────────────────────────────────────────
# Admin data management — CSV ingestion pipeline
# ──────────────────────────────────────────────────────────────────────


class ImportBatch(Base):
    """One CSV upload through the admin data-management pipeline.

    Lifecycle: preview → importing → success | failed | rolled_back |
    discarded. The uploaded file is kept on disk (staging dir) so a preview
    can be committed later without re-uploading and a failed import can be
    retried from the same source bytes.
    """

    __tablename__ = "import_batches"

    id = Column(Integer, primary_key=True, index=True)
    token = Column(String, index=True, nullable=False)   # random staging id
    filename = Column(String, nullable=False)
    stored_path = Column(String, nullable=False)
    file_size = Column(Integer, default=0)
    dataset_type = Column(String, index=True)            # detected/confirmed type
    detected_type = Column(String)                       # what the sniffer found
    detection_confidence = Column(Float, default=0.0)
    detected_columns = Column(Text)                      # JSON: {canonical: header}
    house_coverage = Column(String)                      # 'Lok Sabha' | 'Rajya Sabha' | 'Mixed' | 'Unknown'
    # Admin-chosen house, used when the file has no House column.
    house_override = Column(String)
    uploaded_by = Column(String)
    uploaded_by_id = Column(Integer, index=True)
    uploaded_at = Column(String, nullable=False)
    status = Column(String, index=True, nullable=False, default="preview")
    total_rows = Column(Integer, default=0)
    valid_rows = Column(Integer, default=0)
    new_rows = Column(Integer, default=0)
    update_rows = Column(Integer, default=0)
    unchanged_rows = Column(Integer, default=0)
    duplicate_rows = Column(Integer, default=0)
    rejected_rows = Column(Integer, default=0)
    error_summary = Column(Text)
    preview_json = Column(Text)      # JSON: issues, conflicts, dataset-specific details
    details_json = Column(Text)      # JSON: commit/refresh details, summary
    committed_at = Column(String)
    rolled_back_at = Column(String)
    dataset_version = Column(Integer, default=0)

    __table_args__ = (
        Index("idx_import_status_at", "status", "uploaded_at"),
    )


class ImportChange(Base):
    """Per-row before-state captured during a commit, used for rollback.

    insert → record_pk is the new row id (rollback deletes it)
    update → before_json holds the previous column values (rollback restores)
    """

    __tablename__ = "import_changes"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, index=True, nullable=False)
    table_name = Column(String, index=True, nullable=False)
    record_pk = Column(Integer, index=True)
    change_type = Column(String, nullable=False)   # 'insert' | 'update'
    before_json = Column(Text)

    __table_args__ = (
        Index("idx_import_change_batch", "batch_id", "table_name"),
    )


# ──────────────────────────────────────────────────────────────────────
# Project change detection
# ──────────────────────────────────────────────────────────────────────


class ProjectSnapshot(Base):
    """The project values one import batch carried for one project.

    Snapshots are written from the INCOMING dataset (not from the database
    afterwards), so an unchanged project still has a row here. That is what
    makes "missing from the latest import" distinguishable from "unchanged":
    a project with no snapshot in a batch was genuinely absent from the file.

    Only tracked fields are stored; every value comes from the source record,
    never inferred. `rec_date` / `completion_date` stay NULL unless the source
    dataset carries those columns (the projects CSV has no date fields; the
    lifecycle dates live in the linked work datasets).
    """

    __tablename__ = "project_snapshots"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, index=True, nullable=False)
    project_id = Column(Integer, index=True, nullable=False)
    source = Column(String, default="csv_upload")
    dataset_type = Column(String)
    captured_at = Column(String, nullable=False)
    project_name = Column(Text)
    state = Column(String, index=True)
    district = Column(String)
    constituency = Column(String)
    house = Column(String, index=True)
    project_type = Column(String)
    status = Column(String, index=True)
    fy = Column(String)
    sanctioned_amount = Column(Float)
    expenditure = Column(Float)
    completion_percentage = Column(Float)
    rec_date = Column(String)
    completion_date = Column(String)
    fingerprint = Column(String, index=True)

    __table_args__ = (
        Index("idx_snapshot_batch_project", "batch_id", "project_id"),
        Index("idx_snapshot_project_captured", "project_id", "captured_at"),
    )


class ProjectChangeEvent(Base):
    """One recorded change to one project field in one import batch.

    Every row points at a real ProjectSnapshot pair (or at an insertion /
    absence recorded by the batch), so a change can never be displayed
    without an import to trace it to.
    """

    __tablename__ = "project_change_events"

    id = Column(Integer, primary_key=True, index=True)
    batch_id = Column(Integer, index=True, nullable=False)
    project_id = Column(Integer, index=True, nullable=False)
    # new_project | updated_project | financial_change | completion_change |
    # status_change | missing_from_import | reappeared
    change_type = Column(String, index=True, nullable=False)
    field = Column(String, index=True)
    previous_value = Column(Text)
    new_value = Column(Text)
    previous_numeric = Column(Float)
    new_numeric = Column(Float)
    delta_numeric = Column(Float)
    delta_percent = Column(Float)
    changed_at = Column(String, index=True, nullable=False)
    house = Column(String, index=True)
    state = Column(String, index=True)
    project_status = Column(String, index=True)
    source = Column(String, default="csv_upload")
    # high = matched on the stable project id; review = the description itself
    # changed, so identity deserves a human look before conclusions.
    confidence = Column(String, default="high")
    impact = Column(String, index=True)
    impact_detail = Column(Text)

    __table_args__ = (
        Index("idx_change_project_at", "project_id", "changed_at"),
        Index("idx_change_batch", "batch_id"),
        Index("idx_change_type_field", "change_type", "field"),
        Index("idx_change_state_house", "state", "house"),
    )
