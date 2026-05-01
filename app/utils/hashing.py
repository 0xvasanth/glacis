"""Content hashing for byte-identical duplicate detection.

`canonical_hash` returns SHA-256 of the canonical-form JSON (sorted keys,
no whitespace). Two payloads with byte-identical content (modulo key
order and whitespace) produce the same hash. Two payloads that differ in
any leaf value or key produce different hashes.

The function is pure, deterministic, runs in O(payload size), and
completes in low microseconds for typical payloads. Output is a 64-char
lowercase hex string.

This is the only dedup signal we use. The dominant cause of duplicate
webhook delivery is HTTP-layer retry (timeout / 5xx), which re-sends the
exact same body — and that's what this catches. Anything subtler (vendor
stamps a fresh `sent_at` per attempt, etc.) is consciously left to be
caught later in the audit trail rather than in the hot path.
"""

from __future__ import annotations

import hashlib
from typing import Any

import orjson


def canonical_hash(payload: Any) -> str:
    """SHA-256 of `payload` after canonical JSON serialization."""
    serialized = orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)
    return hashlib.sha256(serialized).hexdigest()
