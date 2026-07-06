import pytest
import sys
from pathlib import Path
from lore import _main_check_vuln, _main_adr
from core.symbol_map import SymbolDB, scan

def test_check_vuln_command(temp_project):
    # Prima crea il database scansionando il progetto
    db_path = temp_project / ".lore_poc.db"
    db = SymbolDB(db_path)
    scan(temp_project, db)
    db.close()
    
    # Esegui check-vuln
    # Dovrebbe avviarsi e terminare correttamente senza sollevare eccezioni impreviste
    _main_check_vuln(["--project", str(temp_project)])

    
    # check-vuln non dovrebbe chiamare sys.exit(1) ma terminare normalmente (in questo caso solleva SystemExit da argparse o simili se non gestito,
    # ma qui ci aspettiamo che termini con SystemExit(0) o semplicemente non esca se argparse non viene interrotto).
    # Se il codice esegue con successo, non lancia SystemExit tranne che in caso di argomenti invalidi o alla fine.
    # In _main_check_vuln non c'è sys.exit alla fine, esce normalmente se non fallisce.
    # Quindi pytest.raises(SystemExit) potrebbe NON catturare nulla se esce normalmente.
    # Facciamo una chiamata normale e catturiamo SystemExit solo se lanciato.

def test_adr_command(temp_project):
    # Inizializza il database
    db_path = temp_project / ".lore_poc.db"
    db = SymbolDB(db_path)
    scan(temp_project, db)
    db.close()

    # Esegui il comando adr per curare l'amnesia
    # adr termina con successo stampando a console
    _main_adr(["--project", str(temp_project), "--file", "app.py", "--title", "Security Invariant for user management"])
    
    # Verifica che il file ADR sia stato creato
    adr_dir = temp_project / ".lore" / "adr"
    assert adr_dir.exists()
    files = list(adr_dir.glob("*.md"))
    assert len(files) == 1

def test_watch_command(temp_project):
    db_path = temp_project / ".lore_poc.db"
    db = SymbolDB(db_path)
    scan(temp_project, db)
    db.close()
    
    from core.scanner.file_watcher import FileWatcher
    watcher = FileWatcher(temp_project, db_path)
    
    # Crea un nuovo file di test per simulare una modifica
    new_file = temp_project / "new_module.py"
    new_file.write_text("def new_feature():\n    pass", encoding="utf-8")
    
    # Esegui watcher.start(once=True) per consumare la modifica e reindicizzare
    watcher.start(once=True)
    
    # Verifica che il nuovo simbolo sia nel database
    db = SymbolDB(db_path)
    rows = db.con.execute("SELECT * FROM symbols WHERE name='new_feature'").fetchall()
    db.close()
    
    assert len(rows) == 1

def test_auto_cure_command(temp_project):
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

    from unittest.mock import MagicMock, patch
    import os
    import lore
    
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
            # Run check-vuln via auto-cure command
            with patch("sys.argv", ["lore.py", "auto-cure", "--project", str(temp_project)]):
                lore.main()
        finally:
            if old_key is not None:
                os.environ["ANTHROPIC_API_KEY"] = old_key
            else:
                if "ANTHROPIC_API_KEY" in os.environ:
                    del os.environ["ANTHROPIC_API_KEY"]
                
    # Verify patch was applied to app.py
    app_code = (temp_project / "app.py").read_text(encoding="utf-8")
    assert "print(\"echo \" + username)" in app_code
    assert "os.system" not in app_code

    # Verify proof-carrying certificate was generated
    proof_file = temp_project / ".lore" / "auto_cure.patch.proof"
    assert proof_file.exists()


