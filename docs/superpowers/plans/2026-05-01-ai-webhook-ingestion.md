# AI Webhook Ingestion — Implementation Plan

**Goal:** Build the service designed in `docs/superpowers/specs/2026-05-01-ai-webhook-ingestion-design.md`.
**Tech:** Python 3.12, FastAPI, SQLAlchemy 2 async, Alembic, Postgres 16, LangChain, uv, pytest, Docker Compose.

## Build order

1. Scaffold: `pyproject.toml` (uv), `.gitignore`, `.env.example`, `docker-compose.yml`, `Dockerfile`, `.pre-commit-config.yaml`, `alembic.ini`.
2. `app/config.py` — settings via pydantic-settings.
3. `app/db.py` — async engine, session factory.
4. `app/models.py` — SQLAlchemy models (raw_events, shipments, invoices, *_events).
5. Alembic initial migration `0001_initial.py`.
6. `app/ingestion/dedupe.py` + unit tests.
7. `app/schemas.py` — `ClassificationResult`, API request/response models.
8. `app/llm/prompts.py` — system prompt, schema, few-shots.
9. `app/llm/__init__.py` — `build_llm()` factory.
10. `app/llm/classifier.py` — `Classifier` wrapper.
11. `app/normalization/persist.py` — `persist_normalized()` transaction.
12. `app/ingestion/store.py` — raw_events insert helper.
13. `app/api/__init__.py` + routers (`webhooks.py`, `entities.py`, `health.py`).
14. `app/worker/main.py` — claim loop + reaper + entrypoint.
15. Tests:
    - `tests/test_dedupe.py` (unit)
    - `tests/test_ingestion.py` (API + DB via testcontainers, fake LLM)
    - `tests/test_persist.py` (out-of-order, idempotency)
    - `tests/test_e2e.py` (gated, real LLM, all 6 sample payloads)
16. README.md (the deliverable architecture doc).
17. Smoke-test via docker compose.
