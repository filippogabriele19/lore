import argparse
import sys
from pathlib import Path
from cli.shared import console, DEFAULT_PROJECT, _get_db_path
from core.symbol_db import SymbolDB


def _main_dismiss(argv: list[str] | None = None) -> None:
    """CLI handler for dismissing false positive LORE warnings."""
    parser = argparse.ArgumentParser(
        prog="lore dismiss",
        description="Dismiss a LORE warning/finding for a specific file or symbol",
    )
    parser.add_argument("--type", default="all", help="Type of finding to dismiss (e.g. invariant, sibling, amnesia, co_change)")
    parser.add_argument("--file", required=True, help="Relative file path")
    parser.add_argument("--symbol", default="", help="Symbol name (optional)")
    parser.add_argument("--reason", default="False positive", help="Reason for dismissal")
    parser.add_argument("--project", default=DEFAULT_PROJECT, help=f"Path to LORE project root (default: {DEFAULT_PROJECT})")
    args = parser.parse_args(argv)

    project_root = Path(args.project).resolve()
    db_path = _get_db_path(project_root)
    if not db_path.exists():
        console.print(f"[error]✖ LORE Database not found under {project_root}. Run 'lore init' first.[/]")
        sys.exit(1)

    db = SymbolDB(db_path)
    try:
        db.dismiss_finding(args.type, args.file, args.symbol, args.reason)
        console.print(f"[success]✔ Successfully dismissed finding for file '{args.file}' (symbol: '{args.symbol or 'all'}')[/]")
    finally:
        db.close()
