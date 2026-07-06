"""
core/chat_miner.py — Chat Intent Miner for LORE.
Parses Claude Code history log file, extracts the last active session's messages,
and uses Claude to dynamically generate ADRs / decision links.
"""
from __future__ import annotations

import os
import json
import re
import random
import logging
from pathlib import Path
from datetime import datetime as _dt

logger = logging.getLogger(__name__)

_CHAT_EXTRACTOR_SYSTEM = (
    "Sei il Supervisore Architetturale di LORE. Il tuo compito è analizzare la cronologia "
    "della chat tra uno sviluppatore e un assistente IA per estrarre decisioni di design, "
    "vincoli di conformità, regole di sicurezza e invarianti di codice stabilite.\n"
    "Restituisci ESCLUSIVAMENTE un oggetto JSON valido (non racchiuderlo in blocchi markdown o altro testo) "
    "che rappresenti una lista di regole estratte. Ogni regola deve avere esattamente questa struttura:\n"
    "{\n"
    "  \"rules\": [\n"
    "    {\n"
    "      \"target_file\": \"nome_file_relativo.ext\",\n"
    "      \"symbol_name\": \"nome_funzione_o_classe_o_global\",\n"
    "      \"rule_title\": \"Titolo breve della regola\",\n"
    "      \"rule_description\": \"Spiegazione dettagliata della decisione architetturale o vincolo\"\n"
    "    }\n"
    "  ]\n"
    "}\n"
    "Se non trovi alcuna regola o decisione architetturale rilevante, restituisci {\"rules\": []}."
)

def find_claude_history_file() -> Path | None:
    # Check default global locations
    path = Path.home() / ".claude" / "history.jsonl"
    if path.exists():
        return path
    # Fallback to local directory .claude if any
    local_path = Path(".claude") / "history.jsonl"
    if local_path.exists():
        return local_path
    return None

def read_lines_backwards(file_path: Path, chunk_size=8192):
    """Yields lines from a file starting from the end."""
    try:
        with open(file_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            file_size = f.tell()
            buffer = bytearray()
            pointer = file_size
            
            while pointer > 0:
                to_read = min(chunk_size, pointer)
                pointer -= to_read
                f.seek(pointer)
                chunk = f.read(to_read)
                buffer = chunk + buffer
                
                while b"\n" in buffer:
                    newline_idx = buffer.rfind(b"\n")
                    line = buffer[newline_idx + 1:]
                    buffer = buffer[:newline_idx]
                    yield line.decode("utf-8", errors="replace")
                    
            if buffer:
                yield buffer.decode("utf-8", errors="replace")
    except Exception:
        return

def extract_last_chat_session(history_path: Path, project_path: Path) -> tuple[str | None, list[str]]:
    """
    Reads the history.jsonl file backwards and extracts user messages belonging to
    the most recent session of the target project.
    """
    project_norm = str(project_path.resolve()).lower().replace("\\", "/").rstrip("/")
    line_count = 0
    target_session_id = None
    msgs = []
    
    try:
        for line in read_lines_backwards(history_path):
            line_count += 1
            if line_count > 5000:
                break
                
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
                proj = data.get("project", "")
                if not proj:
                    continue
                proj_norm = str(Path(proj).resolve()).lower().replace("\\", "/").rstrip("/")
                if proj_norm == project_norm:
                    sess_id = data.get("sessionId")
                    msg = data.get("display", "").strip()
                    if sess_id and msg:
                        if target_session_id is None:
                            target_session_id = sess_id
                        if sess_id == target_session_id:
                            msgs.append(msg)
            except Exception:
                continue
    except Exception as e:
        logger.warning(f"Error reading Claude history file backwards: {e}")
        return None, []
        
    if not target_session_id:
        return None, []
        
    msgs.reverse()
    return target_session_id, msgs

def mine_chat_intent(db_path: Path, project_path: Path) -> int:
    history_file = find_claude_history_file()
    if not history_file:
        logger.debug("Claude history file not found")
        return 0
        
    sess_id, msgs = extract_last_chat_session(history_file, project_path)
    if not msgs:
        logger.debug("No recent chat session found for this project")
        return 0
        
    from core.symbol_db import SymbolDB
    db = SymbolDB(db_path)
    try:
        # Check if already processed
        last_processed = None
        row = db.con.execute("SELECT value FROM meta WHERE key='last_processed_chat_session'").fetchone()
        if row:
            last_processed = row["value"]
            
        if last_processed == sess_id:
            logger.debug(f"Chat session {sess_id} already processed. Skipping.")
            return 0
            
        # Call LLM to extract rules using the unified client
        try:
            from core.llm_client import get_llm_client
            client = get_llm_client(project_path)
        except Exception as e:
            logger.warning(f"Could not initialize LLM client: {e}")
            return 0
            
        chat_transcript = "\n".join(f"- User: {m}" for m in msgs)
        
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=_CHAT_EXTRACTOR_SYSTEM,
            messages=[{"role": "user", "content": f"=== CHAT TRANSCRIPT ===\n{chat_transcript}"}],
            timeout=5.0
        )
        
        raw_res = response.content[0].text.strip()
        match = re.search(r"\{.*\}", raw_res, re.DOTALL)
        if not match:
            return 0
            
        extracted_data = json.loads(match.group(0))
        rules = extracted_data.get("rules", [])
        if not rules:
            db.con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('last_processed_chat_session', ?)", (sess_id,))
            db.commit()
            return 0
            
        adr_dir = project_path / ".lore" / "adr"
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
            adr_filename = f"adr_chat_{ts}_{rand_id}.md"
            adr_path = adr_dir / adr_filename
            
            adr_content = f"""# ADR: {title}
            
## Metadata
- **Date**: {_dt.now().strftime("%Y-%m-%d")}
- **Source**: Claude Code Chat Session {sess_id}
- **Target File**: {target_file}
- **Target Symbol**: {symbol_name}
- **Status**: Active

## Context
This rule was dynamically extracted from the developer's chat session where they specified design or security invariants for this module.

## Decision
{description}

## Consequences
Any future edits to {target_file} or {symbol_name} must comply with this design constraint.
"""
            adr_path.write_text(adr_content, encoding="utf-8")
            
            # Register in DB
            db.register_decision_link(
                symbol_name=symbol_name,
                source_type="chat_adr",
                source_ref=f".lore/adr/{adr_filename}",
                confidence=1.0,
                description=title
            )
            rules_created += 1
            
        # Update meta
        db.con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('last_processed_chat_session', ?)", (sess_id,))
        db.commit()
        
        if rules_created > 0:
            print(f"   [CHAT MINER] Extracted {rules_created} new design rules from Claude Code chat!")
        return rules_created
        
    except Exception as e:
        logger.warning(f"Error mining chat intent: {e}")
        return 0
    finally:
        db.close()
