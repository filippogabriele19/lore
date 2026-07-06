import sys
import argparse
from pathlib import Path

from cli.shared import DEFAULT_PROJECT, _get_db_path
from cli.query import _build_index
from cli.diff_server import _serve_audit_ui

def _main_audit(argv: list[str] | None = None) -> None:
    """audit mode — autonomous full-project analysis, no question needed."""
    parser = argparse.ArgumentParser(
        prog="lore audit",
        description="Run a full autonomous audit of the project Knowledge Graph",
    )
    parser.add_argument("--project", default=DEFAULT_PROJECT,
                        help=f"Path to project root (default: {DEFAULT_PROJECT})")
    parser.add_argument("--rescan", action="store_true",
                        help="Force re-scan before auditing")
    args = parser.parse_args(argv)

    project_root = Path(args.project)
    if not project_root.exists():
        print(f"[error] Project path not found: {project_root}")
        sys.exit(1)

    db_path = _get_db_path(project_root)

    if args.rescan or not db_path.exists():
        _build_index(project_root, db_path, rescan=True)

    _serve_audit_ui(project_root, db_path)
