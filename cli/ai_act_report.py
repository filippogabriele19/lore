"""
cli/ai_act_report.py
─────────────────────────────────────────────────────────────────────────────
CLI command for EU AI Act (Regulation EU 2024/1689) Regulatory Compliance Audits.
"""

import sys
import argparse
from pathlib import Path

from cli.shared import DEFAULT_PROJECT, _get_db_path, console
from core.eu_ai_act_engine import EUAIActEngine


def _main_ai_act_report(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="lore ai-act-report",
        description="Run EU AI Act (Regulation EU 2024/1689) Regulatory Compliance Audit",
    )
    parser.add_argument("--project", default=DEFAULT_PROJECT,
                        help=f"Path to project root (default: {DEFAULT_PROJECT})")
    parser.add_argument("--format", choices=["html", "markdown", "json", "all"], default="all",
                        help="Report format to generate (default: all)")
    parser.add_argument("--output-dir", default=".",
                        help="Directory to save compliance reports (default: current directory)")

    args = parser.parse_args(argv)
    project_root = Path(args.project).resolve()

    if not project_root.exists():
        console.print(f"[bold red]Error:[/] Project path not found: {project_root}")
        sys.exit(1)

    db_path = _get_db_path(project_root)
    engine = EUAIActEngine(project_root, db_path)

    console.print("\n[bold cyan]🤖 Running EU AI Act Regulatory Compliance Audit...[/]")
    audit_data = engine.run_ai_act_audit()

    out_dir = Path(args.output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    generated_files = []

    if args.format in ("html", "all"):
        html_path = out_dir / "ai_act_compliance_report.html"
        engine.generate_html_report(audit_data, html_path)
        generated_files.append(html_path)

    if args.format in ("markdown", "all"):
        md_path = out_dir / "ai_act_compliance_report.md"
        engine.generate_markdown_report(audit_data, md_path)
        generated_files.append(md_path)

    if args.format in ("json", "all"):
        json_path = out_dir / "ai_act_compliance_report.json"
        engine.generate_json_report(audit_data, json_path)
        generated_files.append(json_path)

    score_color = "green" if audit_data["ai_act_score"] >= 85 else "yellow" if audit_data["ai_act_score"] >= 65 else "red"

    console.print(f"\n[bold green]✅ EU AI Act Audit Completed Successfully![/]")
    console.print(f"  • EU AI Act Compliance Score: [bold {score_color}]{audit_data['ai_act_score']} / 100[/]")
    console.print(f"  • Compliance Status: [bold white]{audit_data['compliance_tier']}[/]")
    console.print("\nGenerated Compliance Reports:")
    for f in generated_files:
        console.print(f"  📄 {f}")
    console.print()


if __name__ == "__main__":
    _main_ai_act_report()
