#!/usr/bin/env python3
"""
lore.py — Thin CLI entrypoint for LORE Agent
"""
# Anti-thrashing & offline mode: must be set before ANY torch/numpy/transformers import
import os as _os
_os.environ["OMP_NUM_THREADS"] = "1"
_os.environ["MKL_NUM_THREADS"] = "1"
_os.environ["OPENBLAS_NUM_THREADS"] = "1"
_os.environ["VECLIB_MAXIMUM_THREADS"] = "1"
_os.environ["NUMEXPR_NUM_THREADS"] = "1"
_os.environ["HF_HUB_OFFLINE"] = "1"
_os.environ["TRANSFORMERS_OFFLINE"] = "1"

import sys
from pathlib import Path

# Assicuriamoci che la cartella del progetto sia nel path
sys.path.insert(0, str(Path(__file__).parent))

from cli.shared import console, _print_banner, Table
from cli.apply import _main_apply
from cli.query import _main_query
from cli.audit import _main_audit
from cli.cve import _main_cve
from cli.gh_check import _main_gh_check
from cli.check_vuln import _main_check_vuln
from cli.batch import _main_batch
from cli.adr import _main_adr
from cli.watch import _main_watch
from cli.lsp import _main_lsp
from cli.init import _main_init
from cli.mcp_server import _main_mcp
from cli.git_hook import _main_git_hook
from cli.benchmark import _main_benchmark
from cli.ingest_chat import _main_ingest_chat
from cli.webhook_server import _main_webhook_server
from cli.ingest_github import _main_ingest_github
from cli.ingest_slack import _main_ingest_slack
from cli.feedback import _main_dismiss

def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        _print_banner()
        console.print("\n[bold cyan]Usage:[/]")
        console.print("  [bold white]python lore.py <command> [options][/]\n")
        
        table = Table(show_header=True, header_style="bold magenta", box=None)
        table.add_column("Command", style="bold white", width=12)
        table.add_column("Description", style="dim")
        
        table.add_row("init", "Initialize LORE workspace and index project files (bootstrap)")
        table.add_row("apply", "Apply a natural language task to modify the codebase (default mode)")
        table.add_row("query", "Query the Knowledge Graph for architectural questions (read-only)")
        table.add_row("audit", "Run a full autonomous audit and launch the Developer Console")
        table.add_row("cve", "Run a retrospective analysis on a specific historical CVE")
        table.add_row("batch", "Run retrospective scans in batch mode from a configuration file")
        table.add_row("gh-check", "Run architecture and security audit on PR changes (CI/CD)")
        table.add_row("check-vuln", "Predictive vulnerability and architectural decay analysis")
        table.add_row("auto-cure", "Run predictive vulnerability analysis and automatically cure active taint paths and hotspots")
        table.add_row("adr", "Generate and index an Architectural Decision Record (ADR) to cure Amnesia")
        table.add_row("ingest-chat", "Ingest Claude Code chat history to extract architectural design rules")
        table.add_row("watch", "Monitor filesystem changes and incrementally re-index in real time")
        table.add_row("lsp", "Start the LORE Language Server (LSP) for editor integration")
        table.add_row("mcp", "Start the LORE Model Context Protocol (MCP) server for AI integration")
        table.add_row("git-hook", "Install or uninstall LORE pre-commit git hooks")
        table.add_row("webhook-server", "Start LORE asynchronous Webhook Ingestion Server")
        table.add_row("ingest-github", "Ingest GitHub historical pull requests and issues to extract design rules")
        table.add_row("ingest-slack", "Ingest Slack historical channel messages to extract design rules")
        table.add_row("dismiss", "Dismiss a false positive LORE warning for a file or symbol")
        table.add_row("benchmark", "Run Quantitative SWE-bench validation harness (Baseline vs LORE)")
        
        console.print(table)
        console.print("\n[dim]Run [bold white]python lore.py <command> --help[/] for details on a specific command.[/]\n")
        return

    cmd = sys.argv[1]
    argv = sys.argv[2:]
    
    if cmd == "init":
        _main_init(argv)
    elif cmd == "apply":
        _main_apply(argv)
    elif cmd == "query":
        _main_query(argv)
    elif cmd == "audit":
        _main_audit(argv)
    elif cmd == "cve":
        _main_cve(argv)
    elif cmd == "batch":
        _main_batch(argv)
    elif cmd == "gh-check":
        _main_gh_check(argv)
    elif cmd == "check-vuln":
        _main_check_vuln(argv)
    elif cmd == "auto-cure":
        argv_copy = list(argv)
        if "--auto-cure" not in argv_copy:
            argv_copy.append("--auto-cure")
        _main_check_vuln(argv_copy)
    elif cmd == "adr":
        _main_adr(argv)
    elif cmd == "ingest-chat":
        _main_ingest_chat(argv)
    elif cmd == "watch":
        _main_watch(argv)
    elif cmd == "lsp":
        _main_lsp(argv)
    elif cmd == "mcp":
        _main_mcp(argv)
    elif cmd == "git-hook":
        _main_git_hook(argv)
    elif cmd == "webhook-server":
        _main_webhook_server(argv)
    elif cmd == "ingest-github":
        _main_ingest_github(argv)
    elif cmd == "ingest-slack":
        _main_ingest_slack(argv)
    elif cmd == "dismiss":
        _main_dismiss(argv)
    elif cmd == "benchmark":
        _main_benchmark(argv)
    else:
        # Fallback all'originale comportamento di apply con tutti gli argomenti
        _main_apply(sys.argv[1:])


if __name__ == "__main__":
    main()