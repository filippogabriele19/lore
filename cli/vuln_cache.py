import json
import hashlib
from pathlib import Path

CACHE_FILE = ".lore/vuln_cache.json"

def _hash_file(path: Path) -> str:
    try:
        return hashlib.md5(path.read_bytes()).hexdigest()
    except OSError:
        return ""

def load_cache(project_root: Path) -> dict | None:
    cache_path = project_root / CACHE_FILE
    if not cache_path.exists():
        return None
    try:
        return json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return None

def save_cache(project_root: Path, analysis_result: dict, file_hashes: dict[str, str]):
    cache_path = project_root / CACHE_FILE
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_data = {
        "file_hashes": file_hashes,
        "exposed_paths": analysis_result.get("exposed_paths", []),
        "taint_files": list({f for path in analysis_result.get("exposed_paths", []) for f in path})
    }
    cache_path.write_text(json.dumps(cache_data, indent=2), encoding="utf-8")

def needs_reanalysis(project_root: Path, staged_files: list[str]) -> bool:
    cache = load_cache(project_root)
    if cache is None:
        return True
    taint_files = set(cache.get("taint_files", []))
    staged_set = {f.replace("\\", "/") for f in staged_files}
    return bool(staged_set & taint_files)
