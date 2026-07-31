"""
cli/dora_report.py
─────────────────────────────────────────────────────────────────────────────
CLI command for EU DORA (Digital Operational Resilience Act) & NIS2 Audit.
"""

import sys
import json
import argparse
from pathlib import Path

from cli.shared import DEFAULT_PROJECT, _get_db_path, console
from core.dora_compliance_engine import DORAComplianceEngine


def _main_dora_report(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="lore dora-report",
        description="Run EU DORA (Digital Operational Resilience Act) Regulatory Audit",
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
    engine = DORAComplianceEngine(project_root, db_path)

    console.print("\n[bold cyan]🛡️ Running EU DORA Regulatory Compliance Audit...[/]")
    dora_data = engine.run_dora_audit()

    out_dir = Path(args.output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    generated_files = []

    if args.format in ("html", "all"):
        html_file = out_dir / "dora_compliance_report.html"
        engine.generate_html_report(dora_data, html_file)
        generated_files.append(str(html_file))

    if args.format in ("markdown", "all"):
        md_file = out_dir / "dora_compliance_report.md"
        engine.generate_markdown_report(dora_data, md_file)
        generated_files.append(str(md_file))

    if args.format in ("json", "all"):
        json_file = out_dir / "dora_compliance_report.json"
        json_file.write_text(json.dumps(dora_data, indent=2), encoding="utf-8")
        generated_files.append(str(json_file))

    console.print("\n[bold green]✅ DORA Compliance Audit Completed Successfully![/]")
    console.print(f"  • [bold white]DORA Operational Resilience Score:[/] [bold cyan]{dora_data['dora_score']} / 100[/]")
    console.print(f"  • [bold white]Compliance Status:[/] [bold yellow]{dora_data['compliance_tier']}[/]")
    console.print("\n[bold magenta]Generated Compliance Reports:[ font-size: 14px;]")
    for gf in generated_files:
        console.print(f"  📄 [underline]{gf}[/]")
    console.print("")
