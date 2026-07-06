import pytest
import sqlite3
import os
from pathlib import Path
from unittest.mock import MagicMock, patch
from core.symbol_map import SymbolDB, scan
from core.qa_engine import run_agentic_qa_test
from cli.check_vuln import _main_check_vuln

def test_run_agentic_qa_test(temp_project):
    # Setup a basic task/files to test the QA execution sandbox
    helper_file = temp_project / "helper.py"
    helper_file.write_text("def sanitize(data):\n    return data.replace(';', '')\n", encoding="utf-8")
    
    test_code = """
import sys
from helper import sanitize

def test_happy():
    assert sanitize("safe") == "safe"

def test_sad():
    assert sanitize("dangerous;") == "dangerous"

test_happy()
test_sad()
print("VERIFICATION: SUCCESS")
sys.exit(0)
"""
    
    # Run sandbox test with empty patches (just runs the test code on copied project files)
    res = run_agentic_qa_test(temp_project, {}, test_code)
    assert res["success"] is True
    assert "VERIFICATION: SUCCESS" in res["stdout"]
    assert res["exit_code"] == 0

def test_auto_cure_flow(temp_project):
    # 1. Index the project
    db_path = temp_project / ".lore_poc.db"
    db = SymbolDB(db_path)
    scan(temp_project, db)
    db.close()
    
    # Define candidate patch
    mock_patch = """--- a/app.py
+++ b/app.py
@@ -1,5 +1,4 @@
 def add_user(username):
     # Simula un sink insicuro
-    import os
-    os.system("echo " + username)
+    print("echo " + username)
"""

    mock_test_script = """
import sys
from app import add_user

def test_happy():
    add_user("test")

test_happy()
print("VERIFICATION: SUCCESS")
sys.exit(0)
"""

    # Mock Anthropic messages create responses
    mock_resp_patch = MagicMock()
    mock_resp_patch.content = [MagicMock(text=mock_patch)]
    
    mock_resp_test = MagicMock()
    mock_resp_test.content = [MagicMock(text=mock_test_script)]
    
    with patch("anthropic.Anthropic") as mock_anthropic:
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [mock_resp_patch, mock_resp_test]
        mock_anthropic.return_value = mock_client
        
        # Ensure ANTHROPIC_API_KEY is present
        old_key = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["ANTHROPIC_API_KEY"] = "mock_key"
        
        try:
            # Run check-vuln with auto-cure
            _main_check_vuln(["--project", str(temp_project), "--auto-cure"])
        finally:
            if old_key is not None:
                os.environ["ANTHROPIC_API_KEY"] = old_key
            else:
                del os.environ["ANTHROPIC_API_KEY"]
                
    # Verify patch was applied to app.py
    app_code = (temp_project / "app.py").read_text(encoding="utf-8")
    assert "print(\"echo \" + username)" in app_code
    assert "os.system" not in app_code

    # Verify proof-carrying certificate was generated
    proof_file = temp_project / ".lore" / "auto_cure.patch.proof"
    assert proof_file.exists()
    
    # Verify fingerprint recorded in db
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        cured_paths = conn.execute("SELECT * FROM historical_vulns").fetchall()
        has_cured = len(cured_paths) > 0
    finally:
        conn.close()
    assert has_cured

def test_two_phase_patching(temp_project):
    from cli.agent_runner import _parse_localization_json, run_agent
    from core.symbol_map import SymbolDB, SymbolRetriever
    
    # 1. Test localization JSON parsing helper
    valid_json = """
    ```json
    {
      "target_files": [
        {"path": "django/db/models/lookups.py", "explanation": "fix lookup issue"}
      ]
    }
    ```
    """
    targets = _parse_localization_json(valid_json)
    assert len(targets) == 1
    assert targets[0]["path"] == "django/db/models/lookups.py"
    assert targets[0]["explanation"] == "fix lookup issue"
    
    # Test loose fallback parsing
    loose_text = 'Check "path": "django/db/models/expressions.py" with "explanation": "expression fix"'
    targets_loose = _parse_localization_json(loose_text)
    assert len(targets_loose) == 1
    assert targets_loose[0]["path"] == "django/db/models/expressions.py"
    assert targets_loose[0]["explanation"] == "expression fix"

    # Create files to modify
    target_py = temp_project / "target_func.py"
    target_py.write_text("""
def calculate(x):
    return x * 2
""", encoding="utf-8")

    # 2. Test run_agent in Two-Phase mode using mock LLM client
    db_path = temp_project / ".lore_poc.db"
    db = SymbolDB(db_path)
    scan(temp_project, db)
    retriever = SymbolRetriever(db, temp_project)
    
    # Mock localization response and edit response
    loc_response_text = """
    {
      "target_files": [
        {"path": "target_func.py", "explanation": "change multiplier to 3"}
      ]
    }
    """
    edit_response_text = """
FILE: target_func.py

SEARCH:
<<<
def calculate(x):
    return x * 2
>>>
REPLACE:
<<<
def calculate(x):
    return x * 3
>>>
    """
    
    mock_resp_loc = MagicMock()
    mock_resp_loc.content = [MagicMock(text=loc_response_text)]
    mock_resp_loc.usage = MagicMock(input_tokens=100, output_tokens=50)
    mock_resp_loc.stop_reason = "end_turn"
    
    deconstruct_response_text = '["target_func.py"]'
    mock_resp_dec = MagicMock()
    mock_resp_dec.content = [MagicMock(text=deconstruct_response_text)]
    mock_resp_dec.usage = MagicMock(input_tokens=50, output_tokens=20)
    mock_resp_dec.stop_reason = "end_turn"

    mock_resp_ver = MagicMock()
    mock_resp_ver.content = [MagicMock(text='["target_func.py::calculate"]')]
    mock_resp_ver.usage = MagicMock(input_tokens=60, output_tokens=25)
    mock_resp_ver.stop_reason = "end_turn"

    mock_resp_loc = MagicMock()
    mock_resp_loc.content = [MagicMock(text=loc_response_text)]
    mock_resp_loc.usage = MagicMock(input_tokens=100, output_tokens=50)
    mock_resp_loc.stop_reason = "end_turn"

    mock_resp_arch = MagicMock()
    mock_resp_arch.content = [MagicMock(text="Strategic brief: change calculation multiplier to 3")]
    mock_resp_arch.usage = MagicMock(input_tokens=120, output_tokens=30)
    mock_resp_arch.stop_reason = "end_turn"

    mock_resp_edit = MagicMock()
    mock_resp_edit.content = [MagicMock(text=edit_response_text)]
    mock_resp_edit.usage = MagicMock(input_tokens=150, output_tokens=80)
    mock_resp_edit.stop_reason = "end_turn"

    mock_resp_veto = MagicMock()
    mock_resp_veto.content = [MagicMock(text="<VETO_OVERRIDE_ACCEPT>")]
    mock_resp_veto.usage = MagicMock(input_tokens=180, output_tokens=10)
    mock_resp_veto.stop_reason = "end_turn"

    with patch("core.llm_client.get_llm_client") as mock_get_client, \
         patch("cli.v11_retrieval.get_llm_client", new=mock_get_client):
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = [
            mock_resp_dec, mock_resp_ver, mock_resp_loc, mock_resp_arch, mock_resp_edit, mock_resp_veto
        ]
        mock_get_client.return_value = mock_client
        
        # Run agent
        log_path = temp_project / "fow_logs" / "agent.log"
        res = run_agent(
            task="Change calculation multiplier to 3 in target_func.py",
            project_root=temp_project,
            retriever=retriever,
            db=db,
            log_path=log_path
        )
        
        # Verify changes were staged and written correctly
        assert res["staged_files"]
        assert any(f["path"] == "target_func.py" for f in res["staged_files"])
        assert "target_func.py" in res["diff"]
        assert "return x * 3" in res["diff"]
        
    db.close()


def test_compact_project_map(temp_project):
    from cli.agent_retrieval import _build_compact_project_map
    # Create a couple of mock files
    f1 = temp_project / "file_a.py"
    f1.write_text("def func_a():\n    pass\n", encoding="utf-8")
    f2 = temp_project / "file_b.py"
    f2.write_text("class ClassB:\n    pass\n", encoding="utf-8")

    db_path = temp_project / ".lore_poc_compact.db"
    db = SymbolDB(db_path)
    scan(temp_project, db)

    pmap = _build_compact_project_map(db)
    db.close()

    assert "PROJECT STRUCTURE" in pmap
    assert "file_a.py" in pmap
    assert "file_b.py" in pmap
    assert "func_a(f)" in pmap
    assert "ClassB(C)" in pmap


def test_infer_language():
    from cli.agent_runner import _infer_language
    assert _infer_language("test.py") == "python"
    assert _infer_language("src/index.js") == "javascript"
    assert _infer_language("style.css") == ""
    assert _infer_language("README.md") == "markdown"


def test_validate_syntax(temp_project):
    from cli.agent_runner import _validate_syntax
    # Valid Python
    valid_py = "def test():\n    return 42\n"
    assert _validate_syntax("test.py", valid_py) is None

    # Invalid Python
    invalid_py = "def test(\n    return 42\n"
    err = _validate_syntax("test.py", invalid_py)
    assert err is not None
    assert "SyntaxError" in err

    # Non-python should be skipped (always return None)
    assert _validate_syntax("test.js", "function test() {") is None


def test_validate_and_fix_paths(temp_project):
    from cli.agent_runner import _validate_and_fix_paths
    
    # Setup test files in project
    real_dir = temp_project / "django" / "db" / "models"
    real_dir.mkdir(parents=True, exist_ok=True)
    real_file = real_dir / "lookups.py"
    real_file.write_text("class Lookup:\n    pass\n", encoding="utf-8")

    db_path = temp_project / ".lore_poc_paths.db"
    db = SymbolDB(db_path)
    scan(temp_project, db)

    targets = [
        {"path": "django/db/models/lookups.py", "explanation": "exact match"},
        {"path": "lookups.py", "explanation": "basename match"},
        {"path": "models/lookups.py", "explanation": "suffix match"},
        {"path": "nonexistent.py", "explanation": "no match"},
    ]

    validated = _validate_and_fix_paths(targets, temp_project, db)
    db.close()

    assert len(validated) == 3
    # Exact match kept as is
    assert validated[0]["path"] == "django/db/models/lookups.py"
    assert validated[0]["explanation"] == "exact match"
    
    # Basename match resolved
    assert validated[1]["path"] == "django/db/models/lookups.py"
    
    # Suffix match resolved
    assert validated[2]["path"] == "django/db/models/lookups.py"


def test_astar_bundle_light(temp_project):
    from cli.agent_retrieval import _astar_bundle_light
    from core.symbol_map import SymbolRetriever
    
    f1 = temp_project / "calculator.py"
    f1.write_text("def add(a, b):\n    '''adds two numbers'''\n    return a + b\n", encoding="utf-8")

    db_path = temp_project / ".lore_poc_light.db"
    db = SymbolDB(db_path)
    scan(temp_project, db)
    
    retriever = SymbolRetriever(db, temp_project)
    
    bundle, visited = _astar_bundle_light("add function in calculator", db, retriever)
    db.close()
    
    if bundle:
        assert "SYMBOL SIGNATURES" in bundle
        assert "calculator.py" in bundle
        assert "add" in bundle
        assert "return a + b" not in bundle


