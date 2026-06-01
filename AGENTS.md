# AGENTS.md

## Project

This is the SAGARD Portfolio Intelligence POC. It is a local-first app for processing private-company PDF reporting packages into structured, sector-specific analysis.

The product workflow:

1. Users drop company PDF packages into `data/input/<company_slug>/`.
2. The frontend triggers local folder sync.
3. The backend registers new company folders and PDFs in SQLite.
4. A user chooses a sector prompt bundle for the company.
5. The backend converts PDFs to Markdown artifacts.
6. The backend composes the sector prompts with the extracted document text.
7. If `USE_AI=false` or OpenAI config is missing, the app stores section-level dry-run prompt previews.
8. If `USE_AI=true` and OpenAI credentials are configured, the app calls OpenAI and expects structured JSON.
9. The frontend renders persisted section outputs dynamically.

## Current Architecture

- Backend: FastAPI, SQLite, local file storage.
- Frontend: static HTML/CSS/JavaScript served by FastAPI.
- Data folders:
  - `data/input/` for user-provided PDFs.
  - `data/processed/` for generated Markdown and JSON artifacts.
- Secrets:
  - `.env` is local and ignored.
  - `.env.example` is safe to commit.

## AI Behavior

OpenAI is behind a circuit breaker.

Only call OpenAI when all are true:

- `USE_AI=true`
- `OPENAI_API_KEY` is non-empty
- `OPENAI_MODEL` is non-empty

Otherwise, run in dry-run mode and persist one preview section per configured prompt section plus the exact full prompt/context that would have been sent to OpenAI.

Never print, log, or commit API keys.

The frontend may update local `.env` values through the config API. Existing API keys must never be returned to the browser; only expose configured/not-configured status.

## Prompt Model

The app uses sector prompt bundles. A sector owns prompt sections such as financial snapshot, operating metrics, risks, moat, or DCF assumptions.

OpenAI responses should be valid JSON with sections that the frontend can render. Supported section types include:

- `metric_cards`
- `key_value_table`
- `time_series_table`
- `narrative`
- `risk_list`
- `scenario_inputs`

## Development Commands

```bash
make setup
make demo-data
make backend
make test
```

The backend serves the frontend at `http://127.0.0.1:8000`.

## Guardrails

- Keep private planning docs out of GitHub. `docs/` is ignored.
- Keep generated data and SQLite databases out of GitHub.
- Prefer small, demo-stable changes over broad rewrites.
- Preserve the local-first workflow unless explicitly asked to add external sync.
- If adding OpenAI features, keep dry-run behavior working.
