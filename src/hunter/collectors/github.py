"""GitHub repository discovery (TASK-005).

Normalizes GitHub search results into provenance-preserving
CandidateObservations. Repository content is untrusted and never executed.
The optional token comes only from a named environment variable. Network
access is fully injectable so tests stay offline.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..discovery.models import CandidateObservation, SourceType

GITHUB_API = "https://api.github.com/search/repositories"


class GitHubHttpError(Exception):
    """Raised for HTTP errors (rate limits, transient failures)."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class HttpTransport:
    """Minimal injectable HTTP JSON GET (default uses urllib, no auth headers)."""

    def get_json(self, url: str, params: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        if params:
            url = url + "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            raise GitHubHttpError(f"github http {exc.code}: {exc.reason}", exc.code) from exc
        except urllib.error.URLError as exc:
            raise GitHubHttpError(f"github network error: {exc.reason}") from exc
        try:
            return json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise GitHubHttpError(f"invalid JSON from github: {exc}") from exc


class GitHubCollector:
    """Collect CandidateObservations from the GitHub repository search API."""

    def __init__(
        self,
        http: Optional[HttpTransport] = None,
        token: Optional[str] = None,
        queries: Optional[List[str]] = None,
        per_page: int = 10,
        max_total: int = 50,
    ):
        self.http = http or HttpTransport()
        self.token = token  # token is only read from env by the factory
        self.queries = queries or ["free llm api"]
        self.per_page = per_page
        self.max_total = max_total

    def collect(self, query: str, run_context) -> List[CandidateObservation]:
        if not query or not query.strip():
            raise ValueError("collect requires a non-empty query")
        observations: List[CandidateObservation] = []
        seen: set = set()
        for page in range(1, 100):  # bounded by max_total below
            params = {"q": query, "per_page": str(self.per_page), "page": str(page)}
            payload = self.http.get_json(GITHUB_API, params)
            items = payload.get("items", []) if isinstance(payload, dict) else []
            for item in items:
                if len(observations) >= self.max_total:
                    break
                if not isinstance(item, dict):
                    continue
                obs = self._normalize(item, query, run_context)
                if obs is None or obs.fingerprint in seen:
                    continue
                seen.add(obs.fingerprint)
                observations.append(obs)
            if len(observations) >= self.max_total:
                break
            if not items:
                break
        return observations

    def _normalize(
        self, item: Dict[str, Any], query: str, run_context
    ) -> Optional[CandidateObservation]:
        full_name = item.get("full_name")
        html_url = item.get("html_url")
        if not full_name or not html_url:
            return None
        description = item.get("description")
        claim = (description or "").strip() or f"{full_name} may offer free AI API access"
        stable_id = str(item.get("id") or full_name).strip().lower()
        stable_id = re.sub(r"[^a-z0-9]+", "-", stable_id).strip("-")
        run_ts = run_context.run_timestamp
        try:
            discovered_at = datetime.fromisoformat(run_ts)
        except ValueError:
            discovered_at = datetime.now(timezone.utc)
        return CandidateObservation(
            observation_id=f"github-{item.get('id') or abs(hash(full_name))}",
            source_type=SourceType.github,
            source_url=html_url,
            source_title=full_name,
            claim=claim,
            matched_query=query,
            discovered_at=discovered_at,
            raw_metadata={
                "github_full_name": full_name,
                "github_candidate_id": f"github-{stable_id}",
                "github_owner": full_name.split("/", 1)[0] if "/" in full_name else None,
                "github_html_url": html_url,
                "github_homepage": item.get("homepage"),
                "stargazers_count": item.get("stargazers_count"),
                "fork": bool(item.get("fork")),
                "language": item.get("language"),
                "github_updated_at": item.get("updated_at"),
                "github_description": description,
            },
        )
