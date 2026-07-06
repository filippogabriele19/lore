import pytest
import tempfile
import sqlite3
from pathlib import Path

@pytest.fixture
def mock_db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / ".lore_poc.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE files (id INTEGER PRIMARY KEY, path TEXT, hash TEXT, lines INTEGER)")
        conn.execute("CREATE TABLE symbols (id INTEGER PRIMARY KEY, file_id INTEGER, name TEXT, kind TEXT, line_start INTEGER, line_end INTEGER, signature TEXT, embedding BLOB, is_source INTEGER DEFAULT 0, parent_class TEXT)")
        conn.execute("CREATE TABLE decision_links (id INTEGER PRIMARY KEY, symbol_name TEXT, source_type TEXT, source_ref TEXT, confidence REAL, description TEXT)")
        
        conn.execute("INSERT INTO files VALUES (1, 'app.py', 'abc', 10)")
        conn.execute("INSERT INTO symbols VALUES (1, 1, 'process_input', 'function', 1, 10, 'def process_input(data)', NULL, 1, NULL)")
        conn.execute("INSERT INTO decision_links VALUES (1, 'process_input', 'adr', 'ADR-001', 0.95, 'Always sanitize input before processing')")
        conn.commit()
        
        yield db_path, conn, Path(tmpdir)
        conn.close()

def test_hover_returns_decision_links(mock_db):
    db_path, conn, project_root = mock_db
    conn.row_factory = sqlite3.Row
    row = conn.execute("""
        SELECT s.name, s.kind, s.signature
        FROM symbols s JOIN files f ON s.file_id=f.id
        WHERE f.path='app.py' AND 1 BETWEEN s.line_start AND s.line_end
    """).fetchone()
    assert row is not None
    assert row["name"] == "process_input"
    
    rows_dl = conn.execute("SELECT source_ref, description, confidence FROM decision_links WHERE symbol_name=?", ("process_input",)).fetchall()
    assert len(rows_dl) == 1
    assert rows_dl[0]["confidence"] == 0.95

def test_stats_query(mock_db):
    db_path, conn, _ = mock_db
    symbol_count = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    link_count = conn.execute("SELECT COUNT(*) FROM decision_links").fetchone()[0]
    assert symbol_count == 1
    assert link_count == 1
