"""
core/_intent_miner.py — Intent Node mining with MacroChange grouping + structural subgraph.

Extracted from git_miner.py to stay under the 300-line module limit.

Entry point: mine_intent_nodes(db_path, git_fn)
  git_fn: callable(*args) -> str — bound to GitMiner._git
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections import defaultdict
from datetime import datetime
from typing import Callable, Dict, List

from core._macro_change import build_history_markdown, group_macro_changes

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# LLM system prompt
# ---------------------------------------------------------------------------

_RECONCILER_SYSTEM = (
    "Sei il Motore Epistemico di LORE. "
    "Il tuo compito è estrarre il VERO Intento di Business (il 'perché'), "
    "riconciliare la sua evoluzione nel tempo, identificare eccezioni e debito tecnico, "
    "e strutturarlo in un Nodo del Knowledge Graph computabile. "
    "Riceverai la git history di un file raggruppata in MacroChange "
    "(commit dello stesso autore entro 48h) e il subgrafo strutturale downstream. "
    "Usa il subgrafo per valutare l'impatto reale di ogni MacroChange: "
    "un file con molti downstream ha blast radius maggiore. "
    "Restituisci ESCLUSIVAMENTE un oggetto JSON valido con queste chiavi: "
    "intent_id, version, type, title, intent_health, canonical_intent, "
    "current_binding, evolution_log, active_exceptions, guard_rules, source. "
    "integrity_score (0.0-1.0) deve riflettere quanto l'intento originale è preservato oggi."
)

REASONING_KEYWORDS = ("because", "decided", "avoid", "never", "warning", "tradeoff")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_json(raw: str) -> dict:
    """Extract the first JSON object from an LLM response string."""
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return {}


def _get_downstream_symbols(
    conn: sqlite3.Connection, file_path: str
) -> List[Dict[str, str]]:
    """
    Returns symbols that file_path's code calls — its structural downstream.

    Uses callee_symbol_id (resolved) first; falls back to callee_name match
    when callee_symbol_id is not populated (partial scan).
    Capped at 30 rows to stay within LLM budget.
    """
    query_resolved = """
        SELECT DISTINCT sc.callee_name, f2.path AS callee_file
        FROM symbol_calls sc
        JOIN symbols s  ON sc.caller_symbol_id = s.id
        JOIN files   f  ON s.file_id = f.id
        JOIN symbols s2 ON sc.callee_symbol_id = s2.id
        JOIN files   f2 ON s2.file_id = f2.id
        WHERE f.path = ?
          AND f2.path != f.path
        LIMIT 30
    """
    query_by_name = """
        SELECT DISTINCT sc.callee_name, f2.path AS callee_file
        FROM symbol_calls sc
        JOIN symbols s  ON sc.caller_symbol_id = s.id
        JOIN files   f  ON s.file_id = f.id
        JOIN symbols s2 ON s2.name = sc.callee_name
        JOIN files   f2 ON f2.id = s2.file_id
        WHERE f.path = ?
          AND f2.path != f.path
        LIMIT 30
    """
    for query in (query_resolved, query_by_name):
        try:
            rows = conn.execute(query, (file_path,)).fetchall()
            if rows:
                return [{"callee_name": r["callee_name"], "callee_file": r["callee_file"]}
                        for r in rows]
        except Exception:
            continue
    return []


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def mine_intent_nodes(db_path: str, git_fn: Callable) -> int:
    """
    For every file with >= 5 commits not yet in intent_nodes:
      1. Query downstream symbols (structural subgraph)
      2. Group commits into MacroChange nodes (same author, 48h window)
      3. Build enriched prompt and call Claude Haiku
      4. Persist intent_node to DB

    Capped at 10 new nodes per run. Idempotent (skip already-stored files).
    Returns number of new nodes written.
    """
    import os
    from pathlib import Path
    try:
        from core.llm_client import get_llm_client
        client = get_llm_client(Path(db_path).parent)
    except Exception as e:
        logger.debug(f"[INTENT] LLM Client not initialized: {e}")
        return 0

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        # Load change frequencies from hotspots cache first (Bug 23)
        freq: Dict[str, int] = {}
        try:
            hotspot_rows = conn.execute("SELECT file_path, change_freq FROM hotspots").fetchall()
            for r in hotspot_rows:
                freq[r["file_path"]] = r["change_freq"]
        except Exception:
            pass

        # If cache is empty, fall back to global git log and populate cache
        if not freq:
            raw = git_fn("log", "--format=", "--name-only", "--no-merges")
            if not raw:
                return 0
            freq_temp = defaultdict(int)
            for line in raw.splitlines():
                line = line.strip()
                if line:
                    freq_temp[line] += 1
            freq = dict(freq_temp)
            try:
                with conn:
                    for fp, cnt in freq.items():
                        conn.execute(
                            "INSERT OR REPLACE INTO hotspots (file_path, change_freq) "
                            "VALUES (?, ?)",
                            (fp, cnt)
                        )
            except Exception:
                pass

        already = {row[0] for row in conn.execute("SELECT file_path FROM intent_nodes")}
        candidates = [
            fp for fp, cnt in sorted(freq.items(), key=lambda x: -x[1])
            if cnt >= 5 and fp not in already and fp.endswith((".py", ".ts", ".js"))
        ]

        nodes_written = 0
        batch = candidates[:10]
        print(f"  [INTENT] {len(batch)} candidates → extracting intent nodes...")
        
        target_model = "claude-haiku-4-5-20251001"
        if hasattr(client, "config") and client.config:
            target_model = client.config.get("llm", {}).get("model") or target_model
            
        for idx, file_path in enumerate(batch, 1):
            print(f"  [INTENT] ({idx}/{len(batch)}) {file_path} ...", flush=True)
            try:
                downstream    = _get_downstream_symbols(conn, file_path)
                macro_changes = group_macro_changes(git_fn, file_path)
                history_md    = build_history_markdown(
                    git_fn, file_path, freq[file_path], macro_changes, downstream
                )
                if not history_md:
                    continue
                
                response = client.messages.create(
                    model=target_model,
                    max_tokens=1024,
                    system=_RECONCILER_SYSTEM,
                    messages=[{"role": "user", "content": history_md[:6000]}],
                )
                intent_node = _extract_json(response.content[0].text)
                if not intent_node:
                    continue

                integrity = float(intent_node.get("integrity_score", 0.7))
                conn.execute(
                    "INSERT OR REPLACE INTO intent_nodes "
                    "(file_path, intent_json, integrity_score, generated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (file_path, json.dumps(intent_node), integrity,
                     datetime.utcnow().isoformat()),
                )
                conn.commit()
                nodes_written += 1
                logger.info("[INTENT] %s (integrity=%.2f)", file_path, integrity)
                print(f"   [INTENT NODE] {file_path} — integrity={integrity:.2f}")

            except Exception as exc:
                logger.debug("[INTENT] %s failed: %s", file_path, exc)

        return nodes_written
    finally:
        conn.close()
