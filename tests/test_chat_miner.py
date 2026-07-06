import pytest
import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.chat_miner import find_claude_history_file, extract_last_chat_session, mine_chat_intent
from core.symbol_db import SymbolDB

def test_extract_last_chat_session(tmp_path):
    # Setup mock history.jsonl
    history_file = tmp_path / "history.jsonl"
    
    project_a = tmp_path / "projA"
    project_b = tmp_path / "projB"
    project_a.mkdir()
    project_b.mkdir()
    
    # Session A: project A, older
    line1 = {"display": "older msg A", "timestamp": 1000, "project": str(project_a), "sessionId": "sess-a"}
    # Session B: project B
    line2 = {"display": "msg B", "timestamp": 2000, "project": str(project_b), "sessionId": "sess-b"}
    # Session C: project A, newer
    line3 = {"display": "newer msg A 1", "timestamp": 3000, "project": str(project_a), "sessionId": "sess-c"}
    line4 = {"display": "newer msg A 2", "timestamp": 3100, "project": str(project_a), "sessionId": "sess-c"}
    
    with open(history_file, "w", encoding="utf-8") as f:
        f.write(json.dumps(line1) + "\n")
        f.write(json.dumps(line2) + "\n")
        f.write(json.dumps(line3) + "\n")
        f.write(json.dumps(line4) + "\n")
        
    # Test extraction for Project A (should get sess-c and messages 1 and 2)
    sess_id, msgs = extract_last_chat_session(history_file, project_a)
    assert sess_id == "sess-c"
    assert len(msgs) == 2
    assert msgs[0] == "newer msg A 1"
    assert msgs[1] == "newer msg A 2"
    
    # Test extraction for Project B (should get sess-b)
    sess_id_b, msgs_b = extract_last_chat_session(history_file, project_b)
    assert sess_id_b == "sess-b"
    assert len(msgs_b) == 1
    assert msgs_b[0] == "msg B"

@patch("core.chat_miner.find_claude_history_file")
@patch("anthropic.Anthropic")
def test_mine_chat_intent(mock_anthropic, mock_find_history, tmp_path):
    # Setup temporary project path and DB
    project_path = tmp_path / "my_project"
    project_path.mkdir()
    
    db_path = project_path / ".lore_poc.db"
    db = SymbolDB(db_path)
    db.close()
    
    history_file = project_path / "history.jsonl"
    mock_find_history.return_value = history_file
    
    # Write history log containing design rules
    log_data = {"display": "use safe_load in settings.py", "timestamp": 5000, "project": str(project_path), "sessionId": "session-xyz"}
    history_file.write_text(json.dumps(log_data) + "\n", encoding="utf-8")
    
    # Setup mock LLM response
    mock_client = MagicMock()
    mock_anthropic.return_value = mock_client
    
    mock_message = MagicMock()
    mock_message.content = [MagicMock(text=json.dumps({
        "rules": [
            {
                "target_file": "settings.py",
                "symbol_name": "load_settings",
                "rule_title": "Safe Settings Load",
                "rule_description": "Always use yaml.safe_load instead of unsafe loaders."
            }
        ]
    }))]
    mock_client.messages.create.return_value = mock_message
    
    with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "fake-key"}):
        rules_count = mine_chat_intent(db_path, project_path)
        
    assert rules_count == 1
    
    # Verify ADR file was written
    adr_files = list((project_path / ".lore" / "adr").glob("adr_chat_*.md"))
    assert len(adr_files) == 1
    adr_text = adr_files[0].read_text(encoding="utf-8")
    assert "session-xyz" in adr_text
    assert "Always use yaml.safe_load" in adr_text
    
    # Verify SQLite decision links table populated
    db = SymbolDB(db_path)
    try:
        row = db.con.execute("SELECT symbol_name, source_type, description FROM decision_links").fetchone()
        assert row is not None
        assert row["symbol_name"] == "load_settings"
        assert row["source_type"] == "chat_adr"
        assert row["description"] == "Safe Settings Load"
        
        # Verify idempotent marker in meta
        meta_row = db.con.execute("SELECT value FROM meta WHERE key='last_processed_chat_session'").fetchone()
        assert meta_row is not None
        assert meta_row["value"] == "session-xyz"
    finally:
        db.close()
        
    # Second execution should skip because it is already processed
    with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "fake-key"}):
        rules_count_again = mine_chat_intent(db_path, project_path)
    assert rules_count_again == 0
