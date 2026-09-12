"""Notification outbox (Stage 2).

All notifications go through a persistent outbox before being sent to Feishu.
Each message carries a deterministic event_id used as the Feishu idempotency key.

The outbox is stored at data/notification_outbox.json:
- ``pending``: ready to send, keyed by event_id
- ``sent``: already delivered

Rules:
- Never include Provider Keys, Authorization headers, or secrets.
- Only public info, official links, and status.
- No-op runs and duplicates produce no new outbox entries.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


class OutboxError(Exception):
    """Raised on outbox read/write failures."""


@dataclass(frozen=True)
class OutboxMessage:
    """A single notification destined for Feishu (or other channels)."""
    event_id: str
    provider_id: str
    provider_name: str
    event_type: str                 # NEW_HIGH_VALUE | FREE_TIER_CHANGED | PROMOTION_EXPIRING |
                                    # FREE_TIER_EXPIRED | POOL_SUSPENDED
    title: str
    body: str                       # Markdown body, public info only
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    channel: str = "feishu"         # routing hint


class OutboxStore:
    """Persistent notification outbox at data/notification_outbox.json."""

    def __init__(self, path: Path):
        self.path = path
        self._pending: Dict[str, OutboxMessage] = {}
        self._sent: Dict[str, OutboxMessage] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.is_file():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OutboxError(f"cannot load outbox {self.path}: {exc}") from exc
        for src, items in data.items():
            if not isinstance(items, dict):
                continue
            target = self._pending if src == "pending" else self._sent
            for eid, raw in items.items():
                raw["timestamp"] = datetime.fromisoformat(raw["timestamp"])
                target[eid] = OutboxMessage(**raw)

    def save(self) -> None:
        def _serialize_dict(d: Dict) -> Dict:
            return {
                eid: {
                    "event_id": msg.event_id,
                    "provider_id": msg.provider_id,
                    "provider_name": msg.provider_name,
                    "event_type": msg.event_type,
                    "title": msg.title,
                    "body": msg.body,
                    "timestamp": msg.timestamp.isoformat(),
                    "channel": msg.channel,
                }
                for eid, msg in d.items()
            }
        payload = {
            "pending": _serialize_dict(self._pending),
            "sent": _serialize_dict(self._sent),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".outbox-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8"))
            os.replace(tmp_name, self.path)
        except OSError:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def enqueue(self, message: OutboxMessage) -> bool:
        """Add a pending message if its event_id is new. Returns True if added."""
        if message.event_id in self._sent or message.event_id in self._pending:
            return False
        self._pending[message.event_id] = message
        self.save()
        return True

    def mark_sent(self, event_id: str) -> bool:
        """Move a pending message to sent. Returns True if moved."""
        if event_id not in self._pending:
            return False
        msg = self._pending.pop(event_id)
        self._sent[event_id] = msg
        self.save()
        return True

    def pending(self) -> List[OutboxMessage]:
        return sorted(self._pending.values(), key=lambda m: m.timestamp)

    def sent(self) -> List[OutboxMessage]:
        return sorted(self._sent.values(), key=lambda m: m.timestamp)

    def has_event(self, event_id: str) -> bool:
        return event_id in self._pending or event_id in self._sent
