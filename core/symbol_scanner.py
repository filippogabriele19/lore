import sqlite3
import sys
import os
import time
import struct
from pathlib import Path
from typing import Optional, Any

from core.symbol_db import SymbolDB
from core.symbol_types import SymbolInfo
from core.symbol_extractor import extract_file, _extract_ts_file, _extract_go_file


# ---------------------------------------------------------------------------
# Role Classification
# ---------------------------------------------------------------------------

_TEST_IMPORT_MODULES = frozenset({
    "unittest", "pytest", "django.test", "django.test.TestCase",
    "django.test.TransactionTestCase", "django.test.SimpleTestCase",
    "django.test.LiveServerTestCase", "django.test.Client",
})

_TEST_BASE_CLASSES = frozenset({
    "TestCase", "TransactionTestCase", "SimpleTestCase",
    "LiveServerTestCase", "StaticLiveServerTestCase",
})


def _classify_role(sym: SymbolInfo, imports: list[tuple], rel_path: str) -> str:
    """Classify symbol role based on naming, imports, and file path signals."""
    # Signal 1: Test framework imports in the file
    file_has_test_imports = any(
        mod in _TEST_IMPORT_MODULES
        or mod.startswith("unittest.") or mod.startswith("pytest.")
        for _, mod, _ in imports
    )

    # Signal 2: Symbol name pattern
    name_is_test = (
        sym.name.startswith("test_")
        or (sym.name.startswith("Test") and sym.kind == "class")
    )

    # Signal 3: Base class in signature
    sig_has_test_base = any(
        base in (sym.signature or "") for base in _TEST_BASE_CLASSES
    )

    # Signal 4: File path
    path_parts = rel_path.replace("\\", "/").lower().split("/")
    path_is_test = any(p in ("tests", "test", "testing") for p in path_parts)
    filename_is_test = path_parts[-1].startswith("test_") if path_parts else False

    # Decision: need at least 2 signals, or file_has_test_imports + path signal
    test_signals = sum([
        file_has_test_imports, name_is_test, sig_has_test_base,
        path_is_test, filename_is_test,
    ])
    if test_signals >= 2 or (file_has_test_imports and (path_is_test or filename_is_test)):
        return "test"

    return "source"


def embed_all_symbols(db: SymbolDB, project_root: Path) -> int:
    """
    Compute sentence-transformer embeddings for all unembedded symbols.

    Text per symbol: "{name} {kind}: {signature}\\n{first 10 lines of body}"
    Stores as little-endian float32 BLOB (same format as embedding_indexer.py).
    Returns number of newly embedded symbols, 0 if sentence-transformers absent.
    """
    import sys
    if "pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ:
        return 0

    try:
        import struct
        import torch
        torch.set_num_threads(1)
        from sentence_transformers import SentenceTransformer
    except ImportError:
        return 0

    rows = db.symbols_needing_embedding()
    if not rows:
        return 0

    try:
        model = SentenceTransformer("all-MiniLM-L6-v2")
    except Exception as e:
        import logging
        logging.getLogger(__name__).debug(f"Symbol scanning embedding skipped: failed to initialize SentenceTransformer ({e})")
        return 0

    texts: list[str] = []
    ids:   list[int] = []
    fts_data: list[tuple] = []
    file_cache: dict[str, list[str]] = {}

    for r in rows:
        fpath = project_root / r["path"]
        path_str = str(fpath)
        if path_str not in file_cache:
            try:
                file_cache[path_str] = fpath.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception:
                file_cache[path_str] = []
                
        file_lines = file_cache[path_str]
        try:
            # Preview first 10 lines of body to capture docstrings and initial context natively
            body_preview = "\n".join(file_lines[r["line_start"] - 1: r["line_start"] + 10])
        except Exception:
            body_preview = ""
            
        text = f"{r['name']} {r['kind']}: {r['signature'] or ''}\n{body_preview}"
        texts.append(text)
        ids.append(r["id"])
        fts_data.append((r["id"], r["name"], r["kind"], r["signature"] or "", text, r["path"]))

    print(f"[EMBED] {len(rows):,} symbols...", flush=True)
    vecs = model.encode(texts, normalize_embeddings=True,
                        show_progress_bar=False, batch_size=256)
    dim = len(vecs[0])
    for sym_id, vec in zip(ids, vecs):
        db.store_embedding(sym_id, struct.pack(f"<{dim}f", *vec))
        
    for sym_id, name, kind, sig, text, path in fts_data:
        db.insert_fts(sym_id, name, kind, sig, text, path)
        
    db.commit()
    print(f"[EMBED] done.", flush=True)
    return len(rows)


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

_SCAN_IGNORE_DIRS = {
    ".lore", ".git", "__pycache__", "venv", ".venv", "node_modules", ".pytest_cache",
    ".next", "dist", "build", "target", "out", ".svelte-kit", ".nuxt", ".docusaurus",
    "package", "vendor", ".idea", ".vscode", "docs", "doc", "locale", "locales"
}

_TS_EXTENSIONS = {".ts", ".tsx", ".js", ".jsx"}
_GO_EXTENSIONS = {".go"}


import json

def _get_ignore_dirs(project_root: Path) -> set[str]:
    ignores = set(_SCAN_IGNORE_DIRS)
    config_path = project_root / ".lore" / "lore.config.json"
    if config_path.exists():
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                custom = data.get("exclude_dirs", [])
                ignores.update(custom)
        except Exception:
            pass
    return ignores


def _is_ignored(path: Path, project_root: Path) -> bool:
    """True if any path component is in the ignore list."""
    ignores = _get_ignore_dirs(project_root)
    try:
        parts = path.relative_to(project_root).parts
    except ValueError:
        parts = path.parts
    return any(p in ignores for p in parts)


def _collect_source_files(project_path: Path) -> list[Path]:
    """Collect .py + .ts/.tsx/.js/.jsx + .go files, excluding ignored dirs."""
    files: list[Path] = []
    supported = {".py"} | _TS_EXTENSIONS | _GO_EXTENSIONS
    import os
    ignores = _get_ignore_dirs(project_path)
    for root, dirs, filenames in os.walk(str(project_path)):
        # Prune ignored directories in place
        dirs[:] = [d for d in dirs if d not in ignores]
        for filename in filenames:
            ext = os.path.splitext(filename)[1].lower()
            if ext in supported:
                files.append(Path(root) / filename)
    return sorted(files)


def _process_file(fpath: Path, project_path: Path, db: SymbolDB) -> None:
    """Scan a single file (Python, TS/JS or Go) and store it in the DB."""
    rel = str(fpath.relative_to(project_path))
    
    # Skip files larger than 1MB to avoid parsing massive autogenerated files or data tables
    try:
        size = fpath.stat().st_size
        name_lower = fpath.name.lower()
        if size > 1024 * 1024:
            print(f"  [SCAN] skipping large file (>1MB): {rel}", flush=True)
            return
        if any(x in name_lower for x in ("min.js", "min.css", ".min.", "autogen", "generated")):
            print(f"  [SCAN] skipping autogenerated/minified file: {rel}", flush=True)
            return
    except Exception:
        pass

    try:
        lines_count = fpath.read_text(encoding="utf-8", errors="replace").count("\n")
    except Exception:
        lines_count = 0

    file_id = db.upsert_file(rel, lines_count)
    db.clear_file(file_id)

    if fpath.suffix in _TS_EXTENSIONS:
        symbols, imports = _extract_ts_file(fpath)
    elif fpath.suffix in _GO_EXTENSIONS:
        symbols, imports = _extract_go_file(fpath)
    else:
        symbols, imports = extract_file(fpath)

    db.insert_imports(file_id, imports)
    for sym in symbols:
        sym.role = _classify_role(sym, imports, rel)
        sym_id = db.insert_symbol(file_id, sym)
        db.insert_deps(sym_id, file_id, sym)


def scan(project_path: Path, db: SymbolDB):
    all_files = _collect_source_files(project_path)
    total = len(all_files)
    milestone = max(1, total // 10)  # print at every 10%

    for i, fpath in enumerate(all_files, 1):
        _process_file(fpath, project_path, db)
        if i % milestone == 0 and i < total:
            pct = i * 100 // total
            print(f"  [SCAN] {pct}%  ({i}/{total})", flush=True)

    db.commit()
    print(f"[SCAN]  {total:,} files · {db.con.execute('SELECT COUNT(*) FROM symbols').fetchone()[0]:,} symbols indexed")


def scan_files(db: SymbolDB, project_root: Path, rel_paths: list[str]) -> int:
    """
    Incremental rescan — process only the listed files.
    If a file no longer exists on disk, removes it from the index.
    Returns the number of files processed.
    C2 — used by the post-commit hook.
    """
    processed = 0
    supported = {".py"} | _TS_EXTENSIONS | _GO_EXTENSIONS
    for rel_path in rel_paths:
        abs_path = project_root / rel_path
        if abs_path.suffix not in supported:
            continue
        if not abs_path.exists():
            db.delete_file_by_path(rel_path)
            continue
        _process_file(abs_path, project_root, db)
        processed += 1
    db.commit()
    return processed


def embed_changed(db: SymbolDB, project_root: Path, rel_paths: list[str]) -> int:
    """
    Re-embed only the symbols belonging to the given files.
    After scan_files() the re-inserted symbols have NULL embeddings;
    embed_all_symbols() picks them up.  rel_paths is accepted for API
    symmetry with the hook but the filter is handled implicitly.
    C2 — used by the post-commit hook.
    """
    return embed_all_symbols(db, project_root)


# ---------------------------------------------------------------------------
# Retriever — il cuore del PoC
# ---------------------------------------------------------------------------

LAZY_THRESHOLD = 80   # righe — sopra questa soglia il simbolo non viene auto-espanso


