from __future__ import annotations
import os
import sys
import re
import hashlib
import difflib
from pathlib import Path
from rich import box
from rich.text import Text
from rich.panel import Panel
from cli.shared import console, STAGE_SUBDIR, MODEL
from cli.gh_check import _parse_multi_file_diff, _apply_unified_diff, _get_function_param_name, _generate_proof_certificate
from cli.vuln_analysis import _calculate_path_risk_score, _trace_file_taint


AUTO_CURE_PATCH_SYSTEM = """You are a security engineering agent. Your goal is to write a unified diff patch that cures a source-to-sink vulnerability taint path in the codebase.
The patch must break the dataflow taint propagation from the Source to the Sink.
Ensure that:
1. Legitimate inputs continue to work correctly (happy path).
2. Malicious inputs are validated, sanitized, or rejected safely (sad path).
3. You do NOT break the functionality of other parts of the system.
4. If a vulnerable function like `eval` or `exec` can be replaced with a safer alternative (e.g. `ast.literal_eval`, `json.loads`, or a safe dispatcher dictionary), prefer that replacement.

Return ONLY a unified diff patch in the standard unified diff format (with ---, +++, @@ headers). Do not wrap the diff in markdown code blocks. Do not add any conversational text or comments outside the diff."""

AUTO_CURE_TEST_SYSTEM = """You are a QA automation agent. Your goal is to write a standalone, self-contained Python script named `test_lore_qa_behavior.py` that verifies the correctness and security of a proposed patch.
The script will run in a sandbox environment that has the project files copied.
The test script MUST:
1. Verify the HAPPY PATH: call the function/view/helper with a benign, safe input and assert that it executes normally and returns/produces the expected output.
2. Verify the SAD PATH: call the function/view/helper with a malicious/invalid input and assert that it is blocked, raises a validation error (like ValueError), or is handled safely without reaching the dangerous sink.
3. Print "VERIFICATION: SUCCESS" on stdout and exit with code 0 if and only if both paths pass.
4. Exit with a non-zero code (or raise an AssertionError) if any check fails.
5. Use mocking (e.g. unittest.mock) if external resources like HTTP requests or database calls are required, to ensure the test is fully self-contained.

Return ONLY the executable Python code for the test script. Do not wrap the code in markdown code blocks or add any markdown fences or explanation."""


def _run_patch_validation(
    project_root: Path,
    conn,
    db_path: Path,
    exposed_paths: list[list[str]],
    auto_cure: bool,
    patch_path_str: str | None
) -> dict:
    """Simulate patches statically and dynamically to confirm vulnerability curation and prevent regressions."""
    patched_files = {}
    diff_text = ""
    if patch_path_str:
        patch_path = Path(patch_path_str)
        if not patch_path.exists():
            console.print(f"[error]✖ Patch file not found: {patch_path}[/]")
            sys.exit(1)
        try:
            diff_text = patch_path.read_text(encoding="utf-8", errors="replace")
            patched_files = _parse_multi_file_diff(diff_text)
            console.print(f"[info]🔮 Counterfactual Engine: loaded patch for [bold cyan]{len(patched_files)}[/] files.[/]")
        except Exception as e:
            console.print(f"[error]✖ Failed to read/parse patch file: {e}[/]")
            sys.exit(1)

    cured_paths_details = []
    survived_paths_count = 0
    new_paths_count = 0
    regression_paths_count = 0
    baseline_active_paths_count = 0
    original_contents_backup = {}
    all_auto_cured_files = {}

    if exposed_paths:
        console.print("[error]🚨 Exposed Source-to-Sink Paths (Taint Propagation)[/]")
        console.print("[dim]These public entrypoints route untrusted input to sensitive serialization/execution sinks:[/]")
        
        for i, p in enumerate(exposed_paths):
            risk_score = _calculate_path_risk_score(p, conn, project_root)
            risk_pct = risk_score * 100
            if risk_score >= 0.85:
                sev_label = "CRITICAL"
                sev_style = "bold red"
            elif risk_score >= 0.70:
                sev_label = "HIGH"
                sev_style = "bold yellow"
            elif risk_score >= 0.40:
                sev_label = "MEDIUM"
                sev_style = "yellow"
            else:
                sev_label = "LOW"
                sev_style = "green"
            
            path_str = " → \n  ".join(f"[bold cyan]{f}[/]" for f in p)
            console.print(f"\n[bold red][Path {i+1}][/] (Risk Score: [bold]{risk_pct:.1f}%[/] - [{sev_style}]{sev_label}[/])\n  {path_str}")
            
            # 1. Run Baseline (Without Patch)
            current_taint_sources = set()
            baseline_has_flow = False
            first_flow_info = {"source_var": "unknown", "source_desc": "unknown", "sink_name": "unknown"}
            for idx_f, fpath in enumerate(p):
                abs_fpath = project_root / fpath
                if abs_fpath.exists() and abs_fpath.suffix.lower() in (".py", ".go", ".ts", ".tsx", ".js", ".jsx"):
                    try:
                        code_txt = original_contents_backup.get(fpath) or abs_fpath.read_text(encoding="utf-8", errors="replace")
                        res = _trace_file_taint(fpath, code_txt, current_taint_sources)
                        flows = res.get("flows", [])
                        outgoing_calls = res.get("outgoing_calls", [])
                        if flows:
                            baseline_has_flow = True
                            first_flow_info = {
                                "source_var": flows[0]["var_name"],
                                "source_desc": flows[0]["source_desc"],
                                "sink_name": flows[0]["sink_name"]
                            }
                        next_taint_sources = set()
                        if idx_f + 1 < len(p):
                            next_fpath = p[idx_f + 1]
                            abs_next_fpath = project_root / next_fpath
                            for call in outgoing_calls:
                                param_name = _get_function_param_name(
                                    abs_next_fpath,
                                    call["func_name"],
                                    call.get("arg_index"),
                                    call.get("arg_name")
                                )
                                if param_name:
                                    next_taint_sources.add(param_name)
                        current_taint_sources = next_taint_sources
                    except Exception as e:
                        console.print(f"[warning]⚠️ Inter-procedural flow tracing error on baseline path: {e}[/]")

            if baseline_has_flow:
                baseline_active_paths_count += 1
                
                if auto_cure:
                    console.print(f"            [phase]🔮 LORE Auto-Cure: Attempting to cure active path {i+1}...[/]")
                    
                    for fpath in p:
                        if fpath not in original_contents_backup:
                            abs_f = project_root / fpath
                            if abs_f.exists():
                                original_contents_backup[fpath] = abs_f.read_text(encoding="utf-8", errors="replace")
                            else:
                                original_contents_backup[fpath] = ""
                                
                    constraints = []
                    try:
                        for fpath in p:
                            rows = conn.execute("""
                                SELECT dl.symbol_name, dl.source_ref, dl.confidence, dl.description 
                                FROM decision_links dl
                                JOIN symbols s ON dl.symbol_name = s.name
                                JOIN files f ON s.file_id = f.id
                                WHERE f.path = ? OR f.path = ?
                            """, (fpath, fpath.replace("/", "\\"))).fetchall()
                            for r in rows:
                                constraints.append(f"- Symbol '{r['symbol_name']}' constrained by {r['source_ref']} (Confidence: {r['confidence']}): {r['description']}")
                    except Exception:
                        pass
                        
                    path_source_code = {}
                    for fpath in p:
                        abs_p = project_root / fpath
                        if abs_p.exists():
                            path_source_code[fpath] = abs_p.read_text(encoding="utf-8", errors="replace")
                            
                    user_prompt = f"Task: Break/cure the active security taint propagation flow in this path.\n\n"
                    user_prompt += "=== TAINT PATH ===\n" + " -> ".join(p) + "\n\n"
                    if constraints:
                        user_prompt += "=== DESIGN CONSTRAINTS (ADR / Invariants) ===\n"
                        user_prompt += "\n".join(constraints) + "\n\n"
                    user_prompt += "=== SOURCE CODE OF FILES IN PATH ===\n"
                    for fpath, code in path_source_code.items():
                        user_prompt += f"--- FILE: {fpath} ---\n{code}\n\n"
                    user_prompt += "=== DETECTED VULNERABILITY DATAFLOW DETAILS ===\n"
                    user_prompt += f"- Source Variable: {first_flow_info['source_var']}\n"
                    user_prompt += f"- Source Description: {first_flow_info['source_desc']}\n"
                    user_prompt += f"- Sink Function: {first_flow_info['sink_name']}\n"
                    
                    from core.llm_client import get_llm_client
                    client = get_llm_client(project_root)
                    
                    cured_successfully = False
                    for attempt in range(1, 4):
                        console.print(f"            [info]Auto-Cure Attempt {attempt}/3: Generating code patch via {MODEL}...[/]")
                        try:
                            response = client.messages.create(
                                model=MODEL,
                                max_tokens=2048,
                                system=AUTO_CURE_PATCH_SYSTEM,
                                messages=[{"role": "user", "content": user_prompt}]
                            )
                            patch_candidate = response.content[0].text.strip()
                            patch_candidate = re.sub(r"^```(?:diff)?\s*\n?", "", patch_candidate, flags=re.MULTILINE)
                            patch_candidate = re.sub(r"\n?```\s*$", "", patch_candidate, flags=re.MULTILINE)
                            patch_candidate = patch_candidate.strip()
                            
                            candidate_patched_files = _parse_multi_file_diff(patch_candidate)
                        except Exception as e:
                            console.print(f"            [warning]⚠️ Attempt {attempt} failed to generate/parse patch: {e}. Retrying...[/]")
                            user_prompt += f"\n\nAttempt {attempt} failed: {e}. Please output a valid unified diff patch."
                            continue
                            
                        current_ts = set()
                        candidate_has_flow = False
                        for idx_f, fpath in enumerate(p):
                            abs_fpath = project_root / fpath
                            if abs_fpath.exists() and abs_fpath.suffix.lower() in (".py", ".go", ".ts", ".tsx", ".js", ".jsx"):
                                try:
                                    code_txt = abs_fpath.read_text(encoding="utf-8", errors="replace")
                                    if fpath in candidate_patched_files:
                                        code_txt = _apply_unified_diff(code_txt, candidate_patched_files[fpath])
                                    res = _trace_file_taint(fpath, code_txt, current_ts)
                                    if res.get("flows"):
                                        candidate_has_flow = True
                                    next_ts = set()
                                    if idx_f + 1 < len(p):
                                        next_fpath = p[idx_f + 1]
                                        abs_next_fpath = project_root / next_fpath
                                        for call in res.get("outgoing_calls", []):
                                            param_name = _get_function_param_name(
                                                abs_next_fpath,
                                                call["func_name"],
                                                call.get("arg_index"),
                                                call.get("arg_name")
                                            )
                                            if param_name:
                                                next_ts.add(param_name)
                                    current_ts = next_ts
                                except Exception:
                                    candidate_has_flow = True
                                    
                        if candidate_has_flow:
                            console.print("            [warning]⚠️ Patch failed static Counterfactual check. Retrying...[/]")
                            user_prompt += f"\n\nAttempt {attempt} failed: The generated patch did not break the taint flow. Please refine the patch."
                            continue
                            
                        console.print("            [info]Static check passed! Generating QA behavior test...[/]")
                        test_prompt = f"Write a behavioral test for the proposed patch on files: {', '.join(p)}.\n\n"
                        test_prompt += "=== PROPOSED PATCH ===\n" + patch_candidate + "\n\n"
                        test_prompt += "=== ORIGINAL FILE CONTENTS ===\n"
                        for fpath, code in path_source_code.items():
                            test_prompt += f"--- FILE: {fpath} ---\n{code}\n\n"
                            
                        try:
                            test_response = client.messages.create(
                                model=MODEL,
                                max_tokens=2048,
                                system=AUTO_CURE_TEST_SYSTEM,
                                messages=[{"role": "user", "content": test_prompt}]
                            )
                            test_code = test_response.content[0].text.strip()
                            test_code = re.sub(r"^```(?:python)?\s*\n?", "", test_code, flags=re.MULTILINE)
                            test_code = re.sub(r"\n?```\s*$", "", test_code, flags=re.MULTILINE)
                            test_code = test_code.strip()
                            
                            from core.qa_engine import run_agentic_qa_test
                            qa_res = run_agentic_qa_test(project_root, candidate_patched_files, test_code)
                        except Exception as e:
                            console.print(f"            [warning]⚠️ Attempt {attempt} failed during QA test setup/run: {e}. Retrying...[/]")
                            user_prompt += f"\n\nAttempt {attempt} failed during test generation/execution: {e}."
                            continue
                            
                        if qa_res["success"] and "VERIFICATION: SUCCESS" in qa_res["stdout"]:
                            console.print("            [success]✔ Dynamic QA verification passed! Happy and sad paths validated.[/]")
                            for fpath, diff in candidate_patched_files.items():
                                abs_fpath = project_root / fpath
                                if abs_fpath.exists():
                                    orig_code = abs_fpath.read_text(encoding="utf-8")
                                    patched_code = _apply_unified_diff(orig_code, diff)
                                    abs_fpath.write_text(patched_code, encoding="utf-8")
                                    all_auto_cured_files[fpath] = patched_code
                                    console.print(f"            [success]✔ Applied patch to {fpath}[/]")
                            cured_successfully = True
                            patched_files.update(candidate_patched_files)
                            patch_path_str = "auto_cure"
                            break
                        else:
                            error_details = qa_res.get("error_msg") or f"Exit code {qa_res['exit_code']}\nStdout: {qa_res['stdout']}\nStderr: {qa_res['stderr']}"
                            console.print(f"            [warning]⚠️ Dynamic QA validation failed. Retrying...[/]")
                            user_prompt += f"\n\nAttempt {attempt} failed dynamic QA verification. Error/Output:\n{error_details}\nPlease modify the patch to pass the verification."
                            
                    if not cured_successfully:
                        console.print(f"            [error]✖ Failed to auto-cure path {i+1} after 3 attempts.[/]")

            # 2. Run Patched (With Patch)
            current_taint_sources = set()
            patched_has_flow = False
            patch_applied_files = []
            for idx_f, fpath in enumerate(p):
                abs_fpath = project_root / fpath
                if abs_fpath.exists() and abs_fpath.suffix.lower() in (".py", ".go", ".ts", ".tsx", ".js", ".jsx"):
                    try:
                        code_txt = original_contents_backup.get(fpath) or abs_fpath.read_text(encoding="utf-8", errors="replace")
                        if fpath in patched_files:
                            code_txt = _apply_unified_diff(code_txt, patched_files[fpath])
                            patch_applied_files.append(fpath)
                        res = _trace_file_taint(fpath, code_txt, current_taint_sources)
                        flows = res.get("flows", [])
                        outgoing_calls = res.get("outgoing_calls", [])
                        if flows:
                            patched_has_flow = True
                        next_taint_sources = set()
                        if idx_f + 1 < len(p):
                            next_fpath = p[idx_f + 1]
                            abs_next_fpath = project_root / next_fpath
                            for call in outgoing_calls:
                                param_name = _get_function_param_name(
                                    abs_next_fpath,
                                    call["func_name"],
                                    call.get("arg_index"),
                                    call.get("arg_name")
                                )
                                if param_name:
                                    next_taint_sources.add(param_name)
                        current_taint_sources = next_taint_sources
                    except Exception as e:
                        console.print(f"[warning]⚠️ Inter-procedural flow tracing error on patched path: {e}[/]")

            # 3. Verdict Output
            if patch_path_str:
                if patch_applied_files:
                    console.log(f"    [info]🔮 Counterfactual Simulation: applied proposed patch to {', '.join(patch_applied_files)}[/]")
                else:
                    console.log("    [dim]🔮 Counterfactual Simulation: patch did not modify any file in this path[/]")
                    
                if patched_has_flow:
                    path_str = ",".join(p)
                    path_hash = hashlib.sha256(path_str.encode("utf-8")).hexdigest()
                    is_regression = False
                    try:
                        row_reg = conn.execute("SELECT id FROM historical_vulns WHERE path_fingerprint = ?", (path_hash,)).fetchone()
                        if row_reg:
                            is_regression = True
                    except Exception:
                        pass
                    
                    if is_regression:
                        console.print("    [bold red]🚨 VERDICT: VULNERABILITY REGRESSION DETECTED (This path was previously cured!)[/]")
                        regression_paths_count += 1
                    elif baseline_has_flow:
                        console.print("    [bold red]🚨 VERDICT: PATH SURVIVED (Patch is ineffective, taint still propagates!)[/]")
                        survived_paths_count += 1
                    else:
                        console.print("    [bold yellow]⚠️ VERDICT: NEW VULNERABILITY INTRODUCED BY PATCH![/]")
                        new_paths_count += 1
                elif baseline_has_flow and not patched_has_flow:
                    console.log("    [bold green]✓ VERDICT: PATH CURED BY PATCH (Taint flow broken successfully!)[/]")
                    cured_paths_details.append({
                        "path": p,
                        "source_var": first_flow_info["source_var"],
                        "source_desc": first_flow_info["source_desc"],
                        "sink_name": first_flow_info["sink_name"]
                    })
                else:
                    console.print("    [green]✓ VERDICT: INACTIVE (No taint flow detected)[/]")

            # 4. Print detailed active flows
            current_taint_sources = set()
            for idx_f, fpath in enumerate(p):
                abs_fpath = project_root / fpath
                if abs_fpath.exists() and abs_fpath.suffix == ".py":
                    try:
                        from core.ast_patcher import check_ast_taint_interprocedural
                        code_txt = original_contents_backup.get(fpath) or abs_fpath.read_text(encoding="utf-8", errors="replace")
                        if patch_path_str and fpath in patched_files:
                            code_txt = _apply_unified_diff(code_txt, patched_files[fpath])
                        res = check_ast_taint_interprocedural(code_txt, current_taint_sources)
                        flows = res.get("flows", [])
                        outgoing_calls = res.get("outgoing_calls", [])
                        
                        fname = fpath.replace("\\\\", "/").replace("\\", "/").split("/")[-1]
                        
                        if flows:
                            console.print(f"    [bold yellow]🔍 AST Variable Taint Flow in {fname}:[/]")
                            for flow in flows:
                                console.print(f"      • Line {flow['source_line']}: Input source [magenta]{flow['source_desc']}[/] assigned to [cyan]{flow['var_name']}[/]")
                                console.print(f"      • Line {flow['sink_line']}: Tainted [cyan]{flow['var_name']}[/] flows into [bold red]{flow['sink_name']}()[/]")
                                console.print(f"        [dim]Code: {flow['code_snippet']}[/]")
                                
                        next_taint_sources = set()
                        if idx_f + 1 < len(p):
                            next_fpath = p[idx_f + 1]
                            next_fname = next_fpath.replace("\\\\", "/").replace("\\", "/").split("/")[-1]
                            abs_next_fpath = project_root / next_fpath
                            
                            for call in outgoing_calls:
                                param_name = _get_function_param_name(
                                    abs_next_fpath,
                                    call["func_name"],
                                    call.get("arg_index"),
                                    call.get("arg_name")
                                )
                                if param_name:
                                    next_taint_sources.add(param_name)
                                    if flows or (patch_path_str and patched_has_flow) or not patch_path_str:
                                        console.print(f"      [info]➜ Propagating taint from argument '{call['var_name']}' to parameter [bold cyan]{param_name}[/] of {next_fname} in call [magenta]{call['func_name']}()[/]")
                        current_taint_sources = next_taint_sources
                    except Exception as e:
                        console.print(f"[warning]⚠️ Error during detailed active flow display: {e}[/]")

        if all_auto_cured_files:
            consolidated_diffs = []
            for fpath, patched_content in all_auto_cured_files.items():
                original = original_contents_backup[fpath]
                diff_lines = list(difflib.unified_diff(
                    original.splitlines(keepends=True),
                    patched_content.splitlines(keepends=True),
                    fromfile=f"a/{fpath}",
                    tofile=f"b/{fpath}",
                ))
                if diff_lines:
                    consolidated_diffs.append("".join(diff_lines))
                    
            if consolidated_diffs:
                combined_diff_text = "\n".join(consolidated_diffs)
                lore_dir = project_root / ".lore"
                lore_dir.mkdir(parents=True, exist_ok=True)
                auto_patch_file = lore_dir / "auto_cure.patch"
                auto_patch_file.write_text(combined_diff_text, encoding="utf-8")
                
                patch_path_str = str(auto_patch_file)
                diff_text = combined_diff_text
                patched_files = _parse_multi_file_diff(combined_diff_text)
                
        console.print()

        # 5. Generate Proof Certificate
        if patch_path_str:
            hasher = hashlib.sha256()
            hasher.update(diff_text.encode("utf-8", errors="replace"))
            hasher.update(str(db_path).encode("utf-8", errors="replace"))
            seal_hash = hasher.hexdigest()
            
            if baseline_active_paths_count > 0 and survived_paths_count == 0 and new_paths_count == 0 and regression_paths_count == 0:
                try:
                    proof_txt = _generate_proof_certificate(project_root.name, str(db_path), patch_path_str, diff_text, cured_paths_details)
                    proof_path = Path(patch_path_str + ".proof")
                    proof_path.write_text(proof_txt, encoding="utf-8")
                except Exception as ex:
                    console.print(f"[error]✖ Failed to write proof file: {ex}[/]")
                
                for cp in cured_paths_details:
                    path_str = ",".join(cp["path"])
                    path_hash = hashlib.sha256(path_str.encode("utf-8")).hexdigest()
                    try:
                        conn.execute("""
                            INSERT OR IGNORE INTO historical_vulns (source_symbol, sink_symbol, path_fingerprint, description)
                            VALUES (?, ?, ?, ?)
                        """, (cp["source_desc"], cp["sink_name"], path_hash, f"Cured taint path: {path_str}"))
                    except Exception as ex:
                        console.print(f"[warning]⚠️ Failed to insert into historical_vulns: {ex}[/]")
                try:
                    conn.commit()
                except Exception:
                    pass
                
                cert_text = Text()
                cert_text.append("🛡️  LORE SECURITY VERIFICATION CERTIFICATE\n\n", style="bold green")
                cert_text.append("Verdict:         ", style="bold")
                cert_text.append("VERIFIED SECURE (Proof carry verified)\n", style="bold green")
                cert_text.append("Cured Paths:     ", style="bold")
                cert_text.append(f"{len(cured_paths_details)} / {baseline_active_paths_count} vulnerability flows blocked\n", style="green")
                cert_text.append("New Paths:       ", style="bold")
                cert_text.append("0 detected\n", style="green")
                cert_text.append("Proof Seal:      ", style="bold")
                cert_text.append(f"{seal_hash[:32]}...\n\n", style="cyan")
                cert_text.append("Verification Proof Certificate saved successfully to:\n", style="dim")
                cert_text.append(f"{patch_path_str}.proof", style="underline bold cyan")
                
                console.print(Panel(
                    cert_text,
                    border_style="green",
                    box=box.DOUBLE,
                    expand=False
                ))
            else:
                fail_text = Text()
                fail_text.append("❌  LORE SECURITY VERIFICATION FAILED\n\n", style="bold red")
                fail_text.append("Verdict:         ", style="bold")
                fail_text.append("UNVERIFIED / INCOMPLETE\n", style="bold red")
                if regression_paths_count > 0:
                    fail_text.append("Regressions:     ", style="bold")
                    fail_text.append(f"{regression_paths_count} regressions detected\n", style="bold red")
                fail_text.append("Surviving Paths: ", style="bold")
                fail_text.append(f"{survived_paths_count} paths still active\n", style="bold red")
                fail_text.append("New Paths:       ", style="bold")
                fail_text.append(f"{new_paths_count} introduced by patch\n\n", style="bold red")
                fail_text.append("Proof Certificate could not be generated. Please refine the patch to block all remaining taint flows.", style="dim")
                
                console.print(Panel(
                    fail_text,
                    border_style="red",
                    box=box.DOUBLE,
                    expand=False
                ))
            console.print()
            
    return {
        "baseline_active_paths_count": baseline_active_paths_count,
        "survived_paths_count": survived_paths_count,
        "new_paths_count": new_paths_count,
        "regression_paths_count": regression_paths_count,
        "cured_paths_details": cured_paths_details
    }
