"""
parsers/typescript_parser.py
──────────────────────────────
Parser TypeScript/JavaScript con doppia interfaccia:

1. SCANNER (parse):
   parse(file_path) -> ParseResult
   Estrae simboli e import da un file .ts/.tsx/.js/.jsx.
   Chiamato dallo scanner per popolare il DB.

2. WORKER (operazioni deterministiche):
   collect_definitions, extract_symbol, remove_symbols,
   inject_import, format_import, expand_entities
   Chiamato dal worker per operazioni chirurgiche sul codice.

Dipende da: tree-sitter-languages
  pip install tree-sitter-languages
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import List, Optional, Set, Tuple

from .parse_result import ParseResult, SymbolRecord, ImportRecord


# ─────────────────────────────────────────────────────────────────────────────
# Lazy loader
# ─────────────────────────────────────────────────────────────────────────────

def _load_ts_parser():
    """
    Carica Parser e Language tree-sitter per TypeScript.

    Strategia multi-tentativo per compatibilità massima tra versioni e OS:

    1. API semplice tree-sitter-languages (funziona su Windows con tree-sitter < 0.22)
    2. API tree-sitter >= 0.22 con Language(capsule) via ctypes
       (funziona su Linux/Mac con tree-sitter-languages 1.x)
    3. Fallback package tree-sitter-typescript se installato separatamente

    Almeno uno dei tre deve funzionare — se tutti falliscono, lancia ImportError
    con istruzioni chiare.
    """
    last_error = None

    # ── Tentativo 1: API semplice (Windows, tree-sitter < 0.22) ──────────────
    try:
        from tree_sitter_languages import get_parser as _get_parser
        parser = _get_parser("typescript")
        # Verifica che parse() funzioni davvero prima di restituire
        test = parser.parse(b"const x = 1;")
        if test.root_node:
            return parser, None
    except Exception as e:
        last_error = e

    # ── Tentativo 2: ctypes + Language(capsule) (Linux, tree-sitter >= 0.22) ─
    try:
        import ctypes
        from tree_sitter import Language, Parser as TSParser
        import tree_sitter_languages as _tsl

        pkg_dir = os.path.dirname(_tsl.__file__)
        so_path = None
        for fname in os.listdir(pkg_dir):
            if fname.startswith("languages") and fname.endswith((".so", ".pyd", ".dll")):
                candidate = os.path.join(pkg_dir, fname)
                if os.path.exists(candidate):
                    so_path = candidate
                    break

        if so_path is None:
            raise ImportError(f"Compiled library not found in {pkg_dir}")

        lib = ctypes.cdll.LoadLibrary(so_path)
        func = getattr(lib, "tree_sitter_typescript", None)
        if func is None:
            raise ImportError("Symbol tree_sitter_typescript not found")
        func.restype = ctypes.c_void_p

        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            lang = Language(func())

        parser = TSParser(lang)
        test = parser.parse(b"const x = 1;")
        if test.root_node:
            return parser, lang
    except Exception as e:
        last_error = e

    # ── Tentativo 3: tree-sitter-typescript standalone ────────────────────────
    try:
        import tree_sitter_typescript
        from tree_sitter import Language, Parser as TSParser
        lang = Language(tree_sitter_typescript.language_typescript())
        parser = TSParser(lang)
        return parser, lang
    except Exception as e:
        last_error = e

    raise ImportError(
        f"Impossibile caricare il parser TypeScript. "
        f"Ultimo errore: {last_error}\n"
        f"Esegui: pip install tree-sitter-languages"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Costanti tree-sitter
# ─────────────────────────────────────────────────────────────────────────────

_NAMED_DEFINITION_TYPES = frozenset({
    "function_declaration",
    "class_declaration",
    "abstract_class_declaration",
    "generator_function_declaration",
    "function_signature",
})

_LEXICAL_DECLARATION = "lexical_declaration"
_VARIABLE_DECLARATOR = "variable_declarator"
_FUNCTION_VALUE_TYPES = frozenset({
    "arrow_function", "function", "function_expression", "generator_function",
})
_EXPORT_STATEMENT = "export_statement"
_IMPORT_STATEMENT = "import_statement"
_METHOD_DEFINITION = "method_definition"
_CLASS_BODY = "class_body"
_INTERFACE_DECLARATION = "interface_declaration"
_TYPE_ALIAS_DECLARATION = "type_alias_declaration"

# Tipi che mappano a kind="class"
_CLASS_KINDS = frozenset({
    "class_declaration", "abstract_class_declaration",
})


# ─────────────────────────────────────────────────────────────────────────────
# Helper functions (stateless)
# ─────────────────────────────────────────────────────────────────────────────

def _node_text(node, src_bytes: bytes) -> str:
    return src_bytes[node.start_byte:node.end_byte].decode("utf-8")

def _resolve_inner(node):
    """Scarta export_statement, ritorna il nodo definizione interno."""
    if node.type != _EXPORT_STATEMENT:
        return node
    for child in node.children:
        if (child.type in _NAMED_DEFINITION_TYPES
                or child.type == _LEXICAL_DECLARATION
                or child.type == _INTERFACE_DECLARATION
                or child.type == _TYPE_ALIAS_DECLARATION):
            return child
    return None

def _get_definition_name_and_kind(node, src_bytes: bytes):
    """
    Ritorna (name, kind) per un nodo top-level, o (None, None).
    kind ∈ {"class", "function", "interface", "type", "const"}
    """
    inner = _resolve_inner(node)
    if inner is None:
        return None, None

    t = inner.type

    if t in _CLASS_KINDS:
        name_node = inner.child_by_field_name("name")
        return (_node_text(name_node, src_bytes) if name_node else None, "class")

    if t == "function_declaration":
        name_node = inner.child_by_field_name("name")
        return (_node_text(name_node, src_bytes) if name_node else None, "function")

    if t in ("generator_function_declaration", "function_signature"):
        name_node = inner.child_by_field_name("name")
        return (_node_text(name_node, src_bytes) if name_node else None, "function")

    if t == _INTERFACE_DECLARATION:
        name_node = inner.child_by_field_name("name")
        return (_node_text(name_node, src_bytes) if name_node else None, "interface")

    if t == _TYPE_ALIAS_DECLARATION:
        name_node = inner.child_by_field_name("name")
        return (_node_text(name_node, src_bytes) if name_node else None, "type")

    if t == _LEXICAL_DECLARATION:
        for child in inner.children:
            if child.type == _VARIABLE_DECLARATOR:
                name_node = child.child_by_field_name("name")
                value_node = child.child_by_field_name("value")
                if name_node and value_node and value_node.type in _FUNCTION_VALUE_TYPES:
                    return _node_text(name_node, src_bytes), "function"
                elif name_node:
                    return _node_text(name_node, src_bytes), "const"

    return None, None

def _get_methods(class_node, src_bytes: bytes) -> List[Tuple[str, int, int]]:
    """
    Estrae (name, line_start, line_end) di tutti i metodi in un class_body.
    Esclude: constructor, getter/setter privati (_), metodi con #.
    """
    methods = []
    for child in class_node.children:
        if child.type == _CLASS_BODY:
            for member in child.children:
                if member.type == _METHOD_DEFINITION:
                    name_node = member.child_by_field_name("name")
                    if not name_node:
                        continue
                    name = _node_text(name_node, src_bytes)
                    # Escludi constructor e metodi privati con # (private fields TS)
                    if name == "constructor" or name.startswith("#"):
                        continue
                    line_start = member.start_point[0] + 1  # 1-indexed
                    line_end = member.end_point[0] + 1
                    methods.append((name, line_start, line_end))
    return methods

def _find_named_node(root_node, src_bytes: bytes, name: str):
    """
    Cerca un nodo con il nome dato, sia a livello top-level che come
    metodo dentro un class body. Ritorna il nodo tree-sitter o None.
    """
    for node in root_node.children:
        # Top-level (class, function, const, interface, type)
        found, _ = _get_definition_name_and_kind(node, src_bytes)
        if found == name:
            return node

        # Method inside class body
        inner = _resolve_inner(node)
        if inner and inner.type in _CLASS_KINDS:
            body = inner.child_by_field_name("body")
            if body:
                for child in body.children:
                    if child.type == _METHOD_DEFINITION:
                        name_node = child.child_by_field_name("name")
                        if name_node and _node_text(name_node, src_bytes) == name:
                            return child

    return None

def _extract_import_module(node, src_bytes: bytes) -> Optional[str]:
    """
    Estrae il path/nome del modulo da un import_statement.
    'import { X } from "path"' → "path"
    """
    for child in node.children:
        if child.type == "string":
            text = _node_text(child, src_bytes)
            # Rimuovi virgolette
            return text.strip("'\"")
    return None

def _is_generated_file(source: str) -> bool:
    """Heuristica: file auto-generato da codegen."""
    first_lines = source[:500].lower()
    markers = [
        "do not edit", "auto-generated", "generated by",
        "this file was automatically generated",
        "@generated", "autogenerated",
    ]
    return any(m in first_lines for m in markers)

def _compute_relative_import_path(source_module: str, target_module: str) -> str:
    src_dir = Path(source_module).parent
    target_path = Path(target_module).with_suffix("")
    try:
        rel = os.path.relpath(target_path, src_dir)
    except ValueError:
        rel = str(target_path)
    rel = rel.replace("\\", "/")
    if not rel.startswith("."):
        rel = "./" + rel
    return rel

# ─────────────────────────────────────────────────────────────────────────────
# TypeScriptParser
# ─────────────────────────────────────────────────────────────────────────────

class TypeScriptParser:
    """
    Parser deterministico TypeScript/JavaScript con doppia interfaccia:
    - Scanner: parse(file_path) → ParseResult
    - Worker:  collect_definitions, extract_symbol, remove_symbols, ecc.
    """

    CONTENT_PREVIEW_LINES = 30

    def __init__(self):
        self._ts_parser = None
        self._language = None

    def _ensure_loaded(self):
        if self._ts_parser is None:
            self._ts_parser, self._language = _load_ts_parser()

    def _parse(self, source: str):
        self._ensure_loaded()
        return self._ts_parser.parse(bytes(source, "utf-8"))

    # =========================================================================
    # INTERFACCIA SCANNER — parse(file_path) → ParseResult
    # =========================================================================

    def parse(self, file_path) -> ParseResult:
        """
        Entry point per lo scanner. Legge il file e restituisce ParseResult.

        Estrae per ogni file TypeScript/NestJS:
          - Classi (kind="class")
          - Metodi di classe (kind="method") — CRITICO per detected_entities
          - Funzioni top-level (kind="function")
          - Interface (kind="interface")
          - Import statements

        Progetto NestJS tipico (es. article.service.ts):
          symbols = [
            SymbolRecord("ArticleService", "class",     14, 220),
            SymbolRecord("findAll",        "method",    20,  55),
            SymbolRecord("findFeed",       "method",    57,  80),
            SymbolRecord("findOne",        "method",    82,  85),
            SymbolRecord("create",         "method",   140, 175),
            SymbolRecord("update",         "method",   177, 195),
            SymbolRecord("delete",         "method",   197, 215),
            ...
          ]
        """
        path = Path(file_path)
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return ParseResult.empty()

        if not source.strip():
            return ParseResult.empty()

        lines = source.splitlines()
        src_bytes = source.encode("utf-8")

        try:
            tree = self._parse(source)
        except Exception:
            return ParseResult.empty()

        symbols: List[SymbolRecord] = []
        imports: List[ImportRecord] = []
        seen_names: Set[str] = set()

        for node in tree.root_node.children:

            # ── Import statements ──────────────────────────────────────────
            if node.type == _IMPORT_STATEMENT:
                module = _extract_import_module(node, src_bytes)
                if module:
                    imports.append(ImportRecord(module=module))
                continue

            # ── Top-level definitions ──────────────────────────────────────
            name, kind = _get_definition_name_and_kind(node, src_bytes)
            if not name:
                continue

            line_start = node.start_point[0] + 1  # 1-indexed
            line_end = node.end_point[0] + 1

            if name not in seen_names:
                symbols.append(SymbolRecord(
                    name=name,
                    kind=kind,
                    line_start=line_start,
                    line_end=line_end,
                    docstring=None,
                ))
                seen_names.add(name)

            # ── Metodi di classe (CRITICO per il planner) ─────────────────
            inner = _resolve_inner(node)
            if inner and inner.type in _CLASS_KINDS:
                for method_name, m_start, m_end in _get_methods(inner, src_bytes):
                    if method_name not in seen_names:
                        symbols.append(SymbolRecord(
                            name=method_name,
                            kind="method",
                            line_start=m_start,
                            line_end=m_end,
                            docstring=None,
                        ))
                        seen_names.add(method_name)

        # ── Content preview ────────────────────────────────────────────────
        preview_lines = lines[:self.CONTENT_PREVIEW_LINES]
        content_preview = "\n".join(preview_lines)

        return ParseResult(
            symbols=symbols,
            imports=imports,
            config_keys=[],
            docstring="",
            content_preview=content_preview,
            lines_count=len(lines),
            is_generated=_is_generated_file(source),
        )

    # =========================================================================
    # INTERFACCIA WORKER — operazioni deterministiche sul testo sorgente
    # =========================================================================

    def collect_definitions(self, source: str, include_methods: bool = False) -> List[str]:
        if not source.strip():
            return []
        src_bytes = source.encode("utf-8")
        tree = self._parse(source)
        names: List[str] = []
        seen: Set[str] = set()

        for node in tree.root_node.children:
            name, _ = _get_definition_name_and_kind(node, src_bytes)
            if name and name not in seen:
                names.append(name)
                seen.add(name)

            if include_methods:
                inner = _resolve_inner(node)
                if inner and inner.type in _CLASS_KINDS:
                    for method_name, _, _ in _get_methods(inner, src_bytes):
                        if method_name not in seen:
                            names.append(method_name)
                            seen.add(method_name)

        return names

    def extract_imports(self, source: str) -> str:
        if not source.strip():
            return ""
        src_bytes = source.encode("utf-8")
        tree = self._parse(source)
        return "\n".join(
            _node_text(n, src_bytes)
            for n in tree.root_node.children
            if n.type == _IMPORT_STATEMENT
        )

    def extract_symbol(self, source: str, name: str) -> Optional[str]:
        if not source.strip():
            return None
        src_bytes = source.encode("utf-8")
        tree = self._parse(source)
        node = _find_named_node(tree.root_node, src_bytes, name)
        return _node_text(node, src_bytes) if node else None

    def expand_entities(self, source: str, names: List[str]) -> List[str]:
        return list(names)

    def remove_symbols(self, source: str, names: Set[str]) -> str:
        if not source.strip() or not names:
            return source
        src_bytes = source.encode("utf-8")
        tree = self._parse(source)
        lines = source.splitlines(keepends=True)

        ranges_to_remove: List[Tuple[int, int]] = []
        for name in names:
            node = _find_named_node(tree.root_node, src_bytes, name)
            if node:
                ranges_to_remove.append((node.start_point[0], node.end_point[0]))

        if not ranges_to_remove:
            return source

        lines_to_remove: Set[int] = set()
        for start_line, end_line in ranges_to_remove:
            for i in range(start_line, end_line + 1):
                lines_to_remove.add(i)
            i = start_line - 1
            while i >= 0 and lines[i].strip() == "":
                lines_to_remove.add(i)
                i -= 1

        result = "".join(line for i, line in enumerate(lines) if i not in lines_to_remove)
        return re.sub(r"\n{3,}", "\n\n", result)

    def inject_import(self, source: str, import_stmt: str) -> str:
        import_normalized = import_stmt.rstrip(";").strip()
        for line in source.splitlines():
            if line.rstrip(";").strip() == import_normalized:
                return source

        if not source.strip():
            return import_stmt + "\n"

        src_bytes = source.encode("utf-8")
        tree = self._parse(source)

        last_import_end_byte: Optional[int] = None
        for node in tree.root_node.children:
            if node.type == _IMPORT_STATEMENT:
                last_import_end_byte = node.end_byte

        if last_import_end_byte is None:
            return import_stmt + "\n" + source

        insert_pos = last_import_end_byte
        if insert_pos < len(src_bytes) and src_bytes[insert_pos:insert_pos + 1] == b"\n":
            insert_pos += 1

        result = (
            src_bytes[:insert_pos]
            + (import_stmt + "\n").encode("utf-8")
            + src_bytes[insert_pos:]
        )
        return result.decode("utf-8")

    def format_import(self, entities: List[str], source_module: str, target_module: str) -> str:
        rel_path = _compute_relative_import_path(source_module, target_module)
        named = ", ".join(entities)
        return f"import {{ {named} }} from '{rel_path}';"

    def collect_unresolved_identifiers(self, source: str) -> set:
        """
        Trova identificatori che iniziano con maiuscola (classi/tipi NestJS)
        usati nel file ma non dichiarati localmente e non già importati.
        """
        if not source.strip():
            return set()

        src_bytes = source.encode("utf-8")
        tree = self._parse(source)

        # 1. Raccogli nomi già dichiarati localmente
        local_defs = set(self.collect_definitions(source, include_methods=False))

        # 2. Raccogli nomi già importati
        imported_names: set = set()
        for node in tree.root_node.children:
            if node.type == _IMPORT_STATEMENT:
                # Cerca import_clause -> named_imports -> import_specifier
                for child in node.children:
                    if child.type in ("import_clause", "named_imports"):
                        self._collect_import_names(child, src_bytes, imported_names)

        # 3. Raccogli tutti gli identifier con maiuscola usati nel file
        used_identifiers: set = set()
        self._walk_identifiers(tree.root_node, src_bytes, used_identifiers)

        # Blacklist: built-in JS/TS globals che non vanno importati
        BLACKLIST = {
            "Promise", "Error", "Array", "Object", "Map", "Set", "Date",
            "String", "Number", "Boolean", "undefined", "null", "JSON",
            "Math", "console", "Buffer", "Symbol", "RegExp", "Function",
            "Injectable", "Controller", "Module", "Get", "Post", "Put",
            "Delete", "Patch", "Body", "Param", "Query", "Headers",
            "Request", "Response", "UseGuards", "UsePipes", "UseInterceptors",
            "ApiBearerAuth", "ApiTags", "ApiOperation", "ApiResponse",
            "Optional", "Inject", "Type", "Abstract",
        }

        unresolved = (
            used_identifiers
            - local_defs
            - imported_names
            - BLACKLIST
        )
        return unresolved

    def _collect_import_names(self, node, src_bytes: bytes, result: set):
        """Ricorsivo: raccoglie tutti gli identifier dentro un import clause."""
        for child in node.children:
            if child.type == "identifier":
                result.add(_node_text(child, src_bytes))
            elif child.type == "import_specifier":
                # import { Foo as Bar } — prendi Bar (il nome locale)
                alias = child.child_by_field_name("alias")
                name = child.child_by_field_name("name")
                local = alias if alias else name
                if local:
                    result.add(_node_text(local, src_bytes))
            else:
                self._collect_import_names(child, src_bytes, result)

    def _walk_identifiers(self, node, src_bytes: bytes, result: set):
        """Walk dell'AST: raccoglie identifier che iniziano con maiuscola."""
        if node.type == _IMPORT_STATEMENT:
            return  # Salta — gli import li processiamo separatamente
        if node.type == "identifier" or node.type == "type_identifier":
            name = _node_text(node, src_bytes)
            if name and name[0].isupper():
                result.add(name)
        for child in node.children:
            self._walk_identifiers(child, src_bytes, result)