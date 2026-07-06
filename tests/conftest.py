import pytest
import tempfile
import os
from pathlib import Path
import sys

# Aggiungi il root del progetto al path per gli import
sys.path.insert(0, str(Path(__file__).parent.parent))

@pytest.fixture
def temp_project():
    """Crea una directory di progetto temporanea con alcuni file di prova."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        
        # Crea file Python di prova
        file_a = tmp_path / "app.py"
        file_a.write_text('''
def add_user(username):
    # Simula un sink insicuro
    import os
    os.system("echo " + username)
''', encoding="utf-8")
        
        file_b = tmp_path / "view.py"
        file_b.write_text('''
from app import add_user
def get_user_request(request):
    val = request.GET['user']
    add_user(val)
''', encoding="utf-8")

        # Crea un file TypeScript di prova
        file_c = tmp_path / "service.ts"
        file_c.write_text('''
import { Injectable } from '@nestjs/common';
@Injectable()
export class UserService {
    async deleteUser(id: string) {
        this.auditLog(id);
        return id;
    }
}
''', encoding="utf-8")

        yield tmp_path

@pytest.fixture
def clean_db(temp_project):
    """Inizializza un database temporaneo di SymbolDB."""
    from core.symbol_map import SymbolDB
    db_path = temp_project / ".lore_poc.db"
    db = SymbolDB(db_path)
    yield db
    db.close()
