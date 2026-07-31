"""
tests/test_eu_ai_act.py
─────────────────────────────────────────────────────────────────────────────
Unit tests for EU AI Act Compliance & Audit Engine (Regulation EU 2024/1689).
"""

import pytest
from pathlib import Path
from core.ai_asset_extractor import AIAssetExtractor
from core.eu_ai_act_engine import EUAIActEngine
from cli.ai_act_report import _main_ai_act_report


@pytest.fixture
def test_ai_workspace(tmp_path):
    project = tmp_path / "ai_test_repo"
    project.mkdir()

    # Create dummy Python files with AI framework imports & HITL nodes
    (project / "main.py").write_text("""
import openai
from anthropic import Anthropic

SYSTEM_PROMPT = "You are a helpful AI assistant for financial advisory."

def user_confirmation_gate(action: str) -> bool:
    print(f"Human approval requested for action: {action}")
    return True

def run_agent():
    client = openai.OpenAI()
    if user_confirmation_gate("deploy"):
        print("Action approved by human operator.")
""", encoding="utf-8")

    return project


def test_ai_asset_extractor(test_ai_workspace):
    extractor = AIAssetExtractor(test_ai_workspace)
    assets = extractor.extract_ai_assets()

    assert "OpenAI API" in assets["ai_frameworks"]
    assert "Anthropic Claude API" in assets["ai_frameworks"]
    assert assets["prompts_count"] == 1
    assert assets["hitl_nodes_count"] >= 1
    assert assets["has_ai_integration"] is True


def test_eu_ai_act_engine(test_ai_workspace):
    engine = EUAIActEngine(test_ai_workspace)
    data = engine.run_ai_act_audit()

    assert data["ai_act_score"] >= 65.0
    assert "articles" in data
    assert "article_9" in data["articles"]
    assert "article_14" in data["articles"]
    assert data["articles"]["article_14"]["status"] == "COMPLIANT"

    # Test HTML report generation
    html_out = test_ai_workspace / "report.html"
    engine.generate_html_report(data, html_out)
    assert html_out.exists()
    assert "REGULATION (EU) 2024/1689" in html_out.read_text(encoding="utf-8")


def test_cli_ai_act_report(test_ai_workspace):
    _main_ai_act_report(["--project", str(test_ai_workspace), "--output-dir", str(test_ai_workspace)])
    assert (test_ai_workspace / "ai_act_compliance_report.html").exists()
    assert (test_ai_workspace / "ai_act_compliance_report.md").exists()
    assert (test_ai_workspace / "ai_act_compliance_report.json").exists()
