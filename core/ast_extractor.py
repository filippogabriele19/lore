from __future__ import annotations
import ast
import json
import re
from typing import Iterable, List, Set, Tuple, Optional

_JSON_BLOCK_RE = re.compile(
    r"```(?:json)?\s*(.*?)\s*```",
    re.DOTALL | re.IGNORECASE,
)


def parse_llm_json_list(raw: str) -> List[str]:
    """
    Parse a JSON list returned by an LLM.
    
    Accepts multiple formats:
      - Pure JSON array
      - JSON fenced in markdown code blocks (```json [...] ```)
      - JSON embedded within text
    
    Args:
        raw: Raw LLM output string
    
    Returns:
        Parsed list of strings, or empty list if parsing fails
    """
    if not raw or not raw.strip():
        return []

    candidates: List[str] = []

    # 1. Extract fenced code blocks
    for match in _JSON_BLOCK_RE.findall(raw):
        candidates.append(match.strip())

    # 2. Extract JSON list patterns from text
    json_pattern = re.compile(r'\[\s*(?:"[^"]*"(?:\s*,\s*"[^"]*")*)\s*\]', re.DOTALL)
    for match in json_pattern.findall(raw):
        candidates.append(match.strip())

    # 3. Try raw text as fallback
    candidates.append(raw.strip())

    for candidate in candidates:
        try:
            data = json.loads(candidate)
            if isinstance(data, list) and all(isinstance(x, str) for x in data):
                return data
        except json.JSONDecodeError:
            continue

    return []


class DefinitionCollector(ast.NodeVisitor):
    """Collects all function and class definition names from an AST."""
    
    def __init__(self) -> None:
        self.definitions: Set[str] = set()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.definitions.add(node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.definitions.add(node.name)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.definitions.add(node.name)


def collect_definitions(source: str) -> Set[str]:
    """
    Collect all top-level function and class names from source code.
    
    Args:
        source: Python source code string
        
    Returns:
        Set of definition names
    """
    tree = ast.parse(source)
    collector = DefinitionCollector()
    collector.visit(tree)
    return collector.definitions


def extract_function_source(source_code: str, func_name: str) -> Optional[str]:
    """
    Extract the exact source code of a function or class by name.
    """
    try:
        tree = ast.parse(source_code)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name == func_name:
                    # Determine start line, including decorators
                    start_line = node.lineno
                    if hasattr(node, 'decorator_list') and node.decorator_list:
                        first_decorator = node.decorator_list[0]
                        if hasattr(first_decorator, 'lineno'):
                            start_line = first_decorator.lineno
                    
                    lines = source_code.splitlines(keepends=True)
                    end_line = node.end_lineno if hasattr(node, 'end_lineno') else node.lineno
                    
                    if start_line and end_line:
                        extracted = "".join(lines[start_line - 1:end_line])
                        return extracted
                    else:
                        return ast.get_source_segment(source_code, node)
    except Exception:
        return None
    return None


def extract_function_by_name(source_code: str, function_name: str) -> Optional[str]:
    """
    Extract a complete function or class definition by name using AST.
    """
    try:
        tree = ast.parse(source_code)
        lines = source_code.splitlines(keepends=True)
        
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name == function_name:
                    start_line = node.lineno
                    if hasattr(node, 'decorator_list') and node.decorator_list:
                        first_decorator = node.decorator_list[0]
                        if hasattr(first_decorator, 'lineno'):
                            start_line = first_decorator.lineno
                    
                    end_line = node.end_lineno if hasattr(node, 'end_lineno') else node.lineno
                    
                    if start_line and end_line and start_line <= len(lines) and end_line <= len(lines):
                        extracted = "".join(lines[start_line - 1:end_line])
                        return extracted
    except Exception:
        return None
    return None


def extract_imports_source(source_code: str) -> str:
    """
    Extract ONLY import lines from source code.
    """
    try:
        tree = ast.parse(source_code)
        imports = []
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                segment = ast.get_source_segment(source_code, node)
                if segment:
                    imports.append(segment)
        return "\n".join(imports)
    except Exception:
        return ""


def expand_entity_list(source_code: str, initial_names: list[str]) -> list[str]:
    """
    Analizza il codice e trova tutte le dipendenze locali (classi/helper)
    necessarie per le entità nella lista iniziale.
    """
    AST_EXPANSION_BLACKLIST = {
        '__name__', '__file__', '__doc__', 'self', 'cls', 
        'None', 'True', 'False', 'args', 'kwargs',
        'Exception', 'RuntimeError', 'NotImplementedError'
    }
    try:
        tree = ast.parse(source_code)
    except Exception as e:
        print(f"⚠️ Errore durante il parsing AST per espansione: {e}")
        return initial_names

    local_definitions = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            local_definitions[node.name] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    local_definitions[target.id] = node

    entities_to_move = set(initial_names)
    queue = list(initial_names)
    processed = set()

    while queue:
        current_name = queue.pop(0)
        if current_name in processed or current_name not in local_definitions:
            continue
        
        processed.add(current_name)
        node = local_definitions[current_name]

        for subnode in ast.walk(node):
            if isinstance(subnode, ast.Name) and isinstance(subnode.ctx, ast.Load):
                used_name = subnode.id
                if used_name in AST_EXPANSION_BLACKLIST:
                    continue
                if used_name in local_definitions and used_name not in entities_to_move:
                    print(f"🔍 Dipendenza trovata: '{current_name}' usa '{used_name}'. Aggiungo all'estrazione.")
                    entities_to_move.add(used_name)
                    queue.append(used_name)

    return list(entities_to_move)
