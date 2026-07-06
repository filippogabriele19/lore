"""
core/decision_linker.py — ADR-007

Links symbols to decision sources via three mechanisms:
  M1 — Mention detection   (confidence 0.95)
  M2 — Commit reasoning    (confidence 0.70-0.85)
  M3 — Semantic ADR embed  (confidence 0.55-0.75)
  M4 — Hotspot warnings    (confidence = risk_score)

Public API: DecisionLinker(db_path).build_links() / .get_context() / .get_hotspot_files()
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


class DecisionLinker:
    def __init__(self, db_path: str):
        self.db_path = Path(db_path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build_links(self, project_root: Optional[str] = None) -> int:
        """
        Build decision_links from all mechanisms (idempotent).
        Returns total link count.
        """
        if not self.db_path.exists():
            logger.warning(f"DB not found: {self.db_path}")
            return 0

        root = Path(project_root) if project_root else None
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            try:
                conn.execute("PRAGMA journal_mode=WAL;")
            except Exception:
                pass
            conn.execute("PRAGMA foreign_keys=ON;")
            self._ensure_schema(conn)

            print("[LINK]  building decision links...")

            from core._dl_mention_builder import links_from_mentions
            from core._dl_link_builders import (
                links_from_commit_reasoning,
                links_from_hotspots,
                links_from_adr_semantic,
            )

            with conn:
                m1 = m2 = m3 = m4 = 0
                if root and root.exists():
                    try:
                        m1 = links_from_mentions(conn, root)
                    except Exception as e:
                        logger.warning(f"M1 mention detection failed: {e}")

                try:
                    m2 = links_from_commit_reasoning(conn)
                except Exception as e:
                    logger.warning(f"M2 commit reasoning failed: {e}")

                if root and root.exists():
                    try:
                        m3 = links_from_adr_semantic(conn, root)
                    except Exception as e:
                        logger.warning(f"M3 semantic ADR failed (vec0 unavailable?): {e}")

                try:
                    m4 = links_from_hotspots(conn)
                except Exception as e:
                    logger.warning(f"M4 hotspot links failed: {e}")

            count = m1 + m2 + m3 + m4
            print(f"[LINK]  {count:,} decision links  (M1={m1} · M2={m2:,} · M3={m3} · M4={m4})")
            logger.info(f"DecisionLinker: {count} total links built")
            return count
        finally:
            conn.close()

    def get_context(self, symbol_names: list[str]) -> list[dict]:
        """
        Returns the most relevant citations for the given symbols.
        Each citation: symbol_name, source_type, source_ref, confidence, description.
        Sorted by confidence desc. Returns [] on any error.
        """
        if not self.db_path.exists() or not symbol_names:
            return []

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            if "decision_links" not in tables:
                return []

            placeholders = ",".join("?" * len(symbol_names))
            rows = conn.execute(
                f"""SELECT symbol_name, source_type, source_ref, confidence, description
                    FROM decision_links
                    WHERE symbol_name IN ({placeholders})
                    ORDER BY confidence DESC LIMIT 20""",
                symbol_names,
            ).fetchall()

            cr_table_exists = "commit_reasoning" in tables
            results = []
            for row in rows:
                if row["source_type"] != "commit" or not cr_table_exists:
                    results.append(dict(row))
                    continue
                cr = conn.execute(
                    "SELECT body, files_touched FROM commit_reasoning"
                    " WHERE commit_hash LIKE ?",
                    (row["source_ref"] + "%",),
                ).fetchone()
                if cr is None:
                    results.append(dict(row))
                    continue
                try:
                    files_touched = json.loads(cr["files_touched"] or "[]")
                except Exception:
                    files_touched = []
                body_lower = (cr["body"] or "").lower()
                sym_lower  = (row["symbol_name"] or "").lower()
                if sym_lower in body_lower or len(files_touched) <= 2:
                    results.append(dict(row))

            return results[:10]
        finally:
            conn.close()

    def get_hotspot_files(self) -> list[dict]:
        """Returns files classified as hotspot (risk_score > 0.5)."""
        if not self.db_path.exists():
            return []
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            if "hotspots" not in tables:
                return []
            rows = conn.execute(
                "SELECT file_path, change_freq, risk_score FROM hotspots"
                " WHERE risk_score > 0.5 ORDER BY risk_score DESC"
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Schema (additive migrations only — ADR-002)
    # ------------------------------------------------------------------

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS decision_links (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol_name TEXT,
                source_type TEXT,
                source_ref  TEXT,
                confidence  REAL DEFAULT 0.0,
                description TEXT
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_dl_symbol ON decision_links(symbol_name)"
        )
        try:
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_dl_unique"
                " ON decision_links(symbol_name, source_type, source_ref)"
            )
        except Exception:
            pass  # pre-existing duplicates: acceptable

        # L4 ADR index tables (M3 semantic)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS adr_index (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_path TEXT UNIQUE,
                doc_type TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS adr_chunks (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                doc_id    INTEGER REFERENCES adr_index(id),
                chunk_idx INTEGER,
                content   TEXT,
                embedding BLOB,
                UNIQUE(doc_id, chunk_idx)
            )
        """)
        conn.commit()
