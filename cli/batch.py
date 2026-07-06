import os
import sys
import argparse
import json
import time
from pathlib import Path
from rich.table import Table

from cli.shared import console, DEFAULT_PROJECT, _get_db_path
from cli.cve_registry import _CVE_REGISTRY, _run_cve_retrospective
from cli.cve import _build_batch_html
from cli.diff_server import _serve_console

def _main_batch(argv: list) -> None:
    import argparse, json as _json, webbrowser as _wb, http.server as _hs, threading as _th
    p = argparse.ArgumentParser(prog="lore batch")
    p.add_argument("--config", required=True, help="JSON [{project, cve}, ...]")
    p.add_argument("--out", default=None, help="Output dir for batch report (default: current directory)")
    args = p.parse_args(argv)

    cfg_path = Path(args.config)
    if not cfg_path.exists():
        console.print(f"[error]✖ Config not found: {cfg_path}[/]")
        return
    batch = _json.loads(cfg_path.read_text(encoding="utf-8"))

    results = []
    
    from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
    
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        console=console
    ) as progress:
        task_id = progress.add_task("[bold magenta]Processing batch...[/]", total=len(batch))
        
        for entry in batch:
            project_root = Path(entry["project"])
            cve_id = entry["cve"]
            
            progress.update(task_id, description=f"[bold cyan]Analyzing {cve_id} in {project_root.name}...[/]")
            
            db_path = _get_db_path(project_root)
            if not db_path.exists():
                progress.advance(task_id)
                continue
            if cve_id not in _CVE_REGISTRY:
                progress.advance(task_id)
                continue
                
            result = _run_cve_retrospective(str(db_path), cve_id)
            if "error" in result:
                progress.advance(task_id)
                continue
                
            result["project"] = project_root.name
            result["project_path"] = str(project_root)
            
            # Attach history coverage metadata
            try:
                import sqlite3 as _sq3
                _conn = _sq3.connect(str(db_path))
                _row = _conn.execute(
                    "SELECT COUNT(*), MIN(date) FROM commit_reasoning"
                ).fetchone()
                _conn.close()
                result["history_commits"] = _row[0] or 0
                result["history_oldest"]  = (_row[1] or "")[:10]
            except Exception:
                result["history_commits"] = 0
                result["history_oldest"]  = ""
                
            results.append(result)
            progress.advance(task_id)

    if not results:
        console.print("[warning]⚠ No results to report.[/]")
        return

    # Render summary table in terminal
    summary_table = Table(title="Batch Assessment Summary", show_header=True, header_style="bold magenta")
    summary_table.add_column("Verdict", justify="center")
    summary_table.add_column("CVE ID", style="bold")
    summary_table.add_column("Project", style="cyan")
    summary_table.add_column("Score", justify="right")
    summary_table.add_column("Signals", justify="right")
    summary_table.add_column("Coverage", justify="center")
    summary_table.add_column("Top Signal", max_width=40)
    
    for r in results:
        score = r["stats"]["detection_score"]
        if score >= 55:
            verdict = "[bold red]ALERT[/]"
        elif score >= 35:
            verdict = "[bold yellow]REVIEW[/]"
        else:
            verdict = "[bold green]CLEAR[/]"
            
        cve_id = r["cve_id"]
        proj = r.get("project", "unknown")
        sigs = r["stats"]["signals"]
        
        # Coverage status text
        history_commits = r.get("history_commits", 0)
        history_oldest  = r.get("history_oldest", "")
        cve_disclosure  = r.get("cfg", {}).get("disclosure_date", "")[:7]
        if history_oldest and cve_disclosure and history_oldest[:7] < cve_disclosure:
            coverage = "[green]Full[/]"
        elif history_commits == 0:
            coverage = "[red]None[/]"
        else:
            coverage = "[yellow]Partial[/]"
            
        top_signal = ""
        for s in r.get("signals", []):
            if s["type"] == "amnesia" and s.get("links", 1) == 0:
                top_signal = s['title'].split('—')[0].strip()
                break
                
        summary_table.add_row(
            verdict,
            cve_id,
            proj,
            f"{score}%",
            f"{sigs}",
            coverage,
            top_signal
        )
        
    console.print()
    console.print(summary_table)

    out_dir  = Path(args.out) if args.out else Path.cwd()
    out_dir.mkdir(parents=True, exist_ok=True)
    from datetime import datetime as _dt
    ts       = _dt.now().strftime("%Y%m%d_%H%M%S")
    out_path = out_dir / f"batch_report_{ts}.html"
    out_path.write_text(_build_batch_html(results), encoding="utf-8")
    console.print(f"\n[success]✔[/] [bold green]Report HTML saved to:[/] [underline info]{out_path}[/]")

    # Serve
    db_path = _get_db_path(project_root)
    extra_data = {"batch_results": results}
    _serve_console(project_root, db_path, "batch", 8788, extra_data)


