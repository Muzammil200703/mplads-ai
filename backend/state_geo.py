"""State-name normalisation and geography registry for the India map.

The project catalog and the payment/completion ledgers carry human-entered
state labels. Those labels do not always use the same spelling/casing as the
state-boundary GeoJSON (for example ``Andaman And Nicobar Islands`` vs
``Andaman and Nicobar Islands``, or ``The Dadra And Nagar Haveli And Daman
And Diu``). Matching geometry by display name is therefore brittle.

This module is the single join table between recorded labels and the 36
states/union territories used by the map layer. Every entry carries the
ISO 3166-2 code used by the GeoJSON (``shapeISO``), so the frontend joins
aggregates to boundaries by code — never by a fuzzy name match.

No statistics live here: this is geographic metadata only.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Dict, List, Optional, Tuple


# 36 states and union territories, with the ISO code used by the boundary
# layer. `aliases` cover common alternative spellings found in government
# datasets and older records.
STATE_REGISTRY: List[Dict[str, object]] = [
    {"code": "IN-AN", "name": "Andaman and Nicobar Islands",
     "aliases": ["Andaman & Nicobar Islands", "Andaman and Nicobar", "A&N Islands",
                 "A and N Islands", "Andamans"]},
    {"code": "IN-AP", "name": "Andhra Pradesh", "aliases": []},
    {"code": "IN-AR", "name": "Arunachal Pradesh", "aliases": []},
    {"code": "IN-AS", "name": "Assam", "aliases": []},
    {"code": "IN-BR", "name": "Bihar", "aliases": []},
    {"code": "IN-CH", "name": "Chandigarh", "aliases": []},
    {"code": "IN-CT", "name": "Chhattisgarh", "aliases": ["Chattisgarh"]},
    {"code": "IN-DH", "name": "Dadra and Nagar Haveli and Daman and Diu",
     "aliases": [
         "The Dadra And Nagar Haveli And Daman And Diu",
         "Dadra & Nagar Haveli and Daman & Diu",
         "Dadra and Nagar Haveli", "Daman and Diu",
         "DNH and DD", "DNH & DD",
     ]},
    {"code": "IN-DL", "name": "Delhi",
     "aliases": ["NCT of Delhi", "National Capital Territory of Delhi", "New Delhi"]},
    {"code": "IN-GA", "name": "Goa", "aliases": []},
    {"code": "IN-GJ", "name": "Gujarat", "aliases": []},
    {"code": "IN-HR", "name": "Haryana", "aliases": []},
    {"code": "IN-HP", "name": "Himachal Pradesh", "aliases": []},
    {"code": "IN-JK", "name": "Jammu and Kashmir",
     "aliases": ["Jammu & Kashmir", "J&K", "Jammu Kashmir"]},
    {"code": "IN-JH", "name": "Jharkhand", "aliases": []},
    {"code": "IN-KA", "name": "Karnataka", "aliases": []},
    {"code": "IN-KL", "name": "Kerala", "aliases": []},
    {"code": "IN-LA", "name": "Ladakh", "aliases": []},
    {"code": "IN-LD", "name": "Lakshadweep",
     "aliases": ["Lakshadweep Islands", "Laccadive Islands"]},
    {"code": "IN-MP", "name": "Madhya Pradesh", "aliases": []},
    {"code": "IN-MH", "name": "Maharashtra", "aliases": []},
    {"code": "IN-MN", "name": "Manipur", "aliases": []},
    {"code": "IN-ML", "name": "Meghalaya", "aliases": []},
    {"code": "IN-MZ", "name": "Mizoram", "aliases": []},
    {"code": "IN-NL", "name": "Nagaland", "aliases": []},
    {"code": "IN-OR", "name": "Odisha", "aliases": ["Orissa"]},
    {"code": "IN-PY", "name": "Puducherry", "aliases": ["Pondicherry"]},
    {"code": "IN-PB", "name": "Punjab", "aliases": []},
    {"code": "IN-RJ", "name": "Rajasthan", "aliases": []},
    {"code": "IN-SK", "name": "Sikkim", "aliases": []},
    {"code": "IN-TN", "name": "Tamil Nadu", "aliases": ["Tamilnadu"]},
    {"code": "IN-TG", "name": "Telangana", "aliases": ["Telengana"]},
    {"code": "IN-TR", "name": "Tripura", "aliases": []},
    {"code": "IN-UP", "name": "Uttar Pradesh", "aliases": []},
    {"code": "IN-UT", "name": "Uttarakhand", "aliases": ["Uttaranchal"]},
    {"code": "IN-WB", "name": "West Bengal", "aliases": []},
]

STATE_BY_CODE: Dict[str, Dict[str, object]] = {
    str(entry["code"]): entry for entry in STATE_REGISTRY
}


def _canonical_key(value: object) -> str:
    """Fold a human label to a comparison key.

    Diacritics, punctuation, ``&``, casing and repeated whitespace are
    normalised so the DataMeet labels (``Mahārāshtra``, ``Dādra and ...``)
    and the database labels (``Maharashtra``, ``The Dadra ...``) meet.
    """
    text = "" if value is None else str(value)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    # "The Dadra..." and similar labels start with an article that carries
    # no geographic meaning.
    if text.startswith("the "):
        text = text[4:].strip()
    return text


_ALIAS_TO_CODE: Dict[str, str] = {}
_CODE_KEY_TO_CODE: Dict[str, str] = {}
for _entry in STATE_REGISTRY:
    _code = str(_entry["code"])
    _labels = [str(_entry["name"])] + list(_entry.get("aliases") or [])
    for _label in _labels:
        _ALIAS_TO_CODE[_canonical_key(_label)] = _code
    _CODE_KEY_TO_CODE[_canonical_key(_code)] = _code


def normalize_state_label(raw: object) -> Tuple[Optional[str], str]:
    """Return ``(iso_code, canonical_name)`` for a recorded state label.

    ``(None, display_label)`` is returned when no boundary can be matched.
    A missing/blank label is reported as ``(None, "Unknown")`` — callers
    surface it as unmatched instead of silently dropping the records.
    """
    if raw is None:
        return None, "Unknown"
    text = str(raw).strip()
    if not text:
        return None, "Unknown"

    key = _canonical_key(text)
    if key in _CODE_KEY_TO_CODE:
        code = _CODE_KEY_TO_CODE[key]
        return code, str(STATE_BY_CODE[code]["name"])

    code = _ALIAS_TO_CODE.get(key)
    if code:
        return code, str(STATE_BY_CODE[code]["name"])

    return None, text
