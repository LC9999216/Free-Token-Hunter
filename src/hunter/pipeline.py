"""End-to-end offline pipeline (TASK-010).

Deterministic given the same seed fixture, config, and explicit ``as_of``:
  import seed -> candidates
  -> build evidence from asserted doc URLs
  -> validate officiality via trust anchors
  -> deterministic extraction from structured seed facts
  -> scores
  -> confirm FREE_CONFIRMED providers through confirm_provider()

Running twice with identical inputs produces a byte-identical Registry and no
new no-op History events. No network, no LLM, no system clock.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .collectors.registry_seed import RegistrySeedImporter
from .config import DEFAULT_CONFIG_DIR, load_settings, load_sources
from .discovery.models import SourceType
from .discovery.store import CandidateStore
from .evidence.models import Evidence
from .evidence.store import EvidenceStore
from .evidence.validator import OfficialEvidenceValidator, load_trust_anchors
from .llm.models import ExtractionResult
from .registry.confirmation import ConfirmationError, ConfirmationInput, confirm_provider
from .registry.schema import ProviderStatus
from .registry.store import ProviderRegistry
from .scoring.config import load_scoring_config
from .scoring.scores import free_score, verification_confidence


class Pipeline:
    """Rerunnable offline stage-one pipeline."""

    def __init__(
        self,
        data_dir: Path,
        seed_path: Optional[Path] = None,
        as_of: Optional[datetime] = None,
        scoring_path: Optional[Path] = None,
        sources_path: Optional[Path] = None,
    ):
        self.data_dir = Path(data_dir)
        self.seed_path = Path(seed_path) if seed_path else None
        self.as_of = as_of or datetime(2026, 9, 1, tzinfo=timezone.utc)
        if self.as_of.tzinfo is None:
            self.as_of = self.as_of.replace(tzinfo=timezone.utc)
        self.scoring_path = Path(scoring_path) if scoring_path else DEFAULT_CONFIG_DIR / "scoring.yaml"
        self.sources_path = Path(sources_path) if sources_path else DEFAULT_CONFIG_DIR / "sources.yaml"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.candidate_store = CandidateStore(self.data_dir / "candidates.json")
        self.evidence_store = EvidenceStore(self.data_dir / "evidence.json")
        self.registry = ProviderRegistry(
            providers_path=self.data_dir / "providers.json",
            history_path=self.data_dir / "history.jsonl",
        )
        anchors = load_trust_anchors(self.sources_path)
        self.validator = OfficialEvidenceValidator(anchors=anchors)
        self.scoring_config = load_scoring_config(self.scoring_path)

    # --- step 1: import seed -------------------------------------------------

    def import_seed(self) -> Dict[str, int]:
        if self.seed_path is None:
            raise ValueError("seed_path is required for import_seed")
        result = RegistrySeedImporter().load(self.seed_path)
        counts = self.candidate_store.ingest(result.entries)
        return {
            "version": result.version,
            "candidates_created": counts.candidates_created,
            "candidates_merged": counts.candidates_merged,
            "observations_added": counts.observations_added,
            "unchanged": counts.unchanged,
            "rejected": counts.rejected,
            "errors": counts.errors,
        }

    # --- step 2: build evidence ---------------------------------------------

    def build_evidence(self) -> int:
        """Create Evidence records from asserted doc URLs (deterministic)."""
        created = 0
        for candidate in self.candidate_store.list_candidates():
            for obs in candidate.observations:
                docs_url = (obs.raw_metadata or {}).get("asserted_docs_url")
                url = docs_url if isinstance(docs_url, str) and docs_url.startswith("http") else None
                if not url:
                    continue
                source_type = _guess_source_type(url)
                claim = (obs.raw_metadata or {}).get("asserted_free_tier") or obs.claim or ""
                evidence = Evidence(
                    candidate_id=candidate.candidate_id,
                    provider_id=candidate.candidate_id,
                    url=url,
                    source_type=source_type,
                    retrieved_at=self.as_of,
                    effective_at=self.as_of,
                    title=obs.source_title,
                    claim=str(claim)[:300],
                    content_excerpt=str(obs.claim or "")[:4000],
                )
                if self.evidence_store.upsert(evidence):
                    created += 1
        return created

    # --- step 3: validate officiality ----------------------------------------

    def validate_evidence(self) -> Dict[str, int]:
        counts = {"OFFICIAL": 0, "LIKELY_OFFICIAL": 0, "THIRD_PARTY": 0, "REJECTED": 0, "UNCONFIRMED": 0}
        for evidence in self.evidence_store.list():
            validated = self.validator.validate(evidence)
            counts[validated.officiality.value] = counts.get(validated.officiality.value, 0) + 1
            self.evidence_store.upsert(validated)
        return counts

    # --- step 4: extraction --------------------------------------------------

    def _extraction_for(self, candidate_id: str) -> Optional[ExtractionResult]:
        candidate = self.candidate_store.get(candidate_id)
        if candidate is None:
            return None
        hints: Dict[str, Any] = {}
        asserted: Dict[str, Any] = {}
        for obs in candidate.observations:
            meta = obs.raw_metadata or {}
            hints.update(meta.get("offer_hints") or {})
            for key in (
                "asserted_phone_required",
                "asserted_card_required",
                "asserted_commercial_ok",
                "asserted_openai_compatible",
                "asserted_openai_base_url",
                "asserted_env_key",
            ):
                if meta.get(key) is not None:
                    asserted[key.replace("asserted_", "")] = meta[key]
        return ExtractionResult(
            ok=True,
            offer_kind=hints.get("offer_kind"),
            quota_mode=hints.get("quota_mode"),
            renewal_period=hints.get("renewal_period"),
            access_method=hints.get("access_method"),
            offer_status=None,
            card_required=_bool_or_none(asserted.get("card_required")),
            phone_required=_bool_or_none(asserted.get("phone_required")),
            commercial_use_allowed=_bool_or_none(asserted.get("commercial_ok")),
            openai_compatible=_bool_or_none(asserted.get("openai_compatible")),
            base_url=asserted.get("openai_base_url"),
            description=None,
            quota_text=None,
        )

    # --- step 5+6: score and confirm -----------------------------------------

    def score_and_confirm(self) -> Dict[str, int]:
        """Score each candidate and confirm when every hard gate passes."""
        outcomes = {
            "free_confirmed": 0,
            "providers_created": 0,
            "providers_updated": 0,
            "unchanged": 0,
            "errors": 0,
        }
        for candidate in self.candidate_store.list_candidates():
            try:
                evidence_items = [
                    e
                    for e in self.evidence_store.list()
                    if e.candidate_id == candidate.candidate_id
                ]
                if not evidence_items:
                    outcomes["unchanged"] += 1
                    continue
                extraction = self._extraction_for(candidate.candidate_id)
                if extraction is None or not extraction.ok:
                    # ungrounded extraction: recorded, retryable, not confirmed
                    outcomes["unchanged"] += 1
                    continue
                provider_id = _slug(candidate.provider_name or candidate.candidate_id)
                before = self.registry.get_provider(
                    provider_id
                ) or self.registry.find_by_domain(candidate.canonical_domain_hint)
                vc = verification_confidence(evidence_items, self.scoring_config, self.as_of)
                requirements = _requirements_for(candidate)
                api = _api_for(candidate)
                fs = free_score(
                    _offer_from_hints(_offer_hints_for(candidate), extraction),
                    requirements,
                    api,
                    models=_models_for(candidate),
                    has_documented_limits=_has_limits(candidate),
                    _config=self.scoring_config,
                    as_of=self.as_of,
                )
                best = max(evidence_items, key=lambda e: (e.effective_at or e.retrieved_at))
                confirmed = confirm_provider(
                    ConfirmationInput(
                        provider_name=candidate.provider_name or candidate.candidate_id,
                        canonical_domain=candidate.canonical_domain_hint or "",
                        evidence=best,
                        extraction=extraction,
                        verification_confidence=vc,
                        free_score=fs,
                        as_of=self.as_of,
                        source_metadata={
                            "pipeline": "run-stage-one",
                            "as_of": self.as_of.isoformat(),
                        },
                        reason="stage-one pipeline confirmation from anchored official evidence",
                    ),
                    self.registry,
                    self.validator,
                )
            except ConfirmationError:
                # a hard gate rejected this candidate; nothing was mutated
                outcomes["unchanged"] += 1
                continue
            except Exception:  # noqa: BLE001 - one candidate must not corrupt others
                outcomes["errors"] += 1
                continue

            outcomes["free_confirmed"] += 1
            if before is None:
                outcomes["providers_created"] += 1
            elif confirmed.revision > before.revision:
                outcomes["providers_updated"] += 1
            else:
                outcomes["unchanged"] += 1
        return outcomes

    # --- full run ------------------------------------------------------------

    def run(self) -> Dict[str, Any]:
        """Run the whole stage-one flow and return the spec summary."""
        seed_summary = self.import_seed() if self.seed_path else {"skipped": True}
        candidates = self.candidate_store.list_candidates()
        evidence_built = self.build_evidence()
        validation = self.validate_evidence()
        outcomes = self.score_and_confirm()

        status_counts = {
            "free_confirmed": 0,
            "uncertain": 0,
            "not_free": 0,
            "expired": 0,
            "rejected": 0,
        }
        for provider in self.registry.list_providers():
            status_counts[_status_key(provider.status)] = (
                status_counts.get(_status_key(provider.status), 0) + 1
            )

        return {
            "candidates_processed": len(candidates),
            "providers_created": outcomes["providers_created"],
            "providers_updated": outcomes["providers_updated"],
            "free_confirmed": status_counts["free_confirmed"],
            "uncertain": status_counts["uncertain"],
            "not_free": status_counts["not_free"],
            "expired": status_counts["expired"],
            "rejected": status_counts["rejected"],
            "unchanged": outcomes["unchanged"],
            "errors": outcomes["errors"],
            "detail": {
                "import_seed": seed_summary,
                "evidence_built": evidence_built,
                "validation": validation,
                "providers": len(self.registry.list_providers()),
            },
        }


def _status_key(status) -> str:
    value = status.value if hasattr(status, "value") else str(status)
    return {
        "FREE_CONFIRMED": "free_confirmed",
        "UNCERTAIN": "uncertain",
        "NOT_FREE": "not_free",
        "EXPIRED": "expired",
        "REJECTED": "rejected",
    }.get(value, "uncertain")


def _slug(name: str) -> str:
    import re

    slug = re.sub(r"[^a-z0-9]+", "-", str(name).strip().lower()).strip("-")
    return slug or "provider"


def _guess_source_type(url: str) -> str:
    if "pricing" in url or "plan" in url:
        return "pricing"
    if "api" in url or "developer" in url or "reference" in url:
        return "api-docs"
    if "docs" in url or "documentation" in url:
        return "docs"
    if "blog" in url or "news" in url:
        return "blog"
    return "page"


def _bool_or_none(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    lowered = str(value).strip().lower()
    if lowered in ("true", "yes", "1", "y"):
        return True
    if lowered in ("false", "no", "0", "n"):
        return False
    return None


def _offer_hints_for(candidate) -> Dict[str, Any]:
    hints: Dict[str, Any] = {}
    for obs in candidate.observations:
        hints.update((obs.raw_metadata or {}).get("offer_hints") or {})
    return hints


def _requirements_for(candidate):
    from .registry.schema import ProviderRequirements

    asserted: Dict[str, Any] = {}
    for obs in candidate.observations:
        meta = obs.raw_metadata or {}
        for key, target in (
            ("asserted_phone_required", "phone_required"),
            ("asserted_card_required", "card_required"),
            ("asserted_commercial_ok", "commercial_use_allowed"),
        ):
            if meta.get(key) is not None:
                asserted[target] = _bool_or_none(meta[key])
    regional = _regional_for(candidate)
    return ProviderRequirements(
        signup_required=None,
        phone_required=asserted.get("phone_required"),
        card_required=asserted.get("card_required"),
        commercial_use_allowed=asserted.get("commercial_use_allowed"),
        regional_restrictions=regional,
    )


def _regional_for(candidate) -> Optional[str]:
    for obs in candidate.observations:
        meta = obs.raw_metadata or {}
        if meta.get("asserted_regional_restrictions"):
            return str(meta["asserted_regional_restrictions"])
    return None


def _api_for(candidate):
    from .registry.schema import ProviderApi

    api = ProviderApi()
    for obs in candidate.observations:
        meta = obs.raw_metadata or {}
        if meta.get("asserted_openai_base_url"):
            api.base_url = str(meta["asserted_openai_base_url"])
        if meta.get("asserted_openai_compatible") is not None:
            api.openai_compatible = _bool_or_none(meta["asserted_openai_compatible"])
    return api


def _models_for(candidate) -> List[str]:
    models: List[str] = []
    for obs in candidate.observations:
        meta = obs.raw_metadata or {}
        raw = meta.get("asserted_models_free")
        if isinstance(raw, list):
            models.extend(str(m) for m in raw)
    seen = set()
    return [m for m in models if not (m in seen or seen.add(m))]


def _has_limits(candidate) -> bool:
    for obs in candidate.observations:
        meta = obs.raw_metadata or {}
        if meta.get("asserted_rate_limits"):
            return True
    return False


def _offer_from_hints(hints: Dict[str, Any], extraction: ExtractionResult):
    from .registry.schema import FreeOffer, OfferKind, OfferStatus

    offer_kind = extraction.offer_kind or hints.get("offer_kind")
    try:
        kind = OfferKind(offer_kind) if offer_kind else OfferKind.unknown
    except ValueError:
        kind = OfferKind.unknown
    return FreeOffer(
        offer_kind=kind,
        offer_status=OfferStatus.active,  # will be recomputed by confirmation
        description=extraction.description,
        quota_text=extraction.quota_text,
        expires_at=extraction.expires_at,
    )
