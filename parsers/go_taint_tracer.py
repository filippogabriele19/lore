from __future__ import annotations
from pathlib import Path
from typing import Optional, Tuple, Set, Dict, List
from core.base_tracer import BaseTaintTracer
from parsers.go_parser import _load_go_parser

def _get_node_text(node, src_bytes: bytes) -> str:
    return src_bytes[node.start_byte:node.end_byte].decode("utf-8")

def _check_expr_taint(node, tainted_vars: Set[str], src_bytes: bytes) -> Tuple[bool, str]:
    if node.type == "identifier":
        name = _get_node_text(node, src_bytes)
        if name in tainted_vars:
            return True, f"propagated from {name}"
    elif node.type == "selector_expression":
        text = _get_node_text(node, src_bytes)
        if text in tainted_vars:
            return True, f"propagated from {text}"
        # Check base
        operand = node.child_by_field_name("operand") or node.children[0]
        is_op_tainted, desc = _check_expr_taint(operand, tainted_vars, src_bytes)
        if is_op_tainted:
            return True, desc
    elif node.type == "call_expression":
        func_n = node.child_by_field_name("function") or node.children[0]
        func_text = _get_node_text(func_n, src_bytes).lower()
        if any(pat in func_text for pat in ("formvalue", "formfile", "query", "get", "cookie", "request", "header")):
            return True, f"source: {func_text}"
        # Check args
        args = node.child_by_field_name("arguments") or node.children[-1]
        for child in args.children:
            is_arg_tainted, desc = _check_expr_taint(child, tainted_vars, src_bytes)
            if is_arg_tainted:
                return True, desc
    for child in node.children:
        is_child_tainted, desc = _check_expr_taint(child, tainted_vars, src_bytes)
        if is_child_tainted:
            return True, desc
    return False, ""

def _extract_lhs_names(node, src_bytes: bytes) -> List[str]:
    names = []
    if node.type in ("identifier", "field_identifier"):
        names.append(_get_node_text(node, src_bytes))
    elif node.type == "selector_expression":
        names.append(_get_node_text(node, src_bytes))
    elif node.type == "expression_list":
        for child in node.children:
            names.extend(_extract_lhs_names(child, src_bytes))
    else:
        for child in node.children:
            if child.type not in (",", "=", ":="):
                names.extend(_extract_lhs_names(child, src_bytes))
    return names

class GoASTTaintTracer(BaseTaintTracer):
    def __init__(self, source_code: str, external_sources: Set[str] | None = None):
        self.source_code = source_code
        self.lines = source_code.splitlines()
        self.src_bytes = source_code.encode("utf-8")
        self.external_sources = external_sources if external_sources else set()
        self.tainted_vars: Set[str] = set()
        self.detected_flows: List[Dict] = []
        self.var_sources: Dict[str, Dict] = {}
        self.outgoing_calls: List[Dict] = []

    def trace(self) -> Dict:
        try:
            parser_ts, _ = _load_go_parser()
            tree = parser_ts.parse(self.src_bytes)
        except Exception as e:
            print(f"⚠️ Failed to parse Go file for taint analysis: {e}")
            return {"flows": [], "outgoing_calls": []}

        # 1. Walk and trace
        self._walk(tree.root_node)

        return {
            "flows": self.detected_flows,
            "outgoing_calls": self.outgoing_calls
        }

    def _walk(self, node):
        t = node.type

        # 1. Parameter declaration
        if t == "parameter_declaration":
            # Go parameters: name_list type (e.g. name1, name2 string)
            for child in node.children:
                if child.type == "identifier":
                    name = _get_node_text(child, self.src_bytes)
                    if name in self.external_sources:
                        self.tainted_vars.add(name)
                        self.var_sources[name] = {
                            "line": node.start_point[0] + 1,
                            "desc": f"parameter {name}",
                            "path": [f"{name} (L{node.start_point[0] + 1})"]
                        }

        # 2. Assignment / Var spec
        elif t in ("short_var_declaration", "assignment_statement"):
            left = node.child_by_field_name("left") or node.children[0]
            right = node.child_by_field_name("right") or node.children[-1]
            is_tainted, desc = _check_expr_taint(right, self.tainted_vars, self.src_bytes)
            lhs_names = _extract_lhs_names(left, self.src_bytes)
            for name in lhs_names:
                if is_tainted:
                    self.tainted_vars.add(name)
                    # build path
                    orig_path = []
                    for t_var in self.tainted_vars:
                        if t_var in desc and t_var in self.var_sources:
                            orig_path = self.var_sources[t_var]["path"]
                            break
                    self.var_sources[name] = {
                        "line": node.start_point[0] + 1,
                        "desc": desc,
                        "path": orig_path + [f"{name} (L{node.start_point[0] + 1})"]
                    }
                else:
                    if name in self.tainted_vars:
                        self.tainted_vars.remove(name)

        elif t == "var_spec":
            # E.g. var x = request.Query
            names_node = node.children[0]
            lhs_names = _extract_lhs_names(names_node, self.src_bytes)
            # Find values if present
            values_node = None
            for child in node.children:
                if child.type in ("expression_list", "call_expression", "identifier", "selector_expression"):
                    values_node = child
            if values_node:
                is_tainted, desc = _check_expr_taint(values_node, self.tainted_vars, self.src_bytes)
                for name in lhs_names:
                    if is_tainted:
                        self.tainted_vars.add(name)
                        orig_path = []
                        for t_var in self.tainted_vars:
                            if t_var in desc and t_var in self.var_sources:
                                orig_path = self.var_sources[t_var]["path"]
                                break
                        self.var_sources[name] = {
                            "line": node.start_point[0] + 1,
                            "desc": desc,
                            "path": orig_path + [f"{name} (L{node.start_point[0] + 1})"]
                        }
                    else:
                        if name in self.tainted_vars:
                            self.tainted_vars.remove(name)

        # 3. Call expression
        elif t == "call_expression":
            func_n = node.child_by_field_name("function") or node.children[0]
            func_text = _get_node_text(func_n, self.src_bytes)
            func_lower = func_text.lower()
            
            # Sinks
            sink_names = {"command", "exec", "query", "startprocess", "system", "eval", "rawsql"}
            is_sink = any(s in func_lower for s in sink_names)
            
            # Check args
            args = node.child_by_field_name("arguments") or node.children[-1]
            arg_children = [c for c in args.children if c.type not in (",", "(", ")", ";")]
            for idx, child in enumerate(arg_children):
                is_arg_tainted, desc = _check_expr_taint(child, self.tainted_vars, self.src_bytes)
                if is_arg_tainted:
                    t_var = ""
                    for v in self.tainted_vars:
                        if v in desc:
                            t_var = v
                            break
                    if not t_var and self.tainted_vars:
                        t_var = list(self.tainted_vars)[0]
                        
                    src_info = self.var_sources.get(t_var, {
                        "line": node.start_point[0] + 1, 
                        "desc": "unknown", 
                        "path": [t_var or "unknown"]
                    })
                    
                    if is_sink:
                        self.detected_flows.append({
                            "var_name": t_var or "unknown",
                            "source_line": src_info["line"],
                            "source_desc": src_info["desc"],
                            "sink_line": node.start_point[0] + 1,
                            "sink_name": func_text,
                            "flow_path": src_info["path"] + [f"{func_text}() (L{node.start_point[0] + 1})"],
                            "code_snippet": self.lines[node.start_point[0]].strip() if 0 <= node.start_point[0] < len(self.lines) else ""
                        })
                    else:
                        # Outgoing call
                        self.outgoing_calls.append({
                            "func_name": func_text,
                            "arg_index": idx,
                            "arg_name": None,
                            "var_name": t_var or "unknown",
                            "path": src_info["path"]
                        })

        for child in node.children:
            self._walk(child)

def check_go_taint_interprocedural(source_code: str, external_sources: Set[str] | None = None) -> Dict:
    tracer = GoASTTaintTracer(source_code, external_sources)
    return tracer.trace()
