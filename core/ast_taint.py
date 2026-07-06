from __future__ import annotations
import ast
from core.base_tracer import BaseTaintTracer
from core.ast_taint_helpers import (
    _get_expr_str,
    ReturnTaintChecker,
    CallTaintDetector,
    SourceDetector,
    ExpressionTaintDetector,
    ArgChecker,
    CallArgChecker,
)


class PythonASTTaintTracer(ast.NodeVisitor, BaseTaintTracer):
    def __init__(self, source_code: str, external_sources: set[str] | None = None, external_tainted_functions: set[str] | None = None):
        self.source_code = source_code
        self.lines = source_code.splitlines()
        self.external_sources = external_sources if external_sources else set()
        self.external_tainted_functions = external_tainted_functions if external_tainted_functions else set()
        self.tainted_vars: set[str] = set()
        self.detected_flows: list[dict] = []
        self.var_sources: dict[str, dict] = {}
        self.outgoing_calls: list[dict] = []
        self.tainted_functions: set[str] = set()
        self._current_function_returns_tainted = False

    def trace(self) -> dict:
        try:
            tree = ast.parse(self.source_code)
            self.visit(tree)
            return {
                "flows": self.detected_flows,
                "outgoing_calls": self.outgoing_calls,
                "tainted_returns": list(self.tainted_functions)
            }
        except Exception as e:
            import logging
            logging.getLogger(__name__).exception(f"AST Taint parsing error: {e}")
            return {"flows": [], "outgoing_calls": [], "tainted_returns": []}

    def _check_tainted_expression(self, node: ast.AST) -> Optional[str]:
        expr_str = _get_expr_str(node)
        if expr_str and expr_str in self.tainted_vars:
            return expr_str
        
        curr = node
        while isinstance(curr, (ast.Attribute, ast.Subscript)):
            curr = curr.value
            base_str = _get_expr_str(curr)
            if base_str and base_str in self.tainted_vars:
                return base_str
        return None

    def _process_assignment_target(self, target: ast.AST, is_source: bool, source_desc: str, taint_sources: list[str], lineno: int):
        target_str = _get_expr_str(target)
        if target_str:
            if is_source:
                self.tainted_vars.add(target_str)
                self.var_sources[target_str] = {
                    "line": lineno,
                    "desc": source_desc,
                    "path": (self.var_sources.get(taint_sources[0], {}).get("path", []) if taint_sources else []) + [f"{target_str} (L{lineno})"]
                }
            else:
                if target_str in self.tainted_vars:
                    self.tainted_vars.remove(target_str)
                if isinstance(target, ast.Name):
                    prefix = target.id
                    to_remove = [v for v in self.tainted_vars if v.startswith(f"{prefix}.") or v.startswith(f"{prefix}[") or v.startswith(f"{prefix}['")]
                    for v in to_remove:
                        self.tainted_vars.remove(v)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                self._process_assignment_target(elt, is_source, source_desc, taint_sources, lineno)

    def _merge_states(self, state_a: set[str], sources_a: dict, state_b: set[str], sources_b: dict) -> Tuple[set[str], dict]:
        merged_vars = state_a.union(state_b)
        merged_sources = {}
        for var in merged_vars:
            if var in state_a and var in state_b:
                desc_a = sources_a[var]["desc"]
                desc_b = sources_b[var]["desc"]
                if desc_a == desc_b:
                    merged_sources[var] = sources_a[var]
                else:
                    merged_sources[var] = {
                        "line": sources_a[var]["line"],
                        "desc": f"merged: {desc_a} | {desc_b}",
                        "path": list(dict.fromkeys(sources_a[var]["path"] + sources_b[var]["path"]))
                    }
            elif var in state_a:
                merged_sources[var] = {
                    "line": sources_a[var]["line"],
                    "desc": f"conditional-taint: {sources_a[var]['desc']}",
                    "path": sources_a[var]["path"]
                }
            else:
                merged_sources[var] = {
                    "line": sources_b[var]["line"],
                    "desc": f"conditional-taint: {sources_b[var]['desc']}",
                    "path": sources_b[var]["path"]
                }
        return merged_vars, merged_sources

    def _analyze_assignment_rhs(self, value_node: ast.AST) -> Tuple[bool, str, list[str]]:
        is_source = False
        source_desc = ""
        taint_sources = []
        
        ctd = CallTaintDetector(self.tainted_functions, self.external_tainted_functions)
        ctd.visit(value_node)
        if ctd.found:
            is_source = True
            source_desc = f"return value of {ctd.func_name}()"
        else:
            sd = SourceDetector()
            sd.visit(value_node)
            if sd.found:
                is_source = True
                source_desc = sd.desc
                
        etd = ExpressionTaintDetector(self._check_tainted_expression)
        etd.visit(value_node)
        if etd.hits:
            is_source = True
            taint_sources = etd.hits
            source_desc = f"propagated from {', '.join(etd.hits)}"
            
        return is_source, source_desc, taint_sources

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self._current_function_returns_tainted = False
        all_args = list(node.args.args)
        if hasattr(node.args, "posonlyargs") and node.args.posonlyargs:
            all_args.extend(node.args.posonlyargs)
        if hasattr(node.args, "kwonlyargs") and node.args.kwonlyargs:
            all_args.extend(node.args.kwonlyargs)
        if node.args.vararg:
            all_args.append(node.args.vararg)
        if node.args.kwarg:
            all_args.append(node.args.kwarg)

        for arg in all_args:
            if arg.arg in self.external_sources:
                self.tainted_vars.add(arg.arg)
                self.var_sources[arg.arg] = {
                    "line": node.lineno,
                    "desc": f"parameter {arg.arg}",
                    "path": [f"{arg.arg} (L{node.lineno})"]
                }
        self.generic_visit(node)
        if self._current_function_returns_tainted:
            self.tainted_functions.add(node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        self._current_function_returns_tainted = False
        all_args = list(node.args.args)
        if hasattr(node.args, "posonlyargs") and node.args.posonlyargs:
            all_args.extend(node.args.posonlyargs)
        if hasattr(node.args, "kwonlyargs") and node.args.kwonlyargs:
            all_args.extend(node.args.kwonlyargs)
        if node.args.vararg:
            all_args.append(node.args.vararg)
        if node.args.kwarg:
            all_args.append(node.args.kwarg)

        for arg in all_args:
            if arg.arg in self.external_sources:
                self.tainted_vars.add(arg.arg)
                self.var_sources[arg.arg] = {
                    "line": node.lineno,
                    "desc": f"parameter {arg.arg}",
                    "path": [f"{arg.arg} (L{node.lineno})"]
                }
        self.generic_visit(node)
        if self._current_function_returns_tainted:
            self.tainted_functions.add(node.name)

    def visit_For(self, node: ast.For):
        is_source, source_desc, taint_sources = self._analyze_assignment_rhs(node.iter)
        self._process_assignment_target(node.target, is_source, source_desc, taint_sources, node.lineno)
        self.generic_visit(node)

    def visit_comprehension(self, node: ast.comprehension):
        is_source, source_desc, taint_sources = self._analyze_assignment_rhs(node.iter)
        self._process_assignment_target(node.target, is_source, source_desc, taint_sources, getattr(node, "lineno", 1))
        self.generic_visit(node)

    def visit_Return(self, node: ast.Return):
        if node.value is not None:
            rtc = ReturnTaintChecker(self._check_tainted_expression)
            rtc.visit(node.value)
            if rtc.found:
                self._current_function_returns_tainted = True
        self.generic_visit(node)

    def visit_If(self, node: ast.If):
        vars_before = set(self.tainted_vars)
        sources_before = {k: dict(v) for k, v in self.var_sources.items()}
        
        self.visit(node.test)
        
        # Branch 1: Body
        self.tainted_vars = set(vars_before)
        self.var_sources = {k: dict(v) for k, v in sources_before.items()}
        for stmt in node.body:
            self.visit(stmt)
        vars_body = set(self.tainted_vars)
        sources_body = {k: dict(v) for k, v in self.var_sources.items()}
        
        # Branch 2: Orelse
        self.tainted_vars = set(vars_before)
        self.var_sources = {k: dict(v) for k, v in sources_before.items()}
        for stmt in node.orelse:
            self.visit(stmt)
        vars_orelse = set(self.tainted_vars)
        sources_orelse = {k: dict(v) for k, v in self.var_sources.items()}
        
        self.tainted_vars, self.var_sources = self._merge_states(
            vars_body, sources_body, vars_orelse, sources_orelse
        )

    def visit_Try(self, node: ast.Try):
        vars_before = set(self.tainted_vars)
        sources_before = {k: dict(v) for k, v in self.var_sources.items()}
        
        self.tainted_vars = set(vars_before)
        self.var_sources = {k: dict(v) for k, v in sources_before.items()}
        for stmt in node.body:
            self.visit(stmt)
        vars_try = set(self.tainted_vars)
        sources_try = {k: dict(v) for k, v in self.var_sources.items()}
        
        merged_vars = vars_try
        merged_sources = sources_try
        
        for handler in node.handlers:
            self.tainted_vars = set(vars_before)
            self.var_sources = {k: dict(v) for k, v in sources_before.items()}
            self.visit(handler)
            vars_h = set(self.tainted_vars)
            sources_h = {k: dict(v) for k, v in self.var_sources.items()}
            merged_vars, merged_sources = self._merge_states(
                merged_vars, merged_sources, vars_h, sources_h
            )
            
        if node.orelse:
            self.tainted_vars = set(vars_try)
            self.var_sources = {k: dict(v) for k, v in sources_try.items()}
            for stmt in node.orelse:
                self.visit(stmt)
            vars_orelse = set(self.tainted_vars)
            sources_orelse = {k: dict(v) for k, v in self.var_sources.items()}
            merged_vars, merged_sources = self._merge_states(
                merged_vars, merged_sources, vars_orelse, sources_orelse
            )
            
        if node.finalbody:
            self.tainted_vars = set(merged_vars)
            self.var_sources = {k: dict(v) for k, v in merged_sources.items()}
            for stmt in node.finalbody:
                self.visit(stmt)
        else:
            self.tainted_vars = merged_vars
            self.var_sources = merged_sources

    def visit_Assign(self, node: ast.Assign):
        is_source, source_desc, taint_sources = self._analyze_assignment_rhs(node.value)
        for target in node.targets:
            self._process_assignment_target(target, is_source, source_desc, taint_sources, node.lineno)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign):
        if node.value is None:
            return
        is_source, source_desc, taint_sources = self._analyze_assignment_rhs(node.value)
        self._process_assignment_target(node.target, is_source, source_desc, taint_sources, node.lineno)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign):
        etd = ExpressionTaintDetector(self._check_tainted_expression)
        etd.visit(node.value)
        etd_target = ExpressionTaintDetector(self._check_tainted_expression)
        etd_target.visit(node.target)
        
        hits = etd.hits + etd_target.hits
        is_source = False
        source_desc = ""
        if hits:
            is_source = True
            source_desc = f"propagated via augmented assign from {', '.join(hits)}"
            
        self._process_assignment_target(node.target, is_source, source_desc, hits, node.lineno)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call):
        sink_names = {
            "eval", "exec", "execute", "system", "popen", "subprocess", "pickle", "yaml", 
            "dumps", "loads", "serialize", "deserialize", "sql", "order_by", "explain",
            "StringAgg", "Trunc", "Extract", "KeyTransform", "RawSQL"
        }
        
        func_name = ""
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            func_name = node.func.attr
            
        if func_name in sink_names:
            ac = ArgChecker(self._check_tainted_expression)
            for arg in node.args:
                ac.visit(arg)
            for kw in node.keywords:
                ac.visit(kw.value)
                
            if ac.found_tainted:
                for t_var in ac.found_tainted:
                    src_info = self.var_sources.get(t_var, {"line": node.lineno, "desc": "unknown", "path": [t_var]})
                    self.detected_flows.append({
                        "var_name": t_var,
                        "source_line": src_info["line"],
                        "source_desc": src_info["desc"],
                        "sink_line": node.lineno,
                        "sink_name": func_name,
                        "flow_path": src_info["path"] + [f"{func_name}() (L{node.lineno})"],
                        "code_snippet": self.lines[node.lineno - 1].strip() if 0 < node.lineno <= len(self.lines) else ""
                    })
        elif func_name:
            for idx, arg in enumerate(node.args):
                cac = CallArgChecker(self._check_tainted_expression)
                cac.visit(arg)
                if cac.tainted_args:
                    for t_var in cac.tainted_args:
                        self.outgoing_calls.append({
                            "func_name": func_name,
                            "arg_index": idx,
                            "arg_name": None,
                            "var_name": t_var,
                            "path": self.var_sources[t_var]["path"]
                        })
            for kw in node.keywords:
                cac = CallArgChecker(self._check_tainted_expression)
                cac.visit(kw.value)
                if cac.tainted_args:
                    for t_var in cac.tainted_args:
                        self.outgoing_calls.append({
                            "func_name": func_name,
                            "arg_index": None,
                            "arg_name": kw.arg,
                            "var_name": t_var,
                            "path": self.var_sources[t_var]["path"]
                        })
                    
        self.generic_visit(node)


def check_ast_taint(source_code: str) -> list[dict]:
    """
    Parse Python source code using AST and trace variable assignments
    to find input variables originating from user input (Sources)
    that flow into unsafe functions (Sinks).
    """
    return check_ast_taint_interprocedural(source_code, None).get("flows", [])


def check_ast_taint_interprocedural(source_code: str, external_sources: set[str] | None = None, external_tainted_functions: set[str] | None = None) -> dict:
    """
    Parse Python source code using AST and trace variable assignments.
    """
    tracer = PythonASTTaintTracer(source_code, external_sources, external_tainted_functions)
    return tracer.trace()
