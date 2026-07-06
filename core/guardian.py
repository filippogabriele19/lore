#!/usr/bin/env python3
"""
LORE Intent Guardian - Phase 3: The Semantic Gatekeeper

Reads an Intent Node (JSON) and a proposed Git Diff.
Uses the LLM to verify if the diff violates any guard_rules or exceptions.
Exits with code 0 if PASS, code 1 if BLOCK.

Usage:
    export ANTHROPIC_API_KEY="sk-ant-..."
    python python -m core.guardian .lore/nodes/django...json proposed_change.diff
"""

import os
import sys
import json
import re
import argparse
from pathlib import Path

SYSTEM_PROMPT = """
You are the LORE Semantic Guardian, a ruthless and rigorous gatekeeper of architectural quality.
Your job is to protect the codebase from semantic regressions by analyzing a Pull Request (Diff)
against an Architectural Contract (Intent Node JSON).

You will receive two inputs:
1. <INTENT_NODE>: Business rules, active exceptions, and current guard_rules.
2. <GIT_DIFF>: Proposed changes from the developer.

Your Task:
Mathematically and logically evaluate if the <GIT_DIFF> violates EVEN A SINGLE ONE of the `guard_rules` or
worsens the `active_exceptions` defined in <INTENT_NODE>.

Reasoning Instructions:
Use a `<thinking>` block to analyze the diff line by line and map it against the rules.
Be paranoid: if the diff removes a security check or changes vital logic, block it.
If the diff is just cosmetic refactoring that doesn't alter the intent, let it pass.

Output Formatter:
After the `<thinking>` block, you must return EXCLUSIVELY a JSON with this exact schema:

{
  "decision": "PASS | WARN | BLOCK",
  "violated_rules": ["rule_id_1", "rule_id_2"],
  "analysis": "Short technical explanation (max 3 lines)",
  "developer_comment": "A comment in Markdown to automatically post on the Pull Request explaining to the developer why they were blocked."
}
"""

def extract_json_from_response(llm_response: str) -> dict:
    start_idx = llm_response.find("{")
    if start_idx == -1:
        raise ValueError("No valid JSON found in Guardian response.")
    bracket_count = 0
    in_string = False
    escape_char = False
    quote_char = None
    for i in range(start_idx, len(llm_response)):
        char = llm_response[i]
        if escape_char:
            escape_char = False
            continue
        if char == "\\":
            escape_char = True
            continue
        if char in ('"', "'"):
            if not in_string:
                in_string = True
                quote_char = char
            elif char == quote_char:
                in_string = False
                quote_char = None
            continue
        if not in_string:
            if char == "{":
                bracket_count += 1
            elif char == "}":
                bracket_count -= 1
                if bracket_count == 0:
                    json_str = llm_response[start_idx:i+1]
                    try:
                        return json.loads(json_str)
                    except json.JSONDecodeError as e:
                        print(f"[error] Invalid JSON: {e}", file=sys.stderr)
                        raise
    match = re.search(r'\{.*\}', llm_response, re.DOTALL)
    if not match:
        raise ValueError("No valid JSON found in Guardian response.")
    return json.loads(match.group(0))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("intent_node", help="Path to the Intent JSON file (e.g. .lore/nodes/migration.json)")
    parser.add_argument("diff_file", help="Path to the file containing the git diff to analyze")
    args = parser.parse_args()

    # Load Inputs
    intent_path = Path(args.intent_node)
    diff_path = Path(args.diff_file)

    if not intent_path.exists():
        sys.exit(f"[error] Intent Node not found: {intent_path}")
    if not diff_path.exists():
        sys.exit(f"[error] Diff file not found: {diff_path}")

    intent_content = intent_path.read_text(encoding="utf-8")
    diff_content = diff_path.read_text(encoding="utf-8")

    try:
        from core.llm_client import get_llm_client
        client = get_llm_client(Path.cwd())
    except Exception as e:
        sys.exit(f"[error] Failed to initialize LLM client: {e}")

    # Costruisci il prompt utente
    user_prompt = f"""
<INTENT_NODE>
{intent_content}
</INTENT_NODE>

<GIT_DIFF>
{diff_content}
</GIT_DIFF>

Analyze and return the verdict.
"""

    print("[info] Guardian is analyzing the diff against the Semantic Contract...", file=sys.stderr)
    
    try:
        response = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=2000,
            temperature=0.0, # Zero creativity. Maximum precision.
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}]
        )
        raw_text = response.content[0].text
    except Exception as e:
        sys.exit(f"[error] Anthropic API failed: {e}")

    try:
        verdict = extract_json_from_response(raw_text)
    except Exception:
        print(raw_text, file=sys.stderr)
        sys.exit(1)

    decision = verdict.get("decision", "BLOCK")
    
    print("\n" + "="*50)
    print(f" LORE GUARDIAN VERDICT : {decision}")
    print("="*50)
    
    if decision in ["BLOCK", "WARN"]:
        print(f"🛑 Violated Rules: {', '.join(verdict.get('violated_rules', []))}")
        print(f"🧠 Analysis: {verdict.get('analysis')}")
        print("\n📝 Comment for Pull Request:")
        print(verdict.get('developer_comment'))
        print("="*50)
        if decision == "BLOCK":
            sys.exit(1) # Fallisce la build CI/CD
    else:
        print("✅ No semantic violations detected. The PR is safe to merge.")
        print("="*50)
        sys.exit(0)

if __name__ == "__main__":
    main()