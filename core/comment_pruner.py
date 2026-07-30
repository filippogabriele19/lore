"""
core/comment_pruner.py
───────────────────────
LORE 2.0 Syntax & Comment Pruner.
Strips non-essential inline comments, debug code, and blank line clutter before
tokenizer encoding, while preserving docstrings and ADR/compliance annotations.
"""

import re


def prune_code_context(code: str, language: str = "python", preserve_adrs: bool = True) -> str:
    """Prunes inline comments and redundant whitespace from source code.

    Preserves:
    - Docstrings (Triple quoted strings in Python)
    - ADR architectural tags (e.g. # [ADR-001], # [L4:], // [ADR-...)
    - Compliance / Taint markers (e.g. # [L5:], # noqa)
    """
    if not code:
        return ""

    lines = code.splitlines()
    pruned_lines = []

    for line in lines:
        stripped = line.strip()

        # Preserve empty lines, but avoid multiple consecutive blank lines
        if not stripped:
            if pruned_lines and pruned_lines[-1] != "":
                pruned_lines.append("")
            continue

        # Check for inline comments
        if language.lower() in ("python", "py"):
            if stripped.startswith("#"):
                # Preserve if it contains ADR or L4/L5 tag or noqa
                if preserve_adrs and any(tag in line for tag in ("ADR-", "[L4:", "[L5:", "noqa", "type: ignore")):
                    pruned_lines.append(line)
                continue  # Otherwise strip standalone comment line
            elif "  #" in line:
                # Strip trailing comment unless it contains ADR/L4/L5
                code_part, comment_part = line.split("  #", 1)
                if preserve_adrs and any(tag in comment_part for tag in ("ADR-", "[L4:", "[L5:", "noqa")):
                    pruned_lines.append(line)
                else:
                    pruned_lines.append(code_part.rstrip())
                continue
        elif language.lower() in ("typescript", "javascript", "ts", "js", "c", "go", "golang", "cpp"):
            if stripped.startswith("//"):
                if preserve_adrs and any(tag in line for tag in ("ADR-", "[L4:", "[L5:", "nocheck")):
                    pruned_lines.append(line)
                continue
            elif "  //" in line:
                code_part, comment_part = line.split("  //", 1)
                if preserve_adrs and any(tag in comment_part for tag in ("ADR-", "[L4:", "[L5:")):
                    pruned_lines.append(line)
                else:
                    pruned_lines.append(code_part.rstrip())
                continue

        pruned_lines.append(line)

    return "\n".join(pruned_lines).strip()
