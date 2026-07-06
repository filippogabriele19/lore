from __future__ import annotations
import os
import sys
import json
import logging

logger = logging.getLogger(__name__)
import re
import shutil
import webbrowser
import threading
import http.server
import socketserver
import socket
from pathlib import Path
from rich.text import Text
from rich.panel import Panel

from cli.shared import console, STAGE_SUBDIR, _get_db_path
from cli.html_builders import (
    _read_template,
    _build_diff_html,
    _build_impact_html,
    _build_audit_html,
    _build_unified_dashboard_html,
)


def _serve_diff_ui(task: str, result: dict, port: int = 8080) -> None:
    """Serve the unified dashboard in diff review mode."""
    project_root = Path(result["diff_path"]).parent.parent.parent
    db_path = _get_db_path(project_root)
    extra_data = {"diff_result": result, "task": task}
    _serve_console(project_root, db_path, "diff", port, extra_data)


def _serve_impact_ui(task: str, project_root: Path, db_path: Path, port: int = 8080) -> None:
    """Serve the impact analysis dashboard."""
    extra_data = {"task": task}
    _serve_console(project_root, db_path, "impact", port, extra_data)


def _get_graph_data(db_path: Path) -> dict:
    """Extract nodes (files) and edges (structural deps + virtual coupling) from SQLite."""
    import sqlite3 as _sq
    nodes = []
    edges = []
    
    if not db_path.exists():
        return {"nodes": nodes, "edges": edges}
        
    try:
        with _sq.connect(str(db_path)) as conn:
            conn.row_factory = _sq.Row
            
            hotspots_rows = conn.execute("SELECT file_path FROM hotspots WHERE risk_score > 0.5").fetchall()
            hotspot_paths = {r["file_path"].replace("\\", "/").lower() for r in hotspots_rows}
            
            dl_symbols = set()
            try:
                dl_rows = conn.execute("SELECT symbol_name FROM decision_links").fetchall()
                dl_symbols = {r["symbol_name"].lower() for r in dl_rows}
            except Exception:
                pass
                
            files = conn.execute("SELECT id, path, lines FROM files").fetchall()
            for f in files:
                rel_path = f["path"]
                rel_norm = rel_path.replace("\\", "/")
                rel_norm_lower = rel_norm.lower()
                basename = rel_norm.split("/")[-1]
                basename_lower = basename.lower()
                
                parts = [p for p in rel_norm.split("/") if p and p != "."]
                group = parts[0] if parts else "root"
                
                is_hotspot = rel_norm_lower in hotspot_paths or rel_path.replace("\\", "/").lower() in hotspot_paths
                has_dl = rel_norm_lower in dl_symbols or basename_lower in dl_symbols
                
                nodes.append({
                    "id": rel_norm,
                    "label": basename,
                    "title": f"File: {rel_norm}<br>Lines: {f['lines']}" + ("<br>⚠️ <b>Hotspot File</b>" if is_hotspot else "") + ("<br>🔗 <b>Has Decision Links</b>" if has_dl else ""),
                    "group": group,
                    "value": max(10, f["lines"]),
                    "is_hotspot": is_hotspot,
                    "has_dl": has_dl
                })
                
            deps = conn.execute("""
                SELECT DISTINCT f_from.path as from_path, f_to.path as to_path 
                FROM deps d
                JOIN files f_from ON d.from_file_id = f_from.id
                JOIN symbols s_to ON d.to_name = s_to.name
                JOIN files f_to ON s_to.file_id = f_to.id
                WHERE from_path != to_path AND s_to.kind IN ('class', 'function')
            """).fetchall()
            
            seen_edges = set()
            for d in deps:
                fr = d["from_path"].replace("\\", "/")
                to = d["to_path"].replace("\\", "/")
                edge_key = (fr, to, "structural")
                if edge_key not in seen_edges:
                    edges.append({
                        "from": fr,
                        "to": to,
                        "type": "structural",
                        "arrows": "to",
                        "color": {"color": "#475569", "opacity": 0.3},
                        "dashes": True,
                        "title": "Structural Dependency (Import/Call)"
                    })
                    seen_edges.add(edge_key)
                    
            try:
                ves = conn.execute("""
                    SELECT src_file, dst_file, co_change_rate, shared_commits 
                    FROM virtual_edges
                """).fetchall()
                for ve in ves:
                    fr = ve["src_file"].replace("\\", "/")
                    to = ve["dst_file"].replace("\\", "/")
                    edge_key = (fr, to, "virtual")
                    if edge_key not in seen_edges:
                        edges.append({
                            "from": fr,
                            "to": to,
                            "type": "virtual",
                            "val_coupling": ve["co_change_rate"],
                            "width": max(1, int(ve["co_change_rate"] * 5)),
                            "color": {"color": "#6366F1", "opacity": 0.8},
                            "title": f"Virtual Edge (Co-changes: {ve['shared_commits']} commits, Coupling: {int(ve['co_change_rate']*100)}%)"
                        })
                        seen_edges.add(edge_key)
            except Exception:
                pass
    except Exception as e:
        print(f"[error] Failed to fetch graph data: {e}")
        
    return {"nodes": nodes, "edges": edges}


def _get_console_data_dict(project_name: str, project_root: Path, db_path: Path, mode: str, extra_data: dict) -> dict:
    from cli.audit_runner import _run_full_audit
    
    graph_data = _get_graph_data(db_path)
    
    audit_res = extra_data.get("audit_results")
    if not audit_res:
        try:
            audit_res = _run_full_audit(db_path)
        except Exception:
            audit_res = {"findings": [], "stats": {}}
            
    findings = audit_res.get("findings", [])
    stats = audit_res.get("stats", {})
    stats["files"] = len(graph_data["nodes"])
    stats["virtual_edges"] = sum(1 for e in graph_data["edges"] if e["type"] == "virtual")
    
    diff_res = extra_data.get("diff_result")
    diff_payload = None
    if diff_res:
        diff_payload = {
            "task": extra_data.get("task", ""),
            "diff": diff_res.get("diff", ""),
            "staged_files": diff_res.get("staged_files", []),
            "stats": diff_res.get("stats", {})
        }
        
    cve_payload = extra_data.get("cve_results")
    batch_payload = extra_data.get("batch_results")
    
    commits_reasoning = []
    decision_links = []
    try:
        import sqlite3 as _sq
        with _sq.connect(str(db_path)) as conn:
            conn.row_factory = _sq.Row
            rows = conn.execute("SELECT commit_hash, author, date, body, keywords_found, files_touched FROM commit_reasoning ORDER BY date DESC LIMIT 20").fetchall()
            for r in rows:
                commits_reasoning.append({
                    "hash": r["commit_hash"][:8] if r["commit_hash"] else "",
                    "author": r["author"],
                    "date": r["date"],
                    "body": r["body"],
                    "keywords": json.loads(r["keywords_found"]) if r["keywords_found"] else [],
                    "files": json.loads(r["files_touched"]) if r["files_touched"] else []
                })
            try:
                dl_rows = conn.execute("SELECT symbol_name, source_type, source_ref, confidence, description FROM decision_links ORDER BY id DESC").fetchall()
                for r in dl_rows:
                    decision_links.append({
                        "symbol_name": r["symbol_name"],
                        "source_type": r["source_type"],
                        "source_ref": r["source_ref"],
                        "confidence": r["confidence"],
                        "description": r["description"]
                    })
            except Exception:
                pass
    except Exception:
        pass
        
    backfill_status = "idle"
    backfill_progress = {"processed": 0, "total": 0, "percentage": 0.0}
    try:
        with _sq.connect(str(db_path)) as conn:
            conn.row_factory = _sq.Row
            row_status = conn.execute("SELECT value FROM meta WHERE key = 'git_backfill_status'").fetchone()
            if row_status:
                backfill_status = row_status["value"]
            row_progress = conn.execute("SELECT value FROM meta WHERE key = 'git_backfill_progress'").fetchone()
            if row_progress:
                backfill_progress = json.loads(row_progress["value"])
    except Exception:
        pass

    return {
        "project_name": project_name,
        "mode": mode,
        "stats": stats,
        "findings": findings,
        "graph": graph_data,
        "diff": diff_payload,
        "cve": cve_payload,
        "batch": batch_payload,
        "commits_reasoning": commits_reasoning,
        "decision_links": decision_links,
        "git_backfill": {
            "status": backfill_status,
            "progress": backfill_progress
        }
    }


def _serve_console(project_root: Path, db_path: Path, mode: str, port: int = 8080, extra_data: dict | None = None) -> None:
    """Unified LORE Developer Console HTTP Server."""
    from datetime import datetime
    
    if extra_data is None:
        extra_data = {}

    # Start background git backfill thread if needed
    import sqlite3 as _sq
    from core.git_miner import GitMiner
    
    db_completed = False
    try:
        with _sq.connect(str(db_path)) as conn:
            row_status = conn.execute("SELECT value FROM meta WHERE key = 'git_backfill_status'").fetchone()
            if row_status and row_status["value"] == "completed":
                db_completed = True
    except Exception:
        pass
        
    if not db_completed:
        def run_bg_backfill():
            try:
                miner = GitMiner(str(project_root), str(db_path))
                miner.run_backfill()
            except Exception as e:
                logger.error(f"Background git history backfill thread failed: {e}")
                
        threading.Thread(target=run_bg_backfill, daemon=True).start()
        
    project_name = project_root.name
    
    html_content = _build_unified_dashboard_html(project_name, project_root, db_path, mode, extra_data)
    
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = project_root / ".lore" / "fow_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    
    if mode == "audit":
        archive_name = f"audit_{ts}.html"
    elif mode == "diff":
        archive_name = f"diff_{ts}.html"
    elif mode == "cve":
        archive_name = f"cve_{extra_data.get('cve_id', 'unknown')}_{ts}.html"
    elif mode == "batch":
        archive_name = f"batch_{ts}.html"
    else:
        archive_name = f"console_{ts}.html"
        
    archive_path = log_dir / archive_name
    try:
        archive_path.write_text(html_content, encoding="utf-8")
        print(f"[console] Archive saved -> {archive_path}")
    except Exception as e:
        print(f"[console] Failed to save archive: {e}")
        
    class _ConsoleHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
            
        def do_GET(self):
            if self.path == "/api/data":
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                payload = _get_console_data_dict(project_name, project_root, db_path, mode, extra_data)
                self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
            else:
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(html_content.encode("utf-8"))
                
        def do_POST(self):
            if self.path == "/api/diff/apply":
                success, msg = self._apply_staged_diff()
                self._send_json({"success": success, "message": msg})
            elif self.path == "/api/diff/discard":
                success, msg = self._discard_staged_diff()
                self._send_json({"success": success, "message": msg})
            else:
                self.send_response(404)
                self.end_headers()
                
        def _send_json(self, data):
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.end_headers()
            self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))
            
        def _apply_staged_diff(self) -> tuple[bool, str]:
            diff_res = extra_data.get("diff_result", {})
            staged_files = diff_res.get("staged_files", [])
            
            if not staged_files:
                stage_dir = project_root / STAGE_SUBDIR
                if not stage_dir.exists():
                    return False, "No staged files found."
                copied = 0
                for p in stage_dir.rglob("*"):
                    if p.is_file():
                        rel = p.relative_to(stage_dir)
                        dst = project_root / rel
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(p, dst)
                        copied += 1
                return True, f"Applied {copied} files to project root."
                
            try:
                for f in staged_files:
                    src = Path(f["staged"])
                    dst = project_root / f["path"]
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                stage_dir = project_root / STAGE_SUBDIR
                if stage_dir.exists():
                    shutil.rmtree(stage_dir)
                return True, f"Successfully applied {len(staged_files)} modified files to project root."
            except Exception as e:
                return False, f"Error copying files: {e}"
                
        def _discard_staged_diff(self) -> tuple[bool, str]:
            try:
                stage_dir = project_root / STAGE_SUBDIR
                if stage_dir.exists():
                    shutil.rmtree(stage_dir)
                return True, "Staged changes discarded."
            except Exception as e:
                return False, f"Error clearing stage: {e}"

    class ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

    while True:
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", port), _ConsoleHandler)
            break
        except OSError:
            port += 1
            
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    
    url = f"http://127.0.0.1:{port}/"
    server_text = Text()
    server_text.append("Developer Console is active!\n\n", style="bold green")
    server_text.append("URL:        ", style="bold")
    server_text.append(f"{url}\n", style="underline cyan")
    server_text.append("Mode:       ", style="bold")
    server_text.append(f"{mode.upper()}\n", style="bold magenta")
    server_text.append("Database:   ", style="bold")
    server_text.append(f"{db_path}\n\n", style="info")
    server_text.append("Press ", style="dim")
    server_text.append("Ctrl+C", style="bold red")
    server_text.append(" to close the console server.", style="dim")
    
    console.print()
    console.print(Panel(
        server_text,
        title="[bold white]LORE DEVELOPER CONSOLE[/]",
        border_style="green",
        padding=(1, 4),
        expand=False
    ))
    
    webbrowser.open(url)
    
    try:
        while thread.is_alive():
            thread.join(timeout=1)
    except KeyboardInterrupt:
        print("\n[console] Shutting down server...")
        srv.shutdown()


def _serve_audit_ui(project_root: Path, db_path: Path, port: int = 8081) -> None:
    """Run full autonomous audit, render HTML report, serve on localhost."""
    _serve_console(project_root, db_path, "audit", port)
