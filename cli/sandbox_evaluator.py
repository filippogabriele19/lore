import subprocess
import tempfile
import os
from pathlib import Path
import traceback

class SandboxEvaluator:
    """
    Dyna-Trace Engine core module.
    Evaluates modified code by running it in a subprocess and returning kernel tracebacks.
    Can be extended to run 'pytest' or 'hypothesis' fuzzing.
    """
    
    def __init__(self, project_root: Path):
        self.project_root = project_root

    def evaluate_syntax_and_trace(self, file_path: str, new_content: str) -> str | None:
        """
        Writes the new content to a temporary file, executes 'python -m py_compile', 
        and captures stdout/stderr.
        Returns None if valid, or the error stack trace if failed.
        """
        if not file_path.endswith('.py'):
            return None
            
        # Create a temp file mirroring the target path base name
        fd, temp_path = tempfile.mkstemp(suffix=".py", prefix="dyna_trace_")
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(new_content)
                
            # Run the compile step as a separate process to catch real kernel/VM errors
            result = subprocess.run(
                ["python", "-m", "py_compile", temp_path],
                capture_output=True,
                text=True,
                cwd=str(self.project_root)
            )
            
            if result.returncode != 0:
                # Format the error nicely, removing the temp path to not confuse the LLM
                err_msg = result.stderr or result.stdout
                err_msg = err_msg.replace(temp_path, file_path)
                return f"Execution Failed (Syntax/Compilation Error):\n{err_msg}"
                
            return None
            
        except Exception as e:
            return f"Internal Sandbox Error: {str(e)}\n{traceback.format_exc()}"
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    def _run_in_swebench(self, instance_id: str, python_script: str = None) -> str:
        import subprocess
        import json
        import uuid
        from datasets import load_dataset
        import tempfile
        
        try:
            # 1. Caricamento metadati
            ds = load_dataset("princeton-nlp/SWE-bench_Lite", split="test", trust_remote_code=True)
            instance = None
            for item in ds:
                if item["instance_id"] == instance_id:
                    instance = item
                    break
            if not instance:
                return f"[SWEBENCH_ERROR] Instance {instance_id} not found in dataset."

            from swebench.harness.test_spec import make_test_spec
            test_spec = make_test_spec(instance)

            # Converti output in diff unificato (git diff su project_root che ha i file staged temporaneamente)
            diff_res = subprocess.run(
                ["git", "diff"],
                cwd=str(self.project_root),
                capture_output=True,
                text=True,
                encoding="utf-8"
            )
            patch_content = diff_res.stdout
            
            with tempfile.NamedTemporaryFile("w", suffix=".diff", delete=False) as f:
                f.write(patch_content)
                patch_file = f.name
                
            container_name = f"swebench_{instance_id}_{uuid.uuid4().hex[:6]}"
            image_name = test_spec.instance_image_key
            
            # Start ephemeral container
            subprocess.run(["docker", "run", "-d", "--name", container_name, "-it", image_name, "/bin/bash"], capture_output=True)
            
            try:
                # git config safe.directory per evitare blocchi
                subprocess.run(["docker", "exec", container_name, "git", "config", "--global", "--add", "safe.directory", "/testbed"], capture_output=True)
                
                # Apply patch
                subprocess.run(["docker", "cp", patch_file, f"{container_name}:/tmp/patch.diff"], capture_output=True)
                apply_cmd = ["docker", "exec", container_name, "bash", "-c", "cd /testbed && git apply /tmp/patch.diff"]
                apply_res = subprocess.run(apply_cmd, capture_output=True, text=True, encoding="utf-8")
                
                if apply_res.returncode != 0:
                    return f"[PATCH_APPLY_FAILED]\n{apply_res.stderr}\nLa conversione della patch ha fallito il git apply nel container. Fixa i tuoi path o il contesto e riprova."
                
                # Esecuzione script
                eval_script_path = "/tmp/eval_script.sh"
                with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f_script:
                    f_script.write(test_spec.eval_script)
                    local_eval_script = f_script.name
                    
                subprocess.run(["docker", "cp", local_eval_script, f"{container_name}:{eval_script_path}"], capture_output=True)
                subprocess.run(["docker", "exec", container_name, "chmod", "+x", eval_script_path], capture_output=True)
                
                # TIMEOUT esplicito e attivazione conda
                test_cmd = ["docker", "exec", container_name, "bash", "-c", f"source /opt/miniconda3/bin/activate testbed && {eval_script_path}"]
                test_res = subprocess.run(test_cmd, capture_output=True, text=True, encoding="utf-8", timeout=180)
                
                # Verifica dove l'output è stato scritto
                cat_cmd = ["docker", "exec", container_name, "bash", "-c", "cat /testbed/test_output.txt 2>/dev/null || echo ''"]
                cat_res = subprocess.run(cat_cmd, capture_output=True, text=True, encoding="utf-8")
                
                output = test_res.stdout
                if cat_res.stdout.strip():
                    output += "\n" + cat_res.stdout
                output += "\n" + test_res.stderr
                
                from swebench.harness.grading import get_eval_report
                report = get_eval_report(test_spec, output)
                
                result_str = f"=== SWE-bench Eval Report (Instance: {instance_id}) ===\n"
                for test_status, test_list in report.items():
                    if test_list:
                        result_str += f"\n{test_status}:\n"
                        for t in test_list:
                            result_str += f" - {t}\n"
                
                # Include some raw output for context if everything fails
                if not any(report.values()):
                    result_str += f"\n[RAW OUTPUT (No structured tests parsed)]\n{output[:1500]}\n..."
                        
                return result_str
                
            finally:
                subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
                os.remove(patch_file)
                if 'local_eval_script' in locals():
                    os.remove(local_eval_script)
                    
        except subprocess.TimeoutExpired:
            subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
            return "[TIMEOUT] Test execution exceeded 180 seconds. The patch likely caused an infinite loop (e.g. Django recursion)."
        except Exception as e:
            import traceback
            return f"SWE-bench Sandbox Error: {str(e)}\n{traceback.format_exc()}"

    def run_in_docker(self, command: str, python_script: str = None) -> str:
        """
        Runs a command or a provided python script inside a docker container
        with the project_root mounted. Alternatively, runs locally if LORE_LOCAL_SANDBOX is set.
        """
        import uuid
        
        instance_id = os.environ.get("SWEBENCH_INSTANCE_ID")
        if instance_id:
            return self._run_in_swebench(instance_id, python_script)
            
        reproduce_path = None
        try:
            # If there's a custom python script, write it to project_root
            if python_script:
                reproduce_path = self.project_root / "lore_reproduce.py"
                reproduce_path.write_text(python_script, encoding="utf-8")

            if os.environ.get("LORE_LOCAL_SANDBOX") == "1":
                # Local execution
                result = subprocess.run(
                    command,
                    shell=True,
                    cwd=str(self.project_root),
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=300
                )
                stdout = result.stdout or ""
                stderr = result.stderr or ""
                output = stdout + "\n" + stderr
                return f"Exit Code: {result.returncode}\nOutput:\n{output.strip()}"
            
            container_name = f"lore_fuzzer_{uuid.uuid4().hex[:8]}"
            # Using python:3.9-slim as a reasonable base image.
            docker_cmd = [
                "docker", "run", "--rm",
                "--name", container_name,
                "-v", f"{self.project_root.absolute()}:/app",
                "-w", "/app",
                "python:3.9-slim",
                "bash", "-c", f"pip install pytest && {command}"
            ]
            
            result = subprocess.run(
                docker_cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=300
            )
            
            stdout = result.stdout or ""
            stderr = result.stderr or ""
            output = stdout + "\n" + stderr
            return f"Exit Code: {result.returncode}\nOutput:\n{output.strip()}"
            
        except subprocess.TimeoutExpired:
            if os.environ.get("LORE_LOCAL_SANDBOX") != "1":
                subprocess.run(["docker", "kill", container_name], capture_output=True)
            return "Execution Failed: Timeout after 300 seconds."
        except Exception as e:
            return f"Sandbox Error: {str(e)}\n{traceback.format_exc()}"
        finally:
            if reproduce_path and reproduce_path.exists():
                try:
                    reproduce_path.unlink()
                except:
                    pass

