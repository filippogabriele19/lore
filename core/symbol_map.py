#!/usr/bin/env python3
"""
python -m core.symbol_map — Proof of Concept: Code Point Cloud

Facade module for backward compatibility. Imports and re-exports modular parts.
"""
from __future__ import annotations
import ast
import sqlite3
import sys
import os
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional, Any

# Re-export definitions from sub-modules
from core.symbol_types import SymbolInfo
from core.symbol_db import SymbolDB, SCHEMA
from core.symbol_extractor import FileExtractor, extract_file, _extract_ts_file, _extract_go_file
from core.symbol_retriever import SymbolRetriever
from core.symbol_scanner import scan, scan_files, embed_all_symbols, embed_changed, _is_ignored


def print_block(block: dict):
    print(f"\n{'='*60}")
    print(f"SYMBOL  : {block['symbol']}  [{block['kind']}]")
    print(f"FILE    : {block['file']}  (lines {block['lines']})")
    print(f"{'='*60}")
    print("\n--- BODY ---")
    print(block["body"])

    if block["depends_on"]:
        eager = [d for d in block["depends_on"] if not d.get("lazy")]
        lazy  = [d for d in block["depends_on"] if d.get("lazy")]

        if eager:
            print(f"\n--- DEPENDS ON — EAGER ({len(eager)}) ---")
            for d in eager:
                loc = f"  @ {d['location']}" if d["location"] else ""
                print(f"  [{d['type']:14s}] {d['name']}{loc}")
                if d["signature"]:
                    print(f"               > {d['signature'].strip()}")

        if lazy:
            print(f"\n--- DEPENDS ON — LAZY ({len(lazy)}) — usa 'frontier <nome>' per espandere ---")
            for d in lazy:
                loc = f"  @ {d['location']}" if d["location"] else ""
                print(f"  [{d['type']:14s}] {d['name']:35s} [{d['size']} righe]{loc}")
                if d.get("methods"):
                    print(f"               metodi: {', '.join(d['methods'][:8])}"
                          + (f" ... +{len(d['methods'])-8}" if len(d['methods']) > 8 else ""))

    if block["called_by"]:
        print(f"\n--- CALLED BY ({len(block['called_by'])}) ---")
        for c in block["called_by"]:
            caller = c["caller"] or "(module level)"
            print(f"  {caller}  @ {c['file']}:{c['line']}")


def print_compare(result: dict):
    if "error" in result:
        print(f"[error] {result['error']}")
        return
    print(f"\n{'='*60}")
    print(f"CONFRONTO TOKEN — simbolo: {result['symbol']}")
    print(f"{'='*60}")

    print("\n[APPROCCIO VECCHIO — leggi i file interi]")
    for f in result["old_approach"]["files_to_read"]:
        print(f"  {f['file']:60s}  {f['lines']:>5} righe")
    print(f"  {'TOTALE':60s}  {result['old_approach']['total_lines']:>5} righe")
    print(f"  Token stimati: ~{result['old_approach']['token_estimate']:,}")

    print("\n[APPROCCIO NUOVO — point cloud]")
    print(f"  Righe del simbolo target:     {result['new_approach']['symbol_lines']}")
    print(f"  Firme delle dipendenze:       {result['new_approach']['dep_signatures']}")
    print(f"  Token stimati:               ~{result['new_approach']['token_estimate']:,}")

    print(f"\n{'='*60}")
    print(f"  RIDUZIONE CONTESTO:  {result['reduction_pct']}%")
    print(f"{'='*60}\n")


def print_traverse(result: dict):
    if "error" in result:
        print(f"[error] {result['error']}")
        return

    print(f"\n{'='*65}")
    print(f"  TRAVERSAL from: {result['start']}")
    print(f"{'='*65}")

    for hop in result["hops"]:
        indent = "  " + "  " * hop["depth"]
        status = hop.get("status", "ok")
        if status == "not_found":
            print(f"{indent}[hop {hop['hop']}] {hop['symbol']} — NOT FOUND")
            continue

        marker = ">" if hop["depth"] == 0 else "+"
        print(f"\n{indent}[{marker}] {hop['symbol']}  [{hop.get('kind','')}]  "
              f"{hop['file']}:{hop.get('lines',0)} lines")
        print(f"{indent}    reason     : {hop['reason']}")
        print(f"{indent}    token hop  : +{hop['tokens_this_hop']}  "
              f"(cumulative: {hop['cumulative_tokens']})")

        if hop.get("globals_touched"):
            print(f"{indent}    globals    : {', '.join(hop['globals_touched'])}")

        if hop.get("eager_deps"):
            print(f"{indent}    espando -> : {', '.join(hop['eager_deps'])}")
        if hop.get("lazy_deps"):
            for ld in hop["lazy_deps"]:
                methods_hint = ""
                if ld.get("methods"):
                    methods_hint = f"  metodi: {', '.join(ld['methods'][:5])}"
                    if len(ld["methods"]) > 5:
                        methods_hint += f" ... +{len(ld['methods'])-5}"
                print(f"{indent}    [LAZY {ld['size']:>4} righe] {ld['name']:30s}{methods_hint}"
                      f"  << frontier('{ld['name']}') per espandere")

    s = result["summary"]
    print(f"\n{'='*65}")
    print(f"  RIEPILOGO TRAVERSAL")
    print(f"{'='*65}")
    print(f"  Simboli visitati       : {s['symbols_visited']}")
    print(f"  Righe totali lette     : {s['total_lines_read']}")
    print(f"  Token point cloud      : ~{s['total_tokens_point_cloud']:,}")
    print(f"  Token approccio file   : ~{s['total_tokens_file_approach']:,}")
    print(f"  File che avresti letto :")
    for f in s["files_that_would_be_read"]:
        print(f"    - {f}")
    print(f"\n  RIDUZIONE TOTALE       :  {s['reduction_pct']}%")
    print(f"{'='*65}\n")


def print_stats(db: SymbolDB):
    files  = db.con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
    syms   = db.con.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    deps   = db.con.execute("SELECT COUNT(*) FROM deps").fetchone()[0]
    funcs  = db.con.execute("SELECT COUNT(*) FROM symbols WHERE kind='function'").fetchone()[0]
    methods= db.con.execute("SELECT COUNT(*) FROM symbols WHERE kind='method'").fetchone()[0]
    classes= db.con.execute("SELECT COUNT(*) FROM symbols WHERE kind='class'").fetchone()[0]
    globs  = db.con.execute("SELECT COUNT(*) FROM symbols WHERE kind='variable'").fetchone()[0]
    calls  = db.con.execute("SELECT COUNT(*) FROM deps WHERE dep_type='call'").fetchone()[0]
    rg     = db.con.execute("SELECT COUNT(*) FROM deps WHERE dep_type='read_global'").fetchone()[0]
    wg     = db.con.execute("SELECT COUNT(*) FROM deps WHERE dep_type='write_global'").fetchone()[0]
    avg_lines = db.con.execute("SELECT AVG(lines) FROM files WHERE lines > 0").fetchone()[0]

    print(f"\n{'='*50}")
    print(f"  STATISTICHE INDICE")
    print(f"{'='*50}")
    print(f"  File indicizzati   : {files}")
    print(f"  Linee medie/file   : {avg_lines:.0f}" if avg_lines else "")
    print(f"  Simboli totali     : {syms}")
    print(f"    - funzioni       : {funcs}")
    print(f"    - metodi         : {methods}")
    print(f"    - classi         : {classes}")
    print(f"    - variabili glob : {globs}")
    print(f"  Dipendenze totali  : {deps}")
    print(f"    - chiamate       : {calls}")
    print(f"    - letture global : {rg}")
    print(f"    - scritture glob : {wg}")
    print(f"{'='*50}\n")


def print_search(db: SymbolDB, keyword: str):
    rows = db.con.execute(
        "SELECT s.name, s.kind, s.line_start, s.line_end, f.path "
        "FROM symbols s JOIN files f ON s.file_id=f.id "
        "WHERE s.name LIKE ? ORDER BY s.kind, s.name LIMIT 30",
        (f"%{keyword}%",)
    ).fetchall()
    if not rows:
        print(f"Nessun simbolo trovato per '{keyword}'")
        return
    print(f"\n{len(rows)} risultati per '{keyword}':")
    for r in rows:
        print(f"  [{r['kind']:8s}] {r['name']:40s}  {r['path']}:{r['line_start']}-{r['line_end']}")


# ---------------------------------------------------------------------------
# Main CLI
# ---------------------------------------------------------------------------

DEFAULT_PROJECT = os.environ.get("LORE_PROJECT", str(Path.cwd()))


def _get_db_path(project_root: Path) -> Path:
    db_path = project_root / ".lore_poc.db"
    if not db_path.exists() and (project_root / ".lore" / "lore.db").exists():
        return project_root / ".lore" / "lore.db"
    elif not db_path.exists() and (project_root / ".ase_poc.db").exists():
        return project_root / ".ase_poc.db"
    return db_path


def _get_db(project_path: Path) -> SymbolDB:
    db_path = _get_db_path(project_path)
    return SymbolDB(db_path)


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)

    cmd = args[0]
    project_path = Path(DEFAULT_PROJECT)

    if cmd == "scan":
        if len(args) > 1:
            project_path = Path(args[1])
        if not project_path.exists():
            print(f"[error] Path not found: {project_path}")
            sys.exit(1)
        db = _get_db(project_path)
        scan(project_path, db)
        print_stats(db)
        db.close()

    elif cmd == "get":
        if len(args) < 2:
            print("Usage: get <symbol_name>")
            sys.exit(1)
        db = _get_db(project_path)
        retriever = SymbolRetriever(db, project_path)
        block = retriever.get_symbol_block(args[1])
        if block:
            print_block(block)
        else:
            print(f"Symbol '{args[1]}' not found. Try: search <keyword>")
        db.close()

    elif cmd == "frontier":
        # Alias di get — mostra blocco completo con dipendenze
        if len(args) < 2:
            print("Usage: frontier <symbol_name>")
            sys.exit(1)
        db = _get_db(project_path)
        retriever = SymbolRetriever(db, project_path)
        block = retriever.get_symbol_block(args[1])
        if block:
            print_block(block)
        else:
            print(f"Symbol '{args[1]}' not found.")
        db.close()

    elif cmd == "compare":
        if len(args) < 2:
            print("Usage: compare <symbol_name>")
            sys.exit(1)
        db = _get_db(project_path)
        retriever = SymbolRetriever(db, project_path)
        result = retriever.compare(args[1])
        print_compare(result)
        db.close()

    elif cmd == "stats":
        db = _get_db(project_path)
        print_stats(db)
        db.close()

    elif cmd == "expand":
        # Alias esplicito per "espandi un nodo lazy" — identico a frontier ma con messaggio
        if len(args) < 2:
            print("Usage: expand <symbol_name>")
            sys.exit(1)
        print(f"[EXPAND] Explicit expansion of '{args[1]}' (was lazy)")
        db = _get_db(project_path)
        retriever = SymbolRetriever(db, project_path)
        block = retriever.get_symbol_block(args[1])
        if block:
            print_block(block)
        else:
            print(f"Symbol '{args[1]}' not found.")
        db.close()

    elif cmd == "traverse":
        if len(args) < 2:
            print("Usage: traverse <symbol_name> [max_depth=3]")
            sys.exit(1)
        depth = int(args[2]) if len(args) > 2 else 3
        db = _get_db(project_path)
        retriever = SymbolRetriever(db, project_path)
        result = retriever.traverse(args[1], max_depth=depth)
        print_traverse(result)
        db.close()

    elif cmd == "search":
        if len(args) < 2:
            print("Usage: search <keyword>")
            sys.exit(1)
        db = _get_db(project_path)
        print_search(db, args[1])
        db.close()

    else:
        print(f"Unknown command: {cmd}")
        print("Commands: scan | get | frontier | compare | stats | search")
        sys.exit(1)


if __name__ == "__main__":
    main()
