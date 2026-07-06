
#!/usr/bin/env python3
"""
LORE Intent Miner - Phase A: Git History Extractor

Extracts the complete commit history for a target file and formats it
as a structured Markdown string ready for the Reconciler Engine prompt.

Usage:
    python -m core.git_historian <repo_path> <file_path> [--max-commits N] [--output file.md]

Examples:
    python -m core.git_historian /path/to/repo src/api/auth.py
    python -m core.git_historian /path/to/repo src/api/auth.py --max-commits 50
    python -m core.git_historian /path/to/repo src/api/auth.py --output history.md
"""

import subprocess
import argparse
import sys
import json
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class CommitRecord:
    hash: str
    short_hash: str
    author: str
    date: str
    message_subject: str
    message_body: str
    diff: str
    files_changed: list[str] = field(default_factory=list)

    @property
    def message_quality_hint(self) -> str:
        """
        Heuristic signal for the Reconciler: helps it weight how much
        to trust the commit message as intent documentation.
        """
        subject = self.message_subject.lower()
        body = self.message_body.lower()

        # Conventional commit prefixes that signal semantic intent
        semantic_prefixes = ("feat", "fix(security", "refactor", "breaking", "revert")
        noise_patterns = (
            "wip", "update", "fix typo", "cleanup", "misc",
            "minor", "temp", "tmp", "test", "merge"
        )
        security_keywords = (
            "auth", "jwt", "token", "security", "permission",
            "encrypt", "bypass", "credentials", "vulnerability"
        )

        has_body = len(body.strip()) > 20
        is_semantic = any(subject.startswith(p) for p in semantic_prefixes)
        is_noise = any(p in subject for p in noise_patterns)
        touches_security = any(k in subject + body for k in security_keywords)

        if is_noise and not has_body:
            return "LOW — likely noise commit, treat message with skepticism"
        if touches_security and not has_body:
            return "SUSPICIOUS — security-adjacent change with minimal message"
        if is_semantic and has_body:
            return "HIGH — conventional commit with body"
        if is_semantic:
            return "MEDIUM — conventional prefix but no body"
        return "MEDIUM — no strong signal"


# ---------------------------------------------------------------------------
# Git operations
# ---------------------------------------------------------------------------

def run_git(args: list[str], cwd: str) -> str:
    """Run a git command and return stdout. Raises on failure."""
    result = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace"
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed:\n{result.stderr.strip()}"
        )
    return result.stdout


def get_commit_hashes(repo_path: str, file_path: str, max_commits: Optional[int]) -> list[str]:
    """Get all commit hashes that touched the target file, newest first."""
    args = ["log", "--follow", "--format=%H", "--", file_path]
    if max_commits:
        args = ["log", f"-{max_commits}", "--follow", "--format=%H", "--", file_path]
    output = run_git(args, repo_path)
    return [h.strip() for h in output.splitlines() if h.strip()]


def get_commit_metadata(repo_path: str, commit_hash: str) -> dict:
    """Extract structured metadata for a single commit."""
    # Format: hash|short_hash|author|date|subject
    fmt = "%H|%h|%an|%ad|%s"
    meta_line = run_git(
        ["show", "--no-patch", f"--format={fmt}", "--date=short", commit_hash],
        repo_path
    ).splitlines()[0]

    parts = meta_line.split("|", 4)
    if len(parts) < 5:
        return {}

    # Get the body separately (can be multiline)
    body = run_git(
        ["show", "--no-patch", "--format=%b", commit_hash],
        repo_path
    ).strip()

    return {
        "hash": parts[0],
        "short_hash": parts[1],
        "author": parts[2],
        "date": parts[3],
        "subject": parts[4],
        "body": body,
    }


def get_commit_diff(repo_path: str, commit_hash: str, file_path: str) -> tuple[str, list[str]]:
    """
    Get the diff for a specific file at a specific commit.
    Returns (diff_text, list_of_changed_files).

    Handles file renames via --follow-equivalent logic:
    we track the file's name at that point in history.
    """
    # Get list of files changed in this commit (for context)
    files_output = run_git(
        ["show", "--stat", "--format=", commit_hash],
        repo_path
    )
    files_changed = [
        line.split("|")[0].strip()
        for line in files_output.splitlines()
        if "|" in line
    ]

    # Get the diff for the target file specifically
    # git show with -- handles renames if the file existed under a different name
    diff = run_git(
        ["show", commit_hash, "--", file_path],
        repo_path
    )

    # If the file wasn't in this commit directly (e.g., it was renamed),
    # try to find it by looking at what the file was called before
    if not diff.strip() or "diff --git" not in diff:
        # Fallback: get whatever diff exists for this commit touching our file
        diff = run_git(
            ["show", commit_hash, "--follow", "--", file_path],
            repo_path
        )

    return diff, files_changed


def build_commit_records(
    repo_path: str,
    file_path: str,
    hashes: list[str]
) -> list[CommitRecord]:
    """Build CommitRecord objects for each hash, oldest-first for chronological reading."""
    if not hashes:
        return []

    # 1. Fetch metadata in one bulk run
    commit_meta = {}
    try:
        meta_raw = run_git(["log", "--date=short", "--format====START_META===%n%H%n%h%n%an%n%ad%n%s%n%b%n===END_META===", "--follow", "--", file_path], repo_path)
        parts = meta_raw.split("===START_META===\n")
        for part in parts:
            if not part.strip():
                continue
            subparts = part.split("===END_META===\n", 1)
            meta_section = subparts[0]
            meta_lines = meta_section.splitlines()
            if len(meta_lines) >= 5:
                h = meta_lines[0].strip()
                short_h = meta_lines[1].strip()
                author = meta_lines[2].strip()
                date = meta_lines[3].strip()
                subject = meta_lines[4].strip()
                body = "\n".join(meta_lines[5:]).strip()
                commit_meta[h] = {
                    "hash": h,
                    "short_hash": short_h,
                    "author": author,
                    "date": date,
                    "subject": subject,
                    "body": body
                }
    except Exception as e:
        print(f"  [warn] failed to fetch bulk metadata: {e}", file=sys.stderr)

    # 2. Fetch diffs in one bulk run
    commit_diffs = {}
    try:
        diff_raw = run_git(["show", "--format====START_DIFF===%n%H%n===END_META===", "--", file_path] + hashes, repo_path)
        diff_parts = diff_raw.split("===START_DIFF===\n")
        for part in diff_parts:
            if not part.strip():
                continue
            subparts = part.split("===END_META===\n", 1)
            h = subparts[0].strip()
            diff_text = subparts[1] if len(subparts) > 1 else ""
            commit_diffs[h] = diff_text
    except Exception as e:
        print(f"  [warn] failed to fetch bulk diffs: {e}", file=sys.stderr)

    # 3. Fetch changed files list in one bulk run
    commit_files = {}
    try:
        files_raw = run_git(["show", "--stat", "--format====START_FILES===%n%H%n===END_META==="] + hashes, repo_path)
        files_parts = files_raw.split("===START_FILES===\n")
        for part in files_parts:
            if not part.strip():
                continue
            subparts = part.split("===END_META===\n", 1)
            h = subparts[0].strip()
            files_section = subparts[1] if len(subparts) > 1 else ""
            files = []
            for line in files_section.splitlines():
                if "|" in line:
                    files.append(line.split("|")[0].strip())
            commit_files[h] = files
    except Exception as e:
        print(f"  [warn] failed to fetch bulk files: {e}", file=sys.stderr)

    records = []
    for h in hashes:
        meta = commit_meta.get(h)
        if not meta:
            # Fallback to single lookup if bulk failed for this hash
            try:
                meta = get_commit_metadata(repo_path, h)
            except Exception:
                continue
        if not meta:
            continue

        diff = commit_diffs.get(h, "")
        files = commit_files.get(h, [])
        if not diff:
            # Fallback for diff renames
            try:
                diff, files_fallback = get_commit_diff(repo_path, h, file_path)
                if not files:
                    files = files_fallback
            except Exception:
                pass

        record = CommitRecord(
            hash=meta["hash"],
            short_hash=meta["short_hash"],
            author=meta["author"],
            date=meta["date"],
            message_subject=meta["subject"],
            message_body=meta["body"],
            diff=diff,
            files_changed=files,
        )
        records.append(record)

    # Reverse to chronological order (oldest first)
    records.reverse()
    return records


# ---------------------------------------------------------------------------
# Markdown formatter
# ---------------------------------------------------------------------------

def format_as_markdown(
    records: list[CommitRecord],
    file_path: str,
    repo_path: str,
    total_commits_in_history: int,
) -> str:
    """
    Format the commit history as a structured Markdown document
    ready to be pasted into the Reconciler Engine prompt.
    """
    lines = []

    lines.append("# LORE Intent Miner — Git History Extract")
    lines.append("")
    lines.append("## Target File")
    lines.append(f"- **Path**: `{file_path}`")
    lines.append(f"- **Repository**: `{repo_path}`")
    lines.append(f"- **Total commits touching this file**: {total_commits_in_history}")
    lines.append(f"- **Commits included in this extract**: {len(records)}")
    if len(records) < total_commits_in_history:
        lines.append(
            f"- **⚠ Truncated**: showing {len(records)} of {total_commits_in_history}. "
            "The Reconciler should treat this as a partial view."
        )
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## Commit History (Chronological — oldest first)")
    lines.append("")
    lines.append(
        "> **Reconciler instructions**: Analyze this sequence as an *evolution* of a "
        "single piece of business logic. Identify the original intent, how it changed, "
        "whether it was weakened or strengthened, and produce a single unified Intent Node "
        "JSON representing its current state and full history. Do NOT produce one node per commit."
    )
    lines.append("")

    for i, commit in enumerate(records, 1):
        lines.append(f"### Commit {i} of {len(records)}")
        lines.append("")
        lines.append(f"| Field | Value |")
        lines.append(f"|-------|-------|")
        lines.append(f"| **Hash** | `{commit.hash}` (`{commit.short_hash}`) |")
        lines.append(f"| **Date** | {commit.date} |")
        lines.append(f"| **Author** | {commit.author} |")
        lines.append(f"| **Message** | {commit.message_subject} |")
        lines.append(f"| **Message quality** | {commit.message_quality_hint} |")
        lines.append("")

        if commit.message_body:
            lines.append("**Commit body:**")
            lines.append("")
            lines.append("```")
            lines.append(commit.message_body)
            lines.append("```")
            lines.append("")

        if commit.files_changed:
            other_files = [f for f in commit.files_changed if file_path not in f]
            if other_files:
                lines.append(
                    f"**Other files changed in same commit** "
                    f"({len(other_files)} files — may indicate co-change coupling):"
                )
                for f in other_files[:10]:  # cap at 10 to avoid bloat
                    lines.append(f"- `{f}`")
                if len(other_files) > 10:
                    lines.append(f"- *(and {len(other_files) - 10} more)*")
                lines.append("")

        if commit.diff and "diff --git" in commit.diff:
            lines.append("**Diff:**")
            lines.append("")
            lines.append("```diff")
            # Trim extremely large diffs to avoid token explosion
            diff_content = commit.diff
            diff_lines = diff_content.splitlines()
            if len(diff_lines) > 200:
                trimmed = diff_lines[:200]
                lines.extend(trimmed)
                lines.append(f"... [{len(diff_lines) - 200} lines truncated — diff too large]")
            else:
                lines.append(diff_content)
            lines.append("```")
        else:
            lines.append("*[No diff available for this file at this commit — possible rename or binary file]*")

        lines.append("")
        lines.append("---")
        lines.append("")

    lines.append("## End of History")
    lines.append("")
    lines.append(
        "> Now produce a single unified Intent Node JSON. "
        "Required fields: `intent_id`, `title`, `canonical_intent`, "
        "`intent_health` (with `integrity_score` and `status`), "
        "`evolution_log` (one entry per semantic event, NOT per commit), "
        "`active_exceptions` (if any), `guard_rules`, `current_binding`, `source`."
    )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Stats helper
# ---------------------------------------------------------------------------

def get_file_stats(repo_path: str, file_path: str) -> dict:
    """Basic stats about the file for the summary."""
    try:
        total_hashes = get_commit_hashes(repo_path, file_path, max_commits=None)
        first_commit_meta = get_commit_metadata(repo_path, total_hashes[-1]) if total_hashes else {}
        last_commit_meta = get_commit_metadata(repo_path, total_hashes[0]) if total_hashes else {}
        return {
            "total_commits": len(total_hashes),
            "first_commit_date": first_commit_meta.get("date", "unknown"),
            "last_commit_date": last_commit_meta.get("date", "unknown"),
            "first_author": first_commit_meta.get("author", "unknown"),
        }
    except Exception:
        return {"total_commits": 0}


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="LORE Phase A: Extract git history for a file as Reconciler-ready Markdown."
    )
    parser.add_argument("repo_path", help="Path to the git repository root")
    parser.add_argument("file_path", help="Path to the target file (relative to repo root)")
    parser.add_argument(
        "--max-commits", type=int, default=None,
        help="Maximum number of commits to extract (default: all). "
             "For files with 100+ commits, use 30-50 to avoid token overload."
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="Write output to this file instead of stdout"
    )
    parser.add_argument(
        "--stats", action="store_true",
        help="Print file stats (total commits, date range) before extracting"
    )
    args = parser.parse_args()

    repo_path = str(Path(args.repo_path).resolve())
    file_path = args.file_path

    # Validate
    if not Path(repo_path).exists():
        print(f"[error] repo path does not exist: {repo_path}", file=sys.stderr)
        sys.exit(1)

    git_dir = Path(repo_path) / ".git"
    if not git_dir.exists():
        print(f"[error] not a git repository: {repo_path}", file=sys.stderr)
        sys.exit(1)

    # Stats
    if args.stats:
        print(f"[info] fetching stats for {file_path}...", file=sys.stderr)
        stats = get_file_stats(repo_path, file_path)
        print(json.dumps(stats, indent=2), file=sys.stderr)
        print("", file=sys.stderr)

    # Fetch hashes
    print(f"[info] fetching commit history for {file_path}...", file=sys.stderr)
    all_hashes = get_commit_hashes(repo_path, file_path, max_commits=None)
    total = len(all_hashes)

    if total == 0:
        print(f"[error] no commits found for {file_path}", file=sys.stderr)
        sys.exit(1)

    print(f"[info] found {total} commits total", file=sys.stderr)

    # Apply limit
    hashes_to_process = all_hashes
    if args.max_commits:
        hashes_to_process = all_hashes[:args.max_commits]
        print(
            f"[info] processing {len(hashes_to_process)} commits "
            f"(newest {args.max_commits} of {total})",
            file=sys.stderr
        )

    # Build records
    print(f"[info] extracting diffs...", file=sys.stderr)
    records = build_commit_records(repo_path, file_path, hashes_to_process)
    print(f"[info] built {len(records)} commit records", file=sys.stderr)

    # Format
    markdown = format_as_markdown(records, file_path, repo_path, total)

    # Output
    if args.output:
        Path(args.output).write_text(markdown, encoding="utf-8")
        print(f"[info] written to {args.output}", file=sys.stderr)
        # Also write a summary of what was extracted
        print(f"\n[summary]", file=sys.stderr)
        print(f"  file:    {file_path}", file=sys.stderr)
        print(f"  commits: {len(records)} extracted / {total} total", file=sys.stderr)
        print(f"  output:  {args.output}", file=sys.stderr)
        print(f"\n[next step] Feed {args.output} to the Reconciler Engine:", file=sys.stderr)
        print(f"  python -m core.reconciler {args.output}", file=sys.stderr)
    else:
        print(markdown)


if __name__ == "__main__":
    main()