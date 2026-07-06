import sys
import json
import urllib.parse
import hashlib
import threading
import time
from pathlib import Path
from cli.shared import _get_db_path
from core.symbol_map import SymbolDB, scan_files, embed_changed
from core.ast_patcher import check_ast_taint_interprocedural
from cli.vuln_analysis import _run_vuln_analysis

def log(msg: str):
    sys.stderr.write(f"[LORE LSP] {msg}\n")
    sys.stderr.flush()

def uri_to_path(uri: str) -> Path:
    parsed = urllib.parse.urlparse(uri)
    path_str = urllib.parse.unquote(parsed.path)
    if path_str.startswith('/') and ':' in path_str:
        path_str = path_str.lstrip('/')
    return Path(path_str)

def _main_lsp(argv: list[str] | None = None) -> None:
    log("Starting LORE LSP Server...")
    project_root, db_path, watcher_thread = None, None, None
    shutdown_event = threading.Event()
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    stdout_lock = threading.Lock()
    
    _db_lock = threading.Lock()
    _analysis_result = None
    _analysis_hash = ""
    _debounce_timers = {}
    DEBOUNCE_SECONDS = 0.0 if "pytest" in sys.modules else 2.0

    def send_message(payload: dict):
        body = json.dumps(payload).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        with stdout_lock:
            stdout.write(header + body)
            stdout.flush()

    def _get_cached_analysis(proj_root: Path, database_path: Path) -> dict:
        nonlocal _analysis_result, _analysis_hash
        h = hashlib.md5()
        for f in sorted(proj_root.rglob("*.py")):
            if any(x in str(f) for x in (".venv", "venv", ".lore", "__pycache__")):
                continue
            try:
                h.update(str(f.stat().st_mtime).encode())
            except OSError:
                pass
        current_hash = h.hexdigest()
        
        if _analysis_result is not None and _analysis_hash == current_hash:
            return _analysis_result
        
        with _db_lock:
            db = SymbolDB(database_path)
            try:
                result = _run_vuln_analysis(proj_root, db.con)
            finally:
                db.close()
        _analysis_result, _analysis_hash = result, current_hash
        return result

    def publish_diagnostics(uri: str, file_path: Path):
        nonlocal project_root, db_path
        if not project_root or not db_path:
            return
        rel_path = str(file_path.relative_to(project_root)).replace("\\", "/")
        log(f"Publishing diagnostics for {rel_path}...")
        diagnostics = []
        try:
            analysis_res = _get_cached_analysis(project_root, db_path)
            exposed_paths = analysis_res.get("exposed_paths", [])
            is_exposed = any(rel_path in path for path in exposed_paths)
            if is_exposed:
                code_txt = file_path.read_text(encoding="utf-8", errors="replace")
                res = check_ast_taint_interprocedural(code_txt, set())
                for flow in res.get("flows", []):
                    diagnostics.append({
                        "range": {
                            "start": {"line": flow["source_line"] - 1, "character": 0},
                            "end": {"line": flow["source_line"] - 1, "character": 80}
                        },
                        "severity": 2,
                        "code": "LORE-TAINT-SOURCE",
                        "source": "LORE",
                        "message": f"Taint Source: Input '{flow['var_name']}' is untrusted"
                    })
                    diagnostics.append({
                        "range": {
                            "start": {"line": flow["sink_line"] - 1, "character": 0},
                            "end": {"line": flow["sink_line"] - 1, "character": 80}
                        },
                        "severity": 1,
                        "code": "LORE-TAINT-SINK",
                        "source": "LORE",
                        "message": f"Taint Sink: '{flow['var_name']}' flows into {flow['sink_name']}()"
                    })
        except Exception as e:
            log(f"Error computing diagnostics: {e}")
            
        send_message({
            "jsonrpc": "2.0",
            "method": "textDocument/publishDiagnostics",
            "params": {"uri": uri, "diagnostics": diagnostics}
        })

    def _schedule_diagnostics(uri: str, file_path: Path):
        nonlocal _debounce_timers
        if uri in _debounce_timers:
            _debounce_timers[uri].cancel()
        if DEBOUNCE_SECONDS == 0.0:
            publish_diagnostics(uri, file_path)
            return
        timer = threading.Timer(DEBOUNCE_SECONDS, publish_diagnostics, args=[uri, file_path])
        timer.daemon = True
        _debounce_timers[uri] = timer
        timer.start()

    def file_watcher_loop():
        log("FileWatcher background thread started.")
        from core.scanner.file_watcher import FileWatcher
        time.sleep(0.5)
        if not project_root or not db_path:
            return
        watcher = FileWatcher(project_root, db_path, interval=1.0)
        db = SymbolDB(db_path)
        try:
            while not shutdown_event.is_set():
                changed = watcher.check_changes()
                if changed:
                    processed = scan_files(db, project_root, changed)
                    embed_changed(db, project_root, changed)
                    log(f"Watcher thread: processed {processed} files.")
                    for rel in changed:
                        abs_p = project_root / rel
                        if abs_p.exists():
                            _schedule_diagnostics(abs_p.as_uri(), abs_p)
                for _ in range(10):
                    if shutdown_event.is_set():
                        break
                    time.sleep(0.1)
        except Exception as err:
            log(f"Error in FileWatcher: {err}")
        finally:
            db.close()
            log("FileWatcher background thread stopped.")

    try:
        while True:
            header_line = b""
            while b"\r\n\r\n" not in header_line:
                char = stdin.read(1)
                if not char:
                    return
                header_line += char
                if len(header_line) > 1024:
                    return
            
            content_length = 0
            for line in header_line.decode("ascii").split("\r\n"):
                if line.lower().startswith("content-length:"):
                    content_length = int(line.split(":")[1].strip())
            if content_length == 0:
                continue
                
            body = b""
            while len(body) < content_length:
                chunk = stdin.read(content_length - len(body))
                if not chunk:
                    return
                body += chunk
                
            msg = json.loads(body.decode("utf-8"))
            method, msg_id = msg.get("method"), msg.get("id")
            log(f"Received request/notification: {method}")
            
            if method == "initialize":
                params = msg.get("params", {})
                root_uri, root_path = params.get("rootUri"), params.get("rootPath")
                project_root = uri_to_path(root_uri) if root_uri else (Path(root_path) if root_path else Path.cwd())
                db_path = _get_db_path(project_root)
                log(f"Initialized LSP server for: {project_root}")
                shutdown_event.clear()
                watcher_thread = threading.Thread(target=file_watcher_loop, daemon=True)
                watcher_thread.start()
                send_message({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {"capabilities": {"textDocumentSync": 1, "hoverProvider": True}}
                })
            elif method == "initialized":
                pass
            elif method in ("textDocument/didOpen", "textDocument/didSave", "textDocument/didChange"):
                params = msg.get("params", {})
                doc = params.get("textDocument", {})
                uri = doc.get("uri")
                if uri:
                    fpath = uri_to_path(uri)
                    try:
                        rel_path = str(fpath.relative_to(project_root))
                        if method != "textDocument/didChange":
                            log(f"Rescanning: {rel_path}")
                            db = SymbolDB(db_path)
                            try:
                                scan_files(db, project_root, [rel_path])
                                embed_changed(db, project_root, [rel_path])
                            finally:
                                db.close()
                            _schedule_diagnostics(uri, fpath)
                    except Exception as e:
                        log(f"Failed to scan file: {e}")
            elif method == "textDocument/hover":
                params = msg.get("params", {})
                doc = params.get("textDocument", {})
                uri = doc.get("uri")
                pos = params.get("position", {})
                line = pos.get("line", 0) + 1
                hover_content = None
                if uri and project_root and db_path:
                    fpath = uri_to_path(uri)
                    try:
                        rel_path = str(fpath.relative_to(project_root)).replace("\\", "/")
                        with _db_lock:
                            db = SymbolDB(db_path)
                            try:
                                row_sym = db.con.execute("""
                                    SELECT s.name, s.kind, s.signature
                                    FROM symbols s JOIN files f ON s.file_id=f.id
                                    WHERE (f.path=? OR f.path=?) AND ? BETWEEN s.line_start AND s.line_end
                                    ORDER BY s.kind LIMIT 1
                                """, (rel_path, rel_path.replace("/", "\\"), line)).fetchone()
                                if row_sym:
                                    sym_name = row_sym["name"]
                                    rows_dl = db.con.execute("""
                                        SELECT source_ref, description, confidence FROM decision_links
                                        WHERE symbol_name=?
                                    """, (sym_name,)).fetchall()
                                    if rows_dl:
                                        md = [f"### 🛡️ LORE Architectural Decision Links: `{sym_name}`\n"]
                                        for dl in rows_dl:
                                            md.append(f"- **Decision**: {dl['description']}")
                                            md.append(f"  - Ref: `{dl['source_ref']}`")
                                            md.append(f"  - Confidence: {dl['confidence'] * 100:.1f}%\n")
                                        hover_content = "\n".join(md)
                            finally:
                                db.close()
                    except Exception as e:
                        log(f"Error on hover search: {e}")
                send_message({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {"contents": {"kind": "markdown", "value": hover_content}} if hover_content else None
                })
            elif method == "lore/getStats":
                stats = {"symbols": 0, "decision_links": 0, "hotspots": 0, "taint_paths": 0}
                if db_path and db_path.exists():
                    with _db_lock:
                        db = SymbolDB(db_path)
                        try:
                            stats = db.get_stats()
                        except Exception as e:
                            log(f"Error in lore/getStats: {e}")
                        finally:
                            db.close()
                send_message({"jsonrpc": "2.0", "id": msg_id, "result": stats})
            elif method in ("shutdown", "exit"):
                shutdown_event.set()
                if watcher_thread and watcher_thread.is_alive():
                    watcher_thread.join(timeout=2.0)
                send_message({"jsonrpc": "2.0", "id": msg_id, "result": None})
                if method == "exit":
                    log("LSP exiting.")
                    sys.exit(0)
    except Exception as e:
        log(f"Error in loop: {e}")
    finally:
        shutdown_event.set()
        if watcher_thread and watcher_thread.is_alive():
            watcher_thread.join(timeout=2.0)
