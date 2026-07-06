import json
import pytest
from pathlib import Path
from cli.init import _main_init

def test_lore_init_creates_folders_and_config(temp_project):
    # Assicurati che la directory .lore e il config non esistano prima dell'init
    lore_dir = temp_project / ".lore"
    config_path = lore_dir / "lore.config.json"
    db_path = temp_project / ".lore_poc.db"
    
    assert not lore_dir.exists()
    assert not config_path.exists()
    assert not db_path.exists()
    
    # Esegui il comando init puntando a temp_project
    _main_init(["--project", str(temp_project), "--project-name", "TestApp"])
    
    # Verifica che directory, config e DB vengano creati
    assert lore_dir.exists()
    assert config_path.exists()
    assert db_path.exists()
    
    # Verifica il contenuto del file di configurazione
    with open(config_path, "r", encoding="utf-8") as f:
        config_data = json.load(f)
        
    assert config_data["project_name"] == "TestApp"
    assert config_data["db_path"] == ".lore_poc.db"
    assert "python" in config_data["languages"]
