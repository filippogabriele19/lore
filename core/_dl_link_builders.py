# core/_dl_link_builders.py — M2/M3/M4 link builders (split from decision_linker.py)
from __future__ import annotations

import json
import re
import sqlite3
import struct
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_IGNORE_DIRS = {
    '.git', '.lore', '__pycache__', 'venv', '.venv',
    'node_modules', 'backups', 'build', 'dist', '.pytest_cache',
}

# ADR / decision document keywords for filename matching
_ADR_KEYWORDS = ("adr", "decision", "spec", "design", "arch", "rfc", "proposal")


def links_from_commit_reasoning(conn: sqlite3.Connection) -> int:
    """M2 — Commit reasoning (confidence 0.70-0.85)."""
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if "commit_reasoning" not in tables:
        return 0

    rows = conn.execute(
        "SELECT commit_hash, body, keywords_found, files_touched FROM commit_reasoning"
    ).fetchall()

    # Pre-cache all symbols by their file path to avoid N+1 query problem
    file_symbol_map = {}
    try:
        sym_rows = conn.execute("""
            SELECT f.path, s.name, s.id FROM symbols s
            JOIN files f ON s.file_id = f.id
        """).fetchall()
        for r in sym_rows:
            path_key = r["path"].replace("\\", "/").lower()
            file_symbol_map.setdefault(path_key, []).append((r["name"], r["id"]))
    except Exception:
        pass

    _INSERT = """INSERT OR IGNORE INTO decision_links
                 (symbol_name, symbol_id, source_type, source_ref, confidence, description)
                 VALUES (?, ?, 'commit', ?, ?, ?)"""
    _CAP     = 5_000
    _BATCH   = 500

    inserted = 0
    batch: list[tuple] = []

    def _flush() -> int:
        if not batch:
            return 0
        conn.executemany(_INSERT, batch)
        n = len(batch)
        batch.clear()
        return n

    capped = False
    for row in rows:
        hash_  = row[0]
        body   = row[1] or ""
        try:
            keywords = json.loads(row[2]) if row[2] else []
        except Exception:
            keywords = [row[2]] if row[2] else []

        try:
            files_touched = json.loads(row[3]) if row[3] else []
        except Exception:
            files_touched = [row[3]] if row[3] else []

        first_line = body.strip().splitlines()[0][:120] if body.strip() else ""
        confidence = min(1.0, 0.6 + 0.1 * len(keywords))
        body_lower = body.lower()

        for file_path in files_touched:
            basename = file_path.split("/")[-1].lower()
            if len(files_touched) > 3 and basename not in body_lower:
                continue
            
            path_key = file_path.replace("\\", "/").lower()
            symbol_rows = file_symbol_map.get(path_key, [])
            targets = symbol_rows if symbol_rows else [(file_path, None)]

            for sym_name, sym_id in targets:
                batch.append((sym_name, sym_id, hash_[:8], confidence, first_line))
                if len(batch) >= _BATCH:
                    inserted += _flush()
                if inserted >= _CAP:
                    capped = True
                    break
            if capped:
                break
        if capped:
            break

    inserted += _flush()
    if capped:
        logger.info("M2 cap reached: 5000 links")
    return inserted


def links_from_hotspots(conn: sqlite3.Connection) -> int:
    """M4 — Hotspot warnings (confidence = risk_score)."""
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if "hotspots" not in tables:
        return 0

    rows = conn.execute(
        "SELECT file_path, change_freq, risk_score FROM hotspots WHERE risk_score > 0.5"
    ).fetchall()

    count = 0
    for row in rows:
        file_path, change_freq, risk_score = row
        symbol_rows = conn.execute(
            """SELECT s.name FROM symbols s
               JOIN files f ON s.file_id = f.id
               WHERE f.path LIKE ? LIMIT 20""",
            (f"%{file_path.split('/')[-1]}",),
        ).fetchall()
        targets = [r[0] for r in symbol_rows] if symbol_rows else [file_path]
        description = (
            f"HOTSPOT: {change_freq} commits in last 90 days — "
            f"review carefully before modifying"
        )
        for sym_name in targets:
            conn.execute(
                """INSERT OR IGNORE INTO decision_links
                   (symbol_name, source_type, source_ref, confidence, description)
                   VALUES (?, 'hotspot', ?, ?, ?)""",
                (sym_name, file_path, float(risk_score), description),
            )
            count += 1

    return count


def links_from_adr_semantic(conn: sqlite3.Connection, project_root: Path) -> int:
    """
    M3 — Semantic ADR embedding (confidence 0.55-0.75).

    Algorithm:
    1. Find markdown docs with ADR/decision/spec keywords in filename.
    2. Chunk each doc by paragraph (> 50 chars).
    3. Embed chunks with all-MiniLM-L6-v2 (same model as EmbeddingIndexer).
    4. Compute cosine similarity against symbol embeddings from DB.
    5. For similarity >= 0.72: create link with confidence = sim * 0.85 (capped 0.55-0.75).
    """
    try:
        import numpy as np
        from sentence_transformers import SentenceTransformer
    except ImportError:
        logger.debug("M3 semantic ADR skipped: sentence-transformers not available")
        return 0

    # Get symbol embeddings from DB.
    # symbol_embeddings is a vec0 virtual table — load the extension first or
    # any query on it will raise "no such module: vec0".
    try:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except Exception as e:
        logger.debug(f"M3 semantic ADR skipped: sqlite-vec not loadable ({e})")
        return 0

    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if "symbol_embeddings" not in tables:
        return 0

    sym_rows = conn.execute(
        """SELECT s.name, se.embedding
           FROM symbol_embeddings se
           JOIN symbols s ON s.id = se.symbol_id
           LIMIT 1000"""
    ).fetchall()
    if not sym_rows:
        return 0

    # Decode little-endian float32 embeddings
    sym_data: list[tuple[str, any]] = []
    for name, blob in sym_rows:
        try:
            n = len(blob) // 4
            vec = np.array(struct.unpack(f"<{n}f", blob), dtype=np.float32)
            sym_data.append((name, vec))
        except Exception:
            pass
    if not sym_data:
        return 0

    sym_matrix = np.stack([v for _, v in sym_data])  # (N, D)

    # Find ADR/decision/spec markdown files.
    # Priority: files/dirs matching ADR keywords.
    # Fallback: all .md files >= 300 chars (README, ROADMAP, CONTRIBUTING, etc.)
    # excluding trivial files (license, issue templates, changelogs).
    _TRIVIAL = {"license", "changelog", "authors", "credits", "notice"}
    _ALL_MD_IGNORE = {
        ".github", "issue_template", "vendor", "static", "node_modules",
    }

    all_md = [
        f for f in project_root.rglob("*.md")
        if not any(p in _IGNORE_DIRS for p in f.parts)
        and not any(p.lower() in _ALL_MD_IGNORE for p in f.parts)
        and not any(kw in f.stem.lower() for kw in _TRIVIAL)
    ]

    md_files = [
        f for f in all_md
        if any(kw in f.name.lower() for kw in _ADR_KEYWORDS)
        or any(kw in part.lower() for part in f.parts for kw in _ADR_KEYWORDS)
    ]

    if not md_files:
        # No ADR-specific files found — fall back to all substantial MD files
        md_files = [f for f in all_md if f.stat().st_size >= 300]

    if not md_files:
        return 0

    try:
        model = SentenceTransformer("all-MiniLM-L6-v2")
    except Exception as e:
        logger.debug(f"M3 semantic ADR skipped: failed to initialize SentenceTransformer ({e})")
        return 0
    count = 0

    for md_file in md_files:
        try:
            text = md_file.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue

        rel_path = str(md_file.relative_to(project_root)).replace("\\", "/")
        doc_id = _upsert_adr_doc(conn, rel_path)

        # Chunk by paragraph (blank-line separated, min 50 chars)
        paragraphs = [
            p.strip() for p in re.split(r'\n\s*\n', text)
            if len(p.strip()) > 50
        ][:20]  # cap at 20 chunks per doc

        for chunk_idx, para in enumerate(paragraphs):
            try:
                chunk_emb = model.encode(
                    [para], normalize_embeddings=True, show_progress_bar=False
                )[0].astype(np.float32)
            except Exception:
                continue

            # Upsert chunk embedding
            existing = conn.execute(
                "SELECT id FROM adr_chunks WHERE doc_id=? AND chunk_idx=?",
                (doc_id, chunk_idx),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE adr_chunks SET embedding=? WHERE id=?",
                    (chunk_emb.tobytes(), existing[0]),
                )
            else:
                conn.execute(
                    "INSERT INTO adr_chunks (doc_id, chunk_idx, content, embedding)"
                    " VALUES (?,?,?,?)",
                    (doc_id, chunk_idx, para[:500], chunk_emb.tobytes()),
                )

            # Cosine similarity with symbol matrix (both are L2-normalised)
            sims = sym_matrix @ chunk_emb
            desc = para[:120].replace("\n", " ")

            for i, sim in enumerate(sims):
                if float(sim) < 0.72:
                    continue
                sym_name   = sym_data[i][0]
                confidence = min(0.75, max(0.55, float(sim) * 0.85))
                conn.execute(
                    """INSERT OR IGNORE INTO decision_links
                       (symbol_name, source_type, source_ref, confidence, description)
                       VALUES (?, 'adr_semantic', ?, ?, ?)""",
                    (sym_name, rel_path, confidence, desc),
                )
                count += 1

    conn.commit()
    return count


def _upsert_adr_doc(conn: sqlite3.Connection, rel_path: str) -> int:
    """Insert or return existing adr_index.id for a given doc path."""
    row = conn.execute(
        "SELECT id FROM adr_index WHERE doc_path=?", (rel_path,)
    ).fetchone()
    if row:
        return row[0]
    conn.execute(
        "INSERT INTO adr_index (doc_path, doc_type) VALUES (?, ?)",
        (rel_path, _classify_doc(rel_path)),
    )
    return conn.execute("SELECT last_insert_rowid()").fetchone()[0]


def _classify_doc(path: str) -> str:
    p = path.lower()
    if "adr" in p:
        return "adr"
    if "spec" in p:
        return "spec"
    if "design" in p or "arch" in p:
        return "design"
    if "rfc" in p or "proposal" in p:
        return "proposal"
    return "decision"
