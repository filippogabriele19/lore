"""
core/_cold_start.py — Synthetic Big Bang (Cold Start), L3 layer.

Populates L3 KG layers on repos with absent or thin git history.
L4 intent mining is handled by core._cold_start_intent.

Entry point: run_cold_start(db_path, git_fn, force=False)
"""
from __future__ import annotations

import logging
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List

from core._cold_start_intent import mine_ast_intent_nodes

logger = logging.getLogger(__name__)

COLD_START_THRESHOLD = 3    # fewer commits than this → cold start
_SYNTHETIC_RATE_CAP  = 0.65 # max synthetic co_change_rate
_JACCARD_GATE        = 0.30 # min Jaccard coupling for virtual edge
_CALLERS_GATE        = 2    # min callers to qualify as synthetic hotspot


# ---------------------------------------------------------------------------
# Cold-start detection
# ---------------------------------------------------------------------------

def is_cold_start(git_fn: Callable, threshold: int = COLD_START_THRESHOLD) -> bool:
    """True when the repo has fewer than `threshold` non-merge commits."""
    try:
        raw     = git_fn("log", "--format=%H", "--no-merges")
        commits = [l.strip() for l in raw.splitlines() if l.strip()]
        return len(commits) < threshold
    except Exception:
        return True  # not a git repo → always cold start


# ---------------------------------------------------------------------------
# Schema (additive)
# ---------------------------------------------------------------------------

def _ensure_cold_start_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS cold_start_meta (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            run_at                TEXT NOT NULL,
            files_scanned         INTEGER NOT NULL DEFAULT 0,
            hotspots_created      INTEGER NOT NULL DEFAULT 0,
            virtual_edges_created INTEGER NOT NULL DEFAULT 0,
            intent_nodes_created  INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# L3 — Synthetic hotspots (LOC + structural callers)
# ---------------------------------------------------------------------------

def _synthetic_hotspots(conn: sqlite3.Connection) -> int:
    """
    Compute hotspot proxies from callers count + LOC.
    Skips if hotspots already populated (real git data takes precedence).
    change_freq  = callers_count  (proxy for churn pressure)
    risk_score   = 0.6*norm_callers + 0.4*norm_loc
    Gate: callers >= _CALLERS_GATE and in top-30% by risk_score.
    """
    if conn.execute("SELECT COUNT(*) FROM hotspots").fetchone()[0] > 0:
        return 0

    rows = conn.execute("""
        SELECT f.path,
               COALESCE(f.lines_count, 0)        AS loc,
               COUNT(DISTINCT sc.caller_symbol_id) AS callers
        FROM files f
        LEFT JOIN symbols       s  ON s.file_id          = f.id
        LEFT JOIN symbol_calls  sc ON sc.callee_symbol_id = s.id
        WHERE f.path LIKE '%.py' OR f.path LIKE '%.ts' OR f.path LIKE '%.js'
        GROUP BY f.id
        HAVING callers >= ?
        ORDER BY callers DESC
    """, (_CALLERS_GATE,)).fetchall()

    if not rows:
        return 0

    max_callers = max(r[2] for r in rows) or 1
    max_loc     = max(r[1] for r in rows) or 1

    scored = sorted(
        [(r[0], r[2], r[1], 0.6*(r[2]/max_callers) + 0.4*(r[1]/max_loc))
         for r in rows],
        key=lambda x: -x[3],
    )
    top_n = max(1, int(len(scored) * 0.30))

    inserted = 0
    for path, callers, loc, risk in scored[:top_n]:
        conn.execute(
            "INSERT OR IGNORE INTO hotspots "
            "(file_path, change_freq, complexity_score, risk_score) VALUES (?,?,?,?)",
            (path, callers, round(loc/max_loc, 4), round(risk, 4)),
        )
        inserted += 1
    conn.commit()
    logger.info("[COLD-START] hotspots: %d synthetic rows", inserted)
    return inserted


# ---------------------------------------------------------------------------
# L3 — Synthetic virtual edges (Jaccard on call graph)
# ---------------------------------------------------------------------------

def _synthetic_virtual_edges(conn: sqlite3.Connection) -> int:
    """
    Derive co-change proxies from structural coupling (Jaccard on shared callees).
    Rate is capped at _SYNTHETIC_RATE_CAP to distinguish from real co-change data.
    shared_commits = 0 marks the row as synthetic.
    Skips if virtual_edges already populated.
    """
    if conn.execute("SELECT COUNT(*) FROM virtual_edges").fetchone()[0] > 0:
        return 0

    rows = conn.execute("""
        SELECT DISTINCT fa.path AS src, fb.path AS dst
        FROM symbol_calls sc
        JOIN symbols  sa ON sa.id   = sc.caller_symbol_id
        JOIN files    fa ON fa.id   = sa.file_id
        JOIN symbols  sb ON sb.name = sc.callee_name
        JOIN files    fb ON fb.id   = sb.file_id
        WHERE fa.path != fb.path
    """).fetchall()

    callees: Dict[str, set] = defaultdict(set)
    for src, dst in rows:
        callees[src].add(dst)

    files    = [f for f, c in callees.items() if len(c) >= 2]
    seen: set = set()
    inserted  = 0

    for i, fa in enumerate(files):
        ca = callees[fa]
        for fb in files[i + 1:]:
            key = (fa, fb) if fa < fb else (fb, fa)
            if key in seen:
                continue
            cb     = callees[fb]
            shared = len(ca & cb)
            if shared < 2:
                continue
            jaccard = shared / len(ca | cb)
            if jaccard < _JACCARD_GATE:
                continue
            seen.add(key)
            rate  = round(min(_SYNTHETIC_RATE_CAP, jaccard), 4)
            depth = round(2.0 - rate, 4)
            conn.execute(
                "INSERT OR IGNORE INTO virtual_edges "
                "(src_file, dst_file, co_change_rate, virtual_depth, shared_commits) "
                "VALUES (?, ?, ?, ?, 0)",
                (key[0], key[1], rate, depth),
            )
            inserted += 1

    if inserted:
        conn.commit()
    logger.info("[COLD-START] virtual_edges: %d synthetic rows", inserted)
    return inserted


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_cold_start(
    db_path: str,
    git_fn: Callable,
    force: bool = False,
) -> Dict[str, Any]:
    """
    Synthetic Big Bang: populate L3/L4 without git history.

    No-op when history is sufficient (>= COLD_START_THRESHOLD commits)
    or when cold_start_meta already has a row (idempotent).
    Pass force=True to re-run regardless.

    Returns summary dict with counts of created rows.
    """
    if not is_cold_start(git_fn):
        return {"cold_start_needed": False, "skipped": "history sufficient"}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _ensure_cold_start_schema(conn)

    if not force:
        if conn.execute("SELECT COUNT(*) FROM cold_start_meta").fetchone()[0] > 0:
            conn.close()
            return {"cold_start_needed": True, "skipped": "already ran (use force=True)"}

    print("   [COLD-START] No sufficient git history — synthesising L3/L4...")

    hotspots_n = _synthetic_hotspots(conn)
    virtual_n  = _synthetic_virtual_edges(conn)
    intent_n   = mine_ast_intent_nodes(conn)

    files_n = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    conn.execute(
        "INSERT INTO cold_start_meta "
        "(run_at, files_scanned, hotspots_created, virtual_edges_created, intent_nodes_created) "
        "VALUES (?, ?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), files_n, hotspots_n, virtual_n, intent_n),
    )
    conn.commit()
    conn.close()

    summary = {
        "cold_start_needed":     True,
        "hotspots_created":      hotspots_n,
        "virtual_edges_created": virtual_n,
        "intent_nodes_created":  intent_n,
    }
    logger.info("[COLD-START] complete: %s", summary)
    print(f"   [COLD-START] done — hotspots={hotspots_n}, "
          f"virtual_edges={virtual_n}, intent_nodes={intent_n}")
    return summary
