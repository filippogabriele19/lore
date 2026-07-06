from __future__ import annotations
import ast
from typing import Optional


def _get_expr_str(node: ast.AST) -> Optional[str]:
    """Helper to reconstruct variable expression names (e.g. x.y or x['y']) from AST."""
    if isinstance(node, ast.Name):
        return node.id
    elif isinstance(node, ast.Attribute):
        base = _get_expr_str(node.value)
        return f"{base}.{node.attr}" if base else None
    elif isinstance(node, ast.Subscript):
        base = _get_expr_str(node.value)
        if not base:
            return None
        sl = node.slice
        # Unwrap Index for Python <= 3.8
        if isinstance(sl, ast.Index):
            sl = sl.value
        if isinstance(sl, ast.Constant):
            val = sl.value
            if isinstance(val, str):
                return f"{base}['{val}']"
            else:
                return f"{base}[{val}]"
        elif isinstance(sl, ast.Str):
            return f"{base}['{sl.s}']"
        elif isinstance(sl, ast.Num):
            return f"{base}[{sl.n}]"
        return f"{base}[...]"
    return None


class ReturnTaintChecker(ast.NodeVisitor):
    def __init__(self, check_fn):
        self.check_fn = check_fn
        self.found = False

    def visit_Name(self, name_node):
        if self.check_fn(name_node):
            self.found = True

    def visit_Attribute(self, attr_node):
        if self.check_fn(attr_node):
            self.found = True
        self.generic_visit(attr_node)

    def visit_Subscript(self, sub_node):
        if self.check_fn(sub_node):
            self.found = True
        self.generic_visit(sub_node)


class CallTaintDetector(ast.NodeVisitor):
    def __init__(self, local_tainted, external_tainted):
        self.local_tainted = local_tainted
        self.external_tainted = external_tainted
        self.found = False
        self.func_name = ""

    def visit_Call(self, call_node):
        if isinstance(call_node.func, ast.Name):
            name = call_node.func.id
        elif isinstance(call_node.func, ast.Attribute):
            name = call_node.func.attr
        else:
            name = None
        if name and (name in self.local_tainted or name in self.external_tainted):
            self.found = True
            self.func_name = name


class SourceDetector(ast.NodeVisitor):
    def __init__(self):
        self.found = False
        self.desc = ""

    def visit_Attribute(self, attr_node):
        base_name = _get_expr_str(attr_node.value)
        is_request_base = False
        if base_name:
            base_lower = base_name.lower()
            if base_lower in ("request", "req") or base_lower.endswith(".request") or base_lower.endswith(".req"):
                is_request_base = True

        if is_request_base:
            self.found = True
            self.desc = f"{base_name}.{attr_node.attr}"
        elif attr_node.attr in ("GET", "POST", "query_params", "params", "COOKIES"):
            if base_name and any(x in base_name.lower() for x in ("request", "req", "conn", "http")):
                self.found = True
                self.desc = f"{base_name}.{attr_node.attr}"
        elif attr_node.attr == "data":
            if base_name and any(x in base_name.lower() for x in ("request", "req", "payload", "body")):
                self.found = True
                self.desc = f"{base_name}.data"
        self.generic_visit(attr_node)

    def visit_Subscript(self, sub_node):
        base_name = _get_expr_str(sub_node.value)
        if base_name:
            base_lower = base_name.lower()
            if base_lower in ("request", "req") or base_lower.endswith(".request") or base_lower.endswith(".req"):
                self.found = True
                self.desc = f"{base_name}[...]"
        self.generic_visit(sub_node)


class ExpressionTaintDetector(ast.NodeVisitor):
    def __init__(self, check_fn):
        self.check_fn = check_fn
        self.hits = []

    def visit_Name(self, name_node):
        t = self.check_fn(name_node)
        if t:
            self.hits.append(t)
        self.generic_visit(name_node)

    def visit_Attribute(self, attr_node):
        t = self.check_fn(attr_node)
        if t:
            self.hits.append(t)
            return
        self.generic_visit(attr_node)

    def visit_Subscript(self, sub_node):
        t = self.check_fn(sub_node)
        if t:
            self.hits.append(t)
            return
        self.generic_visit(sub_node)


class ArgChecker(ast.NodeVisitor):
    def __init__(self, check_fn):
        self.check_fn = check_fn
        self.found_tainted = []

    def visit_Name(self, name_node):
        t = self.check_fn(name_node)
        if t:
            self.found_tainted.append(t)
        self.generic_visit(name_node)

    def visit_Attribute(self, attr_node):
        t = self.check_fn(attr_node)
        if t:
            self.found_tainted.append(t)
            return
        self.generic_visit(attr_node)

    def visit_Subscript(self, sub_node):
        t = self.check_fn(sub_node)
        if t:
            self.found_tainted.append(t)
            return
        self.generic_visit(sub_node)


class CallArgChecker(ast.NodeVisitor):
    def __init__(self, check_fn):
        self.check_fn = check_fn
        self.tainted_args = []

    def visit_Name(self, name_node):
        t = self.check_fn(name_node)
        if t:
            self.tainted_args.append(t)
        self.generic_visit(name_node)

    def visit_Attribute(self, attr_node):
        t = self.check_fn(attr_node)
        if t:
            self.tainted_args.append(t)
            return
        self.generic_visit(attr_node)

    def visit_Subscript(self, sub_node):
        t = self.check_fn(sub_node)
        if t:
            self.tainted_args.append(t)
            return
        self.generic_visit(sub_node)
