"""
core/_macro_change.py — MacroChange grouping and prompt rendering.

A MacroChange is a cluster of commits by the same author within a 48h window.
Atomic commits (typo fix, rebase, merge) are absorbed into their surrounding
MacroChange so the LLM reconciler sees intent, not noise.

Imported by core._intent_miner.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional


def group_macro_changes(
    git_fn: Callable,
    file_path: str,
    after_date: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Group commits for file_path into MacroChange nodes.

    Rule: commits from the same author within 48h of the newest commit in the
    current group are merged. Returns list of macro-change dicts, newest first:
      {"hashes": [str], "author": str, "date_range": str,
       "subjects": [str], "files_touched": [str], "commit_count": int}

    Args:
        after_date: ISO date string ("YYYY-MM-DD") passed to git --after.
                    None (default) means all history — used by the Speed Layer.
                    Set by the Batch Layer to scope to the analysis window.
    """
    args = ["log", "--follow", "--format=%H|%an|%ai|%s", "--no-merges"]
    if after_date:
        args.append(f"--after={after_date}")
    args += ["--", file_path]
    raw = git_fn(*args)
    if not raw:
        return []

    commits: List[Dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.count("|") < 3:
            continue
        h, author, date_str, subject = line.split("|", 3)
        try:
            dt = datetime.strptime(date_str[:19].strip(), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        commits.append({
            "hash":    h,
            "author":  author.strip(),
            "dt":      dt,
            "subject": subject.strip(),
        })

    if not commits:
        return []

    WINDOW = timedelta(hours=48)
    groups: List[List[Dict]] = []
    current: List[Dict] = [commits[0]]

    for commit in commits[1:]:
        newest_dt   = current[0]["dt"]
        same_author = commit["author"] == current[0]["author"]
        in_window   = (newest_dt - commit["dt"]) <= WINDOW
        if same_author and in_window:
            current.append(commit)
        else:
            groups.append(current)
            current = [commit]
    groups.append(current)

    macro_changes: List[Dict[str, Any]] = []
    for group in groups:
        hashes   = [c["hash"][:7] for c in group]
        subjects = [c["subject"] for c in group]
        d_newest = group[0]["dt"].strftime("%Y-%m-%d")
        d_oldest = group[-1]["dt"].strftime("%Y-%m-%d")
        date_range = d_newest if d_newest == d_oldest else f"{d_oldest} \u2192 {d_newest}"

        # Collect files touched in a single git invocation instead of diff-tree loop (Bug 14)
        if hashes:
            fr = git_fn("show", "--no-commit-id", "--name-only", "--format=", *hashes[:5])
            if fr:
                for f in fr.splitlines():
                    f = f.strip()
                    if f:
                        files_touched.add(f)

        macro_changes.append({
            "hashes":        hashes,
            "author":        group[0]["author"],
            "date_range":    date_range,
            "subjects":      subjects,
            "files_touched": sorted(files_touched),
            "commit_count":  len(group),
        })

    return macro_changes


def build_history_markdown(
    git_fn: Callable,
    file_path: str,
    total_commits: int,
    macro_changes: List[Dict[str, Any]],
    downstream: List[Dict[str, str]],
) -> str:
    """
    Build the LLM prompt payload for the Intent Reconciler.

    Section 1 — Structural subgraph: downstream symbols the file calls.
    Section 2 — MacroChange history: commit groups (not raw individual commits).
    """
    if not macro_changes:
        return ""

    lines: List[str] = [
        "# LORE Intent Miner — Structural + Historical Extract",
        "",
        "## Target File",
        f"- **Path**: `{file_path}`",
        f"- **Total commits**: {total_commits}",
        f"- **MacroChange groups**: {len(macro_changes)}",
        "",
    ]

    # Section 1: structural subgraph
    if downstream:
        lines += ["## Structural Subgraph — Downstream Dependencies", ""]
        by_file: Dict[str, List[str]] = {}
        for d in downstream:
            by_file.setdefault(d["callee_file"], []).append(d["callee_name"])
        for callee_file, names in sorted(by_file.items()):
            lines.append(f"- `{callee_file}`: {', '.join(names[:8])}")
        lines += ["", "---", ""]

    # Section 2: macro-change history
    lines += [
        "## MacroChange History (newest first)",
        "",
        "_Each MacroChange = commits by the same author within 48h._",
        "",
    ]

    for i, mc in enumerate(macro_changes[:10], 1):
        n     = mc["commit_count"]
        label = (
            f"MacroChange {i} \u2014 {mc['author']} \u2014 {mc['date_range']}"
            f" ({n} commit{'s' if n > 1 else ''})"
        )
        lines += [f"### {label}", ""]

        for subj in mc["subjects"][:5]:
            lines.append(f"- {subj}")
        if len(mc["subjects"]) > 5:
            lines.append(f"- _(+{len(mc['subjects']) - 5} more)_")
        lines.append("")

        if mc["files_touched"]:
            ft = ", ".join(f"`{f}`" for f in mc["files_touched"][:10])
            lines.append(f"**Files touched**: {ft}")

        if mc["hashes"]:
            body = git_fn("show", "--no-patch", "--format=%b", mc["hashes"][0]).strip()
            if body:
                lines.append(f"**Lead commit body**: {body[:300]}")
        lines.append("")

    return "\n".join(lines)
