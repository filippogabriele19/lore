from __future__ import annotations
import ast
from typing import Iterable


class ASTPatchError(RuntimeError):
    """Custom exception for AST patching operations."""
    pass


class DeletionTransformer(ast.NodeTransformer):
    """Transformer that removes specific function and class definitions by name."""
    
    def __init__(self, to_delete: Iterable[str]) -> None:
        self.to_delete = set(to_delete)

    def visit_FunctionDef(self, node):
        if node.name in self.to_delete:
            return None
        return self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node):
        if node.name in self.to_delete:
            return None
        return self.generic_visit(node)

    def visit_ClassDef(self, node):
        if node.name in self.to_delete:
            return None
        return self.generic_visit(node)


def delete_definitions(source: str, names: Iterable[str]) -> str:
    """
    Remove functions or classes by name from source code.
    """
    if not names:
        return source

    tree = ast.parse(source)
    transformer = DeletionTransformer(names)
    new_tree = transformer.visit(tree)
    ast.fix_missing_locations(new_tree)

    return ast.unparse(new_tree)


def inject_import_at_top(source_code: str, new_import_code: str) -> str:
    """
    Insert a new import at the correct position in the file.
    """
    if not new_import_code.strip():
        return source_code

    try:
        tree = ast.parse(source_code)
    except SyntaxError:
        return new_import_code + "\n" + source_code

    lines = source_code.splitlines()
    insert_line_index = 0
    
    last_import_line = 0
    has_imports = False
    
    for node in tree.body:
        if isinstance(node, ast.Expr) and isinstance(node.value, (ast.Str, ast.Constant)):
            continue
            
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            has_imports = True
            if hasattr(node, 'end_lineno') and node.end_lineno:
                last_import_line = max(last_import_line, node.end_lineno)
            else:
                last_import_line = max(last_import_line, node.lineno)
        else:
            if has_imports:
                break
    
    if last_import_line > 0:
        insert_line_index = last_import_line
    else:
        for i, line in enumerate(lines):
            l = line.strip()
            if not l: continue
            if l.startswith("#"): continue
            if l.startswith('"""') or l.startswith("'''"): 
                continue 
            insert_line_index = i
            break
            
    new_lines = new_import_code.strip().splitlines()
    final_lines = lines[:insert_line_index] + new_lines + lines[insert_line_index:]
    
    return "\n".join(final_lines)
