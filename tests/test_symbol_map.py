import pytest
from pathlib import Path
from core.symbol_map import SymbolDB, SymbolRetriever, scan, extract_file

def test_extract_file(temp_project):
    file_path = temp_project / "app.py"
    symbols, imports = extract_file(file_path)
    
    assert len(symbols) == 1
    assert symbols[0].name == "add_user"
    assert symbols[0].kind == "function"
    assert "system" in symbols[0].calls
    assert len(imports) == 0  # import os è interno alla funzione

def test_scan_and_retrieve(temp_project, clean_db):
    # Esegui lo scan sul progetto temporaneo
    scan(temp_project, clean_db)
    
    # Verifica che i file siano inseriti
    rows_files = clean_db.con.execute("SELECT * FROM files").fetchall()
    paths = {r["path"] for r in rows_files}
    assert "app.py" in paths
    assert "view.py" in paths
    
    # Verifica che i simboli siano inseriti
    rows_symbols = clean_db.con.execute("SELECT * FROM symbols").fetchall()
    names = {r["name"] for r in rows_symbols}
    assert "add_user" in names
    assert "get_user_request" in names

    # Inizializza il retriever
    retriever = SymbolRetriever(clean_db, temp_project)
    
    # Cerca il simbolo add_user
    block = retriever.get_symbol_block("add_user")
    assert block is not None
    assert block["symbol"] == "add_user"
    assert block["kind"] == "function"
    assert len(block["called_by"]) > 0
    assert any(c["caller"] == "get_user_request" for c in block["called_by"])

def test_source_detection(temp_project):
    file_src = temp_project / "sources.py"
    file_src.write_text("""
import sys
import os

def read_config():
    # uses open
    with open("config.json") as f:
        return f.read()

def parse_args():
    # uses sys.argv
    return sys.argv[1]

api_token = os.environ.get("TOKEN") # module variable using os.environ
""", encoding="utf-8")

    symbols, imports = extract_file(file_src)
    # Riconverte in dizionario per facilità di asserzione
    sym_map = {s.name: s for s in symbols}
    
    assert sym_map["read_config"].is_source == 1
    assert sym_map["parse_args"].is_source == 1
    assert sym_map["api_token"].is_source == 1

def test_typescript_calls_extraction(temp_project, clean_db):
    # Esegui scansione che indicizza service.ts
    scan(temp_project, clean_db)
    
    # Cerca il simbolo deleteUser
    rows = clean_db.con.execute("SELECT * FROM symbols WHERE name='deleteUser'").fetchall()
    assert len(rows) > 0
    sym_id = rows[0]["id"]
    
    # Cerca le dipendenze della chiamata di deleteUser
    deps_rows = clean_db.con.execute("SELECT * FROM deps WHERE from_symbol_id=? AND dep_type='call'", (sym_id,)).fetchall()
    calls = {r["to_name"] for r in deps_rows}
    assert "auditLog" in calls

def test_symbol_role_classification(temp_project, clean_db):
    # Create a test file
    test_file = temp_project / "test_app.py"
    test_file.write_text("""
import unittest
from app import add_user

class TestApp(unittest.TestCase):
    def test_add_user_success(self):
        assert True
""", encoding="utf-8")
    
    # Process files
    scan(temp_project, clean_db)
    
    # Verify symbols role
    rows = clean_db.con.execute("SELECT name, role FROM symbols").fetchall()
    roles = {r["name"]: r["role"] for r in rows}
    
    assert roles.get("TestApp") == "test"
    assert roles.get("test_add_user_success") == "test"
    assert roles.get("add_user") == "source"

def test_db_role_and_imports(temp_project, clean_db):
    # Create test files
    test_file = temp_project / "test_app.py"
    test_file.write_text("""
import unittest
from app import add_user
""", encoding="utf-8")
    scan(temp_project, clean_db)
    
    # Check get_file_imports
    row_file = clean_db.con.execute("SELECT id FROM files WHERE path='test_app.py'").fetchone()
    assert row_file is not None
    imports = clean_db.get_file_imports(row_file["id"])
    assert "unittest" in imports
    assert "add_user" in imports
    
    # Check all_embeddings_with_role
    sym_row = clean_db.con.execute("SELECT id, name FROM symbols WHERE name='add_user'").fetchone()
    assert sym_row is not None
    dummy_bytes = b'\x00' * 152  # dummy embedding
    clean_db.store_embedding(sym_row["id"], dummy_bytes)
    
    all_emb = clean_db.all_embeddings_with_role()
    assert len(all_emb) > 0
    found = False
    for r in all_emb:
        if r["embedding"] == dummy_bytes:
            found = True
            assert r["role"] == "source"
            assert r["path"] == "app.py"
    assert found

def test_fallback_reparse(temp_project):
    from cli.agent_stage import StageWriter
    from cli.agent_delta import DeltaApplicator
    
    # Create target file
    app_file = temp_project / "target_app.py"
    app_file.write_text("""
def process_data(data):
    # Old logic
    res = data * 2
    return res

def other_func():
    return 42
""", encoding="utf-8")

    stage = StageWriter(temp_project)
    applicator = DeltaApplicator()
    
    # Test case 1: loose SEARCH/REPLACE
    response = """
Here is the fix for target_app.py:
SEARCH:
def process_data(data):
    # Old logic
    res = data * 2
    return res
REPLACE:
def process_data(data):
    # New logic
    res = data * 3
    return res
"""
    n_modified = applicator.fallback_reparse(response, temp_project, stage)
    assert n_modified == 1
    
    staged_path = stage.stage_dir / "target_app.py"
    assert staged_path.exists()
    staged_content = staged_path.read_text(encoding="utf-8")
    assert "res = data * 3" in staged_content
    assert "def other_func():" in staged_content

    # Test case 2: conflict markers
    response_conflict = """
Let's modify target_app.py:
<<<<<<<
def other_func():
    return 42
=======
def other_func():
    return 100
>>>>>>>
"""
    stage_c = StageWriter(temp_project)
    n_modified = applicator.fallback_reparse(response_conflict, temp_project, stage_c)
    assert n_modified == 1
    staged_content = (stage_c.stage_dir / "target_app.py").read_text(encoding="utf-8")
    assert "return 100" in staged_content

    # Test case 3: raw code block matching class/method signature
    response_block = """
Please update target_app.py:
```python
def other_func():
    # modified
    return 200
```
"""
    stage_b = StageWriter(temp_project)
    n_modified = applicator.fallback_reparse(response_block, temp_project, stage_b)
    assert n_modified == 1
    staged_content = (stage_b.stage_dir / "target_app.py").read_text(encoding="utf-8")
    assert "return 200" in staged_content
    assert "# modified" in staged_content

    # Test case 4: SEARCH/REPLACE with inline delimiters (like SEARCH<<< and REPLACE <<<)
    app_file_inline = temp_project / "target_inline.py"
    app_file_inline.write_text("""
def calculate(x):
    y = x + 1
    return y
""", encoding="utf-8")
    
    response_inline = """
FILE: target_inline.py
SEARCH<<<def calculate(x):
    y = x + 1
>>>
REPLACE:
<<<def calculate(x):
    y = x + 100
"""
    stage_i = StageWriter(temp_project)
    n_modified = applicator.apply(response_inline, temp_project, stage_i)
    assert n_modified == 1
    staged_content = (stage_i.stage_dir / "target_inline.py").read_text(encoding="utf-8")
    assert "y = x + 100" in staged_content




