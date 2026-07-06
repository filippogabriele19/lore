import pytest
import io
import sys
import json
import sqlite3
import hashlib
from pathlib import Path
from cli.check_vuln import _main_check_vuln
from cli.lsp import _main_lsp
from core.symbol_map import SymbolDB, scan

# Test regression detection end-to-end
def test_regression_detection(temp_project):
    # Setup files in the temporary project directory
    view_file = temp_project / "view.py"
    view_file.write_text("""def my_view(request):
    data = request.GET['q']
    process_data(data)
""", encoding="utf-8")

    handler_file = temp_project / "handler.py"
    handler_file.write_text("""def process_data(payload):
    eval(payload)
""", encoding="utf-8")

    # Index sandbox
    db_path = temp_project / ".lore_poc.db"
    db = SymbolDB(db_path)
    scan(temp_project, db)
    db.close()
    
    # 1. Create a patch that cures the vulnerability (eval -> print)
    patch_cure = """--- a/handler.py
+++ b/handler.py
@@ -1,2 +1,2 @@
 def process_data(payload):
-    eval(payload)
+    print(payload)
"""
    patch_file = temp_project / "cure.patch"
    patch_file.write_text(patch_cure, encoding="utf-8")
    
    proof_path = Path(str(patch_file) + ".proof")
    if proof_path.exists():
        proof_path.unlink()
        
    # Run check-vuln with curing patch
    _main_check_vuln(["--project", str(temp_project), "--patch", str(patch_file)])
    
    # Verify proof was created, indicating success
    assert proof_path.exists()
    
    # Verify fingerprint was stored in database
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cured_paths = conn.execute("SELECT * FROM historical_vulns").fetchall()
    assert len(cured_paths) == 1
    
    fingerprint = cured_paths[0]["path_fingerprint"]
    assert fingerprint is not None
    
    # 2. Simulate a regressive patch that re-introduces the vulnerability
    # (keeps eval or changes print back to eval)
    patch_regress = """--- a/handler.py
+++ b/handler.py
@@ -1,2 +1,2 @@
 def process_data(payload):
-    print(payload)
+    eval(payload)
"""
    patch_regress_file = temp_project / "regress.patch"
    patch_regress_file.write_text(patch_regress, encoding="utf-8")
    
    proof_regress_path = Path(str(patch_regress_file) + ".proof")
    if proof_regress_path.exists():
        proof_regress_path.unlink()
        
    # Temporarily restore the print version to baseline so the regression diff applies
    handler_file.write_text("""def process_data(payload):
    print(payload)
""", encoding="utf-8")
    
    # Re-scan so that print is in baseline DB
    db = SymbolDB(db_path)
    scan(temp_project, db)
    db.close()
    
    # Run check-vuln with regressive patch
    _main_check_vuln(["--project", str(temp_project), "--patch", str(patch_regress_file)])
    
    # Verify proof was NOT created because it's a regression
    assert not proof_regress_path.exists()
    
    conn.close()

# Test LSP JSON-RPC message processing
def test_lsp_server_messages(temp_project):
    # Setup test file in project
    test_file = temp_project / "app.py"
    test_file.write_text("def index():\n    pass\n", encoding="utf-8")
    
    # Initialize DB
    db_path = temp_project / ".lore_poc.db"
    db = SymbolDB(db_path)
    scan(temp_project, db)
    db.close()
    
    # Create mock inputs representing RPC initialize call
    init_request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "rootUri": temp_project.as_uri(),
            "capabilities": {}
        }
    }
    
    did_save_notification = {
        "jsonrpc": "2.0",
        "method": "textDocument/didSave",
        "params": {
            "textDocument": {
                "uri": test_file.as_uri()
            }
        }
    }
    
    hover_request = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "textDocument/hover",
        "params": {
            "textDocument": {
                "uri": test_file.as_uri()
            },
            "position": {
                "line": 0,
                "character": 4
            }
        }
    }
    
    shutdown_request = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "shutdown"
    }
    
    exit_notification = {
        "jsonrpc": "2.0",
        "method": "exit"
    }
    
    # Encode messages into LSP stdio stream format
    def format_lsp_msg(msg_dict):
        body = json.dumps(msg_dict).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        return header + body
        
    input_data = (
        format_lsp_msg(init_request) +
        format_lsp_msg(did_save_notification) +
        format_lsp_msg(hover_request) +
        format_lsp_msg(shutdown_request) +
        format_lsp_msg(exit_notification)
    )
    
    mock_stdin = io.BytesIO(input_data)
    mock_stdout = io.BytesIO()
    
    # Backup real stdin/stdout
    real_stdin = sys.stdin
    real_stdout = sys.stdout
    
    # Mock systems stdio buffers
    class MockBuffer:
        def __init__(self, stream):
            self.stream = stream
        def read(self, *args, **kwargs):
            return self.stream.read(*args, **kwargs)
        def write(self, *args, **kwargs):
            return self.stream.write(*args, **kwargs)
        def flush(self):
            self.stream.flush()
            
    class MockStdio:
        def __init__(self, stream, real_stream):
            self.stream = stream
            self.real_stream = real_stream
            self.buffer = MockBuffer(stream)
        def write(self, data):
            if isinstance(data, str):
                self.real_stream.write(data)
            else:
                self.stream.write(data)
        def read(self, *args, **kwargs):
            res = self.stream.read(*args, **kwargs)
            if isinstance(res, bytes):
                return res.decode("utf-8")
            return res
        def readline(self, *args, **kwargs):
            res = self.stream.readline(*args, **kwargs)
            if isinstance(res, bytes):
                return res.decode("utf-8")
            return res
        def __getattr__(self, name):
            try:
                return getattr(self.stream, name)
            except AttributeError:
                return getattr(self.real_stream, name)
                
    sys.stdin = MockStdio(mock_stdin, real_stdin)
    sys.stdout = MockStdio(mock_stdout, real_stdout)
    
    try:
        with pytest.raises(SystemExit) as excinfo:
            _main_lsp()
        assert excinfo.value.code == 0
    finally:
        # Restore real stdin/stdout
        sys.stdin = real_stdin
        sys.stdout = real_stdout
        
    # Parse mock stdout outputs
    mock_stdout.seek(0)
    output_bytes = mock_stdout.read()
    
    # Extract JSON RPC payloads
    responses = []
    idx = 0
    while idx < len(output_bytes):
        cl_idx = output_bytes.find(b"Content-Length:", idx)
        if cl_idx == -1:
            break
        delim_idx = output_bytes.find(b"\r\n\r\n", cl_idx)
        if delim_idx == -1:
            break
        header = output_bytes[cl_idx:delim_idx].decode("ascii")
        content_len = int(header.split(":")[1].strip())
        start_body = delim_idx + 4
        body = output_bytes[start_body:start_body + content_len].decode("utf-8")
        responses.append(json.loads(body))
        idx = start_body + content_len
        
    # Validate responses
    assert len(responses) >= 3
    
    # 1. Initialize Response
    assert responses[0]["id"] == 1
    assert "capabilities" in responses[0]["result"]
    assert responses[0]["result"]["capabilities"]["hoverProvider"] is True
    
    # 2. Publish Diagnostics Notification
    # (Might be empty list of diagnostics for app.py, but textDocument/publishDiagnostics method must be called)
    did_save_diag = [r for r in responses if r.get("method") == "textDocument/publishDiagnostics"]
    assert len(did_save_diag) > 0
    assert did_save_diag[0]["params"]["uri"] == test_file.as_uri()
    
    # 3. Hover Response
    hover_resp = [r for r in responses if r.get("id") == 2]
    assert len(hover_resp) == 1
    # Hover is None since we didn't document app.py with ADRs
    assert hover_resp[0]["result"] is None
    
    # 4. Shutdown Response
    shutdown_resp = [r for r in responses if r.get("id") == 3]
    assert len(shutdown_resp) == 1
    assert shutdown_resp[0]["result"] is None
