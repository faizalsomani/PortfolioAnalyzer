from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

import backend.main as main


@dataclass
class RuntimeSettingsFixture:
    root: Path
    use_ai: bool = False
    openai_api_key: str = ""
    openai_model: str = ""

    @property
    def database_path(self) -> Path:
        return self.root / "data" / "app.db"

    @property
    def input_dir(self) -> Path:
        return self.root / "data" / "input"

    @property
    def processed_dir(self) -> Path:
        return self.root / "data" / "processed"

    @property
    def ai_ready(self) -> bool:
        return self.use_ai and bool(self.openai_api_key) and bool(self.openai_model)

    @property
    def mode(self) -> str:
        return "openai" if self.ai_ready else "dry_run"


@pytest.fixture()
def app_state(tmp_path, monkeypatch):
    settings = RuntimeSettingsFixture(root=tmp_path)
    monkeypatch.setattr(main, "settings", settings)
    main.init_db()
    main.create_demo_reports()
    main.sync_local_folder()
    return settings


def count_prompt_sections() -> int:
    with main.get_conn() as conn:
        return conn.execute("SELECT COUNT(*) AS count FROM prompt_sections").fetchone()["count"]


def test_dry_run_creates_section_previews_and_full_request(app_state):
    result = main.process_company(company_id=1, sector_id=1)

    assert result["status"] == "processed_dry_run"
    titles = [section["title"] for section in result["payload"]["sections"]]
    assert "Dry Run: Financial Snapshot" in titles
    assert "Dry Run: Retention And Growth" in titles
    assert "Dry Run: Moat And Competitive Position" in titles
    assert "Dry Run: Key Risks" in titles
    assert "Full OpenAI Request Preview" in titles

    full_prompt = result["payload"]["sections"][-1]["body"]
    assert "report.pdf" in full_prompt
    assert "kpi-appendix.pdf" in full_prompt
    assert "No request was sent to OpenAI" in result["payload"]["warnings"][0]


def test_openai_processing_makes_one_call_for_whole_company(app_state, monkeypatch):
    app_state.use_ai = True
    app_state.openai_api_key = "test-key"
    app_state.openai_model = "test-model"
    calls: list[str] = []

    def fake_call(prompt_text: str, model_client=None) -> str:
        calls.append(prompt_text)
        return json.dumps(
            {
                "company": {"name": "AcmeCloud", "sector": "SaaS", "reporting_period": "Q1 2026"},
                "sections": [
                    {
                        "id": "financial_snapshot",
                        "type": "metric_cards",
                        "title": "Financial Snapshot",
                        "items": [{"label": "ARR", "value": 12400000, "unit": "USD"}],
                    }
                ],
                "metrics": [{"canonical_name": "ARR", "value": 12400000, "unit": "USD", "period": "Q1 2026"}],
                "warnings": [],
            }
        )

    monkeypatch.setattr(main, "call_openai_json", fake_call)
    result = main.process_company(company_id=1, sector_id=1)

    assert result["status"] == "processed"
    assert len(calls) == 1
    assert "### Section: Financial Snapshot" in calls[0]
    assert "### Section: Key Risks" in calls[0]
    assert "report.pdf" in calls[0]
    assert "kpi-appendix.pdf" in calls[0]
    with main.get_conn() as conn:
        stored = conn.execute(
            "SELECT prompt_section_id FROM section_outputs WHERE run_id = ? AND section_key = 'financial_snapshot'",
            (result["run_id"],),
        ).fetchone()
    assert stored["prompt_section_id"] == 1


def test_openai_processing_normalizes_minor_schema_drift(app_state, monkeypatch):
    app_state.use_ai = True
    app_state.openai_api_key = "test-key"
    app_state.openai_model = "test-model"

    def fake_call(prompt_text: str, model_client=None) -> str:
        return json.dumps(
            {
                "sections": [
                    {
                        "id": "section_1",
                        "type": "metric_cards",
                        "metrics": [{"name": "ARR", "value": "$12.4M"}],
                    },
                    {
                        "id": "retention_and_growth",
                        "rows": [{"key": "NRR", "value": "112%"}],
                    },
                ],
                "metrics": [],
            }
        )

    monkeypatch.setattr(main, "call_openai_json", fake_call)
    result = main.process_company(company_id=1, sector_id=1)

    assert result["status"] == "processed"
    sections = result["payload"]["sections"]
    assert sections[0]["title"] == "Financial Snapshot"
    assert sections[0]["items"] == [{"name": "ARR", "value": "$12.4M", "label": "ARR"}]
    assert sections[1]["title"] == "Retention And Growth"
    assert sections[1]["type"] == "key_value_table"
    assert sections[1]["rows"] == [{"key": "NRR", "value": "112%"}]


def test_openai_processing_flattens_nested_metric_values(app_state, monkeypatch):
    app_state.use_ai = True
    app_state.openai_api_key = "test-key"
    app_state.openai_model = "test-model"

    def fake_call(prompt_text: str, model_client=None) -> str:
        return json.dumps(
            {
                "sections": [
                    {
                        "id": "section_1",
                        "type": "metric_cards",
                        "metrics": {
                            "revenue": {
                                "value": 7.8,
                                "unit": "M",
                                "evidence": {"document_id": "doc_1", "page": 1, "snippet": "Revenue was $7.8M"},
                            }
                        },
                    },
                    {
                        "id": "retention_and_growth",
                        "type": "key_value_table",
                        "metrics": {
                            "sales_channels": {
                                "value": {"DTC": "68%", "marketplace": "22%"},
                                "evidence": {"document_id": "doc_2", "page": 1, "snippet": "Sales channels..."},
                            },
                            "retention": {"value": 39, "unit": "%"},
                        },
                    },
                ],
                "metrics": [],
            }
        )

    monkeypatch.setattr(main, "call_openai_json", fake_call)
    result = main.process_company(company_id=1, sector_id=1)

    sections = result["payload"]["sections"]
    assert sections[0]["items"][0]["label"] == "revenue"
    assert sections[0]["items"][0]["value"] == 7.8
    assert sections[0]["items"][0]["unit"] == "M"
    assert sections[1]["rows"][0]["value"] == "DTC: 68%; Marketplace: 22%"
    assert sections[1]["rows"][1]["value"] == "39%"


def test_draft_prompt_test_does_not_replace_company_analysis(app_state):
    processed = main.process_company(company_id=1, sector_id=1)
    before = count_prompt_sections()
    result = main.test_draft_prompt_section(
        main.PromptSectionDraftTest(
            company_id=1,
            sector_id=1,
            name="Liquidity Test",
            section_type="narrative",
            prompt="Assess cash runway and liquidity risk from the provided documents.",
        )
    )
    after = count_prompt_sections()

    assert after == before
    assert result["status"] == "draft_test_dry_run"
    titles = [section["title"] for section in result["payload"]["sections"]]
    assert "Draft Test: Liquidity Test" in titles
    with main.get_conn() as conn:
        latest_any = conn.execute(
            "SELECT status FROM processing_runs WHERE company_id = 1 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        stored_draft_outputs = conn.execute(
            "SELECT COUNT(*) AS count FROM section_outputs WHERE run_id = ?",
            (result["run_id"],),
        ).fetchone()
    assert latest_any["status"] == "draft_test_dry_run"
    assert stored_draft_outputs["count"] == 0
    detail = main.company_detail(1)
    assert detail["latest_run"]["id"] == processed["run_id"]
    assert detail["company"]["status"] == "processed_dry_run"
    assert {document["status"] for document in detail["documents"]} == {"processed"}
    assert all(not section["title"].startswith("Draft Test:") for section in detail["sections"])


def test_prompt_template_test_does_not_replace_company_analysis(app_state):
    processed = main.process_company(company_id=1, sector_id=1)
    result = main.test_prompt_template(
        main.PromptTemplateTest(
            company_id=1,
            sector_id=1,
            template_text=(
                "Custom template test\n"
                "{{company_context}}\n"
                "{{sector_context}}\n"
                "{{section_bundle}}\n"
                "{{document_context}}\n"
            ),
        )
    )

    assert result["status"] == "template_test_dry_run"
    assert "Custom template test" in result["payload"]["sections"][-1]["body"]
    detail = main.company_detail(1)
    assert detail["latest_run"]["id"] == processed["run_id"]
    assert detail["company"]["status"] == "processed_dry_run"


def test_sync_counts_unprocessed_companies_not_documents(app_state):
    summary = main.sync_local_folder()

    assert summary["total_companies"] == 3
    assert summary["total_documents"] == 4
    assert summary["unprocessed_companies"] == 3


def test_company_cards_count_latest_run_sections_only(app_state):
    processed = main.process_company(company_id=1, sector_id=1)
    main.test_draft_prompt_section(
        main.PromptSectionDraftTest(
            company_id=1,
            sector_id=1,
            name="Liquidity Test",
            section_type="narrative",
            prompt="Assess liquidity risk.",
        )
    )

    acme = next(company for company in main.companies() if company["display_name"] == "AcmeCloud")

    assert acme["latest_run_id"] == processed["run_id"]
    assert acme["section_count"] == 5
