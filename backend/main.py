from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import Thread
from typing import Any, Protocol

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


ROOT_DIR = Path(__file__).resolve().parents[1]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


class Settings:
    def __init__(self) -> None:
        env_file = load_env_file(ROOT_DIR / ".env")
        merged = {**env_file, **os.environ}

        self.use_ai = merged.get("USE_AI", "false").lower() in {"1", "true", "yes", "on"}
        self.openai_api_key = merged.get("OPENAI_API_KEY", "").strip()
        self.openai_model = merged.get("OPENAI_MODEL", "").strip()

        database_url = merged.get("DATABASE_URL", "sqlite:///./data/app.db")
        self.database_path = self._sqlite_path(database_url)
        self.input_dir = self._path_from_env(merged.get("INPUT_DIR", "./data/input"))
        self.processed_dir = self._path_from_env(merged.get("PROCESSED_DIR", "./data/processed"))

    def _path_from_env(self, value: str | None) -> Path:
        raw = value or ""
        path = Path(raw)
        if not path.is_absolute():
            path = ROOT_DIR / path
        return path.resolve()

    def _sqlite_path(self, database_url: str) -> Path:
        if database_url.startswith("sqlite:///"):
            return self._path_from_env(database_url.replace("sqlite:///", "", 1))
        return self._path_from_env("./data/app.db")

    @property
    def ai_ready(self) -> bool:
        return self.use_ai and bool(self.openai_api_key) and bool(self.openai_model)

    @property
    def mode(self) -> str:
        return "openai" if self.ai_ready else "dry_run"


settings = Settings()


DEFAULT_PROMPT_TEMPLATE = """You are analyzing private-market portfolio company reporting packages for SAGARD.
Return only valid JSON. Do not include Markdown outside the JSON object.
Do not invent numbers. Use null when a value is missing. Include warnings for missing or ambiguous data.
When using a number or claim from the documents, include evidence with document_id, page, and snippet when possible.

Allowed section types: metric_cards, key_value_table, time_series_table, narrative, risk_list, scenario_inputs.

Output shape rules:
- Return one object per requested section in the top-level "sections" array.
- For metric_cards, use "items": [{"label": string, "value": string|number|null, "unit": string|null, "period": string|null, "evidence": object|null}].
- For key_value_table and scenario_inputs, use "rows": [{"key": string, "value": string|number|null, "unit": string|null, "evidence": object|null}].
- For risk_list, use "items": [{"risk": string, "severity": string|null, "evidence": object|null}].
- For narrative, use "body": string.
- Never put an object or array inside a "value" field. If a value has multiple parts, summarize it as a concise string such as "DTC: 68%; marketplace: 22%; retail: 10%".
- Put document_id, page, snippet, confidence, and assumptions in sibling fields, not inside "value".

Top-level JSON contract:
{{json_contract}}

# Company
{{company_context}}

# Sector
{{sector_context}}

# Sector Prompt Bundle
{{section_bundle}}

# Documents
{{document_context}}
"""

PROMPT_TEMPLATE_VARIABLES = [
    "json_contract",
    "company_context",
    "sector_context",
    "sector_name",
    "sector_description",
    "section_bundle",
    "document_context",
]

COMPANY_ANALYSIS_STATUSES = {"processed", "processed_dry_run"}
RUN_ACTIVE_STATUSES = {"queued", "running"}


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return slug or "company"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def get_conn() -> sqlite3.Connection:
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(settings.database_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(row) for row in rows]


def init_db() -> None:
    settings.input_dir.mkdir(parents=True, exist_ok=True)
    settings.processed_dir.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS companies (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slug TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                sector_id INTEGER,
                status TEXT NOT NULL DEFAULT 'discovered',
                input_path TEXT NOT NULL,
                processed_path TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (sector_id) REFERENCES sectors(id)
            );

            CREATE TABLE IF NOT EXISTS sectors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS prompt_sections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sector_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                section_type TEXT NOT NULL,
                prompt TEXT NOT NULL,
                output_schema TEXT NOT NULL DEFAULT '{}',
                requires_evidence INTEGER NOT NULL DEFAULT 1,
                display_order INTEGER NOT NULL DEFAULT 0,
                version INTEGER NOT NULL DEFAULT 1,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (sector_id) REFERENCES sectors(id)
            );

            CREATE TABLE IF NOT EXISTS documents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER NOT NULL,
                document_uid TEXT NOT NULL UNIQUE,
                filename TEXT NOT NULL,
                source_path TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                mtime REAL NOT NULL,
                size_bytes INTEGER NOT NULL,
                markdown_path TEXT,
                page_count INTEGER,
                status TEXT NOT NULL DEFAULT 'discovered',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (company_id) REFERENCES companies(id)
            );

            CREATE TABLE IF NOT EXISTS processing_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER NOT NULL,
                sector_id INTEGER,
                status TEXT NOT NULL,
                mode TEXT NOT NULL,
                model TEXT,
                prompt_text TEXT,
                raw_response TEXT,
                parsed_json TEXT,
                error TEXT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                FOREIGN KEY (company_id) REFERENCES companies(id),
                FOREIGN KEY (sector_id) REFERENCES sectors(id)
            );

            CREATE TABLE IF NOT EXISTS prompt_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                template_key TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                template_text TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS section_outputs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                company_id INTEGER NOT NULL,
                prompt_section_id INTEGER,
                section_key TEXT NOT NULL,
                title TEXT NOT NULL,
                section_type TEXT NOT NULL,
                data_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (run_id) REFERENCES processing_runs(id),
                FOREIGN KEY (company_id) REFERENCES companies(id),
                FOREIGN KEY (prompt_section_id) REFERENCES prompt_sections(id)
            );

            CREATE TABLE IF NOT EXISTS metrics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER NOT NULL,
                run_id INTEGER NOT NULL,
                canonical_name TEXT NOT NULL,
                value TEXT,
                unit TEXT,
                period TEXT,
                evidence_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                FOREIGN KEY (company_id) REFERENCES companies(id),
                FOREIGN KEY (run_id) REFERENCES processing_runs(id)
            );

            CREATE TABLE IF NOT EXISTS dcf_scenarios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                company_id INTEGER NOT NULL,
                assumptions_json TEXT NOT NULL,
                output_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (company_id) REFERENCES companies(id)
            );
            """
        )
        ensure_column(conn, "processing_runs", "run_type", "TEXT NOT NULL DEFAULT 'company_analysis'")
        conn.commit()
    seed_defaults()


def ensure_column(conn: sqlite3.Connection, table_name: str, column_name: str, definition: str) -> None:
    columns = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    if not any(column["name"] == column_name for column in columns):
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


DEFAULT_SECTORS = [
    {
        "name": "SaaS",
        "description": "Recurring revenue software businesses.",
        "sections": [
            (
                "Financial Snapshot",
                "metric_cards",
                "Extract ARR, revenue, gross margin, EBITDA, cash balance, and reporting period. Include evidence snippets when found.",
                1,
            ),
            (
                "Retention And Growth",
                "key_value_table",
                "Extract churn, net revenue retention, gross revenue retention, pipeline, CAC payback, and headcount when available.",
                2,
            ),
            (
                "Moat And Competitive Position",
                "narrative",
                "Summarize product moat, differentiation, customer concentration, and competitive risk using only the documents.",
                3,
            ),
            (
                "Key Risks",
                "risk_list",
                "List the top risks visible in the reporting package. Mark severity as low, medium, or high.",
                4,
            ),
        ],
    },
    {
        "name": "Healthcare",
        "description": "Healthcare services, software, and enabled-services companies.",
        "sections": [
            (
                "Financial Snapshot",
                "metric_cards",
                "Extract revenue, EBITDA, gross margin, cash, and reporting period. Include evidence snippets when found.",
                1,
            ),
            (
                "Operating Metrics",
                "key_value_table",
                "Extract patient volume, reimbursement exposure, utilization, staffing, churn, and pipeline when available.",
                2,
            ),
            (
                "Regulatory And Execution Risk",
                "risk_list",
                "Identify regulatory, reimbursement, staffing, compliance, or execution risks from the documents.",
                3,
            ),
        ],
    },
    {
        "name": "Industrial",
        "description": "Manufacturing, industrial services, and asset-heavy companies.",
        "sections": [
            (
                "Financial Snapshot",
                "metric_cards",
                "Extract revenue, EBITDA, gross margin, backlog, cash, and working capital signals.",
                1,
            ),
            (
                "Operations And Backlog",
                "key_value_table",
                "Extract backlog, utilization, capacity, supply-chain issues, headcount, and customer concentration.",
                2,
            ),
            (
                "Key Risks",
                "risk_list",
                "List margin pressure, customer concentration, supply-chain, and demand risks with severity.",
                3,
            ),
        ],
    },
    {
        "name": "Consumer",
        "description": "Consumer, retail, marketplace, and brand companies.",
        "sections": [
            (
                "Financial Snapshot",
                "metric_cards",
                "Extract revenue, gross margin, EBITDA, cash, growth rate, and reporting period.",
                1,
            ),
            (
                "Demand And Unit Economics",
                "key_value_table",
                "Extract sales channels, retention, CAC, AOV, inventory, and contribution margin when available.",
                2,
            ),
        ],
    },
    {
        "name": "Fintech",
        "description": "Financial technology and embedded finance companies.",
        "sections": [
            (
                "Financial Snapshot",
                "metric_cards",
                "Extract revenue, ARR, EBITDA, gross margin, cash, transaction volume, and reporting period.",
                1,
            ),
            (
                "Risk And Compliance",
                "risk_list",
                "Identify regulatory, credit, fraud, concentration, liquidity, and compliance risks.",
                2,
            ),
        ],
    },
]


def seed_defaults() -> None:
    with get_conn() as conn:
        existing_template = conn.execute(
            "SELECT id FROM prompt_templates WHERE template_key = 'portfolio_analysis'"
        ).fetchone()
        if not existing_template:
            conn.execute(
                """
                INSERT INTO prompt_templates
                (template_key, name, description, template_text, version, is_active, created_at, updated_at)
                VALUES ('portfolio_analysis', ?, ?, ?, 1, 1, ?, ?)
                """,
                (
                    "Portfolio analysis prompt",
                    "Global prompt wrapper used for company analysis and prompt tests.",
                    DEFAULT_PROMPT_TEMPLATE,
                    now_iso(),
                    now_iso(),
                ),
            )

        for sector in DEFAULT_SECTORS:
            existing = conn.execute("SELECT id FROM sectors WHERE name = ?", (sector["name"],)).fetchone()
            if existing:
                sector_id = existing["id"]
            else:
                cur = conn.execute(
                    "INSERT INTO sectors (name, description, created_at) VALUES (?, ?, ?)",
                    (sector["name"], sector["description"], now_iso()),
                )
                sector_id = cur.lastrowid

            count = conn.execute(
                "SELECT COUNT(*) AS count FROM prompt_sections WHERE sector_id = ?",
                (sector_id,),
            ).fetchone()["count"]
            if count:
                continue

            for name, section_type, prompt, order in sector["sections"]:
                conn.execute(
                    """
                    INSERT INTO prompt_sections
                    (sector_id, name, section_type, prompt, output_schema, requires_evidence, display_order, version, is_active, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 1, ?, 1, 1, ?, ?)
                    """,
                    (sector_id, name, section_type, prompt, "{}", order, now_iso(), now_iso()),
                )
        conn.commit()


class SectorCreate(BaseModel):
    name: str = Field(min_length=1)
    description: str = ""


class SectorUpdate(BaseModel):
    description: str | None = None


class PromptTemplateUpdate(BaseModel):
    template_text: str = Field(min_length=1)
    description: str = ""


class PromptTemplateTest(BaseModel):
    company_id: int
    sector_id: int
    template_text: str = Field(min_length=1)


class PromptSectionCreate(BaseModel):
    name: str = Field(min_length=1)
    section_type: str = "narrative"
    prompt: str = Field(min_length=1)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    requires_evidence: bool = True
    display_order: int = 0


class PromptSectionUpdate(BaseModel):
    name: str | None = None
    section_type: str | None = None
    prompt: str | None = None
    output_schema: dict[str, Any] | None = None
    requires_evidence: bool | None = None
    display_order: int | None = None
    is_active: bool | None = None


class ProcessRequest(BaseModel):
    sector_id: int


class PromptSectionDraftTest(BaseModel):
    company_id: int
    sector_id: int
    name: str = Field(min_length=1)
    section_type: str = "narrative"
    prompt: str = Field(min_length=1)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    requires_evidence: bool = True


class ConfigUpdate(BaseModel):
    use_ai: bool
    openai_model: str = ""
    openai_api_key: str | None = None
    clear_openai_api_key: bool = False


def write_env_values(updates: dict[str, str], preserve_blank_key: bool = True) -> None:
    env_path = ROOT_DIR / ".env"
    existing = load_env_file(env_path)
    if preserve_blank_key and updates.get("OPENAI_API_KEY") == "" and existing.get("OPENAI_API_KEY"):
        updates.pop("OPENAI_API_KEY", None)
    merged = {
        "USE_AI": existing.get("USE_AI", "false"),
        "OPENAI_API_KEY": existing.get("OPENAI_API_KEY", ""),
        "OPENAI_MODEL": existing.get("OPENAI_MODEL", ""),
        "DATABASE_URL": existing.get("DATABASE_URL", "sqlite:///./data/app.db"),
        "INPUT_DIR": existing.get("INPUT_DIR", "./data/input"),
        "PROCESSED_DIR": existing.get("PROCESSED_DIR", "./data/processed"),
    }
    merged.update(updates)
    env_path.write_text(
        "\n".join(
            [
                f"USE_AI={merged['USE_AI']}",
                f"OPENAI_API_KEY={merged['OPENAI_API_KEY']}",
                f"OPENAI_MODEL={merged['OPENAI_MODEL']}",
                f"DATABASE_URL={merged['DATABASE_URL']}",
                f"INPUT_DIR={merged['INPUT_DIR']}",
                f"PROCESSED_DIR={merged['PROCESSED_DIR']}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def reload_settings() -> None:
    global settings
    settings = Settings()


class JsonModelClient(Protocol):
    def complete_json(self, prompt_text: str) -> str:
        ...


class OpenAIJsonClient:
    def __init__(self, api_key: str, model: str) -> None:
        self.api_key = api_key
        self.model = model

    def complete_json(self, prompt_text: str) -> str:
        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError("openai package is not installed. Run `make setup`.") from exc

        client = OpenAI(api_key=self.api_key)
        response = client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": "You return valid JSON only. Do not include prose outside JSON.",
                },
                {"role": "user", "content": prompt_text},
            ],
            response_format={"type": "json_object"},
            temperature=0.1,
        )
        content = response.choices[0].message.content
        if not content:
            raise RuntimeError("OpenAI returned an empty response.")
        return content


def sync_local_folder() -> dict[str, Any]:
    settings.input_dir.mkdir(parents=True, exist_ok=True)
    settings.processed_dir.mkdir(parents=True, exist_ok=True)
    discovered_companies = 0
    discovered_documents = 0
    changed_documents = 0

    with get_conn() as conn:
        for company_dir in sorted([p for p in settings.input_dir.iterdir() if p.is_dir()]):
            slug = slugify(company_dir.name)
            processed_path = settings.processed_dir / slug
            processed_path.mkdir(parents=True, exist_ok=True)
            timestamp = now_iso()
            company = conn.execute("SELECT id FROM companies WHERE slug = ?", (slug,)).fetchone()
            if not company:
                cur = conn.execute(
                    """
                    INSERT INTO companies (slug, display_name, status, input_path, processed_path, created_at, updated_at)
                    VALUES (?, ?, 'discovered', ?, ?, ?, ?)
                    """,
                    (slug, company_dir.name, str(company_dir), str(processed_path), timestamp, timestamp),
                )
                company_id = cur.lastrowid
                discovered_companies += 1
            else:
                company_id = company["id"]
                conn.execute(
                    "UPDATE companies SET input_path = ?, processed_path = ?, updated_at = ? WHERE id = ?",
                    (str(company_dir), str(processed_path), timestamp, company_id),
                )

            for pdf_path in sorted(company_dir.rglob("*.pdf")):
                file_hash = sha256_file(pdf_path)
                stat = pdf_path.stat()
                document_uid = f"doc_{file_hash[:12]}"
                existing = conn.execute(
                    "SELECT id, content_hash, markdown_path FROM documents WHERE source_path = ?",
                    (str(pdf_path),),
                ).fetchone()
                if not existing:
                    existing = conn.execute(
                        "SELECT id, content_hash, markdown_path FROM documents WHERE document_uid = ?",
                        (document_uid,),
                    ).fetchone()

                if not existing:
                    conn.execute(
                        """
                        INSERT INTO documents
                        (company_id, document_uid, filename, source_path, content_hash, mtime, size_bytes, status, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'discovered', ?, ?)
                        """,
                        (
                            company_id,
                            document_uid,
                            pdf_path.name,
                            str(pdf_path),
                            file_hash,
                            stat.st_mtime,
                            stat.st_size,
                            timestamp,
                            timestamp,
                        ),
                    )
                    discovered_documents += 1
                elif existing["content_hash"] != file_hash:
                    conn.execute(
                        """
                        UPDATE documents
                        SET document_uid = ?, content_hash = ?, mtime = ?, size_bytes = ?, status = 'changed',
                            markdown_path = NULL, page_count = NULL, updated_at = ?
                        WHERE id = ?
                        """,
                        (document_uid, file_hash, stat.st_mtime, stat.st_size, timestamp, existing["id"]),
                    )
                    changed_documents += 1
                else:
                    markdown_path = existing["markdown_path"]
                    if markdown_path and not Path(markdown_path).exists():
                        markdown_path = None
                    conn.execute(
                        """
                        UPDATE documents
                        SET company_id = ?, filename = ?, source_path = ?, mtime = ?, size_bytes = ?,
                            markdown_path = ?, updated_at = ?
                        WHERE id = ?
                        """,
                        (
                            company_id,
                            pdf_path.name,
                            str(pdf_path),
                            stat.st_mtime,
                            stat.st_size,
                            markdown_path,
                            timestamp,
                            existing["id"],
                        ),
                    )

        conn.commit()

    with get_conn() as conn:
        summary = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM companies) AS total_companies,
                (SELECT COUNT(*) FROM documents) AS total_documents,
                (SELECT COUNT(*) FROM companies WHERE status NOT LIKE 'processed%') AS unprocessed_companies
            """
        ).fetchone()

    return {
        "input_dir": str(settings.input_dir),
        "processed_dir": str(settings.processed_dir),
        "discovered_companies": discovered_companies,
        "discovered_documents": discovered_documents,
        "changed_documents": changed_documents,
        "total_companies": summary["total_companies"] or 0,
        "total_documents": summary["total_documents"] or 0,
        "unprocessed_companies": summary["unprocessed_companies"] or 0,
    }


def extract_pdf_to_markdown(document: sqlite3.Row, company: sqlite3.Row) -> tuple[str, int]:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("pypdf is not installed. Run `make setup`.") from exc

    source_path = Path(document["source_path"])
    if not source_path.exists():
        raise RuntimeError(f"PDF not found: {source_path}")

    reader = PdfReader(str(source_path))
    pages: list[str] = []
    for idx, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        pages.append(f"## Page {idx}\n\n{text.strip()}\n")

    if not any(page.strip() for page in pages):
        raise RuntimeError(f"No extractable text found in {source_path.name}. OCR is not implemented in v1.")

    company_dir = settings.processed_dir / company["slug"] / "documents"
    company_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = company_dir / f"{document['document_uid']}.md"
    markdown = "\n".join(
        [
            f"# {document['filename']}",
            "",
            f"- Document ID: `{document['document_uid']}`",
            f"- Source path: `{document['source_path']}`",
            f"- Company: {company['display_name']}",
            "",
            *pages,
        ]
    )
    markdown_path.write_text(markdown, encoding="utf-8")
    return str(markdown_path), len(reader.pages)


def get_company_or_404(company_id: int) -> sqlite3.Row:
    with get_conn() as conn:
        company = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
    if not company:
        raise HTTPException(status_code=404, detail="Company not found")
    return company


def get_sector_or_404(sector_id: int) -> sqlite3.Row:
    with get_conn() as conn:
        sector = conn.execute("SELECT * FROM sectors WHERE id = ?", (sector_id,)).fetchone()
    if not sector:
        raise HTTPException(status_code=404, detail="Sector not found")
    return sector


def get_active_prompt_template() -> sqlite3.Row:
    with get_conn() as conn:
        template = conn.execute(
            """
            SELECT * FROM prompt_templates
            WHERE template_key = 'portfolio_analysis' AND is_active = 1
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    if not template:
        raise RuntimeError("Active portfolio analysis prompt template is missing.")
    return template


def render_template(template_text: str, variables: dict[str, str]) -> str:
    rendered = template_text
    for key, value in variables.items():
        rendered = rendered.replace(f"{{{{{key}}}}}", value)
    return rendered


def build_section_bundle(sections: list[sqlite3.Row]) -> str:
    section_lines: list[str] = []
    for section in sections:
        schema = section["output_schema"] or "{}"
        section_lines.append(
            "\n".join(
                [
                    f"### Section: {section['name']}",
                    f"- id: section_{section['id']}",
                    f"- type: {section['section_type']}",
                    f"- requires_evidence: {bool(section['requires_evidence'])}",
                    f"- expected_schema: {schema}",
                    "Instructions:",
                    section["prompt"],
                ]
            )
        )
    return "\n\n".join(section_lines)


def build_document_context(documents: list[sqlite3.Row]) -> str:
    doc_blocks: list[str] = []
    for document in documents:
        markdown_path = Path(document["markdown_path"] or "")
        content = markdown_path.read_text(encoding="utf-8") if markdown_path.exists() else ""
        doc_blocks.append(
            "\n".join(
                [
                    f"## Source Document: {document['filename']}",
                    f"Document ID: {document['document_uid']}",
                    content,
                ]
            )
        )
    return "\n\n".join(doc_blocks)


def compose_prompt(
    company: sqlite3.Row,
    sector: sqlite3.Row,
    sections: list[sqlite3.Row],
    documents: list[sqlite3.Row],
    template_override: str | None = None,
) -> str:
    template_text = template_override if template_override is not None else get_active_prompt_template()["template_text"]
    sector_description = sector["description"] or ""
    variables = {
        "json_contract": json.dumps(
            {
                "company": {"name": company["display_name"], "sector": sector["name"], "reporting_period": None},
                "sections": [],
                "metrics": [],
                "warnings": [],
            },
            indent=2,
        ),
        "company_context": f"Name: {company['display_name']}\nSlug: {company['slug']}\nSector: {sector['name']}",
        "sector_context": f"Name: {sector['name']}\nDescription: {sector_description or 'Not provided'}",
        "sector_name": sector["name"],
        "sector_description": sector_description,
        "section_bundle": build_section_bundle(sections),
        "document_context": build_document_context(documents),
    }
    return render_template(template_text, variables)


def build_section_prompt_preview(section: sqlite3.Row, documents: list[sqlite3.Row]) -> str:
    document_lines = []
    for document in documents:
        document_lines.append(
            f"- {document['filename']} ({document['document_uid']}), pages: {document['page_count'] or 'unknown'}"
        )
    return "\n".join(
        [
            f"Section: {section['name']}",
            f"Expected frontend renderer: {section['section_type']}",
            f"Requires evidence: {bool(section['requires_evidence'])}",
            f"Prompt version: {section['version']}",
            "",
            "Prompt instructions:",
            section["prompt"],
            "",
            "Documents that would be included:",
            "\n".join(document_lines) or "- No documents",
            "",
            "Dry run mode is enabled, so this section has not been sent to OpenAI yet.",
        ]
    )


def build_dry_run_response(
    company: sqlite3.Row,
    sector: sqlite3.Row,
    sections: list[sqlite3.Row],
    documents: list[sqlite3.Row],
    prompt_text: str,
) -> dict[str, Any]:
    preview_sections = []
    for section in sections:
        preview_sections.append(
            {
                "id": f"dry_run_section_{section['id']}",
                "type": "narrative",
                "title": f"Dry Run: {section['name']}",
                "body": build_section_prompt_preview(section, documents),
                "evidence": [],
                "dry_run": True,
                "target_section_type": section["section_type"],
                "prompt_section_id": section["id"],
            }
        )
    preview_sections.append(
        {
            "id": "openai_full_request_preview",
            "type": "narrative",
            "title": "Full OpenAI Request Preview",
            "body": prompt_text,
            "evidence": [],
            "dry_run": True,
        }
    )
    return {
        "company": {"name": company["display_name"], "sector": sector["name"], "reporting_period": None},
        "sections": preview_sections,
        "metrics": [],
        "warnings": [
            "Dry run mode: USE_AI is false or OpenAI configuration is incomplete. No request was sent to OpenAI."
        ],
    }


def prepare_company_documents(
    company_id: int,
    company: sqlite3.Row,
    *,
    mutate_document_status: bool = True,
) -> list[sqlite3.Row]:
    with get_conn() as conn:
        documents = conn.execute(
            "SELECT * FROM documents WHERE company_id = ? ORDER BY filename",
            (company_id,),
        ).fetchall()

    if not documents:
        raise HTTPException(status_code=400, detail="No PDFs found for this company. Add PDFs and sync again.")

    settings.processed_dir.joinpath(company["slug"]).mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        for document in documents:
            markdown_path, page_count = extract_pdf_to_markdown(document, company)
            if mutate_document_status:
                conn.execute(
                    """
                    UPDATE documents
                    SET markdown_path = ?, page_count = ?, status = 'markdown_ready', updated_at = ?
                    WHERE id = ?
                    """,
                    (markdown_path, page_count, now_iso(), document["id"]),
                )
            else:
                conn.execute(
                    """
                    UPDATE documents
                    SET markdown_path = ?, page_count = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (markdown_path, page_count, now_iso(), document["id"]),
                )
        conn.commit()

    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM documents WHERE company_id = ? ORDER BY filename",
            (company_id,),
        ).fetchall()


def call_openai_json(prompt_text: str, model_client: JsonModelClient | None = None) -> str:
    client = model_client or OpenAIJsonClient(settings.openai_api_key, settings.openai_model)
    return client.complete_json(prompt_text)


def section_value(section: Any, key: str, default: Any = None) -> Any:
    try:
        return section[key]
    except (KeyError, IndexError, TypeError):
        return default


def section_id(section: Any) -> str:
    value = section_value(section, "id")
    return f"section_{value}" if value is not None else ""


def section_name(section: Any) -> str:
    return str(section_value(section, "name", "Section"))


def section_type(section: Any) -> str:
    return str(section_value(section, "section_type", "narrative") or "narrative")


def title_from_key(value: str) -> str:
    cleaned = re.sub(r"[_-]+", " ", value or "").strip()
    return cleaned.title() if cleaned else "Section"


def compact_key_label(value: str) -> str:
    cleaned = re.sub(r"[_-]+", " ", value or "").strip()
    if not cleaned:
        return "Value"
    return cleaned if cleaned.isupper() else cleaned.title()


VALUE_METADATA_KEYS = {
    "value",
    "unit",
    "period",
    "evidence",
    "confidence",
    "document_id",
    "page",
    "snippet",
    "source",
    "source_document",
    "source_page",
    "assumptions",
}


def compact_display_value(value: Any) -> str | int | float | bool | None:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return "; ".join(str(compact_display_value(item)) for item in value)
    if isinstance(value, dict):
        if "value" in value and set(value.keys()).issubset(VALUE_METADATA_KEYS):
            return compact_display_value(value.get("value"))
        return "; ".join(
            f"{compact_key_label(str(key))}: {compact_display_value(item)}"
            for key, item in value.items()
            if key not in {"evidence", "confidence", "document_id", "page", "snippet"}
        )
    return str(value)


def apply_value_metadata(normalized: dict[str, Any], value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    for metadata_key in ("unit", "period", "evidence", "confidence", "document_id", "page", "snippet"):
        if metadata_key in value and normalized.get(metadata_key) in (None, "", [], {}):
            normalized[metadata_key] = value[metadata_key]
    if "value" in value:
        return value.get("value")
    return value


def display_value_with_unit(value: str | int | float | bool | None, unit: Any) -> str | int | float | bool | None:
    if unit in (None, "") or value is None or isinstance(value, bool):
        return value
    separator = "" if str(unit).strip() == "%" else " "
    return f"{value}{separator}{unit}"


def records_from_mapping(mapping: dict[str, Any], label_key: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for key, value in mapping.items():
        if isinstance(value, dict):
            record = {label_key: key, **value}
            if "value" not in record:
                record["value"] = {inner_key: inner_value for inner_key, inner_value in value.items() if inner_key not in VALUE_METADATA_KEYS}
        else:
            record = {label_key: key, "value": value}
        records.append(record)
    return records


def normalize_metric_card_item(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {"label": "Metric", "value": compact_display_value(item)}
    normalized = dict(item)
    normalized.setdefault("label", normalized.get("name") or normalized.get("key") or normalized.get("canonical_name") or "Metric")
    value = normalized.get("value", normalized.get("amount"))
    value = apply_value_metadata(normalized, value)
    normalized["value"] = compact_display_value(value)
    return normalized


def normalize_key_value_row(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {"key": "Value", "value": compact_display_value(item)}
    normalized = dict(item)
    normalized.setdefault("key", normalized.get("label") or normalized.get("name") or normalized.get("canonical_name") or "Value")
    value = normalized.get("value", normalized.get("amount") or normalized.get("text"))
    value = apply_value_metadata(normalized, value)
    display_value = compact_display_value(value)
    unit = normalized.get("unit")
    normalized["value"] = display_value_with_unit(display_value, unit)
    return normalized


def normalize_risk_item(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {"risk": compact_display_value(item), "severity": None}
    normalized = dict(item)
    normalized["risk"] = compact_display_value(normalized.get("risk") or normalized.get("text") or normalized.get("label"))
    normalized["severity"] = compact_display_value(normalized.get("severity"))
    return normalized


def normalize_processing_json(payload: dict[str, Any], prompt_sections: list[Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Response must be a JSON object.")

    raw_sections = payload.get("sections")
    if isinstance(raw_sections, dict):
        raw_sections = [
            {"id": key, "type": "narrative", "title": title_from_key(key), "body": value}
            for key, value in raw_sections.items()
        ]
    if raw_sections is None:
        raw_sections = []
    if not isinstance(raw_sections, list):
        raise ValueError("Response sections must be an array or object.")

    id_lookup = {section_id(section): section for section in prompt_sections}
    prompt_id_lookup = {
        section_value(section, "id"): section
        for section in prompt_sections
        if section_value(section, "id") is not None
    }
    name_lookup = {
        slugify(section_name(section)).replace("-", "_"): section
        for section in prompt_sections
    }

    normalized_sections: list[dict[str, Any]] = []
    for index, item in enumerate(raw_sections):
        section = item if isinstance(item, dict) else {"body": item}
        section_key = str(section.get("id") or "")
        normalized_title = slugify(str(section.get("title", ""))).replace("-", "_")
        prompt_section = None
        if section.get("prompt_section_id") in prompt_id_lookup:
            prompt_section = prompt_id_lookup[section["prompt_section_id"]]
        elif section_key in id_lookup:
            prompt_section = id_lookup[section_key]
        elif normalized_title in name_lookup:
            prompt_section = name_lookup[normalized_title]
        elif index < len(prompt_sections):
            prompt_section = prompt_sections[index]

        if not section.get("id"):
            section["id"] = section_id(prompt_section) or f"section_{index + 1}"
        if not section.get("title"):
            section["title"] = section_name(prompt_section) if prompt_section is not None else title_from_key(str(section["id"]))
        if not section.get("type"):
            section["type"] = section_type(prompt_section) if prompt_section is not None else "narrative"
        if prompt_section is not None and not section.get("prompt_section_id"):
            prompt_id = section_value(prompt_section, "id")
            if isinstance(prompt_id, int):
                section["prompt_section_id"] = prompt_id
        if section["type"] == "metric_cards":
            items = section.get("items") or section.get("metrics") or section.get("cards") or section.get("rows") or []
            if isinstance(items, dict):
                items = records_from_mapping(items, "name")
            section["items"] = [normalize_metric_card_item(item) for item in items]
            for alias in ("metrics", "cards", "rows"):
                section.pop(alias, None)
        if section["type"] in {"key_value_table", "scenario_inputs"}:
            rows = section.get("rows") or section.get("items") or section.get("metrics") or section.get("values") or []
            if isinstance(rows, dict):
                rows = records_from_mapping(rows, "key")
            section["rows"] = [normalize_key_value_row(item) for item in rows]
            for alias in ("items", "metrics", "values"):
                section.pop(alias, None)
        if section["type"] == "risk_list":
            risks = section.get("items") or section.get("risks") or section.get("risk_items") or []
            if isinstance(risks, dict):
                risks = [{"risk": key, "severity": value} for key, value in risks.items()]
            section["items"] = [normalize_risk_item(item) for item in risks] if isinstance(risks, list) else []
            for alias in ("risks", "risk_items"):
                section.pop(alias, None)
        if section["type"] == "narrative" and not section.get("body") and not section.get("text"):
            section["body"] = json.dumps({k: v for k, v in section.items() if k not in {"id", "title", "type"}}, indent=2)
        normalized_sections.append(section)

    payload["sections"] = normalized_sections
    payload.setdefault("metrics", [])
    payload.setdefault("warnings", [])
    payload.setdefault("company", {})
    return payload


def validate_processing_json(payload: dict[str, Any]) -> None:
    if not isinstance(payload.get("sections"), list):
        raise ValueError("Response must include a top-level sections array.")
    for section in payload["sections"]:
        if not isinstance(section, dict):
            raise ValueError("Each section must be an object.")
        for field in ("id", "type", "title"):
            if field not in section:
                raise ValueError(f"Section is missing required field: {field}")


def attach_prompt_section_ids(payload: dict[str, Any], sections: list[Any]) -> None:
    id_lookup = {section_id(section): section_value(section, "id") for section in sections}
    name_lookup = {slugify(section_name(section)).replace("-", "_"): section_value(section, "id") for section in sections}
    for section in payload.get("sections", []) or []:
        if not isinstance(section, dict):
            continue
        if section.get("prompt_section_id"):
            continue
        model_section_id = str(section.get("id", ""))
        normalized_title = slugify(str(section.get("title", ""))).replace("-", "_")
        if model_section_id in id_lookup:
            section["prompt_section_id"] = id_lookup[model_section_id]
        elif normalized_title in name_lookup:
            section["prompt_section_id"] = name_lookup[normalized_title]


def store_processing_outputs(
    conn: sqlite3.Connection,
    run_id: int,
    company_id: int,
    payload: dict[str, Any],
) -> None:
    created_at = now_iso()
    for section in payload.get("sections", []):
        prompt_section_id = section.get("prompt_section_id")
        conn.execute(
            """
            INSERT INTO section_outputs
            (run_id, company_id, prompt_section_id, section_key, title, section_type, data_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                company_id,
                prompt_section_id,
                str(section.get("id", "section")),
                str(section.get("title", "Section")),
                str(section.get("type", "narrative")),
                json.dumps(section),
                created_at,
            ),
        )

    for metric in payload.get("metrics", []) or []:
        conn.execute(
            """
            INSERT INTO metrics
            (company_id, run_id, canonical_name, value, unit, period, evidence_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                company_id,
                run_id,
                str(metric.get("canonical_name") or metric.get("name") or "metric"),
                None if metric.get("value") is None else str(metric.get("value")),
                metric.get("unit"),
                metric.get("period"),
                json.dumps(metric.get("evidence", [])),
                created_at,
            ),
        )


class PortfolioProcessingService:
    def create_queued_run(self, company_id: int, sector_id: int, run_type: str) -> int:
        get_company_or_404(company_id)
        get_sector_or_404(sector_id)
        return self._create_run(company_id, sector_id, "", run_type=run_type, status="queued")

    def process_company(self, company_id: int, sector_id: int, run_id: int | None = None) -> dict[str, Any]:
        company = get_company_or_404(company_id)
        sector = get_sector_or_404(sector_id)

        with get_conn() as conn:
            sections = conn.execute(
                """
                SELECT * FROM prompt_sections
                WHERE sector_id = ? AND is_active = 1
                ORDER BY display_order, id
                """,
                (sector_id,),
            ).fetchall()

        if not sections:
            raise HTTPException(status_code=400, detail="No active prompt sections for this sector.")

        prepare_company_documents(company_id, company)

        with get_conn() as conn:
            conn.execute(
                "UPDATE companies SET sector_id = ?, status = 'processing', updated_at = ? WHERE id = ?",
                (sector_id, now_iso(), company_id),
            )
            conn.commit()

        with get_conn() as conn:
            company = conn.execute("SELECT * FROM companies WHERE id = ?", (company_id,)).fetchone()
            documents = conn.execute(
                "SELECT * FROM documents WHERE company_id = ? ORDER BY filename",
                (company_id,),
            ).fetchall()
            sections = conn.execute(
                """
                SELECT * FROM prompt_sections
                WHERE sector_id = ? AND is_active = 1
                ORDER BY display_order, id
                """,
                (sector_id,),
            ).fetchall()

        prompt_text = compose_prompt(company, sector, sections, documents)
        run_id = self._start_or_create_run(
            run_id,
            company_id,
            sector_id,
            prompt_text,
            run_type="company_analysis",
        )

        raw_response = ""
        try:
            if settings.ai_ready:
                raw_response = call_openai_json(prompt_text)
                payload = json.loads(raw_response)
                attach_prompt_section_ids(payload, sections)
                payload = normalize_processing_json(payload, sections)
            else:
                payload = build_dry_run_response(company, sector, sections, documents, prompt_text)
                raw_response = json.dumps(payload, indent=2)

            validate_processing_json(payload)
            self._write_company_output(company, sector, run_id, payload)
            status = "processed" if settings.ai_ready else "processed_dry_run"
            self._complete_run(run_id, company_id, payload, raw_response, status)
            with get_conn() as conn:
                conn.execute(
                    "UPDATE documents SET status = 'processed', updated_at = ? WHERE company_id = ?",
                    (now_iso(), company_id),
                )
                conn.commit()
            return {"run_id": run_id, "status": status, "mode": settings.mode, "payload": payload}
        except Exception as exc:
            self._fail_run(run_id, company_id, str(exc), raw_response=raw_response)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    def test_draft_prompt_section(
        self,
        request: PromptSectionDraftTest,
        run_id: int | None = None,
    ) -> dict[str, Any]:
        company = get_company_or_404(request.company_id)
        sector = get_sector_or_404(request.sector_id)
        documents = prepare_company_documents(request.company_id, company, mutate_document_status=False)
        draft_section = {
            "id": "draft",
            "name": request.name,
            "section_type": request.section_type,
            "prompt": request.prompt,
            "output_schema": json.dumps(request.output_schema),
            "requires_evidence": int(request.requires_evidence),
            "display_order": 0,
            "version": "draft",
            "is_active": 1,
        }
        prompt_text = compose_prompt(company, sector, [draft_section], documents)
        run_id = self._start_or_create_run(
            run_id,
            request.company_id,
            request.sector_id,
            prompt_text,
            run_type="prompt_test",
        )

        raw_response = ""
        try:
            if settings.ai_ready:
                raw_response = call_openai_json(prompt_text)
                payload = json.loads(raw_response)
                attach_prompt_section_ids(payload, [draft_section])
                payload = normalize_processing_json(payload, [draft_section])
            else:
                payload = build_dry_run_response(company, sector, [draft_section], documents, prompt_text)
                for section in payload["sections"]:
                    if section["id"].startswith("dry_run_section_"):
                        section["id"] = "draft_prompt_test"
                        section["title"] = f"Draft Test: {request.name}"
                        section["prompt_section_id"] = None
                raw_response = json.dumps(payload, indent=2)

            validate_processing_json(payload)
            company_output_dir = settings.processed_dir / company["slug"] / "outputs"
            company_output_dir.mkdir(parents=True, exist_ok=True)
            (company_output_dir / f"draft_test_run_{run_id}.json").write_text(
                json.dumps(payload, indent=2),
                encoding="utf-8",
            )
            status = "draft_test" if settings.ai_ready else "draft_test_dry_run"
            self._complete_draft_test_run(run_id, payload, raw_response, status)
            return {"run_id": run_id, "status": status, "mode": settings.mode, "payload": payload}
        except Exception as exc:
            self._fail_run(run_id, None, str(exc), raw_response=raw_response)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    def test_prompt_template(
        self,
        request: PromptTemplateTest,
        run_id: int | None = None,
    ) -> dict[str, Any]:
        company = get_company_or_404(request.company_id)
        sector = get_sector_or_404(request.sector_id)
        documents = prepare_company_documents(request.company_id, company, mutate_document_status=False)
        with get_conn() as conn:
            sections = conn.execute(
                """
                SELECT * FROM prompt_sections
                WHERE sector_id = ? AND is_active = 1
                ORDER BY display_order, id
                """,
                (request.sector_id,),
            ).fetchall()
        if not sections:
            raise HTTPException(status_code=400, detail="No active prompt sections for this sector.")

        prompt_text = compose_prompt(company, sector, sections, documents, template_override=request.template_text)
        run_id = self._start_or_create_run(
            run_id,
            request.company_id,
            request.sector_id,
            prompt_text,
            run_type="template_test",
        )

        raw_response = ""
        try:
            if settings.ai_ready:
                raw_response = call_openai_json(prompt_text)
                payload = json.loads(raw_response)
                attach_prompt_section_ids(payload, sections)
                payload = normalize_processing_json(payload, sections)
            else:
                payload = build_dry_run_response(company, sector, sections, documents, prompt_text)
                raw_response = json.dumps(payload, indent=2)

            validate_processing_json(payload)
            status = "template_test" if settings.ai_ready else "template_test_dry_run"
            self._complete_draft_test_run(run_id, payload, raw_response, status)
            return {"run_id": run_id, "status": status, "mode": settings.mode, "payload": payload}
        except Exception as exc:
            self._fail_run(run_id, None, str(exc), raw_response=raw_response)
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    def _create_run(
        self,
        company_id: int,
        sector_id: int,
        prompt_text: str,
        *,
        run_type: str,
        status: str = "running",
    ) -> int:
        with get_conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO processing_runs
                (company_id, sector_id, status, mode, model, prompt_text, started_at, run_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    company_id,
                    sector_id,
                    status,
                    settings.mode,
                    settings.openai_model or None,
                    prompt_text,
                    now_iso(),
                    run_type,
                ),
            )
            conn.commit()
            return int(cur.lastrowid)

    def _start_or_create_run(
        self,
        run_id: int | None,
        company_id: int,
        sector_id: int,
        prompt_text: str,
        *,
        run_type: str,
    ) -> int:
        if run_id is None:
            return self._create_run(company_id, sector_id, prompt_text, run_type=run_type)
        with get_conn() as conn:
            conn.execute(
                """
                UPDATE processing_runs
                SET status = 'running', mode = ?, model = ?, prompt_text = ?, started_at = ?, run_type = ?
                WHERE id = ?
                """,
                (settings.mode, settings.openai_model or None, prompt_text, now_iso(), run_type, run_id),
            )
            conn.commit()
        return run_id

    def _write_company_output(
        self,
        company: sqlite3.Row,
        sector: sqlite3.Row,
        run_id: int,
        payload: dict[str, Any],
    ) -> None:
        company_output_dir = settings.processed_dir / company["slug"] / "outputs"
        company_output_dir.mkdir(parents=True, exist_ok=True)
        (company_output_dir / f"run_{run_id}.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        manifest_path = settings.processed_dir / company["slug"] / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "company_id": company["id"],
                    "company_slug": company["slug"],
                    "sector": sector["name"],
                    "latest_run_id": run_id,
                    "mode": settings.mode,
                    "updated_at": now_iso(),
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def _complete_run(
        self,
        run_id: int,
        company_id: int,
        payload: dict[str, Any],
        raw_response: str,
        status: str,
    ) -> None:
        with get_conn() as conn:
            store_processing_outputs(conn, run_id, company_id, payload)
            conn.execute(
                """
                UPDATE processing_runs
                SET status = ?, raw_response = ?, parsed_json = ?, finished_at = ?
                WHERE id = ?
                """,
                (status, raw_response, json.dumps(payload), now_iso(), run_id),
            )
            conn.execute(
                "UPDATE companies SET status = ?, updated_at = ? WHERE id = ?",
                (status, now_iso(), company_id),
            )
            conn.commit()

    def _complete_draft_test_run(
        self,
        run_id: int,
        payload: dict[str, Any],
        raw_response: str,
        status: str,
    ) -> None:
        with get_conn() as conn:
            conn.execute(
                """
                UPDATE processing_runs
                SET status = ?, raw_response = ?, parsed_json = ?, finished_at = ?
                WHERE id = ?
                """,
                (status, raw_response, json.dumps(payload), now_iso(), run_id),
            )
            conn.commit()

    def _fail_run(
        self,
        run_id: int,
        company_id: int | None,
        error: str,
        raw_response: str | None = None,
    ) -> None:
        raw_update = ", raw_response = ?" if raw_response else ""
        values: list[Any] = [error]
        if raw_response:
            values.append(raw_response)
        values.extend([now_iso(), run_id])
        with get_conn() as conn:
            conn.execute(
                f"""
                UPDATE processing_runs
                SET status = 'failed', error = ?{raw_update}, finished_at = ?
                WHERE id = ?
                """,
                values,
            )
            if company_id is not None:
                conn.execute(
                    "UPDATE companies SET status = 'failed', updated_at = ? WHERE id = ?",
                    (now_iso(), company_id),
                )
            conn.commit()


processing_service = PortfolioProcessingService()


def process_company(company_id: int, sector_id: int) -> dict[str, Any]:
    return processing_service.process_company(company_id, sector_id)


def test_draft_prompt_section(request: PromptSectionDraftTest) -> dict[str, Any]:
    return processing_service.test_draft_prompt_section(request)


def test_prompt_template(request: PromptTemplateTest) -> dict[str, Any]:
    return processing_service.test_prompt_template(request)


def exception_detail(exc: Exception) -> str:
    if isinstance(exc, HTTPException):
        return str(exc.detail)
    return str(exc)


def run_company_analysis_job(run_id: int, company_id: int, sector_id: int) -> None:
    try:
        processing_service.process_company(company_id, sector_id, run_id=run_id)
    except Exception as exc:
        processing_service._fail_run(run_id, company_id, exception_detail(exc))


def run_prompt_test_job(run_id: int, request: PromptSectionDraftTest) -> None:
    try:
        processing_service.test_draft_prompt_section(request, run_id=run_id)
    except Exception as exc:
        processing_service._fail_run(run_id, None, exception_detail(exc))


def run_template_test_job(run_id: int, request: PromptTemplateTest) -> None:
    try:
        processing_service.test_prompt_template(request, run_id=run_id)
    except Exception as exc:
        processing_service._fail_run(run_id, None, exception_detail(exc))


def start_background_job(target: Any, *args: Any) -> None:
    Thread(target=target, args=args, daemon=True).start()


def create_pdf(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    def esc(text: str) -> str:
        return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    content_lines = ["BT", "/F1 11 Tf", "72 740 Td"]
    for idx, line in enumerate(lines):
        if idx:
            content_lines.append("0 -16 Td")
        content_lines.append(f"({esc(line)}) Tj")
    content_lines.append("ET")
    stream = "\n".join(content_lines).encode("utf-8")

    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream",
    ]

    pdf = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for idx, obj in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{idx} 0 obj\n".encode("ascii"))
        pdf.extend(obj)
        pdf.extend(b"\nendobj\n")
    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.extend(
        f"trailer << /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    path.write_bytes(bytes(pdf))


def create_demo_reports() -> dict[str, Any]:
    companies = {
        "AcmeCloud": [
            "AcmeCloud Q1 2026 Board Report",
            "Sector: SaaS",
            "Reporting period: Q1 2026",
            "ARR reached $12.4M, up 28% year over year.",
            "Revenue was $3.1M for the quarter.",
            "Gross margin improved to 72%.",
            "Adjusted EBITDA was negative $0.4M.",
            "Cash balance was $8.2M at quarter end.",
            "Net revenue retention was 112%. Logo churn was 3.2%.",
            "Management highlighted enterprise pipeline strength and slower SMB conversion.",
        ],
        "AcmeCloud/KPI Appendix": [
            "AcmeCloud KPI Appendix",
            "Reporting period: Q1 2026",
            "Enterprise pipeline reached $18.6M.",
            "CAC payback improved to 16 months.",
            "Headcount ended the quarter at 92 employees.",
            "Expansion revenue was strongest in financial services customers.",
        ],
        "BetaHealth": [
            "BetaHealth Investor Update",
            "Sector: Healthcare",
            "Reporting period: Q1 2026",
            "Revenue was $6.8M, up 14% year over year.",
            "EBITDA was $0.9M.",
            "Gross margin was 48%.",
            "Cash and equivalents were $5.5M.",
            "Patient volume increased 9%. Staffing pressure remains a risk.",
            "Management noted reimbursement delays in two regional markets.",
        ],
        "CobaltIndustrial": [
            "CobaltIndustrial Monthly Portfolio Report",
            "Sector: Industrial",
            "Reporting period: March 2026",
            "Revenue was $9.6M for the month.",
            "Adjusted EBITDA margin was 13%.",
            "Backlog increased to $42M.",
            "Gross margin was 31%, down from 34% due to input costs.",
            "Cash balance was $11.1M.",
            "Supply-chain delays continue to pressure delivery timelines.",
        ],
    }
    for company, lines in companies.items():
        parts = company.split("/", 1)
        company_name = parts[0]
        filename = "report.pdf" if len(parts) == 1 else f"{slugify(parts[1])}.pdf"
        create_pdf(settings.input_dir / company_name / filename, lines)
    return {"created_companies": sorted({company.split('/', 1)[0] for company in companies}), "input_dir": str(settings.input_dir)}


def seed_betahealth_demo_llm_output() -> dict[str, Any]:
    create_demo_reports()
    sync_local_folder()
    with get_conn() as conn:
        company = conn.execute("SELECT * FROM companies WHERE slug = 'betahealth'").fetchone()
        sector = conn.execute("SELECT * FROM sectors WHERE name = 'Healthcare'").fetchone()
    if not company or not sector:
        raise RuntimeError("BetaHealth or Healthcare sector was not found.")

    documents = prepare_company_documents(company["id"], company)
    with get_conn() as conn:
        section_rows = conn.execute(
            "SELECT * FROM prompt_sections WHERE sector_id = ? ORDER BY display_order, id",
            (sector["id"],),
        ).fetchall()
    section_ids = {row["name"]: row["id"] for row in section_rows}
    payload = {
        "company": {"name": "BetaHealth", "sector": "Healthcare", "reporting_period": "Q1 2026"},
        "sections": [
            {
                "id": "financial_snapshot",
                "prompt_section_id": section_ids.get("Financial Snapshot"),
                "type": "metric_cards",
                "title": "Financial Snapshot",
                "items": [
                    {"label": "Revenue", "value": "$6.8M", "period": "Q1 2026"},
                    {"label": "YoY growth", "value": "14%", "period": "Q1 2026"},
                    {"label": "EBITDA", "value": "$0.9M", "period": "Q1 2026"},
                    {"label": "Gross margin", "value": "48%", "period": "Q1 2026"},
                    {"label": "Cash", "value": "$5.5M", "period": "Q1 2026"},
                ],
                "evidence": [
                    {"document_id": documents[0]["document_uid"], "page": 1, "snippet": "Revenue was $6.8M, up 14% year over year."},
                    {"document_id": documents[0]["document_uid"], "page": 1, "snippet": "Cash and equivalents were $5.5M."},
                ],
            },
            {
                "id": "operating_metrics",
                "prompt_section_id": section_ids.get("Operating Metrics"),
                "type": "key_value_table",
                "title": "Operating Metrics",
                "rows": [
                    {"key": "Patient volume", "value": "Increased 9%"},
                    {"key": "Staffing pressure", "value": "Flagged by management"},
                    {"key": "Reimbursement delays", "value": "Two regional markets"},
                    {"key": "Margin profile", "value": "48% gross margin"},
                ],
            },
            {
                "id": "regulatory_execution_risk",
                "prompt_section_id": section_ids.get("Regulatory And Execution Risk"),
                "type": "risk_list",
                "title": "Regulatory And Execution Risk",
                "items": [
                    {"risk": "Reimbursement delays could pressure cash conversion.", "severity": "high"},
                    {"risk": "Staffing pressure may constrain patient volume growth.", "severity": "medium"},
                    {"risk": "Regional exposure creates operating variability.", "severity": "medium"},
                ],
            },
        ],
        "metrics": [
            {"canonical_name": "Revenue", "value": "$6.8M", "period": "Q1 2026"},
            {"canonical_name": "EBITDA", "value": "$0.9M", "period": "Q1 2026"},
            {"canonical_name": "Cash", "value": "$5.5M", "period": "Q1 2026"},
        ],
        "warnings": [],
    }
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO processing_runs
            (company_id, sector_id, status, mode, model, prompt_text, raw_response, parsed_json, started_at, finished_at, run_type)
            VALUES (?, ?, 'processed', 'demo', 'synthetic-demo', ?, ?, ?, ?, ?, 'company_analysis')
            """,
            (
                company["id"],
                sector["id"],
                "Seeded demo LLM-style output for BetaHealth.",
                json.dumps(payload),
                json.dumps(payload),
                now_iso(),
                now_iso(),
            ),
        )
        run_id = cur.lastrowid
        store_processing_outputs(conn, run_id, company["id"], payload)
        conn.execute(
            "UPDATE companies SET sector_id = ?, status = 'processed', updated_at = ? WHERE id = ?",
            (sector["id"], now_iso(), company["id"]),
        )
        conn.execute(
            "UPDATE documents SET status = 'processed', updated_at = ? WHERE company_id = ?",
            (now_iso(), company["id"]),
        )
        conn.commit()
    return {"company": "BetaHealth", "run_id": run_id, "status": "processed"}


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="SAGARD Portfolio Intelligence POC", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/config/status")
def config_status() -> dict[str, Any]:
    return {
        "use_ai": settings.use_ai,
        "openai_key_configured": bool(settings.openai_api_key),
        "openai_model_configured": bool(settings.openai_model),
        "openai_model": settings.openai_model,
        "mode": settings.mode,
        "database_path": str(settings.database_path),
        "input_dir": str(settings.input_dir),
        "processed_dir": str(settings.processed_dir),
    }


@app.post("/api/config")
def update_config(request: ConfigUpdate) -> dict[str, Any]:
    api_key_update = ""
    preserve_blank_key = True
    if request.clear_openai_api_key:
        api_key_update = ""
        preserve_blank_key = False
    elif request.openai_api_key is not None and request.openai_api_key.strip():
        api_key_update = request.openai_api_key.strip()
        preserve_blank_key = False

    write_env_values(
        {
            "USE_AI": "true" if request.use_ai else "false",
            "OPENAI_MODEL": request.openai_model.strip(),
            "OPENAI_API_KEY": api_key_update,
        },
        preserve_blank_key=preserve_blank_key,
    )
    reload_settings()
    return config_status()


@app.post("/api/demo-data")
def api_demo_data() -> dict[str, Any]:
    result = create_demo_reports()
    result["sync"] = sync_local_folder()
    return result


@app.post("/api/demo/betahealth-llm-output")
def api_betahealth_demo_output() -> dict[str, Any]:
    return seed_betahealth_demo_llm_output()


@app.post("/api/sync/local")
def api_sync_local() -> dict[str, Any]:
    return sync_local_folder()


@app.get("/api/processing/queue")
def processing_queue() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT c.*, s.name AS sector_name,
                   COUNT(d.id) AS document_count,
                   MAX(d.updated_at) AS latest_document_update
            FROM companies c
            LEFT JOIN sectors s ON s.id = c.sector_id
            LEFT JOIN documents d ON d.company_id = c.id
            GROUP BY c.id
            ORDER BY c.updated_at DESC
            """
        ).fetchall()
    return rows_to_dicts(rows)


@app.get("/api/companies")
def companies() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            WITH latest_runs AS (
                SELECT company_id, MAX(id) AS latest_run_id
                FROM processing_runs
                WHERE run_type = 'company_analysis' AND status IN ('processed', 'processed_dry_run')
                GROUP BY company_id
            )
            SELECT c.*, s.name AS sector_name,
                   COUNT(DISTINCT d.id) AS document_count,
                   COUNT(DISTINCT so.id) AS section_count,
                   lr.latest_run_id AS latest_run_id,
                   pr.status AS latest_analysis_status
            FROM companies c
            LEFT JOIN sectors s ON s.id = c.sector_id
            LEFT JOIN documents d ON d.company_id = c.id
            LEFT JOIN latest_runs lr ON lr.company_id = c.id
            LEFT JOIN processing_runs pr ON pr.id = lr.latest_run_id
            LEFT JOIN section_outputs so ON so.run_id = lr.latest_run_id
            GROUP BY c.id, lr.latest_run_id, pr.status
            ORDER BY c.updated_at DESC
            """
        ).fetchall()
    results = rows_to_dicts(rows)
    for company in results:
        if company.get("latest_analysis_status"):
            company["status"] = company["latest_analysis_status"]
    return results


@app.get("/api/companies/{company_id}")
def company_detail(company_id: int) -> dict[str, Any]:
    with get_conn() as conn:
        company = conn.execute(
            """
            SELECT c.*, s.name AS sector_name
            FROM companies c
            LEFT JOIN sectors s ON s.id = c.sector_id
            WHERE c.id = ?
            """,
            (company_id,),
        ).fetchone()
        if not company:
            raise HTTPException(status_code=404, detail="Company not found")

        documents = conn.execute(
            "SELECT * FROM documents WHERE company_id = ? ORDER BY filename",
            (company_id,),
        ).fetchall()
        latest_run = conn.execute(
            """
            SELECT * FROM processing_runs
            WHERE company_id = ? AND run_type = 'company_analysis' AND status IN ('processed', 'processed_dry_run')
            ORDER BY id DESC
            LIMIT 1
            """,
            (company_id,),
        ).fetchone()
        sections: list[dict[str, Any]] = []
        if latest_run:
            section_rows = conn.execute(
                "SELECT * FROM section_outputs WHERE run_id = ? ORDER BY id",
                (latest_run["id"],),
            ).fetchall()
            for row in section_rows:
                item = dict(row)
                item["data"] = json.loads(item["data_json"])
                sections.append(item)
        metrics = conn.execute(
            "SELECT * FROM metrics WHERE company_id = ? ORDER BY id",
            (company_id,),
        ).fetchall()

    company_result = row_to_dict(company)
    if company_result and latest_run:
        company_result["status"] = latest_run["status"]
    document_results = rows_to_dicts(documents)
    if latest_run and latest_run["status"] in {"processed", "processed_dry_run"}:
        for document in document_results:
            if document.get("status") == "markdown_ready":
                document["status"] = "processed"

    return {
        "company": company_result,
        "documents": document_results,
        "latest_run": row_to_dict(latest_run),
        "sections": sections,
        "metrics": rows_to_dicts(metrics),
    }


@app.post("/api/companies/{company_id}/process")
def api_process_company(
    company_id: int,
    request: ProcessRequest,
) -> dict[str, Any]:
    run_id = processing_service.create_queued_run(company_id, request.sector_id, "company_analysis")
    start_background_job(run_company_analysis_job, run_id, company_id, request.sector_id)
    return {"run_id": run_id, "status": "queued", "mode": settings.mode}


@app.get("/api/sectors")
def sectors() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM sectors WHERE is_active = 1 ORDER BY name").fetchall()
    return rows_to_dicts(rows)


@app.post("/api/sectors")
def create_sector(request: SectorCreate) -> dict[str, Any]:
    with get_conn() as conn:
        try:
            cur = conn.execute(
                "INSERT INTO sectors (name, description, created_at) VALUES (?, ?, ?)",
                (request.name, request.description, now_iso()),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            raise HTTPException(status_code=409, detail="Sector already exists") from exc
    return {"id": cur.lastrowid, "name": request.name, "description": request.description}


@app.patch("/api/sectors/{sector_id}")
def update_sector(sector_id: int, request: SectorUpdate) -> dict[str, Any]:
    get_sector_or_404(sector_id)
    updates: list[str] = []
    values: list[Any] = []
    if request.description is not None:
        updates.append("description = ?")
        values.append(request.description)
    if not updates:
        return {"updated": False}
    values.append(sector_id)
    with get_conn() as conn:
        conn.execute(f"UPDATE sectors SET {', '.join(updates)} WHERE id = ?", values)
        conn.commit()
    return {"updated": True}


@app.get("/api/prompt-template")
def prompt_template() -> dict[str, Any]:
    template = row_to_dict(get_active_prompt_template())
    if template:
        template["variables"] = PROMPT_TEMPLATE_VARIABLES
    return template


@app.patch("/api/prompt-template")
def update_prompt_template(request: PromptTemplateUpdate) -> dict[str, Any]:
    current = get_active_prompt_template()
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE prompt_templates
            SET template_text = ?, description = ?, version = version + 1, updated_at = ?
            WHERE id = ?
            """,
            (request.template_text, request.description, now_iso(), current["id"]),
        )
        conn.commit()
    return prompt_template()


@app.post("/api/prompt-template/test")
def test_prompt_template_api(
    request: PromptTemplateTest,
) -> dict[str, Any]:
    run_id = processing_service.create_queued_run(request.company_id, request.sector_id, "template_test")
    start_background_job(run_template_test_job, run_id, request)
    return {"run_id": run_id, "status": "queued", "mode": settings.mode}


@app.get("/api/sectors/{sector_id}/sections")
def sector_sections(sector_id: int) -> list[dict[str, Any]]:
    get_sector_or_404(sector_id)
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT * FROM prompt_sections
            WHERE sector_id = ?
            ORDER BY display_order, id
            """,
            (sector_id,),
        ).fetchall()
    return rows_to_dicts(rows)


@app.post("/api/sectors/{sector_id}/sections")
def create_prompt_section(sector_id: int, request: PromptSectionCreate) -> dict[str, Any]:
    get_sector_or_404(sector_id)
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO prompt_sections
            (sector_id, name, section_type, prompt, output_schema, requires_evidence, display_order, version, is_active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1, ?, ?)
            """,
            (
                sector_id,
                request.name,
                request.section_type,
                request.prompt,
                json.dumps(request.output_schema),
                int(request.requires_evidence),
                request.display_order,
                now_iso(),
                now_iso(),
            ),
        )
        conn.commit()
    return {"id": cur.lastrowid}


@app.patch("/api/prompt-sections/{section_id}")
def update_prompt_section(section_id: int, request: PromptSectionUpdate) -> dict[str, Any]:
    updates: list[str] = []
    values: list[Any] = []
    for field in ("name", "section_type", "prompt", "display_order"):
        value = getattr(request, field)
        if value is not None:
            updates.append(f"{field} = ?")
            values.append(value)
    if request.output_schema is not None:
        updates.append("output_schema = ?")
        values.append(json.dumps(request.output_schema))
    if request.requires_evidence is not None:
        updates.append("requires_evidence = ?")
        values.append(int(request.requires_evidence))
    if request.is_active is not None:
        updates.append("is_active = ?")
        values.append(int(request.is_active))
    if not updates:
        return {"updated": False}
    updates.append("version = version + 1")
    updates.append("updated_at = ?")
    values.append(now_iso())
    values.append(section_id)
    with get_conn() as conn:
        cur = conn.execute(f"UPDATE prompt_sections SET {', '.join(updates)} WHERE id = ?", values)
        conn.commit()
    if cur.rowcount == 0:
        raise HTTPException(status_code=404, detail="Prompt section not found")
    return {"updated": True}


@app.post("/api/prompt-sections/{section_id}/test")
def test_prompt_section(section_id: int) -> dict[str, Any]:
    with get_conn() as conn:
        section = conn.execute("SELECT * FROM prompt_sections WHERE id = ?", (section_id,)).fetchone()
        if not section:
            raise HTTPException(status_code=404, detail="Prompt section not found")
    return {
        "mode": "dry_run",
        "message": "Section test endpoint is wired. Select a company-specific test in the UI roadmap to compose full document context.",
        "section": row_to_dict(section),
    }


@app.post("/api/prompt-sections/test-draft")
def test_draft_prompt_section_api(
    request: PromptSectionDraftTest,
) -> dict[str, Any]:
    run_id = processing_service.create_queued_run(request.company_id, request.sector_id, "prompt_test")
    start_background_job(run_prompt_test_job, run_id, request)
    return {"run_id": run_id, "status": "queued", "mode": settings.mode}


@app.get("/api/runs")
def runs() -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT pr.*, c.display_name AS company_name, s.name AS sector_name
            FROM processing_runs pr
            JOIN companies c ON c.id = pr.company_id
            LEFT JOIN sectors s ON s.id = pr.sector_id
            ORDER BY pr.id DESC
            LIMIT 50
            """
        ).fetchall()
    return rows_to_dicts(rows)


@app.get("/api/runs/{run_id}")
def run_detail(run_id: int) -> dict[str, Any]:
    with get_conn() as conn:
        run = conn.execute("SELECT * FROM processing_runs WHERE id = ?", (run_id,)).fetchone()
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
    result = row_to_dict(run)
    if result and result.get("parsed_json"):
        result["parsed"] = json.loads(result["parsed_json"])
    return result


@app.get("/api/documents/{document_id}")
def document_detail(document_id: int) -> dict[str, Any]:
    with get_conn() as conn:
        document = conn.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
        if not document:
            raise HTTPException(status_code=404, detail="Document not found")
    result = row_to_dict(document)
    markdown_path = result.get("markdown_path") if result else None
    if markdown_path and Path(markdown_path).exists():
        result["markdown_preview"] = Path(markdown_path).read_text(encoding="utf-8")[:8000]
    return result


frontend_dir = ROOT_DIR / "frontend"
app.mount("/", StaticFiles(directory=frontend_dir, html=True), name="frontend")
