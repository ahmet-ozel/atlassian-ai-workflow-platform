"""Firecrawl Egress Allowlist API endpoints.

CRUD operations for managing the Firecrawl domain allowlist.
Supports hot-reload without container restart.

Requirements: 12.1, 12.2, 12.3, 12.4, 12.5, 12.6
"""
from __future__ import annotations
import logging
import re
from datetime import datetime, timezone
from typing import Any
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, field_validator

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/firecrawl/allowlist", tags=["firecrawl"])

_DNS_PATTERN = re.compile(
    r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.[A-Za-z0-9-]{1,63})*\.[A-Za-z]{2,}$"
)
MAX_DOMAIN_LENGTH = 253

# In-memory store (production would use PostgreSQL firecrawl_allowlist table)
_allowlist: list[dict[str, Any]] = []

class DomainAddRequest(BaseModel):
    domain: str

    @field_validator("domain")
    @classmethod
    def validate_domain(cls, v: str) -> str:
        v = v.strip().lower()
        if len(v) > MAX_DOMAIN_LENGTH:
            raise ValueError(f"Domain exceeds {MAX_DOMAIN_LENGTH} characters")
        if not _DNS_PATTERN.match(v):
            raise ValueError(f"Invalid DNS domain format: {v}")
        return v

class DomainResponse(BaseModel):
    id: str
    domain: str
    added_by: str
    added_at: str

@router.get("/")
async def list_allowlist(page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=50)):
    start = (page - 1) * page_size
    end = start + page_size
    return {"domains": _allowlist[start:end], "total": len(_allowlist), "page": page}

@router.post("/", status_code=201)
async def add_domain(request: DomainAddRequest, admin_id: str = "system"):
    domain = request.domain
    if any(d["domain"] == domain for d in _allowlist):
        raise HTTPException(status_code=409, detail=f"Domain '{domain}' already exists")

    entry = {
        "id": f"fc-{len(_allowlist)+1}",
        "domain": domain,
        "added_by": admin_id,
        "added_at": datetime.now(timezone.utc).isoformat(),
    }
    _allowlist.append(entry)
    _logger.info("Added domain to allowlist: %s by %s", domain, admin_id)
    # TODO: Call hot-reload endpoint for Firecrawl service
    return entry

@router.delete("/{domain_id}")
async def remove_domain(domain_id: str):
    for i, entry in enumerate(_allowlist):
        if entry["id"] == domain_id:
            removed = _allowlist.pop(i)
            _logger.info("Removed domain from allowlist: %s", removed["domain"])
            return {"removed": removed}
    raise HTTPException(status_code=404, detail=f"Domain ID '{domain_id}' not found")
