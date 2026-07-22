"""
core/invariant_miner.py — Code Invariant & Convention Mining Engine
Mines temporal code invariants (Guard Stability) and structural module conventions (Sibling Conventions).
"""

import ast
import re
import logging
from pathlib import Path
from collections import defaultdict
from typing import List, Dict, Set, Tuple

logger = logging.getLogger(__name__)


def extract_guard_clauses(func_node: ast.AST) -> List[str]:
    """
    Extract guard clause descriptions from a function's AST node.
    Recognizes:
    - isinstance() checks
    - if arg is None / if not arg checks
    - attribute guards (e.g. self._state.adding)
    - assert statements
    - early returns
    """
    guards = []
    if not isinstance(func_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return guards

    for stmt in func_node.body:
        # 1. Assert statements
        if isinstance(stmt, ast.Assert):
            try:
                guards.append(f"assert {ast.unparse(stmt.test)}")
            except Exception:
                guards.append("assert statement")

        # 2. Top-level If statements acting as guards
        elif isinstance(stmt, ast.If):
            try:
                cond_str = ast.unparse(stmt.test)
                if "isinstance" in cond_str:
                    guards.append(f"isinstance check ({cond_str})")
                elif "is None" in cond_str or "not " in cond_str:
                    guards.append(f"null/empty guard ({cond_str})")
                elif "self._" in cond_str:
                    guards.append(f"state guard ({cond_str})")
                else:
                    guards.append(f"guard if ({cond_str})")
            except Exception:
                guards.append("if guard")

        # 3. Try blocks acting as validation guards
        elif isinstance(stmt, ast.Try):
            guards.append("try/except validation block")

        # Stop looking past the first few statements to focus on entry guards
        if len(guards) >= 5:
            break

    return guards


def mine_sibling_conventions(file_path: Path) -> List[Dict]:
    """
    Mine intra-file sibling conventions for Python, Go, and TypeScript/JavaScript.
    If >= 70% of sibling functions in a file start with an entry guard,
    flag any function in that file that lacks this guard.
    """
    warnings = []
    if not file_path.exists():
        return warnings

    suffix = file_path.suffix.lower()
    if suffix == ".go":
        return _mine_go_sibling_conventions(file_path)
    elif suffix in (".ts", ".tsx", ".js", ".jsx"):
        return _mine_ts_sibling_conventions(file_path)
    elif suffix != ".py":
        return warnings

    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source)
    except Exception as e:
        logger.debug(f"Failed to parse AST for sibling convention mining: {e}")
        return warnings

    functions = [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if len(functions) < 3:
        return warnings

    # Track guard features across functions
    func_guards: Dict[str, List[str]] = {}
    has_isinstance_count = 0
    has_any_guard_count = 0

    for fn in functions:
        guards = extract_guard_clauses(fn)
        func_guards[fn.name] = guards
        if any("isinstance" in g for g in guards):
            has_isinstance_count += 1
        if len(guards) > 0:
            has_any_guard_count += 1

    total_fn = len(functions)
    isinstance_ratio = has_isinstance_count / total_fn
    any_guard_ratio = has_any_guard_count / total_fn

    # 1. Whole-file Sibling convention
    if isinstance_ratio >= 0.70:
        for fn in functions:
            guards = func_guards[fn.name]
            if not any("isinstance" in g for g in guards):
                warnings.append({
                    "file": str(file_path),
                    "symbol": fn.name,
                    "convention": "isinstance input type guard",
                    "ratio": isinstance_ratio,
                    "count": has_isinstance_count,
                    "total": total_fn,
                    "msg": f"⚠️ Sibling Convention Warning: {has_isinstance_count}/{total_fn} sibling functions ({int(isinstance_ratio*100)}%) in `{file_path.name}` start with an isinstance type guard. Function `{fn.name}` deviates from this convention."
                })

    # 2. Prefix-based Sibling convention (e.g. validate_*, handle_*, get_*)
    prefix_groups: Dict[str, List[ast.FunctionDef]] = defaultdict(list)
    for fn in functions:
        if "_" in fn.name:
            prefix = fn.name.split("_")[0]
            if len(prefix) >= 3 and not prefix.startswith("__"):
                prefix_groups[prefix].append(fn)

    for prefix, group in prefix_groups.items():
        if len(group) >= 3:
            g_guarded = [fn for fn in group if len(func_guards[fn.name]) > 0]
            g_ratio = len(g_guarded) / len(group)
            if g_ratio >= 0.70:
                for fn in group:
                    if len(func_guards[fn.name]) == 0:
                        warnings.append({
                            "file": str(file_path),
                            "symbol": fn.name,
                            "convention": f"guard clause for `{prefix}_*` functions",
                            "ratio": g_ratio,
                            "count": len(g_guarded),
                            "total": len(group),
                            "msg": f"⚠️ Sibling Convention Warning: {len(g_guarded)}/{len(group)} `{prefix}_*` sibling functions ({int(g_ratio*100)}%) in `{file_path.name}` have entry guard clauses. Function `{fn.name}` deviates from this convention."
                        })

    return warnings


def check_guard_stability(file_path: Path, patch_diff: str) -> List[Dict]:
    """
    Detect if a patch diff removes an entry guard or invariant check from modified functions.
    """
    invariant_warnings = []
    if not patch_diff or "@@" not in patch_diff:
        return invariant_warnings

    # Look for removed lines starting with '-' that contain guards
    removed_guards = []
    for line in patch_diff.splitlines():
        if line.startswith("-") and not line.startswith("---"):
            content = line[1:].strip()
            if any(kw in content for kw in ["isinstance", "assert ", "if self._", "is None", "raise TypeError", "if err != nil", "if (!", "if (typeof"]):
                removed_guards.append(content)

    if removed_guards:
        for guard in removed_guards[:3]: # Cap at 3 top guards
            invariant_warnings.append({
                "file": str(file_path),
                "guard": guard,
                "msg": f"🚨 Critical Invariant Alert: Historical guard clause/assertion `{guard}` was removed or altered in this patch! (Potential Code Invariant Regression)"
            })

    return invariant_warnings


def _mine_go_sibling_conventions(file_path: Path) -> List[Dict]:
    """Mine intra-file sibling conventions for Go files."""
    warnings = []
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return warnings

    # Match func declarations: func FuncName(...) [return_type] { or func (r *Recv) FuncName(...) [return_type] {
    pattern = re.compile(r'func\s+(?:\([^)]+\)\s+)?([A-Za-z0-9_]+)\s*\([^)]*\)[^{]*{')
    matches = list(pattern.finditer(content))
    if len(matches) < 3:
        return warnings

    func_has_guard = {}
    guarded_count = 0

    for i, m in enumerate(matches):
        fn_name = m.group(1)
        start_pos = m.end()
        end_pos = matches[i + 1].start() if i + 1 < len(matches) else min(start_pos + 1000, len(content))
        body_snippet = content[start_pos:end_pos][:400]
        has_guard = any(kw in body_snippet for kw in ["if err != nil", "if == nil", "if != nil", "if len(", "errors.New", "fmt.Errorf"])
        func_has_guard[fn_name] = has_guard
        if has_guard:
            guarded_count += 1

    ratio = guarded_count / len(matches)
    if ratio >= 0.70:
        for fn_name, has_guard in func_has_guard.items():
            if not has_guard:
                warnings.append({
                    "file": str(file_path),
                    "symbol": fn_name,
                    "convention": "error/nil entry guard in Go function",
                    "ratio": ratio,
                    "count": guarded_count,
                    "total": len(matches),
                    "msg": f"⚠️ Sibling Convention Warning: {guarded_count}/{len(matches)} sibling functions ({int(ratio*100)}%) in `{file_path.name}` have entry error/nil guards. Go function `{fn_name}` deviates from this convention."
                })

    return warnings


def _mine_ts_sibling_conventions(file_path: Path) -> List[Dict]:
    """Mine intra-file sibling conventions for TypeScript/JavaScript files."""
    warnings = []
    try:
        content = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return warnings

    pattern = re.compile(r'(?:async\s+)?function\s+([A-Za-z0-9_]+)\s*\(|(?:const|let|var)\s+([A-Za-z0-9_]+)\s*=\s*(?:async\s+)?\([^)]*\)\s*=>')
    matches = list(pattern.finditer(content))
    if len(matches) < 3:
        return warnings

    func_has_guard = {}
    guarded_count = 0

    for i, m in enumerate(matches):
        fn_name = m.group(1) or m.group(2)
        if not fn_name:
            continue
        start_pos = m.end()
        end_pos = matches[i + 1].start() if i + 1 < len(matches) else min(start_pos + 1000, len(content))
        body_snippet = content[start_pos:end_pos][:400]
        has_guard = any(kw in body_snippet for kw in ["if (!", "if (typeof", "if (", "throw new Error", "throw new TypeError"])
        func_has_guard[fn_name] = has_guard
        if has_guard:
            guarded_count += 1

    if len(func_has_guard) < 3:
        return warnings

    ratio = guarded_count / len(func_has_guard)
    if ratio >= 0.70:
        for fn_name, has_guard in func_has_guard.items():
            if not has_guard:
                warnings.append({
                    "file": str(file_path),
                    "symbol": fn_name,
                    "convention": "input validation entry guard in TS/JS function",
                    "ratio": ratio,
                    "count": guarded_count,
                    "total": len(func_has_guard),
                    "msg": f"⚠️ Sibling Convention Warning: {guarded_count}/{len(func_has_guard)} sibling functions ({int(ratio*100)}%) in `{file_path.name}` have entry guard statements. TS/JS function `{fn_name}` deviates from this convention."
                })

    return warnings
