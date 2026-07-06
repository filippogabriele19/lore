from __future__ import annotations
import sys
import argparse
import stat
from pathlib import Path
from cli.shared import console

def _main_git_hook(argv: list[str] | None = None) -> None:
    """git-hook subcommand entrypoint."""
    parser = argparse.ArgumentParser(
        prog="lore git-hook",
        description="Install or uninstall LORE pre-commit git hooks",
    )
    parser.add_argument("action", choices=["install", "uninstall"],
                        help="Action to perform: install or uninstall the hook")
    parser.add_argument("--project", default=".",
                        help="Path to project root (default: '.')")
    args = parser.parse_args(argv)

    project_root = Path(args.project).resolve()
    git_dir = project_root / ".git"
    if not git_dir.exists():
        console.print(f"[error]✖ .git directory not found under: {project_root}[/]")
        sys.exit(1)

    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_file = hooks_dir / "pre-commit"

    if args.action == "install":
        _install_hook(project_root, hook_file)
    else:
        _uninstall_hook(hook_file)

def _install_hook(project_root: Path, hook_file: Path) -> None:
    # Build standard pre-commit shell script content
    content = f"""#!/bin/sh
# LORE compliance pre-commit hook

# Always run from project root
cd "{project_root}"

# Detect Python inside virtual environment
if [ -f "venv/Scripts/python" ]; then
    PYTHON_BIN="venv/Scripts/python"
elif [ -f "venv/bin/python" ]; then
    PYTHON_BIN="venv/bin/python"
else
    PYTHON_BIN="python"
fi

echo "🔮 LORE: auditing staged changes before commit..."

$PYTHON_BIN lore.py check-vuln --patch-staged --fail-on-regression
RESULT=$?

if [ $RESULT -ne 0 ]; then
    echo "🚨 LORE: Commit blocked due to compliance violations or unresolved vulnerabilities."
    exit 1
fi

exit 0
"""
    try:
        hook_file.write_text(content, encoding="utf-8")
        # Make executable
        if sys.platform != "win32":
            st = hook_file.stat()
            hook_file.chmod(st.st_mode | stat.S_IEXEC)
        console.print(f"[success]✔ LORE pre-commit hook installed successfully at: {hook_file}[/]")
    except Exception as e:
        console.print(f"[error]✖ Failed to install pre-commit hook: {e}[/]")
        sys.exit(1)

def _uninstall_hook(hook_file: Path) -> None:
    if not hook_file.exists():
        console.print("[info]Pre-commit hook was not installed.[/]")
        return
    try:
        hook_file.unlink()
        console.print("[success]✔ LORE pre-commit hook uninstalled successfully.[/]")
    except Exception as e:
        console.print(f"[error]✖ Failed to remove pre-commit hook: {e}[/]")
        sys.exit(1)
