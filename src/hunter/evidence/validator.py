"""Official evidence validator and trust anchors (AGENTS.md 8.2, 8.3; TASK-008).

Only configured anchors produce OFFICIAL evidence. LIKELY_OFFICIAL and
third-party evidence never upgrade. Contradictions resolve by fixed priority
then dates; missing dates, equal-priority conflict, or ambiguity block
confirmation.

Anchor schema (spec-pinned, config/sources.yaml):
    provider_id, domains[], allow_subdomains, github_organizations[],
    github_repositories[], provenance, reviewed_at

Host normalization: lowercase, trailing-dot removal, IDNA ASCII. Labels compare
exactly; lookalikes fail. A subdomain is accepted only when its matching anchor
explicitly allows subdomains.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import yaml

from .models import Evidence, Officiality

# Evidence precedence, highest first (AGENTS.md 8.3).
SOURCE_PRIORITY = ["pricing", "api-docs", "docs", "blog", "github"]

# Deterministic rule identifiers returned with every decision.
RULE_EXACT_DOMAIN = "TA-001"
RULE_ALLOWED_SUBDOMAIN = "TA-002"
RULE_GITHUB_MAPPING = "TA-003"
RULE_SAME_REGISTRABLE_DOMAIN = "TA-004"
RULE_NO_ANCHOR_FOR_PROVIDER = "TA-005"
RULE_NO_MATCH_THIRD_PARTY = "TA-006"
RULE_UNMAPPED_GITHUB = "TA-007"

# Comparing a shortener cannot establish officiality.
URL_SHORTENERS = {
    "bit.ly",
    "t.co",
    "tinyurl.com",
    "goo.gl",
    "ow.ly",
    "is.gd",
    "buff.ly",
    "rebrand.ly",
    "cutt.ly",
    "shorturl.at",
}


class ContradictionError(Exception):
    """Raised when evidence cannot resolve without ambiguity."""


@dataclass(frozen=True)
class TrustAnchor:
    provider_id: str
    domains: List[str] = field(default_factory=list)
    allow_subdomains: bool = False
    github_organizations: List[str] = field(default_factory=list)
    github_repositories: List[str] = field(default_factory=list)
    provenance: str = ""
    reviewed_at: str = ""


@dataclass(frozen=True)
class ValidationDecision:
    """Officiality decision plus rule id and human note (TASK-008 contract)."""

    officiality: Officiality
    rule_id: str
    note: str


def _normalize_host(host: str) -> str:
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return ""
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        return host


def load_trust_anchors(path: Any) -> List[TrustAnchor]:
    """Load trust anchors from config/sources.yaml."""
    with open(path, "r", encoding="utf-8") as fh:
        payload = yaml.safe_load(fh)
    raw = (payload or {}).get("trust_anchors") or []
    anchors: List[TrustAnchor] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        provider_id = item.get("provider_id")
        domains = item.get("domains") or ([item["domain"]] if item.get("domain") else [])
        if not provider_id or not domains:
            raise ValueError("trust anchor requires provider_id and domains[]")
        anchors.append(
            TrustAnchor(
                provider_id=str(provider_id),
                domains=[_normalize_host(str(d)) for d in domains if str(d).strip()],
                allow_subdomains=bool(item.get("allow_subdomains", False)),
                github_organizations=[
                    str(o)
                    for o in (
                        item.get("github_organizations")
                        or item.get("github_orgs")
                        or []
                    )
                ],
                github_repositories=[
                    str(r)
                    for r in (
                        item.get("github_repositories")
                        or item.get("github_repos")
                        or []
                    )
                ],
                provenance=str(item.get("provenance") or ""),
                reviewed_at=str(item.get("reviewed_at") or item.get("review_date") or ""),
            )
        )
    return anchors


def github_repo_from_url(url: str) -> Optional[str]:
    """Return ``owner/repo`` for a github.com URL, else None."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if _normalize_host(parsed.hostname or "") not in ("github.com", "www.github.com"):
        return None
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return None


def match_anchor(anchor: TrustAnchor, host: str, github_repo: Optional[str] = None) -> bool:
    """Exact-label anchor match. Lookalike/homograph domains fail."""
    host = _normalize_host(host)
    if github_repo:
        repo = github_repo.strip().lower()
        repositories = {r.lower() for r in anchor.github_repositories}
        organizations = {o.lower() for o in anchor.github_organizations}
        if repo in repositories:
            return True
        owner = repo.split("/", 1)[0]
        if owner and owner in organizations:
            return True
    if not host:
        return False
    for raw_domain in anchor.domains:
        domain = _normalize_host(raw_domain)
        if not domain:
            continue
        if domain == host:
            return True
        if anchor.allow_subdomains and host.endswith("." + domain):
            return True
    return False


def _same_registrable_domain(host: str, domain: str) -> bool:
    h = _normalize_host(host)
    d = _normalize_host(domain)
    if not h or not d:
        return False
    return h == d or h.endswith("." + d)


def _registrable(host: str) -> str:
    """Two-label registrable approximation used only for the LIKELY rule."""
    labels = _normalize_host(host).split(".")
    if len(labels) <= 2:
        return ".".join(labels)
    return ".".join(labels[-2:])


class OfficialEvidenceValidator:
    """Assigns officiality from anchors and resolves contradictions."""

    source_priority_order = list(SOURCE_PRIORITY)

    def __init__(self, anchors: List[TrustAnchor]):
        self.anchors = anchors

    # --- officiality --------------------------------------------------------

    def evaluate(self, evidence: Evidence) -> ValidationDecision:
        """Decide officiality with a deterministic rule id and note."""
        if evidence.officiality in (Officiality.OFFICIAL, Officiality.REJECTED):
            return ValidationDecision(
                officiality=evidence.officiality,
                rule_id="TA-000",
                note="officiality already decided; validator never re-decides",
            )
        host = _normalize_host(urlparse(evidence.url).hostname or "")
        repo = github_repo_from_url(evidence.url)

        if not self._provider_has_anchors(evidence.provider_id):
            return ValidationDecision(
                officiality=Officiality.LIKELY_OFFICIAL,
                rule_id=RULE_NO_ANCHOR_FOR_PROVIDER,
                note="no trust anchor configured for this provider; cannot be OFFICIAL",
            )

        if _registrable(host) in URL_SHORTENERS:
            return ValidationDecision(
                officiality=Officiality.THIRD_PARTY,
                rule_id=RULE_NO_MATCH_THIRD_PARTY,
                note=f"URL shortener {host!r} cannot establish officiality",
            )

        anchor = self._anchor_for(evidence.provider_id, host, repo)
        if anchor is not None:
            if repo is not None and self._github_rule_applies(anchor, repo):
                return ValidationDecision(
                    officiality=Officiality.OFFICIAL,
                    rule_id=RULE_GITHUB_MAPPING,
                    note=f"mapped official GitHub repository {repo!r}",
                )
            normalized = [_normalize_host(d) for d in anchor.domains]
            if host and any(d == host for d in normalized):
                return ValidationDecision(
                    officiality=Officiality.OFFICIAL,
                    rule_id=RULE_EXACT_DOMAIN,
                    note=f"exact configured anchor domain for {anchor.provider_id!r}",
                )
            if host and anchor.allow_subdomains and any(
                host.endswith("." + d) for d in normalized
            ):
                return ValidationDecision(
                    officiality=Officiality.OFFICIAL,
                    rule_id=RULE_ALLOWED_SUBDOMAIN,
                    note=f"anchor for {anchor.provider_id!r} allows this subdomain",
                )
            return ValidationDecision(
                officiality=Officiality.OFFICIAL,
                rule_id=RULE_EXACT_DOMAIN,
                note=f"matched configured anchor for {anchor.provider_id!r}",
            )

        if repo is not None and self._any_github_anchor_for(evidence.provider_id):
            return ValidationDecision(
                officiality=Officiality.THIRD_PARTY,
                rule_id=RULE_UNMAPPED_GITHUB,
                note="github repository is not mapped by an anchor",
            )
        if self._same_registrable_domain_as_anchor(evidence.provider_id, host):
            return ValidationDecision(
                officiality=Officiality.LIKELY_OFFICIAL,
                rule_id=RULE_SAME_REGISTRABLE_DOMAIN,
                note="same registrable domain but not an allowed anchor match",
            )
        return ValidationDecision(
            officiality=Officiality.THIRD_PARTY,
            rule_id=RULE_NO_MATCH_THIRD_PARTY,
            note="no trust anchor match",
        )

    def validate(self, evidence: Evidence) -> Evidence:
        """Return a copy with officiality assigned and rule notes appended.

        Idempotent: re-validating the same evidence does not duplicate its rule
        note, so repeated pipeline runs do not rewrite the evidence file.
        """
        decision = self.evaluate(evidence)
        if decision.officiality is evidence.officiality:
            return evidence
        note = f"{decision.rule_id}: {decision.note}"
        if note in evidence.validation_notes:
            notes = list(evidence.validation_notes)
        else:
            notes = evidence.validation_notes + [note]
        return evidence.model_copy(
            update={"officiality": decision.officiality, "validation_notes": notes}
        )

    def _github_rule_applies(self, anchor: TrustAnchor, repo: Optional[str]) -> bool:
        if repo is None:
            return False
        return match_anchor(anchor, "github.com", github_repo=repo)

    def _anchor_for(self, provider_id, host, repo) -> Optional[TrustAnchor]:
        for anchor in self.anchors:
            if anchor.provider_id != provider_id:
                continue
            if match_anchor(anchor, host, github_repo=repo):
                return anchor
        return None

    def _any_github_anchor_for(self, provider_id) -> bool:
        return any(
            a.provider_id == provider_id
            and (a.github_organizations or a.github_repositories)
            for a in self.anchors
        )

    def _provider_has_anchors(self, provider_id) -> bool:
        return any(a.provider_id == provider_id for a in self.anchors)

    def _same_registrable_domain_as_anchor(self, provider_id, host) -> bool:
        for anchor in self.anchors:
            if anchor.provider_id != provider_id:
                continue
            if any(_same_registrable_domain(host, d) for d in anchor.domains):
                return True
        return False

    # --- contradictions ------------------------------------------------------

    def resolve_contradictions(
        self, evidences: List[Evidence], require_dates: bool = False
    ) -> Dict[str, Any]:
        """Pick the winning evidence deterministically; record unresolved conflict.

        Priority wins first. Within equal priority a later ``effective_at`` wins,
        then ``published_at``. ``retrieved_at`` never acts as the policy date.
        """
        official = [e for e in evidences if e.officiality is Officiality.OFFICIAL]
        if not official:
            raise ContradictionError("no OFFICIAL evidence supports confirmation")

        ranked = sorted(official, key=lambda e: self._rank(e.source_type))
        top_rank = self._rank(ranked[0].source_type)
        top = [e for e in official if self._rank(e.source_type) == top_rank]

        def _sort_key(e: Evidence):
            eff = e.effective_at or datetime.min.replace(tzinfo=timezone.utc)
            pub = e.published_at or datetime.min.replace(tzinfo=timezone.utc)
            return (eff, pub)

        winner = max(top, key=_sort_key)
        unresolved: List[str] = []
        for e in top:
            if e is winner:
                continue
            if _sort_key(e) == _sort_key(winner):
                unresolved.append(
                    f"equal-priority conflict between {e.evidence_id} and {winner.evidence_id}"
                )
        if require_dates and (winner.effective_at is None and winner.published_at is None):
            unresolved.append(f"missing usable date on {winner.evidence_id}")
            raise ContradictionError(f"missing usable date on {winner.evidence_id}")
        return {"winner": winner, "unresolved": unresolved}

    def current_paid_overrides_older_free(self, evidences: List[Evidence]) -> Optional[Evidence]:
        """A current pricing page saying paid overrides an older blog saying free."""
        pricing = [e for e in evidences if e.source_type == "pricing" and e.officiality is Officiality.OFFICIAL]
        blog = [e for e in evidences if e.source_type == "blog" and e.officiality is Officiality.OFFICIAL]
        if not pricing or not blog:
            return None
        paid = [e for e in pricing if _says_paid(e)]
        if not paid:
            return None
        return max(paid, key=lambda e: e.effective_at or e.retrieved_at)

    def _rank(self, source_type: str) -> int:
        try:
            return SOURCE_PRIORITY.index(source_type)
        except ValueError:
            return len(SOURCE_PRIORITY)


_PAID_MARKERS = ("paid", "no free tier", "not free", "requires payment", "subscription required")


def _says_paid(evidence: Evidence) -> bool:
    text = f"{evidence.claim or ''} {evidence.content_excerpt or ''}".lower()
    return any(m in text for m in _PAID_MARKERS)
