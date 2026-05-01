# AI Webhook Ingestion Service — Design

**Status:** Approved for implementation
**Date:** 2026-05-01
**Context:** Glacis take-home interview, ~3-hour budget
**Stack:** Python 3.12 · FastAPI · SQLAlchemy 2 (async) · Alembic · Postgres 16 · LangChain (OpenRouter for dev, Anthropic for prod) · Docker Compose · uv · pytest

---

## 1. Problem Summary

Ingest arbitrary vendor webhook payloads, classify each as `shipment` / `invoice` / `unclassified`, normalize vendor-specific vocabulary into canonical entity states using an LLM, and persist results — while satisfying:

- **Sub-second acknowledgement** to the vendor.
- **Idempotency** under repeated delivery of the same event.
- **Correctness** under out-of-order arrival.

Canonical states:

- Shipment: `PICKED_UP → IN_TRANSIT → OUT_FOR_DELIVERY → DELIVERED`
- Invoice: `ISSUED → PAID`, alternative terminals `VOIDED`, `REFUNDED`

---

## 2. Architecture

```
┌────────────┐   POST /api/v1/webhooks/{vendor}    ┌─────────────────┐
│  Vendor    │ ───────────────────────────────────►│  FastAPI app    │
└────────────┘  ◄──────── 202 Accepted ────────── │  (ingest only)  │
                          (sub-second)             └────────┬────────┘
                                                            │ INSERT raw_events
                                                            ▼
                                                   ┌─────────────────┐
                                                   │   Postgres      │
                                                   │  raw_events,    │
                                                   │  shipments,     │
                                                   │  invoices,      │
                                                   │  *_events       │
                                                   └────────▲────────┘
                                                            │ SELECT … FOR UPDATE SKIP LOCKED
                                                   ┌────────┴────────┐
                                                   │  Worker process │──► LLM
                                                   │  (asyncio loop) │   (OpenRouter / Anthropic)
                                                   └─────────────────┘
```

Two processes in `docker-compose.yml`, sharing one codebase and one Postgres:

- **`api`** — uvicorn + FastAPI. Synchronous request path: read body, compute dedupe key, insert into `raw_events`, return `202`. **No LLM call here.**
- **`worker`** — long-running Python process. Polls `raw_events` for `pending` rows, calls the LLM, normalizes, persists. N=1 in the demo; the `SELECT … FOR UPDATE SKIP LOCKED` pattern makes N>1 safe.

### Why split api/worker?
- Vendors expect <1s ack; LLM calls take 1–3s. The split lets the API stay fast.
- Worker can be scaled / restarted independently of the public surface.
- Postgres-as-queue avoids extra infrastructure (no Redis/RabbitMQ) while preserving durability and exactly-once semantics within a single transaction.

---

## 3. Data Model

Five tables. Defined via SQLAlchemy 2 ORM models, migrated with Alembic.

### 3.1 `raw_events` — durable inbox, append-only

```sql
id              UUID PK
dedupe_key      TEXT UNIQUE NOT NULL    -- vendor-provided id, fallback sha256(canonical_json)
vendor_hint     TEXT NOT NULL           -- from URL path (e.g. "maersk"); informational only, distinct from `vendor` on entities (which is the LLM-extracted canonical identifier, e.g. "MAEU")
payload         JSONB NOT NULL
received_at     TIMESTAMPTZ NOT NULL DEFAULT now()
status          TEXT NOT NULL           -- 'pending' | 'processing' | 'processed' | 'failed'
attempts        INT NOT NULL DEFAULT 0
last_error      TEXT
locked_at       TIMESTAMPTZ
processed_at    TIMESTAMPTZ
INDEX (status, received_at) WHERE status = 'pending'
```

### 3.2 `shipments` — entity, materialized "current" view

```sql
id                  UUID PK
vendor              TEXT NOT NULL
external_ref        TEXT NOT NULL                -- MBL / house BL / etc.
current_state       TEXT                         -- canonical
last_event_at       TIMESTAMPTZ
attributes          JSONB NOT NULL DEFAULT '{}'  -- container, vessel, ports, …
created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
UNIQUE (vendor, external_ref)
```

### 3.3 `invoices` — same shape

```sql
id                  UUID PK
vendor              TEXT NOT NULL
external_ref        TEXT NOT NULL
current_state       TEXT                         -- ISSUED | PAID | VOIDED | REFUNDED
last_event_at       TIMESTAMPTZ
attributes          JSONB NOT NULL DEFAULT '{}'  -- amount, currency, line_items, …
created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
UNIQUE (vendor, external_ref)
```

### 3.4 `shipment_events` / `invoice_events` — full event history per entity

```sql
id                UUID PK
entity_id         UUID FK → shipments.id (or invoices.id), ON DELETE CASCADE
raw_event_id      UUID FK → raw_events.id, UNIQUE
canonical_state   TEXT NOT NULL
event_at          TIMESTAMPTZ NOT NULL
vendor_milestone  TEXT                  -- original vendor wording, audit trail
attributes        JSONB NOT NULL DEFAULT '{}'
created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
INDEX (entity_id, event_at DESC)
```

**Why two event tables instead of one polymorphic table?** Cleaner FKs, no nullable parent columns, simpler queries. Trade-off acceptable for two entity types.

### 3.5 Idempotency layers

1. **`raw_events.dedupe_key UNIQUE`** — repeated delivery of the same payload by a vendor results in one row.
2. **`*_events.raw_event_id UNIQUE`** — even if a raw event were processed twice (worker crash mid-transaction), only one normalized event ever exists.
3. **Conditional `UPDATE` on entity** (see §6.3 step 4) — late-arriving events do not overwrite newer state.

---

## 4. API

All paths under `/api/v1` except health.

### `POST /api/v1/webhooks/{vendor}`

- **Body:** any JSON.
- **Behavior:**
  1. Compute `dedupe_key` — try common vendor id fields (`event_msg_id`, `event_id`, `advisory_id`, `doc_ref`, `id`); fall back to SHA-256 of the canonical-form JSON (sorted keys, no whitespace).
  2. `INSERT INTO raw_events ON CONFLICT (dedupe_key) DO NOTHING RETURNING id`.
  3. Respond `202 {"raw_event_id": "...", "duplicate": false}` on insert, `202 {"raw_event_id": "...", "duplicate": true}` on conflict (look up existing id).
- **Latency target:** p99 < 100 ms. One INSERT, no LLM, no external I/O.
- **Why 202 on duplicate (not 409):** vendors retry on non-2xx, and we explicitly want them to stop retrying.

### `GET /api/v1/entities/{entity_type}/{id}`

`entity_type` ∈ `shipments | invoices`. Returns the entity row plus its event history ordered by `event_at`. Used by the README walkthrough and tests.

### `GET /healthz`

Returns 200 if the process is up and Postgres is reachable. Stays at root — convention for k8s/compose probes.

---

## 5. LLM Layer

Built on **LangChain** for a free provider-swap.

### 5.1 Classifier

```python
# app/llm/classifier.py
class ClassificationResult(BaseModel):
    entity_type: Literal["shipment", "invoice", "unclassified"]
    vendor: str                       # extracted vendor identifier (e.g. "MAEU", "globalfreightpay")
    external_ref: str | None          # natural key (None for unclassified)
    canonical_state: str | None       # PICKED_UP / … / ISSUED / …
    event_at: datetime | None         # vendor's event timestamp, normalized to UTC
    vendor_milestone: str | None      # original vendor wording
    attributes: dict                  # extracted entity attributes
    confidence: float                 # 0..1, self-reported

class Classifier:
    def __init__(self, llm: BaseChatModel):
        self._llm = llm.with_structured_output(ClassificationResult)

    async def classify(self, payload: dict) -> ClassificationResult:
        return await self._llm.ainvoke([
            SystemMessage(content=SYSTEM_PROMPT_BLOCKS),
            HumanMessage(content=json.dumps(payload, sort_keys=True)),
        ])
```

### 5.2 Provider factory

```python
# app/llm/__init__.py
def build_llm(settings) -> BaseChatModel:
    if settings.llm_provider == "openrouter":
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            model=settings.llm_model,                 # default: meta-llama/llama-3.3-70b-instruct:free
            base_url="https://openrouter.ai/api/v1",
            api_key=settings.openrouter_api_key,
            temperature=0,
            timeout=30,
            max_retries=2,
        )
    if settings.llm_provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        return ChatAnthropic(
            model=settings.llm_model,                 # claude-sonnet-4-6
            api_key=settings.anthropic_api_key,
            temperature=0,
            timeout=30,
            max_retries=2,
        )
    raise ValueError(f"unknown LLM_PROVIDER: {settings.llm_provider}")
```

Selected via `LLM_PROVIDER` env var (`openrouter` for dev, `anthropic` for prod).

### 5.3 Prompt

Single system prompt containing:
1. Role: "You normalize vendor webhook payloads into a canonical schema."
2. Both canonical state vocabularies, verbatim.
3. The output JSON schema (matches `ClassificationResult`).
4. 3–4 few-shot examples covering shipment, invoice, unclassified.
5. Rules: "If unsure, set `entity_type=unclassified`. Never invent fields. `event_at` must be ISO-8601 UTC. Choose the most stable identifier as `external_ref` (prefer MBL > house BL > container; for invoices use the document reference)."

For Anthropic, the system prompt is sent as a structured block with `cache_control: ephemeral` so prompt caching is active in production. LangChain forwards the block to the Anthropic API. For OpenRouter, the same content is sent as a plain string.

### 5.4 Resilience

- LangChain client configured with `timeout=30`, `max_retries=2`.
- On final failure, the worker marks the raw event `failed` (see §6).

---

## 6. Worker

Single asyncio loop. Run as its own container in compose.

### 6.1 Loop

```python
async def run():
    classifier = Classifier(build_llm(settings))
    while not shutdown.is_set():
        async with session() as s:
            raw = await claim_one(s)
        if raw is None:
            await asyncio.sleep(POLL_INTERVAL_S)        # 1s when idle
            continue
        try:
            result = await classifier.classify(raw.payload)
            async with session() as s:
                await persist_normalized(s, raw, result)
        except Exception as e:
            await mark_failed(raw.id, e)
```

### 6.2 Atomic claim

```sql
UPDATE raw_events
SET status='processing', locked_at=now(), attempts=attempts+1
WHERE id = (
  SELECT id FROM raw_events
  WHERE status='pending' AND attempts < 5
  ORDER BY received_at
  FOR UPDATE SKIP LOCKED
  LIMIT 1
)
RETURNING *;
```

`SKIP LOCKED` guarantees multiple workers never claim the same row.

### 6.3 `persist_normalized` — single transaction

1. If `entity_type == 'unclassified'`: mark `raw_event.status = 'processed'`, return.
2. Otherwise upsert the entity:

   ```sql
   INSERT INTO {shipments|invoices} (vendor, external_ref, attributes)
   VALUES (...)
   ON CONFLICT (vendor, external_ref)
   DO UPDATE SET updated_at = now()
   RETURNING id;
   ```

3. Insert the normalized event, deduped by `raw_event_id`:

   ```sql
   INSERT INTO {shipment|invoice}_events (entity_id, raw_event_id, canonical_state, event_at, vendor_milestone, attributes)
   VALUES (...)
   ON CONFLICT (raw_event_id) DO NOTHING;
   ```

4. Conditionally update entity state — **the out-of-order guard**:

   ```sql
   UPDATE {shipments|invoices}
   SET current_state = $new_state,
       last_event_at = $event_at,
       attributes    = attributes || $new_attrs,
       updated_at    = now()
   WHERE id = $entity_id
     AND ($event_at > last_event_at OR last_event_at IS NULL);
   ```

5. `UPDATE raw_events SET status='processed', processed_at=now() WHERE id=…`.

All five steps in one transaction. Atomic.

### 6.4 Failure handling

- **Transient errors** (timeout, 5xx, DB serialization): the `try/except` calls `mark_failed`, which sets `status='pending'` again and stores `last_error`. Next claim re-attempts. Counter is already incremented by `claim_one`.
- **Worker crash mid-LLM-call:** because `claim_one` commits before the LLM call (we don't want to hold a DB transaction open across a 1–3s network call), a hard crash leaves the row in `status='processing'` with no holder. **Stale-claim reaper:** at worker startup and every 60s thereafter, run `UPDATE raw_events SET status='pending' WHERE status='processing' AND locked_at < now() - interval '5 minutes'`. The 5-minute window is comfortably greater than the 30-second LLM timeout × 3 attempts, so it cannot reclaim live work.
- **After 5 attempts:** row stays `status='processing'` with `attempts=5` — effectively a dead-letter queue. A `GET /api/v1/admin/dlq` endpoint surfaces them (stretch).
- **Graceful shutdown:** SIGTERM sets the `shutdown` event. The current iteration finishes (commit or rollback), then the loop exits. Compose `stop_grace_period: 30s`.

---

## 7. Project Structure

```
glacis/
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml                  # uv-managed
├── uv.lock                         # committed
├── .pre-commit-config.yaml         # ruff, mypy, uv lock --check, unit tests, hygiene
├── README.md                       # the architecture doc (deliverable)
├── .env.example
├── alembic.ini
├── alembic/
│   ├── env.py
│   └── versions/
│       └── 0001_initial.py
├── app/
│   ├── __init__.py
│   ├── config.py                   # pydantic-settings
│   ├── db.py                       # async engine, session factory
│   ├── models.py                   # SQLAlchemy ORM models
│   ├── schemas.py                  # Pydantic API + ClassificationResult
│   ├── api/
│   │   ├── __init__.py             # FastAPI app factory
│   │   ├── webhooks.py             # POST /api/v1/webhooks/{vendor}
│   │   ├── entities.py             # GET  /api/v1/entities/{type}/{id}
│   │   └── health.py               # GET  /healthz
│   ├── ingestion/
│   │   ├── dedupe.py               # extract vendor id or hash payload
│   │   └── store.py                # raw_events insert
│   ├── llm/
│   │   ├── __init__.py             # build_llm() factory
│   │   ├── classifier.py           # Classifier wrapper
│   │   └── prompts.py              # SYSTEM_PROMPT, few-shot examples
│   ├── normalization/
│   │   └── persist.py              # persist_normalized() — the transaction
│   └── worker/
│       ├── __init__.py
│       └── main.py                 # claim loop + entrypoint
└── tests/
    ├── conftest.py                 # pytest-asyncio + testcontainers Postgres
    ├── test_dedupe.py
    ├── test_ingestion.py
    ├── test_persist.py             # out-of-order events derive correct state
    ├── test_classifier_fake.py     # FakeListChatModel for deterministic runs
    └── test_e2e.py                 # all 6 sample payloads via real LLM, gated by RUN_LLM_TESTS=1
```

Clear seams: **ingestion** is sync, fast, no LLM. **Normalization** is the LLM call + the transactional persist. **Worker** is the loop that drives normalization.

---

## 8. Testing

- **Unit (no DB):** dedupe key extraction, prompt rendering.
- **Integration (testcontainers Postgres, fake LLM via `FakeListChatModel`):**
  - Same payload twice → second `POST` returns `202 {duplicate: true}`, exactly one `raw_events` row.
  - Out-of-order events for one entity → `current_state` reflects the event with the latest `event_at`, regardless of insertion order.
  - Two concurrent workers contending on one pending row → exactly one processes it (`SKIP LOCKED` proof).
  - `unclassified` payload → no entity created, raw event marked processed.
- **End-to-end (gated by `RUN_LLM_TESTS=1`):** the six appendix payloads through the real LLM. Asserts: the two Maersk events link to one shipment with the correct state progression; the two GFP events link to one invoice; the marine advisory is `unclassified`.

---

## 9. Out of Scope (called out in README roadmap)

- HMAC signature verification per vendor.
- Auth on the API (assignment doesn't ask).
- Multi-key entity correlation (master BL ↔ house BL ↔ container linking when a vendor switches references mid-lifecycle).
- DLQ admin UI beyond a simple GET endpoint.
- Metrics / tracing (OpenTelemetry).
- Horizontal worker autoscaling, backpressure.
- Per-vendor extractor fallback when LLM costs/latency are unacceptable.

---

## 10. Production Roadmap (README sketch)

1. **Replace Postgres-as-queue with Kafka or SQS** once event volume exceeds a few hundred per second per partition.
2. **Multi-key correlation** — store all candidate references the LLM sees; entity matching becomes a graph problem.
3. **Per-vendor schema fast paths** — cache LLM classifications; promote stable mappings to deterministic parsers; LLM becomes the fallback.
4. **HMAC verification** at the edge, per-vendor secret rotation.
5. **Observability** — OpenTelemetry traces from webhook → worker → LLM → DB; metrics on classification confidence, dedupe rate, DLQ depth.
6. **Replay tooling** — re-classify historical raw events when prompts/models change, without losing the audit trail.

---

## 11. Tooling

### 11.1 Project management — `uv`

- `pyproject.toml` is the single source of truth for deps. `uv.lock` is committed.
- `uv sync` installs the project + dev deps in a `.venv`.
- `uv run pytest`, `uv run alembic upgrade head`, `uv run uvicorn app.api:app --reload`, `uv run python -m app.worker.main` for everything.
- The `Dockerfile` uses the official `ghcr.io/astral-sh/uv` builder stage to install deps from the lockfile, then copies the resolved `.venv` into a slim runtime image. Reproducible, fast cold builds.

### 11.2 Pre-commit hooks

`.pre-commit-config.yaml` runs on every commit. The list, in order:

1. **`ruff check --fix`** — lint + import sort + autofixes.
2. **`ruff format`** — formatting.
3. **`mypy app/`** — strict typing on the application code (tests excluded for speed).
4. **`uv lock --check`** — fail if `pyproject.toml` and `uv.lock` are out of sync.
5. **`pytest tests/unit -q`** — only unit tests (no DB/network), so the hook stays fast (<5s).
6. Standard hygiene: trailing whitespace, end-of-file newline, no large files, valid YAML/TOML, no merge conflict markers, no committed secrets (`detect-secrets`).

Integration tests (Postgres via testcontainers) and the LLM e2e test are NOT in the pre-commit hook — they belong in CI. The hook must stay fast enough that developers actually let it run.

CI mirrors the hook plus runs the full integration suite. The user's `commit-commands:commit` skill is expected to invoke pre-commit; commits that bypass hooks (`--no-verify`) are explicitly disallowed by the user's global rules.
