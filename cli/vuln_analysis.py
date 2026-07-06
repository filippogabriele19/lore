from __future__ import annotations
import math
from pathlib import Path
from cli.shared import console


def _calculate_path_risk_score(path_files: list[str], conn, project_root) -> float:
    last_file = path_files[-1]
    base_score = 0.5
    critical_sinks = {"eval", "exec", "system", "popen", "subprocess"}
    moderate_sinks = {"pickle", "yaml", "loads", "deserialize", "sql", "query"}
    
    try:
        rows = conn.execute("""
            SELECT DISTINCT to_name FROM deps 
            WHERE from_file_id = (SELECT id FROM files WHERE path = ?)
              AND dep_type = 'call'
        """, (last_file,)).fetchall()
        calls = {r["to_name"].lower() for r in rows}
        if any(sink in calls for sink in critical_sinks):
            base_score = 0.90
        elif any(sink in calls for sink in moderate_sinks):
            base_score = 0.70
    except Exception:
        pass
        
    first_file = path_files[0]
    source_boost = 0.0
    try:
        has_ast_source = conn.execute("""
            SELECT COUNT(*) FROM symbols 
            WHERE file_id = (SELECT id FROM files WHERE path = ?)
              AND is_source = 1
        """, (first_file,)).fetchone()[0] > 0
        if has_ast_source:
            source_boost = 0.1
    except Exception:
        pass
        
    path_len = len(path_files)
    length_multiplier = max(0.5, 1.0 - (path_len - 1) * 0.05)
    
    sanitization_reduction = 0.0
    for f in path_files:
        abs_p = Path(project_root) / f
        if abs_p.exists():
            try:
                code = abs_p.read_text(encoding="utf-8", errors="replace").lower()
                if any(k in code for k in ("int(", "float(", "isdigit()", "isinstance(", "validate_", "sanitize_")):
                    sanitization_reduction += 0.2
            except Exception:
                pass
                
    score = (base_score + source_boost) * length_multiplier - sanitization_reduction
    return max(0.05, min(1.0, score))


def _trace_file_taint(fpath: str, code_txt: str, current_taint_sources: set[str]) -> dict:
    suffix = Path(fpath).suffix.lower()
    if suffix == ".py":
        from core.ast_taint import PythonASTTaintTracer
        tracer = PythonASTTaintTracer(code_txt, current_taint_sources)
    elif suffix == ".go":
        from parsers.go_taint_tracer import GoASTTaintTracer
        tracer = GoASTTaintTracer(code_txt, current_taint_sources)
    elif suffix in (".ts", ".tsx", ".js", ".jsx"):
        from parsers.ts_taint_tracer import TSASTTaintTracer
        tracer = TSASTTaintTracer(code_txt, current_taint_sources)
    else:
        return {"flows": [], "outgoing_calls": []}
    return tracer.trace()



def _run_vuln_analysis(project_root: Path, conn) -> dict:
    """Run the core vulnerability, amnesia, and decay analysis against the SQLite KG."""
    # 1. Fetch files and identify Sources / Sinks
    files_map = {}
    try:
        rows = conn.execute("SELECT id, path FROM files").fetchall()
        files_map = {r["id"]: r["path"].replace("\\", "/") for r in rows}
    except Exception as e:
        console.print(f"[error]Failed to read files table: {e}[/]")
        raise e

    source_patterns = ["view", "handler", "api", "route", "controller", "request", "middleware"]
    sink_names = {
        "eval", "exec", "execute", "system", "popen", "subprocess", "pickle", "yaml", 
        "dumps", "dumpd", "loads", "loadd", "serialize", "deserialize", "sql", "query"
    }

    sources = {}
    try:
        rows_src = conn.execute("SELECT DISTINCT file_id FROM symbols WHERE is_source = 1").fetchall()
        for r in rows_src:
            f_id = r["file_id"]
            if f_id in files_map:
                sources[f_id] = files_map[f_id]
    except Exception as e:
        console.print(f"[warning]⚠️ Failed to load AST-based sources: {e}[/]")

    for f_id, path in files_map.items():
        path_lower = path.lower()
        if any(pat in path_lower for pat in source_patterns):
            sources[f_id] = path

    sinks = {}
    try:
        rows = conn.execute("SELECT DISTINCT from_file_id, to_name FROM deps WHERE dep_type = 'call'").fetchall()
        for r in rows:
            to_name_lower = r["to_name"].lower()
            if to_name_lower in sink_names:
                f_id = r["from_file_id"]
                if f_id in files_map:
                    sinks[f_id] = files_map[f_id]
    except Exception as e:
        console.print(f"[warning]⚠️ Failed to identify files calling sinks: {e}[/]")

    # 2. Build file-level call graph
    graph = {}
    try:
        rows = conn.execute("""
            SELECT DISTINCT s_from.file_id AS from_id, s_to.file_id AS to_id
            FROM deps d
            JOIN symbols s_from ON d.from_symbol_id = s_from.id
            JOIN symbols s_to ON d.to_name = s_to.name
            WHERE d.dep_type = 'call'
              AND s_to.kind IN ('function', 'method', 'class')
        """).fetchall()
        for r in rows:
            from_id = r["from_id"]
            to_id = r["to_id"]
            if from_id not in graph:
                graph[from_id] = set()
            graph[from_id].add(to_id)
    except Exception as e:
        console.print(f"[warning]⚠️ Failed to build file-level call graph: {e}[/]")

    # 3. BFS Taint Propagation
    exposed_paths = []
    for src_id, src_path in sources.items():
        queue = [(src_id, [src_id])]
        visited = {src_id}
        while queue:
            curr_id, path_ids = queue.pop(0)
            if curr_id in sinks:
                exposed_paths.append([files_map[pid] for pid in path_ids])
                if len(exposed_paths) >= 15:
                    break
            
            neighbors = graph.get(curr_id, [])
            for n_id in neighbors:
                if n_id not in visited:
                    visited.add(n_id)
                    queue.append((n_id, path_ids + [n_id]))
        if len(exposed_paths) >= 15:
            break

    # 4. Sink Amnesia check
    amnesia_hotspots = []
    for f_id, f_path in sinks.items():
        row_h = conn.execute(
            "SELECT change_freq, risk_score FROM hotspots WHERE file_path = ? OR file_path = ?",
            (f_path, f_path.replace("/", "\\"))
        ).fetchone()
        if row_h:
            change_freq = row_h["change_freq"]
            try:
                dl_count = conn.execute(
                    "SELECT COUNT(*) FROM decision_links dl "
                    "JOIN symbols s ON dl.symbol_name = s.name "
                    "JOIN files f2 ON s.file_id = f2.id "
                    "WHERE f2.path = ? OR f2.path = ?", (f_path, f_path.replace("/", "\\"))
                ).fetchone()[0]
            except Exception as e:
                console.print(f"[warning]⚠️ Failed to count decision links for {f_path}: {e}[/]")
                dl_count = 0
            
            n = max(1, change_freq)
            prior = 1.0 / (1.0 + math.exp(-(n / 15.0 - 1.5)))
            if dl_count > 0:
                posterior = prior * (0.2 ** dl_count)
            else:
                posterior = min(1.0, prior * 1.5)
                
            se = math.sqrt((posterior * (1.0 - posterior)) / n)
            me = 1.96 * se
            ci_lower = max(0.0, posterior - me)
            ci_upper = min(1.0, posterior + me)
            
            if change_freq >= 10 and dl_count == 0:
                amnesia_hotspots.append({
                    "path": f_path,
                    "change_freq": change_freq,
                    "adr_count": dl_count,
                    "bayes_risk": posterior,
                    "ci": (ci_lower, ci_upper)
                })

    amnesia_hotspots.sort(key=lambda x: x["bayes_risk"], reverse=True)

    # 5. Architectural Decay (Implicit Drift)
    decay_events = []
    risk_keywords = ["workaround", "temporary", "hack", "bypass", "quick fix", "hotfix", "regression", "disabled", "security"]
    try:
        rows_cr = conn.execute("SELECT commit_hash, author, date, body, files_touched FROM commit_reasoning").fetchall()
        for r in rows_cr:
            body_lower = r["body"].lower() if r["body"] else ""
            if any(kw in body_lower for kw in risk_keywords):
                touched = r["files_touched"] or ""
                touched_files = [tf.strip().replace("\\", "/") for tf in touched.split(",") if tf.strip()]
                touches_sink = any(any(sp in tf for sp in sinks.values()) for tf in touched_files)
                if touches_sink:
                    h = r["commit_hash"]
                    has_dl = conn.execute("SELECT COUNT(*) FROM decision_links WHERE source_ref = ?", (h,)).fetchone()[0] > 0
                    if not has_dl:
                        decay_events.append({
                            "hash": h,
                            "author": r["author"],
                            "date": r["date"],
                            "body": r["body"].strip().split("\n")[0],
                            "files": touched_files
                        })
    except Exception as e:
        console.print(f"[warning]⚠️ Failed to check architectural decay: {e}[/]")

    return {
        "files_map": files_map,
        "sinks": sinks,
        "exposed_paths": exposed_paths,
        "amnesia_hotspots": amnesia_hotspots,
        "decay_events": decay_events
    }
