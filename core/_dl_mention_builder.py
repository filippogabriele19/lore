# core/_dl_mention_builder.py — M1 mention detection helpers (split from decision_linker.py)
from __future__ import annotations

import os
import ast
import re
import sqlite3
from pathlib import Path
from typing import Optional

_MENTION_RE = re.compile(
    r'(?:'
    r'ADR-\d+'
    r'|decision:\S+'
    r'|DO\s+NOT\s+(?:MODIFY|TOUCH|CALL|DELETE|REMOVE)'
    r'|HOTSPOT'
    r')',
    re.IGNORECASE,
)
_COMMENT_RE = re.compile(r'(?:#|//|\*)\s*(.+)')

_IGNORE_DIRS = {
    '.git', '.lore', '__pycache__', 'venv', '.venv',
    'node_modules', 'backups', 'build', 'dist', '.pytest_cache',
}


def links_from_mentions(conn: sqlite3.Connection, project_root: Path) -> int:
    """M1 — Mention detection (confidence 0.95). Scans source files for ADR/decision refs."""
    conn.execute("DELETE FROM decision_links WHERE source_type = 'mention'")
    count = 0

    # Optimized directory traversal: prune ignored dirs using os.walk (Bug 36)
    for root, dirs, files in os.walk(str(project_root)):
        dirs[:] = [d for d in dirs if d not in _IGNORE_DIRS]
        
        for file in files:
            file_path = Path(root) / file
            if file_path.suffix.lower() not in ('.py', '.ts', '.tsx', '.js', '.jsx', '.go'):
                continue
            
            try:
                source = file_path.read_text(encoding='utf-8', errors='replace')
            except Exception:
                continue

            rel_path = str(file_path.relative_to(project_root)).replace('\\', '/')
            mentions: list[tuple[int, str, str]] = []
            for line_num, line in enumerate(source.splitlines(), start=1):
                m = _MENTION_RE.search(line)
                if not m:
                    continue
                cm = _COMMENT_RE.search(line)
                comment_text = cm.group(1).strip() if cm else line.strip()
                ref = m.group(0).upper().replace('  ', ' ')
                mentions.append((line_num, ref, comment_text))

            if not mentions:
                continue

            if file_path.suffix == '.py':
                count += _insert_python_mentions(conn, source, rel_path, mentions)
            else:
                for _, ref, comment_text in mentions:
                    # Non-python/generic matches: register under file path (symbol_id=NULL)
                    conn.execute(
                        "INSERT OR IGNORE INTO decision_links"
                        " (symbol_name, symbol_id, source_type, source_ref, confidence, description)"
                        " VALUES (?, NULL, 'mention', ?, 0.95, ?)",
                        (rel_path, ref, comment_text),
                    )
                    count += 1

    return count


def _insert_python_mentions(
    conn: sqlite3.Connection,
    source: str,
    rel_path: str,
    mentions: list[tuple[int, str, str]],
) -> int:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        cnt = 0
        for _, ref, text in mentions:
            conn.execute(
                "INSERT OR IGNORE INTO decision_links"
                " (symbol_name, symbol_id, source_type, source_ref, confidence, description)"
                " VALUES (?, NULL, 'mention', ?, 0.95, ?)",
                (rel_path, ref, text),
            )
            cnt += 1
        return cnt

    fn_ranges:    list[tuple[int, int, str]] = []
    class_ranges: list[tuple[int, int, str]] = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        start = node.lineno
        end   = getattr(node, 'end_lineno', node.lineno)
        if hasattr(node, 'decorator_list') and node.decorator_list:
            start = node.decorator_list[0].lineno
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn_ranges.append((start, end, node.name))
        else:
            class_ranges.append((start, end, node.name))

    all_ranges = fn_ranges + class_ranges
    count = 0

    for line_num, ref, comment_text in mentions:
        symbol = _find_next_symbol(line_num, fn_ranges, lookahead=2)
        if not symbol:
            symbol = _find_innermost_symbol(line_num, fn_ranges)
        if not symbol:
            symbol = _find_innermost_symbol(line_num, class_ranges)
        if not symbol:
            symbol = _find_next_symbol(line_num, all_ranges, lookahead=5)
        if not symbol:
            symbol = rel_path

        # Resolve symbol_id if possible
        symbol_id = None
        if symbol != rel_path:
            try:
                row = conn.execute("SELECT id FROM symbols WHERE name = ? LIMIT 1", (symbol,)).fetchone()
                if row:
                    symbol_id = row["id"]
            except Exception:
                pass

        conn.execute(
            "INSERT OR IGNORE INTO decision_links"
            " (symbol_name, symbol_id, source_type, source_ref, confidence, description)"
            " VALUES (?, ?, 'mention', ?, 0.95, ?)",
            (symbol, symbol_id, ref, comment_text),
        )
        count += 1

    return count


def _find_innermost_symbol(
    line: int, node_ranges: list[tuple[int, int, str]]
) -> Optional[str]:
    best_name = None
    best_size = float('inf')
    for start, end, name in node_ranges:
        if start <= line <= end and (end - start) < best_size:
            best_size = end - start
            best_name = name
    return best_name


def _find_next_symbol(
    line: int, node_ranges: list[tuple[int, int, str]], lookahead: int = 5
) -> Optional[str]:
    candidates = [
        (start, name)
        for start, _, name in node_ranges
        if line < start <= line + lookahead
    ]
    return min(candidates, key=lambda x: x[0])[1] if candidates else None
