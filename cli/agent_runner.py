import os
import sys
import json
import re
from pathlib import Path
from datetime import datetime

from cli.shared import console, MODEL, MAX_TOOL_CALLS
from cli.prompts import EXPLORE_SYSTEM, GENERATE_SYSTEM, LOCALIZE_SYSTEM, EDIT_SYSTEM
from core.symbol_map import SymbolDB, SymbolRetriever

from cli.agent_tools import TOOLS, FowExecutor
from cli.agent_stage import StageWriter, _extract_target_files
from cli.agent_history import _compress_history
from cli.agent_retrieval import _build_project_map, _build_compact_project_map, _astar_bundle_light
from cli.v11_retrieval import v11_retrieve_context
from cli.agent_delta import DeltaApplicator

def _extract_script(response) -> str:
    text = ""
    for block in response.content:
        if hasattr(block, "text") and block.text:
            text = block.text
            break
    text = re.sub(r"^```(?:python)?\s*\n?", "", text.strip(), flags=re.MULTILINE)
    text = re.sub(r"\n?```\s*$", "", text.strip(), flags=re.MULTILINE)
    return text.strip()

def _execute_exploration_script(script: str, fow: FowExecutor) -> str:
    results: list[str] = []
    namespace: dict = {
        "results":      results,
        "fow_search":   fow.search,
        "fow_frontier": fow.frontier,
        "fow_expand":   fow.expand,
    }
    try:
        exec(compile(script, "<exploration>", "exec"), namespace)
    except Exception as exc:
        results.append(f"[SCRIPT ERROR: {exc}]")
    if not results:
        return "[Exploration script returned no results — FOW tools are still available]"
    return "\n\n".join(str(r) for r in results)

def _parse_localization_json(text: str) -> list[dict]:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE | re.MULTILINE)
    text = re.sub(r"\s*```$", "", text, flags=re.IGNORECASE | re.MULTILINE)
    text = text.strip()
    try:
        data = json.loads(text)
        return data.get("target_files", [])
    except Exception:
        # Fallback: search for path in text
        files = []
        for m in re.finditer(r'"path"\s*:\s*"([^"]+)"', text):
            reason = ""
            pos = m.end()
            m_exp = re.search(r'"(?:reason_for_selection|explanation)"\s*:\s*"([^"]+)"', text[pos:pos+300])
            if m_exp:
                reason = m_exp.group(1)
            files.append({
                "path": m.group(1),
                "reason_for_selection": reason,
                "explanation": reason
            })
        return files


# ---------------------------------------------------------------------------
# Helpers for Points 2, 3, 4
# ---------------------------------------------------------------------------

_EXT_TO_LANG = {
    ".py": "python", ".js": "javascript", ".ts": "typescript",
    ".tsx": "tsx", ".jsx": "jsx", ".go": "go", ".rs": "rust",
    ".java": "java", ".rb": "ruby", ".cpp": "cpp", ".c": "c",
    ".h": "c", ".cs": "csharp", ".php": "php",
    ".yaml": "yaml", ".yml": "yaml", ".json": "json",
    ".toml": "toml", ".md": "markdown",
}


def _infer_language(file_path: str) -> str:
    """Infer fenced code block language from file extension."""
    ext = Path(file_path).suffix.lower()
    return _EXT_TO_LANG.get(ext, "")


def _validate_syntax(file_path: str, content: str) -> str | None:
    """Returns None if valid, or error message if syntax is broken. Python-only."""
    if not file_path.endswith(".py"):
        return None
    try:
        import ast
        ast.parse(content)
        return None
    except SyntaxError as e:
        return f"SyntaxError at line {e.lineno}: {e.msg}"


def _validate_and_fix_paths(targets: list[dict], project_root: Path, db) -> list[dict]:
    """Validate localizer paths, attempt fuzzy correction for non-existent ones."""
    validated = []
    all_paths = [r[0] for r in db.con.execute("SELECT path FROM files").fetchall()]

    for target in targets:
        raw_path = target["path"].replace("\\", "/").strip().lstrip("./")
        abs_path = project_root / raw_path

        if abs_path.exists():
            target["path"] = raw_path
            validated.append(target)
            continue

        # Fuzzy match: try suffix matching against known files
        basename = raw_path.split("/")[-1]
        candidates = [p for p in all_paths if p.replace("\\", "/").endswith(basename)]

        if len(candidates) == 1:
            target["path"] = candidates[0].replace("\\", "/")
            validated.append(target)
            continue

        # Try partial path match (last 2-3 segments)
        segments = raw_path.split("/")
        found = False
        for n_seg in range(min(3, len(segments)), 0, -1):
            suffix = "/".join(segments[-n_seg:])
            matches = [p for p in all_paths if p.replace("\\", "/").endswith(suffix)]
            if len(matches) == 1:
                target["path"] = matches[0].replace("\\", "/")
                validated.append(target)
                found = True
                break
        # If no match found, skip this target (caller will log)

    return validated


def _extract_ast_outline(content: str) -> str:
    import ast
    try:
        tree = ast.parse(content)
        lines = content.splitlines()
        outline = ["\n=== STRUCTURAL OUTLINE (Remaining file) ==="]
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                line = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
                outline.append(f"{line.strip()} ...  # (Lines {node.lineno}-{node.end_lineno})")
                if isinstance(node, ast.ClassDef):
                    for child in node.body:
                        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            cline = lines[child.lineno - 1] if child.lineno <= len(lines) else ""
                            outline.append(f"    {cline.strip()} ...  # (Lines {child.lineno}-{child.end_lineno})")
        header = "\n".join(lines[:800]) + "\n... [TRUNCATED] ..."
        return header + "\n".join(outline)
    except Exception:
        lines = content.splitlines()
        return "\n".join(lines[:800]) + "\n... [TRUNCATED] ..."

def _build_windowed_context(content: str, focus_ranges: list[range], window: int = 150) -> str:
    lines = content.splitlines()
    if len(lines) <= 800:
        return content
    if not focus_ranges:
        return _extract_ast_outline(content)
        
        
    # Merge overlapping ranges
    intervals = []
    for r in focus_ranges:
        # r.start and r.stop are 1-indexed line numbers from SQLite
        start_idx = max(0, r.start - 1 - window)
        end_idx = min(len(lines), r.stop - 1 + window)
        intervals.append([start_idx, end_idx])
    
    intervals.sort(key=lambda x: x[0])
    merged = []
    for interval in intervals:
        if not merged:
            merged.append(interval)
        else:
            prev = merged[-1]
            if interval[0] <= prev[1]:
                prev[1] = max(prev[1], interval[1])
            else:
                merged.append(interval)
                
    result_parts = []
    for start_idx, end_idx in merged:
        block_text = "\n".join(lines[start_idx:end_idx])
        result_parts.append(f"--- Lines {start_idx + 1}-{end_idx} ---\n{block_text}")
        
    return "\n... [TRUNCATED] ...\n".join(result_parts)



def _execute_editor_tool(t_name: str, t_input: dict, project_root: Path, stage=None) -> str:
    """Helper for executing MCP/Editor tools."""
    if t_name == "lore_get_adr":
        from cli.mcp_server import lore_get_adr
        return lore_get_adr(str(project_root), t_input.get("adr_id", ""))
    elif t_name == "lore_get_git_context":
        from cli.mcp_server import lore_get_git_context
        return lore_get_git_context(
            str(project_root),
            t_input.get("file_path", ""),
            t_input.get("focus_lines", None)
        )
    elif t_name == "lore_get_symbol_context":
        from cli.mcp_server import lore_get_symbol_context
        return lore_get_symbol_context(str(project_root), t_input.get("symbol_name", ""))
    elif t_name == "lore_get_similar_fixes":
        from cli.mcp_server import lore_get_similar_fixes
        return lore_get_similar_fixes(str(project_root), t_input.get("file_path", ""), t_input.get("task", ""))
    elif t_name == "lore_run_docker_sandbox":
        from cli.sandbox_evaluator import SandboxEvaluator
        
        # Apply staged files temporarily
        backups = {}
        if stage and stage.written:
            import shutil
            for entry in stage.written:
                rel = entry["path"]
                staged_p = Path(entry["staged"])
                if staged_p.exists():
                    real_p = project_root / rel
                    if real_p.exists():
                        backups[rel] = real_p.read_bytes()
                    else:
                        backups[rel] = None
                    shutil.copy2(staged_p, real_p)
        
        try:
            evaluator = SandboxEvaluator(project_root)
            return evaluator.run_in_docker(
                command=t_input.get("command", ""),
                python_script=t_input.get("python_script", None)
            )
        finally:
            # Revert staged files
            for rel, b_content in backups.items():
                real_p = project_root / rel
                if b_content is not None:
                    real_p.write_bytes(b_content)
                else:
                    if real_p.exists():
                        real_p.unlink()
    elif t_name == "lore_get_related_tests":
        from cli.mcp_server import lore_get_related_tests
        return lore_get_related_tests(str(project_root), t_input.get("file_path", ""))
    else:
        return f"Unknown tool: {t_name}"


def run_agent(
    task: str,
    project_root: Path,
    retriever: SymbolRetriever,
    db: SymbolDB,
    log_path: Path,
) -> dict:
    from core.llm_client import get_llm_client
    client = get_llm_client(project_root)
    fow    = FowExecutor(retriever, db)
    stage  = StageWriter(project_root)
    log: list[str] = []
    stats = {
        "api_calls": 0, "tool_calls": 0, "fow_calls": 0, "files_staged": 0,
        "api_input_tokens": 0, "api_output_tokens": 0, "dup_calls_blocked": 0,
    }
    _DEDUP_EXEMPT   = {"write_staged_file", "done"}
    called_tools:   dict[str, str] = {}
    tool_summaries: dict[str, str] = {}

    def _log(msg: str) -> None:
        ts   = datetime.now().strftime("%H:%M:%S")
        plain_line = f"[{ts}] {msg}"
        log.append(plain_line)
        if "TASK:" in msg or "PROJECT:" in msg:
            console.print(f"[bold cyan][{ts}][/] {msg}")
        elif "[PHASE" in msg:
            console.print(f"\n[bold magenta][{ts}] ── {msg} ──[/]")
        elif "[A*]" in msg:
            console.print(f"[bold blue][{ts}][/] {msg}")
        elif "[LLM]" in msg:
            console.print(f"[bold yellow][{ts}][/] {msg}")
        elif "WRITE" in msg or "STAGED" in msg or "Done" in msg or "completed" in msg.lower():
            console.print(f"[bold green][{ts}][/] {msg}")
        else:
            console.print(f"[dim][{ts}][/] {msg}")

    def _record_call(response) -> None:
        stats["api_calls"]         += 1
        stats["api_input_tokens"]  += response.usage.input_tokens
        stats["api_output_tokens"] += response.usage.output_tokens

    _log(f"TASK: {task}")
    _log(f"PROJECT: {project_root}")

    _log("\n[PHASE 2] Building context bundle via semantic A*...")
    bundle, visited_syms = v11_retrieve_context(task, db, retriever, token_budget=15000)

    target_files_raw = _extract_target_files(task)
    resolved_paths: list[str] = []
    seen_resolved: set[str] = set()

    def add_resolved(rel: str):
        rel_norm = rel.replace("\\", "/").strip().lstrip("./")
        if rel_norm and rel_norm not in seen_resolved:
            seen_resolved.add(rel_norm)
            resolved_paths.append(rel_norm)

    # 1. Resolve candidates from task description
    for cand in target_files_raw:
        try:
            cand_lower = cand.lower()
            row = db.con.execute("SELECT path FROM files WHERE LOWER(path) = ?", (cand_lower,)).fetchone()
            if row:
                add_resolved(row[0])
                continue
            row = db.con.execute("SELECT path FROM files WHERE LOWER(path) LIKE ?", (f"%{cand_lower}",)).fetchone()
            if row:
                add_resolved(row[0])
                continue
        except Exception:
            pass
        abs_cand = project_root / cand
        if abs_cand.exists():
            add_resolved(cand)

    # 2. Add files containing visited symbols from semantic search
    for sym_key in visited_syms:
        if "::" in sym_key:
            fpath = sym_key.split("::", 1)[0]
            add_resolved(fpath)
        else:
            block = retriever.get_symbol_block(sym_key)
            if block and "file" in block:
                add_resolved(block["file"])

    existing_prefix_parts: list[str] = []
    MAX_LINES_FOR_WHOLE_FILE = 500

    for rel_path in resolved_paths:
        abs_path = project_root / rel_path
        if abs_path.exists():
            try:
                content = abs_path.read_text(encoding="utf-8", errors="replace")
                lines_count = len(content.splitlines())
                if lines_count <= MAX_LINES_FOR_WHOLE_FILE:
                    existing_prefix_parts.append(
                        f"EXISTING FILE — preserve all fields and structure "
                        f"(modify, do not replace or omit anything):\n"
                        f"FILE: {rel_path}\n"
                        f"```\n{content}\n```"
                    )
                    _log(f"[PHASE 2] Existing file prepended to bundle: {rel_path} ({lines_count} lines)")
                else:
                    _log(f"[PHASE 2] File {rel_path} is too long ({lines_count} lines) to prepend in full")
            except OSError:
                pass

    if existing_prefix_parts:
        bundle = "\n\n".join(existing_prefix_parts) + ("\n\n" + bundle if bundle else "")

    if bundle:
        _log(f"[PHASE 2] A* bundle: {len(visited_syms)} symbols visited, ~{len(bundle)//4} tokens")
    else:
        _log("[PHASE 2] No embeddings — falling back to exploration script (API Call 1)...")
        project_map = _build_project_map(db, project_root)
        r1 = client.messages.create(
            model=MODEL,
            max_tokens=1024,
            system=EXPLORE_SYSTEM,
            messages=[{"role": "user", "content": f"{project_map}\n\n=== TASK ===\n{task}"}],
        )
        _record_call(r1)
        script = _extract_script(r1)
        _log(f"[PHASE 2-fallback] Script ({len(script.splitlines())} lines):")
        for line in script.splitlines():
            _log(f"  {line}")
        bundle = _execute_exploration_script(script, fow)
        _log(f"[PHASE 3-fallback] Bundle ready — ~{len(bundle)//4} tokens")

    for sym in fow.body_seen:
        for tname in ("fow_expand", "fow_frontier"):
            key = f"{tname}|{json.dumps({'symbol': sym}, sort_keys=True)}"
            called_tools[key] = f"[fetched in Phase 3 exploration script]"

    FILE_INLINE_LIMIT = 300
    fow_full_parts: list[str] = []
    astar_paths = set()
    for m in re.finditer(r"^FILE:\s*(.+)$", bundle, re.MULTILINE):
        astar_paths.add(m.group(1).strip().replace("\\", "/"))

    for rel in fow.files_accessed:
        rel_norm = rel.replace("\\", "/")
        if rel_norm in astar_paths:
            continue
        abs_path = project_root / rel
        try:
            content = abs_path.read_text(encoding="utf-8", errors="replace")
            if content.count("\n") <= FILE_INLINE_LIMIT:
                fow_full_parts.append(
                    f"FULL FILE (accessed via FOW — preserve ALL existing code):\n"
                    f"FILE: {rel_norm}\n"
                    f"```\n{content.rstrip()}\n```"
                )
                _log(f"[PHASE 4] Full file injected (FOW-accessed): {rel_norm} ({content.count(chr(10))} lines)")
        except OSError:
            pass

    if fow_full_parts:
        bundle = "\n\n".join(fow_full_parts) + "\n\n" + bundle

    _log("\n[PHASE 4-A] Two-Phase Localizer (API Call 2)...")
    light_bundle, _ = _astar_bundle_light(task, db, retriever, token_budget=2000)
    compact_map = _build_compact_project_map(db)
    
    hints_str = ""
    if resolved_paths:
        hints_str = "=== CANDIDATE FILE HINTS ===\n" + "\n".join(f"  - {p}" for p in resolved_paths) + "\n\n"
        
    localize_messages = [{
        "role": "user",
        "content": (
            f"=== PROJECT STRUCTURE ===\n{compact_map}\n\n"
            f"{hints_str}"
            f"=== CODE CONTEXT (top semantic matches) ===\n{light_bundle}\n"
            f"=== END CONTEXT ===\n\n=== YOUR TASK ===\n{task}"
        ),
    }]
    
    target_files = []
    try:
        response_loc = client.messages.create(
            model=MODEL,
            max_tokens=2048,
            system=LOCALIZE_SYSTEM,
            messages=localize_messages,
        )
        _record_call(response_loc)
        loc_text = "".join(b.text for b in response_loc.content if hasattr(b, "text") and b.text)
        _log(f"[Localizer response]\n{loc_text}")
        target_files = _parse_localization_json(loc_text)
        if target_files:
            original_count = len(target_files)
            target_files = _validate_and_fix_paths(target_files, project_root, db)
            if len(target_files) < original_count:
                _log(f"[WARN] Localizer proposed {original_count} file(s), {original_count - len(target_files)} path(s) not found in repo — skipped")
    except Exception as exc:
        _log(f"[WARN] Localizer call failed or parsed incorrectly: {exc}")

    if target_files:
        from cli.brief_builder import BriefBuilder
        brief_builder = BriefBuilder(db.db_path, project_root)
        
        _log("[PHASE 3] Architect: Generating strategic brief...")
        architect_context_blocks = []
        for target in target_files:
            rel_path = target["path"].replace("\\", "/").strip().lstrip("./")
            abs_path = project_root / rel_path
            file_content = ""
            
            # Find line ranges of visited symbols in this file
            focus_lines = []
            for sym_key in visited_syms:
                try:
                    if "::" in sym_key:
                        fpath, sym_name = sym_key.split("::", 1)
                    else:
                        fpath, sym_name = None, sym_key
                    rows = retriever.find_symbol(sym_name)
                    for r in rows:
                        r_path = r["path"].replace("\\", "/").strip().lstrip("./")
                        if r_path == rel_path and (fpath is None or fpath == r_path):
                            focus_lines.append(range(r["line_start"], r["line_end"] + 1))
                except Exception:
                    pass
            
            target["focus_lines"] = focus_lines

            if abs_path.exists():
                try:
                    file_content = abs_path.read_text(encoding="utf-8", errors="replace")
                    file_content = _build_windowed_context(file_content, focus_lines)
                except OSError:
                    pass
            brief = brief_builder.build_signpost_brief(rel_path, task)
            architect_context_blocks.append(
                f"=== FILE: {rel_path} ===\n"
                f"=== SIGNPOST ===\n{brief}\n\n"
                f"=== CONTENT ===\n{file_content}\n"
            )
            
        architect_prompt = (
            f"=== TASK ===\n{task}\n\n"
            f"=== LOCALIZED FILES & CONTEXT ===\n"
            + "\n".join(architect_context_blocks)
        )
        
        strategic_brief = ""
        try:
            from cli.prompts import ARCHITECT_SYSTEM
            response_arch = client.messages.create(
                model=MODEL,
                max_tokens=4096,
                system=ARCHITECT_SYSTEM,
                messages=[{"role": "user", "content": architect_prompt}],
            )
            _record_call(response_arch)
            strategic_brief = "".join(b.text for b in response_arch.content if hasattr(b, "text") and b.text)
            _log(f"  [Architect Strategic Brief generated, length: {len(strategic_brief)} chars]")
        except Exception as exc:
            _log(f"  [WARN] Architect call failed: {exc}")
        
        _log(f"[PHASE 4-B] Two-Phase Editor: modifying {len(target_files)} target file(s)...")
        for target in target_files:
            rel_path = target["path"].replace("\\", "/").strip().lstrip("./")
            reason_for_selection = target.get("reason_for_selection", "")
            _log(f"\n--- Editing File: {rel_path} ---")
            _log(f"Reason for selection: {reason_for_selection}")
            
            abs_path = project_root / rel_path
            file_content = ""
            if abs_path.exists():
                try:
                    file_content = abs_path.read_text(encoding="utf-8", errors="replace")
                except OSError as err:
                    _log(f"  [ERROR] Failed to read {rel_path}: {err}")
            
            # Find line ranges of visited symbols in this file to restrict blame to
            focus_lines = target.get("focus_lines", [])
            if not focus_lines:
                for sym_key in visited_syms:
                    try:
                        if "::" in sym_key:
                            fpath, sym_name = sym_key.split("::", 1)
                        else:
                            fpath, sym_name = None, sym_key
                        rows = retriever.find_symbol(sym_name)
                        for r in rows:
                            r_path = r["path"].replace("\\", "/").strip().lstrip("./")
                            if r_path == rel_path and (fpath is None or fpath == r_path):
                                focus_lines.append(range(r["line_start"], r["line_end"] + 1))
                    except Exception:
                        pass
            
            if file_content:
                file_content = _build_windowed_context(file_content, focus_lines)
            
            brief = brief_builder.build_signpost_brief(rel_path, task)
            if brief:
                _log(f"  [Brief Builder] Generated signpost brief for {rel_path} (~{len(brief)//4} tokens)")
            
            lang = _infer_language(rel_path)
            
            content_str = (
                f"=== TASK ===\n{task}\n\n"
                f"=== STRATEGIC ARCHITECTURAL BRIEF ===\n{strategic_brief}\n\n"
                f"=== TARGET FILE ===\n{rel_path}\n\n"
                f"=== REASON FOR SELECTION ===\n{reason_for_selection}\n\n"
            )
            if brief:
                content_str += f"{brief}\n\n"
            content_str += f"=== FILE CONTENT ===\n(NOTE: Some large files are windowed. Do NOT copy the '--- Lines ... ---' headers into your SEARCH/REPLACE blocks.)\n```{lang}\n{file_content}\n```"
            
            edit_messages = [{
                "role": "user",
                "content": content_str
            }]
            
            # MCP tools defined for Editor LLM
            editor_tools = [
                {
                    "name": "lore_run_docker_sandbox",
                    "description": "[CRITICAL] Run a bash command or python script in an isolated Docker container to reproduce the bug.",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "command": {
                                "type": "string",
                                "description": "The bash command to run (e.g. 'python test.py')."
                            },
                            "python_script": {
                                "type": "string",
                                "description": "The complete Python script content to write to a file and execute. Use this for reproducing the bug."
                            }
                        },
                        "required": ["command"]
                    }
                },
                {
                    "name": "lore_get_git_context",
                    "description": "Get recent commits and git blame for a specific file or line range.",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "file_path": {
                                "type": "string",
                                "description": "Path to the file to get git context for."
                            },
                            "focus_lines": {
                                "type": "string",
                                "description": "Optional line range, e.g. '10-20' or single line '10' for blame check."
                            }
                        },
                        "required": ["file_path"]
                    }
                },
                {
                    "name": "lore_get_adr",
                    "description": "Retrieve full text and requirements of a specific architectural decision (ADR).",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "adr_id": {
                                "type": "string",
                                "description": "ID of the ADR (e.g. 'ADR-007' or 'ADR-004')."
                            }
                        },
                        "required": ["adr_id"]
                    }
                },
                {
                    "name": "lore_get_symbol_context",
                    "description": "Get symbol definitions, callers, dependencies, and linked ADRs.",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "symbol_name": {
                                "type": "string",
                                "description": "The name of the symbol (class, function, variable) to inspect."
                            }
                        },
                        "required": ["symbol_name"]
                    }
                },
                {
                    "name": "lore_get_related_tests",
                    "description": "Get related test cases (test oracle) and expectations for a specific file to know how it should behave.",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "file_path": {
                                "type": "string",
                                "description": "Path to the file to find related tests for."
                            }
                        },
                        "required": ["file_path"]
                    }
                },
                {
                    "name": "lore_get_similar_fixes",
                    "description": "Find past commits/fixes in git history that are semantically similar to the task for the given file.",
                    "input_schema": {
                        "type": "object",
                        "properties": {
                            "file_path": {
                                "type": "string",
                                "description": "Path to the file."
                            },
                            "task": {
                                "type": "string",
                                "description": "The task or bug description to match against commit messages."
                            }
                        },
                        "required": ["file_path", "task"]
                    }
                }
            ]

            attempts = 5
            staged_ok = False
            for attempt in range(attempts):
                _log(f"  [Attempt {attempt + 1}/{attempts}] Querying Editor LLM...")
                try:
                    # Initialize message thread for this attempt
                    current_messages = list(edit_messages)
                    
                    tool_calls_count = 0
                    max_tool_calls_limit = 10
                    response_edit = None
                    
                    while tool_calls_count < max_tool_calls_limit:
                        response_edit = client.messages.create(
                            model=MODEL,
                            max_tokens=8192,
                            system=EDIT_SYSTEM,
                            messages=current_messages,
                            tools=editor_tools,
                        )
                        _record_call(response_edit)
                        
                        if response_edit.stop_reason == "tool_use":
                            assistant_message_content = []
                            tool_use_blocks = []
                            for block in response_edit.content:
                                if block.type == "text":
                                    assistant_message_content.append({"type": "text", "text": block.text})
                                elif block.type == "tool_use":
                                    assistant_message_content.append({
                                        "type": "tool_use",
                                        "id": block.id,
                                        "name": block.name,
                                        "input": block.input
                                    })
                                    tool_use_blocks.append(block)
                            
                            current_messages.append({
                                "role": "assistant",
                                "content": assistant_message_content
                            })
                            
                            tool_results_content = []
                            for tu in tool_use_blocks:
                                t_name = tu.name
                                t_id = tu.id
                                t_input = tu.input
                                _log(f"    [Editor Tool Call] {t_name} with {t_input}")
                                stats["tool_calls"] += 1
                                
                                try:
                                    result_text = _execute_editor_tool(t_name, t_input, project_root, stage=stage)
                                except Exception as te:
                                    result_text = f"Error executing tool: {te}"
                                
                                tool_results_content.append({
                                    "type": "tool_result",
                                    "tool_use_id": t_id,
                                    "content": result_text
                                })
                                
                            current_messages.append({
                                "role": "user",
                                "content": tool_results_content
                            })
                            tool_calls_count += 1
                        else:
                            break
                    
                    if not response_edit:
                        raise ValueError("No response received from Editor LLM.")
                        
                    full_text = "".join(b.text for b in response_edit.content if hasattr(b, "text") and b.text)
                    _log(f"  [Editor response choice]\n{full_text[:400]}...")
                    
                    if "<VETO_OVERRIDE_ACCEPT>" in full_text:
                        _log("  [SUCCESS] LLM explicitly accepted the patch after testing.")
                        staged_ok = True
                        break

                    applicator = DeltaApplicator()
                    n_written = applicator.apply(full_text, project_root, stage, _log)
                    if n_written == 0 and full_text.strip():
                        _log("  [Attempt] Zero files staged — attempting fallback re-parse...")
                        n_written = applicator.fallback_reparse(full_text, project_root, stage, _log)
                    
                    if n_written == 0:
                        if attempt < attempts - 1:
                            _log("  [WARN] Patch failed to apply strictly. Retrying...")
                            edit_messages.append({"role": "assistant", "content": response_edit.content})
                            edit_messages.append({
                                "role": "user",
                                "content": (
                                    "Your SEARCH/REPLACE block failed to apply. The SEARCH block must match the existing code EXACTLY or STRIP-EXACTLY, byte-for-byte.\n"
                                    "Do not hallucinate code or leave out lines in the middle of the SEARCH block. Please try again."
                                )
                            })
                            continue
                        else:
                            _log("  [ERROR] Exhausted attempts to apply patch strictly.")
                    elif n_written > 0:
                        # Validate syntax of patched files (Point 3)
                        syntax_errors = []
                        for entry in stage.written:
                            staged_p = Path(entry["staged"])
                            if staged_p.exists() and entry["path"] == rel_path:
                                patched_content = staged_p.read_text(encoding="utf-8", errors="replace")
                                from cli.sandbox_evaluator import SandboxEvaluator
                                evaluator = SandboxEvaluator(project_root)
                                err = evaluator.evaluate_syntax_and_trace(entry["path"], patched_content)
                                if err:
                                    syntax_errors.append(f"{entry['path']}: {err}")

                        if syntax_errors and attempt < attempts - 1:
                            _log(f"  [WARN] Syntax validation failed: {'; '.join(syntax_errors)}")
                            # Revert staged file and retry
                            for entry in stage.written:
                                if entry["path"] == rel_path:
                                    staged_p = Path(entry["staged"])
                                    if staged_p.exists():
                                        orig = project_root / entry["path"]
                                        if orig.exists():
                                            import shutil
                                            shutil.copy2(orig, staged_p)
                            stage.written = [e for e in stage.written if e["path"] != rel_path]
                            edit_messages.append({"role": "assistant", "content": response_edit.content})
                            edit_messages.append({
                                "role": "user",
                                "content": (
                                    f"Your patch was applied but produced invalid Python syntax:\n"
                                    f"{chr(10).join(syntax_errors)}\n"
                                    f"Please fix the SEARCH/REPLACE blocks to produce valid Python."
                                )
                            })
                            continue  # retry

                        if "<VETO_OVERRIDE_ACCEPT>" in full_text:
                            _log("  [SUCCESS] LLM explicitly accepted the patch after testing.")
                            staged_ok = True
                            break

                        if attempt < attempts - 1:
                            _log("  [PHASE 4-B2] Patch applied. Prompting Editor for Sandbox testing...")
                            edit_messages.append({"role": "assistant", "content": response_edit.content})
                            edit_messages.append({
                                "role": "user",
                                "content": (
                                    "Your patch was successfully applied locally and passed syntax checks.\n"
                                    "You MUST now use the `lore_run_docker_sandbox` tool to run the native test suite (e.g., `./tests/runtests.py <app>` or `pytest`) to verify your fix.\n"
                                    "If the tests fail, provide a new SEARCH/REPLACE block.\n"
                                    "If the tests pass and you are confident the bug is fixed without regressions, output exactly: <VETO_OVERRIDE_ACCEPT>"
                                )
                            })
                            continue # Restart the attempt loop so LLM can call tools!
                except Exception as e:
                    _log(f"  [ERROR] Attempt {attempt + 1} failed: {e}")
                    if attempt == attempts - 1:
                        raise
                    continue

            if not staged_ok:
                _log(f"  [WARN] Failed to apply changes to {rel_path} after all attempts.")
    else:
        _log("\n[PHASE 4] Generating files (API Call 2+) via One-Shot Fallback...")
        messages: list[dict] = [{
            "role": "user",
            "content": (
                f"=== CODE CONFLOW (gathered by exploration script) ===\n{bundle}\n"
                f"=== END CONTEXT ===\n\n=== YOUR TASK ===\n{task}"
            ),
        }]

        for iteration in range(MAX_TOOL_CALLS):
            _log(f"\n--- Iteration {iteration + 1} ---")
            response = client.messages.create(
                model=MODEL,
                max_tokens=8192,
                system=GENERATE_SYSTEM,
                messages=messages,
            )
            _record_call(response)
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                _log("Claude finished (end_turn)")
                full_text = "".join(b.text for b in response.content if hasattr(b, "text") and b.text)
                if full_text:
                    _log(f"[Claude response]\n{full_text}")

                applicator  = DeltaApplicator()
                n_written   = applicator.apply(full_text, project_root, stage, _log)
                if n_written == 0 and full_text.strip():
                    _log("[DELTA] Zero files staged — attempting fallback re-parse...")
                    n_written = applicator.fallback_reparse(full_text, project_root, stage, _log)
                stats["files_staged"] += n_written
                break

            if response.stop_reason != "tool_use":
                _log(f"Unexpected stop_reason: {response.stop_reason}")
                break

            tool_results: list[dict] = []
            should_stop = False

            for block in response.content:
                if block.type != "tool_use":
                    if hasattr(block, "text") and block.text:
                        _log(f"[Claude] {block.text[:200]}")
                    continue

                tool_name, tool_input = block.name, block.input
                stats["tool_calls"] += 1
                _log(f"[TOOL] {tool_name}({json.dumps(tool_input, ensure_ascii=False)[:120]})")

                call_key = f"{tool_name}|{json.dumps(tool_input, sort_keys=True)}"
                if tool_name not in _DEDUP_EXEMPT and call_key in called_tools:
                    prev = called_tools[call_key]
                    result = f"[DUPLICATE CALL BLOCKED — you already have this data]\nPrevious result: {prev}\nProceed with what you already have."
                    stats["dup_calls_blocked"] += 1
                    _log(f"  [DUP blocked] {tool_name}")
                elif tool_name == "fow_search":
                    keyword = tool_input.get("keyword", "").strip()
                    result = fow.search(keyword) if keyword else "Error: 'keyword' is required for fow_search."
                    stats["fow_calls"] += 1
                elif tool_name == "fow_frontier":
                    result = fow.frontier(tool_input["symbol"], min(2, tool_input.get("depth", 1)))
                    stats["fow_calls"] += 1
                elif tool_name == "fow_expand":
                    result = fow.expand(tool_input["symbol"])
                    stats["fow_calls"] += 1
                elif tool_name == "write_staged_file":
                    result = stage.write(tool_input["relative_path"], tool_input["content"], tool_input.get("reason", ""))
                    stats["files_staged"] += 1
                    _log(f"  >> {result}")
                elif tool_name == "done":
                    _log(f"[DONE] {tool_input.get('summary', '')}")
                    should_stop, result = True, "Done acknowledged."
                else:
                    result = f"Unknown tool: {tool_name}"

                summary = next((l.strip() for l in result.splitlines() if l.strip()), result[:100])[:150]
                called_tools[call_key] = summary
                tool_summaries[block.id] = summary
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})

            messages.append({"role": "user", "content": tool_results})
            _compress_history(messages, keep_last_n=1, tool_summaries=tool_summaries)
            if should_stop:
                break

    full_file_tokens = fow.full_file_tokens_if_read_whole()
    actual_input = stats["api_input_tokens"]
    saving_pct = round(100 * (1 - actual_input / full_file_tokens), 1) if full_file_tokens else 0

    _log("\n--- Token Report ---")
    _log(f"  API calls total                  : {stats['api_calls']}")
    _log(f"  API input tokens (actual)        : {actual_input:,}")
    _log(f"  API output tokens                : {stats['api_output_tokens']:,}")
    _log(f"  Full-file tokens (would-be)      : {full_file_tokens:,} (reading {len(fow.files_accessed)} files entirely)")
    _log(f"  Context saving vs full-file      : {saving_pct}%")
    _log(f"  Duplicate calls blocked          : {stats['dup_calls_blocked']}")

    stats["full_file_tokens_estimate"] = full_file_tokens
    stats["context_saving_pct"] = saving_pct
    stats["files_accessed_by_fow"] = sorted(fow.files_accessed)

    _log("\n--- Generating diff ---")
    diff_text = stage.generate_diff()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("\n".join(log), encoding="utf-8")
    diff_path = log_path.with_suffix(".diff")
    diff_path.write_text(diff_text, encoding="utf-8")

    _log(f"\nLog:  {log_path}")
    _log(f"Diff: {diff_path}")
    _log(f"Stage: {stage.stage_dir}")

    return {
        "stats": stats,
        "staged_files": stage.written,
        "diff": diff_text,
        "log_path": str(log_path),
        "diff_path": str(diff_path),
    }
