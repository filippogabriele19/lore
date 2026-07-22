"""
core/git_miner.py — TASK-01
Estrae dal git history: commit reasoning, co-change patterns, hotspots, intent nodes.
Idempotente: skip commit già presenti (by hash).
"""
import sqlite3
import json
import subprocess
import logging
import re
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Optional

logger = logging.getLogger(__name__)


from core._intent_miner import REASONING_KEYWORDS


class GitMiner:
    def __init__(self, project_root: str, db_path: str):
        self.project_root = Path(project_root).resolve()
        self.db_path = Path(db_path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def mine_last_commit(self) -> None:
        """
        Process only the most recent commit (incremental, for post-commit hook).
        Cheap: only updates commit_reasoning — no full co-change/hotspot recompute.
        """
        if not self._is_git_repo():
            return
        conn = sqlite3.connect(self.db_path)
        try:
            self._ensure_schema(conn)
            self._mine_commit_reasoning(conn)  # fast path: only fetches new commits
            conn.commit()
        finally:
            conn.close()

    def run(self) -> dict:
        """
        Estrae dal git history:
        - commit con body > 100 chars e keyword in REASONING_KEYWORDS
        - co-change patterns: coppie file committati insieme > 3 volte in 90 giorni
        - hotspot: file con alta frequenza modifica (top 20% per change_freq)
        Salva risultati in DB. Idempotente: skip commit già presenti (by hash).
        Returns: {'commits_processed': N, 'reasoning_found': M, 'hotspots': K}
        """
        if not self._is_git_repo():
            logger.warning(f"Not a git repo: {self.project_root}")
            return {"commits_processed": 0, "reasoning_found": 0, "hotspots": 0}

        conn = sqlite3.connect(self.db_path)
        try:
            self._ensure_schema(conn)
            commits_processed, reasoning_found = self._mine_commit_reasoning(conn)
            backfilled = self._backfill_diffs(conn)
            if backfilled:
                print(f"  [GIT]  backfilled diffs: {backfilled} commits")
            print(f"  [GIT]  co-change analysis...")
            self._mine_co_changes(conn)
            print(f"  [GIT]  symbol co-change analysis...")
            self._mine_symbol_co_changes(conn)
            print(f"  [GIT]  building virtual edges...")
            ve_count = self._build_virtual_edges(conn)
            print(f"  [GIT]  virtual edges: {ve_count} · computing hotspots...")
            hotspots_count = self._mine_hotspots(conn, force=True)
            conn.commit()
        finally:
            conn.close()

        # Intent nodes open their own connection (long-running LLM calls)
        intent_nodes_count = self._mine_intent_nodes()

        result = {
            "commits_processed": commits_processed,
            "reasoning_found": reasoning_found,
            "hotspots": hotspots_count,
            "intent_nodes": intent_nodes_count,
        }
        print(f"[GIT]   {commits_processed:,} commits · {reasoning_found} with reasoning · {hotspots_count} hotspots")
        logger.info(f"GitMiner complete: {result}")
        return result

    def run_backfill(self) -> None:
        """
        Runs asynchronously in a background thread to map the older history of the project
        (commits before 90 days ago) and save them to SQLite.
        Updates backfill progress in the `meta` table.
        """
        if not self._is_git_repo():
            return

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            self._ensure_schema(conn)
            # Check if already completed
            row_status = conn.execute("SELECT value FROM meta WHERE key = 'git_backfill_status'").fetchone()
            if row_status and row_status["value"] == "completed":
                return

            # Set status to running
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('git_backfill_status', 'running')")
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('git_backfill_progress', ?)",
                         (json.dumps({"processed": 0, "total": 0, "percentage": 0.0}),))
            conn.commit()
        finally:
            conn.close()

        # Extract commits before 90 days ago
        since_date = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
        raw = self._git("log", f"--before={since_date}", "--format=%H|%an|%ai|%B|--END--", "--no-merges")
        if not raw or not raw.strip():
            # Nothing to backfill
            conn = sqlite3.connect(self.db_path)
            try:
                conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('git_backfill_status', 'completed')")
                conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('git_backfill_progress', ?)",
                             (json.dumps({"processed": 0, "total": 0, "percentage": 100.0}),))
                conn.commit()
            finally:
                conn.close()
            return

        commits = self._parse_git_log(raw)
        total_commits = len(commits)
        if total_commits == 0:
            conn = sqlite3.connect(self.db_path)
            try:
                conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('git_backfill_status', 'completed')")
                conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('git_backfill_progress', ?)",
                             (json.dumps({"processed": 0, "total": 0, "percentage": 100.0}),))
                conn.commit()
            finally:
                conn.close()
            return

        # Pre-filter commits in Python using the reasoning keywords to avoid slow git subprocess execution
        matching_commits = []
        for c in commits:
            body = c.get("body", "")
            if len(body) >= 100:
                body_lower = body.lower()
                found_keywords = [kw for kw in REASONING_KEYWORDS if kw in body_lower]
                if found_keywords:
                    c["found_keywords"] = found_keywords
                    matching_commits.append(c)

        total_matching = len(matching_commits)

        # Process matching commits in small batches
        processed = 0
        conn = sqlite3.connect(self.db_path)
        try:
            # Load existing processed hashes to avoid duplicates
            existing_hashes = {
                row[0]
                for row in conn.execute("SELECT commit_hash FROM commit_reasoning")
            }

            for commit in matching_commits:
                h = commit["hash"]
                processed += 1

                # Update progress in DB every 10 commits
                if processed % 10 == 0 or processed == total_matching:
                    pct = (processed / total_matching) * 100 if total_matching > 0 else 100.0
                    conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('git_backfill_progress', ?)",
                                 (json.dumps({"processed": processed, "total": total_matching, "percentage": round(pct, 1)}),))
                    conn.commit()

                if h in existing_hashes:
                    continue

                # Get files touched
                files_raw = self._git("diff-tree", "--no-commit-id", "-r", "--name-only", h)
                files_touched = [f.strip() for f in files_raw.splitlines() if f.strip()]

                # Fetch diff text
                diff_raw = self._git("diff-tree", "--no-commit-id", "-r", "-p",
                                     "--no-color", "--unified=3", h)
                commit_diff = diff_raw[:6000] if diff_raw else None

                conn.execute(
                    """INSERT OR IGNORE INTO commit_reasoning
                       (commit_hash, author, date, body, keywords_found, files_touched, commit_diff)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        h,
                        commit.get("author", ""),
                        commit.get("date", ""),
                        commit.get("body", ""),
                        json.dumps(commit["found_keywords"]),
                        json.dumps(files_touched),
                        commit_diff,
                    ),
                )
                conn.commit()
                
                # Yield CPU to prevent database lockups for UI queries
                import time
                time.sleep(0.01)

            # Recompute co-changes and hotspots once fully populated
            self._mine_co_changes(conn)
            self._build_virtual_edges(conn)
            self._mine_hotspots(conn, force=True)

            # Mark as completed
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('git_backfill_status', 'completed')")
            pct = 100.0
            conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('git_backfill_progress', ?)",
                         (json.dumps({"processed": total_matching, "total": total_matching, "percentage": pct}),))
            conn.commit()
            print(f"[backfill] Background history backfill completed successfully. Mapped {total_matching} commits.")

        except Exception as e:
            logger.error(f"[backfill] Background backfill failed: {e}")
            try:
                conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('git_backfill_status', 'failed')")
                conn.commit()
            except Exception:
                pass
        finally:
            conn.close()

    def run_batch_consolidation(self, window_days: int = 30) -> dict:
        """
        Batch Layer of the Lambda Architecture.

        Re-evaluates integrity_score for all intent_nodes using only commits
        in the last `window_days`. Detects silent weakening (score drop > 20%)
        and stores trend data in intent_node_history.

        Intended to run weekly (or after N commits via cron / CI).
        Safe to call alongside the per-commit Speed Layer (mine_last_commit).

        Returns summary dict: files_evaluated, silent_weakening_detected, alerts.
        """
        if not self._is_git_repo():
            return {"files_evaluated": 0, "silent_weakening_detected": 0, "alerts": []}
        conn = sqlite3.connect(self.db_path)
        try:
            self._ensure_schema(conn)
            conn.commit()
        finally:
            conn.close()
        from core._batch_consolidator import run_batch_consolidation
        result = run_batch_consolidation(str(self.db_path), self._git, window_days)
        logger.info("[BATCH] consolidation complete: %s", result)
        return result

    def run_cold_start(self, force: bool = False) -> dict:
        """
        Synthetic Big Bang: populate L3/L4 KG layers without git history.

        Intended for repos with no commits, meaningless commit messages,
        or as a fast onboarding step before git history accumulates.

        Steps:
          1. Detect if cold start is needed (< 3 non-merge commits)
          2. Synthesise hotspots from LOC + structural callers
          3. Synthesise virtual_edges from Jaccard coupling on call graph
          4. Mine intent_nodes from AST alone (no history section)

        Idempotent — skips if cold_start_meta already populated.
        Pass force=True to re-run regardless.
        Returns summary dict with counts of created rows.
        """
        conn = sqlite3.connect(self.db_path)
        try:
            self._ensure_schema(conn)
            conn.commit()
        finally:
            conn.close()
        from core._cold_start import run_cold_start
        result = run_cold_start(str(self.db_path), self._git, force=force)
        logger.info("[COLD-START] complete: %s", result)
        return result

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _ensure_schema(self, conn: sqlite3.Connection):
        cursor = conn.cursor()

        # Additive migrations
        existing_cols = {
            row[1] for row in cursor.execute("PRAGMA table_info(symbols)")
        }
        if "tags" not in existing_cols:
            cursor.execute("ALTER TABLE symbols ADD COLUMN tags TEXT DEFAULT ''")

        cr_cols = {
            row[1] for row in cursor.execute("PRAGMA table_info(commit_reasoning)")
        }
        if "commit_diff" not in cr_cols and cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='commit_reasoning'"
        ).fetchone():
            cursor.execute("ALTER TABLE commit_reasoning ADD COLUMN commit_diff TEXT")

        cursor.executescript("""
            CREATE TABLE IF NOT EXISTS virtual_edges (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                src_file        TEXT NOT NULL,
                dst_file        TEXT NOT NULL,
                co_change_rate  REAL NOT NULL,
                virtual_depth   REAL NOT NULL,
                shared_commits  INTEGER NOT NULL,
                UNIQUE(src_file, dst_file)
            );

            CREATE TABLE IF NOT EXISTS commit_reasoning (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                commit_hash TEXT UNIQUE,
                author TEXT,
                date TEXT,
                body TEXT,
                keywords_found TEXT,
                files_touched TEXT,
                commit_diff TEXT
            );

            CREATE TABLE IF NOT EXISTS co_changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_a TEXT,
                file_b TEXT,
                count INTEGER DEFAULT 0,
                last_seen TEXT,
                UNIQUE(file_a, file_b)
            );

            CREATE TABLE IF NOT EXISTS symbol_co_changes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol_a TEXT NOT NULL,
                symbol_b TEXT NOT NULL,
                file_a TEXT,
                file_b TEXT,
                shared_commits INTEGER NOT NULL,
                total_commits_a INTEGER NOT NULL,
                confidence REAL NOT NULL,
                last_seen TEXT,
                UNIQUE(symbol_a, symbol_b)
            );

            CREATE TABLE IF NOT EXISTS hotspots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT UNIQUE,
                change_freq INTEGER DEFAULT 0,
                complexity_score REAL DEFAULT 0.0,
                risk_score REAL DEFAULT 0.0
            );

            CREATE TABLE IF NOT EXISTS intent_nodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT NOT NULL UNIQUE,
                intent_json TEXT NOT NULL,
                integrity_score REAL DEFAULT 0.0,
                generated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS intent_node_history (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path       TEXT NOT NULL,
                integrity_score REAL NOT NULL,
                window_days     INTEGER NOT NULL,
                computed_at     TEXT NOT NULL,
                macro_changes   INTEGER NOT NULL DEFAULT 0,
                commit_count    INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS silent_weakening_alerts (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path   TEXT NOT NULL,
                score_before REAL NOT NULL,
                score_after  REAL NOT NULL,
                drop_pct     REAL NOT NULL,
                window_days  INTEGER NOT NULL,
                flagged_at   TEXT NOT NULL,
                acknowledged INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS cold_start_meta (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                run_at                TEXT NOT NULL,
                files_scanned         INTEGER NOT NULL DEFAULT 0,
                hotspots_created      INTEGER NOT NULL DEFAULT 0,
                virtual_edges_created INTEGER NOT NULL DEFAULT 0,
                intent_nodes_created  INTEGER NOT NULL DEFAULT 0
            );
        """)
        conn.commit()

    # ------------------------------------------------------------------
    # Commit reasoning
    # ------------------------------------------------------------------

    def _mine_commit_reasoning(self, conn: sqlite3.Connection) -> tuple[int, int]:
        """Extract commits with meaningful decision rationale."""
        # Load already-processed hashes
        existing_hashes = {
            row[0]
            for row in conn.execute("SELECT commit_hash FROM commit_reasoning")
        }

        # ── FAST PATH: se il DB ha già dati, skippa il git log pesante ──────
        reasoning_already = conn.execute(
            "SELECT COUNT(*) FROM commit_reasoning"
        ).fetchone()[0]
        if reasoning_already > 0:
            # Controlla solo i commit nuovi dall'ultimo hash nel DB
            last_date = conn.execute(
                "SELECT date FROM commit_reasoning ORDER BY date DESC LIMIT 1"
            ).fetchone()
            if last_date:
                raw = self._git(
                    "log", "--format=%H|%an|%ai|%B|--END--", "--no-merges",
                    f"--after={last_date[0][:10]}"
                )
                if not raw or not raw.strip():
                    print(f"[GIT]   up-to-date ({reasoning_already} reasoning entries already in DB)")
                    return reasoning_already, 0
            else:
                since_date = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
                raw = self._git("log", f"--since={since_date}", "--format=%H|%an|%ai|%B|--END--", "--no-merges")
                if not raw or not raw.strip():
                    raw = self._git("log", "-n", "1000", "--format=%H|%an|%ai|%B|--END--", "--no-merges")
        else:
            since_date = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
            raw = self._git("log", f"--since={since_date}", "--format=%H|%an|%ai|%B|--END--", "--no-merges")
            if not raw or not raw.strip():
                raw = self._git("log", "-n", "1000", "--format=%H|%an|%ai|%B|--END--", "--no-merges")
        # ─────────────────────────────────────────────────────────────────────

        if not raw:
            return 0, 0

        commits = self._parse_git_log(raw)
        total = len(commits)
        commits_processed = 0
        reasoning_found = 0

        milestone = max(1, total // 10)  # print at every 10%

        for commit in commits:
            h = commit["hash"]
            if h in existing_hashes:
                continue

            commits_processed += 1
            if commits_processed % milestone == 0 and commits_processed < total:
                pct = commits_processed * 100 // total
                print(f"  [GIT]  {pct}%  ({commits_processed:,}/{total:,} commits · {reasoning_found} with reasoning)", flush=True)
            body = commit.get("body", "")

            if len(body) < 100:
                continue

            body_lower = body.lower()
            found_keywords = [kw for kw in REASONING_KEYWORDS if kw in body_lower]
            if not found_keywords:
                continue

            # Get files touched by this commit
            files_raw = self._git("diff-tree", "--no-commit-id", "-r", "--name-only", h)
            files_touched = [f.strip() for f in files_raw.splitlines() if f.strip()]

            # Fetch diff text (capped at 6000 chars to avoid bloating the DB)
            diff_raw = self._git("diff-tree", "--no-commit-id", "-r", "-p",
                                 "--no-color", "--unified=3", h)
            commit_diff = diff_raw[:6000] if diff_raw else None

            # Increment fragility score for modified symbols on bugfix commits
            if re.search(r'\b(fix(es|ed|ing)?|bug(fix)?|regression)\b', body_lower):
                if diff_raw:
                    current_file = None
                    modified_lines_by_file = defaultdict(list)
                    for d_line in diff_raw.splitlines():
                        if d_line.startswith("+++ b/"):
                            current_file = d_line[6:].strip().replace("\\", "/")
                        elif d_line.startswith("@@ ") and current_file:
                            m = re.search(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", d_line)
                            if m:
                                l_start = int(m.group(1))
                                l_cnt = int(m.group(2)) if m.group(2) else 1
                                l_end = l_start + max(0, l_cnt - 1)
                                modified_lines_by_file[current_file].append((l_start, l_end))
                    
                    for fpath, ranges in modified_lines_by_file.items():
                        f_alt = fpath.replace("/", "\\")
                        for l_start, l_end in ranges:
                            try:
                                conn.execute("""
                                    UPDATE symbols SET fragility_score = fragility_score + 1
                                    WHERE file_id IN (SELECT id FROM files WHERE path = ? OR path = ?)
                                      AND NOT (line_end < ? OR line_start > ?)
                                """, (fpath, f_alt, l_start, l_end))
                            except Exception:
                                pass
            commit_diff = diff_raw[:6000] if diff_raw else None

            conn.execute(
                """INSERT OR IGNORE INTO commit_reasoning
                   (commit_hash, author, date, body, keywords_found, files_touched, commit_diff)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    h,
                    commit.get("author", ""),
                    commit.get("date", ""),
                    body,
                    json.dumps(found_keywords),
                    json.dumps(files_touched),
                    commit_diff,
                ),
            )
            reasoning_found += 1

        return commits_processed, reasoning_found

    def _backfill_diffs(self, conn: sqlite3.Connection) -> int:
        """Populate commit_diff for existing rows that were indexed before this column existed."""
        rows = conn.execute(
            "SELECT commit_hash FROM commit_reasoning WHERE commit_diff IS NULL"
        ).fetchall()
        if not rows:
            return 0
        updated = 0
        for (h,) in rows:
            diff_raw = self._git("diff-tree", "--no-commit-id", "-r", "-p",
                                 "--no-color", "--unified=3", h)
            if not diff_raw:
                continue
            conn.execute(
                "UPDATE commit_reasoning SET commit_diff=? WHERE commit_hash=?",
                (diff_raw[:6000], h),
            )
            updated += 1
        return updated

    # ------------------------------------------------------------------
    # Co-change patterns
    # ------------------------------------------------------------------

    def _mine_co_changes(self, conn: sqlite3.Connection):
        """Find file pairs often committed together."""
        cutoff = (datetime.now() - timedelta(days=2000)).strftime("%Y-%m-%d")
        raw = self._git(
            "log",
            f"--after={cutoff}",
            "--format=%ai",
            "--name-only",
            "--no-merges",
        )
        if not raw or not raw.strip():
            raw = self._git(
                "log",
                "-n", "5000",
                "--format=%ai",
                "--name-only",
                "--no-merges",
            )
        if not raw:
            return

        # Parse into blocks: date + list of files
        blocks = []
        current_date = None
        current_files: list[str] = []

        for line in raw.splitlines():
            line = line.strip()
            if not line:
                if current_files:
                    blocks.append((current_date, current_files))
                    current_date = None
                    current_files = []
                continue
            # Date lines look like: 2024-03-15 12:00:00 +0100
            if len(line) > 18 and line[4] == "-" and line[7] == "-":
                current_date = line[:10]
            else:
                current_files.append(line)

        if current_files:
            blocks.append((current_date, current_files))

        # Count co-occurrences
        pair_counts: dict[tuple, dict] = defaultdict(lambda: {"count": 0, "last_seen": ""})

        for date, files in blocks:
            files = [f for f in files if f.endswith((".ts", ".js", ".py", ".java"))]
            if len(files) > 15:
                # Skip bulk refactoring / reformatting commits to prevent spurius co-changes
                continue
            for i in range(len(files)):
                for j in range(i + 1, len(files)):
                    a, b = sorted([files[i], files[j]])
                    pair_counts[(a, b)]["count"] += 1
                    if date and date > pair_counts[(a, b)]["last_seen"]:
                        pair_counts[(a, b)]["last_seen"] = date

        for (a, b), data in pair_counts.items():
            if data["count"] < 3:
                continue
            conn.execute(
                """INSERT INTO co_changes (file_a, file_b, count, last_seen)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(file_a, file_b) DO UPDATE SET
                     count = excluded.count,
                     last_seen = excluded.last_seen""",
                (a, b, data["count"], data["last_seen"]),
            )

    def _mine_symbol_co_changes(self, conn: sqlite3.Connection):
        """Perform fast association rule mining on symbol co-occurrences using git log -U0 hunk headers."""
        cutoff = (datetime.now() - timedelta(days=2000)).strftime("%Y-%m-%d")
        raw = self._git(
            "log",
            f"--after={cutoff}",
            "-U0",
            "--format=COMMIT|%ai",
            "--no-merges",
        )
        if not raw or raw.count("COMMIT|") < 10:
            raw = self._git(
                "log",
                "-n", "5000",
                "-U0",
                "--format=COMMIT|%ai",
                "--no-merges",
            )
        if not raw:
            return

        symbols_by_file = defaultdict(list)
        rows = conn.execute("""
            SELECT f.path, s.name, s.line_start, s.line_end
            FROM symbols s JOIN files f ON s.file_id = f.id
            WHERE f.path LIKE '%.py'
        """).fetchall()
        for r in rows:
            p_norm = r[0].replace("\\", "/")
            symbols_by_file[p_norm].append((r[1], r[2], r[3]))

        if not symbols_by_file:
            return

        commit_symbol_sets = []
        current_symbols = set()
        current_file = None
        current_date = None

        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            if line.startswith("COMMIT|"):
                if current_symbols:
                    commit_symbol_sets.append((current_date, list(current_symbols)))
                    current_symbols = set()
                current_date = line.split("|", 1)[1][:10] if "|" in line else ""
                current_file = None
                continue

            if line.startswith("+++ b/"):
                current_file = line[6:].strip().replace("\\", "/")
                continue

            if line.startswith("@@ ") and current_file and current_file in symbols_by_file:
                m = re.search(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
                if m:
                    start_line = int(m.group(1))
                    line_count = int(m.group(2)) if m.group(2) else 1
                    end_line = start_line + max(0, line_count - 1)
                    for sym_name, l_start, l_end in symbols_by_file[current_file]:
                        if not (end_line < l_start or start_line > l_end):
                            current_symbols.add((sym_name, current_file))

        if current_symbols:
            commit_symbol_sets.append((current_date, list(current_symbols)))

        symbol_counts = defaultdict(int)
        pair_counts = defaultdict(lambda: {"count": 0, "last_seen": ""})

        for date, sym_list in commit_symbol_sets:
            for sym, fpath in sym_list:
                symbol_counts[sym] += 1

            for i in range(len(sym_list)):
                for j in range(i + 1, len(sym_list)):
                    s1, f1 = sym_list[i]
                    s2, f2 = sym_list[j]
                    if s1 == s2:
                        continue
                    for (src_sym, src_file, dst_sym, dst_file) in [(s1, f1, s2, f2), (s2, f2, s1, f1)]:
                        key = (src_sym, dst_sym, src_file, dst_file)
                        pair_counts[key]["count"] += 1
                        if date and date > pair_counts[key]["last_seen"]:
                            pair_counts[key]["last_seen"] = date

        for (src_sym, dst_sym, src_file, dst_file), data in pair_counts.items():
            shared = data["count"]
            if shared < 3:
                continue
            total_a = symbol_counts.get(src_sym, shared)
            confidence = shared / max(total_a, 1)
            if confidence >= 0.40:
                conn.execute(
                    """INSERT INTO symbol_co_changes (symbol_a, symbol_b, file_a, file_b, shared_commits, total_commits_a, confidence, last_seen)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(symbol_a, symbol_b) DO UPDATE SET
                         shared_commits = excluded.shared_commits,
                         total_commits_a = excluded.total_commits_a,
                         confidence = excluded.confidence,
                         last_seen = excluded.last_seen""",
                    (src_sym, dst_sym, src_file, dst_file, shared, total_a, round(confidence, 4), data["last_seen"]),
                )

    # ------------------------------------------------------------------
    # Virtual edges
    # ------------------------------------------------------------------

    def _build_virtual_edges(self, conn: sqlite3.Connection) -> int:
        """
        Materialise virtual_edges from co_changes.

        Gate: co_change_rate > 0.40 AND shared_commits >= 5.
        Rate = shared_commits / min(commits_a, commits_b) in the 2000-day window
        — filters bootstrap commits and global refactors.
        Returns the number of rows inserted / updated.
        """
        candidates = conn.execute(
            "SELECT file_a, file_b, count FROM co_changes WHERE count >= 5"
        ).fetchall()
        if not candidates:
            return 0

        # Per-file commit counts in the same 2000-day window
        cutoff = (datetime.now() - timedelta(days=2000)).strftime("%Y-%m-%d")
        raw = self._git(
            "log", f"--after={cutoff}", "--format=", "--name-only", "--no-merges"
        )
        if not raw or not raw.strip():
            raw = self._git(
                "log", "-n", "5000", "--format=", "--name-only", "--no-merges"
            )
        file_commit_counts: dict[str, int] = defaultdict(int)
        for line in raw.splitlines():
            line = line.strip()
            if line:
                file_commit_counts[line] += 1

        inserted = 0
        for file_a, file_b, count in candidates:
            commits_a = file_commit_counts.get(file_a, count)
            commits_b = file_commit_counts.get(file_b, count)
            min_commits = max(1, min(commits_a, commits_b))
            co_change_rate = min(1.0, count / min_commits)
            if co_change_rate <= 0.40:
                continue
            virtual_depth = 2.0 - co_change_rate
            conn.execute(
                """INSERT INTO virtual_edges
                       (src_file, dst_file, co_change_rate, virtual_depth, shared_commits)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(src_file, dst_file) DO UPDATE SET
                     co_change_rate = excluded.co_change_rate,
                     virtual_depth  = excluded.virtual_depth,
                     shared_commits = excluded.shared_commits""",
                (file_a, file_b, co_change_rate, virtual_depth, count),
            )
            inserted += 1
        logger.info("virtual_edges: %d rows upserted", inserted)
        return inserted

    # ------------------------------------------------------------------
    # Hotspots
    # ------------------------------------------------------------------

    # Minimum absolute commit count to qualify as a hotspot.
    # Top-20% alone is meaningless in repos with few commits (1 commit = 100% = hotspot).
    # A real hotspot needs at least 5 commits AND be in the top 20%.
    _MIN_HOTSPOT_COMMITS = 5

    def _mine_hotspots(self, conn: sqlite3.Connection, force: bool = False) -> int:
        """
        Flag files that are BOTH in the top 20% by change frequency AND
        have at least _MIN_HOTSPOT_COMMITS absolute commits.
        This prevents labelling stable/untouched files as hotspots in fresh repos.

        Args:
            force: if True, purge stale hotspot rows (change_freq < _MIN_HOTSPOT_COMMITS)
                   before the skip-guard runs. Use once after raising the threshold.
                   Idempotent — safe to call multiple times.
        """
        if force:
            deleted = self._delete_hotspots_below_threshold(conn, self._MIN_HOTSPOT_COMMITS)
            if deleted:
                print(f"   🧹 [HOTSPOTS] Purged {deleted} stale rows (change_freq < {self._MIN_HOTSPOT_COMMITS})")

        hotspots_in_db = conn.execute("SELECT COUNT(*) FROM hotspots").fetchone()[0]
        if hotspots_in_db > 0:
            return hotspots_in_db

        raw = self._git("log", "--format=", "--name-only", "--no-merges")
        if not raw:
            return 0

        freq: dict[str, int] = defaultdict(int)
        for line in raw.splitlines():
            line = line.strip()
            if line:
                freq[line] += 1

        if not freq:
            return 0

        # Only consider files that meet the absolute minimum
        eligible = {p: c for p, c in freq.items() if c >= self._MIN_HOTSPOT_COMMITS}
        if not eligible:
            logger.info(
                f"No files meet minimum {self._MIN_HOTSPOT_COMMITS} commits for hotspot threshold "
                f"(max in repo: {max(freq.values())})"
            )
            return 0

        counts = sorted(eligible.values(), reverse=True)
        threshold_idx = max(0, int(len(counts) * 0.2) - 1)
        threshold = counts[threshold_idx]

        hotspots_written = 0
        for file_path, change_freq in eligible.items():
            if change_freq < threshold:
                continue
            risk_score = min(1.0, change_freq / max(counts[0], 1))
            conn.execute(
                """INSERT INTO hotspots (file_path, change_freq, risk_score)
                   VALUES (?, ?, ?)
                   ON CONFLICT(file_path) DO UPDATE SET
                     change_freq = excluded.change_freq,
                     risk_score = excluded.risk_score""",
                (file_path, change_freq, risk_score),
            )
            hotspots_written += 1

        return hotspots_written

    def _delete_hotspots_below_threshold(self, conn: sqlite3.Connection, min_commits: int) -> int:
        """
        Delete hotspot rows whose change_freq < min_commits.
        Idempotent: safe to call multiple times — rows at or above threshold are untouched.
        Returns the number of deleted rows.
        """
        cursor = conn.execute(
            "DELETE FROM hotspots WHERE change_freq < ?",
            (min_commits,)
        )
        return cursor.rowcount

    # ------------------------------------------------------------------
    # Intent Nodes (L4 KG) — delegated to core._intent_miner
    # ------------------------------------------------------------------

    def _mine_intent_nodes(self) -> int:
        """
        MacroChange grouping + structural subgraph + Claude Haiku intent extraction.
        See core/_intent_miner.py for the full implementation.
        """
        import os
        if os.environ.get("LORE_SKIP_INTENT_MINING") == "1":
            print("  [GIT]  skipping intent nodes extraction (LORE_SKIP_INTENT_MINING=1)")
            return 0
        from core._intent_miner import mine_intent_nodes
        print(f"  [GIT]  extracting intent nodes (LLM)...")
        return mine_intent_nodes(str(self.db_path), self._git)

    # ------------------------------------------------------------------
    # Git helpers
    # ------------------------------------------------------------------

    def _is_git_repo(self) -> bool:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(self.project_root),
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    def _git(self, *args) -> str:
        result = subprocess.run(
            ["git"] + list(args),
            cwd=str(self.project_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            logger.debug(f"git {args[0]} failed: {result.stderr[:200]}")
            return ""
        return result.stdout

    def _parse_git_log(self, raw: str) -> list[dict]:
        """Parse git log --format=%H|%an|%ai|%B|--END-- output."""
        commits = []
        current: Optional[dict] = None

        for line in raw.splitlines():
            if "|" in line and len(line.split("|")) >= 3:
                parts = line.split("|", 3)
                if len(parts[0]) == 40:  # SHA hash
                    if current:
                        commits.append(current)
                    current = {
                        "hash": parts[0],
                        "author": parts[1] if len(parts) > 1 else "",
                        "date": parts[2] if len(parts) > 2 else "",
                        "body": parts[3] if len(parts) > 3 else "",
                    }
                    continue

            if current and line.strip() == "--END--":
                commits.append(current)
                current = None
                continue

            if current:
                current["body"] = current.get("body", "") + "\n" + line

        if current:
            commits.append(current)

        return commits