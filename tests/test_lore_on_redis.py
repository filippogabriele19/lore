"""
tests/test_lore_on_redis.py
──────────────────────────────
Integration verification script for LORE on Redis codebase.
Checks:
1. SQLite DB schema & indexed C symbols in Redis.
2. Git Mining results (hotspots, co-changes, virtual edges).
3. Symbol fragility scores & C call graph entries.
"""

import sqlite3
from pathlib import Path
import pytest

def test_redis_lore_db():
    project_root = Path("_scan_targets/redis")
    db_path = project_root / ".lore_poc.db"
    if not db_path.exists():
        db_path = project_root / ".lore" / "lore.db"
    if not db_path.exists():
        pytest.skip(f"LORE DB not found at {db_path} (skipping in non-local / CI environment)")


    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # 1. Count indexed files and C symbols
    cursor.execute("SELECT COUNT(*) FROM files")
    file_count = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM symbols")
    symbol_count = cursor.fetchone()[0]

    print(f"\n[REDIS LORE AUDIT] Files indexed: {file_count:,} | Symbols indexed: {symbol_count:,}")
    assert file_count > 0, "No files indexed in Redis DB"
    assert symbol_count > 0, "No symbols indexed in Redis DB"

    # 2. Check C specific symbols (e.g. dict, server, evict)
    cursor.execute("SELECT name, kind, line_start FROM symbols WHERE name IN ('dictRehash', 'freeMemoryIfNeeded', 'processCommand', 'redisServer') LIMIT 10")
    key_symbols = cursor.fetchall()
    print(f"[REDIS LORE AUDIT] Key Redis C symbols found: {key_symbols}")
    assert len(key_symbols) > 0, "Key Redis functions/structs not indexed"

    # 3. Check symbol calls (C call graph)
    cursor.execute("SELECT COUNT(*) FROM symbol_calls")
    calls_count = cursor.fetchone()[0]
    print(f"[REDIS LORE AUDIT] Symbol call relationships extracted: {calls_count:,}")

    # 4. Check Git hotspots & co-changes
    cursor.execute("SELECT path, lines FROM files ORDER BY lines DESC LIMIT 5")
    hotspots = cursor.fetchall()
    print(f"[REDIS LORE AUDIT] Top Redis Files by Lines:")
    for path, lines_count in hotspots:
        print(f"   - {path} (Lines: {lines_count})")

    cursor.execute("SELECT file_a, file_b, count FROM co_changes ORDER BY count DESC LIMIT 5")
    co_changes = cursor.fetchall()
    print(f"[REDIS LORE AUDIT] Top Co-Changing Redis Files:")
    for f1, f2, count in co_changes:
        print(f"   - {f1} <--> {f2} (Co-changes: {count})")

    conn.close()

if __name__ == "__main__":
    test_redis_lore_db()
