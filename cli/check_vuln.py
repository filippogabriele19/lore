from __future__ import annotations
import sys
import argparse
from pathlib import Path
import sqlite3 as _sq
from rich.table import Table
from rich.panel import Panel
from rich.text import Text

from cli.shared import console, DEFAULT_PROJECT, _print_banner, _get_db_path
from cli.vuln_analysis import _calculate_path_risk_score, _trace_file_taint, _run_vuln_analysis
from cli.vuln_cure import _cure_decay_and_amnesia
from cli.patch_validator import _run_patch_validation, AUTO_CURE_PATCH_SYSTEM, AUTO_CURE_TEST_SYSTEM


def _main_check_vuln(argv: list[str] | None = None) -> None:
    """check-vuln mode — run predictive vulnerability audit against the KG."""
    parser = argparse.ArgumentParser(
        prog="lore check-vuln",
        description="Predictive vulnerability and architectural decay analysis",
    )
    parser.add_argument("--project", default=DEFAULT_PROJECT,
                        help=f"Path to project root (default: {DEFAULT_PROJECT})")
    parser.add_argument("--auto-cure", action="store_true",
                        help="Automatically generate draft ADRs, register decision links, and generate/verify code patches to cure active taint paths and architectural hotspots")
    parser.add_argument("--patch", default=None,
                        help="Path to unified diff/patch file to simulate and analyze")
    parser.add_argument("--patch-staged", action="store_true",
                        help="Dynamically capture git staged changes and run counterfactual simulation on them")
    parser.add_argument("--fail-on-regression", action="store_true",
                        help="Exit with non-zero code if patch validation fails or regression is detected")
    args = parser.parse_args(argv)

    project_root = Path(args.project)
    if not project_root.exists():
        console.print(f"[error]Project path not found: {project_root}[/]")
        sys.exit(1)

    db_path = _get_db_path(project_root)
    if not db_path.exists():
        console.print(f"[error]✖ Database not found under {project_root}[/]")
        sys.exit(1)

    from core.symbol_db import SymbolDB
    db = SymbolDB(db_path)
    conn = db.con


    # Check cache bypass for pre-commit hook
    if args.patch_staged:
        try:
            import subprocess
            res_files = subprocess.run(
                ["git", "diff", "--cached", "--name-only"],
                cwd=str(project_root),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                check=True
            )
            staged_files = [f.strip().replace("\\", "/") for f in res_files.stdout.splitlines() if f.strip()]
            if staged_files:
                from cli.vuln_cache import needs_reanalysis
                if not needs_reanalysis(project_root, staged_files):
                    console.print("[info]⚡ Git Hook Bypass: staged changes do not intersect with cached taint paths. Skipping full scan.[/]")
                    sys.exit(0)
        except Exception:
            pass

    # 1. Run core vulnerability analysis
    try:
        from core.chat_miner import mine_chat_intent
        mine_chat_intent(db_path, project_root)
    except Exception:
        pass

    analysis_res = _run_vuln_analysis(project_root, conn)
    try:
        from cli.vuln_cache import save_cache, _hash_file
        file_hashes = {str(f.relative_to(project_root)): _hash_file(f)
                       for f in project_root.rglob("*.py")
                       if not any(x in str(f) for x in (".venv", "venv", ".lore", "__pycache__"))}
        save_cache(project_root, analysis_res, file_hashes)
    except Exception:
        pass
    files_map = analysis_res["files_map"]
    sinks = analysis_res["sinks"]
    exposed_paths = analysis_res["exposed_paths"]
    amnesia_hotspots = analysis_res["amnesia_hotspots"]
    decay_events = analysis_res["decay_events"]

    # 2. Handle auto-cure of decay and amnesia
    if args.auto_cure:
        _cure_decay_and_amnesia(project_root, conn, decay_events, amnesia_hotspots)
        decay_events = []
        amnesia_hotspots = []

    # 3. Premium CLI Output header
    console.print()
    _print_banner()
    console.print("\n[phase]=== LORE PREDICTIVE VULNERABILITY AUDIT ===[/]")
    console.print(f"Database: [info]{db_path}[/]\n")

    summary_text = Text()
    summary_text.append("Files Analyzed:   ", style="bold")
    summary_text.append(f"{len(files_map)}\n", style="info")
    summary_text.append("Source Patterns:  ", style="bold")
    summary_text.append("view, handler, api, route, controller, request, middleware\n", style="info")
    summary_text.append("Sinks Exposed:    ", style="bold")
    summary_text.append(f"{len(sinks)}\n", style="info")
    summary_text.append("Taint Paths:      ", style="bold")
    summary_text.append(f"{len(exposed_paths)} detected", style="error" if exposed_paths else "success")
    summary_text.append("\nSevere Amnesia:   ", style="bold")
    summary_text.append(f"{len(amnesia_hotspots)} files", style="error" if amnesia_hotspots else "success")
    summary_text.append("\nDecay Events:     ", style="bold")
    summary_text.append(f"{len(decay_events)} detected\n", style="warning" if decay_events else "success")

    console.print(Panel(
        summary_text,
        title="[bold white]Audit Summary[/]",
        border_style="red" if (exposed_paths or amnesia_hotspots) else "green",
        expand=False
    ))
    console.print()

    # 4. Handle dynamic staged patch validation
    import subprocess
    staged_patch_path = None
    if args.patch_staged:
        try:
            res_git = subprocess.run(
                ["git", "diff", "--cached"],
                cwd=str(project_root),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                check=True
            )
            staged_diff = res_git.stdout
            if not staged_diff.strip():
                console.print("[info]No staged changes detected in git. Skipping patch audit.[/]")
            else:
                lore_dir = project_root / ".lore"
                lore_dir.mkdir(parents=True, exist_ok=True)
                staged_patch_path = lore_dir / "staged_changes.patch"
                staged_patch_path.write_text(staged_diff, encoding="utf-8")
                args.patch = str(staged_patch_path)
        except Exception as e:
            console.print(f"[error]✖ Failed to extract staged git diff: {e}[/]")
            sys.exit(1)

    # Run counterfactual simulation / auto-curing of code paths
    res_patch = {}
    try:
        res_patch = _run_patch_validation(
            project_root=project_root,
            conn=conn,
            db_path=db_path,
            exposed_paths=exposed_paths,
            auto_cure=args.auto_cure,
            patch_path_str=args.patch
        )
    finally:
        if staged_patch_path and staged_patch_path.exists():
            try:
                staged_patch_path.unlink()
            except Exception:
                pass

    # 4.5. Run compliance firewall validation (fail-on-regression)
    if args.fail_on_regression:
        failed = False
        reasons = []

        if args.patch:
            if res_patch.get("regression_paths_count", 0) > 0:
                failed = True
                reasons.append(f"Regressions detected: {res_patch['regression_paths_count']} previously cured taint path(s) re-introduced.")
            if res_patch.get("survived_paths_count", 0) > 0:
                failed = True
                reasons.append(f"Patch ineffective: {res_patch['survived_paths_count']} taint path(s) survived.")
            if res_patch.get("new_paths_count", 0) > 0:
                failed = True
                reasons.append(f"New vulnerabilities: {res_patch['new_paths_count']} taint path(s) introduced by the patch.")

        staged_files = []
        if args.patch_staged:
            try:
                res_files = subprocess.run(
                    ["git", "diff", "--cached", "--name-only"],
                    cwd=str(project_root),
                    capture_output=True,
                    encoding="utf-8",
                    errors="replace",
                    check=True
                )
                staged_files = [f.strip().replace("\\", "/") for f in res_files.stdout.splitlines() if f.strip()]
            except Exception:
                pass

        if staged_files:
            staged_amnesia = [h for h in amnesia_hotspots if h["path"].replace("\\", "/") in staged_files]
            if staged_amnesia:
                failed = True
                for h in staged_amnesia:
                    reasons.append(f"Severe Amnesia: Staged file '{h['path']}' is a critical subsystem hotspot but lacks documented design ADRs.")
            
            staged_decay = []
            for e in decay_events:
                intersection = [f for f in e["files"] if f.replace("\\", "/") in staged_files]
                if intersection:
                    staged_decay.append((e, intersection))
            if staged_decay:
                failed = True
                for e, files in staged_decay:
                    reasons.append(f"Architectural Decay: Staged file(s) {files} modified in commit {e['hash'][:8]} without ADR documenting the implicit invariants.")

        if failed:
            console.print("\n[bold red]🚨 LORE COMMIT BLOCKER - COMPLIANCE FAILURES DETECTED:[/]")
            for r in reasons:
                console.print(f"  * {r}")
            console.print("\n[dim]To bypass, resolve the taint paths, add missing ADRs using 'lore adr', or run 'lore auto-cure' to self-heal the codebase.[/]\n")
            try:
                conn.close()
            except Exception:
                pass
            sys.exit(1)

    # 5. Output amnesia hotspots table if any remain
    if amnesia_hotspots:
        console.print("[error]🚨 Severe Amnesia on Critical Subsystems (Bayesian Analysis)[/]")
        console.print("[dim]The following files perform sensitive sink actions, have high commit activity, but contain zero documented decisions in the KG:[/]")
        
        table = Table(show_header=True, header_style="bold magenta")
        table.add_column("File Path", style="bold white")
        table.add_column("Commits", justify="center")
        table.add_column("ADRs", justify="center")
        table.add_column("Bayesian Risk", justify="center", style="bold red")
        table.add_column("95% Conf. Interval", justify="center", style="bold cyan")
        table.add_column("Status", style="bold red")
        
        for h in amnesia_hotspots[:10]:
            risk_pct = h["bayes_risk"] * 100
            ci_lower = h["ci"][0] * 100
            ci_upper = h["ci"][1] * 100
            table.add_row(
                h["path"],
                str(h["change_freq"]),
                str(h["adr_count"]),
                f"{risk_pct:.1f}%",
                f"[{ci_lower:.1f}% - {ci_upper:.1f}%]",
                "Document Missing"
            )
        console.print(table)
        console.print()

    # 6. Output decay events log if any remain
    if decay_events:
        console.print("[warning]⚠️ Architectural Decay (Implicit Invariant Drift)[/]")
        console.print("[dim]Commits with risk-related descriptions affecting critical sinks without updating design documentation:[/]")
        for e in decay_events[:5]:
            console.print(f"\n* [bold yellow]{e['hash'][:8]}[/] by [cyan]{e['author']}[/] on [info]{e['date']}[/]")
            console.print(f"  [bold]Message:[/] {e['body']}")
            console.print(f"  [bold]Files touched:[/] {', '.join(e['files'])}")
        console.print()

    if not exposed_paths and not amnesia_hotspots and not decay_events:
        console.print("[success]✓ Zero Predictive Vulnerabilities Detected[/]")
        console.print("[info]LORE did not find any call paths from sources to sinks, missing documentation on critical modules, or architectural decay events.[/]")

    try:
        conn.close()
    except Exception:
        pass
