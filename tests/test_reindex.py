import sqlite3
import pytest
from pathlib import Path
from cli.reindex import _main_reindex
from core.symbol_db import SymbolDB

def test_lore_reindex_command(tmp_path):
    # Setup test workspace with DB
    lore_dir = tmp_path / ".lore"
    lore_dir.mkdir(parents=True)
    db_path = lore_dir / "lore.db"
    
    db = SymbolDB(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO files (id, path, lines) VALUES (1, 'main.py', 100)")
    conn.execute("INSERT INTO symbols (id, name, file_id, line_start, line_end, kind, fragility_score) VALUES (10, 'run', 1, 1, 50, 'function', 0)")
    conn.commit()
    conn.close()
    db.close()
    
    # Run reindex
    _main_reindex(["--project", str(tmp_path)])
    
    # Verify metadata table updated
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT value FROM meta WHERE key = 'last_reindex_at'").fetchone()
    assert row is not None
    assert len(row[0]) > 0
    conn.close()
