"""GitHub repository discovery (TASK-005).

Normalizes GitHub search results into provenance-preserving
CandidateObservations. Repository content is untrusted and never executed.
The optional token comes only from a named environment variable. Network
access is fully injectable so tests stay offline.

FIX-003 (Stage 1.5):
- GITHUB_TOKEN is sent only to api.github.com (Authorization: Bearer).
- Redirects to non-GitHub domains are refused; the token never leaks.
- Token is redacted from exceptions, observations, and log output.
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

# Known official GitHub API domains; token must never leave these.
OFFICIAL_GITHUB_DOMAINS = {"api.github.com", "github.com"}


class GitHubHttpError(Exception):
    """Raised for HTTP errors (rate limits, transient failures)."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class RedirectHandler(urllib.request.HTTPRedirectHandler):
    """Custom redirect handler that refuses to follow redirects off
    official GitHub API domains, preventing token leakage."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlparse(newurl)
        host = (parsed.hostname or "").lower()
        if host not in OFFICIAL_GITHUB_DOMAINS:
            raise urllib.error.HTTPError(
                newurl, code,
                f"refusing to follow redirect to non-official domain {host!r}",
                headers, fp
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HttpTransport:
    """Minimal injectable HTTP JSON GET (default uses urllib, no auth headers).

    FIX-003: token is sent only to api.github.com via Authorization header,
    and errors are sanitized so the token never appears in exception text.
    """

    def __init__(self, token: Optional[str] = None):
        self._token = token

    def _sanitize(self, msg: str) -> str:
        """Redact token-like patterns from messages."""
        if self._token:
            msg = msg.replace(self._token, "***REDACTED***")
        # Also redact common bearer patterns
        import re as _re
        msg = _re.sub(r'(Bearer|bearer|api[_-]?key|token)[\s=:]+[A-Za-z0-9_\-\.]+', r'\1 ***REDACTED***', msg)
        return msg

    def get_json(self, url: str, params: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        if params:
            url = url + "?" + urllib.parse.urlencode(params)
        parsed = urllib.parse.urlparse(url)
        host = (parsed.hostname or "").lower()

        # Only send token to official GitHub API domain
        headers = {"Accept": "application/vnd.github+json"}
        if self._token and host == "api.github.com":
            headers["Authorization"] = f"Bearer {self._token}"

        opener = urllib.request.build_opener(RedirectHandler)
        request = urllib.request.Request(url, headers=headers)
        try:
            with opener.open(request, timeout=30) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            msg = self._sanitize(f"github http {exc.code}: {exc.reason}")
            raise GitHubHttpError(msg, exc.code) from exc
        except urllib.error.URLError as exc:
            msg = self._sanitize(f"github network error: {exc.reason}")
            raise GitHubHttpError(msg) from exc
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
        self.http = http or HttpTransport(token=token)
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
