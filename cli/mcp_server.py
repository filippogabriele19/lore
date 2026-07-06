import sys
import json
import argparse
import re
from pathlib import Path
from cli.shared import console, _get_db_path
from core.symbol_map import SymbolDB, scan as fow_scan
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("LORE")

def log(msg: str):
    sys.stderr.write(f"[LORE MCP] {msg}\n")
    sys.stderr.flush()

def _resolve_project_db(project_path_str: str | None = None) -> tuple[Path, Path]:
    p_path = Path(project_path_str or ".").resolve()
    if not p_path.exists():
        p_path = Path(".").resolve()
    db_p = _get_db_path(p_path)
    
    if db_p.exists():
        from core.symbol_db import SymbolDB
        try:
            db = SymbolDB(db_p)
            db.close()
        except Exception:
            pass
    return p_path, db_p

def _git(project_root: Path, *args) -> str:
    import subprocess
    result = subprocess.run(
        ["git"] + list(args),
        cwd=str(project_root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        return ""
    return result.stdout

@mcp.tool()
def lore_trace_taint(project_path: str, file_path: str | None = None) -> str:
    """Predictive taint flow analysis showing paths from Source to Sink."""
    try:
        project_root, db_path = _resolve_project_db(project_path)
        if not db_path.exists():
            return f"Error: Database not found at {db_path}."
        
        from core.symbol_db import SymbolDB
        db = SymbolDB(db_path)
        try:
            from cli.vuln_analysis import _run_vuln_analysis
            res = _run_vuln_analysis(project_root, db.con)
            exposed = res.get("exposed_paths", [])
        finally:
            db.close()
            
        if not exposed:
            return "No active taint paths detected in project."
            
        if file_path:
            fp_norm = file_path.replace("\\", "/").lower()
            exposed = [p for p in exposed if any(fp_norm in f.lower() for f in p)]
            if not exposed:
                return f"No active taint paths found involving file: {file_path}"
                
        out = []
        for i, path in enumerate(exposed, 1):
            out.append(f"Path {i}: {' -> '.join(path)}")
        return "\n".join(out)
    except Exception as e:
        return f"Error during taint analysis: {e}"

@mcp.tool()
def lore_get_symbol_context(project_path: str, symbol_name: str) -> str:
    """Get symbol definitions, callers, dependencies, and linked ADRs."""
    try:
        project_root, db_path = _resolve_project_db(project_path)
        if not db_path.exists():
            return f"Error: Database not found at {db_path}."
        
        from core.symbol_map import SymbolDB, SymbolRetriever
        db = SymbolDB(db_path)
        try:
            retriever = SymbolRetriever(db, project_root)
            block = retriever.get_symbol_block(symbol_name)
            
            if not block:
                return f"Symbol '{symbol_name}' not found in Knowledge Graph."
                
            adrs = db.con.execute(
                "SELECT source_ref, confidence, description FROM decision_links WHERE symbol_name=?",
                (symbol_name,)
            ).fetchall()
        finally:
            db.close()
        
        out = [f"Symbol: {block['symbol']} [{block['kind']}]", f"Defined in: {block['file']} (lines {block['lines']})", "\n--- Code Body ---", block["body"]]
        if block["depends_on"]:
            out.append("\n--- Direct Dependencies ---")
            for d in block["depends_on"]:
                loc = f" @ {d['location']}" if d['location'] else ""
                out.append(f"  - [{d['type']}] {d['name']} ({d['kind']}){loc}")
        if block["called_by"]:
            out.append("\n--- Callers ---")
            for c in block["called_by"]:
                out.append(f"  - {c['caller']} in {c['file']}:{c['line']}")
        if adrs:
            out.append("\n--- Linked Architectural Decisions (ADR) ---")
            for a in adrs:
                out.append(f"  - [{a[0]}] Confidence: {a[1]:.2f} - {a[2]}")
        return "\n".join(out)
    except Exception as e:
        return f"Error retrieving symbol context: {e}"

@mcp.tool()
def lore_audit_changes(project_path: str, diff_content: str) -> str:
    """Verify if unified diff cures taint flows without regressions."""
    try:
        project_root, db_path = _resolve_project_db(project_path)
        if not db_path.exists():
            return f"Error: Database not found at {db_path}."
            
        temp_patch = project_root / ".lore" / "mcp_temp_patch.patch"
        temp_patch.parent.mkdir(parents=True, exist_ok=True)
        temp_patch.write_text(diff_content, encoding="utf-8")
            
        from core.symbol_db import SymbolDB
        db = SymbolDB(db_path)
        try:
            from cli.vuln_analysis import _run_vuln_analysis
            res_analysis = _run_vuln_analysis(project_root, db.con)
            exposed = res_analysis.get("exposed_paths", [])
            
            from cli.patch_validator import _run_patch_validation
            res = _run_patch_validation(
                project_root=project_root,
                conn=db.con,
                db_path=db_path,
                exposed_paths=exposed,
                auto_cure=False,
                patch_path_str=str(temp_patch)
            )
        finally:
            db.close()
            
        if temp_patch.exists():
            temp_patch.unlink()
            
        out = [
            "=== PATCH AUDIT RESULT ===",
            f"Initial Taint Paths:   {res['baseline_active_paths_count']}",
            f"Remaining Taint Paths: {res['survived_paths_count']}",
            f"New Taint Paths:       {res['new_paths_count']}",
            f"Detected Regressions:  {res['regression_paths_count']}"
        ]
        if res['survived_paths_count'] == 0 and res['new_paths_count'] == 0 and res['regression_paths_count'] == 0:
            out.append("\n[SUCCESS] The patch is safe and cures all analyzed vulnerability paths!")
        else:
            out.append("\n[WARNING] The patch is incomplete or introduces new issues. Correct it and retry.")
        return "\n".join(out)
    except Exception as e:
        return f"Error during patch audit: {e}"

@mcp.tool()
def lore_get_compliance_adrs(project_path: str) -> str:
    """Retrieve all architectural decisions (ADRs) from Knowledge Graph."""
    try:
        project_root, db_path = _resolve_project_db(project_path)
        if not db_path.exists():
            return f"Error: Database not found at {db_path}."
            
        from core.symbol_db import SymbolDB
        db = SymbolDB(db_path)
        try:
            rows = db.con.execute("SELECT symbol_name, source_type, source_ref, confidence, description FROM decision_links").fetchall()
        finally:
            db.close()
            
        if not rows:
            return "No architectural decisions or constraints registered in Knowledge Graph."
            
        out = ["=== ARCHITECTURAL DECISIONS AND COMPLIANCE CONSTRAINTS ==="]
        for r in rows:
            out.append(f"- Symbol: {r[0]} | Source: {r[1]} ({r[2]}) | Confidence: {r[3]:.2f}\n  Description: {r[4]}")
        return "\n".join(out)
    except Exception as e:
        return f"Error retrieving architectural decisions: {e}"

@mcp.tool()
def lore_get_git_context(project_path: str, file_path: str, focus_lines: str | None = None) -> str:
    """Get recent commits and git blame for a specific file or line range."""
    try:
        project_root, db_path = _resolve_project_db(project_path)
        file_path_norm = file_path.replace("\\", "/").strip().lstrip("./")
        
        # Recent commits (last 5)
        raw_log = _git(project_root, "log", "-n", "5", "--format=%h|%an|%ad|%s", "--date=short", "--", file_path_norm)
        commits_lines = []
        if raw_log:
            for line in raw_log.splitlines():
                parts = line.split("|", 3)
                if len(parts) == 4:
                    h, author, date, subject = parts
                    commits_lines.append(f"  - {date} [{h}]: {subject} (by {author})")
                    
        commits_str = "\n".join(commits_lines) if commits_lines else "No recent commits found."
        
        # Blame context
        blame_str = ""
        if focus_lines:
            args = ["blame", "--date=short"]
            m = re.match(r"^(\d+)\s*-\s*(\d+)$", focus_lines.strip())
            if m:
                args.extend(["-L", f"{m.group(1)},{m.group(2)}"])
            else:
                m_single = re.match(r"^(\d+)$", focus_lines.strip())
                if m_single:
                    args.extend(["-L", f"{m_single.group(1)},{m_single.group(1)}"])
                    
            args.append(file_path_norm)
            raw_blame = _git(project_root, *args)
            if raw_blame:
                blame_lines = raw_blame.splitlines()
                entries = []
                seen_commits = set()
                for line in blame_lines[:15]:
                    bm = re.match(r"^([0-9a-f^]+)\s+\((.*?)\s+(\d{4}-\d{2}-\d{2})", line)
                    if bm:
                        commit_hash = bm.group(1)
                        author = bm.group(2)
                        date = bm.group(3)
                        if commit_hash not in seen_commits:
                            seen_commits.add(commit_hash)
                            subj = _git(project_root, "log", "-1", "--format=%s", commit_hash).strip()
                            entries.append(f"  - Commit {commit_hash[:8]} by {author} on {date}: \"{subj}\"")
                if entries:
                    blame_str = "\n" + "\n".join(entries)
                    
        out = [
            f"=== GIT CONTEXT FOR {file_path_norm} ===",
            "RECENT COMMITS:",
            commits_str
        ]
        if blame_str:
            out.extend(["", f"RELEVANT BLAME CONTEXT FOR LINES {focus_lines}:", blame_str])
        out.append("=== END GIT CONTEXT ===")
        return "\n".join(out)
    except Exception as e:
        return f"Error retrieving git context: {e}"

@mcp.tool()
def lore_get_adr(project_path: str, adr_id: str) -> str:
    """Retrieve full text and requirements of a specific architectural decision (ADR)."""
    try:
        project_root, db_path = _resolve_project_db(project_path)
        adr_clean = adr_id.strip().upper()
        
        db_results = []
        if db_path.exists():
            from core.symbol_db import SymbolDB
            db = SymbolDB(db_path)
            try:
                rows = db.con.execute(
                    "SELECT symbol_name, confidence, description FROM decision_links WHERE UPPER(source_ref) = ? OR UPPER(source_ref) LIKE ?",
                    (adr_clean, f"%{adr_clean}%")
                ).fetchall()
                for r in rows:
                    db_results.append(f"  - Governing Symbol: {r[0]} (confidence: {r[1]:.2f})\n    Rule: {r[2]}")
            finally:
                db.close()
                
        fs_text = ""
        search_paths = [
            project_root / ".lore-docs" / "LORE_ADR.md",
            project_root / "docs" / "adr" / f"{adr_clean}.md",
            project_root / "docs" / f"{adr_clean}.md",
            project_root / "adr" / f"{adr_clean}.md",
        ]
        
        for path in search_paths:
            if path.exists():
                try:
                    content = path.read_text(encoding="utf-8", errors="replace")
                    if path.name == "LORE_ADR.md":
                        pattern = rf"(#+\s*{adr_clean}\s*—.*?(?=\n#+|$))"
                        match = re.search(pattern, content, re.DOTALL | re.IGNORECASE)
                        if match:
                            fs_text = match.group(1).strip()
                            break
                    else:
                        fs_text = content.strip()
                        break
                except Exception:
                    pass
                    
        out = [f"=== LORE ADR SEARCH FOR {adr_clean} ==="]
        if fs_text:
            out.extend(["DOCUMENTATION FROM FILE:", fs_text])
        if db_results:
            out.extend(["", "GRAPH RELATIONSHIPS & CONSTRAINTS IN DB:", "\n".join(db_results)])
            
        if not fs_text and not db_results:
            return f"ADR '{adr_id}' not found in Knowledge Graph or documentation files."
            
        out.append("=== END ADR REPORT ===")
        return "\n".join(out)
    except Exception as e:
        return f"Error retrieving ADR {adr_id}: {e}"

@mcp.tool()
def lore_comply_and_apply(project_path: str, task_description: str) -> str:
    """Analyze proposed task, find relevant ADRs, and yield compliance prompt."""
    try:
        project_root, db_path = _resolve_project_db(project_path)
        if not db_path.exists():
            return f"Error: Database not found at {db_path}."

        # Auto-ingest chat intent on the fly
        try:
            from core.chat_miner import mine_chat_intent
            mine_chat_intent(db_path, project_root)
        except Exception as e:
            log(f"Chat miner background run skipped: {e}")

        from core.symbol_db import SymbolDB
        db = SymbolDB(db_path)
        try:
            matched_adrs = db.find_relevant_decisions(task_description)
        finally:
            db.close()

        if not matched_adrs:
            return f"No specific architectural constraints in KG refer directly to the task: '{task_description}'."

        prompt = [
            "You are an AI coding agent implementing the following task:",
            f'"{task_description}"\n',
            "LORE's Institutional Memory has identified active compliance constraints and architectural invariants governing these modules:"
        ]
        for r in matched_adrs:
            prompt.append(f"- Symbol: [{r['symbol_name']}] | Source: {r['source_type']} ({r['source_ref']}) | Confidence: {r['confidence']*100:.1f}%")
            prompt.append(f"  Constraint: {r['description']}")
        prompt.append("\nInstructions:\n1. Ensure your implementation adheres strictly to the security bounds and rules detailed above.\n2. Avoid introducing patterns that deviate from these documented architectural choices.")
        return "\n".join(prompt)
    except Exception as e:
        return f"Error processing compliance ADRs: {e}"

@mcp.tool()
def lore_get_related_tests(project_path: str, file_path: str) -> str:
    """Get related test cases (test oracle) and expectations for a specific file to know how it should behave."""
    try:
        project_root, db_path = _resolve_project_db(project_path)
        if not db_path.exists():
            return f"Error: Database not found at {db_path}."
        
        from cli.agent_retrieval import _find_related_tests
        oracles = _find_related_tests(db_path, file_path, project_root)
        if not oracles:
            return f"No related tests found in Knowledge Graph for {file_path}."
            
        out = [f"=== TEST ORACLE & EXPECTED BEHAVIORS FOR {file_path} ==="]
        for t in oracles:
            out.append(f"  - Test Case: {t['test_name']} in {t['test_file']}")
            out.append(f"    Expectation: {t['docstring']}")
        out.append("=== END TEST ORACLE ===")
        return "\n".join(out)
    except Exception as e:
        return f"Error retrieving related tests: {e}"

@mcp.tool()
def lore_get_similar_fixes(project_path: str, file_path: str, task: str) -> str:
    """Find past commits/fixes in git history that are semantically similar to the task for the given file."""
    try:
        project_root, db_path = _resolve_project_db(project_path)
        if not db_path.exists():
            return f"Error: Database not found at {db_path}."
        
        from cli.brief_builder import BriefBuilder
        builder = BriefBuilder(db_path, project_root)
        similar = builder._get_similar_fixes(file_path, task)
        if not similar:
            return f"No similar fixes found in git history for {file_path}."
        return similar
    except Exception as e:
        return f"Error retrieving similar fixes: {e}"

@mcp.tool()
def lore_get_architecture_constraints(project_path: str, file_path: str) -> str:
    """Get all active architectural constraints, safety rules, and decisions for a specific file."""
    try:
        project_root, db_path = _resolve_project_db(project_path)
        if not db_path.exists():
            return f"Error: Database not found at {db_path}."
        
        file_path_norm = file_path.replace("\\", "/").strip().lstrip("./")
        from core.symbol_db import SymbolDB
        db = SymbolDB(db_path)
        try:
            links = db.get_decision_links_for_file(file_path_norm)
        finally:
            db.close()
            
        if not links:
            return f"No specific architectural constraints found in Knowledge Graph for file: {file_path_norm}"
            
        out = [f"=== ACTIVE ARCHITECTURAL CONSTRAINTS FOR {file_path_norm} ==="]
        for r in links:
            out.append(f"- Symbol: {r[0]} | Source: {r[1]} | Confidence: {r[2]:.2f}\n  Constraint: {r[3]}")
        out.append("=== END CONSTRAINTS ===")
        return "\n".join(out)
    except Exception as e:
        return f"Error retrieving architectural constraints: {e}"

@mcp.tool()
def lore_query_knowledge_graph(project_path: str, query: str) -> str:
    """Run a query on the Knowledge Graph to find relevant symbols, code definitions, and architectural decisions."""
    try:
        project_root, db_path = _resolve_project_db(project_path)
        if not db_path.exists():
            return f"Error: Database not found at {db_path}."
            
        from core.symbol_db import SymbolDB
        db = SymbolDB(db_path)
        try:
            matched_adrs = db.find_relevant_decisions(query, max_results=5)
            fts_rows = db.search_fts(query, limit=5)
        finally:
            db.close()
            
        out = [f"=== CONTEXT SEARCH RESULTS FOR: '{query}' ==="]
        
        if matched_adrs:
            out.append("\n--- Relevant Architectural Decisions (ADRs) ---")
            for r in matched_adrs:
                out.append(f"- Symbol: {r['symbol_name']} | Source: {r['source_type']} ({r['source_ref']})")
                out.append(f"  Constraint: {r['description']}")
                
        if fts_rows:
            out.append("\n--- Relevant Symbols and Code Definitions ---")
            for r in fts_rows:
                out.append(f"- Symbol: {r['name']} in {r['file_path']} (ID: {r['rowid']})")
                
        if not matched_adrs and not fts_rows:
            return f"No relevant results found in Knowledge Graph for query: '{query}'."
            
        out.append("\n=== END RESULTS ===")
        return "\n".join(out)
    except Exception as e:
        return f"Error during Knowledge Graph query: {e}"

def _main_mcp(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="lore mcp", description="Start the LORE MCP server")
    parser.parse_args(argv)
    import sys
    import cli.shared
    from rich.console import Console
    cli.shared.console = Console(theme=cli.shared._THEME, file=sys.stderr)
    sys.stdout = sys.stderr
    log("Starting MCP Stdio Server...")
    mcp.run()
