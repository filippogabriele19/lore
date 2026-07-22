"""
core/_cold_start_intent.py — Synthetic L4 intent mining (AST-only).

Called by core._cold_start.run_cold_start() to populate intent_nodes
for the top-N largest files using AST structure alone (no git history).

The LLM is instructed to infer design intent from naming, parameters,
call patterns, and docstrings.  source is always 'cold_start_ast'.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List

logger = logging.getLogger(__name__)

_TOP_FILES = 10   # max intent nodes to mine per cold-start run

_COLD_START_SYSTEM = (
    "You are the Cold Start Analyzer of LORE — no git history available. "
    "You will receive the AST structure of a file: symbols, signatures, docstrings, "
    "and the downstream subgraph. "
    "Infer design intent exclusively from the code: naming, parameters, "
    "dependencies, visible patterns, docstrings. "
    "Return EXCLUSIVELY a JSON object with: "
    "intent_id, version, type, title, intent_health, canonical_intent, "
    "current_binding, evolution_log (empty list), "
    "active_exceptions, guard_rules, source (ALWAYS 'cold_start_ast'). "
    "integrity_score (0.0-1.0): perceived structural coherence."
)


def _build_prompt(conn: sqlite3.Connection, file_path: str) -> str:
    """Build the AST-only intent prompt for a single file."""
    meta = conn.execute(
        "SELECT lines_count, type FROM files WHERE path = ?", (file_path,)
    ).fetchone()
    loc  = meta[0] if meta else 0
    lang = meta[1] if meta else "unknown"

    syms = conn.execute("""
        SELECT name, kind, COALESCE(NULLIF(summary,''), docstring, '') AS txt
        FROM symbols
        WHERE file_id = (SELECT id FROM files WHERE path = ?)
          AND kind IN ('function','method','class')
        ORDER BY kind, name
        LIMIT 20
    """, (file_path,)).fetchall()

    # Try using resolved callee_symbol_id first (Bug 16)
    deps = []
    try:
        deps = conn.execute("""
            SELECT DISTINCT sb.name, fb.path
            FROM symbol_calls sc
            JOIN symbols  sa ON sa.id   = sc.caller_symbol_id
            JOIN files    fa ON fa.id   = sa.file_id
            JOIN symbols  sb ON sb.id   = sc.callee_symbol_id
            JOIN files    fb ON fb.id   = sb.file_id
            WHERE fa.path = ? AND fb.path != fa.path
            LIMIT 15
        """, (file_path,)).fetchall()
    except sqlite3.OperationalError:
        pass

    if not deps:
        # Fallback to name-only matching
        deps = conn.execute("""
            SELECT DISTINCT sb.name, fb.path
            FROM symbol_calls sc
            JOIN symbols  sa ON sa.id   = sc.caller_symbol_id
            JOIN files    fa ON fa.id   = sa.file_id
            JOIN symbols  sb ON sb.name = sc.callee_name
            JOIN files    fb ON fb.id   = sb.file_id
            WHERE fa.path = ? AND fb.path != fa.path
            LIMIT 15
        """, (file_path,)).fetchall()

    lines = [f"## Target File: `{file_path}` ({lang}, {loc} LOC)", "", "## Symbols", ""]
    for name, kind, txt in syms:
        lines.append(f"- `{name}` ({kind})" + (f": {txt[:120]}" if txt else ""))

    if deps:
        lines += ["", "## Downstream Dependencies", ""]
        by_file: Dict[str, List[str]] = defaultdict(list)
        for name, fp in deps:
            by_file[fp].append(name)
        for fp, names in sorted(by_file.items()):
            lines.append(f"- `{fp}`: {', '.join(names[:6])}")

    lines += ["", "## Task",
              "Infer the design intent from the code structure. No git history available."]
    return "\n".join(lines)


def mine_ast_intent_nodes(conn: sqlite3.Connection) -> int:
    """
    Generate synthetic intent_nodes for the top-_TOP_FILES largest files.

    Skips files already in intent_nodes.
    Returns number of new nodes written.
    Degrades gracefully when ANTHROPIC_API_KEY is absent or SDK is missing.
    """
    import os
    from pathlib import Path
    try:
        cursor = conn.cursor()
        db_path_str = cursor.execute("PRAGMA database_list").fetchall()[0][2]
        project_root = Path(db_path_str).parent
    except Exception:
        project_root = Path.cwd()
        
    try:
        from core.llm_client import get_llm_client
        client = get_llm_client(project_root)
        from core._intent_miner import _extract_json
    except Exception as e:
        logger.debug(f"[COLD-START-INTENT] LLM client init failed: {e}")
        return 0

    already = {r[0] for r in conn.execute("SELECT file_path FROM intent_nodes")}
    candidates = conn.execute("""
        SELECT path FROM files
        WHERE (path LIKE '%.py' OR path LIKE '%.ts' OR path LIKE '%.js')
          AND lines_count > 20
        ORDER BY lines_count DESC
        LIMIT ?
    """, (_TOP_FILES,)).fetchall()

    written = 0
    for (file_path,) in candidates:
        if file_path in already:
            continue
        try:
            prompt = _build_prompt(conn, file_path)
            if not prompt:
                continue
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=768,
                system=_COLD_START_SYSTEM,
                messages=[{"role": "user", "content": prompt[:5000]}],
            )
            node = _extract_json(resp.content[0].text)
            if not node:
                continue
            node["source"] = "cold_start_ast"
            integrity = float(node.get("integrity_score", 0.6))
            conn.execute(
                "INSERT OR REPLACE INTO intent_nodes "
                "(file_path, intent_json, integrity_score, generated_at) VALUES (?,?,?,?)",
                (file_path, json.dumps(node), integrity,
                 datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
            written += 1
            print(f"   [COLD-START] intent node: {file_path} (integrity={integrity:.2f})")
        except Exception as exc:
            logger.debug("[COLD-START-INTENT] %s: %s", file_path, exc)

    return written
