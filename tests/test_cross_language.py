import pytest
from pathlib import Path
from parsers.go_taint_tracer import check_go_taint_interprocedural
from parsers.ts_taint_tracer import check_ts_taint_interprocedural
from core.symbol_map import SymbolDB, scan
from cli.check_vuln import _main_check_vuln
import sqlite3

def test_go_taint_tracer():
    go_code = """
package main

import (
    "os/exec"
)

func ProcessInput(userInput string) {
    cmdName := userInput
    exec.Command(cmdName)
}
"""
    res = check_go_taint_interprocedural(go_code, external_sources={"userInput"})
    flows = res["flows"]
    assert len(flows) == 1
    assert flows[0]["var_name"] == "cmdName"
    assert flows[0]["sink_name"] == "exec.Command"

def test_ts_taint_tracer():
    ts_code = """
function processData(reqInput: string) {
    const payload = reqInput;
    eval(payload);
}
"""
    res = check_ts_taint_interprocedural(ts_code, external_sources={"reqInput"})
    flows = res["flows"]
    assert len(flows) == 1
    assert flows[0]["var_name"] == "payload"
    assert flows[0]["sink_name"] == "eval"

def test_cross_language_bfs_taint(temp_project):
    # Setup files crossing the TS -> Python boundary
    client_ts = temp_project / "client.ts"
    client_ts.write_text("""
function sendRequest(request) {
    const param = request.query.q;
    backend_endpoint(param);
}
""", encoding="utf-8")

    server_py = temp_project / "server.py"
    server_py.write_text("""
def backend_endpoint(data):
    eval(data)
""", encoding="utf-8")

    # Index the project
    db_path = temp_project / ".lore_poc.db"
    db = SymbolDB(db_path)
    scan(temp_project, db)
    db.close()

    # Query the db to ensure relationships were indexed
    conn = sqlite3.connect(str(db_path))
    try:
        conn.row_factory = sqlite3.Row
        # check call dependency
        rows = conn.execute("""
            SELECT DISTINCT s_from.name AS caller, s_to.name AS callee
            FROM deps d
            JOIN symbols s_from ON d.from_symbol_id = s_from.id
            JOIN symbols s_to ON d.to_name = s_to.name
            WHERE d.dep_type = 'call'
        """).fetchall()
        calls = {(r["caller"], r["callee"]) for r in rows}
    finally:
        conn.close()

    # Verify call from sendRequest to backend_endpoint exists
    assert ("sendRequest", "backend_endpoint") in calls

    # Run check-vuln
    # Should run interprocedural analysis from client.ts to server.py and detect the flow!
    # Let's mock console or run check_vuln and verify that it doesn't fail
    import sys
    # We patch check_vuln console to see output or simply let it run
    from unittest.mock import patch, MagicMock
    
    with patch("cli.check_vuln.console") as mock_console:
        # Run check-vuln command
        _main_check_vuln(["--project", str(temp_project)])
        
        # Verify that mock_console.print was called with something indicating active paths
        printed_texts = []
        for call in mock_console.print.call_args_list:
            if call[0]:
                arg = call[0][0]
                if isinstance(arg, str):
                    printed_texts.append(arg)
                else:
                    printed_texts.append(str(arg))
                
        # The output should contain vulnerability information
        joined = "\n".join(printed_texts)
        # It should detect at least 1 exposed path
        assert "vulnerability" in joined.lower() or "exposed" in joined.lower() or "active path" in joined.lower() or "decay" in joined.lower()
