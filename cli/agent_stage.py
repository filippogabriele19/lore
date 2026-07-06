import difflib
import re
import sqlite3
from pathlib import Path
from cli.shared import STAGE_SUBDIR

class StageWriter:
    def __init__(self, project_root: Path):
        self.project_root = project_root
        self.stage_dir = project_root / STAGE_SUBDIR
        self.stage_dir.mkdir(parents=True, exist_ok=True)
        self.written: list[dict] = []  # {path, reason, staged_path}

    def write(self, relative_path: str, content: str, reason: str = "") -> str:
        rel = Path(relative_path.replace("\\", "/"))
        staged_path = self.stage_dir / rel
        staged_path.parent.mkdir(parents=True, exist_ok=True)
        staged_path.write_text(content, encoding="utf-8")
        self.written.append({
            "path": str(rel),
            "reason": reason,
            "staged": str(staged_path),
        })
        return f"Staged: {rel}  ({len(content.splitlines())} lines)"

    def generate_diff(self) -> str:
        diffs = []
        for entry in self.written:
            orig_path = self.project_root / entry["path"]
            staged_path = Path(entry["staged"])

            original = orig_path.read_text(encoding="utf-8", errors="replace") if orig_path.exists() else ""
            modified = staged_path.read_text(encoding="utf-8", errors="replace")

            if original == modified:
                diffs.append(f"# {entry['path']} — no changes")
                continue

            diff_lines = list(difflib.unified_diff(
                original.splitlines(keepends=True),
                modified.splitlines(keepends=True),
                fromfile=f"a/{entry['path']}",
                tofile=f"b/{entry['path']}",
            ))
            diffs.append("".join(diff_lines))

        return "\n".join(diffs)

def _get_co_changes(db_path: Path, symbol_names: list[str]) -> list[dict]:
    if not db_path.exists() or not symbol_names:
        return []
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "co_changes" not in tables or "symbols" not in tables or "files" not in tables:
            return []

        placeholders = ",".join("?" * len(symbol_names))
        file_rows = conn.execute(
            f"""SELECT DISTINCT f.path
                FROM symbols s JOIN files f ON s.file_id = f.id
                WHERE s.name IN ({placeholders})""",
            symbol_names,
        ).fetchall()
        file_paths = [r[0] for r in file_rows]
        if not file_paths:
            return []

        fp_ph = ",".join("?" * len(file_paths))
        co_rows = conn.execute(
            f"""SELECT file_a, file_b, count, last_seen
                FROM co_changes
                WHERE file_a IN ({fp_ph}) OR file_b IN ({fp_ph})
                ORDER BY count DESC LIMIT 15""",
            file_paths * 2,
        ).fetchall()
        return [dict(r) for r in co_rows]
    except Exception:
        return []
    finally:
        conn.close()

def _extract_target_files(task: str) -> list[str]:
    pattern = re.compile(
        r"[\w./\\-]+\.(?:py|ts|js|tsx|jsx|java|go|rb|rs|cpp|c|h|cs|php|yaml|yml|json|toml|md)",
        re.IGNORECASE,
    )
    seen: set[str] = set()
    results: list[str] = []
    for m in pattern.finditer(task):
        path = m.group(0).replace("\\", "/").lstrip("./")
        if path and path not in seen:
            seen.add(path)
            results.append(path)
            
    # Capture python module styles like django.db.models.deletion
    module_pattern = re.compile(
        r"\b[a-zA-Z_][\w_]*(?:\.[a-zA-Z_][\w_]*){2,}\b"
    )
    for m in module_pattern.finditer(task):
        mod = m.group(0)
        # Skip names ending with a common extension to avoid duplicates
        if mod.split(".")[-1].lower() in ("py", "ts", "js", "java", "go", "rb", "rs", "cpp", "c", "h", "cs", "php"):
            continue
        path = mod.replace(".", "/") + ".py"
        if path not in seen:
            seen.add(path)
            results.append(path)
            
    return results
