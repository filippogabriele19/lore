import os
import sys
import json
import argparse
from pathlib import Path
from datetime import datetime
from rich.table import Table
from rich.panel import Panel
from rich.text import Text

from cli.shared import console, DEFAULT_PROJECT, _get_db_path
from cli.agent_runner import run_agent
from cli.diff_server import _serve_diff_ui, _serve_impact_ui
# Import helper functions from query to build the database index
from cli.query import _build_index, _is_analysis_query

def _main_apply(argv: list[str] | None = None) -> None:
    """apply mode — modify code (original behaviour)."""
    parser = argparse.ArgumentParser(
        prog="lore apply",
        description="Apply a code modification task",
    )
    parser.add_argument("task", help="Natural language task to execute")
    parser.add_argument("--project", default=DEFAULT_PROJECT,
                        help=f"Path to project root (default: {DEFAULT_PROJECT})")
    parser.add_argument("--rescan", action="store_true",
                        help="Force re-scan of the project (rebuild FOW index)")
    parser.add_argument("--demo", action="store_true",
                        help="Open diff viewer in browser after completion")
    parser.add_argument("--headless", action="store_true",
                        help="Headless mode: skip all interactive UI, auto-apply staged files, exit 0/1. For CI/CD and benchmark use.")
    args = parser.parse_args(argv)

    project_root = Path(args.project).resolve()
    if not project_root.exists():
        print(f"[error] Project path not found: {project_root}")
        sys.exit(1)

    # Load provider to check correct API keys
    provider = "anthropic"
    config_path = project_root / ".lore" / "lore.config.json"
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config_data = json.load(f)
                provider = config_data.get("llm", {}).get("provider", "anthropic")
        except Exception:
            pass

    # Load .env if present (for keys not in system env)
    from core.llm_client import load_local_env
    load_local_env(project_root)

    if provider == "anthropic":
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("[error] ANTHROPIC_API_KEY not set")
            sys.exit(1)
    elif provider in ("openai", "openrouter", "deepseek"):
        if not os.environ.get("OPENAI_API_KEY") and not os.environ.get("DEEPSEEK_API_KEY"):
            print("[error] API key not set (OPENAI_API_KEY or DEEPSEEK_API_KEY required)")
            sys.exit(1)


    db_path = _get_db_path(project_root)

    # Auto-ingest chat intent on the fly
    try:
        from core.chat_miner import mine_chat_intent
        mine_chat_intent(db_path, project_root)
    except Exception:
        pass

    db, retriever = _build_index(project_root, db_path, args.rescan)

    # In headless mode: skip impact UI entirely, force apply path
    if _is_analysis_query(args.task) and not os.environ.get("LORE_FORCE_APPLY"):
        if args.headless:
            # Headless: non aprire la UI, forza comunque l'apply
            os.environ["LORE_FORCE_APPLY"] = "1"
        else:
            db.close()
            _serve_impact_ui(args.task, project_root, db_path)
            return

    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = project_root / ".lore" / "fow_logs" / f"fow_{ts}.log"
    result   = run_agent(args.task, project_root, retriever, db, log_path)

    s = result["stats"]

    if args.headless:
        # Headless mode: output minimo, auto-applica i file staged, exit pulito
        staged = result.get("staged_files", [])
        if staged:
            import shutil
            from cli.shared import STAGE_SUBDIR
            stage_dir = project_root / STAGE_SUBDIR
            applied = 0
            for f in staged:
                src = Path(f["staged"]) if "staged" in f else None
                if src and src.exists():
                    dst = project_root / f["path"]
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    applied += 1
            if stage_dir.exists():
                shutil.rmtree(stage_dir)
            print(f"[headless] Applied {applied}/{len(staged)} staged files.")
        else:
            print("[headless] No staged files produced.")
        
        print(f"[headless] Diff: {result.get('diff_path', 'N/A')}")
        print(f"[headless] Log:  {result.get('log_path', 'N/A')}")
        db.close()
        sys.exit(0 if staged else 1)

    # --- UI normale (invariata) ---
    
    # 1. Execution Summary Panel
    stats_table = Table.grid(padding=(0, 2))
    stats_table.add_column("Stat", style="bold cyan")
    stats_table.add_column("Value", style="bold white", justify="right")
    stats_table.add_row("Tool Calls Total", f"{s['tool_calls']}")
    stats_table.add_row("FOW Navigations", f"{s['fow_calls']}")
    stats_table.add_row("Files Staged", f"{s['files_staged']}")
    
    # 2. Token / API Report Table
    token_table = Table.grid(padding=(0, 2))
    token_table.add_column("Metric", style="bold yellow")
    token_table.add_column("Value", style="bold white", justify="right")
    token_table.add_row("API Calls Total", f"{s['api_calls']:,}")
    token_table.add_row("Input Tokens Used (Actual)", f"{s['api_input_tokens']:,}")
    token_table.add_row("Output Tokens Generated", f"{s['api_output_tokens']:,}")
    est_files_count = len(s.get('files_accessed_by_fow', []))
    token_table.add_row(
        "Full-file Estimate", 
        f"{s['full_file_tokens_estimate']:,} ({est_files_count} files read entirely)"
    )
    saving_style = "bold green" if s['context_saving_pct'] >= 50 else "bold yellow"
    token_table.add_row(
        "Context Saving", 
        f"[{saving_style}]{s['context_saving_pct']}%[/]"
    )

    # 3. Staged Files Table
    staged_table = Table(title="Staged Files", show_header=True, header_style="bold green")
    staged_table.add_column("File Path", style="bold green")
    staged_table.add_column("Modification Reason", style="dim")
    for f in result["staged_files"]:
        staged_table.add_row(f['path'], f['reason'] or "N/A")

    # 4. Accessed Files list
    accessed_list = Text()
    for f in s.get("files_accessed_by_fow", []):
        accessed_list.append(f"  • {f}\n", style="dim cyan")
    
    console.print()
    console.print(Panel(
        stats_table,
        title="[bold green]✔ FOW AGENT COMPLETE[/]",
        border_style="green",
        expand=False
    ))
    
    console.print(Panel(
        token_table,
        title="[bold yellow]🪙 TOKEN & API REPORT[/]",
        border_style="yellow",
        expand=False
    ))
    
    if result["staged_files"]:
        console.print()
        console.print(staged_table)
        
    if s.get("files_accessed_by_fow"):
        console.print()
        console.print(Panel(
            accessed_list.strip(),
            title="[bold cyan]📂 Files Accessed via FOW[/]",
            border_style="cyan",
            expand=False
        ))
        
    paths_text = Text()
    paths_text.append("\nDiff saved to: ", style="bold")
    paths_text.append(f"{result['diff_path']}\n", style="underline info")
    paths_text.append("Log saved to:  ", style="bold")
    paths_text.append(f"{result['log_path']}\n", style="underline info")
    console.print(paths_text)
    db.close()

    if args.demo and result["staged_files"]:
        _serve_diff_ui(args.task, result)