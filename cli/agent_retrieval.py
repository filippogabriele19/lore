import os
import re
import struct
import heapq
from pathlib import Path
from core.symbol_map import SymbolDB, SymbolRetriever

_embed_model = None

def _get_embed_model():
    global _embed_model
    if _embed_model is not None:
        return _embed_model
    try:
        from sentence_transformers import SentenceTransformer
        _embed_model = SentenceTransformer("all-MiniLM-L6-v2")
    except ImportError:
        pass
    return _embed_model

def _cosine_sim(a_bytes: bytes, b_vec: list) -> float:
    dim = len(b_vec)
    a_vec = struct.unpack(f"<{dim}f", a_bytes[:dim * 4])
    return sum(x * y for x, y in zip(a_vec, b_vec))

_data_containers_cache = None

def _detect_data_containers(db: SymbolDB) -> frozenset:
    """Identify classes that are pure data containers (many fields, few methods)."""
    rows = db.con.execute("""
        SELECT s.name, s.file_id,
               (SELECT COUNT(*) FROM symbols s2 
                WHERE s2.parent_class = s.name AND s2.file_id = s.file_id 
                AND s2.kind = 'method') as method_count
        FROM symbols s
        WHERE s.kind = 'class'
    """).fetchall()
    containers = set()
    for r in rows:
        if r["method_count"] <= 2:
            containers.add(r["name"])
    return frozenset(containers)

def _get_data_containers(db: SymbolDB) -> frozenset:
    global _data_containers_cache
    if _data_containers_cache is None:
        _data_containers_cache = _detect_data_containers(db)
    return _data_containers_cache

def _make_embed_fn():
    import os as _os
    _os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    _os.environ.setdefault("HF_DATASETS_OFFLINE", "1")
    try:
        from sentence_transformers import SentenceTransformer as _ST
        _model = _ST("all-MiniLM-L6-v2")
        def _fn(text: str) -> list:
            return _model.encode(text, normalize_embeddings=True).tolist()
        return _fn
    except Exception:
        return None

def _compute_intent_delta(body: str, files_touched: str, embed_fn) -> float:
    try:
        v_intent = embed_fn(body[:512])
        v_impl   = embed_fn(files_touched[:512])
        if not v_intent or not v_impl:
            return -1.0
        dot = sum(a * b for a, b in zip(v_intent, v_impl))
        na  = sum(x * x for x in v_intent) ** 0.5
        nb  = sum(x * x for x in v_impl)   ** 0.5
        if na == 0 or nb == 0:
            return -1.0
        return round(1.0 - dot / (na * nb), 4)
    except Exception:
        return -1.0

def _build_project_map(db: SymbolDB, project_root: Path, max_files: int = 80) -> str:
    INLINE_THRESHOLD = 150
    rows = db.con.execute(
        "SELECT f.path, f.lines, s.name, s.kind "
        "FROM files f "
        "LEFT JOIN symbols s ON s.file_id = f.id "
        "  AND s.parent_class IS NULL "
        "  AND s.kind IN ('class', 'function') "
        "ORDER BY f.path, s.kind DESC, s.line_start "
        "LIMIT 1000",
    ).fetchall()

    file_lines: dict[str, int] = {}
    file_syms: dict[str, list[str]] = {}
    for r in rows:
        p = r["path"]
        file_lines.setdefault(p, r["lines"] or 0)
        file_syms.setdefault(p, [])
        if r["name"]:
            tag = "C" if r["kind"] == "class" else "f"
            file_syms[p].append(f"{r['name']}({tag})")

    lines = [f"PROJECT MAP — {len(file_lines)} files  (files ≤{INLINE_THRESHOLD} lines shown in full)"]
    inlined = 0
    for path in list(file_lines.keys())[:max_files]:
        n_lines = file_lines[path]
        if n_lines <= INLINE_THRESHOLD:
            abs_path = project_root / path
            try:
                content = abs_path.read_text(encoding="utf-8", errors="replace")
                lines.append(f"\n── {path}  ({n_lines} lines) ──")
                lines.append(content.rstrip())
                lines.append(f"── end {path} ──")
                inlined += 1
                continue
            except OSError:
                pass
        syms = file_syms[path]
        sym_str = "  [" + ", ".join(syms[:10]) + "]" if syms else ""
        if len(syms) > 10:
            sym_str = sym_str[:-1] + f", +{len(syms)-10} more]"
        lines.append(f"  {path}  ({n_lines} lines){sym_str}")

    if len(file_lines) > max_files:
        lines.append(f"  ... +{len(file_lines) - max_files} more files")
    lines.append(f"\n({inlined} small files inlined, {len(file_lines) - inlined} large files listed by symbol)")
    return "\n".join(lines)

def _import_chase(top_symbols: list[tuple[float, str]], db: SymbolDB, emb_map: dict[str, bytes], task_vec: list, heap: list[tuple[float, str]], role_map: dict[str, str], file_path_map: dict[str, str], name_to_keys: dict[str, list[str]]):
    """When test symbols rank high, follow their imports to find source code."""
    chased_files = set()
    for sim, sym_key in top_symbols:
        if role_map.get(sym_key) != "test":
            continue
        # Find the file_id for this test symbol
        file_path = file_path_map.get(sym_key)
        if not file_path:
            continue
        row = db.con.execute("SELECT id FROM files WHERE path = ?", (file_path,)).fetchone()
        if not row:
            continue
        file_id = row["id"]
        # Get all imports from this test file
        imported_names = db.get_file_imports(file_id)
        for imp_name in imported_names:
            if imp_name in chased_files:
                continue
            chased_files.add(imp_name)
            # Find symbols matching the imported name
            for imp_key in name_to_keys.get(imp_name, []):
                imp_sim = _cosine_sim(emb_map[imp_key], task_vec)
                # Boost: imported-by-test symbols get a 1.3x multiplier
                boosted = min(imp_sim * 1.3, 1.0)
                heapq.heappush(heap, (-boosted, imp_key))

def _astar_bundle(
    task: str,
    db: SymbolDB,
    retriever: SymbolRetriever,
    token_budget: int = 5000,
) -> tuple[str, list[str]]:
    from cli.agent_stage import _extract_target_files
    target_files = _extract_target_files(task)
    
    model = _get_embed_model()
    if model is None:
        return "", []

    task_vec = model.encode([task], normalize_embeddings=True, show_progress_bar=False)[0]
    all_emb = db.all_embeddings_with_role()
    if not all_emb:
        return "", []

    emb_map: dict[str, bytes] = {}
    role_map: dict[str, str] = {}
    file_path_map: dict[str, str] = {}
    key_to_kind: dict[str, str] = {}
    name_to_keys: dict[str, list[str]] = {}
    data_containers = _get_data_containers(db)
    scored: list[tuple[float, str]] = []
    for name, emb_bytes, role, file_path, kind in all_emb:
        if emb_bytes:
            norm_path = file_path.replace("\\", "/")
            sym_key = f"{norm_path}::{name}"
            sim = _cosine_sim(emb_bytes, task_vec)
            # Soft penalty for test symbols
            if role == "test":
                sim *= 0.5
            # Penalty for data containers (gravitational attractors)
            if kind == "class" and name in data_containers:
                sim *= 0.5
            scored.append((sim, sym_key))
            emb_map[sym_key] = emb_bytes
            role_map[sym_key] = role
            file_path_map[sym_key] = norm_path
            key_to_kind[sym_key] = kind
            name_to_keys.setdefault(name, []).append(sym_key)

    if not scored:
        return "", []

    # --- HYBRID SEARCH: combine semantic + BM25 keyword ---
    k = 60  # RRF constant
    
    # Semantic ranking
    semantic_ranked = sorted(scored, reverse=True)
    semantic_rrf = {}
    for rank, (sim, sym_key) in enumerate(semantic_ranked):
        semantic_rrf[sym_key] = 1.0 / (k + rank + 1)
        
    # BM25 keyword ranking
    keyword_results = db.search_fts(task, limit=30)
    keyword_rrf = {}
    for rank, row in enumerate(keyword_results):
        sym_name = row["name"]
        file_path = row["file_path"].replace("\\", "/")
        sym_key = f"{file_path}::{sym_name}"
        keyword_rrf[sym_key] = 1.0 / (k + rank + 1)
        
    # Merge
    all_keys = set(semantic_rrf) | set(keyword_rrf)
    hybrid_scored = []
    for sym_key in all_keys:
        sem_score = semantic_rrf.get(sym_key, 0)
        kw_score = keyword_rrf.get(sym_key, 0)
        combined = 0.6 * sem_score + 0.4 * kw_score
        hybrid_scored.append((combined, sym_key))

    top_n = 5
    hybrid_ranked = sorted(hybrid_scored, reverse=True)
    
    # Check low confidence
    is_low_confidence = False
    if hybrid_ranked:
        top_score, top_key = hybrid_ranked[0]
        sem_score = semantic_rrf.get(top_key, 0)
        # If the top result comes entirely from keyword and semantic is very poor
        if sem_score == 0 and top_score < 0.005: 
            is_low_confidence = True
            
    top_symbols = [(score, key) for score, key in hybrid_ranked[:top_n]]

    if not top_symbols or is_low_confidence:
        return "[LOW CONFIDENCE] Il retriever non ha trovato simboli rilevanti per questa query.", []
    
    # Expand top_symbols to include omonym classes/functions in other files
    expanded_top_symbols = []
    seen_expanded = set()
    for sim, sym_key in top_symbols:
        if sym_key in seen_expanded:
            continue
        seen_expanded.add(sym_key)
        expanded_top_symbols.append((sim, sym_key))
        
        if "::" in sym_key:
            _, sym_name = sym_key.split("::", 1)
            kind = key_to_kind.get(sym_key, "")
            if kind in ("class", "function") and not sym_name.startswith("__"):
                for other_key in name_to_keys.get(sym_name, []):
                    if other_key not in seen_expanded:
                        seen_expanded.add(other_key)
                        expanded_top_symbols.append((sim, other_key))
                        
    top_symbols = expanded_top_symbols

    heap: list[tuple[float, str]] = [(-sim, sym_key) for sim, sym_key in top_symbols]
    heapq.heapify(heap)

    # Import chasing: if top symbols are tests, follow their imports
    # to find the source code being tested
    _import_chase(top_symbols, db, emb_map, task_vec, heap, role_map, file_path_map, name_to_keys)

    FILE_INLINE_LIMIT = 200
    visited: set[str] = set()
    bundle_parts: list[str] = []
    file_to_syms: dict[str, list[str]] = {}
    tokens_used = 0

    while heap and tokens_used < token_budget:
        neg_sim, sym_key = heapq.heappop(heap)
        if sym_key in visited:
            continue
        visited.add(sym_key)

        if "::" in sym_key:
            fpath, sym_name = sym_key.split("::", 1)
        else:
            fpath, sym_name = None, sym_key

        block = retriever.get_symbol_block(sym_name, fpath)
        if not block:
            continue

        body_tokens = len(block["body"]) // 4
        if tokens_used + body_tokens > token_budget:
            continue

        bundle_parts.append(
            f"SYMBOL: {sym_name}  [{block['kind']}]\n"
            f"FILE:   {block['file']}  (lines {block['lines']})\n"
            f"\n{block['body']}\n"
        )
        tokens_used += body_tokens
        rel = block["file"].replace("\\", "/")
        file_to_syms.setdefault(rel, []).append(sym_key)

        for dep in block.get("depends_on", []):
            dep_name = dep["name"]
            for dep_key in name_to_keys.get(dep_name, []):
                if dep_key not in visited:
                    if "::" in dep_key:
                        dep_file, dep_name_real = dep_key.split("::", 1)
                    else:
                        dep_file, dep_name_real = None, dep_name
                    # Recuperiamo il blocco della dipendenza per controllare in quale file risiede
                    dep_block = retriever.get_symbol_block(dep_name_real, dep_file)
                    if dep_block:
                        dep_file_norm = dep_block["file"].replace("\\", "/")
                        sim = _cosine_sim(emb_map[dep_key], task_vec)
                        
                        # Regola di pruning chirurgica:
                        if target_files:
                            # Espandi solo se appartiene a uno dei file target espliciti
                            norm_targets = [tf.replace("\\", "/") for tf in target_files]
                            if dep_file_norm in norm_targets:
                                heapq.heappush(heap, (-sim, dep_key))
                        else:
                            # Altrimenti espandi solo se ha un'ottima similarità al task
                            if sim > 0.4:
                                heapq.heappush(heap, (-sim, dep_key))

    if not bundle_parts:
        return "", list(visited)

    full_file_map: dict[str, str] = {}
    symbol_files_to_skip: set[str] = set()
    for rel, syms in file_to_syms.items():
        abs_path = retriever.project_root / rel
        try:
            content = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if content.count("\n") <= FILE_INLINE_LIMIT:
            full_file_map[rel] = content
            symbol_files_to_skip.add(rel)

    filtered_parts: list[str] = []
    for part in bundle_parts:
        m = re.search(r"^FILE:\s+(\S+)", part, re.MULTILINE)
        if m:
            part_file = m.group(1).replace("\\", "/")
            if part_file in symbol_files_to_skip:
                continue
        filtered_parts.append(part)

    full_file_parts: list[str] = [
        f"FULL FILE (≤{FILE_INLINE_LIMIT} lines — read-only reference, do NOT reproduce):\n"
        f"FILE: {rel}\n"
        f"```\n{content.rstrip()}\n```"
        for rel, content in full_file_map.items()
    ]

    bundle = "\n".join(f"{'─'*60}\n{p}" for p in filtered_parts)
    if full_file_parts:
        bundle = "\n\n".join(full_file_parts) + "\n\n" + bundle

    try:
        from core.decision_linker import DecisionLinker
        dl = DecisionLinker(str(db.db_path))
        visited_names = [k.split("::", 1)[1] if "::" in k else k for k in visited]
        citations = dl.get_context(visited_names)
        # Only include high-confidence citations to reduce noise
        citations = [c for c in citations if c.get('confidence', 0) >= 0.7]
        if citations:
            ctx_lines = ["=== DECISION CONTEXT (vincoli non derogabili) ==="]
            for c in citations:
                ctx_lines.append(f"[{c['source_type'].upper()} {c['source_ref']} conf={c['confidence']:.2f}] {c['symbol_name']}: {c['description']}")
            bundle = "\n".join(ctx_lines) + "\n\n" + bundle

        # Skip hotspot warnings during benchmarks (env var) to reduce token noise
        if not os.environ.get("LORE_SKIP_HOTSPOT_WARNINGS"):
            hotspots = {h["file_path"] for h in dl.get_hotspot_files()}
            hot_syms = [s for s in visited if any(h in s for h in hotspots)]
            if hot_syms:
                warn = "=== HOTSPOT WARNING ===\nSimboli in file ad alto rischio (molte modifiche recenti):\n" + "\n".join(f"  ⚠ {s}" for s in hot_syms)
                bundle = warn + "\n\n" + bundle
    except Exception:
        pass

    return bundle, list(visited)


def _build_compact_project_map(db: SymbolDB, max_files: int = 120) -> str:
    """Compact project map for the Localizer: file paths + top-level symbol names only."""
    rows = db.con.execute(
        "SELECT f.path, f.lines, s.name, s.kind "
        "FROM files f "
        "LEFT JOIN symbols s ON s.file_id = f.id "
        "  AND s.parent_class IS NULL "
        "  AND s.kind IN ('class', 'function') "
        "ORDER BY f.path, s.kind DESC, s.line_start "
        "LIMIT 2000",
    ).fetchall()

    file_lines: dict[str, int] = {}
    file_syms: dict[str, list[str]] = {}
    for r in rows:
        p = r["path"]
        file_lines.setdefault(p, r["lines"] or 0)
        file_syms.setdefault(p, [])
        if r["name"]:
            tag = "C" if r["kind"] == "class" else "f"
            file_syms[p].append(f"{r['name']}({tag})")

    lines = [f"PROJECT STRUCTURE — {len(file_lines)} files"]
    for path in list(file_lines.keys())[:max_files]:
        n = file_lines[path]
        syms = file_syms[path]
        sym_str = "  [" + ", ".join(syms[:8]) + "]" if syms else ""
        if len(syms) > 8:
            sym_str = sym_str[:-1] + f", +{len(syms)-8} more]"
        lines.append(f"  {path}  ({n}L){sym_str}")
    if len(file_lines) > max_files:
        lines.append(f"  ... +{len(file_lines) - max_files} more files")
    return "\n".join(lines)


def _astar_bundle_light(
    task: str,
    db: SymbolDB,
    retriever: SymbolRetriever,
    token_budget: int = 2000,
) -> tuple[str, list[str]]:
    """Lightweight A* bundle: symbol names + signatures only, no bodies.
    Used for the Localizer phase where implementation details are not needed."""
    from cli.agent_stage import _extract_target_files
    target_files = _extract_target_files(task)

    model = _get_embed_model()
    if model is None:
        return "", []

    task_vec = model.encode([task], normalize_embeddings=True, show_progress_bar=False)[0]
    all_emb = db.all_embeddings_with_role()
    if not all_emb:
        return "", []

    emb_map: dict[str, bytes] = {}
    role_map: dict[str, str] = {}
    file_path_map: dict[str, str] = {}
    key_to_kind: dict[str, str] = {}
    name_to_keys: dict[str, list[str]] = {}
    data_containers = _get_data_containers(db)
    scored: list[tuple[float, str]] = []
    for name, emb_bytes, role, file_path, kind in all_emb:
        if emb_bytes:
            norm_path = file_path.replace("\\", "/")
            sym_key = f"{norm_path}::{name}"
            sim = _cosine_sim(emb_bytes, task_vec)
            if role == "test":
                sim *= 0.5
            if kind == "class" and name in data_containers:
                sim *= 0.5
            scored.append((sim, sym_key))
            emb_map[sym_key] = emb_bytes
            role_map[sym_key] = role
            file_path_map[sym_key] = norm_path
            key_to_kind[sym_key] = kind
            name_to_keys.setdefault(name, []).append(sym_key)

    if not scored:
        return "", []

    # --- HYBRID SEARCH: combine semantic + BM25 keyword ---
    k = 60
    semantic_ranked = sorted(scored, reverse=True)
    semantic_rrf = {}
    for rank, (sim, sym_key) in enumerate(semantic_ranked):
        semantic_rrf[sym_key] = 1.0 / (k + rank + 1)
        
    keyword_results = db.search_fts(task, limit=30)
    keyword_rrf = {}
    for rank, row in enumerate(keyword_results):
        sym_name = row["name"]
        file_path = row["file_path"].replace("\\", "/")
        sym_key = f"{file_path}::{sym_name}"
        keyword_rrf[sym_key] = 1.0 / (k + rank + 1)
        
    all_keys = set(semantic_rrf) | set(keyword_rrf)
    hybrid_scored = []
    for sym_key in all_keys:
        sem_score = semantic_rrf.get(sym_key, 0)
        kw_score = keyword_rrf.get(sym_key, 0)
        combined = 0.6 * sem_score + 0.4 * kw_score
        hybrid_scored.append((combined, sym_key))

    top_n = 10
    hybrid_ranked = sorted(hybrid_scored, reverse=True)
    
    is_low_confidence = False
    if hybrid_ranked:
        top_score, top_key = hybrid_ranked[0]
        sem_score = semantic_rrf.get(top_key, 0)
        if sem_score == 0 and top_score < 0.005: 
            is_low_confidence = True
            
    top_symbols = [(score, key) for score, key in hybrid_ranked[:top_n]]

    if not top_symbols or is_low_confidence:
        return "[LOW CONFIDENCE] Il retriever non ha trovato simboli rilevanti per questa query.", []

    # Expand top_symbols to include omonym classes/functions in other files
    expanded_top_symbols = []
    seen_expanded = set()
    for sim, sym_key in top_symbols:
        if sym_key in seen_expanded:
            continue
        seen_expanded.add(sym_key)
        expanded_top_symbols.append((sim, sym_key))
        
        if "::" in sym_key:
            _, sym_name = sym_key.split("::", 1)
            kind = key_to_kind.get(sym_key, "")
            if kind in ("class", "function") and not sym_name.startswith("__"):
                for other_key in name_to_keys.get(sym_name, []):
                    if other_key not in seen_expanded:
                        seen_expanded.add(other_key)
                        expanded_top_symbols.append((sim, other_key))
                        
    top_symbols = expanded_top_symbols

    visited: list[str] = []
    bundle_parts: list[str] = []
    tokens_used = 0
    seen = set()

    for sim, sym_key in top_symbols:
        if sym_key in seen:
            continue
        seen.add(sym_key)

        if "::" in sym_key:
            fpath, sym_name = sym_key.split("::", 1)
        else:
            fpath, sym_name = None, sym_key

        block = retriever.get_symbol_block(sym_name, fpath)
        if not block:
            continue

        # Only include header: name, kind, file, signature
        sig = block.get("signature", "") or ""
        header = (
            f"  {sym_name} [{block['kind']}] in {block['file']} "
            f"(L{block['lines']})"
        )
        if sig:
            header += f"  — {sig.strip()}"

        header_tokens = len(header) // 4
        if tokens_used + header_tokens > token_budget:
            break

        bundle_parts.append(header)
        tokens_used += header_tokens
        visited.append(sym_key)

    if not bundle_parts:
        return "", visited

    bundle = "SYMBOL SIGNATURES (top semantic matches):\n" + "\n".join(bundle_parts)
    return bundle, visited


def _find_related_tests(db_path: Path, file_path: str, project_root: Path) -> list[dict]:
    """Find test symbols that likely test the given file.
    
    Strategy:
    1. Find symbols in file_path
    2. Search symbol_calls/deps for test files that call those symbols
    3. Fallback: filename heuristic (test_{basename})
    
    Returns: list of {test_file, test_name, test_docstring}
    """
    import sqlite3
    import json
    tests = []
    file_path_norm = file_path.replace("\\", "/")
    
    try:
        with sqlite3.connect(str(db_path)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT id FROM files WHERE path = ? OR path = ?", 
                               (file_path_norm, file_path_norm.replace("/", "\\"))).fetchone()
            if row:
                file_id = row["id"]
                syms = [r["name"] for r in conn.execute("SELECT name FROM symbols WHERE file_id = ?", (file_id,)).fetchall()]
                if syms:
                    placeholders = ",".join("?" * len(syms))
                    test_rows = conn.execute(f"""
                        SELECT DISTINCT f.path 
                        FROM deps d
                        JOIN files f ON d.from_file_id = f.id
                        WHERE d.to_name IN ({placeholders}) AND (f.path LIKE '%test%' OR f.path LIKE '%tests%')
                    """, syms).fetchall()
                    for tr in test_rows:
                        tests.append(tr["path"].replace("\\", "/"))
            
            basename = Path(file_path_norm).stem
            possible_patterns = [
                f"test_{basename}.py",
                f"{basename}_test.py",
                f"tests/test_{basename}.py",
                f"test_{basename}.ts",
                f"test_{basename}.js",
            ]
            for p in possible_patterns:
                row_h = conn.execute("SELECT path FROM files WHERE path LIKE ?", (f"%{p}",)).fetchone()
                if row_h:
                    tpath = row_h["path"].replace("\\", "/")
                    if tpath not in tests:
                        tests.append(tpath)
                        
            test_oracles = []
            for tpath in list(set(tests))[:3]:
                rows = conn.execute("""
                    SELECT s.name, s.line_start, s.line_end
                    FROM symbols s
                    JOIN files f ON s.file_id = f.id
                    WHERE (f.path = ? OR f.path = ?) AND s.kind = 'function' AND s.name LIKE 'test_%'
                    LIMIT 5
                """, (tpath, tpath.replace("/", "\\"))).fetchall()
                
                for r in rows:
                    docstring = ""
                    abs_tpath = project_root / tpath
                    if abs_tpath.exists():
                        try:
                            file_lines = abs_tpath.read_text(encoding="utf-8", errors="replace").splitlines()
                            func_lines = file_lines[r["line_start"]-1:r["line_end"]]
                            func_body = "\n".join(func_lines)
                            m = re.search(r'"""(.*?)"""', func_body, re.DOTALL)
                            if not m:
                                m = re.search(r"'''(.*?)'''", func_body, re.DOTALL)
                            if m:
                                docstring = m.group(1).strip().split("\n")[0]
                        except Exception:
                            pass
                    
                    test_oracles.append({
                        "test_file": tpath,
                        "test_name": r["name"],
                        "docstring": docstring or "Asserts behavior"
                    })
            return test_oracles[:5]
    except Exception as e:
        import logging
        logging.getLogger(__name__).debug(f"Failed to find related tests for {file_path}: {e}")
        
    return []

