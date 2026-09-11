"""Replaceable structured LLM client interface (TASK-009).

The client receives messages, a JSON schema, and evidence text only. It gets no
tools, no environment variables, no files, and no arbitrary network access.
Implementations are injected so all default tests stay offline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol, Sequence, runtime_checkable


@dataclass(frozen=True)
class LlmRequest:
    """A single tool-less structured completion request."""

    system: str
    user: str
    schema: Dict[str, Any]
    evidence_id: str
    evidence_text: str
    tools: Optional[Sequence[Any]] = None  # must stay empty; tools are forbidden
    max_repairs: int = 1

    def __post_init__(self) -> None:
        if self.tools:
            raise ValueError("the structured LLM boundary never passes tools")


@dataclass(frozen=True)
class LlmResponse:
    text: str
    raw: Dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class StructuredLlmClient(Protocol):
    """Anything that can complete a tool-less structured request."""

    def complete(self, request: LlmRequest) -> LlmResponse: ...


class FakeStructuredClient:
    """In-memory client for tests and offline pipelines.

    Accepts a queue of raw text outputs; never touches the network.
    """

    def __init__(self, responses: Optional[Sequence[str]] = None):
        self.responses: List[str] = list(responses or [])
        self.requests: List[LlmRequest] = []

    def complete(self, request: LlmRequest) -> LlmResponse:
        self.requests.append(request)
        if self.responses:
            return LlmResponse(text=self.responses.pop(0))
        return LlmResponse(text="{}")


def assert_no_secrets(payload: str, secret_values: Sequence[str]) -> None:
    """Raise if any known secret value appears in outbound model payload."""
    for secret in secret_values:
        if secret and secret in payload:
            raise ValueError("refusing to send secret material to the LLM boundary")


def assert_no_tools(request: LlmRequest) -> None:
    """Raise if a request carries tools.

    Only the request's own structure is inspected: untrusted page text may
    legitimately contain strings like ``api_key=`` and must not be mistaken for
    outbound credential material. Real secrets are guarded separately by
    :func:`assert_no_secrets` against known secret values.
    """
    if request.tools:
        raise ValueError("refusing to send tools to the LLM boundary")
