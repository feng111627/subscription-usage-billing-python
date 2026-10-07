"""HTTP 幂等键存储。

语义：
- 同一 actor 使用相同 ``Idempotency-Key`` 重放请求时，返回首次响应，
  不会再次执行业务（不重复计费/扣款）；
- 相同幂等键但请求指纹（方法+路径+载荷摘要）不同，视为冲突，
  抛出 IDEMPOTENCY_CONFLICT（409），避免“同键不同单”；
- 幂等键按 actor 隔离，不同用户的同名键互不影响。
"""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from typing import Any

IdemKey2 = tuple[str, str]  # (actor_id, key)


def fingerprint(method: str, path: str, payload: dict[str, Any] | None) -> str:
    raw = json.dumps(
        {"m": method, "p": path, "b": payload or {}},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class IdempotencyRecord:
    key: str
    actor_id: str
    fingerprint: str
    response: Any
    status_code: int = 200


class IdempotencyStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._records: dict[IdemKey2, IdempotencyRecord] = {}

    @staticmethod
    def _k(actor_id: str, key: str) -> IdemKey2:
        return (actor_id, key)

    def get(self, actor_id: str, key: str) -> IdempotencyRecord | None:
        with self._lock:
            return self._records.get(self._k(actor_id, key))

    def save(self, record: IdempotencyRecord) -> None:
        with self._lock:
            self._records[self._k(record.actor_id, record.key)] = record
