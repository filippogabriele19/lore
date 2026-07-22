import sqlite3
from pathlib import Path
from typing import Optional
from core.symbol_types import SymbolInfo

# ---------------------------------------------------------------------------
# Schema DB
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS files (
    id    INTEGER PRIMARY KEY,
    path  TEXT UNIQUE NOT NULL,
    lines INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS symbols (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    file_id      INTEGER NOT NULL,
    line_start   INTEGER NOT NULL,
    line_end     INTEGER NOT NULL,
    kind         TEXT NOT NULL,   -- function | class | method | variable
    signature    TEXT,            -- prima riga (def/class/assign)
    parent_class TEXT,            -- se è un metodo
    is_source    INTEGER DEFAULT 0,
    FOREIGN KEY (file_id) REFERENCES files(id)
);

CREATE TABLE IF NOT EXISTS deps (
    id              INTEGER PRIMARY KEY,
    from_symbol_id  INTEGER,         -- NULL = dipendenza a livello di modulo
    from_file_id    INTEGER NOT NULL,
    to_name         TEXT NOT NULL,
    dep_type        TEXT NOT NULL,   -- call | read_global | write_global | import
    line            INTEGER
);

CREATE TABLE IF NOT EXISTS decision_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_name TEXT,
    symbol_id INTEGER,
    source_type TEXT,
    source_ref TEXT,
    confidence REAL,
    description TEXT,
    mechanism TEXT,
    constraint_text TEXT,
    warning INTEGER DEFAULT 0,
    embedding BLOB,
    FOREIGN KEY (symbol_id) REFERENCES symbols(id) ON DELETE CASCADE
);



CREATE TABLE IF NOT EXISTS hotspots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT UNIQUE, 
    change_freq INTEGER DEFAULT 0,
    complexity_score REAL DEFAULT 0.0, 
    risk_score REAL DEFAULT 0.0
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

CREATE TABLE IF NOT EXISTS historical_vulns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source_symbol   TEXT,
    sink_symbol     TEXT,
    path_fingerprint TEXT UNIQUE NOT NULL,
    cured_at        TEXT DEFAULT CURRENT_TIMESTAMP,
    description     TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS feedback (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    finding_type    TEXT NOT NULL,
    file_path       TEXT NOT NULL,
    symbol_name     TEXT DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'dismissed',
    reason          TEXT DEFAULT '',
    created_at      TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(finding_type, file_path, symbol_name)
);

CREATE TABLE IF NOT EXISTS virtual_edges (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    src_file        TEXT NOT NULL,
    dst_file        TEXT NOT NULL,
    co_change_rate  REAL NOT NULL,
    virtual_depth   REAL NOT NULL,
    shared_commits  INTEGER NOT NULL,
    UNIQUE(src_file, dst_file)
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


CREATE VIEW IF NOT EXISTS symbol_calls AS
SELECT 
    d.from_symbol_id AS caller_symbol_id,
    s.id AS callee_symbol_id,
    d.to_name AS callee_name,
    d.line AS call_line
FROM deps d
LEFT JOIN symbols s ON d.to_name = s.name
WHERE d.dep_type = 'call';

CREATE INDEX IF NOT EXISTS idx_symbols_name     ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_file     ON symbols(file_id);
CREATE INDEX IF NOT EXISTS idx_deps_from_symbol ON deps(from_symbol_id);
CREATE INDEX IF NOT EXISTS idx_deps_to_name     ON deps(to_name);
CREATE INDEX IF NOT EXISTS idx_historical_vulns_fingerprint ON historical_vulns(path_fingerprint);

CREATE VIRTUAL TABLE IF NOT EXISTS vec_symbols USING vec0(
    rowid INTEGER PRIMARY KEY,
    embedding float[384]
);

CREATE VIRTUAL TABLE IF NOT EXISTS vec_decision_links USING vec0(
    rowid INTEGER PRIMARY KEY,
    embedding float[384]
);

CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5(
    name, kind, signature, body_preview, file_path,
    tokenize='porter unicode61'
);
"""

class SymbolDB:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.con = sqlite3.connect(str(db_path), check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        try:
            self.con.enable_load_extension(True)
            import sqlite_vec
            sqlite_vec.load(self.con)
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f"Failed to load sqlite-vec extension: {e}")
        try:
            self.con.execute("PRAGMA journal_mode=WAL;")
        except Exception:
            pass
        self.con.execute("PRAGMA foreign_keys = ON;")
        self.con.executescript(SCHEMA)
        self._apply_migrations()
        self._sync_vector_tables()
        self.con.commit()

        self.con.commit()

    def close(self):
        try:
            self.con.close()
        except Exception:
            pass

    def _apply_migrations(self):
        """Additive migrations — never rename/drop columns."""
        try:
            self.con.execute("ALTER TABLE symbols ADD COLUMN embedding BLOB")
            self.con.commit()
        except sqlite3.OperationalError:
            pass  # column already exists
        for col_def in [
            "ALTER TABLE decision_links ADD COLUMN symbol_id INTEGER REFERENCES symbols(id) ON DELETE CASCADE",
            "ALTER TABLE decision_links ADD COLUMN mechanism TEXT",
            "ALTER TABLE decision_links ADD COLUMN constraint_text TEXT",
            "ALTER TABLE decision_links ADD COLUMN warning INTEGER DEFAULT 0",
            "ALTER TABLE decision_links ADD COLUMN embedding BLOB",
        ]:
            try:
                self.con.execute(col_def)
                self.con.commit()
            except sqlite3.OperationalError:
                pass
        try:
            self.con.execute("ALTER TABLE symbols ADD COLUMN is_source INTEGER DEFAULT 0")
            self.con.commit()
        except sqlite3.OperationalError:
            pass
        try:
            self.con.execute("""
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
                )
            """)
            self.con.commit()
        except sqlite3.OperationalError:
            pass
        try:
            self.con.execute("ALTER TABLE symbols ADD COLUMN role TEXT DEFAULT 'source'")
            self.con.commit()
        except sqlite3.OperationalError:
            pass
        try:
            self.con.execute("ALTER TABLE symbols ADD COLUMN fragility_score INTEGER DEFAULT 0")
            self.con.commit()
        except sqlite3.OperationalError:
            pass
        try:
            self.con.execute("ALTER TABLE decision_links ADD COLUMN file_path TEXT DEFAULT ''")
            self.con.execute("ALTER TABLE decision_links ADD COLUMN symbol_id INTEGER DEFAULT NULL")
            self.con.commit()
        except sqlite3.OperationalError:
            pass
        try:
            self.con.execute("CREATE INDEX IF NOT EXISTS idx_decision_links_symbol ON decision_links(symbol_id)")
            self.con.execute("CREATE INDEX IF NOT EXISTS idx_decision_links_name ON decision_links(symbol_name)")
            self.con.execute("CREATE INDEX IF NOT EXISTS idx_decision_links_file_name ON decision_links(file_path, symbol_name)")
            self.con.commit()
        except sqlite3.OperationalError:
            pass
        try:
            self.con.execute("""
                CREATE TABLE IF NOT EXISTS decision_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol_name TEXT, file_path TEXT, symbol_id INTEGER,
                    source_type TEXT, source_ref TEXT,
                    confidence REAL, description TEXT
                )
            """)
            self.con.execute("""
                CREATE TABLE IF NOT EXISTS hotspots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_path TEXT UNIQUE, change_freq INTEGER DEFAULT 0,
                    complexity_score REAL DEFAULT 0.0, risk_score REAL DEFAULT 0.0
                )
            """)
            self.con.execute("""
                CREATE TABLE IF NOT EXISTS commit_reasoning (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    commit_hash TEXT UNIQUE, author TEXT, date TEXT,
                    body TEXT, keywords_found TEXT, files_touched TEXT,
                    commit_diff TEXT
                )
            """)
            self.con.execute("""
                CREATE TABLE IF NOT EXISTS historical_vulns (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_symbol   TEXT,
                    sink_symbol     TEXT,
                    path_fingerprint TEXT UNIQUE NOT NULL,
                    cured_at        TEXT DEFAULT CURRENT_TIMESTAMP,
                    description     TEXT
                )
            """)
            self.con.execute("CREATE INDEX IF NOT EXISTS idx_historical_vulns_fingerprint ON historical_vulns(path_fingerprint)")
            self.con.execute("""
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY, value TEXT
                )
            """)
            self.con.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5(
                    name, kind, signature, body_preview, file_path,
                    tokenize='porter unicode61'
                )
            """)
            self.con.commit()
        except sqlite3.OperationalError:
            pass

    # ── Embedding storage ──────────────────────────────────────────────────────

    def store_embedding(self, symbol_id: int, vec_bytes: bytes) -> None:
        self.con.execute(
            "UPDATE symbols SET embedding=? WHERE id=?", (vec_bytes, symbol_id)
        )
        try:
            self.con.execute(
                "INSERT OR REPLACE INTO vec_symbols(rowid, embedding) VALUES(?, ?)",
                (symbol_id, vec_bytes)
            )
        except Exception:
            pass


    def get_embedding(self, name: str) -> Optional[bytes]:
        row = self.con.execute(
            "SELECT embedding FROM symbols "
            "WHERE name=? AND embedding IS NOT NULL LIMIT 1",
            (name,),
        ).fetchone()
        return row[0] if row else None

    def symbols_needing_embedding(self) -> list:
        """Returns rows (id, name, kind, signature, path, line_start, line_end)."""
        return self.con.execute(
            "SELECT s.id, s.name, s.kind, s.signature, f.path, "
            "       s.line_start, s.line_end "
            "FROM symbols s JOIN files f ON s.file_id=f.id "
            "WHERE s.embedding IS NULL "
            "  AND s.kind IN ('function', 'class', 'method', 'variable')",
        ).fetchall()

    def all_embeddings(self) -> list:
        """Returns (name, embedding_bytes) for all embedded symbols."""
        return self.con.execute(
            "SELECT name, embedding FROM symbols WHERE embedding IS NOT NULL"
        ).fetchall()

    def all_embeddings_with_role(self) -> list:
        """Returns (name, embedding_bytes, role, file_path, kind) for all embedded symbols."""
        return self.con.execute(
            "SELECT s.name, s.embedding, s.role, f.path, s.kind "
            "FROM symbols s JOIN files f ON s.file_id = f.id "
            "WHERE s.embedding IS NOT NULL"
        ).fetchall()

    def get_file_imports(self, file_id: int) -> list[str]:
        """Returns list of imported module names for a given file."""
        rows = self.con.execute(
            "SELECT to_name FROM deps WHERE from_file_id = ? AND dep_type = 'import'",
            (file_id,)
        ).fetchall()
        return [r[0] for r in rows]

    def upsert_file(self, path: str, lines: int) -> int:
        cur = self.con.execute(
            "INSERT INTO files(path, lines) VALUES(?,?) "
            "ON CONFLICT(path) DO UPDATE SET lines=excluded.lines",
            (path, lines)
        )
        row = self.con.execute("SELECT id FROM files WHERE path=?", (path,)).fetchone()
        return row["id"]

    def clear_file(self, file_id: int):
        try:
            # Delete from FTS5 index by rowid
            rows = self.con.execute("SELECT id FROM symbols WHERE file_id=?", (file_id,)).fetchall()
            for r in rows:
                self.con.execute("DELETE FROM symbols_fts WHERE rowid=?", (r["id"],))
        except Exception:
            pass
        self.con.execute("DELETE FROM symbols WHERE file_id=?", (file_id,))
        self.con.execute("DELETE FROM deps WHERE from_file_id=?", (file_id,))
        
    def clear_fts(self):
        """Clear the FTS5 index (used during rescan)."""
        try:
            self.con.execute("DELETE FROM symbols_fts")
        except Exception:
            pass

    def delete_file_by_path(self, rel_path: str) -> None:
        """Remove a file and all its symbols/deps from the index (file deleted on disk)."""
        row = self.con.execute("SELECT id FROM files WHERE path=?", (rel_path,)).fetchone()
        if row:
            self.clear_file(row["id"])
            self.con.execute("DELETE FROM files WHERE id=?", (row["id"],))

    def insert_symbol(self, file_id: int, sym: SymbolInfo) -> int:
        cur = self.con.execute(
            "INSERT INTO symbols(name, file_id, line_start, line_end, kind, signature, parent_class, is_source, role) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (sym.name, file_id, sym.line_start, sym.line_end,
             sym.kind, sym.signature, sym.parent_class, sym.is_source, sym.role)
        )
        return cur.lastrowid

    def insert_fts(self, rowid: int, name: str, kind: str, signature: str, body_preview: str, file_path: str):
        """Insert a symbol into the FTS5 index for keyword search."""
        self.con.execute(
            "INSERT INTO symbols_fts(rowid, name, kind, signature, body_preview, file_path) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (rowid, name, kind, signature or "", body_preview or "", file_path or "")
        )

    def search_fts(self, query: str, limit: int = 20) -> list:
        """BM25 keyword search over symbols. Returns [(symbol_id, rank, name, file_path)]."""
        import re
        safe_query = re.sub(r'[^\w\s]', ' ', query).strip()
        if not safe_query:
            return []
        terms = safe_query.split()
        fts_query = " OR ".join(terms)
        try:
            return self.con.execute(
                "SELECT rowid, rank, name, file_path "
                "FROM symbols_fts WHERE symbols_fts MATCH ? "
                "ORDER BY rank LIMIT ?",
                (fts_query, limit)
            ).fetchall()
        except Exception:
            return []

    def insert_deps(self, sym_id: int, file_id: int, sym: SymbolInfo):
        rows = []
        for name in sym.calls:
            rows.append((sym_id, file_id, name, "call", sym.line_start))
        for name in sym.reads_global:
            rows.append((sym_id, file_id, name, "read_global", sym.line_start))
        for name in sym.writes_global:
            rows.append((sym_id, file_id, name, "write_global", sym.line_start))
        self.con.executemany(
            "INSERT INTO deps(from_symbol_id, from_file_id, to_name, dep_type, line) VALUES(?,?,?,?,?)",
            rows
        )

    def insert_imports(self, file_id: int, imports: list[tuple]):
        rows = [(None, file_id, name, "import", line) for name, module, line in imports]
        self.con.executemany(
            "INSERT INTO deps(from_symbol_id, from_file_id, to_name, dep_type, line) VALUES(?,?,?,?,?)",
            rows
        )

    def commit(self):
        self.con.commit()

    # ── Centalized SQLite Helpers ──────────────────────────────────────────────

    def get_stats(self) -> dict:
        stats = {"symbols": 0, "decision_links": 0, "hotspots": 0, "taint_paths": 0}
        try:
            stats["symbols"] = self.con.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
            tables = {r[0] for r in self.con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "decision_links" in tables:
                stats["decision_links"] = self.con.execute("SELECT COUNT(*) FROM decision_links").fetchone()[0]
            if "hotspots" in tables:
                stats["hotspots"] = self.con.execute("SELECT COUNT(*) FROM hotspots WHERE risk_score > 0.5").fetchone()[0]
        except sqlite3.OperationalError as e:
            import logging
            logging.getLogger(__name__).warning(f"Error getting stats from DB: {e}")
        return stats

    def find_relevant_decisions(self, task_description: str, max_results: int = 5) -> list:
        try:
            from cli.agent_retrieval import _make_embed_fn
            embed_fn = _make_embed_fn()
            if embed_fn:
                # Dynamic on-the-fly sync of any decision links missing in the virtual table
                try:
                    unsynced = self.con.execute("""
                        SELECT id, symbol_name, description 
                        FROM decision_links 
                        WHERE id NOT IN (SELECT rowid FROM vec_decision_links)
                    """).fetchall()
                    for r in unsynced:
                        vec = embed_fn(f"{r['symbol_name']} {r['description']}")
                        import struct
                        vec_bytes = struct.pack(f"<{len(vec)}f", *vec)
                        self.con.execute("UPDATE decision_links SET embedding=? WHERE id=?", (vec_bytes, r["id"]))
                        self.con.execute("INSERT OR REPLACE INTO vec_decision_links(rowid, embedding) VALUES(?, ?)", (r["id"], vec_bytes))
                    if unsynced:
                        self.con.commit()
                except Exception:
                    pass

                task_vec = embed_fn(task_description)
                import struct
                task_vec_bytes = struct.pack(f"<{len(task_vec)}f", *task_vec)
                
                rows = self.con.execute("""
                    SELECT 
                        dl.symbol_name, 
                        dl.source_type, 
                        dl.source_ref, 
                        dl.confidence, 
                        dl.description,
                        v.distance
                    FROM vec_decision_links v
                    JOIN decision_links dl ON v.rowid = dl.id
                    WHERE v.embedding MATCH ? AND k = ?
                """, (task_vec_bytes, max_results)).fetchall()
                
                results = []
                for r in rows:
                    l2_dist = r["distance"]
                    sim = 1.0 - (l2_dist * l2_dist) / 2.0
                    if sim > 0.35:
                        d = dict(r)
                        d["confidence"] = sim
                        results.append(d)
                if results:
                    return results

        except Exception as e:
            import logging
            logging.getLogger(__name__).debug(f"sqlite-vec search failed, falling back to Jaccard: {e}")
            
        try:
            rows = self.con.execute(
                "SELECT symbol_name, source_type, source_ref, confidence, description FROM decision_links"
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        if not rows:
            return []
            
        task_words = set(task_description.lower().replace("_", " ").split()) - {
            "the", "and", "for", "all", "add", "with", "that", "this", "from", "are", "was", "to", "in", "of"
        }
        scored = []
        for r in rows:
            doc_words = (set(r["symbol_name"].lower().replace("_", " ").split()) | set(r["description"].lower().split())) - {
                "the", "and", "for", "all", "add", "with", "that", "this", "from", "are", "was", "to", "in", "of"
            }
            overlap = task_words & doc_words
            if overlap:
                score = len(overlap) / max(len(task_words), 1)
                if score > 0.15:
                    scored.append((score, r))
        scored.sort(key=lambda x: -x[0])
        return [dict(r) for _, r in scored[:max_results]]

    def get_decision_links_for_file(self, fpath: str) -> list:
        try:
            return self.con.execute("""
                SELECT dl.symbol_name, dl.source_ref, dl.confidence, dl.description 
                FROM decision_links dl
                JOIN symbols s ON (dl.symbol_id IS NOT NULL AND dl.symbol_id = s.id) OR (dl.symbol_id IS NULL AND dl.symbol_name = s.name)
                JOIN files f ON s.file_id = f.id
                WHERE f.path = ? OR f.path = ?
            """, (fpath, fpath.replace("/", "\\"))).fetchall()
        except sqlite3.OperationalError:
            return []

    def register_decision_link(self, symbol_name: str, source_type: str, source_ref: str, confidence: float, description: str) -> None:
        symbol_id = None
        try:
            row = self.con.execute("SELECT id FROM symbols WHERE name = ? LIMIT 1", (symbol_name,)).fetchone()
            if row:
                symbol_id = row["id"]
        except Exception:
            pass

        vec_bytes = None
        try:
            from cli.agent_retrieval import _make_embed_fn
            embed_fn = _make_embed_fn()
            if embed_fn:
                vec = embed_fn(f"{symbol_name} {description}")
                import struct
                vec_bytes = struct.pack(f"<{len(vec)}f", *vec)
        except Exception:
            pass

        cur = self.con.execute(
            "INSERT INTO decision_links (symbol_name, symbol_id, source_type, source_ref, confidence, description, embedding) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (symbol_name, symbol_id, source_type, source_ref, confidence, description, vec_bytes)
        )
        row_id = cur.lastrowid
        
        if vec_bytes:
            try:
                self.con.execute(
                    "INSERT OR REPLACE INTO vec_decision_links(rowid, embedding) VALUES(?, ?)",
                    (row_id, vec_bytes)
                )
            except Exception:
                pass

    def _sync_vector_tables(self):
        try:
            rows = self.con.execute("SELECT id, embedding FROM decision_links WHERE embedding IS NOT NULL").fetchall()
            for r in rows:
                self.con.execute(
                    "INSERT OR IGNORE INTO vec_decision_links(rowid, embedding) VALUES(?, ?)",
                    (r["id"], r["embedding"])
                )
            
            rows_sym = self.con.execute("SELECT id, embedding FROM symbols WHERE embedding IS NOT NULL").fetchall()
            for r in rows_sym:
                self.con.execute(
                    "INSERT OR IGNORE INTO vec_symbols(rowid, embedding) VALUES(?, ?)",
                    (r["id"], r["embedding"])
                )
        except Exception:
            pass


    def check_regression(self, path_hash: str) -> bool:
        try:
            row = self.con.execute(
                "SELECT id FROM historical_vulns WHERE path_fingerprint = ?", (path_hash,)
            ).fetchone()
            return row is not None
        except sqlite3.OperationalError:
            return False

    def register_historical_vuln(self, source_desc: str, sink_name: str, path_hash: str, description: str) -> None:
        try:
            self.con.execute("""
                INSERT OR IGNORE INTO historical_vulns (source_symbol, sink_symbol, path_fingerprint, description)
                VALUES (?, ?, ?, ?)
            """, (source_desc, sink_name, path_hash, description))
        except sqlite3.OperationalError as e:
            import logging
            logging.getLogger(__name__).warning(f"Failed to register historical vuln: {e}")

    def get_meta_age_minutes(self, key: str, now: float) -> float | None:
        try:
            row = self.con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
            if not row:
                return None
            stored_ts = float(row["value"])
            return (now - stored_ts) / 60.0
        except (sqlite3.OperationalError, ValueError):
            return None

    def set_meta(self, key: str, now: float) -> None:
        try:
            self.con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(now)))
        except sqlite3.OperationalError as e:
            import logging
            logging.getLogger(__name__).warning(f"Failed to set meta key '{key}': {e}")

    def dismiss_finding(self, finding_type: str, file_path: str, symbol_name: str = "", reason: str = "") -> None:
        try:
            self.con.execute("""
                INSERT OR REPLACE INTO feedback (finding_type, file_path, symbol_name, status, reason)
                VALUES (?, ?, ?, 'dismissed', ?)
            """, (finding_type, file_path, symbol_name, reason))
            self.con.commit()
        except sqlite3.OperationalError as e:
            import logging
            logging.getLogger(__name__).warning(f"Failed to dismiss finding: {e}")

    def get_dismissed_findings(self) -> set[tuple[str, str, str]]:
        try:
            rows = self.con.execute(
                "SELECT finding_type, file_path, symbol_name FROM feedback WHERE status='dismissed'"
            ).fetchall()
            return {(r[0], r[1], r[2]) for r in rows}
        except sqlite3.OperationalError:
            return set()


