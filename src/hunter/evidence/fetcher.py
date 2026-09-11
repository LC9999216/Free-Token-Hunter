"""Safe evidence fetcher with SSRF controls (AGENTS.md section 9).

Every requested URL and redirect hop must pass all checks before connecting:
scheme HTTP/HTTPS, port 80/443, no user-info, hostname resolves, every
IPv4/IPv6 result is public (not loopback/private/link-local/reserved/
multicast/unspecified), redirects are revalidated from scratch, max 3
redirects, max 2 MiB body, 10-second total timeout, bounded content types,
max 6 pages per Provider per run. No scripts, no cookies, no auth headers.
"""

from __future__ import annotations

import ipaddress
import socket
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence
from urllib.parse import urlparse

MAX_REDIRECTS = 3
MAX_BODY_BYTES = 2 * 1024 * 1024
TOTAL_TIMEOUT = 10
MAX_PAGES_PER_PROVIDER = 6
ALLOWED_SCHEMES = {"http", "https"}
ALLOWED_PORTS = {80, 443}
ACCEPTED_CONTENT_TYPES = ("text/html", "text/plain", "application/json", "application/xhtml+xml")


class FetcherError(Exception):
    """Raised when a fetch is blocked by a security check or fails."""


@dataclass
class FetchResult:
    status: int
    headers: Dict[str, str]
    body: bytes
    final_url: str


def check_host_addresses(addresses: Sequence[ipaddress._BaseAddress]) -> bool:
    """Return True only if every address is public (not special)."""
    if not addresses:
        return False
    for addr in addresses:
        if not isinstance(addr, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
            return False
        if addr.is_private:
            return False
        if addr.is_loopback:
            return False
        if addr.is_link_local:
            return False
        if addr.is_multicast:
            return False
        if addr.is_reserved:
            return False
        if addr.is_unspecified:
            return False
    return True


def dns_resolve(host: str, resolver=None) -> bool:
    """Resolve a hostname and confirm all results are public."""
    if resolver is None:
        try:
            infos = socket.getaddrinfo(host, None)
            addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
        except (socket.gaierror, ValueError, OSError):
            return False
    else:
        raw = resolver(host)
        try:
            addresses = [ipaddress.ip_address(item) for item in raw]
        except ValueError:
            return False
    return check_host_addresses(addresses)


def check_url(url: str) -> urlparse.ParseResult:
    """Validate scheme, port, user-info, and parseable URL. Returns parsed."""
    if not isinstance(url, str) or not url.strip():
        raise FetcherError("empty URL")
    try:
        parsed = urlparse(url.strip())
    except ValueError as exc:
        raise FetcherError(f"unparseable URL {url!r}") from exc
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise FetcherError(f"disallowed scheme {parsed.scheme!r}")
    if parsed.hostname is None:
        raise FetcherError(f"URL has no host: {url!r}")
    if parsed.port is not None and parsed.port not in ALLOWED_PORTS:
        raise FetcherError(f"disallowed port {parsed.port}")
    if parsed.username or parsed.password:
        raise FetcherError("URL must not contain user-info credentials")
    return parsed


class SafeFetcher:
    """HTTP(S) fetcher enforcing all SSRF and size/timeout limits."""

    def __init__(
        self,
        transport: Any = None,
        max_redirects: int = MAX_REDIRECTS,
        max_body_bytes: int = MAX_BODY_BYTES,
        total_timeout: float = TOTAL_TIMEOUT,
        max_pages_per_provider: int = MAX_PAGES_PER_PROVIDER,
        resolver=None,
    ):
        self.transport = transport
        self.max_redirects = max_redirects
        self.max_body_bytes = max_body_bytes
        self.total_timeout = total_timeout
        self.max_pages_per_provider = max_pages_per_provider
        self._resolver = resolver
        self._page_counts: Dict[str, int] = {}

    def fetch(self, url: str, provider_id: str) -> FetchResult:
        self._bump_page_count(provider_id)
        start = time.monotonic()
        current = url
        for _ in range(self.max_redirects + 1):
            parsed = check_url(current)
            self._assert_public(parsed)
            status, headers, body = self._transact(parsed, start)
            if status in (301, 302, 303, 307, 308) and "Location" in headers:
                current = headers["Location"]
                # redirect destination is revalidated from scratch
                continue
            self._check_declared_length(headers)
            self._check_content_type(headers, body)
            if len(body) > self.max_body_bytes:
                raise FetcherError("response body exceeds size limit")
            return FetchResult(
                status=status,
                headers=dict(headers),
                body=body,
                final_url=current,
            )
        raise FetcherError("too many redirects")

    def _assert_public(self, parsed: urlparse.ParseResult) -> None:
        host = parsed.hostname or ""
        if self.transport is not None and hasattr(self.transport, "resolve"):
            try:
                raw = self.transport.resolve(host)
            except Exception:  # noqa: BLE001 - resolution failure blocks fetch
                raise FetcherError(f"cannot resolve host {host!r}") from None
            if not check_host_addresses([ipaddress.ip_address(a) for a in raw]):
                raise FetcherError(f"host {host!r} resolves to non-public addresses")
            return
        if not dns_resolve(host, self._resolver):
            raise FetcherError(f"host {host!r} does not resolve to public addresses")

    def _transact(self, parsed, start) -> tuple[int, Dict[str, str], bytes]:
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        remaining = self.total_timeout - (time.monotonic() - start)
        if remaining <= 0:
            raise FetcherError("total request timeout exceeded")
        url = parsed.geturl()
        if self.transport is not None:
            result = self.transport.request(url)
            if time.monotonic() - start > self.total_timeout:
                raise FetcherError("total request timeout exceeded")
            return result
        return self._real_request(host, port, url, remaining)

    def _real_request(self, host, port, url, timeout) -> tuple[int, Dict[str, str], bytes]:
        # Deliberately simple: no cookies, no auth, no scripts.
        import http.client

        conn = http.client.HTTPSConnection(host, port, timeout=timeout)
        try:
            conn.request("GET", url)
            response = conn.getresponse()
            headers = {k.lower(): v for k, v in response.getheaders()}
            body = response.read()
            return (response.status, headers, body)
        finally:
            conn.close()

    def _check_content_type(self, headers, body) -> None:
        ctype = (headers.get("Content-Type") or headers.get("content-type") or "").lower().split(";")[0].strip()
        if ctype and ctype not in ACCEPTED_CONTENT_TYPES:
            raise FetcherError(f"unsupported content type {ctype!r}")

    def _check_declared_length(self, headers) -> None:
        raw = headers.get("Content-Length") or headers.get("content-length")
        if raw is None:
            return
        try:
            declared = int(str(raw).strip())
        except ValueError:
            return
        if declared > self.max_body_bytes:
            raise FetcherError(f"declared Content-Length {declared} exceeds size limit")

    def _bump_page_count(self, provider_id: str) -> None:
        count = self._page_counts.get(provider_id, 0)
        if count >= self.max_pages_per_provider:
            raise FetcherError(
                f"provider {provider_id!r} exceeded page limit {self.max_pages_per_provider}"
            )
        self._page_counts[provider_id] = count + 1
