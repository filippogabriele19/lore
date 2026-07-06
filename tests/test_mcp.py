import pytest
import sqlite3
from pathlib import Path
from cli.init import _main_init
from cli.mcp_server import (
    lore_trace_taint,
    lore_get_symbol_context,
    lore_audit_changes,
    lore_get_compliance_adrs,
    lore_get_architecture_constraints,
    lore_query_knowledge_graph
)

def test_mcp_tools_with_temp_project(temp_project):
    # Inizializza il progetto temporaneo per popolare il database
    _main_init(["--project", str(temp_project)])
    
    # 1. Test lore_trace_taint (dovrebbe trovare il percorso: view.py -> app.py)
    res_taint = lore_trace_taint(str(temp_project))
    assert "view.py" in res_taint
    assert "app.py" in res_taint
    
    # Test lore_trace_taint filtrando per file specifico
    res_filter = lore_trace_taint(str(temp_project), file_path="view.py")
    assert "view.py" in res_filter
    
    # 2. Test lore_get_symbol_context (recupero informazioni per 'add_user')
    res_context = lore_get_symbol_context(str(temp_project), "add_user")
    assert "add_user" in res_context
    assert "def add_user(username):" in res_context
    
    # 3. Test lore_get_compliance_adrs (inizialmente vuoto)
    res_adrs = lore_get_compliance_adrs(str(temp_project))
    assert "No architectural decisions" in res_adrs
    
    # Aggiungi un ADR manualmente per testare il recupero di compliance
    from cli.shared import _get_db_path
    db_path = _get_db_path(temp_project)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO decision_links (symbol_name, source_type, source_ref, confidence, description) "
            "VALUES ('add_user', 'manual', 'ADR-001', 0.95, 'Safe command printing')"
        )
        conn.commit()
    finally:
        conn.close()

        
    res_adrs_after = lore_get_compliance_adrs(str(temp_project))
    assert "Safe command printing" in res_adrs_after
    
    # 4. Test lore_get_architecture_constraints per il file app.py
    res_constraints = lore_get_architecture_constraints(str(temp_project), "app.py")
    assert "Safe command printing" in res_constraints
    assert "ADR-001" in res_constraints
    
    # 5. Test lore_query_knowledge_graph
    res_kg_query = lore_query_knowledge_graph(str(temp_project), "add_user")
    assert "add_user" in res_kg_query
    assert "Safe command printing" in res_kg_query

    # 6. Test lore_audit_changes con patch correttiva (sostituzione os.system con print)
    patch_diff = """--- app.py
+++ app.py
@@ -3,2 +3,2 @@
 def add_user(username):
-    os.system("echo " + username)
+    print(username)
"""
    res_audit = lore_audit_changes(str(temp_project), patch_diff)
    assert "Remaining Taint Paths: 0" in res_audit
    assert "[SUCCESS]" in res_audit
