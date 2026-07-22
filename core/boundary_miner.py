"""
core/boundary_miner.py — Boundary Condition & Value Range Miner

Detects comparison operator weakening and boundary condition shifts in diff hunks
across Python, Go, and TypeScript/JavaScript source files.
"""

import ast
import re
from pathlib import Path

_COMP_OPERATORS_PY = {
    ast.Gt: ">",
    ast.GtE: ">=",
    ast.Lt: "<",
    ast.LtE: "<=",
    ast.Eq: "==",
    ast.NotEq: "!=",
    ast.In: "in",
    ast.NotIn: "not in",
    ast.Is: "is",
    ast.IsNot: "is not"
}

_WEAKENING_PAIRS = {
    (">", ">="): "Strict inequality '>' weakened to '>=' (boundary expanded)",
    ("<", "<="): "Strict inequality '<' weakened to '<=' (boundary expanded)",
    ("==", "!="): "Equality assertion flipped to inequality '!='",
    ("not in", "in"): "Inclusion check inverted ('not in' -> 'in')",
}

def check_boundary_mutations(file_path: Path, diff_text: str) -> list[dict]:
    """
    Analyzes unified diff text for a file and detects comparison operator mutations
    (e.g., '>' changed to '>=', '<' changed to '<=').
    """
    if not diff_text or not diff_text.strip():
        return []

    alerts = []
    rel_file = str(file_path).replace("\\", "/")
    
    # Process diff hunks looking for modified comparison lines
    removed_comps = []
    added_comps = []
    
    lines = diff_text.splitlines()
    for idx, line in enumerate(lines):
        if line.startswith("-") and not line.startswith("---"):
            content = line[1:].strip()
            # Extract comparison patterns
            matches = re.findall(r'(\b[\w\.]+\b)\s*(>=|<=|==|!=|>|<)\s*(\b[\w\.]+\b)', content)
            for m in matches:
                removed_comps.append((m[0], m[1], m[2], content))
        elif line.startswith("+") and not line.startswith("+++"):
            content = line[1:].strip()
            matches = re.findall(r'(\b[\w\.]+\b)\s*(>=|<=|==|!=|>|<)\s*(\b[\w\.]+\b)', content)
            for m in matches:
                added_comps.append((m[0], m[1], m[2], content))

    # Match removed vs added comparisons with same operands but altered operator
    for r_left, r_op, r_right, r_line in removed_comps:
        for a_left, a_op, a_right, a_line in added_comps:
            if (r_left == a_left or r_right == a_right) and r_op != a_op:
                pair = (r_op, a_op)
                reason = _WEAKENING_PAIRS.get(pair, f"Boundary comparison operator mutated from '{r_op}' to '{a_op}'")
                alerts.append({
                    "file": rel_file,
                    "old_op": r_op,
                    "new_op": a_op,
                    "left": r_left,
                    "right": r_right,
                    "msg": f"⚖️ **Boundary Shift**: In `{rel_file}`: `{r_left} {r_op} {r_right}` mutated to `{a_left} {a_op} {a_right}`. {reason}."
                })

    return alerts
