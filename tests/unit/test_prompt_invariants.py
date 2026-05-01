"""Snapshot tests for the LLM system prompt's stable invariants.

The prompt is the most fragile piece of code in the repo (no compiler,
no type checker). These tests assert that the rules we've documented in
prompts.SYSTEM_PROMPT are still present — a regression where someone
silently strips the timezone instructions or the European-format rule
breaks production classification but is otherwise invisible.

These do NOT test that the LLM follows the rules — that's the job of
`tests/integration/test_e2e_real_llm.py` (gated by RUN_LLM_TESTS=1).
They DO test that the rules are still in the prompt.
"""

from __future__ import annotations

import re

from app.llm.prompts import SYSTEM_PROMPT


def test_prompt_lists_all_canonical_state_values():
    """The discriminator enum: prompt MUST mention each state literally so
    Gemini (which doesn't enforce JSON Schema `const`) doesn't invent new ones."""
    for state in (
        "PICKED_UP",
        "IN_TRANSIT",
        "OUT_FOR_DELIVERY",
        "DELIVERED",
        "ISSUED",
        "PAID",
        "VOIDED",
        "REFUNDED",
        "UNCLASSIFIED",
    ):
        assert state in SYSTEM_PROMPT, f"prompt is missing canonical_state literal '{state}'"


def test_prompt_describes_three_classification_buckets():
    for bucket in ("shipment", "invoice", "unclassified"):
        assert re.search(rf"\b{bucket}\b", SYSTEM_PROMPT, re.IGNORECASE), (
            f"prompt does not describe the '{bucket}' bucket"
        )


def test_prompt_requires_iso_8601_utc_for_event_at():
    """Datetime normalization is the most common LLM mistake. The prompt
    MUST tell the model both the target format and the conversion rules."""
    assert "ISO-8601 UTC" in SYSTEM_PROMPT
    assert "event_at" in SYSTEM_PROMPT


def test_prompt_includes_timezone_conversion_rules():
    """If someone deletes these, classifications from non-UTC vendors silently
    drift hours off."""
    # Numeric offset conversion guidance
    assert "+08:00" in SYSTEM_PROMPT or "offset" in SYSTEM_PROMPT.lower()
    # Named-timezone conversion guidance (covers cases like 'WIB')
    assert "WIB" in SYSTEM_PROMPT or "named timezone" in SYSTEM_PROMPT.lower()


def test_prompt_includes_european_number_format_rule():
    """'EUR 24.350,75' must parse as Decimal('24350.75'), not 24.350 or 24350,75.
    This is silent corruption if dropped — the typed schema accepts both."""
    assert "European" in SYSTEM_PROMPT
    assert "24350.75" in SYSTEM_PROMPT  # the canonical example
    # also covers the US format conversion to be unambiguous
    assert "US" in SYSTEM_PROMPT


def test_prompt_includes_master_BL_preference_rule():
    """For shipments with both master and house BL, we must always pick
    master so events for the same shipment converge."""
    assert "Master BL" in SYSTEM_PROMPT or "master BL" in SYSTEM_PROMPT


def test_prompt_includes_lossless_extras_capture_instruction():
    """Vendor-specific fields not modeled by the typed schema must end up in
    `extras` so we don't lose them."""
    assert "extras" in SYSTEM_PROMPT


def test_prompt_includes_unclassified_fallback_rule():
    """If a payload looks like a shipment / invoice but a required field is
    missing, the LLM must fall back to UNCLASSIFIED with reason instead of
    inventing a value."""
    text = SYSTEM_PROMPT.lower()
    assert "unclassified" in text and "missing" in text
    assert "reason" in SYSTEM_PROMPT


def test_prompt_forbids_inventing_values_not_in_payload():
    """Hallucination guard."""
    text = SYSTEM_PROMPT.lower()
    assert ("never invent" in text) or ("do not invent" in text)


def test_prompt_includes_required_top_level_fields():
    """The flat output schema requires `canonical_state`, `vendor`, and
    `event_at` — all three must be mentioned by name so the LLM emits
    them on every classification."""
    for token in ("canonical_state", "vendor", "event_at"):
        assert token in SYSTEM_PROMPT, f"prompt is missing required field reference '{token}'"
