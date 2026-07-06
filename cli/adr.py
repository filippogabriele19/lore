import os, sys, argparse, json
from pathlib import Path
from cli.shared import console, DEFAULT_PROJECT, _get_db_path

def _main_adr(argv: list[str]) -> None:
    """adr mode — generate a draft ADR to cure institutional amnesia and index it in the KG."""
    import argparse
    parser = argparse.ArgumentParser(
        prog="lore adr",
        description="Generate and index an Architectural Decision Record (ADR) to cure Amnesia",
    )
    parser.add_argument("--project", default=DEFAULT_PROJECT,
                        help=f"Path to project root (default: {DEFAULT_PROJECT})")
    parser.add_argument("--file", required=True,
                        help="Relative file path associated with the decision")
    parser.add_argument("--title", required=True,
                        help="Short title/description of the architectural invariant")
    parser.add_argument("--symbol", default=None,
                        help="Optional symbol name (class/function) to bind the decision to")
    args = parser.parse_args(argv)

    project_root = Path(args.project)
    if not project_root.exists():
        console.print(f"[error]Project path not found: {project_root}[/]")
        sys.exit(1)

    db_path = _get_db_path(project_root)
    if not db_path.exists():
        console.print(f"[error]✖ Database not found under {project_root}[/]")
        sys.exit(1)

    # 1. Write the markdown ADR file
    adr_dir = project_root / ".lore" / "adr"
    adr_dir.mkdir(parents=True, exist_ok=True)
    
    # Generate unique ID/filename
    from datetime import datetime as _dt
    import random
    ts = _dt.now().strftime("%Y%m%d_%H%M%S")
    rand_id = random.randint(1000, 9999)
    adr_filename = f"adr_{ts}_{rand_id}.md"
    adr_path = adr_dir / adr_filename

    adr_content = f"""# ADR: {args.title}

## Metadata
- **Date**: {_dt.now().strftime("%Y-%m-%d")}
- **Target File**: {args.file}
- **Target Symbol**: {args.symbol or "File-level Invariant"}
- **Status**: Accepted

## Context
Describe the context and the problem we are solving, including any security requirements or invariants (e.g. inputs must be validated, credentials must come from environment).

## Decision
Describe the decision and the design rules. What must developers do or avoid doing in this subsystem?

## Consequences
What are the consequences of this decision? What are the tradeoffs?
"""
    adr_path.write_text(adr_content, encoding="utf-8")
    console.print(f"[success]✔ ADR file generated successfully:[/] [underline info]{adr_path}[/]")

    # 2. Register it in decision_links table in the database
    from core.symbol_db import SymbolDB
    db = SymbolDB(db_path)
    
    try:
        # Try to find symbol ID if symbol is specified
        symbol_name = args.symbol if args.symbol else args.file.replace("\\", "/").split("/")[-1]
        
        db.register_decision_link(symbol_name, "adr", f".lore/adr/{adr_filename}", 1.0, args.title)
        db.commit()
    finally:
        db.close()

    
    console.print(f"[success]✔ Decision Link registered in the Knowledge Graph for symbol [bold cyan]{symbol_name}[/]. Amnesia cured![/]\n")


