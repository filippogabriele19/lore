"""
cli/due_diligence.py
─────────────────────────────────────────────────────────────────────────────
CLI command for Technical Due Diligence & Codebase Health Audits (M&A, VCs).
"""

import sys
import json
import argparse
from pathlib import Path

from cli.shared import DEFAULT_PROJECT, _get_db_path, console
from core.due_diligence_engine import DueDiligenceEngine


def _main_due_diligence(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="lore due-diligence",
        description="Run Technical Due Diligence audit for M&A and VC codebase inspection",
    )
    parser.add_argument("--project", default=DEFAULT_PROJECT,
                        help=f"Path to project root (default: {DEFAULT_PROJECT})")
    parser.add_argument("--format", choices=["html", "markdown", "json", "all"], default="all",
                        help="Report format to generate (default: all)")
    parser.add_argument("--output-dir", default=".",
                        help="Directory to save audit reports (default: current directory)")

    args = parser.parse_args(argv)
    project_root = Path(args.project).resolve()

    if not project_root.exists():
        console.print(f"[bold red]Error:[/] Project path not found: {project_root}")
        sys.exit(1)

    db_path = _get_db_path(project_root)
    engine = DueDiligenceEngine(project_root, db_path)

    console.print("\n[bold cyan]🔮 Running LORE Technical Due Diligence Audit...[/]")
    audit_data = engine.run_due_diligence_audit()

    out_dir = Path(args.output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    generated_files = []

    if args.format in ("html", "all"):
        html_file = out_dir / "due_diligence_report.html"
        engine.generate_html_report(audit_data, html_file)
        generated_files.append(str(html_file))

    if args.format in ("markdown", "all"):
        md_file = out_dir / "due_diligence_report.md"
        engine.generate_markdown_report(audit_data, md_file)
        generated_files.append(str(md_file))

    if args.format in ("json", "all"):
        json_file = out_dir / "due_diligence_report.json"
        json_file.write_text(json.dumps(audit_data, indent=2), encoding="utf-8")
        generated_files.append(str(json_file))

    health = audit_data["health"]
    bus = audit_data["bus_factor"]

    console.print("\n[bold green]✅ Due Diligence Audit Completed Successfully![/]")
    console.print(f"  • [bold white]Codebase Health Score:[/] [bold cyan]{health['health_score']} / 100[/] ({health['health_grade']})")
    console.print(f"  • [bold white]Bus Factor Risk Level:[/] [bold yellow]{bus['bus_factor_risk_level']}[/]")
    console.print(f"  • [bold white]Single-Author Concentrated Files:[/] {bus['single_author_files_count']} ({bus['bus_factor_ratio_percent']}%)")
    console.print("\n[bold magenta]Generated Audit Reports:[/]")
    for gf in generated_files:
        console.print(f"  📄 [underline]{gf}[/]")
    console.print("")
