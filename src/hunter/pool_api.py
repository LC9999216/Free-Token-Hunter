"""Pool Control loopback HTTP API + Hunter-side client (review 二.1).

The Pool Control process exposes exactly four endpoints, bound to 127.0.0.1
only, authenticated with a Bearer token (HUNTER_POOL_CONTROL_TOKEN on the
Hunter side; the Pool Control side reads its own copy from its environment or
command line flag at startup):

    GET  /providers/{id}/status   -> sanitized status (no keys)
    POST /providers/{id}/probe    -> real health/protocol canaries
    POST /providers/{id}/promote  -> staging -> production (verified readback)
    POST /providers/{id}/suspend  -> production removal (verified readback)
    POST /pool/stop               -> halt production (containment)

Guarantees:
- the server NEVER accepts or returns provider key material; key entry
  happens only in the Pool Control process console (getpass);
- request bodies are limited and must be JSON objects without key-like
  fields (fail closed);
- responses carry only structured, sanitized fields.
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

from .pool_control import PoolControl, PoolControlError

MAX_BODY_BYTES = 8 * 1024

_STATUS_ROUTE = re.compile(r"^/providers/(?P<provider_id>[^/]+)/status$")
_ACTION_ROUTE = re.compile(r"^/providers/(?P<provider_id>[^/]+)/(?P<action>probe|promote|suspend)$")

_FORBIDDEN_BODY_KEYS = {
    "key", "api_key", "apikey", "secret", "token", "password", "authorization",
    "value", "provider_key",
}


class PoolApiError(Exception):
    """Structured client-side error; never carries secrets."""


def _sanitize(payload: Any) -> Any:
    """Ensure no key-like value leaks into a response."""
    if isinstance(payload, dict):
        clean = {}
        for key, value in payload.items():
            if str(key).lower() in _FORBIDDEN_BODY_KEYS:
                continue
            clean[key] = _sanitize(value)
        return clean
    if isinstance(payload, list):
        return [_sanitize(item) for item in payload]
    return payload


class PoolControlHandler(BaseHTTPRequestHandler):
    """HTTP handler bound to 127.0.0.1 with Bearer auth."""

    server_version = "HunterPoolControl/1.0"

    # injected by serve()
    pool_control: PoolControl
    bearer_token: str

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        # Never log query strings, bodies, or auth material.
        import logging

        logging.getLogger("hunter.pool_api").info(
            "%s %s", self.command, self.path.split("?")[0]
        )

    # -- helpers -----------------------------------------------------------

    def _authorized(self) -> bool:
        header = self.headers.get("Authorization") or ""
        if not header.startswith("Bearer "):
            return False
        supplied = header[len("Bearer "):].strip()
        if not supplied or not self.bearer_token:
            return False
        return supplied == self.bearer_token

    def _deny(self, status: int, code: str) -> None:
        body = json.dumps({"error": code}).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json_response(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(_sanitize(payload)).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json_body(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise PoolApiError("body_too_large")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise PoolApiError("body_not_json") from None
        if not isinstance(payload, dict):
            raise PoolApiError("body_not_object")
        lowered = {str(k).lower() for k in payload}
        if lowered & _FORBIDDEN_BODY_KEYS:
            raise PoolApiError("body_contains_forbidden_fields")
        return payload

    # -- routes ---------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        if not self._authorized():
            self._deny(401, "unauthorized")
            return
        match = _STATUS_ROUTE.match(self.path.split("?")[0])
        if match is None:
            self._deny(404, "not_found")
            return
        provider_id = match.group("provider_id")
        status = self.pool_control.status(provider_id)
        self._json_response(
            200,
            {
                "provider_id": status.provider_id,
                "in_staging": status.in_staging,
                "in_production": status.in_production,
                "key_configured": status.key_configured,
                "health_status": status.health_status,
                "protocol_status": status.protocol_status,
                "production_halted": status.production_halted,
            },
        )

    def do_POST(self) -> None:  # noqa: N802
        if not self._authorized():
            self._deny(401, "unauthorized")
            return
        path = self.path.split("?")[0]
        try:
            body = self._read_json_body()
        except PoolApiError as exc:
            self._deny(400, str(exc))
            return
        if path == "/pool/stop":
            result = self.pool_control.stop_production()
            self._json_response(200, result)
            return
        if path == "/pool/resume":
            result = self.pool_control.resume_production()
            self._json_response(200, result)
            return
        match = _ACTION_ROUTE.match(path)
        if match is None:
            self._deny(404, "not_found")
            return
        provider_id = match.group("provider_id")
        action = match.group("action")
        try:
            if action == "probe":
                features = body.get("features") or ["chat"]
                result = self.pool_control.probe(provider_id, features=features)
            elif action == "promote":
                result = self.pool_control.promote(provider_id)
            else:
                result = self.pool_control.suspend(provider_id)
        except PoolControlError as exc:
            self._json_response(409, {"error": str(exc)})
            return
        self._json_response(200, result)


class PoolControlServer:
    """Bound-to-loopback HTTP server exposing the four Pool Control endpoints."""

    def __init__(
        self,
        pool_control: PoolControl,
        bearer_token: str,
        host: str = "127.0.0.1",
        port: int = 0,
    ):
        if not bearer_token:
            raise PoolApiError("bearer_token_required")
        self.pool_control = pool_control
        self.bearer_token = bearer_token

        handler_class = type(
            "BoundPoolControlHandler",
            (PoolControlHandler,),
            {"pool_control": pool_control, "bearer_token": bearer_token},
        )
        self._httpd = ThreadingHTTPServer((host, port), handler_class)
        self.host, self.port = self._httpd.server_address[:2]

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def serve_forever(self) -> None:
        self._httpd.serve_forever()

    def shutdown(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


class PoolControlClient:
    """Hunter-side client. Same method surface as PoolControl; no keys cross."""

    def __init__(self, base_url: str, bearer_token: str, timeout: float = 30.0):
        if not bearer_token:
            raise PoolApiError("bearer_token_required")
        self.base_url = base_url.rstrip("/")
        self.bearer_token = bearer_token
        self.timeout = timeout

    def _request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        import urllib.error
        import urllib.request

        url = f"{self.base_url}{path}"
        data = None
        headers = {"Authorization": f"Bearer {self.bearer_token}"}
        if body is not None:
            lowered = {str(k).lower() for k in body}
            if lowered & _FORBIDDEN_BODY_KEYS:
                raise PoolApiError("request_contains_forbidden_fields")
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8"))
                code = payload.get("error", f"http_{exc.code}")
            except Exception:  # noqa: BLE001
                code = f"http_{exc.code}"
            raise PoolApiError(code) from None
        except (urllib.error.URLError, OSError):
            raise PoolApiError("pool_control_unreachable") from None
        if not isinstance(payload, dict):
            raise PoolApiError("invalid_response")
        return payload

    # -- PoolControl-compatible surface --------------------------------------

    def status(self, provider_id: str) -> Dict[str, Any]:
        return self._request("GET", f"/providers/{provider_id}/status")

    def probe(
        self, provider_id: str, features=None, timeout: float = 20.0
    ) -> Dict[str, Any]:
        return self._request(
            "POST",
            f"/providers/{provider_id}/probe",
            body={"features": list(features or ["chat"])},
        )

    def promote(self, provider_id: str) -> Dict[str, Any]:
        return self._request("POST", f"/providers/{provider_id}/promote", body={})

    def suspend(self, provider_id: str) -> Dict[str, Any]:
        return self._request("POST", f"/providers/{provider_id}/suspend", body={})

    def stop_production(self) -> Dict[str, Any]:
        return self._request("POST", "/pool/stop", body={})

    def resume_production(self) -> Dict[str, Any]:
        return self._request("POST", "/pool/resume", body={})


__all__ = [
    "PoolControlServer",
    "PoolControlClient",
    "PoolApiError",
    "PoolControlHandler",
]
