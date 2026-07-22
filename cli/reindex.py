"""
cli/reindex.py — Fast History Re-Indexing Engine

Re-computes symbol fragility scores, symbol co-change matrices, virtual edges,
and decision links across an existing Knowledge Graph database.
"""

import sys
import argparse
import sqlite3
from datetime import datetime
from pathlib import Path
from cli.shared import console, DEFAULT_PROJECT, _get_db_path
from core.symbol_db import SymbolDB
from core.git_miner import GitMiner
from core.decision_linker import DecisionLinker

def _main_reindex(argv: list[str] | None = None) -> None:
    """reindex mode — fast history re-indexing engine for fragility & co-changes."""
    parser = argparse.ArgumentParser(
        prog="lore reindex",
        description="Fast history re-indexing engine for symbol fragility & co-changes",
    )
    parser.add_argument("--project", default=DEFAULT_PROJECT,
                        help=f"Path to project root (default: {DEFAULT_PROJECT})")
    args = parser.parse_args(argv)

    project_root = Path(args.project).resolve()
    if not project_root.exists():
        console.print(f"[error]Project path not found: {project_root}[/]")
        sys.exit(1)

    db_path = _get_db_path(project_root)
    if not db_path.exists():
        console.print(f"[error]Database not found under {project_root}. Run 'lore init' first.[/]")
        sys.exit(1)

    console.print(f"[info]🔄 Running LORE Fast History Re-Indexing Engine on {project_root}...[/]")

    # 1. Run migrations
    db_obj = SymbolDB(db_path)
    db_obj.close()

    # 2. Mine Git history (fragility, co-changes, virtual edges, hotspots)
    miner = GitMiner(str(project_root), str(db_path))
    git_res = miner.run()

    # 3. Build decision links
    linker = DecisionLinker(str(db_path))
    link_count = linker.build_links(str(project_root))

    # 4. Update metadata
    conn = sqlite3.connect(str(db_path))
    now_iso = datetime.now().isoformat()
    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('last_reindex_at', ?)", (now_iso,))
    conn.commit()

    fragile_count = conn.execute("SELECT COUNT(*) FROM symbols WHERE fragility_score >= 2").fetchone()[0]
    conn.close()

    console.print(f"[success]✔ Re-indexing complete![/]")
    console.print(f"  • Commits Processed: [bold white]{git_res.get('commits_processed', 0):,}[/]")
    console.print(f"  • Decision Links Built: [bold white]{link_count:,}[/]")
    console.print(f"  • Hotspots Mined: [bold white]{git_res.get('hotspots', 0):,}[/]")
    console.print(f"  • Historically Fragile Symbols: [bold yellow]{fragile_count:,}[/]")
