"""Evidence model and store (AGENTS.md 8.1).

New evidence starts ``UNCONFIRMED``. The resolver/fetcher never mark evidence
``OFFICIAL``. Identity derives from canonical URL plus content fingerprint.
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
