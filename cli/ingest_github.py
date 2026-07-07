import os
import sys
import argparse
import json
import re
import random
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cli.shared import console, DEFAULT_PROJECT, _get_db_path
from core.symbol_db import SymbolDB
from core.llm_client import get_llm_client, load_local_env

_GITHUB_INGEST_SYSTEM = """You are the LORE Architectural Supervisor. You receive the details of a closed GitHub Pull Request or Issue (title, description, and target files).
Your task is to analyze the text to extract design decisions, compliance constraints, security rules, or structural invariants established.
Return EXCLUSIVELY a valid JSON object (do not wrap it in markdown blocks or any other text) with this structure:
{
  "rules": [
    {
      "target_file": "relative_file_name.ext",
      "symbol_name": "function_or_class_or_global_name",
      "rule_title": "Short rule title",
      "rule_description": "Detailed explanation of the decision or constraint"
    }
  ]
}
If you find no relevant architectural rules or constraints, return {"rules": []}.
"""

def fetch_github_api(url: str, token: str | None = None) -> Any:
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "LORE-Agent")
    req.add_header("Accept", "application/vnd.github.v3+json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            return json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 403 and "rate limit" in e.reason.lower():
            console.print("[warning]⚠️ GitHub API rate limit exceeded. Please provide a GitHub token via --token.[/]")
        else:
            console.print(f"[error]✖ GitHub API error: {e.code} {e.reason}[/]")
        return None
    except Exception as e:
        console.print(f"[error]✖ Failed to connect to GitHub API: {e}[/]")
        return None

def process_and_register_item(
    client: Any,
    db: SymbolDB,
    adr_dir: Path,
    item_type: str,
    number: int,
    title: str,
    body: str,
    html_url: str,
    merged_at: str | None = None
) -> int:
    body = body or ""
    text_content = f"Title: {title}\nDescription: {body}\nType: {item_type} #{number}\nLink: {html_url}"
    
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=_GITHUB_INGEST_SYSTEM,
            messages=[{"role": "user", "content": f"=== SOURCE CONTENT ===\n{text_content}"}],
            timeout=10.0
        )
        raw_res = response.content[0].text.strip()
        match = re.search(r"\{.*\}", raw_res, re.DOTALL)
        if not match:
            return 0

        extracted_data = json.loads(match.group(0))
        rules = extracted_data.get("rules", [])
        if not rules:
            return 0

        rules_created = 0
        for rule in rules:
            target_file = rule.get("target_file", "global")
            symbol_name = rule.get("symbol_name", "global")
            rule_title = rule.get("rule_title", "Design Rule")
            description = rule.get("rule_description", "")
            
            if not description:
                continue
                
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            rand_id = random.randint(1000, 9999)
            adr_filename = f"adr_gh_{item_type}_{number}_{ts}_{rand_id}.md"
            adr_path = adr_dir / adr_filename
            
            ref_date = merged_at or datetime.now().strftime("%Y-%m-%d")
            if "T" in ref_date:
                ref_date = ref_date.split("T")[0]

            adr_content = f"""# ADR: {rule_title}
            
## Metadata
- **Date**: {ref_date}
- **Source**: GitHub {item_type.upper()} #{number}
- **Source Link**: [{item_type.upper()} #{number}]({html_url})
- **Target File**: {target_file}
- **Target Symbol**: {symbol_name}
- **Status**: Active

## Context
This rule was dynamically extracted from GitHub historical records ({item_type} #{number}) and registered as an active compliance check.

## Decision
{description}

## Consequences
Any future edits to {target_file} or {symbol_name} must comply with this design constraint.
"""
            adr_path.write_text(adr_content, encoding="utf-8")
            
            db.register_decision_link(
                symbol_name=symbol_name,
                source_type=f"github_{item_type}",
                source_ref=f".lore/adr/{adr_filename}",
                confidence=0.85,
                description=rule_title
            )
            rules_created += 1
            
        return rules_created
    except Exception as e:
        console.print(f"[warning]⚠️ LLM rule extraction failed for #{number}: {e}[/]")
        return 0

def _main_ingest_github(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="lore ingest-github",
        description="Ingest GitHub pull requests and issues to extract architectural design rules",
    )
    parser.add_argument("--repo", required=True,
                        help="GitHub repository path (e.g. owner/repo)")
    parser.add_argument("--token", default=None,
                        help="GitHub personal access token (optional, recommended)")
    parser.add_argument("--since", default=None,
                        help="Start date YYYY-MM-DD (defaults to past 30 days)")
    parser.add_argument("--type", choices=["all", "prs", "issues"], default="all",
                        help="Which items to ingest (default: all)")
    parser.add_argument("--project", default=DEFAULT_PROJECT,
                        help=f"Path to LORE project root (default: {DEFAULT_PROJECT})")
    args = parser.parse_args(argv)

    project_root = Path(args.project).resolve()
    if not project_root.exists():
        console.print(f"[error]Project path not found: {project_root}[/]")
        sys.exit(1)

    load_local_env(project_root)
    db_path = _get_db_path(project_root)
    if not db_path.exists():
        console.print(f"[error]✖ LORE Database not found under {project_root}. Please run 'lore init' first.[/]")
        sys.exit(1)

    # Resolve token from CLI flag, environment or local .env
    token = args.token or os.environ.get("GITHUB_TOKEN")

    console.print(f"[info]🔮 Connecting to GitHub repository: {args.repo}...[/]")
    
    client = get_llm_client(project_root)
    db = SymbolDB(db_path)
    adr_dir = project_root / ".lore" / "adr"
    adr_dir.mkdir(parents=True, exist_ok=True)
    
    total_rules = 0
    items_processed = 0

    try:
        # Ingest Pull Requests
        if args.type in ("all", "prs"):
            console.print("[info]Fetching closed Pull Requests...[/]")
            prs_url = f"https://api.github.com/repos/{args.repo}/pulls?state=closed&per_page=100"
            prs = fetch_github_api(prs_url, token)
            if prs:
                for pr in prs:
                    if not pr.get("merged_at"):
                        continue  # Only interested in merged PRs
                    
                    # Optional date filtering
                    if args.since:
                        try:
                            since_date = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                            merged_date = datetime.strptime(pr["merged_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                            if merged_date < since_date:
                                continue
                        except ValueError:
                            pass

                    console.print(f"[info]  Processing PR #{pr['number']}: {pr['title']}...[/]")
                    rules_found = process_and_register_item(
                        client, db, adr_dir, "pr", pr["number"],
                        pr.get("title", ""), pr.get("body", ""), pr.get("html_url", ""), pr.get("merged_at")
                    )
                    if rules_found > 0:
                        console.print(f"[success]    ✔ Extracted {rules_found} rule(s)[/]")
                        total_rules += rules_found
                    items_processed += 1

        # Ingest Issues
        if args.type in ("all", "issues"):
            console.print("[info]Fetching closed Issues...[/]")
            issues_url = f"https://api.github.com/repos/{args.repo}/issues?state=closed&per_page=100"
            issues = fetch_github_api(issues_url, token)
            if issues:
                for issue in issues:
                    if "pull_request" in issue:
                        continue  # Skip PRs here since they are fetched separately
                    
                    if args.since:
                        try:
                            since_date = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                            closed_date = datetime.strptime(issue["closed_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                            if closed_date < since_date:
                                continue
                        except ValueError:
                            pass

                    console.print(f"[info]  Processing Issue #{issue['number']}: {issue['title']}...[/]")
                    rules_found = process_and_register_item(
                        client, db, adr_dir, "issue", issue["number"],
                        issue.get("title", ""), issue.get("body", ""), issue.get("html_url", ""), issue.get("closed_at")
                    )
                    if rules_found > 0:
                        console.print(f"[success]    ✔ Extracted {rules_found} rule(s)[/]")
                        total_rules += rules_found
                    items_processed += 1

        if items_processed > 0:
            db.commit()
            console.print(f"[success]✔ Done. Processed {items_processed} items. Registered {total_rules} new rules.[/]")
        else:
            console.print("[info]No eligible PRs or issues found to ingest.[/]")
            
    except Exception as e:
        console.print(f"[error]✖ Error running ingestion: {e}[/]")
    finally:
        db.close()
