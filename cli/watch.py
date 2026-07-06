import argparse
import sys
from pathlib import Path
from cli.shared import console, DEFAULT_PROJECT, _get_db_path
from core.scanner.file_watcher import FileWatcher

def _main_watch(argv: list[str] | None = None) -> None:
    """watch mode — watch filesystem and index incrementally on changes."""
    parser = argparse.ArgumentParser(
        prog="lore watch",
        description="Monitor the project filesystem and incrementally update the Knowledge Graph",
    )
    parser.add_argument("--project", default=DEFAULT_PROJECT,
                        help=f"Path to the project root (default: {DEFAULT_PROJECT})")
    parser.add_argument("--interval", type=float, default=1.0,
                        help="Polling interval in seconds (default: 1.0)")
    args = parser.parse_args(argv)

    project_root = Path(args.project)
    if not project_root.exists():
        console.print(f"[error]Project path not found: {project_root}[/]")
        sys.exit(1)

    db_path = _get_db_path(project_root)
    
    watcher = FileWatcher(project_root, db_path, interval=args.interval)
    watcher.start()
