"""Evidence model and store (AGENTS.md 8.1).

New evidence starts ``UNCONFIRMED``. The resolver/fetcher never mark evidence
``OFFICIAL``. Identity derives from canonical URL plus content fingerprint.

Provenance (FIX-001): created only by SafeFetcher; imported/constructed
evidence carries no provenance and cannot become OFFICIAL without it.
"""

from __future__ import annotations

import enum
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..discovery.models import _require_aware_iso


class Officiality(str, enum.Enum):
    """Officiality of an evidence item (AGENTS.md 8.1)."""

    OFFICIAL = "OFFICIAL"
    LIKELY_OFFICIAL = "LIKELY_OFFICIAL"
    UNCONFIRMED = "UNCONFIRMED"
    THIRD_PARTY = "THIRD_PARTY"
    REJECTED = "REJECTED"


class EvidenceProvenance(BaseModel):
    """Provenance metadata created only by SafeFetcher (FIX-001).

    Only evidence with ``retrieved_from_origin=true`` provenance is eligible
    for OFFICIAL. Imported/constructed evidence must NOT carry provenance.
    """

    model_config = ConfigDict(extra="forbid")

    retrieval_method: str = "safe_fetch"
    original_url: str
    final_url: str
    redirect_chain: List[str] = Field(default_factory=list)
    http_status: int = 200
    content_sha256: str = ""
    retrieved_from_origin: bool = True
    retrieved_at: datetime


class Evidence(BaseModel):
    """One piece of resolved evidence (AGENTS.md 8.1)."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str = ""
    candidate_id: Optional[str] = None
    provider_id: Optional[str] = None
    url: str
    normalized_domain: Optional[str] = None
    source_type: str = "page"
    officiality: Officiality = Officiality.UNCONFIRMED
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    title: Optional[str] = None
    claim: Optional[str] = None
    supported_fields: List[str] = Field(default_factory=list)
    published_at: Optional[datetime] = None
    effective_at: Optional[datetime] = None
    validation_notes: List[str] = Field(default_factory=list)
    content_fingerprint: str = ""
    content_excerpt: Optional[str] = None
    provenance: Optional[EvidenceProvenance] = None

    @field_validator("evidence_id")
    @classmethod
    def _id(cls, v: str) -> str:
        if not v:
            return ""
        if not re.match(r"^[a-z0-9][a-z0-9._-]*$", v):
            raise ValueError("evidence_id must be a lowercase URL-safe string")
        return v

    @model_validator(mode="after")
    def _fill_derived(self) -> "Evidence":
        """Derive canonical url, domain, and content fingerprint at construction."""
        if not self.url:
            return self
        canonical = canonicalize_evidence_url(self.url)
        fingerprint = content_fingerprint(canonical, self.content_excerpt or "")
        domain = normalize_domain(canonical)
        self.url = canonical
        self.normalized_domain = self.normalized_domain or domain
        self.content_fingerprint = self.content_fingerprint or fingerprint
        return self

    @field_validator("retrieved_at", "published_at", "effective_at")
    @classmethod
    def _times(cls, v: Optional[datetime]) -> Optional[datetime]:
        if v is not None:
            return _require_aware_iso(v, "timestamp")
        return v

    def finalize(self) -> "Evidence":
        """Fill derived identity fields (url, domain, fingerprint)."""
        canonical = canonicalize_evidence_url(self.url)
        fingerprint = content_fingerprint(canonical, self.content_excerpt or "")
        domain = normalize_domain(canonical)
        return self.model_copy(
            update={
                "url": canonical,
                "normalized_domain": domain,
                "content_fingerprint": fingerprint,
            }
        )

    @classmethod
    def from_fetch(
        cls,
        fetch_result: Any,
        *,
        provider_id: Optional[str] = None,
        source_type: str = "page",
        claim: Optional[str] = None,
        content_excerpt: Optional[str] = None,
        retrieved_at: Optional[datetime] = None,
        candidate_id: Optional[str] = None,
        title: Optional[str] = None,
    ) -> "Evidence":
        """Build evidence from a SafeFetcher FetchResult, populating provenance.

        Only this factory creates Evidence with provenance. Callers must not
        manually set ``provenance``; the validator checks ``retrieved_from_origin``
        before granting OFFICIAL.
        """
        sha256 = hashlib.sha256(fetch_result.body).hexdigest()
        provenance = EvidenceProvenance(
            retrieval_method="safe_fetch",
            original_url=fetch_result.original_url,
            final_url=fetch_result.final_url,
            redirect_chain=list(fetch_result.redirect_chain),
            http_status=fetch_result.status,
            content_sha256=sha256,
            retrieved_from_origin=True,
            retrieved_at=retrieved_at or fetch_result.retrieved_at or datetime.now(timezone.utc),
        )
        return cls(
            evidence_id="",
            candidate_id=candidate_id or provider_id,
            provider_id=provider_id or candidate_id,
            url=fetch_result.final_url or fetch_result.original_url,
            source_type=source_type,
            officiality=Officiality.UNCONFIRMED,
            retrieved_at=provenance.retrieved_at,
            title=title or fetch_result.final_url or fetch_result.original_url,
            claim=claim or "",
            content_excerpt=content_excerpt or "",
            provenance=provenance,
        )


def normalize_domain(url: str) -> Optional[str]:
    try:
        host = urlparse(url).hostname
    except ValueError:
        return None
    if not host:
        return None
    host = host.lower().rstrip(".")
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def canonicalize_evidence_url(url: str) -> str:
    """Normalize scheme/host/port/path for identity (AGENTS.md 8.2 + 4.2)."""
    stripped = url.strip()
    try:
        parsed = urlparse(stripped)
    except ValueError:
        return stripped
    scheme = (parsed.scheme or "https").lower()
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return stripped
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    port = parsed.port
    if port in (80, 443):
        port = None
    netloc = host
    if port is not None:
        netloc = f"{host}:{port}"
    path = parsed.path or "/"
    if not path.startswith("/"):
        path = "/" + path
    # strip trailing slash except root
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    query = ("?" + parsed.query) if parsed.query else ""
    return f"{scheme}://{netloc}{path}{query}"


def content_fingerprint(canonical_url: str, excerpt: str) -> str:
    material = f"{canonical_url}\n{excerpt}".encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:40]


def _new_evidence_id(evidence: Evidence) -> str:
    material = f"{evidence.url}|{evidence.content_fingerprint}"
    return "ev-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


__all__ = [
    "Officiality",
    "Evidence",
    "normalize_domain",
    "canonicalize_evidence_url",
    "content_fingerprint",
    "_new_evidence_id",
]
