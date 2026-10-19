"""Append-only, hash-chained audit log.

Every event stores the hash of the previous event, so editing or deleting any
past line breaks verification from that point on. One JSON object per line.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

GENESIS = "0" * 64


def _hash(record: dict[str, Any]) -> str:
    body = json.dumps(record, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


class AuditLog:
    def __init__(self, path: str | Path, clock: Callable[[], datetime] | None = None) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def events(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def append(self, case_id: str, actor: str, event: str, payload: dict[str, Any] | None = None) -> dict:
        prior = self.events()
        record = {
            "seq": len(prior) + 1,
            "ts": self._clock().isoformat(),
            "case_id": case_id,
            "actor": actor,
            "event": event,
            "payload": payload or {},
            "prev_hash": prior[-1]["hash"] if prior else GENESIS,
        }
        record["hash"] = _hash(record)
        with self.path.open("a") as f:
            f.write(json.dumps(record, sort_keys=True) + "\n")
        return record

    def verify(self) -> tuple[bool, str]:
        prev = GENESIS
        for i, rec in enumerate(self.events(), 1):
            claimed = rec.get("hash")
            body = {k: v for k, v in rec.items() if k != "hash"}
            if rec.get("seq") != i:
                return False, f"sequence break at line {i}"
            if rec.get("prev_hash") != prev:
                return False, f"chain break at seq {i}"
            if _hash(body) != claimed:
                return False, f"record tampered at seq {i}"
            prev = claimed
        return True, "ok"

    def for_case(self, case_id: str) -> list[dict[str, Any]]:
        return [e for e in self.events() if e["case_id"] == case_id]
