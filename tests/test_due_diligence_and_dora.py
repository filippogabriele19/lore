"""
tests/test_due_diligence_and_dora.py
─────────────────────────────────────────────────────────────────────────────
Unit tests for LORE Technical Due Diligence and DORA Compliance Audit engines.
"""

import pytest
import sqlite3
import json
from pathlib import Path

from core.symbol_db import SymbolDB
from core.due_diligence_engine import DueDiligenceEngine
from core.dora_compliance_engine import DORAComplianceEngine
from cli.due_diligence import _main_due_diligence
from cli.dora_report import _main_dora_report


@pytest.fixture
def test_workspace(tmp_path):
    """Creates a populated temporary workspace with a mock LORE Knowledge Graph."""
    db_path = tmp_path / ".lore_poc.db"
    db = SymbolDB(db_path)

    conn = db.con
    # Populate symbols & files
    file_id = db.upsert_file("auth.py", 120)
    conn.execute(
        "INSERT INTO symbols (name, file_id, line_start, line_end, kind, signature) VALUES (?, ?, ?, ?, ?, ?)",
        ("login", file_id, 10, 40, "function", "def login(u, p):")
    )
    conn.execute(
        "INSERT INTO decision_links (symbol_name, source_type, source_ref, confidence, description) VALUES (?, ?, ?, ?, ?)",
        ("login", "adr", "ADR-001", 0.95, "OAuth2 flow required")
    )
    conn.execute(
        "INSERT INTO hotspots (file_path, change_freq, risk_score) VALUES (?, ?, ?)",
        ("auth.py", 15, 0.82)
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS virtual_edges (id INTEGER PRIMARY KEY, src_file TEXT, dst_file TEXT, co_change_rate REAL, virtual_depth REAL, shared_commits INTEGER)"
    )
    conn.execute(
        "INSERT INTO virtual_edges (src_file, dst_file, co_change_rate, virtual_depth, shared_commits) VALUES (?, ?, ?, ?, ?)",
        ("auth.py", "user.py", 0.80, 1.1, 10)
    )
    conn.commit()
    db.close()

    yield tmp_path


def test_due_diligence_engine(test_workspace):
    engine = DueDiligenceEngine(test_workspace)
    data = engine.run_due_diligence_audit()

    assert "bus_factor" in data
    assert "health" in data
    assert data["health"]["health_score"] > 0
    assert data["health"]["adr_count"] == 1

    # Test report generation
    html_out = test_workspace / "due_diligence_report.html"
    md_out = test_workspace / "due_diligence_report.md"

    engine.generate_html_report(data, html_out)
    engine.generate_markdown_report(data, md_out)

    assert html_out.exists()
    assert md_out.exists()
    assert "Technical Due Diligence" in html_out.read_text(encoding="utf-8")


def test_dora_compliance_engine(test_workspace):
    engine = DORAComplianceEngine(test_workspace)
    data = engine.run_dora_audit()

    assert "dora_score" in data
    assert "compliance_tier" in data
    assert "article_6" in data["articles"]
    assert "article_9" in data["articles"]
    assert "article_11" in data["articles"]

    # Test report generation
    html_out = test_workspace / "dora_compliance_report.html"
    md_out = test_workspace / "dora_compliance_report.md"

    engine.generate_html_report(data, html_out)
    engine.generate_markdown_report(data, md_out)

    assert html_out.exists()
    assert md_out.exists()
    assert "EU DORA" in html_out.read_text(encoding="utf-8")


def test_cli_due_diligence(test_workspace):
    _main_due_diligence(["--project", str(test_workspace), "--output-dir", str(test_workspace)])
    assert (test_workspace / "due_diligence_report.html").exists()
    assert (test_workspace / "due_diligence_report.md").exists()
    assert (test_workspace / "due_diligence_report.json").exists()


def test_cli_dora_report(test_workspace):
    _main_dora_report(["--project", str(test_workspace), "--output-dir", str(test_workspace)])
    assert (test_workspace / "dora_compliance_report.html").exists()
    assert (test_workspace / "dora_compliance_report.md").exists()
    assert (test_workspace / "dora_compliance_report.json").exists()
