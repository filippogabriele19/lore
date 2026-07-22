import os, sys, argparse, json
from pathlib import Path
from cli.shared import console, DEFAULT_PROJECT, _get_db_path

def _main_gh_check(argv: list[str] | None = None) -> None:
    """gh-check mode — check PR changes against the KG and generate Markdown report."""
    parser = argparse.ArgumentParser(
        prog="lore gh-check",
        description="Run architecture and security audit on PR changes",
    )
    parser.add_argument("--project", default=DEFAULT_PROJECT,
                        help=f"Path to project root (default: {DEFAULT_PROJECT})")
    parser.add_argument("--changed-files", default=None,
                        help="Path to a file containing a list of changed files (one per line)")
    parser.add_argument("--commit-range", default=None,
                        help="Commit range to check (e.g. 'origin/main...HEAD')")
    parser.add_argument("--format", choices=["markdown", "json", "sarif"], default="markdown",
                        help="Output format (default: markdown)")
    parser.add_argument("--fail-on", choices=["none", "critical", "warning"], default="none",
                        help="Exit code behavior: fail on critical/warning findings (default: none)")
    args = parser.parse_args(argv)

    project_root = Path(args.project)
    if not project_root.exists():
        console.print(f"[error]Project path not found: {project_root}[/]")
        sys.exit(1)

    changed_files = []
    if args.changed_files:
        p = Path(args.changed_files)
        if p.exists():
            changed_files = [line.strip() for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
    elif args.commit_range:
        try:
            import subprocess
            res = subprocess.run(
                ["git", "diff", "--name-only", args.commit_range],
                cwd=str(project_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True
            )
            changed_files = [line.strip() for line in res.stdout.splitlines() if line.strip()]
        except Exception as e:
            console.print(f"[error]✖ Failed to run git diff: {e}[/]")
            sys.exit(1)

    # Normalize separators
    changed_files = [f.replace("\\", "/") for f in changed_files]

    if not changed_files:
        console.print("[warning]⚠ No changed files detected or provided.[/]")
        return

    db_path = _get_db_path(project_root)
    if not db_path.exists():
        console.print(f"[error]✖ Database not found under {project_root}[/]")
        sys.exit(1)

    import sqlite3 as _sq3
    conn = _sq3.connect(str(db_path))
    conn.row_factory = _sq3.Row

    # 1. Hotspots check
    hotspots = []
    for f in changed_files:
        if f.startswith(("docs/", "doc/")) or f.endswith((".txt", ".rst", ".md")):
            continue
        f_alt = f.replace("/", "\\")
        row = conn.execute(
            "SELECT file_path, change_freq, risk_score FROM hotspots WHERE (file_path = ? OR file_path = ?) AND risk_score >= 0.50 AND change_freq >= 10", (f, f_alt)
        ).fetchone()
        if row:
            dl_cols = [r[1] for r in conn.execute("PRAGMA table_info(decision_links)").fetchall()]
            if "file_path" in dl_cols:
                dl_count = conn.execute(
                    "SELECT COUNT(*) FROM decision_links dl "
                    "JOIN symbols s ON dl.symbol_name = s.name "
                    "JOIN files f2 ON s.file_id = f2.id "
                    "WHERE (f2.path = ? OR f2.path = ?) AND (dl.file_path = ? OR dl.file_path = ? OR dl.file_path = '')",
                    (f, f_alt, f, f_alt)
                ).fetchone()[0]
            else:
                dl_count = conn.execute(
                    "SELECT COUNT(*) FROM decision_links dl "
                    "JOIN symbols s ON dl.symbol_name = s.name "
                    "JOIN files f2 ON s.file_id = f2.id "
                    "WHERE (f2.path = ? OR f2.path = ?)",
                    (f, f_alt)
                ).fetchone()[0]

            file_lines = 0
            frow = conn.execute("SELECT lines FROM files WHERE path = ? OR path = ?", (f, f_alt)).fetchone()
            if frow and frow["lines"]:
                file_lines = frow["lines"]

            basename = f.split("/")[-1].lower()
            is_trivial = (
                basename in ("__init__.py", "constants.py", "types.ts", "types.py", "settings.py", "version.py")
                or (file_lines > 0 and file_lines < 50)
            )

            hotspots.append({
                "path": f,
                "change_freq": row["change_freq"],
                "risk_score": row["risk_score"],
                "decision_links": dl_count,
                "is_trivial": is_trivial
            })

    # 2. File-level Co-changes (Virtual Edges) - High Precision Threshold (>= 0.70)
    co_change_warnings = []
    for f in changed_files:
        f_alt = f.replace("/", "\\")
        rows = conn.execute("""
            SELECT dst_file AS file_b, co_change_rate, shared_commits FROM virtual_edges WHERE (src_file = ? OR src_file = ?) AND co_change_rate >= 0.70 AND shared_commits >= 4
            UNION
            SELECT src_file AS file_b, co_change_rate, shared_commits FROM virtual_edges WHERE (dst_file = ? OR dst_file = ?) AND co_change_rate >= 0.70 AND shared_commits >= 4
        """, (f, f_alt, f, f_alt)).fetchall()
        for r in rows:
            target_file = r["file_b"].replace("\\", "/")
            rate = r["co_change_rate"]
            if target_file not in changed_files:
                co_change_warnings.append({
                    "src": f,
                    "dst": target_file,
                    "rate": rate
                })

    # 2b. Symbol-level Statistical Co-changes (Support & Confidence)
    symbol_co_change_warnings = []
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    if "symbol_co_changes" in tables:
        for f in changed_files:
            f_alt = f.replace("/", "\\")
            s_rows = conn.execute("""
                SELECT symbol_a, symbol_b, file_b, shared_commits, total_commits_a, confidence 
                FROM symbol_co_changes 
                WHERE (file_a = ? OR file_a = ?) AND confidence >= 0.50 AND shared_commits >= 3
            """, (f, f_alt)).fetchall()
            for r in s_rows:
                dst = r["file_b"].replace("\\", "/")
                if dst not in changed_files:
                    symbol_co_change_warnings.append({
                        "src_sym": r["symbol_a"],
                        "dst_sym": r["symbol_b"],
                        "src_file": f,
                        "dst_file": dst,
                        "shared": r["shared_commits"],
                        "total_a": r["total_commits_a"],
                        "confidence": r["confidence"]
                    })

    # 2c. Test Coverage Coupling
    core_source_files = [
        f for f in changed_files 
        if f.endswith((".py", ".ts", ".go", ".js", ".java")) 
        and "test" not in f.lower() 
        and not f.startswith(("docs/", "doc/", "examples/", "vendor/", "node_modules/", "venv/", ".venv/"))
    ]
    test_files = [f for f in changed_files if "test" in f.lower()]
    has_test_coupling_warning = len(core_source_files) > 0 and len(test_files) == 0

    # 2d. Code Invariant Mining & Sibling Conventions
    from core.invariant_miner import mine_sibling_conventions, check_guard_stability
    sibling_warnings = []
    invariant_alerts = []

    # Fetch patch diffs for invariant checking
    patch_diff_map = {}
    if args.commit_range:
        try:
            import subprocess
            diff_res = subprocess.run(
                ["git", "diff", args.commit_range],
                cwd=str(project_root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True
            )
            patch_diff_map = _parse_multi_file_diff(diff_res.stdout)
        except Exception:
            pass

    for f in changed_files:
        full_path = project_root / f
        if full_path.exists():
            s_warns = mine_sibling_conventions(full_path)
            sibling_warnings.extend(s_warns)

            file_diff = patch_diff_map.get(f, "")
            if not file_diff:
                try:
                    import subprocess
                    d_res = subprocess.run(
                        ["git", "diff", "--", f],
                        cwd=str(project_root),
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True
                    )
                    if d_res.returncode == 0:
                        file_diff = d_res.stdout
                except Exception:
                    pass

            if file_diff:
                inv_warns = check_guard_stability(full_path, file_diff)
                invariant_alerts.extend(inv_warns)

    # 2e. Fragile Symbols Query (threshold >= 2)
    fragile_symbols = []
    symbol_cols = [r[1] for r in conn.execute("PRAGMA table_info(symbols)").fetchall()]
    if "fragility_score" in symbol_cols:
        for f in changed_files:
            f_alt = f.replace("/", "\\")
            f_rows = conn.execute("""
                SELECT s.name, s.fragility_score 
                FROM symbols s JOIN files f2 ON s.file_id = f2.id
                WHERE (f2.path = ? OR f2.path = ?) AND s.fragility_score >= 2
                ORDER BY s.fragility_score DESC LIMIT 5
            """, (f, f_alt)).fetchall()
            for r in f_rows:
                fragile_symbols.append({
                    "file": f,
                    "symbol": r["name"],
                    "score": r["fragility_score"]
                })

    # 2f. Retrieve Scoped Decision Links & Context
    from core.decision_linker import DecisionLinker
    linker = DecisionLinker(str(db_path))
    scoped_queries = []
    for f in changed_files:
        f_alt = f.replace("/", "\\")
        sym_rows = conn.execute("""
            SELECT s.name FROM symbols s JOIN files f2 ON s.file_id = f2.id
            WHERE f2.path = ? OR f2.path = ?
        """, (f, f_alt)).fetchall()
        for s_r in sym_rows:
            scoped_queries.append((f, s_r[0]))
    raw_links = linker.get_context(scoped_queries)
    links = []
    for rl in raw_links:
        links.append({
            "file": rl.get("file_path") or f,
            "symbol": rl["symbol_name"],
            "type": rl["source_type"],
            "ref": rl["source_ref"],
            "desc": rl.get("description", "")
        })

    # Filter out dismissed findings
    from core.symbol_db import SymbolDB
    db_obj = SymbolDB(db_path)
    dismissed = db_obj.get_dismissed_findings()
    db_obj.close()

    invariant_alerts = [
        ia for ia in invariant_alerts
        if ("invariant", ia["file"], "") not in dismissed and ("all", ia["file"], "") not in dismissed
    ]
    sibling_warnings = [
        sw for sw in sibling_warnings
        if ("sibling", sw["file"], sw["symbol"]) not in dismissed and ("all", sw["file"], sw["symbol"]) not in dismissed
    ]
    co_change_warnings = [
        cw for cw in co_change_warnings
        if ("co_change", cw["src"], "") not in dismissed and ("all", cw["src"], "") not in dismissed
    ]

    conn.close()

    # Output formatting
    if args.format == "json":
        print(json.dumps({
            "changed_files_count": len(changed_files),
            "invariant_alerts": invariant_alerts,
            "hotspots": hotspots,
            "fragile_symbols": fragile_symbols,
            "sibling_warnings": sibling_warnings,
            "symbol_co_change_warnings": symbol_co_change_warnings,
            "co_change_warnings": co_change_warnings,
            "has_test_coupling_warning": has_test_coupling_warning
        }, indent=2))
    elif args.format == "sarif":
        print(_generate_sarif_report(
            changed_files, hotspots, fragile_symbols, sibling_warnings,
            symbol_co_change_warnings, co_change_warnings, invariant_alerts, has_test_coupling_warning
        ))
    else:
        # Output markdown report
        print("# 🧬 LORE CI/CD Security & Architecture Audit\n")
        print(f"LORE analyzed **{len(changed_files)}** modified files in this Pull Request.\n")

        if invariant_alerts:
            print("### 🚨 Critical Invariant Alerts (Removed Entry Guards)")
            print("Historical entry guards or assertions altered or removed in this patch:")
            for ia in invariant_alerts:
                print(f"* {ia['msg']}")
            print()

        if hotspots:
            print("### ⚠️ Modified Architectural Hotspots")
            print("These files are high-risk hotspots. Ensure changes are reviewed carefully:")
            print("| File Path | Commits | Risk Score | Decision Links | Status |")
            print("|:---|:---:|:---:|:---:|:---|")
            for h in hotspots:
                is_amnesia = h["decision_links"] == 0 and h["change_freq"] >= 15 and not h.get("is_trivial", False)
                status = "🚨 Amnesia Warning" if is_amnesia else "✓ Documented"
                print(f"| `{h['path']}` | {h['change_freq']} | {h['risk_score']:.2f} | {h['decision_links']} | {status} |")
            print()

        if fragile_symbols:
            print("### 🔥 Historically Fragile Symbols (High Fix Count)")
            print("The following symbols have been patched multiple times for historical bugs:")
            for fs in fragile_symbols:
                print(f"* 🔥 **`{fs['symbol']}`** in `{fs['file']}` has been patched in **{fs['score']}** historical bugfix commits. High regression vulnerability.")
            print()

        if sibling_warnings:
            print("### 🏛️ Sibling Convention Warnings (Intra-Module Idioms)")
            print("Code convention deviations detected across sibling functions:")
            for sw in sibling_warnings:
                print(f"* {sw['msg']}")
            print()

        if symbol_co_change_warnings:
            print("### ⚡ Statistical Symbol Co-Change Alerts (Association Rules)")
            print("High-confidence association rules detected missing symbol changes:")
            for w in symbol_co_change_warnings:
                pct = int(w["confidence"] * 100)
                print(f"* 🚨 **`{w['src_sym']}`** in `{w['src_file']}` co-changed **{pct}%** of the time ({w['shared']}/{w['total_a']} commits) with **`{w['dst_sym']}`** in `{w['dst_file']}`. **`{w['dst_file']}` is NOT in this diff!** (Estimated Regression Risk: HIGH)")
            print()

        if co_change_warnings:
            print("### 🔗 File-Level Co-change Warnings")
            print("Historical patterns suggest that changes in these files usually require updates in related files:")
            for w in co_change_warnings:
                pct = int(w["rate"] * 100)
                print(f"* ⚠️ **`{w['src']}`** changes **{pct}%** of the time with **`{w['dst']}`**, which is **NOT** included in this PR. Did you forget to update it?")
            print()

        if has_test_coupling_warning:
            print("### 🧪 Test Coverage Coupling Warning")
            print("⚠️ **No test files were modified in this PR.** Modified source files (`" + ", ".join(core_source_files[:3]) + ("..." if len(core_source_files) > 3 else "") + "`) have high coupling with tests. Ensure new or updated tests are provided.\n")

        if links:
            print("### 📜 Design Decisions & Constraints")
            print("The following active rules and ADRs govern the modified code. Ensure these constraints are not violated:")
            for l in links:
                ref_label = l["ref"]
                if l["type"] == "commit":
                    ref_label = f"Commit `{l['ref'][:8]}`"
                print(f"* **`{l['file']}`** (symbol: `{l['symbol']}`): linked to **{ref_label}**")
                if l["desc"]:
                    print(f"  > *Context:* {l['desc']}")
            print()

        if not hotspots and not co_change_warnings and not links and not invariant_alerts:
            print("### ✓ Zero Risks Detected")
            print("LORE did not detect any hotspots, missing documentation risks, or co-change patterns for these modified files.")

    # Check exit code behavior
    has_critical = len(invariant_alerts) > 0
    has_warning = len(sibling_warnings) > 0 or len(co_change_warnings) > 0 or len(symbol_co_change_warnings) > 0 or has_test_coupling_warning

    if args.fail_on == "critical" and has_critical:
        sys.exit(1)
    elif args.fail_on == "warning" and (has_critical or has_warning):
        sys.exit(1)


def _generate_sarif_report(
    changed_files: list[str],
    hotspots: list[dict],
    fragile_symbols: list[dict],
    sibling_warnings: list[dict],
    symbol_co_change_warnings: list[dict],
    co_change_warnings: list[dict],
    invariant_alerts: list[dict],
    has_test_coupling_warning: bool
) -> str:
    rules = [
        {
            "id": "LORE001",
            "name": "CriticalInvariantAlert",
            "shortDescription": {"text": "Historical entry guard or assertion altered or removed"},
            "defaultConfiguration": {"level": "error"}
        },
        {
            "id": "LORE002",
            "name": "FragileSymbolAlert",
            "shortDescription": {"text": "Symbol modified has high historical bugfix frequency"},
            "defaultConfiguration": {"level": "warning"}
        },
        {
            "id": "LORE003",
            "name": "SiblingConventionWarning",
            "shortDescription": {"text": "Code convention deviation detected across sibling functions"},
            "defaultConfiguration": {"level": "warning"}
        }
    ]

    results = []
    for ia in invariant_alerts:
        results.append({
            "ruleId": "LORE001",
            "level": "error",
            "message": {"text": ia["msg"]},
            "locations": [{
                "physicalLocation": {
                    "artifactLocation": {"uri": ia["file"]}
                }
            }]
        })

    for fs in fragile_symbols:
        results.append({
            "ruleId": "LORE002",
            "level": "warning",
            "message": {"text": f"Symbol {fs['symbol']} has been patched in {fs['score']} bugfixes."},
            "locations": [{
                "physicalLocation": {
                    "artifactLocation": {"uri": fs["file"]}
                }
            }]
        })

    for sw in sibling_warnings:
        results.append({
            "ruleId": "LORE003",
            "level": "warning",
            "message": {"text": sw["msg"]},
            "locations": [{
                "physicalLocation": {
                    "artifactLocation": {"uri": sw["file"]}
                }
            }]
        })

    sarif = {
        "$schema": "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json",
        "version": "2.1.0",
        "runs": [{
            "tool": {
                "driver": {
                    "name": "LORE Guardian",
                    "version": "6.0.0",
                    "rules": rules
                }
            },
            "results": results
        }]
    }
    return json.dumps(sarif, indent=2)


def _get_function_param_name(file_path: Path, func_name: str, arg_index: int | None, arg_name: str | None) -> str | None:
    if not file_path.exists():
        return None
    suffix = file_path.suffix.lower()
    
    if suffix == ".py":
        try:
            import ast
            tree = ast.parse(file_path.read_text(encoding="utf-8", errors="replace"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name:
                    if arg_name:
                        for arg in node.args.args:
                            if arg.arg == arg_name:
                                return arg_name
                    elif arg_index is not None:
                        params = [arg.arg for arg in node.args.args]
                        if params and params[0] in ("self", "cls"):
                            if arg_index + 1 < len(params):
                                return params[arg_index + 1]
                        else:
                            if arg_index < len(params):
                                return params[arg_index]
        except Exception:
            pass
            
    elif suffix == ".go":
        try:
            from parsers.go_parser import _load_go_parser
            parser, _ = _load_go_parser()
            src_bytes = file_path.read_bytes()
            tree = parser.parse(src_bytes)
            
            params = []
            def walk(node):
                if node.type in ("function_declaration", "method_declaration"):
                    name_node = node.child_by_field_name("name")
                    if name_node and src_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8") == func_name:
                        param_list = node.child_by_field_name("parameters")
                        if param_list:
                            for param_decl in param_list.children:
                                if param_decl.type == "parameter_declaration":
                                    for child in param_decl.children:
                                        if child.type == "identifier":
                                            params.append(src_bytes[child.start_byte:child.end_byte].decode("utf-8"))
                        return True
                for child in node.children:
                    if walk(child):
                        return True
                return False
            walk(tree.root_node)
            
            if arg_name and arg_name in params:
                return arg_name
            elif arg_index is not None and arg_index < len(params):
                return params[arg_index]
        except Exception:
            pass
            
    elif suffix in (".ts", ".tsx", ".js", ".jsx"):
        try:
            from parsers.typescript_parser import _load_ts_parser
            parser, _ = _load_ts_parser()
            src_bytes = file_path.read_bytes()
            tree = parser.parse(src_bytes)
            
            params = []
            def walk(node):
                found = False
                param_list = None
                if node.type in ("function_declaration", "generator_function_declaration", "method_definition"):
                    name_node = node.child_by_field_name("name") or node.child_by_field_name("key")
                    if name_node and src_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8") == func_name:
                        found = True
                        param_list = node.child_by_field_name("parameters")
                elif node.type == "variable_declarator":
                    name_node = node.child_by_field_name("name")
                    value_node = node.child_by_field_name("value")
                    if name_node and src_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8") == func_name:
                        if value_node and value_node.type in ("arrow_function", "function", "function_expression"):
                            found = True
                            param_list = value_node.child_by_field_name("parameters")
                            
                if found and param_list:
                    for param in param_list.children:
                        if param.type in ("required_parameter", "optional_parameter", "identifier"):
                            for child in param.children:
                                if child.type == "identifier":
                                    params.append(src_bytes[child.start_byte:child.end_byte].decode("utf-8"))
                                    break
                            else:
                                if param.type == "identifier":
                                    params.append(src_bytes[param.start_byte:param.end_byte].decode("utf-8"))
                    return True
                for child in node.children:
                    if walk(child):
                        return True
                return False
            walk(tree.root_node)
            
            if arg_name and arg_name in params:
                return arg_name
            elif arg_index is not None and arg_index < len(params):
                return params[arg_index]
        except Exception:
            pass
            
    return None


def _parse_multi_file_diff(diff_text: str) -> dict[str, str]:
    """Parse a multi-file unified diff into a dict: {relative_file_path: diff_content_string}."""
    diff_text = diff_text.replace("\r\n", "\n")
    files_diffs = {}
    current_file = None
    current_diff_lines = []
    
    for line in diff_text.splitlines():
        if line.startswith("--- "):
            pass
        elif line.startswith("+++ "):
            # Extract filepath target
            target = line[4:].strip()
            if target.startswith("b/"):
                target = target[2:]
            # Clean trailing metadata
            target = target.split("\t")[0].split()[0]
            
            if current_file:
                files_diffs[current_file] = "\n".join(current_diff_lines)
            current_file = target.replace("\\", "/")
            current_diff_lines = []
        elif current_file is not None:
            current_diff_lines.append(line)
            
    if current_file:
        files_diffs[current_file] = "\n".join(current_diff_lines)
        
    return files_diffs


def _apply_unified_diff(original_text: str, diff_text: str) -> str:
    """Apply unified diff lines to the original text in-memory and return patched string."""
    original_text = original_text.replace("\r\n", "\n")
    diff_text = diff_text.replace("\r\n", "\n")
    
    lines = original_text.splitlines(keepends=True)
    diff_lines = diff_text.splitlines()
    
    hunks = []
    current_hunk = None
    
    for line in diff_lines:
        if line.startswith("@@"):
            parts = line.split()
            if len(parts) < 3:
                continue
            old_part = parts[1].lstrip("-").split(",")
            old_start = int(old_part[0])
            old_len = int(old_part[1]) if len(old_part) > 1 else 1
            
            new_part = parts[2].lstrip("+").split(",")
            new_start = int(new_part[0])
            new_len = int(new_part[1]) if len(new_part) > 1 else 1
            
            current_hunk = {
                "old_start": old_start,
                "old_len": old_len,
                "new_start": new_start,
                "new_len": new_len,
                "lines": []
            }
            hunks.append(current_hunk)
        elif current_hunk is not None:
            if line.startswith(("---", "+++")):
                continue
            current_hunk["lines"].append(line)
            
    hunks.sort(key=lambda h: h["old_start"], reverse=True)
    
    for h in hunks:
        old_idx = h["old_start"] - 1
        if old_idx < 0:
            old_idx = 0
        old_end = old_idx + h["old_len"]
        
        replacement = []
        for line in h["lines"]:
            if line.startswith("+"):
                replacement.append(line[1:] + "\n")
            elif line.startswith(" "):
                replacement.append(line[1:] + "\n")
                
        lines[old_idx:old_end] = replacement
        
    return "".join(lines)


def _generate_proof_certificate(project_name: str, db_path: str, patch_path: str, patch_content: str, cured_paths_details: list[dict]) -> str:
    """Generate a formal verification proof certificate in Markdown format sealed with SHA-256."""
    import hashlib
    from datetime import datetime as _dt
    from pathlib import Path
    
    # 1. Compute SHA-256 seal
    hasher = hashlib.sha256()
    hasher.update(patch_content.encode("utf-8", errors="replace"))
    hasher.update(db_path.encode("utf-8", errors="replace"))
    seal_hash = hasher.hexdigest()
    
    # 2. Build Markdown content
    proof = []
    proof.append(f"# 🛡️ LORE Formal Safety Verification Proof Certificate\n")
    proof.append(f"## Verification Metadata")
    proof.append(f"- **Verification Date**: {_dt.now().strftime('%Y-%m-%d %H:%M:%S')}")
    proof.append(f"- **Target Project**: `{project_name}`")
    proof.append(f"- **Knowledge Graph Database**: `{db_path}`")
    proof.append(f"- **Input Patch File**: `{patch_path}`")
    proof.append(f"- **Cryptographic Seal Signature**: `{seal_hash}`\n")
    
    proof.append(f"## Safety Theorem")
    proof.append(f"> **Theorem**: Let $\\mathcal{{S}}$ be the set of public inputs (Sources) and $\\mathcal{{T}}$ be the set of security-sensitive execution functions (Sinks). For the mutated Causal Graph $\\mathcal{{G}}'$ resulting from the in-memory application of patch `{Path(patch_path).name}`, there exists no active dataflow taint path $p = (s, v_1, v_2, \\dots, t)$ where $s \\in \\mathcal{{S}}$ and $t \\in \\mathcal{{T}}$.\n")
    
    proof.append(f"## Verifiable Proof Steps")
    proof.append(f"The Counterfactual Patch Engine verified that the proposed patch acts as a **cut-set** blocking all source-to-sink pathways:\n")
    
    for idx, cp in enumerate(cured_paths_details, 1):
        proof.append(f"### Path {idx} Proof: Flow Blocked")
        proof.append(f"- **Exposed Chain**: " + " → ".join(f"`{f}`" for f in cp["path"]))
        proof.append(f"- **Original Baseline Flow**: Variable `{cp['source_var']}` (from `{cp['source_desc']}`) propagated to sink `{cp['sink_name']}()`.")
        proof.append(f"- **Cut-Set Verification**: The patch successfully modified the source code to break this taint propagation chain.")
        proof.append(f"  - *Verification State*: **CURED** (Verified in-memory post-patch AST check returned 0 active flows).\n")
        
    proof.append(f"## Status Verdict")
    proof.append(f"### [✓] VERIFIED SECURE")
    proof.append(f"The program modifications carry a formal proof certificate showing that all taint paths are successfully broken. No new vulnerability paths were introduced by the patch.")
    
    return "\n".join(proof)


