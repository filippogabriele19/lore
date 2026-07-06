import os
import sys
import shutil
import tempfile
import subprocess
import time
import stat
from pathlib import Path
from cli.gh_check import _apply_unified_diff

def _robust_cleanup(temp_dir: str) -> None:
    path = Path(temp_dir)
    if not path.exists():
        return

    # Helper function to remove read-only attributes
    def onerror(func, p, exc_info):
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except Exception:
            pass

    for attempt in range(5):
        try:
            shutil.rmtree(temp_dir, onerror=onerror)
            if not path.exists():
                return
        except Exception:
            pass
        time.sleep(0.2)

def run_agentic_qa_test(
    project_root: Path,
    patched_files: dict[str, str],  # rel_path -> diff_content (can be empty to test baseline)
    test_code: str,
    timeout_seconds: float = 10.0
) -> dict:
    """
    Prepares a temporary sandbox directory, copies the project files (excluding venv, git, secrets),
    applies the proposed patches, writes the behavioral test script, and executes it.
    
    Returns:
        dict: {
            "success": bool,
            "exit_code": int,
            "stdout": str,
            "stderr": str,
            "duration": float,
            "error_msg": str (optional)
        }
    """
    start_time = time.time()
    temp_dir = tempfile.mkdtemp(prefix="lore_qa_sandbox_")
    sandbox_path = Path(temp_dir)
    
    try:
        # 1. Copy project structure, excluding heavy/unneeded folders and secrets
        ignore_dirs = {".git", ".venv", "venv", ".lore", "__pycache__", ".pytest_cache", ".idea", ".vscode"}
        
        for item in os.listdir(project_root):
            s = project_root / item
            d = sandbox_path / item
            if s.is_dir():
                if item in ignore_dirs:
                    continue
                shutil.copytree(s, d, ignore=shutil.ignore_patterns("*.pyc", "__pycache__", ".lore_poc.db", ".env", "*.env", ".env.*"))
            else:
                if item == ".lore_poc.db" or item.endswith(".pyc") or item == ".env" or ".env." in item or item.endswith(".env"):
                    continue
                shutil.copy2(s, d)
                
        # 2. Apply patches in-place inside the sandbox
        for rel_path, diff_content in patched_files.items():
            sandbox_file = sandbox_path / rel_path
            if sandbox_file.exists():
                try:
                    original_text = sandbox_file.read_text(encoding="utf-8", errors="replace")
                    patched_text = _apply_unified_diff(original_text, diff_content)
                    sandbox_file.write_text(patched_text, encoding="utf-8")
                except Exception as e:
                    return {
                        "success": False,
                        "exit_code": -1,
                        "stdout": "",
                        "stderr": "",
                        "duration": time.time() - start_time,
                        "error_msg": f"Failed to apply patch to {rel_path} in sandbox: {e}"
                    }
            else:
                pass
                
        # 3. Write the behavioral test script
        test_file = sandbox_path / "test_lore_qa_behavior.py"
        test_file.write_text(test_code, encoding="utf-8")
        
        # 4. Run the test script
        # Check if docker is available for sandboxed containerized execution
        use_docker = False
        try:
            res_dock = subprocess.run(["docker", "info"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=2)
            if res_dock.returncode == 0:
                use_docker = True
        except Exception:
            pass

        if use_docker:
            abs_sandbox = sandbox_path.resolve()
            cmd = [
                "docker", "run", "--rm",
                "--network", "none",
                "-v", f"{abs_sandbox}:/app",
                "-w", "/app",
                "python:3.10-slim",
                "python", "test_lore_qa_behavior.py"
            ]
        else:
            cmd = [sys.executable, str(test_file)]
        
        proc_start = time.time()
        try:
            res = subprocess.run(
                cmd,
                cwd=str(sandbox_path),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout_seconds,
                encoding="utf-8",
                errors="replace"
            )
            proc_duration = time.time() - proc_start
            
            return {
                "success": res.returncode == 0,
                "exit_code": res.returncode,
                "stdout": res.stdout,
                "stderr": res.stderr,
                "duration": proc_duration
            }
        except subprocess.TimeoutExpired as e:
            proc_duration = time.time() - proc_start
            return {
                "success": False,
                "exit_code": -9,
                "stdout": e.stdout if e.stdout else "",
                "stderr": e.stderr if e.stderr else "",
                "duration": proc_duration,
                "error_msg": f"Test timed out after {timeout_seconds}s (Potential infinite loop, ReDoS, or performance regression)"
            }
            
    except Exception as e:
        return {
            "success": False,
            "exit_code": -2,
            "stdout": "",
            "stderr": "",
            "duration": time.time() - start_time,
            "error_msg": f"Sandbox setup or execution failed: {e}"
        }
    finally:
        _robust_cleanup(temp_dir)
