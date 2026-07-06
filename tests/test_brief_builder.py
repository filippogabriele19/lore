import pytest
import sqlite3
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.symbol_db import SymbolDB
from cli.brief_builder import BriefBuilder


@pytest.fixture
def populated_db(tmp_path):
    """Creates a temporary SQLite DB populated with LORE Knowledge Graph metadata."""
    db_path = tmp_path / "lore_test.db"
    db = SymbolDB(db_path)
    
    conn = db.con
    # Ensure L4 KG tables exist
    conn.execute("""
        CREATE TABLE IF NOT EXISTS intent_nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT NOT NULL UNIQUE,
            intent_json TEXT NOT NULL,
            integrity_score REAL DEFAULT 0.0,
            generated_at TEXT NOT NULL
        );
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS virtual_edges (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            src_file        TEXT NOT NULL,
            dst_file        TEXT NOT NULL,
            co_change_rate  REAL NOT NULL,
            virtual_depth   REAL NOT NULL,
            shared_commits  INTEGER NOT NULL,
            UNIQUE(src_file, dst_file)
        );
    """)
    conn.commit()
    
    # 1. Populate intent_nodes
    intent_json = {
        "canonical_intent": "Manage user authentication sessions secure and clean",
        "evolution_log": [
            {"version": "1.0", "description": "Initial setup"},
            {"version": "1.1", "description": "Add rate limiting support"}
        ]
    }
    conn.execute(
        "INSERT INTO intent_nodes (file_path, intent_json, integrity_score, generated_at) VALUES (?, ?, ?, ?)",
        ("auth.py", json.dumps(intent_json), 0.90, "2026-06-19T00:00:00Z")
    )
    
    # 2. Populate symbols and decision_links
    # Create file row
    file_id = db.upsert_file("auth.py", 100)
    # Create symbol row
    conn.execute(
        "INSERT INTO symbols (name, file_id, line_start, line_end, kind, signature) VALUES (?, ?, ?, ?, ?, ?)",
        ("login", file_id, 10, 30, "function", "def login(username, password):")
    )
    conn.execute(
        "INSERT INTO decision_links (symbol_name, source_type, source_ref, confidence, description) VALUES (?, ?, ?, ?, ?)",
        ("login", "adr", "ADR-007", 0.95, "Never bypass auth middleware")
    )
    
    # 3. Populate hotspots and virtual_edges
    conn.execute(
        "INSERT INTO hotspots (file_path, change_freq, risk_score) VALUES (?, ?, ?)",
        ("auth.py", 20, 0.85)
    )
    conn.execute(
        "INSERT INTO virtual_edges (src_file, dst_file, co_change_rate, virtual_depth, shared_commits) VALUES (?, ?, ?, ?, ?)",
        ("auth.py", "middleware.py", 0.75, 1.25, 8)
    )
    
    # 4. Populate test file in files and symbols for test oracle
    test_file_id = db.upsert_file("test_auth.py", 50)
    conn.execute(
        "INSERT INTO symbols (name, file_id, line_start, line_end, kind, signature) VALUES (?, ?, ?, ?, ?, ?)",
        ("test_login_auth", test_file_id, 5, 15, "function", "def test_login_auth():")
    )
    # Dep from test_login_auth to login
    conn.execute(
        "INSERT INTO deps (from_symbol_id, from_file_id, to_name, dep_type, line) VALUES (?, ?, ?, ?, ?)",
        (2, test_file_id, "login", "call", 7)
    )
    
    # 5. Populate commit_reasoning
    conn.execute(
        "INSERT INTO commit_reasoning (commit_hash, author, date, body, keywords_found, files_touched, commit_diff) VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("abc123456789", "Alice", "2026-06-18", "Fixed login rate limit because of crash", '["because"]', '["auth.py"]', "diff --git a/auth.py b/auth.py\n...")
    )
    
    conn.commit()
    yield db_path
    db.close()


@pytest.fixture
def empty_db(tmp_path):
    """Creates an empty temporary SQLite DB."""
    db_path = tmp_path / "lore_empty.db"
    db = SymbolDB(db_path)
    db.close()
    return db_path


def test_build_brief_empty_kg(empty_db, tmp_path):
    builder = BriefBuilder(empty_db, tmp_path)
    brief = builder.build_brief("auth.py", "Fix auth")
    # Graceful degradation: returns empty string
    assert brief == ""


def test_intent_context(populated_db, tmp_path):
    builder = BriefBuilder(populated_db, tmp_path)
    intent = builder._get_intent_context("auth.py")
    assert "Manage user authentication sessions" in intent
    assert "Integrity: 90%" in intent
    assert "Healthy" in intent
    assert "[v1.0] Initial setup" in intent


def test_decision_context(populated_db, tmp_path):
    builder = BriefBuilder(populated_db, tmp_path)
    decisions = builder._get_decision_context("auth.py")
    assert "[ADR ADR-007]" in decisions
    assert "Never bypass auth middleware" in decisions
    assert "Symbol: login" in decisions


def test_risk_context(populated_db, tmp_path):
    builder = BriefBuilder(populated_db, tmp_path)
    risk = builder._get_risk_context("auth.py")
    assert "Change frequency: 20 commits" in risk
    assert "HIGH hotspot" in risk
    assert "middleware.py (75%)" in risk


@patch("cli.brief_builder.BriefBuilder._git")
def test_git_blame_context(mock_git, populated_db, tmp_path):
    builder = BriefBuilder(populated_db, tmp_path)
    mock_git.side_effect = lambda *args: (
        "abc12345 (Alice 2026-06-18 10) def login(username, password):\n"
        if args[0] == "blame"
        else "Fix login rate limit"
    )
    
    blame = builder._get_git_blame_context("auth.py", range(10, 12))
    assert "RELEVANT BLAME CONTEXT" in blame
    assert "Commit abc12345 by Alice on 2026-06-18" in blame


@patch("cli.brief_builder.BriefBuilder._git")
def test_recent_changes(mock_git, populated_db, tmp_path):
    builder = BriefBuilder(populated_db, tmp_path)
    mock_git.return_value = "abc1234|Alice|2026-06-18|Fix login rate limit"
    
    changes = builder._get_recent_changes("auth.py")
    assert "RECENT COMMITS ON THIS FILE" in changes
    assert "2026-06-18 [abc1234]: Fix login rate limit (by Alice)" in changes


@patch("cli.agent_retrieval._get_embed_model")
def test_similar_fixes(mock_get_embed, populated_db, tmp_path):
    mock_model = MagicMock()
    # Mock model encoding
    mock_model.encode.return_value = [[1.0] * 384]
    mock_get_embed.return_value = mock_model
    
    builder = BriefBuilder(populated_db, tmp_path)
    similar = builder._get_similar_fixes("auth.py", "Fix auth rate limit")
    assert "SIMILAR PREVIOUS FIX" in similar
    assert "Commit: abc12345" in similar
    assert "Fixed login rate limit" in similar


def test_build_audit_brief(populated_db, tmp_path):
    builder = BriefBuilder(populated_db, tmp_path)
    brief = builder.build_audit_brief("auth.py")
    
    assert brief["intent_health"]["score"] == 0.90
    assert brief["intent_health"]["status"] == "Healthy"
    assert brief["risk_profile"]["change_freq"] == 20
    assert brief["risk_profile"]["risk_score"] == 0.85
    assert brief["risk_profile"]["hotspot_rank"] == "High"
    assert brief["documentation"]["has_intent_node"] is True
    assert brief["documentation"]["decision_links_count"] == 1
    assert "middleware.py" in brief["coupling"]["co_change_partners"]


@patch("cli.brief_builder.BriefBuilder._git")
@patch("cli.agent_retrieval._find_related_tests")
@patch("cli.agent_retrieval._get_embed_model")
def test_build_brief_full(mock_get_embed, mock_find_tests, mock_git, populated_db, tmp_path):
    mock_model = MagicMock()
    mock_model.encode.return_value = [[1.0] * 384]
    mock_get_embed.return_value = mock_model
    
    mock_find_tests.return_value = [
        {"test_file": "test_auth.py", "test_name": "test_login_auth", "docstring": "Assert login ok"}
    ]
    
    mock_git.side_effect = lambda *args: (
        "abc12345 (Alice 2026-06-18 10) def login(username, password):\n"
        if args[0] == "blame"
        else "abc1234|Alice|2026-06-18|Fix login rate limit" if args[0] == "log" and "%h" in args[3]
        else ""
    )
    
    builder = BriefBuilder(populated_db, tmp_path)
    brief = builder.build_brief("auth.py", "Fix auth rate limit", focus_lines=[range(10, 12)])
    
    assert "=== ANALYSIS BRIEF for auth.py ===" in brief
    assert "INTENT (why this code exists):" in brief
    assert "Manage user authentication sessions" in brief
    assert "DECISIONS & CONSTRAINTS:" in brief
    assert "Never bypass auth middleware" in brief
    assert "RISK PROFILE:" in brief
    assert "test_login_auth" in brief
    assert "Assert login ok" in brief
    assert "RELEVANT BLAME CONTEXT:" in brief
    assert "RECENT COMMITS ON THIS FILE:" in brief
    assert "SIMILAR PREVIOUS FIX" in brief
    assert "=== END BRIEF ===" in brief

def test_build_signpost_brief(populated_db, tmp_path):
    builder = BriefBuilder(populated_db, tmp_path)
    signpost = builder.build_signpost_brief("auth.py", "Fix auth")
    
    assert "=== LORE CONTEXT SIGNPOST for auth.py ===" in signpost
    assert "INTENT: Manage user authentication sessions secure and clean." in signpost
    assert "RISK PROFILE: High Hotspot (20 commits) | Co-change partner: middleware.py (75%)" in signpost
    assert "ACTIVE DECISIONS & CONSTRAINTS:" in signpost
    assert "- [ADR ADR-007] governs Symbol 'login'" in signpost
    assert "MUST invoke the corresponding LORE MCP tool" in signpost
    assert "=== END SIGNPOST ===" in signpost

@patch("cli.mcp_server._git")
def test_mcp_git_context_tool(mock_git, populated_db):
    from cli.mcp_server import lore_get_git_context
    mock_git.side_effect = lambda *args: (
        "abc12345 (Alice 2026-06-18 10) def login():\n"
        if args[1] == "blame"
        else "abc1234|Alice|2026-06-18|Fix login rate limit"
    )
    
    with patch("cli.mcp_server._resolve_project_db") as mock_resolve:
        mock_resolve.return_value = (Path("."), Path(populated_db))
        
        res = lore_get_git_context(".", "auth.py")
        assert "RECENT COMMITS:" in res
        assert "2026-06-18 [abc1234]: Fix login rate limit (by Alice)" in res
        
        res_blame = lore_get_git_context(".", "auth.py", "10-12")
        assert "RELEVANT BLAME CONTEXT FOR LINES 10-12:" in res_blame
        assert "Commit abc12345 by Alice on 2026-06-18" in res_blame

def test_mcp_get_adr_tool(populated_db, tmp_path):
    with patch("cli.mcp_server._resolve_project_db") as mock_resolve:
        mock_resolve.return_value = (tmp_path, Path(populated_db))
        
        adr_dir = tmp_path / ".lore-docs"
        adr_dir.mkdir(parents=True, exist_ok=True)
        adr_file = adr_dir / "LORE_ADR.md"
        adr_file.write_text(
            "# ADR-007 — Decision Linking\nSome requirements here\n\n# ADR-008 — MCP Context\nMore details",
            encoding="utf-8"
        )
        
        from cli.mcp_server import lore_get_adr
        res = lore_get_adr(str(tmp_path), "ADR-007")
        assert "=== LORE ADR SEARCH FOR ADR-007 ===" in res
        assert "DOCUMENTATION FROM FILE:" in res
        assert "Some requirements here" in res
        assert "GRAPH RELATIONSHIPS & CONSTRAINTS IN DB:" in res
        assert "Never bypass auth middleware" in res


def test_mcp_get_related_tests_tool(populated_db, tmp_path):
    with patch("cli.mcp_server._resolve_project_db") as mock_resolve:
        mock_resolve.return_value = (tmp_path, Path(populated_db))
        
        from cli.mcp_server import lore_get_related_tests
        res = lore_get_related_tests(str(tmp_path), "auth.py")
        
        assert "=== TEST ORACLE & EXPECTED BEHAVIORS FOR auth.py ===" in res
        assert "test_login_auth" in res


def test_mcp_get_similar_fixes_tool(populated_db, tmp_path):
    with patch("cli.mcp_server._resolve_project_db") as mock_resolve, \
         patch("cli.agent_retrieval._get_embed_model") as mock_embed:
        
        mock_resolve.return_value = (tmp_path, Path(populated_db))
        mock_model = MagicMock()
        mock_model.encode.return_value = [[1.0] * 384]
        mock_embed.return_value = mock_model
        
        from cli.mcp_server import lore_get_similar_fixes
        res = lore_get_similar_fixes(str(tmp_path), "auth.py", "Fix login rate limit")
        
        assert "SIMILAR PREVIOUS FIX" in res
        assert "Commit: abc12345" in res
        assert "Fixed login rate limit" in res


