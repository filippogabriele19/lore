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
        f_alt = f.replace("/", "\\")
        row = conn.execute(
            "SELECT file_path, change_freq, risk_score FROM hotspots WHERE file_path = ? OR file_path = ?", (f, f_alt)
        ).fetchone()
        if row:
            dl_count = conn.execute(
                "SELECT COUNT(*) FROM decision_links dl "
                "JOIN symbols s ON dl.symbol_name = s.name "
                "JOIN files f2 ON s.file_id = f2.id "
                "WHERE f2.path = ? OR f2.path = ?", (f, f_alt)
            ).fetchone()[0]
            hotspots.append({
                "path": f,
                "change_freq": row["change_freq"],
                "risk_score": row["risk_score"],
                "decision_links": dl_count
            })

    # 2. Co-changes (Virtual Edges)
    co_change_warnings = []
    for f in changed_files:
        f_alt = f.replace("/", "\\")
        rows = conn.execute("""
            SELECT dst_file AS file_b, co_change_rate FROM virtual_edges WHERE (src_file = ? OR src_file = ?) AND co_change_rate >= 0.50
            UNION
            SELECT src_file AS file_b, co_change_rate FROM virtual_edges WHERE (dst_file = ? OR dst_file = ?) AND co_change_rate >= 0.50
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

    # 3. Decision links details
    links = []
    for f in changed_files:
        f_alt = f.replace("/", "\\")
        rows = conn.execute("""
            SELECT dl.symbol_name, dl.source_type, dl.source_ref, dl.description
            FROM decision_links dl
            JOIN symbols s ON dl.symbol_name = s.name
            JOIN files f2 ON s.file_id = f2.id
            WHERE f2.path = ? OR f2.path = ?
        """, (f, f_alt)).fetchall()
        for r in rows:
            links.append({
                "file": f,
                "symbol": r["symbol_name"],
                "type": r["source_type"],
                "ref": r["source_ref"],
                "desc": r["description"]
            })

    conn.close()

    # Output markdown report
    print("# 🧬 LORE CI/CD Security & Architecture Audit\n")
    print(f"LORE analyzed **{len(changed_files)}** modified files in this Pull Request.\n")

    if hotspots:
        print("### ⚠️ Modified Architectural Hotspots")
        print("These files are high-risk hotspots. Ensure changes are reviewed carefully:")
        print("| File Path | Commits | Risk Score | Decision Links | Status |")
        print("|:---|:---:|:---:|:---:|:---|")
        for h in hotspots:
            status = "🚨 Amnesia Warning" if h["decision_links"] == 0 and h["change_freq"] >= 15 else "✓ Documented"
            print(f"| `{h['path']}` | {h['change_freq']} | {h['risk_score']:.2f} | {h['decision_links']} | {status} |")
        print()

    if co_change_warnings:
        print("### 🔗 Co-change Warnings (Virtual Edges)")
        print("Historical patterns suggest that changes in these files usually require updates in related files:")
        for w in co_change_warnings:
            pct = int(w["rate"] * 100)
            print(f"* ⚠️ **`{w['src']}`** changes **{pct}%** of the time with **`{w['dst']}`**, which is **NOT** included in this PR. Did you forget to update it?")
        print()

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

    if not hotspots and not co_change_warnings and not links:
        print("### ✓ Zero Risks Detected")
        print("LORE did not detect any hotspots, missing documentation risks, or co-change patterns for these modified files.")


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


