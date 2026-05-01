"""Unit tests for the canonical-form content hash."""

from __future__ import annotations

import re

from app.utils.hashing import canonical_hash

HEX64 = re.compile(r"^[0-9a-f]{64}$")


def test_returns_64_char_hex():
    assert HEX64.match(canonical_hash({"a": 1}))


def test_is_deterministic():
    payload = {"event_msg_id": "X", "nested": {"a": 1, "b": [2, 3]}}
    assert canonical_hash(payload) == canonical_hash(payload)


def test_is_key_order_independent():
    a = {"a": 1, "b": [{"x": 1, "y": 2}]}
    b = {"b": [{"y": 2, "x": 1}], "a": 1}
    assert canonical_hash(a) == canonical_hash(b)


def test_diverges_for_different_leaf_values():
    a = {"event_msg_id": "X", "milestone": "loaded"}
    b = {"event_msg_id": "X", "milestone": "delivered"}
    assert canonical_hash(a) != canonical_hash(b)


def test_diverges_when_a_key_is_added():
    a = {"event_msg_id": "X"}
    b = {"event_msg_id": "X", "sent_at": "2026-04-21T00:00:00Z"}
    assert canonical_hash(a) != canonical_hash(b)


def test_handles_unicode_in_values():
    a = {"memo": "Shanghai → Hamburg"}
    b = {"memo": "Shanghai → Hamburg"}
    assert canonical_hash(a) == canonical_hash(b)


def test_distinguishes_two_events_sharing_entity_ref():
    """The critical invariant: two genuinely different events MUST hash differently
    even when they share an entity reference like doc_ref or MBL."""
    issued = {
        "doc_ref": "GFP-INV-2026-Q2-08821",
        "transaction": {"kind": "freight invoice raised", "issued_at": "2026-04-15"},
    }
    paid = {
        "doc_ref": "GFP-INV-2026-Q2-08821",
        "transaction": {"kind": "settled in full", "settled_at": "2026-04-22"},
    }
    assert canonical_hash(issued) != canonical_hash(paid)


def test_handles_empty_dict():
    h = canonical_hash({})
    assert HEX64.match(h)


def test_distinguishes_payloads_with_different_nested_lists():
    a = {"items": [1, 2, 3]}
    b = {"items": [1, 2, 3, 4]}
    assert canonical_hash(a) != canonical_hash(b)
