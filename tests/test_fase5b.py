import pytest
import os
import sys
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch
from cli.git_hook import _main_git_hook
from cli.check_vuln import _main_check_vuln
from cli.mcp_server import lore_comply_and_apply
from core.symbol_map import SymbolDB, scan

def test_git_hook_lifecycle(temp_project):
    # Setup a mock .git directory inside the temp folder
    git_dir = temp_project / ".git"
    git_dir.mkdir(exist_ok=True)
    
    # Install
    _main_git_hook(["install", "--project", str(temp_project)])
    hook_file = git_dir / "hooks" / "pre-commit"
    assert hook_file.exists()
    
    content = hook_file.read_text(encoding="utf-8")
    assert "pre-commit hook" in content
    assert "check-vuln --patch-staged" in content
    
    # Uninstall
    _main_git_hook(["uninstall", "--project", str(temp_project)])
    assert not hook_file.exists()

def test_check_vuln_staged_compliance(temp_project):
    # Initialize real git repository inside temp_project
    subprocess.run(["git", "init"], cwd=str(temp_project), check=True)
    
    # Config git user for commits
    subprocess.run(["git", "config", "user.name", "LORE Tester"], cwd=str(temp_project), check=True)
    subprocess.run(["git", "config", "user.email", "tester@lore.ai"], cwd=str(temp_project), check=True)
    
    # Create an app.py file with a vulnerable flow
    app_file = temp_project / "app.py"
    app_file.write_text("def run(inp):\n    eval(inp)\n", encoding="utf-8")
    
    # Create DB and scan
    db_path = temp_project / ".lore_poc.db"
    db = SymbolDB(db_path)
    scan(temp_project, db)
    db.close()
    
    # Commit baseline
    subprocess.run(["git", "add", "app.py"], cwd=str(temp_project), check=True)
    subprocess.run(["git", "commit", "-m", "Initial baseline"], cwd=str(temp_project), check=True)
    
    # Now let's stage a safe patch
    app_file.write_text("def run(inp):\n    # safe print added\n    print('test')\n    eval(inp)\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=str(temp_project), check=True)
    
    # Running check-vuln with --patch-staged
    _main_check_vuln(["--project", str(temp_project), "--patch-staged"])
    
    # Test fail-on-regression exits on regression or errors
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS historical_vulns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_symbol TEXT,
            sink_symbol TEXT,
            path_fingerprint TEXT UNIQUE NOT NULL,
            cured_at TEXT DEFAULT CURRENT_TIMESTAMP,
            description TEXT
        )
    """)
    conn.execute("INSERT OR IGNORE INTO historical_vulns (source_symbol, sink_symbol, path_fingerprint, description) VALUES ('inp', 'eval', 'dummy_hash', 'Cured path')")
    conn.commit()
    conn.close()

def test_mcp_comply_and_apply(temp_project):
    # Setup database
    db_path = temp_project / ".lore_poc.db"
    db = SymbolDB(db_path)
    db.close()
    
    # Inject an ADR into decision_links
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS decision_links (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol_name TEXT, source_type TEXT, source_ref TEXT,
            confidence REAL, description TEXT
        )
    """)
    conn.execute(
        "INSERT INTO decision_links (symbol_name, source_type, source_ref, confidence, description) "
        "VALUES ('yaml_load', 'ADR', 'ADR-005', 0.95, 'Use safe_load for all yaml imports')"
    )
    conn.commit()
    conn.close()
    
    # Execute comply_and_apply mcp tool
    res = lore_comply_and_apply(str(temp_project), "Configure yaml_load endpoint")
    assert "ADR-005" in res
    assert "yaml_load" in res
    assert "safe_load" in res
    
    # Test fallback message
    res_fallback = lore_comply_and_apply(str(temp_project), "Just implement a simple print statement")
    assert "No specific architectural constraints" in res_fallback
