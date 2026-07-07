from __future__ import annotations

import os
import json
import re
import random
import logging
from pathlib import Path
from datetime import datetime as _dt
from typing import Any

from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse
import uvicorn

from cli.shared import console, DEFAULT_PROJECT, _get_db_path
from core.symbol_db import SymbolDB
from core.llm_client import get_llm_client

logger = logging.getLogger(__name__)

app = FastAPI(
    title="LORE Webhook Ingestion Server",
    description="Asynchronously ingests Slack and GitHub events to feed LORE's Institutional Memory.",
    version="1.0.0"
)

# Shared project root configuration (set when server starts)
_PROJECT_ROOT: Path = Path(DEFAULT_PROJECT)

_GITHUB_EXTRACTOR_SYSTEM = """You are the LORE Architectural Supervisor. You receive the details of a merged Pull Request (title, description, modified files).
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

_SLACK_EXTRACTOR_SYSTEM = """You are the LORE Architectural Supervisor. You receive a message or conversation from Slack.
Your task is to analyze the text to extract design decisions, compliance constraints, security rules, or structural invariants agreed upon by the team.
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
If you find no relevant rules or decisions, return {"rules": []}.
"""

def _extract_and_register_rules(system_prompt: str, text_content: str) -> int:
    """Invokes Claude to extract rules from text and registers them in LORE database."""
    try:
        client = get_llm_client(_PROJECT_ROOT)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=system_prompt,
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

        db_path = _get_db_path(_PROJECT_ROOT)
        db = SymbolDB(db_path)
        
        adr_dir = _PROJECT_ROOT / ".lore" / "adr"
        adr_dir.mkdir(parents=True, exist_ok=True)
        
        rules_created = 0
        for rule in rules:
            target_file = rule.get("target_file", "global")
            symbol_name = rule.get("symbol_name", "global")
            title = rule.get("rule_title", "Design Rule")
            description = rule.get("rule_description", "")
            
            if not description:
                continue
                
            ts = _dt.now().strftime("%Y%m%d_%H%M%S")
            rand_id = random.randint(1000, 9999)
            adr_filename = f"adr_webhook_{ts}_{rand_id}.md"
            adr_path = adr_dir / adr_filename
            
            adr_content = f"""# ADR: {title}
            
## Metadata
- **Date**: {_dt.now().strftime("%Y-%m-%d")}
- **Source**: Webhook Ingestion
- **Target File**: {target_file}
- **Target Symbol**: {symbol_name}
- **Status**: Active

## Context
This rule was dynamically extracted from webhook events (GitHub PR / Slack discussions) and registered as an active compliance check.

## Decision
{description}

## Consequences
Any future edits to {target_file} or {symbol_name} must comply with this design constraint.
"""
            adr_path.write_text(adr_content, encoding="utf-8")
            
            db.register_decision_link(
                symbol_name=symbol_name,
                source_type="webhook_adr",
                source_ref=f".lore/adr/{adr_filename}",
                confidence=1.0,
                description=title
            )
            rules_created += 1
            
        db.commit()
        db.close()
        
        if rules_created > 0:
            console.print(f"[success]✔ Webhook Ingestion: Registered {rules_created} new design rules to the Knowledge Graph.[/]")
        return rules_created
    except Exception as e:
        logger.warning(f"Error processing webhook content: {e}")
        return 0

@app.post("/webhooks/github")
async def github_webhook(request: Request, background_tasks: BackgroundTasks) -> Any:
    """Handles GitHub PR closed/merged event webhooks."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON payload"})
        
    action = payload.get("action")
    pull_request = payload.get("pull_request")
    
    if action == "closed" and pull_request and pull_request.get("merged") is True:
        title = pull_request.get("title", "")
        body = pull_request.get("body", "")
        
        # Build text description of PR context
        pr_text = f"PR Title: {title}\nPR Body: {body}\n"
        background_tasks.add_task(_extract_and_register_rules, _GITHUB_EXTRACTOR_SYSTEM, pr_text)
        return {"status": "processing", "message": "PR merged webhook received"}
        
    return {"status": "ignored", "message": "Only closed & merged PR events are processed"}

@app.post("/webhooks/slack")
async def slack_webhook(request: Request, background_tasks: BackgroundTasks) -> Any:
    """Handles Slack event webhooks (includes Event API challenge verification)."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON payload"})
        
    # Handle Slack URL verification challenge
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge")}
        
    event = payload.get("event")
    if event and event.get("type") == "message" and not event.get("bot_id"):
        text = event.get("text", "")
        # Process Slack message for rules
        background_tasks.add_task(_extract_and_register_rules, _SLACK_EXTRACTOR_SYSTEM, text)
        return {"status": "processing", "message": "Slack message received"}
        
    return {"status": "ignored", "message": "Event type ignored"}

def _main_webhook_server(argv: list[str] | None = None) -> None:
    """Main webhook server entrypoint."""
    import argparse
    parser = argparse.ArgumentParser(
        prog="lore webhook-server",
        description="Starts LORE asynchronous Webhook Ingestion Server",
    )
    parser.add_argument("--project", default=DEFAULT_PROJECT,
                        help=f"Path to project root (default: {DEFAULT_PROJECT})")
    parser.add_argument("--port", type=int, default=9000,
                        help="Port to run the webhook server on (default: 9000)")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Host to run the webhook server on (default: 127.0.0.1)")
    args = parser.parse_args(argv)
    
    global _PROJECT_ROOT
    _PROJECT_ROOT = Path(args.project).resolve()
    
    console.print(f"\n[bold cyan]🚀 Starting LORE Webhook Ingestion Server...[/]")
    console.print(f"Host: [info]{args.host}[/]")
    console.print(f"Port: [info]{args.port}[/]")
    console.print(f"Workspace: [info]{_PROJECT_ROOT}[/]\n")
    
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
