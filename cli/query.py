import os
import sys
import argparse
import json
from pathlib import Path

from cli.shared import console, DEFAULT_PROJECT, MODEL, _get_db_path, _print_banner, Panel, Text
from rich.markdown import Markdown
from cli.prompts import QUERY_SYSTEM
from cli.agent_retrieval import _build_project_map, _build_compact_project_map, _astar_bundle_light
from cli.v11_retrieval import v11_retrieve_context
from cli.agent_stage import _get_co_changes
from core.symbol_map import SymbolDB, SymbolRetriever

def lore_query(question: str, db_path: Path) -> str:
    """
    Answers an architectural question using the Knowledge Graph.
    Read-only: never modifies project files.

    Uses the same A* semantic retrieval as run_agent (which already injects
    DECISION CONTEXT and HOTSPOT WARNINGS via _astar_bundle), then enriches
    the bundle with CO-CHANGE PATTERNS before sending to Claude.
    """
    if not db_path.exists():
        return f"[error] DB not found at {db_path}. Run `python lore.py --rescan` first."

    project_root = db_path.parent
    try:
        from core.llm_client import get_llm_client
        client = get_llm_client(project_root)
    except Exception as e:
        return f"[error] {e}"

    db           = SymbolDB(db_path)
    retriever    = SymbolRetriever(db, project_root)

    print(f"[A*]    retrieving context for: {question!r}")
    bundle, visited_syms = v11_retrieve_context(question, db, retriever, token_budget=4500)

    if not bundle:
        db.close()
        return (
            "[error] No embeddings found in the index. "
            "Run with --rescan to build the symbol index."
        )

    print(f"[A*]    {len(visited_syms)} symbols retrieved · enriching with co-change patterns...")

    co_changes = _get_co_changes(db_path, visited_syms)
    if co_changes:
        co_lines = ["=== CO-CHANGE PATTERNS (files frequently modified together) ==="]
        for c in co_changes:
            co_lines.append(
                f"  {c['file_a']}  ↔  {c['file_b']}"
                f"  (together {c['count']}x, last: {c['last_seen']})"
            )
        bundle = bundle + "\n\n" + "\n".join(co_lines)

    db.close()

    print(f"[LLM]   calling {MODEL} (read-only)...")
    response = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=QUERY_SYSTEM,
        messages=[{
            "role": "user",
            "content": f"{bundle}\n\n=== DOMANDA ===\n{question}",
        }],
    )
    return response.content[0].text


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_index(project_root: Path, db_path: Path, rescan: bool) -> tuple["SymbolDB", "SymbolRetriever"]:
    """Shared scan/embed/mine step used by both apply and query modes."""
    _print_banner()

    project_panel_content = Text()
    project_panel_content.append("Project Root: ", style="bold")
    project_panel_content.append(f"{project_root}\n", style="info")
    project_panel_content.append("Database:     ", style="bold")
    project_panel_content.append(f"{db_path}\n", style="info")
    project_panel_content.append("Mode:         ", style="bold")
    project_panel_content.append("Rescan" if rescan else "Incremental", style="warning" if rescan else "success")
    
    console.print(Panel(project_panel_content, title="[bold white]Project Environment[/]", border_style="cyan"))

    from cli.cache import restore_or_create_db
    db = restore_or_create_db(project_root, db_path, rescan=rescan)

    import time as _time
    _now = _time.time()

    def _get_meta_age_minutes(key: str) -> float | None:
        """Returns age in minutes of a meta timestamp, or None if not found."""
        return db.get_meta_age_minutes(key, _now)

    def _set_meta(key: str) -> None:
        db.set_meta(key, _now)
        db.commit()


    is_headless = "--headless" in sys.argv or "LORE_HEADLESS" in os.environ
    _threshold_min = 43200.0 if is_headless else 60.0

    _git_age = _get_meta_age_minutes("git_mined_at")
    _link_age = _get_meta_age_minutes("links_built_at")

    _run_git = rescan or _git_age is None or _git_age >= _threshold_min
    _run_link = rescan or _link_age is None or _link_age >= _threshold_min

    if is_headless:
        console.print(f"[info][HEADLESS MODE][/] Metadata age threshold set to 30 days ({int(_threshold_min)}m). Cache status:")

    if _run_git:
        reason = "missing mining data (cold start)" if _git_age is None else f"mining age {int(_git_age)}m >= threshold {int(_threshold_min)}m"
        console.print(f"[info][GIT MINING][/] Relaunching git history mining ({reason})...")
        with console.status("[bold magenta]Mining git history and commit reasoning...[/]", spinner="dots"):
            try:
                from core.git_miner import GitMiner
                GitMiner(str(project_root), str(db_path)).run()
                _set_meta("git_mined_at")
                console.print("[success]✔[/] [bold green]Git History:[/] Mining complete and indexed")
            except Exception as e:
                console.print(f"[error]✖[/] [bold green]Git History:[/] Failed or skipped: {e}")
    else:
        console.print(f"[success]✔[/] [bold green]Git History:[/] Using cached data (mined [bold yellow]{int(_git_age)}[/] minutes ago, threshold: {int(_threshold_min)}m)")

    if _run_link:
        reason = "missing decision links (cold start)" if _link_age is None else f"link age {int(_link_age)}m >= threshold {int(_threshold_min)}m"
        console.print(f"[info][DECISION LINKS][/] Relaunching decision links construction ({reason})...")
        with console.status("[bold magenta]Building decision links...[/]", spinner="dots"):
            try:
                from core.decision_linker import DecisionLinker
                DecisionLinker(str(db_path)).build_links(str(project_root))
                _set_meta("links_built_at")
                console.print("[success]✔[/] [bold yellow]Decision Links:[/] Construction completed")
            except Exception as e:
                console.print(f"[error]✖[/] [bold yellow]Decision Links:[/] Failed or skipped: {e}")
    else:
        console.print(f"[success]✔[/] [bold yellow]Decision Links:[/] Using cached data (built [bold yellow]{int(_link_age)}[/] minutes ago, threshold: {int(_threshold_min)}m)")

    # Aggiorna la cache se abbiamo fatto mining (git o decision links)
    if _run_git or _run_link:
        console.print("[info][CACHE UPDATE][/] Updating centralized cache with new mining metadata and intents...")
        db.close()
        try:
            from cli.cache import save_db_to_cache
            save_db_to_cache(project_root, db_path)
        except Exception as e:
            console.print(f"[warning]Failed to save database to cache: {e}[/]")
        db = SymbolDB(db_path)

    retriever = SymbolRetriever(db, project_root)
    console.print("[accent]════════════════════════════════════════════════════════════[/]")
    return db, retriever


def _is_analysis_query(task: str) -> bool:
    """
    Returns True if task is a read-only analysis question, False if it is a
    code modification request.

    Step 1: zero-cost heuristic (keyword prefix or '?').
    Step 2: if ambiguous, one Haiku call (max_tokens=10) to classify.
    """
    _QUERY_STARTERS = {
        # English
        "what", "why", "how", "who", "show", "explain", "find", "list", "describe", "where", "can",
        # Italian
        "se", "cosa", "quali", "come", "perché", "dimmi",
        "spiega", "mostra", "elenca", "trova",
    }
    t = task.strip()
    first_word = t.split()[0].lower().rstrip("?.,!") if t.split() else ""
    if first_word in _QUERY_STARTERS or "?" in task:
        return True
    try:
        from core.llm_client import get_llm_client
        _client = get_llm_client(Path.cwd())
        _resp = _client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=10,
            messages=[{
                "role": "user",
                "content": (
                    "Is this a read-only analysis question or a code modification task?\n"
                    f"Reply with exactly one word: QUERY or MODIFY.\nTask: {task}"
                ),
            }],
        )
        return _resp.content[0].text.strip().upper() == "QUERY"
    except Exception:
        return False


def _main_query(argv: list[str] | None = None) -> None:
    """query mode — answer architectural questions, read-only."""
    parser = argparse.ArgumentParser(
        prog="lore query",
        description="Query the Knowledge Graph (read-only)",
    )
    parser.add_argument("question", help="Architectural question (e.g. 'posso migrare il billing?')")
    parser.add_argument("--project", default=DEFAULT_PROJECT,
                        help=f"Path to project root (default: {DEFAULT_PROJECT})")
    parser.add_argument("--rescan", action="store_true",
                        help="Force re-scan before querying")
    args = parser.parse_args(argv)

    # Provider-agnostic API key check
    has_key = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENROUTER_API_KEY") or os.environ.get("DEEPSEEK_API_KEY"))
    if not has_key:
        # Check .env in project root
        env_path = Path(args.project) / ".env"
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if "API_KEY" in line and "=" in line:
                    has_key = True
                    break
    if not has_key:
        print("[error] No API key found. Set ANTHROPIC_API_KEY, OPENAI_API_KEY, or configure via `lore init`.")
        sys.exit(1)

    project_root = Path(args.project)
    if not project_root.exists():
        print(f"[error] Project path not found: {project_root}")
        sys.exit(1)

    db_path = _get_db_path(project_root)

    if args.rescan or not db_path.exists():
        _build_index(project_root, db_path, rescan=True)

    answer = lore_query(args.question, db_path)
    console.print()
    console.print(Panel(
        Markdown(answer),
        title=f"[bold cyan]Answer to: {args.question}[/]",
        border_style="cyan",
        padding=(1, 2)
    ))
    console.print()


# ---------------------------------------------------------------------------
# Demo diff viewer — localhost HTML UI
# ---------------------------------------------------------------------------

