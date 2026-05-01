"""End-to-end test against a real LLM.

Gated by RUN_LLM_TESTS=1 — these tests cost tokens and require network.

Asserts on the six appendix payloads from the assignment:
  - Maersk vessel-departed       -> shipment / IN_TRANSIT
  - Maersk gate-in               -> shipment / PICKED_UP   (links to same entity)
  - GFP "settled in full"        -> invoice / PAID
  - GFP "freight invoice raised" -> invoice / ISSUED       (links to same entity)
  - ONE "released to consignee"  -> shipment / DELIVERED
  - Marine traffic advisory      -> unclassified
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import session_scope
from app.core.models import Invoice, RawEvent, Shipment
from app.llm import build_llm
from app.llm.classifier import Classifier
from app.services.normalization import persist_normalized

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.e2e,
    pytest.mark.skipif(os.getenv("RUN_LLM_TESTS") != "1", reason="set RUN_LLM_TESTS=1 to run"),
]


SAMPLE_PAYLOADS = {
    "maersk_in_transit": {
        "carrier_scac": "MAEU",
        "event_msg_id": "MAEU-EVT-2026-04-22-0001",
        "transport_doc": {"type": "MBL", "number": "MAEU240498712"},
        "container": "MSKU7748112",
        "vessel": {"name": "MAERSK GUATEMALA", "imo": "9778120", "voyage": "424W"},
        "milestone": "Loaded onboard and sailed",
        "milestone_at": "2026-04-21T22:47:00+08:00",
        "port": {"code": "CNSHA", "name": "Shanghai"},
    },
    "maersk_picked_up": {
        "carrier_scac": "MAEU",
        "event_msg_id": "MAEU-EVT-2026-04-19-0042",
        "transport_doc": {"type": "MBL", "number": "MAEU240498712"},
        "container": "MSKU7748112",
        "milestone": "Empty container released to shipper; full container received at origin terminal",
        "milestone_at": "2026-04-19T11:15:00+08:00",
        "port": {"code": "CNSHA", "name": "Shanghai"},
        "shipper_ref": "ACME-IND-PO-2026-9921",
    },
    "gfp_paid": {
        "source": "globalfreightpay.api",
        "channel": "carrier_billing",
        "doc_ref": "GFP-INV-2026-Q2-08821",
        "carrier": "Hapag-Lloyd AG",
        "linked_bl": "HLCU2604OCEAN221",
        "transaction": {
            "kind": "settled in full",
            "settled_at": "2026-04-22 18:47:11+02:00",
            "amount": "EUR 24.350,75",
            "remitter": "ACME Logistics GmbH",
            "memo": "Ocean freight + THC + BAF, Shanghai → Hamburg, container HLBU4490221",
        },
    },
    "gfp_issued": {
        "source": "globalfreightpay.api",
        "channel": "carrier_billing",
        "doc_ref": "GFP-INV-2026-Q2-08821",
        "carrier": "Hapag-Lloyd AG",
        "linked_bl": "HLCU2604OCEAN221",
        "transaction": {
            "kind": "freight invoice raised",
            "issued_at": "2026-04-15T09:00:00+02:00",
            "amount": "EUR 24.350,75",
            "due_at": "2026-05-15T00:00:00+02:00",
            "line_items": [
                {"desc": "Ocean freight Shanghai → Hamburg", "amt": "EUR 21.000,00"},
                {"desc": "Terminal handling charges (THC)", "amt": "EUR 1.850,75"},
                {"desc": "Bunker adjustment factor (BAF)", "amt": "EUR 1.500,00"},
            ],
        },
    },
    "one_delivered": {
        "carrier": "Ocean Network Express",
        "carrier_scac": "ONEY",
        "event_id": "ONE-2026-04-28-114",
        "house_bl": "ONEYJKTHKG2604113",
        "master_bl": "ONEYMBLHKG260499",
        "container_no": "TLLU2890442",
        "consignee": "ACME Manufacturing PT.",
        "milestone_text": "Cargo released to consignee at consignee facility — empty container returned to depot",
        "milestone_local_time": "28/04/2026 09:42 WIB",
        "port_of_discharge": "IDJKT",
        "delivery_order_no": "DO-IDJKT-26044881",
    },
    "advisory_unclassified": {
        "issuer": "marine-traffic-advisory",
        "advisory_id": "MTA-2026-04-26-EU-007",
        "severity": "AMBER",
        "issued_at": "2026-04-26T06:00:00Z",
        "subject": "Ongoing congestion at Port of Antwerp-Bruges",
        "body": "Vessel waiting times at Antwerp-Bruges berths have increased to 4-6 days due to labour action.",
        "affected_services": ["AE7", "FAL3", "Mediterranean Bridge"],
        "expires_at": "2026-05-03T00:00:00Z",
    },
}


async def _seed(payload: dict, _key: str) -> RawEvent:
    from app.utils.hashing import canonical_hash

    async with session_scope() as session:
        re = RawEvent(payload=payload, vendor_hint="test", hash_exact=canonical_hash(payload))
        session.add(re)
        await session.flush()
        await session.refresh(re)
        return re


async def test_e2e_six_sample_payloads():
    settings = get_settings()
    if not settings.google_api_key:
        pytest.skip("GOOGLE_API_KEY not set")
    classifier = Classifier(build_llm(settings))

    raws = {name: await _seed(p, f"e2e:{name}") for name, p in SAMPLE_PAYLOADS.items()}

    for name, payload in SAMPLE_PAYLOADS.items():
        result = await classifier.classify(payload)
        async with session_scope() as session:
            row = await session.get(RawEvent, raws[name].id)
            assert row is not None
            await persist_normalized(session, row, result)

    async with session_scope() as session:
        ships = (await session.execute(select(Shipment))).scalars().all()
        invs = (await session.execute(select(Invoice))).scalars().all()

        # 2 Maersk events + 1 ONE event = 2 shipments (Maersk events link).
        assert len(ships) == 2
        # 2 GFP events = 1 invoice.
        assert len(invs) == 1

        maersk = next(s for s in ships if s.external_ref == "MAEU240498712")
        assert maersk.current_state == "IN_TRANSIT"
        # Vessel-departed (2026-04-21) is later than gate-in (2026-04-19).
        assert maersk.last_event_at is not None
        assert maersk.last_event_at > datetime(2026, 4, 19, tzinfo=UTC)

        one = next(s for s in ships if s.external_ref != "MAEU240498712")
        assert one.current_state == "DELIVERED"

        gfp = invs[0]
        assert gfp.current_state == "PAID"
        assert gfp.external_ref == "GFP-INV-2026-Q2-08821"
