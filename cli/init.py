import sys
import json
import argparse
from pathlib import Path
from cli.shared import console, _print_banner, _get_db_path, Panel
from cli.cache import restore_or_create_db

def _main_init(argv: list[str] | None = None) -> None:
    """Initialize a LORE workspace in the target project directory."""
    parser = argparse.ArgumentParser(
        prog="lore init",
        description="Bootstrap a LORE metadata workspace, index project files, and set up the local Knowledge Graph database."
    )
    parser.add_argument("--project", default=".",
                        help="Path to the project root (default: .)")
    parser.add_argument("--project-name", default=None,
                        help="Project name (default: folder name)")
    args = parser.parse_args(argv)

    project_root = Path(args.project).resolve()
    if not project_root.exists():
        console.print(f"[error]Project path not found: {project_root}[/]")
        sys.exit(1)

    lore_dir = project_root / ".lore"
    lore_dir.mkdir(parents=True, exist_ok=True)

    config_path = lore_dir / "lore.config.json"
    p_name = args.project_name or project_root.name

    config_data = {}
    if not config_path.exists():
        config_data = {
            "project_name": p_name,
            "db_path": ".lore_poc.db",
            "languages": ["python", "go", "typescript"]
        }
        import sys
        if sys.stdin.isatty():
            from core.llm_client import setup_llm_interactively
            config_data = setup_llm_interactively(project_root, config_data)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config_data, f, indent=4)
        try:
            rel_config = config_path.relative_to(project_root)
        except ValueError:
            rel_config = config_path
        console.print(f"[info]Created configuration file:[/] [bold cyan]{rel_config}[/]")
    else:
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config_data = json.load(f)
            p_name = config_data.get("project_name", p_name)
        except Exception:
            pass
        
        if "llm" not in config_data:
            import sys
            if sys.stdin.isatty():
                from core.llm_client import setup_llm_interactively
                config_data = setup_llm_interactively(project_root, config_data)
                with open(config_path, "w", encoding="utf-8") as f:
                    json.dump(config_data, f, indent=4)

    db_path = _get_db_path(project_root)
    db_exists = db_path.exists()

    _print_banner()

    try:
        rel_db = db_path.relative_to(project_root)
    except ValueError:
        rel_db = db_path

    console.print(f"\n[phase]Initializing LORE Workspace[/]")
    console.print(f"[step]Project:[/] [bold white]{p_name}[/]")
    console.print(f"[step]Directory:[/] [dim]{project_root}[/]")
    console.print(f"[step]Database:[/] [dim]{rel_db}[/]\n")

    db = restore_or_create_db(project_root, db_path)
    db.close()

    console.print(Panel(
        f"[bold green]LORE Workspace Initialized Successfully![/]\n\n"
        f"Use [bold white]lore query[/] to query the architecture or [bold white]lore check-vuln[/] to analyze taint paths/hotspots.",
        border_style="green",
        expand=False
    ))
