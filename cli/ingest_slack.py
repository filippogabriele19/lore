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

_SLACK_INGEST_SYSTEM = """You are the LORE Architectural Supervisor. You receive a list of messages from a Slack channel conversation.
Your task is to analyze the discussion to extract design decisions, compliance constraints, security rules, or structural invariants agreed upon by the team.
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
If you find no relevant architectural rules or decisions, return {"rules": []}.
"""

def fetch_slack_history(channel: str, token: str, oldest: float | None = None) -> Any:
    url = f"https://slack.com/api/conversations.history?channel={channel}&limit=100"
    if oldest:
        url += f"&oldest={oldest}"
        
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    
    try:
        with urllib.request.urlopen(req, timeout=15) as res:
            data = json.loads(res.read().decode("utf-8"))
            if not data.get("ok"):
                console.print(f"[error]✖ Slack API error: {data.get('error')}[/]")
                return None
            return data.get("messages", [])
    except Exception as e:
        console.print(f"[error]✖ Failed to connect to Slack API: {e}[/]")
        return None

def process_slack_discussion(
    client: Any,
    db: SymbolDB,
    adr_dir: Path,
    messages: list[dict],
    channel_id: str
) -> int:
    # Build a thread-like transcript of the messages
    lines = []
    for msg in reversed(messages):  # Chronological order
        user = msg.get("user", "unknown_user")
        text = msg.get("text", "")
        if text.strip() and not msg.get("bot_id"):
            lines.append(f"[{user}]: {text}")
            
    if not lines:
        return 0
        
    transcript = "\n".join(lines)
    
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=_SLACK_INGEST_SYSTEM,
            messages=[{"role": "user", "content": f"=== SLACK CONVERSATION ===\n{transcript}"}],
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
            adr_filename = f"adr_slack_{channel_id}_{ts}_{rand_id}.md"
            adr_path = adr_dir / adr_filename
            
            adr_content = f"""# ADR: {rule_title}
            
## Metadata
- **Date**: {datetime.now().strftime("%Y-%m-%d")}
- **Source**: Slack Ingestion (Channel {channel_id})
- **Target File**: {target_file}
- **Target Symbol**: {symbol_name}
- **Status**: Active

## Context
This rule was dynamically extracted from Slack channel discussions ({channel_id}) and registered as an active compliance check.

## Decision
{description}

## Consequences
Any future edits to {target_file} or {symbol_name} must comply with this design constraint.
"""
            adr_path.write_text(adr_content, encoding="utf-8")
            
            db.register_decision_link(
                symbol_name=symbol_name,
                source_type="slack_msg",
                source_ref=f".lore/adr/{adr_filename}",
                confidence=0.80,
                description=rule_title
            )
            rules_created += 1
            
        return rules_created
    except Exception as e:
        console.print(f"[warning]⚠️ LLM rule extraction failed for Slack discussion: {e}[/]")
        return 0

def _main_ingest_slack(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="lore ingest-slack",
        description="Ingest Slack messages to extract architectural design rules",
    )
    parser.add_argument("--channel", required=True,
                        help="Slack channel ID (e.g. C12345678)")
    parser.add_argument("--token", default=None,
                        help="Slack user/bot token (optional, defaults to SLACK_API_TOKEN env)")
    parser.add_argument("--since", default=None,
                        help="Start date YYYY-MM-DD (defaults to past 30 days)")
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

    token = args.token or os.environ.get("SLACK_API_TOKEN") or os.environ.get("SLACK_BOT_TOKEN")
    if not token:
        console.print("[error]✖ Slack API Token not provided. Pass it via --token or set the SLACK_API_TOKEN environment variable.[/]")
        sys.exit(1)

    oldest_ts = None
    if args.since:
        try:
            oldest_dt = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            oldest_ts = oldest_dt.timestamp()
        except ValueError:
            console.print("[warning]⚠️ Invalid date format for --since. Expected YYYY-MM-DD.[/]")

    console.print(f"[info]🔮 Fetching history from Slack channel: {args.channel}...[/]")
    
    client = get_llm_client(project_root)
    db = SymbolDB(db_path)
    adr_dir = project_root / ".lore" / "adr"
    adr_dir.mkdir(parents=True, exist_ok=True)

    try:
        messages = fetch_slack_history(args.channel, token, oldest_ts)
        if not messages:
            console.print("[info]No messages found to ingest in the specified channel or date range.[/]")
            return

        console.print(f"[info]Retrieved {len(messages)} messages. Analyzing discussions for architectural intent...[/]")
        
        # Batch messages in chunks of 20 to preserve conversation context
        chunk_size = 20
        total_rules = 0
        for i in range(0, len(messages), chunk_size):
            chunk = messages[i:i+chunk_size]
            rules_found = process_slack_discussion(client, db, adr_dir, chunk, args.channel)
            if rules_found > 0:
                console.print(f"[success]  ✔ Extracted {rules_found} rule(s) from conversation chunk[/]")
                total_rules += rules_found
                
        if total_rules > 0:
            db.commit()
            console.print(f"[success]✔ Done. Registered {total_rules} new rules from Slack channel {args.channel}.[/]")
        else:
            console.print("[info]No architectural decisions or design rules extracted from the chat log.[/]")

    except Exception as e:
        console.print(f"[error]✖ Error running Slack ingestion: {e}[/]")
    finally:
        db.close()
