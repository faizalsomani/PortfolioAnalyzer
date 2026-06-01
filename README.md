# SAGARD Portfolio Intelligence POC

Local-first proof of concept for turning private-company PDF reporting packages into persistent, sector-specific portfolio analysis.

The app lets a user:

- drop company PDFs into `data/input/<company_slug>/`,
- click `Sync Local Folder`,
- assign a sector prompt bundle,
- process PDFs into Markdown,
- preview each section prompt and the full OpenAI request when AI is disabled,
- call OpenAI for structured JSON when AI is enabled,
- persist companies, documents, runs, sections, and metrics in SQLite,
- view generated sections in a frontend dashboard.

## Repository Shape

```text
backend/              FastAPI API, SQLite persistence, PDF processing, OpenAI circuit breaker
frontend/             Static frontend served by FastAPI
data/input/           Local company folders and PDFs, ignored by git
data/processed/       Generated Markdown and JSON artifacts, ignored by git
docs/                 Private planning notes, ignored by git
AGENTS.md             Public agent-facing project guide
.env.example          Safe config template
.env                  Local config, ignored by git
```

## Quick Start

```bash
make setup
make demo-data
make backend
```

Then open:

```text
http://127.0.0.1:8000
```

## AI Circuit Breaker

The app is safe to run without an OpenAI key.

In `.env`:

```text
USE_AI=false
OPENAI_API_KEY=
OPENAI_MODEL=
```

With that configuration, processing still works end-to-end, but the backend stores and displays dry-run sections for each configured prompt section plus the full prompt and document text that would have been sent to OpenAI.

The frontend also includes a local AI config form. It can update `USE_AI`, `OPENAI_MODEL`, and `OPENAI_API_KEY` in `.env`. Existing keys are never shown back in the browser; the UI only reports whether a key is configured.

To enable live OpenAI calls later:

```text
USE_AI=true
OPENAI_API_KEY=<your key>
OPENAI_MODEL=gpt-4.1-nano
```

Do not commit `.env`.

## Local Folder Workflow

Create a folder under `data/input/` for each company:

```text
data/input/
  AcmeCloud/
    Q1 Board Report.pdf
  BetaHealth/
    Investor Update.pdf
```

Click `Sync Local Folder` in the app. The backend scans direct child folders, registers PDFs in SQLite, and marks new or changed companies as unprocessed.

Click `Process` for a company and choose a sector. Processing:

1. extracts text from PDFs,
2. writes Markdown artifacts to `data/processed/<company_slug>/documents/`,
3. composes the sector prompt bundle,
4. either stores dry-run prompt previews or calls OpenAI,
5. validates/persists JSON-backed sections,
6. renders the latest company analysis in the frontend.

## Useful Commands

```bash
make help        # Show available Makefile commands
make setup       # Create or verify .venv and install Python dependencies
make demo-data   # Generate sample text-based PDFs under data/input
make backend     # Start FastAPI and serve the frontend
make test        # Compile backend Python files
make db-summary  # Show companies and recent processing runs from SQLite
make sql         # Open the SQLite shell for data/app.db
make clean       # Remove local generated data and database
```

`make backend` depends on setup, so a fresh clone can usually start with:

```bash
make backend
```

For direct SQLite access:

```bash
sqlite3 data/app.db
```

## Notes For GitHub

Private planning documents under `docs/` are ignored by git. Local data, processed artifacts, SQLite databases, and `.env` are also ignored.

The public project explanation for coding agents lives in [AGENTS.md](AGENTS.md).
