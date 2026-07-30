"""
parsers/ast_skeleton.py
───────────────────────
LORE 2.0 AST Skeletonizer.
Strips function and method bodies while preserving type-hinted signatures,
classes, return types, docstrings, and ADR/Taint annotations.
"""

import ast
import re

def skeletonize_python(code: str, preserve_docstrings: bool = True) -> str:
    """Generate a Python .pyi style typed skeleton from Python source code."""
    try:
        tree = ast.parse(code)
        
        class SkeletonTransformer(ast.NodeTransformer):
            def visit_FunctionDef(self, node):
                return self._skeletonize_func(node)
                
            def visit_AsyncFunctionDef(self, node):
                return self._skeletonize_func(node)

            def _skeletonize_func(self, node):
                docstring = ast.get_docstring(node)
                new_body = []
                if docstring and preserve_docstrings:
                    new_body.append(ast.Expr(value=ast.Constant(value=docstring)))
                new_body.append(ast.Expr(value=ast.Constant(value=Ellipsis)))
                node.body = new_body
                return node

        transformed = SkeletonTransformer().visit(tree)
        ast.fix_missing_locations(transformed)
        return ast.unparse(transformed)
    except Exception:
        # Regex fallback for partial code snippets or syntax errors
        return _regex_skeletonize_python(code)


def _regex_skeletonize_python(code: str) -> str:
    """Fallback regex skeletonizer for Python snippets."""
    lines = code.splitlines()
    out = []
    in_func = False
    func_indent = 0

    for line in lines:
        stripped = line.lstrip()
        indent = len(line) - len(stripped)

        if stripped.startswith(("def ", "async def ", "class ")):
            in_func = stripped.startswith(("def ", "async def "))
            func_indent = indent
            out.append(line)
            if in_func and stripped.endswith(":"):
                out.append(" " * (indent + 4) + "...")
        elif in_func and indent > func_indent:
            continue  # Skip body lines
        else:
            in_func = False
            out.append(line)

    return "\n".join(out)


def skeletonize_generic(code: str, language: str = "typescript") -> str:
    """Generic AST skeletonizer for TypeScript, Go, C, etc."""
    # Pattern to match function signatures and body blocks { ... }
    if language in ("typescript", "javascript", "js", "ts"):
        # Match function/method declaration and body
        pattern = r'((?:export\s+)?(?:async\s+)?function\s+\w+\s*\([^)]*\)\s*(?::\s*[^{]+)?)\s*\{[^}]*\}'
        return re.sub(pattern, r'\1 { ... }', code)
    elif language in ("go", "golang"):
        pattern = r'(func\s+(?:\([^)]+\)\s+)?\w+\s*\([^)]*\)\s*(?:[^{]+)?)\s*\{[^}]*\}'
        return re.sub(pattern, r'\1 { ... }', code)
    elif language in ("c", "cpp"):
        pattern = r'((?:\w+\s+)+\w+\s*\([^)]*\)\s*)\s*\{[^}]*\}'
        return re.sub(pattern, r'\1 { ... }', code)
    return code


def skeletonize_code(code: str, language: str = "python") -> str:
    """Main entry point for skeletonizing code snippets based on language."""
    lang_lower = language.lower()
    if lang_lower in ("py", "python"):
        return skeletonize_python(code)
    else:
        return skeletonize_generic(code, lang_lower)
