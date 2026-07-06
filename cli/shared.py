#!/usr/bin/env python3
"""
lore.py — Fog-of-War Agent

Agente minimalista che usa solo FOW navigation per modificare codice.
Niente planner complesso, niente convergent loop.

Flusso:
  1. Claude riceve il task
  2. Claude chiama FOW tools (search, frontier, expand) per esplorare il codebase
  3. Claude scrive i file modificati via write_staged_file
  4. Il sistema genera diff + log

Usage:
    export ANTHROPIC_API_KEY="sk-ant-..."
    python lore.py "aggiungi logging alla funzione authenticate"
    python lore.py "task" --project /path/to/project
    python lore.py "task" --project /path/to/project --rescan
"""
from __future__ import annotations

import os
# Limit CPU threads to 1 for PyTorch and linear algebra libraries to prevent thrashing
os.environ["OMP_NUM_THREADS"] = "1"
os.environ["MKL_NUM_THREADS"] = "1"
os.environ["OPENBLAS_NUM_THREADS"] = "1"
os.environ["VECLIB_MAXIMUM_THREADS"] = "1"
os.environ["NUMEXPR_NUM_THREADS"] = "1"

# Suppress noisy HuggingFace / transformers warnings before any imports
os.environ.setdefault("TRANSFORMERS_VERBOSITY", "error")
os.environ.setdefault("HF_HUB_DISABLE_IMPLICIT_TOKEN", "1")
os.environ.setdefault("HF_HUB_VERBOSITY", "error")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

# Force offline mode for Hugging Face and transformers to bypass network checks
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

import argparse
import difflib
import heapq
import json
import re
import struct
import sys
try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass
import textwrap
from datetime import datetime
from io import StringIO
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.markdown import Markdown
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.text import Text
from rich.theme import Theme

# Setup premium console theme
_THEME = Theme({
    "info": "cyan",
    "warning": "yellow",
    "error": "bold red",
    "success": "bold green",
    "phase": "bold magenta",
    "step": "blue",
    "accent": "bold cyan",
})
console = Console(theme=_THEME)

def _print_banner() -> None:
    banner_text = textwrap.dedent("""
     __    ____  ____  _____ 
    / /   / __ \\/ __ \\/ ___/ 
    / /   / / / / /_/ / __/  
    / /___/ /_/ / _, _/ /___ 
    /_____/\\____/_/ |_/_____/ 
    """)
    console.print(Panel(
        Text(banner_text.strip(), style="bold magenta"),
        subtitle="[bold white]Autonomous KG & Vulnerability Intelligence[/]",
        subtitle_align="center",
        border_style="magenta",
        padding=(0, 4),
        expand=False
    ))

# Importa il layer FOW dallo script di navigazione
sys.path.insert(0, str(Path(__file__).parent))
from core.symbol_map import SymbolDB, SymbolRetriever, scan as fow_scan, embed_all_symbols

# ---------------------------------------------------------------------------
# Costanti
# ---------------------------------------------------------------------------

DEFAULT_PROJECT = os.environ.get("LORE_PROJECT", str(Path.cwd()))
MODEL           = "default"  # resolved by LoreLLMClient from lore.config.json
MAX_TOOL_CALLS  = 30      # limite massimo iterazioni tool use
STAGE_SUBDIR    = ".lore/stage"

def _get_db_path(project_root: Path) -> Path:
    db_path = project_root / ".lore_poc.db"
    if not db_path.exists() and (project_root / ".lore" / "lore.db").exists():
        selected_db = project_root / ".lore" / "lore.db"
    elif not db_path.exists() and (project_root / ".ase_poc.db").exists():
        selected_db = project_root / ".ase_poc.db"
    else:
        selected_db = db_path

    # Run inline migrations to avoid missing column/view errors in existing databases
    if selected_db.exists():
        try:
            import sqlite3
            conn = sqlite3.connect(str(selected_db))
            try:
                try:
                    conn.execute("ALTER TABLE symbols ADD COLUMN embedding BLOB")
                except Exception:
                    pass
                try:
                    conn.execute("ALTER TABLE symbols ADD COLUMN is_source INTEGER DEFAULT 0")
                except Exception:
                    pass
                try:
                    conn.execute("""
                        CREATE VIEW IF NOT EXISTS symbol_calls AS
                        SELECT 
                            d.from_symbol_id AS caller_symbol_id,
                            s.id AS callee_symbol_id,
                            d.to_name AS callee_name,
                            d.line AS call_line
                        FROM deps d
                        LEFT JOIN symbols s ON d.to_name = s.name
                        WHERE d.dep_type = 'call';
                    """)
                except Exception:
                    pass
                conn.commit()
            finally:
                try:
                    conn.close()
                except Exception:
                    pass

        except Exception:
            pass

    return selected_db

