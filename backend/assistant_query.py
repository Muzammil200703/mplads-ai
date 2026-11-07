"""
Natural-language query endpoints for the MPLADS AI assistant
===========================================================

These are the query-specific entry points. The conversational path stays
`POST /assistant/ask` (this router also feeds it), so the assistant remains
one assistant — this module simply exposes the same engine explicitly:

  POST /assistant/query                     ask a data question
  POST /assistant/query/validate            validate a statement WITHOUT running it
  GET  /assistant/query/{query_id}/results  full stored result for one query

Every endpoint reuses the existing JWT session (`auth.get_current_user`), the
existing role ladder, and the read-only executor. Nothing here can write.
"""

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import auth
import natural_language_query_service as nlq

# Mounted under the existing /assistant router (see assistant.py), so the
# final paths are /assistant/query, /assistant/query/validate and
# /assistant/query/{query_id}/results — no duplicated /assistant prefix.
router = APIRouter(prefix="/query", tags=["AI Assistant · Query"])


class QueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=500)
    session_id: Optional[str] = Field(None, max_length=64)
    page: Optional[str] = Field(None, max_length=60)
    house: Optional[str] = Field(None, max_length=20)


class ValidateRequest(BaseModel):
    sql: str = Field(min_length=1, max_length=4000)


@router.post("")
def run_query(
    payload: QueryRequest,
    user=Depends(auth.get_current_user),
) -> Dict[str, Any]:
    """Answer one natural-language question from real database records."""
    result = nlq.handle(
        payload.question,
        user=user,
        house=payload.house,
        session_id=payload.session_id,
    )
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "That is not a database question. Use /assistant/ask for MPLADS "
                "guidance, navigation and role-specific answers."
            ),
        )
    meta = result.get("meta") or {}
    return {
        "answer": result.get("answer"),
        "intent": meta.get("intent"),
        "table": meta.get("table"),
        "findings": meta.get("findings") or [],
        "limitations": meta.get("limitations") or [],
        "chart": meta.get("chart"),
        "query": meta.get("query"),
    }


@router.post("/validate")
def validate_query(
    payload: ValidateRequest,
    user=Depends(auth.require_role("analyst")),
) -> Dict[str, Any]:
    """Run ONLY the validator — never executes the statement."""
    return nlq.validate_sql(payload.sql, user)


@router.get("/{query_id}/results")
def query_results(
    query_id: str,
    user=Depends(auth.get_current_user),
) -> Dict[str, Any]:
    """Return the stored rows for a query the caller already ran."""
    data = nlq.results_for(query_id, user)
    if not data:
        raise HTTPException(status_code=404, detail="Query result not found or expired")
    return data
