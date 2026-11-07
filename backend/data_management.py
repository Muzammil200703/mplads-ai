"""Admin CSV ingestion pipeline for MPLADS-AI.

Turns the current one-off import scripts into a safe, auditable upload flow:

    upload → detect dataset → validate → preview → commit (one transaction)
           → refresh derived analytics (rec_info + risk + caches) → history

Design notes
------------
* Streaming: files are parsed twice at most (a light prescan for keys, then
  the classification pass) with ``csv.DictReader`` and ``yield_per`` reads.
  Nothing is loaded with pandas and no whole-table ORM materialisation
  happens except for the bounded indexes needed by the current file.
* Idempotent: keyed datasets (recommended / completed / MP summary /
  projects) upsert by a stable source key that always includes the House.
  The expenditure ledger has no source id, so it uses a content-fingerprint
  multiset: a fingerprint already present N times in the DB consumes the
  first N occurrences in the file, and only the surplus is inserted. Re-
  uploading the same file therefore inserts nothing.
* Reversible: every inserted row id and every changed row's prior values are
  recorded in ``import_changes`` inside the same transaction, so a completed
  batch can be rolled back exactly.
* Serialised: a process-wide lock plus an "importing" batch check prevents
  two concurrent imports from interleaving SQLite writes.
"""

from __future__ import annotations

import csv
import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import bindparam, func, text
from sqlalchemy.orm import Session

import models
from database import SessionLocal, normkey

logger = logging.getLogger("mplads.data_management")

IMPORT_DIR = os.environ.get(
    "MPLADS_IMPORT_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "imports"),
)
MAX_FILE_BYTES = int(os.environ.get("MPLADS_IMPORT_MAX_BYTES", 60 * 1024 * 1024))
MAX_ISSUES = 250
MAX_LINKAGE_KEYS = 40000
ALLOWED_EXTENSIONS = (".csv", ".tsv", ".txt")

_WRITE_LOCK = threading.Lock()
_REFRESH_LOCK = threading.Lock()

HOUSES = ("Lok Sabha", "Rajya Sabha")
PROJECT_STATUSES = {
    "ongoing", "completed", "complete", "sanctioned", "in progress",
    "in-progress", "not started", "stalled", "abandoned", "dropped",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────────────────
# Value parsing
# ─────────────────────────────────────────────────────────────────────

def _clean(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def parse_money(value: Any) -> Optional[float]:
    """Parse '1,234.50', '₹1,234', 'Rs 1234', '1234.5%' → float."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s or s.lower() in {"na", "n/a", "nan", "none", "-", "--", "nil"}:
        return None
    s = s.replace("\u20b9", "").replace(",", "").replace("₹", "")
    s = re.sub(r"(?i)\b(rs|inr|rupees|usd)\b\.?", "", s)
    s = s.replace("%", "").strip()
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def parse_date(value: Any) -> Optional[str]:
    """Parse a wide set of date spellings to the DB's ISO-with-Z form.

    The existing dataset stores ``YYYY-MM-DDT00:00:00.000Z``; normalising to
    that shape keeps sorting/comparisons consistent with historical rows.
    Returns None when the value is not a recognisable date.
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.lower() in {"na", "n/a", "nan", "none", "-", "--"}:
        return None
    # ISO timestamp already (2026-08-21T00:00:00.000Z)
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})(?:[T ].*)?$", s)
    if m:
        try:
            # Validate the calendar date — the regex alone accepts 2026-13-45.
            datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}T00:00:00.000Z"
        except ValueError:
            return None
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d", "%d.%m.%Y",
                "%d %b %Y", "%d %B %Y", "%b %d %Y", "%b %d, %Y",
                "%B %d %Y", "%B %d, %Y", "%d-%b-%Y", "%d-%b-%y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%dT00:00:00.000Z")
        except ValueError:
            continue
    return None


def parse_bool(value: Any) -> Optional[bool]:
    s = _clean(value).lower()
    if s in ("yes", "true", "1", "y", "t"):
        return True
    if s in ("no", "false", "0", "n", "f"):
        return False
    return None


def parse_house(value: Any) -> Optional[str]:
    s = _clean(value).lower().replace("_", " ").replace("-", " ")
    s = re.sub(r"\s+", " ", s)
    if not s:
        return None
    if s in ("ls", "lok sabha", "loksabha", "lower house", "lok"):
        return "Lok Sabha"
    if s in ("rs", "rajya sabha", "rajyasabha", "upper house", "rajya"):
        return "Rajya Sabha"
    return None


def _to_int(value: Any) -> Optional[int]:
    f = parse_money(value)
    if f is None:
        return None
    try:
        return int(round(f))
    except (OverflowError, ValueError):
        return None


# ─────────────────────────────────────────────────────────────────────
# Dataset schemas
# ─────────────────────────────────────────────────────────────────────
# Each field: (type, aliases). Aliases are compared after normkey(), so
# "Recommended Amount (₹)" → "recommended amount" matches directly.

def _f(field_type: str, *aliases: str) -> Tuple[str, Tuple[str, ...]]:
    return (field_type, tuple(aliases))


SPECS: Dict[str, Dict[str, Any]] = {
    "recommended_works": {
        "label": "Recommended Works",
        "model": models.RecommendedWork,
        "table": "recommended_works",
        "mode": "keyed",
        "fields": {
            "work_id": _f("int", "work id", "workid", "work no", "work number", "id"),
            "work_description": _f("text", "work description", "work", "description", "work name", "project name"),
            "category": _f("text", "category", "work category", "project type"),
            "mp_name": _f("text", "mp name", "mp", "member of parliament", "name of mp"),
            "constituency": _f("text", "constituency", "pc", "parliamentary constituency"),
            "state": _f("text", "state", "state name"),
            "house": _f("house", "house", "sabha"),
            "recommended_amount": _f("money", "recommended amount", "recommended amt", "amount", "sanctioned amount", "recommended"),
            "recommendation_date": _f("date", "recommendation date", "recommended date", "date"),
            "has_images": _f("bool", "has images", "images", "has image"),
            "ida": _f("text", "ida", "ida name", "implementing department agency"),
        },
        "required": ("work_id", "work_description", "mp_name", "constituency", "state", "recommended_amount"),
        "key": ("house", "work_id"),
        "compare": ("work_description", "category", "mp_name", "constituency", "state",
                    "recommended_amount", "recommendation_date", "has_images", "ida"),
        "discriminators": {"recommendation_date": 3.0, "recommended_amount": 2.0, "ida": 1.0},
    },
    "expenditures": {
        "label": "Expenditure Ledger",
        "model": models.Expenditure,
        "table": "expenditures",
        "mode": "fingerprint",
        "fields": {
            "mp_name": _f("text", "mp name", "mp", "member of parliament"),
            "constituency": _f("text", "constituency", "pc", "parliamentary constituency"),
            "state": _f("text", "state", "state name"),
            "house": _f("house", "house", "sabha"),
            "work_description": _f("text", "work description", "work", "description", "work name"),
            "vendor": _f("text", "vendor", "vendor name", "contractor"),
            "ida": _f("text", "ida", "ida name"),
            "expenditure_amount": _f("money", "expenditure amount", "expenditure", "amount", "amount paid"),
            "expenditure_date": _f("date", "expenditure date", "payment date", "date"),
            "payment_status": _f("text", "payment status", "status"),
        },
        "required": ("mp_name", "constituency", "state", "work_description",
                     "expenditure_amount", "expenditure_date"),
        "key": ("house", "work_description", "constituency", "state", "mp_name",
                "vendor", "ida", "expenditure_amount", "expenditure_date", "payment_status"),
        "compare": (),
        "discriminators": {"expenditure_amount": 3.0, "expenditure_date": 3.0, "vendor": 1.0},
    },
    "completed_works": {
        "label": "Completed Works",
        "model": models.CompletedWork,
        "table": "completed_works",
        "mode": "keyed",
        "fields": {
            "work_id": _f("int", "work id", "workid", "work no", "work number", "id"),
            "work_description": _f("text", "work description", "work", "description", "work name"),
            "category": _f("text", "category", "work category", "project type"),
            "mp_name": _f("text", "mp name", "mp", "member of parliament"),
            "constituency": _f("text", "constituency", "pc", "parliamentary constituency"),
            "state": _f("text", "state", "state name"),
            "house": _f("house", "house", "sabha"),
            "final_amount": _f("money", "final amount", "amount", "completed amount"),
            "completed_date": _f("date", "completed date", "completion date", "date"),
            "has_images": _f("bool", "has images", "images"),
            "average_rating": _f("money", "average rating", "rating"),
            "ida": _f("text", "ida", "ida name"),
        },
        "required": ("work_id", "work_description", "mp_name", "constituency", "state",
                     "final_amount", "completed_date"),
        "key": ("house", "work_id"),
        "compare": ("work_description", "category", "mp_name", "constituency", "state",
                    "final_amount", "completed_date", "has_images", "average_rating", "ida"),
        "discriminators": {"completed_date": 3.0, "final_amount": 2.0, "average_rating": 1.0},
    },
    "mp_summaries": {
        "label": "MP Summaries",
        "model": models.MPSummary,
        "table": "mp_summaries",
        "mode": "keyed",
        "fields": {
            "mp_name": _f("text", "mp name", "mp", "member of parliament"),
            "constituency": _f("text", "constituency", "pc", "parliamentary constituency"),
            "state": _f("text", "state", "state name"),
            "house": _f("house", "house", "sabha"),
            "allocated_amount": _f("money", "allocated amount", "allocation", "allocated"),
            "total_expenditure": _f("money", "total expenditure", "expenditure"),
            "utilization_percentage": _f("money", "utilization", "utilization percentage", "utilization pct"),
            "completed_works": _f("int", "completed works", "completed"),
            "recommended_works": _f("int", "recommended works", "recommended", "amount recommended"),
            "completion_rate_percentage": _f("money", "completion rate", "completion rate percentage"),
            "unspent_amount": _f("money", "balance not yet paid to vendors", "unspent amount", "balance"),
            "transaction_count": _f("int", "transaction count", "transactions"),
            "successful_payments": _f("int", "successful payments", "successful"),
            "pending_payments": _f("int", "pending payments", "pending"),
            "average_rating": _f("money", "average rating", "rating"),
        },
        "required": ("mp_name", "house", "allocated_amount"),
        "key": ("house", "mp_name_key"),
        "compare": ("constituency", "state", "allocated_amount", "total_expenditure",
                    "utilization_percentage", "completed_works", "recommended_works",
                    "completion_rate_percentage", "unspent_amount", "transaction_count",
                    "successful_payments", "pending_payments", "average_rating"),
        "discriminators": {"transaction_count": 2.0, "utilization_percentage": 2.0,
                           "completed_works": 1.0},
    },
    "projects": {
        "label": "Project Records",
        "model": models.Project,
        "table": "projects",
        "mode": "projects",
        "fields": {
            "id": _f("int", "id", "project id", "projectid", "sl no", "serial no"),
            "project_name": _f("text", "project name", "work description", "project", "name", "title"),
            "state": _f("text", "state", "state name"),
            "district": _f("text", "district", "district name"),
            "constituency": _f("text", "constituency", "pc"),
            "project_type": _f("text", "project type", "category", "type"),
            "sanctioned_amount": _f("money", "sanctioned amount", "sanctioned", "allocated amount", "amount"),
            "expenditure": _f("money", "expenditure", "expenditure amount", "spent"),
            "completion_percentage": _f("money", "completion percentage", "completion", "progress"),
            "status": _f("text", "status", "project status"),
            "fy": _f("text", "fy", "financial year", "year"),
            "house": _f("house", "house", "sabha"),
        },
        "required": ("project_name", "state", "constituency"),
        "key": ("id_or_composite",),
        "compare": ("project_name", "state", "district", "constituency", "project_type",
                    "sanctioned_amount", "expenditure", "completion_percentage", "status",
                    "fy", "house"),
        "discriminators": {"district": 3.0, "completion_percentage": 2.0, "status": 2.0,
                           "project_type": 1.0},
    },
}

DATASET_LABELS = {k: v["label"] for k, v in SPECS.items()}


# ─────────────────────────────────────────────────────────────────────
# Header detection
# ─────────────────────────────────────────────────────────────────────

def _header_map(headers: List[str]) -> Dict[str, str]:
    """normkey(header) → original header (first wins)."""
    out: Dict[str, str] = {}
    for h in headers:
        key = normkey(h)
        if key and key not in out:
            out[key] = h
    return out


def _match_fields(headers: List[str], spec: Dict[str, Any]) -> Dict[str, str]:
    normalized = _header_map(headers)
    mapping: Dict[str, str] = {}
    for canonical, (_, aliases) in spec["fields"].items():
        for alias in aliases:
            key = normkey(alias)
            if key in normalized:
                mapping[canonical] = normalized[key]
                break
        if canonical in mapping:
            continue
        # Fallback: header whose tokens contain the alias tokens in order.
        for hkey, original in normalized.items():
            alias_key = normkey(aliases[0]) if aliases else ""
            if alias_key and alias_key in hkey:
                # Guard: "amount recommended" must not be claimed by "amount".
                if canonical not in mapping:
                    mapping[canonical] = original
                    break
    return mapping


def detect_dataset(headers: List[str]) -> Dict[str, Any]:
    """Score every spec against the file headers.

    Returns the best match, its matched/missing required columns, the
    canonical→header mapping, and the ranked alternatives. Confidence is the
    share of required columns found (only a full match is auto-accepted).
    """
    best_type: Optional[str] = None
    best_score = -1.0
    best_map: Dict[str, str] = {}
    ranked = []
    for name, spec in SPECS.items():
        mapping = _match_fields(headers, spec)
        required = spec["required"]
        matched = [c for c in required if c in mapping]
        frac = len(matched) / len(required) if required else 0.0
        bonus = sum(w for f, w in spec["discriminators"].items() if f in mapping)
        score = frac * 10 + bonus
        ranked.append({
            "dataset_type": name,
            "label": spec["label"],
            "required_found": len(matched),
            "required_total": len(required),
            "missing": [c for c in required if c not in mapping],
            "confidence": round(frac, 2),
        })
        if score > best_score:
            best_score, best_type, best_map = score, name, mapping
    ranked.sort(key=lambda r: (-r["confidence"], -r["required_found"]))
    best = next((r for r in ranked if r["dataset_type"] == best_type), None)
    confident = bool(best and best["required_total"] > 0
                     and best["required_found"] == best["required_total"])
    return {
        "dataset_type": best_type if confident else None,
        "candidate_type": best_type,
        "confidence": (best["confidence"] if best else 0.0),
        "confident": confident,
        "columns": best_map if confident else (best_map or {}),
        "missing": (best["missing"] if best else []),
        "ranked": ranked,
    }


# ─────────────────────────────────────────────────────────────────────
# Row normalisation / validation
# ─────────────────────────────────────────────────────────────────────

def _normalize_row(
    raw: Dict[str, Any],
    spec: Dict[str, Any],
    mapping: Dict[str, str],
    house_override: Optional[str],
) -> Tuple[Dict[str, Any], List[Dict[str, str]], List[Dict[str, str]]]:
    """Return (normalized values, errors, warnings)."""
    vals: Dict[str, Any] = {}
    errors: List[Dict[str, str]] = []
    warnings: List[Dict[str, str]] = []
    bad_values: List[str] = []

    for canonical, (typ, _aliases) in spec["fields"].items():
        header = mapping.get(canonical)
        raw_value = raw.get(header) if header else None
        raw_present = _clean(raw_value).lower() not in (
            "", "na", "n/a", "nan", "none", "-", "--", "nil")
        if typ == "int":
            v = _to_int(raw_value)
        elif typ == "money":
            v = parse_money(raw_value)
        elif typ == "date":
            v = parse_date(raw_value)
        elif typ == "bool":
            v = parse_bool(raw_value)
            if v is None:
                v = False
        elif typ == "house":
            v = parse_house(raw_value)
        else:
            v = _clean(raw_value) or None
        # A value that was present but could not be parsed is a data-quality
        # error, never silently treated as "missing".
        if typ in ("int", "money", "date") and v is None and raw_present:
            bad_values.append(canonical)
        vals[canonical] = v

    # House resolution: a House column wins; otherwise the admin's choice.
    if vals.get("house") is None:
        if house_override:
            vals["house"] = house_override
        elif "house" in spec["fields"]:
            vals["house"] = None
    if "house" in spec["fields"] and vals.get("house") is None:
        errors.append({"field": "house", "message": "House missing or unrecognised (Lok Sabha / Rajya Sabha)"})

    for canonical in bad_values:
        if canonical not in spec["required"]:
            errors.append({"field": canonical, "message": f"Invalid {canonical.replace('_', ' ')} value"})

    for canonical in spec["required"]:
        if vals.get(canonical) in (None, ""):
            human = canonical.replace("_", " ")
            errors.append({"field": canonical, "message": f"Missing or invalid {human}"})

    for canonical, v in list(vals.items()):
        if v is None:
            continue
        if canonical in ("recommended_amount", "final_amount", "expenditure_amount",
                         "sanctioned_amount", "expenditure", "allocated_amount",
                         "total_expenditure", "unspent_amount") and v < 0:
            errors.append({"field": canonical, "message": "Negative amount"})
        if canonical == "completion_percentage" and not (0 <= v <= 100):
            warnings.append({"field": canonical, "message": "Completion outside 0–100"})
        if canonical == "status" and _clean(v).lower() not in PROJECT_STATUSES:
            errors.append({"field": "status", "message": f"Unrecognised status '{v}'"})
        if canonical == "work_id" and v is not None and v <= 0:
            errors.append({"field": "work_id", "message": "Work ID must be positive"})

    # MP summary: 'Amount Recommended (₹)' shares the 'recommended works'
    # alias set; when the column is monetary (> 1000 rows) it's an amount,
    # not a count, so fall back to the count column if present.
    if spec["table"] == "mp_summaries":
        rw = vals.get("recommended_works")
        if rw is not None and rw > 1000 and mapping.get("recommended_works"):
            # Header literally "Amount Recommended" — not a work count.
            if "recommended" in normkey(mapping["recommended_works"]):
                vals["recommended_works"] = None

    # Optional missing-value warnings (never fatal).
    for canonical in ("district", "project_type", "status", "vendor", "ida"):
        if canonical in spec["fields"] and vals.get(canonical) in (None, ""):
            warnings.append({"field": canonical, "message": f"{canonical.replace('_', ' ').title()} unavailable"})
            break
    return vals, errors, warnings


def _row_key(spec: Dict[str, Any], vals: Dict[str, Any]) -> Optional[Tuple]:
    mode = spec["mode"]
    if mode == "keyed":
        parts = []
        for k in spec["key"]:
            if k == "mp_name_key":
                v = normkey(vals.get("mp_name") or "")
            else:
                v = vals.get(k)
            if v in (None, ""):
                return None
            parts.append(v)
        return tuple(parts)
    if mode == "projects":
        pid = vals.get("id")
        if pid:
            return ("id", int(pid))
        name = normkey(vals.get("project_name") or "")
        if not name:
            return None
        return ("composite", name, normkey(vals.get("constituency") or ""), normkey(vals.get("state") or ""))
    if mode == "fingerprint":
        amount = vals.get("expenditure_amount")
        return (
            vals.get("house") or "",
            normkey(vals.get("work_description") or ""),
            normkey(vals.get("constituency") or ""),
            normkey(vals.get("state") or ""),
            normkey(vals.get("mp_name") or ""),
            normkey(vals.get("vendor") or ""),
            normkey(vals.get("ida") or ""),
            round(float(amount), 2) if amount is not None else None,
            (vals.get("expenditure_date") or "")[:10],
            normkey(vals.get("payment_status") or ""),
        )
    return None


def _file_fingerprint(spec: Dict[str, Any], vals: Dict[str, Any]) -> Optional[str]:
    key = _row_key(spec, vals)
    if key is None or key[7] is None:
        return None
    return "|".join(str(p) for p in key)


# ─────────────────────────────────────────────────────────────────────
# Stored file helpers
# ─────────────────────────────────────────────────────────────────────

def _ensure_dir() -> None:
    os.makedirs(IMPORT_DIR, exist_ok=True)


def _stored_path(token: str) -> str:
    return os.path.join(IMPORT_DIR, f"{token}.csv")


def _detect_delimiter(sample: str) -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        return ","


def read_headers(path: str) -> Tuple[List[str], str]:
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        sample = fh.read(8192)
        fh.seek(0)
        delimiter = _detect_delimiter(sample)
        reader = csv.reader(fh, delimiter=delimiter)
        headers = next(reader, [])
    return [_clean(h) for h in headers], delimiter


def _rows(path: str, delimiter: str):
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh, delimiter=delimiter)
        for idx, raw in enumerate(reader, start=2):  # row 1 = header
            if not raw or all((v is None or str(v).strip() == "") for v in raw.values()):
                continue
            yield idx, raw


# ─────────────────────────────────────────────────────────────────────
# Existing-data indexes (bounded to what the file actually references)
# ─────────────────────────────────────────────────────────────────────

def _parse_stored_date(v: Any) -> Optional[str]:
    s = _clean(v)
    return s[:10] if len(s) >= 10 else None


def _prescan(path: str, delimiter: str, spec: Dict[str, Any], mapping: Dict[str, str],
             house_override: Optional[str]) -> Dict[str, Any]:
    """Light first pass: house values + the keys the file references."""
    info: Dict[str, Any] = {"house_values": set(), "keys": set(), "names": set(),
                            "ids": set(), "linkage": set(), "row_count": 0}
    mode = spec["mode"]
    for _idx, raw in _rows(path, delimiter):
        info["row_count"] += 1
        house = parse_house(raw.get(mapping.get("house"))) if mapping.get("house") else None
        if house:
            info["house_values"].add(house)
        elif house_override:
            info["house_values"].add(house_override)
        if mode == "keyed":
            k = _to_int(raw.get(mapping.get("work_id"))) if mapping.get("work_id") else None
            if k is not None:
                info["keys"].add(k)
            if spec["table"] == "mp_summaries" and mapping.get("mp_name"):
                nm = normkey(_clean(raw.get(mapping["mp_name"])))
                if nm:
                    info["names"].add(nm)
        elif mode == "projects":
            pid = _to_int(raw.get(mapping.get("id"))) if mapping.get("id") else None
            if pid:
                info["ids"].add(pid)
            else:
                nm = normkey(_clean(raw.get(mapping.get("project_name"))))
                if nm:
                    info["names"].add(nm)
        elif mode == "fingerprint":
            if len(info["linkage"]) < MAX_LINKAGE_KEYS:
                wd = normkey(_clean(raw.get(mapping.get("work_description"))))
                cons = normkey(_clean(raw.get(mapping.get("constituency"))))
                st = normkey(_clean(raw.get(mapping.get("state"))))
                if wd:
                    info["linkage"].add(f"{wd}|{cons}|{st}")
    return info


def _load_index(
    db: Session,
    spec: Dict[str, Any],
    info: Dict[str, Any],
    fingerprints: Optional[set] = None,
) -> Dict[Tuple, Tuple[int, Dict[str, Any]]]:
    """key → (pk, existing values) for rows the file may touch.

    For the expenditure ledger, ``fingerprints`` (the content fingerprints
    actually present in the uploaded file) restricts the group-by to the work
    descriptions the file references. The previous unconditional scan grouped
    all 108k ledger rows into a Python dict on every preview — ~30s for a 20k-
    row file. Missing fingerprints mean "absent from the DB" either way, so
    duplicate accounting is unchanged.
    """
    model = spec["model"]
    compare = [getattr(model, f) for f in spec["compare"]]
    index: Dict[Tuple, Tuple[int, Dict[str, Any]]] = {}
    mode = spec["mode"]

    if mode == "keyed":
        if spec["table"] == "mp_summaries":
            names = sorted(info["names"])
            for i in range(0, len(names), 200):
                chunk = names[i:i + 200]
                rows = db.query(model.id, model.house, model.mp_name, *compare).filter(
                    func.normkey(model.mp_name).in_(chunk)
                ).all()
                for r in rows:
                    key = (r[1], normkey(r[2] or ""))
                    index[key] = (r[0], dict(zip(spec["compare"], r[3:])))
            return index
        keys = sorted(info["keys"])
        houses = sorted(info["house_values"]) or list(HOUSES)
        for i in range(0, len(keys), 400):
            chunk = keys[i:i + 400]
            rows = db.query(model.id, model.house, model.work_id, *compare).filter(
                model.work_id.in_(chunk), model.house.in_(houses)
            ).all()
            for r in rows:
                key = (r[1], int(r[2]))
                index[key] = (r[0], dict(zip(spec["compare"], r[3:])))
        return index

    if mode == "projects":
        ids = sorted(info["ids"])
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            rows = db.query(model.id, *compare).filter(model.id.in_(chunk)).all()
            for r in rows:
                index[("id", int(r[0]))] = (r[0], dict(zip(spec["compare"], r[1:])))
                index[("composite", normkey(r[1] or ""), normkey(r[4] or ""), normkey(r[2] or ""))] = (
                    r[0], dict(zip(spec["compare"], r[1:])))
        # Composite lookups for rows without an id (bounded by the names seen).
        names = sorted(info["names"])
        for i in range(0, len(names), 200):
            chunk = names[i:i + 200]
            rows = db.query(model.id, *compare).filter(
                func.normkey(model.project_name).in_(chunk)
            ).all()
            for r in rows:
                vals = dict(zip(spec["compare"], r[1:]))
                index[("composite", normkey(vals.get("project_name") or ""),
                       normkey(vals.get("constituency") or ""),
                       normkey(vals.get("state") or ""))] = (r[0], vals)
        return index

    # fingerprint mode — existing occurrence counts per content fingerprint
    sql = (
        "SELECT house, work_description, constituency, state, mp_name, vendor, ida, "
        "expenditure_amount, expenditure_date, payment_status, COUNT(*) "
        "FROM expenditures {where} GROUP BY 1,2,3,4,5,6,7,8,9,10"
    )
    if fingerprints is not None:
        # Only the work descriptions referenced by this file. normkey() is a
        # deterministic UDF, so idx_exp_workkey answers the IN filter.
        descriptions = sorted({
            fp.split("|")[1] for fp in fingerprints
            if "|" in fp and fp.split("|")[1]
        })
        for i in range(0, len(descriptions), 250):
            chunk = descriptions[i:i + 250]
            stmt = text(sql.format(
                where="WHERE normkey(work_description) IN :keys"
            )).bindparams(bindparam("keys", expanding=True))
            for row in db.execute(stmt, {"keys": chunk}):
                fp = "|".join([
                    row[0] or "",
                    normkey(row[1] or ""), normkey(row[2] or ""), normkey(row[3] or ""),
                    normkey(row[4] or ""), normkey(row[5] or ""), normkey(row[6] or ""),
                    str(round(float(row[7] or 0), 2)),
                    (str(row[8] or "")[:10]),
                    normkey(row[9] or ""),
                ])
                if fp in fingerprints:
                    index[(fp,)] = (int(row[10]), {})
        return index

    counts = db.execute(text(sql.format(where="")))
    for row in counts:
        fp = "|".join([
            row[0] or "",
            normkey(row[1] or ""), normkey(row[2] or ""), normkey(row[3] or ""),
            normkey(row[4] or ""), normkey(row[5] or ""), normkey(row[6] or ""),
            str(round(float(row[7] or 0), 2)),
            (str(row[8] or "")[:10]),
            normkey(row[9] or ""),
        ])
        index[(fp,)] = (int(row[10]), {})
    return index


# ─────────────────────────────────────────────────────────────────────
# Core scan (preview + apply)
# ─────────────────────────────────────────────────────────────────────

def _existing_work_keys(db: Session, linkage_keys: List[str]) -> set:
    """Which (work, constituency, state) keys exist in recommended_works.

    Queries by the work-description prefix in bounded chunks so the existing
    expression index (idx_rec_workkey) can be used; composite equality is
    then checked in Python.
    """
    found = set()
    descriptions = sorted({k.split("|")[0] for k in linkage_keys if k.split("|")[0]})
    for i in range(0, len(descriptions), 250):
        chunk = descriptions[i:i + 250]
        stmt = text(
            "SELECT DISTINCT normkey(work_description), normkey(constituency), "
            "normkey(state) FROM recommended_works "
            "WHERE normkey(work_description) IN :keys"
        ).bindparams(bindparam("keys", expanding=True))
        for wd, cons, st in db.execute(stmt, {"keys": chunk}):
            found.add(f"{wd}|{cons or ''}|{st or ''}")
    return found


def _scan(
    db: Session,
    path: str,
    delimiter: str,
    spec: Dict[str, Any],
    mapping: Dict[str, str],
    batch_id: Optional[int],
    house_override: Optional[str],
    apply: bool,
) -> Dict[str, Any]:
    """Validate + classify every row; optionally write the changes."""
    info = _prescan(path, delimiter, spec, mapping, house_override)

    # Fingerprint counts are needed both to bound the existing-data index and
    # for the surplus (duplicate) accounting below — compute them once.
    file_counts: Dict[str, int] = {}
    if spec["mode"] == "fingerprint":
        for _idx, raw in _rows(path, delimiter):
            fp = _file_fingerprint(spec, _normalize_row(raw, spec, mapping, house_override)[0])
            if fp:
                file_counts[fp] = file_counts.get(fp, 0) + 1
    index = _load_index(
        db, spec, info,
        fingerprints=set(file_counts) if file_counts else None,
    )

    counts = {"total": 0, "valid": 0, "new": 0, "update": 0, "unchanged": 0,
              "duplicate": 0, "rejected": 0}
    issues: List[Dict[str, Any]] = []
    duplicate_examples: List[Dict[str, Any]] = []
    issue_count = 0
    seen_keys: Dict[Tuple, Dict[str, Any]] = {}
    inserted_rows: List[Any] = []
    affected_mp: set = set()
    mp_house: set = set()
    changed_fields_total = 0

    # ── Project change detection: remember the dataset state of EVERY row this
    #    batch carries (including unchanged ones) so the next import can tell
    #    "still present" apart from "missing from the file". Written inside the
    #    import transaction; a rolled-back import leaves no snapshot rows.
    import change_tracking

    snapshot_rows: List[Dict[str, Any]] = []

    def _snapshot(pid, values):
        if not (apply and spec["table"] == "projects" and pid and batch_id is not None):
            return
        snapshot_rows.append(change_tracking.snapshot_mapping(batch_id, pid, values))
        if len(snapshot_rows) >= 1000:
            db.bulk_insert_mappings(models.ProjectSnapshot, snapshot_rows)
            snapshot_rows.clear()

    def _flush_snapshots():
        if snapshot_rows:
            db.bulk_insert_mappings(models.ProjectSnapshot, snapshot_rows)
            snapshot_rows.clear()

    # Expenditure surplus bookkeeping (multiset semantics).
    surplus: Dict[str, int] = {}
    if spec["mode"] == "fingerprint":
        for fp, n in file_counts.items():
            existing_n = index.get((fp,), (0, {}))[0]
            surplus[fp] = n - existing_n

    linkage_checked = False
    missing_linkage: set = set()
    if spec["mode"] == "fingerprint" and info["linkage"]:
        existing_work = _existing_work_keys(db, sorted(info["linkage"]))
        missing_linkage = info["linkage"] - existing_work
        linkage_checked = True

    for row_no, raw in _rows(path, delimiter):
        counts["total"] += 1
        vals, errors, warnings = _normalize_row(raw, spec, mapping, house_override)

        if errors:
            counts["rejected"] += 1
            for e in errors[:4]:
                if issue_count < MAX_ISSUES:
                    issues.append({"row": row_no, "field": e["field"],
                                   "message": e["message"], "severity": "error"})
                    issue_count += 1
            continue

        if apply and spec["table"] == "mp_summaries":
            affected_mp.add((vals.get("house"), normkey(vals.get("mp_name") or "")))
        elif apply:
            if vals.get("house") and vals.get("mp_name"):
                affected_mp.add((vals.get("house"), normkey(vals["mp_name"])))

        key = _row_key(spec, vals)
        if key is None:
            counts["rejected"] += 1
            if issue_count < MAX_ISSUES:
                issues.append({"row": row_no, "field": "key", "severity": "error",
                               "message": "Missing/invalid identifier for this dataset"})
                issue_count += 1
            continue

        if spec["mode"] == "fingerprint":
            fp = _file_fingerprint(spec, vals)
            left = surplus.get(fp, 0)
            if left > 0:
                surplus[fp] = left - 1
                action = "new"
            else:
                action = "unchanged"
            counts["valid"] += 1
            if action == "new":
                counts["new"] += 1
                if apply:
                    obj = spec["model"](**vals)
                    db.add(obj)
                    inserted_rows.append(obj)
                    if len(inserted_rows) >= 1000:
                        db.flush()
                        db.add_all([models.ImportChange(
                            batch_id=batch_id, table_name=spec["table"],
                            record_pk=o.id, change_type="insert") for o in inserted_rows])
                        db.flush()
                        inserted_rows = []
            else:
                counts["unchanged"] += 1
                if len(duplicate_examples) < 5:
                    duplicate_examples.append({"row": row_no, "key": fp[:120]})
            continue

        if key in seen_keys:
            counts["duplicate"] += 1
            if len(duplicate_examples) < 5:
                duplicate_examples.append({"row": row_no, "key": str(key)})
            if issue_count < MAX_ISSUES:
                issues.append({"row": row_no, "field": "duplicate",
                               "severity": "warning",
                               "message": "Same key appears earlier in this file — skipped to avoid double counting"})
                issue_count += 1
            continue
        seen_keys[key] = vals
        counts["valid"] += 1

        existing = index.get(key)
        if existing is None:
            counts["new"] += 1
            if apply:
                obj = spec["model"](**vals)
                db.add(obj)
                inserted_rows.append(obj)
                if len(inserted_rows) >= 1000:
                    db.flush()
                    db.add_all([models.ImportChange(
                        batch_id=batch_id, table_name=spec["table"],
                        record_pk=o.id, change_type="insert") for o in inserted_rows])
                    db.flush()
                    for o in inserted_rows:
                        _snapshot(o.id, {f: getattr(o, f, None)
                                         for f in change_tracking.TRACKED_FIELDS})
                    inserted_rows = []
            continue

        pk, old_vals = existing
        changed: Dict[str, Any] = {}
        for field_name in spec["compare"]:
            new_val = vals.get(field_name)
            old_val = old_vals.get(field_name)
            # Never blank out a reliable stored value with a missing one.
            if new_val in (None, ""):
                continue
            if isinstance(new_val, float) and isinstance(old_val, (int, float)):
                if abs(new_val - float(old_val)) < 0.01:
                    continue
            if isinstance(new_val, str) and isinstance(old_val, str) and new_val == old_val:
                continue
            if new_val == old_val:
                continue
            # Dates: compare on the day only (stored form may differ).
            if field_name.endswith("_date") and _parse_stored_date(new_val) == _parse_stored_date(old_val):
                continue
            changed[field_name] = new_val

        if not changed:
            counts["unchanged"] += 1
            _snapshot(pk, old_vals)
            continue

        counts["update"] += 1
        changed_fields_total += len(changed)
        if apply:
            db.execute(
                spec["model"].__table__.update()
                .where(spec["model"].id == pk)
                .values(**changed)
            )
            if batch_id is not None:
                db.add(models.ImportChange(
                    batch_id=batch_id, table_name=spec["table"], record_pk=pk,
                    change_type="update",
                    before_json=json.dumps({f: old_vals.get(f) for f in changed})))
            _snapshot(pk, {**old_vals, **changed})

    if apply and inserted_rows:
        db.flush()
        db.add_all([models.ImportChange(
            batch_id=batch_id, table_name=spec["table"],
            record_pk=o.id, change_type="insert") for o in inserted_rows])
        db.flush()
        for o in inserted_rows:
            _snapshot(o.id, {f: getattr(o, f, None) for f in change_tracking.TRACKED_FIELDS})

    if linkage_checked and missing_linkage:
        sample = sorted(missing_linkage)[:10]
        for s in sample:
            if issue_count < MAX_ISSUES:
                parts = s.split("|")
                issues.append({"row": None, "field": "work_description", "severity": "warning",
                               "message": "No recommended work matches this work/constituency/state — verify the expenditure link: "
                                          + " / ".join(parts[:3])})
                issue_count += 1

    _flush_snapshots()

    return {
        "counts": counts,
        "issues": issues,
        "issue_count": issue_count,
        "duplicates": duplicate_examples,
        "houses": sorted(info["house_values"]),
        "affected_mp": sorted([list(x) for x in affected_mp]),
        "changed_fields": changed_fields_total,
        "linkage_checked": linkage_checked,
        "missing_linkage": len(missing_linkage),
        "schema": mapping,
    }


# ─────────────────────────────────────────────────────────────────────
# Batch lifecycle
# ─────────────────────────────────────────────────────────────────────

def _batch_payload(batch: models.ImportBatch, include_detail: bool = False) -> Dict[str, Any]:
    preview = {}
    details = {}
    try:
        preview = json.loads(batch.preview_json or "{}")
    except (ValueError, TypeError):
        preview = {}
    try:
        details = json.loads(batch.details_json or "{}")
    except (ValueError, TypeError):
        details = {}
    out = {
        "id": batch.id,
        "filename": batch.filename,
        "file_size": batch.file_size or 0,
        "dataset_type": batch.dataset_type,
        "detected_type": batch.detected_type,
        "dataset_label": DATASET_LABELS.get(batch.dataset_type or batch.detected_type or "", "Unknown"),
        "detection_confidence": batch.detection_confidence or 0,
        "detected_columns": json.loads(batch.detected_columns or "{}"),
        "house_coverage": batch.house_coverage,
        "house_override": batch.house_override,
        "uploaded_by": batch.uploaded_by,
        "uploaded_at": batch.uploaded_at,
        "status": batch.status,
        "total_rows": batch.total_rows or 0,
        "valid_rows": batch.valid_rows or 0,
        "new_rows": batch.new_rows or 0,
        "update_rows": batch.update_rows or 0,
        "unchanged_rows": batch.unchanged_rows or 0,
        "duplicate_rows": batch.duplicate_rows or 0,
        "rejected_rows": batch.rejected_rows or 0,
        "error_summary": batch.error_summary,
        "committed_at": batch.committed_at,
        "rolled_back_at": batch.rolled_back_at,
        "dataset_version": batch.dataset_version or 0,
        "preview": preview if include_detail else {
            "issues": (preview.get("issues") or [])[:25],
            "ranked": preview.get("ranked") or [],
            "missing": preview.get("missing") or [],
            "houses": preview.get("houses") or [],
            "duplicates": preview.get("duplicates") or [],
        },
    }
    if include_detail:
        out["details"] = details
    return out


def validate_upload(filename: str, size: int, sample: bytes) -> None:
    """Cheap pre-flight checks shared by the streaming upload endpoint."""
    safe_name = os.path.basename(filename or "upload.csv")
    if not safe_name.lower().endswith(ALLOWED_EXTENSIONS):
        raise ValueError("Only CSV, TSV or TXT files are accepted")
    if size == 0:
        raise ValueError("The uploaded file is empty")
    if size > MAX_FILE_BYTES:
        raise ValueError(f"File exceeds the {MAX_FILE_BYTES // (1024 * 1024)} MB limit")
    try:
        text_sample = sample[:65536].decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("File is not valid UTF-8 text")
    if "\x00" in text_sample:
        raise ValueError("Binary content is not accepted")


batch_payload = _batch_payload


def create_batch(
    db: Session,
    filename: str,
    content: bytes,
    user: models.User,
    dataset_type: Optional[str] = None,
    house_override: Optional[str] = None,
) -> models.ImportBatch:
    """Persist the uploaded bytes + detect/validate, returning a preview batch."""
    validate_upload(filename, len(content), content[:65536])
    _ensure_dir()
    token = uuid.uuid4().hex
    path = _stored_path(token)
    with open(path, "wb") as fh:
        fh.write(content)
    return _register_batch(db, filename, path, len(content), user, dataset_type, house_override)


def create_batch_from_path(
    db: Session,
    filename: str,
    source_path: str,
    size: int,
    user: models.User,
    dataset_type: Optional[str] = None,
    house_override: Optional[str] = None,
) -> models.ImportBatch:
    """Register a file already streamed to disk by the upload endpoint."""
    _ensure_dir()
    token = uuid.uuid4().hex
    path = _stored_path(token)
    os.replace(source_path, path)
    return _register_batch(db, filename, path, size, user, dataset_type, house_override)


def _register_batch(
    db: Session,
    filename: str,
    path: str,
    size: int,
    user: models.User,
    dataset_type: Optional[str],
    house_override: Optional[str],
) -> models.ImportBatch:
    safe_name = os.path.basename(filename or "upload.csv")
    if dataset_type and dataset_type not in SPECS:
        raise ValueError(f"Unknown dataset type '{dataset_type}'")
    if house_override and house_override not in HOUSES:
        raise ValueError("House must be 'Lok Sabha' or 'Rajya Sabha'")

    headers, delimiter = read_headers(path)
    if not headers:
        os.remove(path)
        raise ValueError("No readable header row found")
    detection = detect_dataset(headers)

    batch = models.ImportBatch(
        token=os.path.splitext(os.path.basename(path))[0],
        filename=safe_name,
        stored_path=path,
        file_size=size,
        dataset_type=dataset_type or detection["dataset_type"],
        detected_type=detection["candidate_type"],
        detection_confidence=detection["confidence"],
        detected_columns=json.dumps(detection["columns"]),
        uploaded_by=(user.name or user.email) if user else None,
        uploaded_by_id=user.id if user else None,
        uploaded_at=_now(),
        status="preview",
        dataset_version=0,
    )
    db.add(batch)
    db.commit()
    db.refresh(batch)
    preview_batch(db, batch, dataset_type=dataset_type, house_override=house_override)
    return batch


def preview_batch(
    db: Session,
    batch: models.ImportBatch,
    dataset_type: Optional[str] = None,
    house_override: Optional[str] = None,
) -> models.ImportBatch:
    """(Re)validate a staged file and store the preview summary."""
    if dataset_type:
        if dataset_type not in SPECS:
            raise ValueError("Unknown dataset type")
        batch.dataset_type = dataset_type
    if house_override:
        if house_override not in HOUSES:
            raise ValueError("House must be 'Lok Sabha' or 'Rajya Sabha'")
        batch.house_override = house_override
    house_override = house_override or batch.house_override

    headers, delimiter = read_headers(batch.stored_path)
    detection = detect_dataset(headers)
    mapping = detection["columns"]
    spec = SPECS.get(batch.dataset_type or "")

    if spec is None:
        batch.status = "needs_type"
        batch.detected_type = detection["candidate_type"]
        batch.detection_confidence = detection["confidence"]
        batch.detected_columns = json.dumps(detection["columns"])
        batch.error_summary = "Dataset type could not be determined — select the dataset type manually."
        batch.preview_json = json.dumps({
            "issues": [], "ranked": detection["ranked"],
            "missing": detection["missing"], "houses": [], "duplicates": [],
        })
        db.commit()
        return batch

    if not dataset_type:
        # Keep the detected mapping for the detected type (may be None).
        mapping = _match_fields(headers, spec) if spec else mapping

    required_missing = [c for c in spec["required"] if c not in mapping]
    if required_missing:
        batch.status = "invalid"
        batch.error_summary = "Missing required columns: " + ", ".join(required_missing)
        batch.preview_json = json.dumps({
            "issues": [{"row": 1, "field": c, "severity": "error",
                        "message": f"Required column missing: {c}"} for c in required_missing],
            "ranked": detection["ranked"],
            "missing": required_missing,
            "houses": [],
            "duplicates": [],
        })
        batch.total_rows = 0
        db.commit()
        return batch

    batch.detected_columns = json.dumps(mapping)
    try:
        result = _scan(db, batch.stored_path, delimiter, spec, mapping, None,
                       house_override, apply=False)
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("preview failed for batch %s", batch.id)
        batch.status = "invalid"
        batch.error_summary = "Validation failed while reading the file."
        db.commit()
        return batch

    counts = result["counts"]
    houses = result["houses"]
    coverage = houses[0] if len(houses) == 1 else ("Mixed" if houses else "Unknown")
    if not houses and "house" in spec["fields"]:
        coverage = "Unknown"
    batch.status = "preview"
    batch.total_rows = counts["total"]
    batch.valid_rows = counts["valid"]
    batch.new_rows = counts["new"]
    batch.update_rows = counts["update"]
    batch.unchanged_rows = counts["unchanged"]
    batch.duplicate_rows = counts["duplicate"]
    batch.rejected_rows = counts["rejected"]
    batch.house_coverage = coverage
    batch.detection_confidence = detection["confidence"]
    batch.preview_json = json.dumps({
        "issues": result["issues"],
        "issue_count": result["issue_count"],
        "duplicates": result["duplicates"],
        "houses": houses,
        "ranked": detection["ranked"],
        "missing": [],
        "linkage_checked": result["linkage_checked"],
        "missing_linkage": result["missing_linkage"],
        "changed_fields": result["changed_fields"],
    })
    if len(houses) > 1:
        try:
            payload = json.loads(batch.preview_json or "{}")
        except (ValueError, TypeError):
            payload = {}
        payload.setdefault("issues", []).insert(0, {
            "row": None, "field": "house", "severity": "warning",
            "message": "This file mixes houses (%s); rows are imported with their own House value."
                       % ", ".join(houses),
        })
        batch.preview_json = json.dumps(payload)

    if counts["total"] == 0:
        batch.status = "invalid"
        batch.error_summary = "The file contains no data rows."
    elif counts["new"] == 0 and counts["update"] == 0:
        batch.error_summary = "Nothing to change — every valid row already matches the database."
    elif coverage == "Unknown":
        batch.error_summary = "House could not be determined — select Lok Sabha or Rajya Sabha before importing."
    db.commit()
    return batch


def commit_batch(
    db: Session,
    batch: models.ImportBatch,
    user: models.User,
    dataset_type: Optional[str] = None,
    house_override: Optional[str] = None,
) -> Dict[str, Any]:
    """Apply a previewed batch in ONE transaction, then refresh derived data."""
    if batch.status not in ("preview", "failed", "needs_type", "partial"):
        raise ValueError(f"Batch {batch.id} is in state '{batch.status}' and cannot be imported")
    if dataset_type and dataset_type != batch.dataset_type:
        if dataset_type not in SPECS:
            raise ValueError("Unknown dataset type")
        batch.dataset_type = dataset_type
        db.commit()
        preview_batch(db, batch, dataset_type=dataset_type, house_override=house_override)
        db.refresh(batch)

    spec = SPECS.get(batch.dataset_type or "")
    if spec is None:
        raise ValueError("Dataset type must be selected before importing")
    if batch.status == "invalid":
        raise ValueError(batch.error_summary or "This file failed validation — fix it and re-upload")
    # A file with valid rows is importable even when every value matches what is
    # already stored: the import still has to be recorded so "missing from this
    # import" can be determined against the previous dataset.
    if (batch.valid_rows or 0) == 0:
        raise ValueError(batch.error_summary or "Nothing to import — the file has no valid rows")
    house_override = house_override or batch.house_override
    headers, delimiter = read_headers(batch.stored_path)
    mapping = _match_fields(headers, spec)
    missing = [c for c in spec["required"] if c not in mapping]
    if missing:
        raise ValueError("Missing required columns: " + ", ".join(missing))

    if not _WRITE_LOCK.acquire(blocking=False):
        raise RuntimeError("Another import is currently running — try again shortly")
    try:
        busy = db.query(models.ImportBatch).filter(
            models.ImportBatch.status == "importing",
            models.ImportBatch.id != batch.id,
        ).count()
        if busy:
            raise RuntimeError("Another import is currently running — try again shortly")

        batch.status = "importing"
        batch.error_summary = None
        db.commit()
        preview: Optional[Dict[str, Any]] = None

        # Fresh session so the data transaction is independent of the HTTP one.
        work = SessionLocal()
        try:
            preview = _scan(work, batch.stored_path, delimiter, spec, mapping,
                            batch.id, house_override, apply=True)
            counts = preview["counts"]
            if counts["valid"] == 0:
                work.rollback()
                batch.status = "preview"
                batch.error_summary = "Nothing to import — the file has no valid rows."
                db.commit()
                return _batch_payload(batch, include_detail=True)

            # Generate change events inside the same transaction: a failure
            # below rolls back the data AND its change history together.
            import change_tracking

            change_summary = change_tracking.record_changes_for_batch(work, batch, spec, preview)

            affected = preview.get("affected_mp") or []
            version = (work.query(func.max(models.ImportBatch.dataset_version)).scalar() or 0) + 1
            details = {
                "counts": counts,
                "houses": preview["houses"],
                "affected_mps": len(affected),
                "affected_mps_list": affected,
                "changed_fields": preview["changed_fields"],
                "missing_linkage": preview["missing_linkage"],
                "changes": change_summary,
                "imported_by": (user.name or user.email) if user else None,
                "refresh": {"status": "pending"},
            }
            batch.total_rows = counts["total"]
            batch.valid_rows = counts["valid"]
            batch.new_rows = counts["new"]
            batch.update_rows = counts["update"]
            batch.unchanged_rows = counts["unchanged"]
            batch.duplicate_rows = counts["duplicate"]
            batch.rejected_rows = counts["rejected"]
            batch.house_coverage = (
                preview["houses"][0] if len(preview["houses"]) == 1
                else ("Mixed" if preview["houses"] else batch.house_coverage)
            )
            batch.details_json = json.dumps(details)
            batch.dataset_version = int(version)
            batch.committed_at = _now()
            batch.status = "refreshing"
            work.commit()
        except Exception:
            work.rollback()
            raise
        finally:
            work.close()
    finally:
        _WRITE_LOCK.release()

    db.commit()
    db.refresh(batch)
    _spawn_refresh(batch.id, (preview or {}).get("affected_mp") or [])
    return _batch_payload(batch, include_detail=True)


def _spawn_refresh(batch_id: int, affected_mp: Optional[List[List[str]]] = None) -> None:
    thread = threading.Thread(target=refresh_batch, args=(batch_id, affected_mp), daemon=True)
    thread.start()


def refresh_batch(batch_id: int, affected_mp: Optional[List[List[str]]] = None) -> None:
    """Recompute derived analytics after an import (single writer)."""
    if not _REFRESH_LOCK.acquire(blocking=False):
        return
    db = SessionLocal()
    try:
        batch = db.query(models.ImportBatch).filter(models.ImportBatch.id == batch_id).first()
        if not batch:
            return
        details = {}
        try:
            details = json.loads(batch.details_json or "{}")
        except (ValueError, TypeError):
            details = {}
        steps: Dict[str, Any] = {}
        ok = True
        if affected_mp is None:
            affected_mp = [tuple(x) for x in (details.get("affected_mps_list") or [])]
        try:
            from rec_info import rebuild as rebuild_rec_info
            steps["project_rec_info"] = rebuild_rec_info(db, force=True)
        except Exception as exc:
            logger.exception("rec_info rebuild failed for batch %s", batch_id)
            steps["project_rec_info"] = {"error": str(exc)}
        try:
            steps["mp_summaries"] = _refresh_mp_summaries(db, affected_mp)
        except Exception as exc:
            logger.exception("mp summary refresh failed for batch %s", batch_id)
            steps["mp_summaries"] = {"error": str(exc)}
        try:
            steps["risk_scores"] = _regenerate_risk_scores(db)
        except Exception as exc:
            logger.exception("risk regeneration failed for batch %s", batch_id)
            steps["risk_scores"] = {"error": str(exc)}
            ok = False
        try:
            from sync import invalidate_freshness_cache
            invalidate_freshness_cache()
            import main as main_module
            main_module.clear_intel_cache()
            steps["caches"] = "cleared"
        except Exception as exc:
            steps["caches"] = f"warning: {exc}"

        refresh_state = "success" if ok else "partial"
        details["refresh"] = {"status": refresh_state, "steps": steps, "at": _now()}
        # Re-fetch: the scoring pass calls expunge_all(), which detaches the
        # batch instance loaded at the top of this worker.
        batch = db.query(models.ImportBatch).filter(
            models.ImportBatch.id == batch_id).first()
        if batch is None:
            return
        batch.details_json = json.dumps(details)
        # Never resurrect a rolled-back batch: its status is terminal.
        if batch.status != "rolled_back":
            batch.status = refresh_state
        db.commit()
    except Exception:
        logger.exception("refresh worker crashed for batch %s", batch_id)
    finally:
        db.close()
        _REFRESH_LOCK.release()


def _refresh_mp_summaries(db: Session, affected: List[Tuple[str, str]]) -> Dict[str, int]:
    """Recompute ledger-derived MP metrics for the MPs a file touched.

    Only fields the ledgers can prove are rewritten (spend, transaction
    counts, completed/recommended work counts, utilization/completion rate
    derived from the stored allocated amount). Allocated amounts are left
    exactly as recorded — no invented numbers.
    """
    updated = 0
    affected = [tuple(x) for x in (affected or []) if x and x[0] and x[1]]
    for house, name_key in affected:
        exp = db.execute(text(
            "SELECT COUNT(*), COALESCE(SUM(expenditure_amount),0), "
            "SUM(CASE WHEN lower(payment_status) LIKE '%success%' THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN lower(payment_status) LIKE '%pending%' OR lower(payment_status) LIKE '%progress%' THEN 1 ELSE 0 END) "
            "FROM expenditures WHERE house=:h AND normkey(mp_name)=:n"
        ), {"h": house, "n": name_key}).first()
        comp = db.execute(text(
            "SELECT COUNT(*) FROM completed_works WHERE house=:h AND normkey(mp_name)=:n"
        ), {"h": house, "n": name_key}).scalar() or 0
        rec = db.execute(text(
            "SELECT COUNT(*) FROM recommended_works WHERE house=:h AND normkey(mp_name)=:n"
        ), {"h": house, "n": name_key}).scalar() or 0
        row = db.execute(text(
            "SELECT id, allocated_amount FROM mp_summaries WHERE house=:h AND normkey(mp_name)=:n"
        ), {"h": house, "n": name_key}).first()
        if not row:
            continue
        allocated = float(row[1] or 0)
        spend = float(exp[1] or 0)
        utilization = round(spend / allocated * 100, 2) if allocated else 0.0
        completion_rate = round(comp / rec * 100, 2) if rec else 0.0
        db.execute(text(
            "UPDATE mp_summaries SET total_expenditure=:sp, transaction_count=:tc, "
            "successful_payments=:succ, pending_payments=:pend, completed_works=:comp, "
            "recommended_works=:rec, utilization_percentage=:util, "
            "completion_rate_percentage=:cr WHERE id=:id"
        ), {"sp": spend, "tc": int(exp[0] or 0), "succ": int(exp[2] or 0),
            "pend": int(exp[3] or 0), "comp": int(comp), "rec": int(rec),
            "util": utilization, "cr": completion_rate, "id": row[0]})
        updated += 1
    db.commit()
    return {"updated": updated}


def _regenerate_risk_scores(db: Session) -> Dict[str, Any]:
    """Re-score every project with the EXISTING model + rules.

    Batched reads keep the ORM identity map small; the delete + inserts share
    one transaction so no dashboard ever sees a half-refreshed snapshot. The
    Isolation Forest is loaded once from disk (never retrained here).
    """
    import numpy as np

    from ml import predictor as predictor_mod
    from ml.predictor import predict_risk
    from models import ProjectRecInfo, Project, RiskScore

    amounts = dict(
        db.query(ProjectRecInfo.project_id, ProjectRecInfo.linked_expenditure)
        .filter(ProjectRecInfo.linked_expenditure > 0)
        .all()
    )

    # Load the existing bundle ONCE (never retrain) so the ML part can run as
    # one matrix prediction per chunk instead of one sklearn call per row.
    predictor_mod._ensure_model()
    model = predictor_mod.model
    has_ml = bool(model is not None and predictor_mod.features)

    db.query(RiskScore).delete()
    db.flush()

    last_id = 0
    total = 0
    while True:
        projects = (
            db.query(Project).filter(Project.id > last_id)
            .order_by(Project.id).limit(2000).all()
        )
        if not projects:
            break
        for p in projects:
            amt = amounts.get(int(p.id))
            if amt is not None:
                p._authoritative_expenditure = float(amt)

        preds = scores = None
        if has_ml:
            try:
                X = np.vstack([predictor_mod.create_project_features(p) for p in projects])
                preds = model.predict(X)
                scores = model.decision_function(X)
            except Exception:
                logger.exception("batch ML scoring failed; falling back to per-row")
                preds = scores = None

        rows = []
        for idx, p in enumerate(projects):
            ml_override = None
            if preds is not None:
                ml_override = (bool(preds[idx] == -1), float(scores[idx]))
            risk = predict_risk(p, batch_mode=True, ml_override=ml_override)
            rows.append({
                "project_id": p.id,
                "ml_anomaly": risk["ml_anomaly"],
                "ml_score": risk["ml_score"],
                "risk_score": risk["risk_score"],
                "risk_level": risk["risk_level"],
                "reasons": ", ".join(risk["reasons"]),
            })
        db.bulk_insert_mappings(RiskScore, rows)
        last_id = projects[-1].id
        total += len(rows)
        db.expunge_all()
    db.commit()
    return {"scored": total}


def rollback_batch(db: Session, batch: models.ImportBatch, user: models.User) -> Dict[str, Any]:
    """Undo a committed batch using its recorded before-state."""
    if batch.status not in ("success", "partial"):
        raise ValueError("Only successfully imported batches can be rolled back")
    if not _WRITE_LOCK.acquire(blocking=False):
        raise RuntimeError("Another import is currently running — try again shortly")
    try:
        # Drop this batch's project snapshots + change events: they described
        # data that is about to be undone, so they must not survive it.
        import change_tracking

        change_tracking.drop_batch_history(db, batch.id)

        changes = db.query(models.ImportChange).filter(
            models.ImportChange.batch_id == batch.id).all()
        if not changes:
            raise ValueError("No rollback information is available for this batch")
        by_table: Dict[str, List[models.ImportChange]] = {}
        for c in changes:
            by_table.setdefault(c.table_name, []).append(c)

        # Delete inserted rows in reverse creation order.
        for table, rows in by_table.items():
            spec = next((s for s in SPECS.values() if s["table"] == table), None)
            if spec is None:
                continue
            model = spec["model"]
            inserts = sorted([r for r in rows if r.change_type == "insert"],
                             key=lambda r: r.record_pk or 0, reverse=True)
            for i in range(0, len(inserts), 500):
                ids = [r.record_pk for r in inserts[i:i + 500] if r.record_pk]
                if ids:
                    db.execute(model.__table__.delete().where(model.id.in_(ids)))
            # Restore prior values for updated rows.
            for r in (r for r in rows if r.change_type == "update"):
                try:
                    before = json.loads(r.before_json or "{}")
                except (ValueError, TypeError):
                    before = {}
                if not before or not r.record_pk:
                    continue
                db.execute(model.__table__.update()
                           .where(model.id == r.record_pk).values(**before))
        db.query(models.ImportChange).filter(
            models.ImportChange.batch_id == batch.id).delete()
        batch.status = "rolled_back"
        batch.rolled_back_at = _now()
        details = {}
        try:
            details = json.loads(batch.details_json or "{}")
        except (ValueError, TypeError):
            details = {}
        details["rollback"] = {"by": (user.name or user.email) if user else None,
                               "at": batch.rolled_back_at,
                               "rows_restored": len([c for c in changes if c.change_type == "update"]),
                               "rows_deleted": len([c for c in changes if c.change_type == "insert"])}
        batch.details_json = json.dumps(details)
        db.commit()
        _spawn_refresh(batch.id, [])
        return _batch_payload(batch, include_detail=True)
    except Exception:
        db.rollback()
        raise
    finally:
        _WRITE_LOCK.release()


def discard_batch(db: Session, batch: models.ImportBatch) -> None:
    # Only uncommitted batches can be discarded. Deleting a committed batch
    # record would erase its history/rollback trail while leaving the data.
    if batch.status not in ("preview", "needs_type", "invalid", "failed"):
        raise ValueError(f"Batch is {batch.status}; only uncommitted batches can be discarded")
    try:
        if batch.stored_path and os.path.exists(batch.stored_path):
            os.remove(batch.stored_path)
    except OSError:
        pass
    db.delete(batch)
    db.commit()


# ─────────────────────────────────────────────────────────────────────
# Reporting
# ─────────────────────────────────────────────────────────────────────

def _count_by_house(db: Session, table: str) -> Dict[str, int]:
    rows = db.execute(text(f"SELECT COALESCE(house,'Unattributed'), COUNT(*) FROM {table} GROUP BY 1"))
    return {str(h): int(n) for h, n in rows}


def overview(db: Session) -> Dict[str, Any]:
    datasets = [
        ("recommended_works", "Recommended Works", models.RecommendedWork),
        ("completed_works", "Completed Works", models.CompletedWork),
        ("expenditures", "Expenditure Ledger", models.Expenditure),
        ("mp_summaries", "MP Summaries", models.MPSummary),
        ("projects", "Project Records", models.Project),
    ]
    counts = []
    for key, label, model in datasets:
        total = db.query(func.count(model.id)).scalar() or 0
        houses = _count_by_house(db, model.__tablename__) if key != "projects" else \
            _count_by_house(db, "projects")
        counts.append({"dataset_type": key, "label": label, "records": int(total), "houses": houses})

    last = (
        db.query(models.ImportBatch)
        .filter(models.ImportBatch.status.in_(("success", "partial")))
        .order_by(models.ImportBatch.id.desc()).first()
    )
    pending = db.query(func.count(models.ImportBatch.id)).filter(
        models.ImportBatch.status.in_(("preview", "needs_type", "invalid"))).scalar() or 0
    version = db.query(func.max(models.ImportBatch.dataset_version)).scalar() or 0
    last_sync = db.execute(text(
        "SELECT source_updated_at, synced_at FROM sync_metadata "
        "WHERE source_updated_at IS NOT NULL ORDER BY id DESC LIMIT 1")).first()

    return {
        "datasets": counts,
        "last_import": _batch_payload(last, include_detail=True) if last else None,
        "last_success_at": last.committed_at if last else None,
        "snapshot_date": (last_sync[0] if last_sync else (last.committed_at[:10] if last and last.committed_at else None)),
        "pending_validation_batches": int(pending),
        "dataset_version": int(version),
        "risk_scores": int(db.query(func.count(models.RiskScore.id)).scalar() or 0),
        "supported_types": [{"dataset_type": k, "label": v["label"], "required": list(v["required"]),
                             "mode": v["mode"]} for k, v in SPECS.items()],
    }


def list_batches(db: Session, limit: int = 50) -> List[Dict[str, Any]]:
    rows = (db.query(models.ImportBatch)
            .order_by(models.ImportBatch.id.desc()).limit(max(1, min(limit, 200))).all())
    return [_batch_payload(r) for r in rows]


def get_batch(db: Session, batch_id: int) -> Optional[Dict[str, Any]]:
    row = db.query(models.ImportBatch).filter(models.ImportBatch.id == batch_id).first()
    return _batch_payload(row, include_detail=True) if row else None
