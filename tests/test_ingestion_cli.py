import pytest
import os
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from lore import main
from core.symbol_map import SymbolDB, scan

def test_ingest_github_command(temp_project):
    # Setup temporary project and DB
    db_path = temp_project / ".lore_poc.db"
    db = SymbolDB(db_path)
    scan(temp_project, db)
    db.close()
    
    # Mock Anthropic and GitHub API calls
    mock_pr_response = [
        {
            "number": 42,
            "title": "Fix memory leak in session validation",
            "body": "Constraint: sessions must have an explicit timeout.",
            "html_url": "https://github.com/mock/repo/pull/42",
            "merged_at": "2026-07-07T12:00:00Z"
        }
    ]
    
    mock_rule_json = {
        "rules": [
            {
                "target_file": "app.py",
                "symbol_name": "validate_session",
                "rule_title": "Session timeout invariant",
                "rule_description": "Sessions must have an explicit timeout"
            }
        ]
    }
    
    mock_llm_response = MagicMock()
    mock_llm_response.content = [MagicMock(text=json.dumps(mock_rule_json))]
    
    with patch("cli.ingest_github.fetch_github_api", return_value=mock_pr_response), \
         patch("anthropic.Anthropic") as mock_anthropic, \
         patch("sys.argv", ["lore.py", "ingest-github", "--repo", "mock/repo", "--project", str(temp_project), "--type", "prs"]):
        
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_llm_response
        mock_anthropic.return_value = mock_client
        
        # Run CLI command
        main()

    # Verify ADR file was created
    adr_dir = temp_project / ".lore" / "adr"
    assert adr_dir.exists()
    adr_files = list(adr_dir.glob("adr_gh_pr_*.md"))
    assert len(adr_files) == 1
    
    # Verify links registered in database
    db = SymbolDB(db_path)
    links = db.con.execute("SELECT * FROM decision_links WHERE source_type='github_pr'").fetchall()
    db.close()
    assert len(links) == 1
    assert links[0]["symbol_name"] == "validate_session"


def test_ingest_slack_command(temp_project):
    # Setup temporary project and DB
    db_path = temp_project / ".lore_poc.db"
    db = SymbolDB(db_path)
    scan(temp_project, db)
    db.close()
    
    # Mock Slack and Anthropic API calls
    mock_slack_response = [
        {
            "user": "U111",
            "text": "Make sure all API key reads are cached."
        }
    ]
    
    mock_rule_json = {
        "rules": [
            {
                "target_file": "config.py",
                "symbol_name": "get_api_key",
                "rule_title": "Cache API keys",
                "rule_description": "API key reads must be cached in memory"
            }
        ]
    }
    
    mock_llm_response = MagicMock()
    mock_llm_response.content = [MagicMock(text=json.dumps(mock_rule_json))]
    
    with patch("cli.ingest_slack.fetch_slack_history", return_value=mock_slack_response), \
         patch("anthropic.Anthropic") as mock_anthropic, \
         patch("sys.argv", ["lore.py", "ingest-slack", "--channel", "C12345", "--token", "xoxb-mock", "--project", str(temp_project)]):
        
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_llm_response
        mock_anthropic.return_value = mock_client
        
        # Run CLI command
        main()

    # Verify ADR file was created
    adr_dir = temp_project / ".lore" / "adr"
    assert adr_dir.exists()
    adr_files = list(adr_dir.glob("adr_slack_*.md"))
    assert len(adr_files) == 1
    
    # Verify links registered in database
    db = SymbolDB(db_path)
    links = db.con.execute("SELECT * FROM decision_links WHERE source_type='slack_msg'").fetchall()
    db.close()
    assert len(links) == 1
    assert links[0]["symbol_name"] == "get_api_key"
