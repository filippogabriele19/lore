import ast
import sys
import os
from pathlib import Path
from typing import Optional, Any

from core.symbol_types import SymbolInfo


class FileExtractor(ast.NodeVisitor):
    """Estrae simboli e dipendenze da un file Python usando ast."""

    def __init__(self, source_lines: list[str]):
        self.lines = source_lines
        self.symbols: list[SymbolInfo] = []
        self.imports: list[tuple] = []   # (name, module, line)
        self._module_globals: set[str] = set()
        self._current_class: Optional[str] = None

    # ------------------------------------------------------------------
    # Prima passata: raccoglie i nomi globali del modulo
    # ------------------------------------------------------------------

    def collect_globals(self, tree: ast.Module):
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name):
                        self._module_globals.add(t.id)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self._module_globals.add(node.name)

    # ------------------------------------------------------------------
    # Import
    # ------------------------------------------------------------------

    def visit_Import(self, node: ast.Import):
        if not self._inside_func:
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                self.imports.append((name, alias.name, node.lineno))
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        if not self._inside_func:
            module = node.module or ""
            for alias in node.names:
                name = alias.asname or alias.name
                self.imports.append((name, f"{module}.{alias.name}", node.lineno))
        self.generic_visit(node)

    # ------------------------------------------------------------------
    # Variabili di modulo
    # ------------------------------------------------------------------

    def visit_Assign(self, node: ast.Assign):
        # Solo a livello di modulo (non dentro funzioni/classi)
        if self._current_class is None and not self._inside_func:
            for t in node.targets:
                if isinstance(t, ast.Name):
                    sig = self._get_line(node.lineno)
                    is_src = self._check_node_is_source(node.value)
                    self.symbols.append(SymbolInfo(
                        name=t.id,
                        kind="variable",
                        line_start=node.lineno,
                        line_end=node.lineno,
                        signature=sig.strip(),
                        is_source=1 if is_src else 0,
                    ))
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign):
        if self._current_class is None and not self._inside_func:
            if isinstance(node.target, ast.Name):
                sig = self._get_line(node.lineno)
                is_src = node.value is not None and self._check_node_is_source(node.value)
                self.symbols.append(SymbolInfo(
                    name=node.target.id,
                    kind="variable",
                    line_start=node.lineno,
                    line_end=node.lineno,
                    signature=sig.strip(),
                    is_source=1 if is_src else 0,
                ))
        self.generic_visit(node)

    # ------------------------------------------------------------------
    # Classi
    # ------------------------------------------------------------------

    def visit_ClassDef(self, node: ast.ClassDef):
        sig = self._get_line(node.lineno)
        sym = SymbolInfo(
            name=node.name,
            kind="class",
            line_start=node.lineno,
            line_end=node.end_lineno or node.lineno,
            signature=sig.strip(),
        )
        self.symbols.append(sym)
        prev = self._current_class
        self._current_class = node.name
        self.generic_visit(node)
        self._current_class = prev

    # ------------------------------------------------------------------
    # Funzioni e metodi
    # ------------------------------------------------------------------

    _inside_func = False  # flag per bloccare visit_Assign a livello di modulo

    def _visit_funcdef(self, node):
        sig = self._get_line(node.lineno)
        sym = SymbolInfo(
            name=node.name,
            kind="method" if self._current_class else "function",
            line_start=node.lineno,
            line_end=node.end_lineno or node.lineno,
            signature=sig.strip(),
            parent_class=self._current_class,
        )

        # Analizza il corpo della funzione
        prev_flag = self._inside_func
        self._inside_func = True
        self._analyze_func_body(node, sym)

        self.symbols.append(sym)
        self.generic_visit(node)  # Visita ricorsivamente le funzioni/classi annidate!
        self._inside_func = prev_flag

    visit_FunctionDef = _visit_funcdef
    visit_AsyncFunctionDef = _visit_funcdef

    def _analyze_func_body(self, node, sym: SymbolInfo):
        """Raccoglie: chiamate, letture/scritture di variabili globali."""
        if self._check_node_is_source(node):
            sym.is_source = 1

        def walk_local(top_node):
            for child in ast.iter_child_nodes(top_node):
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                yield child
                yield from walk_local(child)

        # Nomi assegnati localmente nella funzione (parametri + assign)
        local_names: set[str] = {a.arg for a in node.args.args}
        for a in getattr(node.args, "kwonlyargs", []):
            local_names.add(a.arg)
        for a in getattr(node.args, "posonlyargs", []):
            local_names.add(a.arg)
        if node.args.vararg:
            local_names.add(node.args.vararg.arg)
        if node.args.kwarg:
            local_names.add(node.args.kwarg.arg)

        for child in walk_local(node):
            if isinstance(child, ast.Assign):
                for t in child.targets:
                    if isinstance(t, ast.Name):
                        local_names.add(t.id)
            elif isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name):
                local_names.add(child.target.id)
            elif isinstance(child, ast.Global):
                for name in child.names:
                    local_names.discard(name)

        for child in walk_local(node):
            # Chiamate a funzione
            if isinstance(child, ast.Call):
                fname = self._extract_call_name(child)
                if fname and fname not in sym.calls:
                    sym.calls.append(fname)

            # Lettura di variabile globale
            elif isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                if child.id in self._module_globals and child.id not in local_names:
                    if child.id not in sym.reads_global:
                        sym.reads_global.append(child.id)

            # Scrittura su variabile globale
            elif isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                if child.id in self._module_globals and child.id not in local_names:
                    if child.id not in sym.writes_global:
                        sym.writes_global.append(child.id)

    @staticmethod
    def _extract_call_name(node: ast.Call) -> Optional[str]:
        if isinstance(node.func, ast.Name):
            return node.func.id
        if isinstance(node.func, ast.Attribute):
            return node.func.attr
        return None

    def _check_node_is_source(self, node: ast.AST) -> bool:
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                name = self._extract_call_name(child)
                if name in ("input", "open", "read_text", "read", "getenv", "environ"):
                    return True
            elif isinstance(child, ast.Attribute):
                if isinstance(child.value, ast.Name):
                    if child.value.id == "sys" and child.attr == "argv":
                        return True
                    elif child.value.id == "os" and child.attr == "environ":
                        return True
                    elif child.value.id == "request":
                        return True
            elif isinstance(child, ast.Name):
                if child.id in ("argv", "environ", "request"):
                    return True
        return False

    def _get_line(self, lineno: int) -> str:
        if 1 <= lineno <= len(self.lines):
            return self.lines[lineno - 1]
        return ""


def extract_file(file_path: Path) -> tuple[list[SymbolInfo], list[tuple]]:
    """Parsa un file Python e ritorna (simboli, import)."""
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return [], []
    lines = source.splitlines()
    if "\x00" in source:
        return [], []
    try:
        tree = ast.parse(source, filename=str(file_path))
    except (SyntaxError, ValueError):
        return [], []
    extractor = FileExtractor(lines)
    extractor.collect_globals(tree)
    extractor.visit(tree)
    return extractor.symbols, extractor.imports


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------


def _extract_ts_file(fpath: Path) -> tuple[list[SymbolInfo], list[tuple]]:
    """
    Parse a TypeScript/JavaScript file via TypeScriptParser.
    Returns (symbols, imports) in the same format as extract_file().
    Falls back to ([], []) if tree-sitter is not installed.
    """
    try:
        from parsers.typescript_parser import TypeScriptParser
    except ImportError:
        return [], []

    try:
        result = TypeScriptParser().parse(fpath)
    except Exception:
        return [], []

    source_lines: list[str] = []
    try:
        source_lines = fpath.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        pass

    symbols: list[SymbolInfo] = []
    seen: set[str] = set()
    for rec in result.symbols:
        if rec.name in seen:
            continue
        seen.add(rec.name)
        # signature = first line of the symbol body
        sig = ""
        if source_lines and 1 <= rec.line_start <= len(source_lines):
            sig = source_lines[rec.line_start - 1].strip()[:200]
        symbols.append(SymbolInfo(
            name=rec.name,
            kind=rec.kind,
            line_start=rec.line_start,
            line_end=rec.line_end,
            signature=sig,
            parent_class=rec.parent_class,
        ))

    # imports as (name, module, line) — same tuple format used by insert_imports()
    imports: list[tuple] = []
    for imp in result.imports:
        imports.append((imp.alias or imp.module, imp.module, 0))

    # Extract calls inside each TS symbol's lines
    try:
        source_text = fpath.read_text(encoding="utf-8", errors="replace")
        if source_text:
            parser_ts = TypeScriptParser()
            tree = parser_ts._parse(source_text)
            src_bytes = source_text.encode("utf-8")
            
            all_calls = []
            def walk(node):
                if node.type == "call_expression":
                    all_calls.append(node)
                for child in node.children:
                    walk(child)
            walk(tree.root_node)
            
            for sym in symbols:
                for call in all_calls:
                    call_line = call.start_point[0] + 1
                    if sym.line_start <= call_line <= sym.line_end:
                        func_node = call.child_by_field_name("function") or call.children[0]
                        if func_node.type == "member_expression":
                            name_node = func_node.child_by_field_name("property") or func_node.children[-1]
                        else:
                            name_node = func_node
                        called_name = src_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8")
                        if called_name and called_name not in sym.calls:
                            sym.calls.append(called_name)
    except Exception:
        pass

    return symbols, imports


def _extract_go_file(fpath: Path) -> tuple[list[SymbolInfo], list[tuple]]:
    """
    Parse a Go file via GoParser.
    Returns (symbols, imports) in the same format as extract_file().
    Falls back to ([], []) if tree-sitter is not installed.
    """
    try:
        from parsers.go_parser import GoParser
    except ImportError:
        return [], []

    try:
        result = GoParser().parse(fpath)
    except Exception:
        return [], []

    source_lines: list[str] = []
    source_text = ""
    try:
        source_text = fpath.read_text(encoding="utf-8", errors="replace")
        source_lines = source_text.splitlines()
    except Exception:
        pass

    symbols: list[SymbolInfo] = []
    seen: set[str] = set()
    for rec in result.symbols:
        if rec.name in seen:
            continue
        seen.add(rec.name)
        sig = ""
        if source_lines and 1 <= rec.line_start <= len(source_lines):
            sig = source_lines[rec.line_start - 1].strip()[:200]
        symbols.append(SymbolInfo(
            name=rec.name,
            kind=rec.kind,
            line_start=rec.line_start,
            line_end=rec.line_end,
            signature=sig,
            parent_class=rec.parent_class,
        ))

    # imports as (name, module, line) — same tuple format used by insert_imports()
    imports: list[tuple] = []
    for imp in result.imports:
        alias_or_module = imp.alias or imp.module.split("/")[-1]
        imports.append((alias_or_module, imp.module, 0))

    # Extract calls inside each symbol's lines
    try:
        if source_text:
            parser = GoParser()
            tree = parser._parse(source_text)
            src_bytes = source_text.encode("utf-8")
            
            all_calls = []
            def walk(node):
                if node.type == "call_expression":
                    all_calls.append(node)
                for child in node.children:
                    walk(child)
            walk(tree.root_node)
            
            for sym in symbols:
                for call in all_calls:
                    call_line = call.start_point[0] + 1
                    if sym.line_start <= call_line <= sym.line_end:
                        func_node = call.child_by_field_name("function") or call.children[0]
                        if func_node.type == "selector_expression":
                            name_node = func_node.child_by_field_name("field") or func_node.children[-1]
                        else:
                            name_node = func_node
                        called_name = src_bytes[name_node.start_byte:name_node.end_byte].decode("utf-8")
                        if called_name and called_name not in sym.calls:
                            sym.calls.append(called_name)
    except Exception:
        pass

    return symbols, imports


