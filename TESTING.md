# How to test

Three layers, each runnable independently. All commands assume `uv` is
installed and the repo is checked out.

```bash
# one-time
cp .env.example .env
# put your real ANTHROPIC_API_KEY in .env (only needed for the live LLM tests)
uv sync --extra dev
```

---

## 1. Static checks (fast, no DB, no network)

```bash
uv run ruff check app tests          # lint
uv run ruff format --check app tests # formatting
uv run mypy app                      # strict type-check
```

All three should print "ok" / "Success". Failure here is the cheapest
signal — fix before anything else.

---

## 2. Unit tests (no DB, no network)

```bash
uv run pytest tests/unit -v
```

Covers content hashing, the typed event schema (per-state required
fields, discriminator dispatch, `extras` capture), and prompt
invariants (verifies the LLM prompt still mentions all 9 canonical
states, ISO-8601 UTC, European-format parsing, Master-BL preference,
the `extras` rule, etc.).

Expected: ~40 tests, all pass in <0.1 s.

---

## 3. Integration tests (Postgres via testcontainers, no LLM)

```bash
uv run pytest tests/integration -v
```

Boots an ephemeral Postgres in Docker (via testcontainers) and
exercises:

- `POST /api/v1/webhooks/{vendor}` — accept/dedupe + validation
- Concurrent identical POSTs — partial-UNIQUE backstop, exactly one primary
- `GET /api/v1/shipments|invoices/{id}` — entity reads with full event history
- `POST /api/v1/raw-events/{id}/retry` — operator retry endpoint (404 / 409 / 200)
- Worker loop: claim → classify (scripted, no LLM) → persist
- `FOR UPDATE SKIP LOCKED` concurrent-claim correctness
- Stale-claim reaper recovery
- DLQ semantics: `attempts` only increments on real LLM failure (not crashes); terminal `failed` status invisible to the claim queue
- Out-of-order ordering: the full per-state lifecycle, REVERSE arrival, skip states, late-event-doesn't-regress

Expected: ~70 tests, run in 5–10 s. Docker daemon required.

---

## 4. Live LLM end-to-end (real Anthropic API call)

Gated by `RUN_LLM_TESTS=1` because it costs tokens. Hits the real
Anthropic API with the 6 sample payloads from the assignment, runs
them through the full classify + persist pipeline, and asserts the
resulting entities.

```bash
# Set the key once in your shell (or already in .env)
export ANTHROPIC_API_KEY=sk-ant-...

# run
RUN_LLM_TESTS=1 uv run pytest tests/integration/test_e2e_real_llm.py -v
```

Expected: 1 test, 25–45 s end-to-end, asserts:

- 2 Maersk events → 1 shipment, `current_state=IN_TRANSIT`, history has both `PICKED_UP` and `IN_TRANSIT` rows
- 1 ONE event → 1 shipment, `current_state=DELIVERED` (with WIB → UTC time conversion)
- 2 GFP events → 1 invoice, `current_state=PAID`, history has both `ISSUED` and `PAID` rows (with European number format parsed)
- 1 marine traffic advisory → unclassified (no entity created)

To pick a different Claude model:
```bash
LLM_MODEL=claude-sonnet-4-6 RUN_LLM_TESTS=1 uv run pytest tests/integration/test_e2e_real_llm.py -v
LLM_MODEL=claude-opus-4-7   RUN_LLM_TESTS=1 uv run pytest tests/integration/test_e2e_real_llm.py -v
```

Note on Haiku (`claude-haiku-4-5`): tool calls occasionally return the
nested `payload` field as a JSON-encoded string instead of an object,
which breaks Pydantic dispatch. Sonnet returns the proper nested
structure. Stick with Sonnet (the default) for this test.

---

## 5. Whole suite in one shot

```bash
uv run pytest tests/ -v                               # offline (skips the LLM-gated test)
RUN_LLM_TESTS=1 uv run pytest tests/ -v               # everything including the live LLM call
```

---

## 6. Live `docker compose` smoke test

This runs the actual prod-shaped stack (api + worker + postgres) and
exercises every endpoint with curl, with the worker calling the real
Anthropic API for classification.

```bash
# Start the stack (reads .env automatically)
docker compose up --build -d

# Wait for the API to become ready
until curl -sf http://localhost:8000/healthz >/dev/null 2>&1; do sleep 1; done && echo OK

# Send the 6 sample payloads
declare -A vendors=(
  [maersk_in_transit]=maersk
  [maersk_picked_up]=maersk
  [gfp_paid]=gfp
  [gfp_issued]=gfp
  [one_delivered]=oney
  [advisory_unclassified]=mta
)
for f in "${!vendors[@]}"; do
  curl -sX POST "http://localhost:8000/api/v1/webhooks/${vendors[$f]}" \
       -H 'content-type: application/json' \
       -d @samples/$f.json
  echo
done

# Re-fire one to verify duplicate detection
curl -sX POST http://localhost:8000/api/v1/webhooks/maersk \
     -H 'content-type: application/json' \
     -d @samples/maersk_in_transit.json

# Wait for the worker to process all 6 primaries
until [ "$(docker compose exec -T postgres psql -U glacis -d glacis -tAc \
        "SELECT count(*) FROM raw_events WHERE duplicate_of_id IS NULL AND status='processed'")" = "6" ]; do
  sleep 2
done
echo "all classified"

# Inspect the entities Claude produced
docker compose exec -T postgres psql -U glacis -d glacis -c \
  "SELECT vendor, external_ref, current_state, last_event_at FROM shipments;"
docker compose exec -T postgres psql -U glacis -d glacis -c \
  "SELECT vendor, external_ref, current_state, last_event_at FROM invoices;"

# Read an entity through the API
SHIP_ID=$(docker compose exec -T postgres psql -U glacis -d glacis -tAc \
          "SELECT id FROM shipments WHERE vendor='maersk';" | tr -d '[:space:]')
curl -s "http://localhost:8000/api/v1/shipments/$SHIP_ID" | python3 -m json.tool

# Tear down
docker compose down -v
```
