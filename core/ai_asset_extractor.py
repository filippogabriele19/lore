"""
core/ai_asset_extractor.py
─────────────────────────────────────────────────────────────────────────────
Extracts AI/LLM frameworks, prompt templates, system instructions,
and Human-in-the-Loop (HITL) approval nodes from Python AST and Knowledge Graph.
"""

import ast
from pathlib import Path
from typing import Dict, Any, List


AI_FRAMEWORKS = {
    "openai": "OpenAI API",
    "anthropic": "Anthropic Claude API",
    "langchain": "LangChain Framework",
    "llama_index": "LlamaIndex RAG Framework",
    "transformers": "HuggingFace Transformers",
    "vllm": "vLLM Inference Engine",
    "ollama": "Ollama Local Models",
    "google.generativeai": "Google Gemini API",
    "litellm": "LiteLLM Router"
}

HITL_KEYWORDS = [
    "confirm", "approve", "review", "human_override", "manual_approval",
    "wait_for_user", "ask_user", "ask_permission", "user_confirmation",
    "hitl", "human_in_the_loop", "override_gate"
]


class AIAssetExtractor:
    """
    Scans project codebase to detect AI/LLM models, prompt declarations,
    and Human-in-the-Loop oversight mechanisms (EU AI Act Art. 14).
    """

    def __init__(self, project_root: Path):
        self.project_root = Path(project_root)

    def extract_ai_assets(self) -> Dict[str, Any]:
        """
        Scans all Python files in project_root for AI frameworks, prompt constants,
        and human oversight gates.
        """
        detected_frameworks = set()
        prompts = []
        hitl_nodes = []
        ai_symbol_calls = 0

        import os

        skip_dirs = {"venv", ".git", "repos", "reports", "node_modules", ".lore", "build", "dist"}

        for root, dirs, files in os.walk(str(self.project_root)):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            for file in files:
                if not file.endswith(".py"):
                    continue

                py_file = Path(root) / file
                try:
                    content = py_file.read_text(encoding="utf-8", errors="ignore")
                    tree = ast.parse(content, filename=str(py_file))
                    rel_path = str(py_file.relative_to(self.project_root)).replace("\\", "/")

                    for node in ast.walk(tree):
                        # Check imports for AI Frameworks
                        if isinstance(node, ast.Import):
                            for alias in node.names:
                                for fw_key, fw_name in AI_FRAMEWORKS.items():
                                    if alias.name.startswith(fw_key):
                                        detected_frameworks.add(fw_name)

                        elif isinstance(node, ast.ImportFrom):
                            if node.module:
                                for fw_key, fw_name in AI_FRAMEWORKS.items():
                                    if node.module.startswith(fw_key):
                                        detected_frameworks.add(fw_name)

                        # Check for Prompt strings & system instructions
                        elif isinstance(node, ast.Assign):
                            for target in node.targets:
                                if isinstance(target, ast.Name):
                                    var_name = target.id.lower()
                                    if "prompt" in var_name or "system_message" in var_name or "instruction" in var_name:
                                        prompts.append({
                                            "file": rel_path,
                                            "name": target.id,
                                            "line": node.lineno
                                        })

                        # Check for Function calls or defs matching HITL keywords
                        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            func_name = node.name.lower()
                            if any(k in func_name for k in HITL_KEYWORDS):
                                hitl_nodes.append({
                                    "file": rel_path,
                                    "function": node.name,
                                    "line": node.lineno
                                })

                        elif isinstance(node, ast.Call):
                            if isinstance(node.func, ast.Name):
                                if any(k in node.func.id.lower() for k in HITL_KEYWORDS):
                                    hitl_nodes.append({
                                        "file": rel_path,
                                        "call": node.func.id,
                                        "line": node.lineno
                                    })

                except Exception:
                    continue

        return {
            "ai_frameworks": sorted(list(detected_frameworks)),
            "prompts_count": len(prompts),
            "prompts": prompts[:10],
            "hitl_nodes_count": len(hitl_nodes),
            "hitl_nodes": hitl_nodes[:10],
            "has_ai_integration": len(detected_frameworks) > 0 or len(prompts) > 0
        }
