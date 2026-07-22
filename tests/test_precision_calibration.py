import sqlite3
import pytest
from pathlib import Path
from core.symbol_db import SymbolDB
from core._dl_link_builders import links_from_commit_reasoning, links_from_hotspots, _BOILERPLATE_SYMBOLS
from core.decision_linker import DecisionLinker

def test_decision_link_scoping_and_boilerplate_filter(tmp_path):
    db_path = tmp_path / "test_lore.db"
    db = SymbolDB(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    # Insert test file and symbols
    conn.execute("INSERT INTO files (id, path, lines) VALUES (1, 'django/forms/fields.py', 500)")
    conn.execute("INSERT INTO files (id, path, lines) VALUES (2, 'django/db/models/base.py', 800)")
    
    conn.execute("INSERT INTO symbols (id, name, file_id, line_start, line_end, kind) VALUES (10, '__init__', 1, 10, 20, 'function')")
    conn.execute("INSERT INTO symbols (id, name, file_id, line_start, line_end, kind) VALUES (11, 'clean', 1, 30, 40, 'function')")
    conn.execute("INSERT INTO symbols (id, name, file_id, line_start, line_end, kind) VALUES (12, '__init__', 2, 10, 20, 'function')")
    conn.execute("INSERT INTO symbols (id, name, file_id, line_start, line_end, kind) VALUES (13, 'save', 2, 50, 60, 'function')")
    
    # Insert generic commit reasoning touching both files
    conn.execute("""
        INSERT INTO commit_reasoning (commit_hash, body, keywords_found, files_touched)
        VALUES ('abc123456789', 'Fixed validation logic in fields.py clean method because of edge case', '["fix"]', '["django/forms/fields.py", "django/db/models/base.py"]')
    """)
    conn.commit()
    
    # Build links
    link_count = links_from_commit_reasoning(conn)
    conn.commit()
    
    # Verify links
    links = conn.execute("SELECT symbol_name, file_path FROM decision_links WHERE source_type = 'commit'").fetchall()
    link_symbols = [r["symbol_name"] for r in links]
    
    # '__init__' should be filtered out because it is in _BOILERPLATE_SYMBOLS and not in body text
    assert "__init__" not in link_symbols
    assert "clean" in link_symbols
    assert "save" in link_symbols
    
    # Test DecisionLinker scoped query
    linker = DecisionLinker(db_path)
    ctx = linker.get_context([("django/forms/fields.py", "clean")])
    assert len(ctx) >= 1
    assert ctx[0]["symbol_name"] == "clean"
    
    conn.close()
    db.close()

def test_fragility_score_column_migration(tmp_path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    # Create legacy table without fragility_score
    conn.execute("CREATE TABLE symbols (id INTEGER PRIMARY KEY, name TEXT, file_id INTEGER)")
    conn.commit()
    conn.close()
    
    # Initializing SymbolDB should run migration
    db = SymbolDB(db_path)
    conn = sqlite3.connect(db_path)
    symbol_cols = [r[1] for r in conn.execute("PRAGMA table_info(symbols)").fetchall()]
    assert "fragility_score" in symbol_cols
    conn.close()
    db.close()
