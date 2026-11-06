"""Admin data-management API.

Mounted from main.py via APIRouter. Every endpoint requires the admin role —
the frontend hides the page for other roles, but authorization is enforced
here, server-side.

Routes
------
GET    /admin/data/overview                dataset counts, version, last import
GET    /admin/data/imports                 import history
GET    /admin/data/imports/{id}            one batch (preview + details)
POST   /admin/data/uploads                 multipart upload → preview batches
POST   /admin/data/imports/{id}/preview    re-validate with a chosen type/house
POST   /admin/data/imports/{id}/commit     apply the previewed batch
POST   /admin/data/imports/{id}/rollback   undo a committed batch
POST   /admin/data/imports/{id}/refresh    re-run derived analytics only
DELETE /admin/data/imports/{id}            discard a non-committed batch
"""

import logging
import os
import tempfile
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy.orm import Session

import auth
import data_management as dm
import models
from database import SessionLocal

logger = logging.getLogger("mplads.admin_data")

router = APIRouter(prefix="/admin/data", tags=["Admin Data Management"])

_CHUNK = 1024 * 1024
_admin = Depends(auth.require_role("admin"))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _batch_or_404(db: Session, batch_id: int) -> models.ImportBatch:
    batch = db.query(models.ImportBatch).filter(models.ImportBatch.id == batch_id).first()
    if not batch:
        raise HTTPException(status_code=404, detail="Import batch not found")
    return batch


@router.get("/overview")
def data_overview(db: Session = Depends(get_db), _user: models.User = _admin):
    return dm.overview(db)


@router.get("/imports")
def import_history(
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    _user: models.User = _admin,
):
    return {"items": dm.list_batches(db, limit)}


@router.get("/imports/{batch_id}")
def import_detail(batch_id: int, db: Session = Depends(get_db), _user: models.User = _admin):
    payload = dm.get_batch(db, batch_id)
    if not payload:
        raise HTTPException(status_code=404, detail="Import batch not found")
    return payload


@router.post("/uploads", status_code=201)
async def upload_files(
    files: List[UploadFile] = File(...),
    dataset_type: Optional[str] = Form(None),
    house: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    user: models.User = _admin,
):
    """Stream each file to a staging directory, then validate it.

    Files are never executed or parsed as anything but delimited text. A
    failure on one file is reported for that file only; the others still
    produce previews.
    """
    if not files:
        raise HTTPException(status_code=400, detail="No files were uploaded")
    if len(files) > 20:
        raise HTTPException(status_code=400, detail="Upload at most 20 files at a time")

    dm._ensure_dir()
    results = []
    for upload in files:
        tmp_path = None
        try:
            if not (upload.filename or "").lower().endswith(dm.ALLOWED_EXTENSIONS):
                raise ValueError("Only CSV, TSV or TXT files are accepted")
            fd, tmp_path = tempfile.mkstemp(prefix="mplads_upload_", suffix=".csv", dir=dm.IMPORT_DIR)
            size = 0
            head = b""
            with os.fdopen(fd, "wb") as out:
                while True:
                    chunk = await upload.read(_CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > dm.MAX_FILE_BYTES:
                        raise ValueError(
                            f"File exceeds the {dm.MAX_FILE_BYTES // (1024 * 1024)} MB limit")
                    if len(head) < 65536:
                        head += chunk[:65536]
                    out.write(chunk)
            dm.validate_upload(upload.filename or "upload.csv", size, head)
            batch = dm.create_batch_from_path(
                db, upload.filename or "upload.csv", tmp_path, size, user,
                dataset_type=dataset_type or None, house_override=house or None,
            )
            tmp_path = None
            results.append({"ok": True, "batch": dm._batch_payload(batch, include_detail=True)})
        except ValueError as exc:
            results.append({"ok": False, "filename": upload.filename, "error": str(exc)})
        except Exception:
            logger.exception("upload failed for %s", upload.filename)
            results.append({"ok": False, "filename": upload.filename,
                            "error": "The file could not be processed."})
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
    return {"results": results}


@router.post("/imports/{batch_id}/preview")
def revalidate_batch(
    batch_id: int,
    dataset_type: Optional[str] = Form(None),
    house: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    _user: models.User = _admin,
):
    batch = _batch_or_404(db, batch_id)
    if batch.status in ("importing", "success", "partial", "rolled_back"):
        raise HTTPException(status_code=400, detail=f"Batch is already {batch.status}")
    try:
        dm.preview_batch(db, batch, dataset_type=dataset_type or None,
                         house_override=house or None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        logger.exception("revalidation failed for batch %s", batch_id)
        raise HTTPException(status_code=500, detail="Validation failed while reading the file.")
    return dm._batch_payload(batch, include_detail=True)


@router.post("/imports/{batch_id}/commit")
def commit_import(
    batch_id: int,
    dataset_type: Optional[str] = Form(None),
    house: Optional[str] = Form(None),
    db: Session = Depends(get_db),
    user: models.User = _admin,
):
    batch = _batch_or_404(db, batch_id)
    try:
        return dm.commit_batch(db, batch, user, dataset_type=dataset_type or None,
                               house_override=house or None)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception:
        logger.exception("commit failed for batch %s", batch_id)
        db.rollback()
        raise HTTPException(status_code=500, detail="The import failed and was rolled back.")


@router.post("/imports/{batch_id}/rollback")
def rollback_import(batch_id: int, db: Session = Depends(get_db), user: models.User = _admin):
    batch = _batch_or_404(db, batch_id)
    try:
        return dm.rollback_batch(db, batch, user)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception:
        logger.exception("rollback failed for batch %s", batch_id)
        db.rollback()
        raise HTTPException(status_code=500, detail="Rollback failed; no partial undo was applied.")


@router.post("/imports/{batch_id}/refresh")
def refresh_import(batch_id: int, db: Session = Depends(get_db), _user: models.User = _admin):
    batch = _batch_or_404(db, batch_id)
    if batch.status not in ("success", "partial"):
        raise HTTPException(status_code=400, detail="Only completed imports can be refreshed")
    batch.status = "refreshing"
    db.commit()
    dm._spawn_refresh(batch.id)
    db.refresh(batch)
    return dm._batch_payload(batch, include_detail=True)


@router.delete("/imports/{batch_id}")
def discard_import(batch_id: int, db: Session = Depends(get_db), _user: models.User = _admin):
    batch = _batch_or_404(db, batch_id)
    try:
        dm.discard_batch(db, batch)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"deleted": batch_id}
