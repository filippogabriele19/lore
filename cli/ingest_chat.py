import sys
import argparse
from pathlib import Path
from cli.shared import console, DEFAULT_PROJECT, _get_db_path
from core.chat_miner import mine_chat_intent

def _main_ingest_chat(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="lore ingest-chat",
        description="Ingest Claude Code chat history to dynamically extract architectural design rules",
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
        console.print(f"[error]✖ Database not found under {project_root}[/]")
        sys.exit(1)

    console.print(f"[info]🔮 Analyzing Claude Code chat logs for project: {project_root}...[/]")
    rules_count = mine_chat_intent(db_path, project_root)
    if rules_count > 0:
        console.print(f"[success]✔ Successfully extracted {rules_count} design rules from chat intent.[/]")
    else:
        console.print("[info]No new design rules or chat sessions found to ingest.[/]")
