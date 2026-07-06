"""
parsers/go_parser.py
──────────────────────────────
Parser Go con doppia interfaccia:

1. SCANNER (parse):
   parse(file_path) -> ParseResult
   Estrae simboli e import da un file .go.
   Chiamato dallo scanner per popolare il DB.

2. WORKER (operazioni deterministiche):
   collect_definitions, extract_symbol, remove_symbols,
   inject_import, format_import, expand_entities
   Chiamato dal worker per operazioni chirurgiche sul codice.

Dipende da: tree-sitter-languages
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

def _load_go_parser():
    """
    Carica Parser e Language tree-sitter per Go.
    Strategia multi-tentativo per compatibilità massima tra versioni e OS.
    """
    last_error = None

    # ── Tentativo 1: API semplice (Windows, tree-sitter < 0.22) ──────────────
    try:
        from tree_sitter_languages import get_parser as _get_parser
        parser = _get_parser("go")
        test = parser.parse(b"package main")
        if test.root_node:
            return parser, None
    except Exception as e:
        last_error = e

    # ── Tentativo 2: ctypes + Language(capsule) (Linux/Windows, tree-sitter >= 0.22) ─
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
        func = getattr(lib, "tree_sitter_go", None)
        if func is None:
            raise ImportError("Symbol tree_sitter_go not found")
        func.restype = ctypes.c_void_p

        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            lang = Language(func())

        parser = TSParser(lang)
        test = parser.parse(b"package main")
        if test.root_node:
            return parser, lang
    except Exception as e:
        last_error = e

    # ── Tentativo 3: tree-sitter-go standalone se installato ──────────────────
    try:
        import tree_sitter_go
        from tree_sitter import Language, Parser as TSParser
        lang = Language(tree_sitter_go.language_go())
        parser = TSParser(lang)
        return parser, lang
    except Exception as e:
        last_error = e

    raise ImportError(
        f"Impossibile caricare il parser Go. "
        f"Ultimo errore: {last_error}\n"
        f"Esegui: pip install tree-sitter-languages"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Helper functions (stateless)
# ─────────────────────────────────────────────────────────────────────────────

def _node_text(node, src_bytes: bytes) -> str:
    return src_bytes[node.start_byte:node.end_byte].decode("utf-8")


def _get_receiver_type(receiver_node, src_bytes: bytes) -> Optional[str]:
    """
    Risolve il tipo del receiver per una dichiarazione di metodo.
    Esempio: '(s *MyStruct)' -> 'MyStruct'
    """
    for child in receiver_node.children:
        if child.type == "parameter_declaration":
            type_n = child.child_by_field_name("type")
            if type_n:
                if type_n.type == "pointer_type":
                    inner = type_n.child_by_field_name("type") or type_n.children[-1]
                    return _node_text(inner, src_bytes)
                else:
                    return _node_text(type_n, src_bytes)
    return None


def _get_definition_name_kind_parent(node, src_bytes: bytes) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Ritorna (name, kind, parent_class) per un nodo top-level, o (None, None, None).
    """
    t = node.type

    if t == "type_declaration":
        # type_declaration contiene type_spec
        for child in node.children:
            if child.type == "type_spec":
                name_node = child.child_by_field_name("name")
                type_node = child.child_by_field_name("type")
                if name_node and type_node:
                    name = _node_text(name_node, src_bytes)
                    kind = "class" if type_node.type == "struct_type" else "interface" if type_node.type == "interface_type" else "type"
                    return name, kind, None

    elif t == "function_declaration":
        name_node = node.child_by_field_name("name")
        if name_node:
            return _node_text(name_node, src_bytes), "function", None

    elif t == "method_declaration":
        name_node = node.child_by_field_name("name")
        receiver_node = node.child_by_field_name("receiver")
        if name_node:
            name = _node_text(name_node, src_bytes)
            parent = _get_receiver_type(receiver_node, src_bytes) if receiver_node else None
            return name, "method", parent

    elif t in ("const_declaration", "var_declaration"):
        # prendi il primo identificatore dichiarato come nome del simbolo
        for child in node.children:
            if child.type in ("const_spec", "var_spec"):
                for spec_child in child.children:
                    if spec_child.type == "identifier":
                        return _node_text(spec_child, src_bytes), "const" if t == "const_declaration" else "variable", None

    return None, None, None


def _extract_imports(root_node, src_bytes: bytes) -> List[ImportRecord]:
    imports = []
    def walk(node):
        if node.type == "import_spec":
            path_node = node.child_by_field_name("path")
            alias_node = node.child_by_field_name("name")
            if path_node:
                path_val = _node_text(path_node, src_bytes).strip('"\'')
                alias_val = _node_text(alias_node, src_bytes) if alias_node else None
                imports.append(ImportRecord(module=path_val, alias=alias_val))
        for child in node.children:
            walk(child)
    walk(root_node)
    return imports


def _find_named_node(root_node, src_bytes: bytes, name: str):
    """Cerca un nodo con il nome dato a livello top-level."""
    for node in root_node.children:
        found, _, _ = _get_definition_name_kind_parent(node, src_bytes)
        if found == name:
            return node
    return None


class GoParser:
    """
    Parser deterministico Go con doppia interfaccia:
    - Scanner: parse(file_path) -> ParseResult
    - Worker:  collect_definitions, extract_symbol, remove_symbols, ecc.
    """

    CONTENT_PREVIEW_LINES = 30

    def __init__(self):
        self._go_parser = None
        self._language = None

    def _ensure_loaded(self):
        if self._go_parser is None:
            self._go_parser, self._language = _load_go_parser()

    def _parse(self, source: str):
        self._ensure_loaded()
        return self._go_parser.parse(bytes(source, "utf-8"))

    # =========================================================================
    # INTERFACCIA SCANNER — parse(file_path) -> ParseResult
    # =========================================================================

    def parse(self, file_path) -> ParseResult:
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
        imports: List[ImportRecord] = _extract_imports(tree.root_node, src_bytes)
        seen_names: Set[str] = set()

        for node in tree.root_node.children:
            name, kind, parent = _get_definition_name_kind_parent(node, src_bytes)
            if not name:
                continue

            line_start = node.start_point[0] + 1
            line_end = node.end_point[0] + 1

            if name not in seen_names:
                symbols.append(SymbolRecord(
                    name=name,
                    kind=kind,
                    line_start=line_start,
                    line_end=line_end,
                    docstring=None,
                    parent_class=parent,
                ))
                seen_names.add(name)

        preview_lines = lines[:self.CONTENT_PREVIEW_LINES]
        content_preview = "\n".join(preview_lines)

        return ParseResult(
            symbols=symbols,
            imports=imports,
            config_keys=[],
            docstring="",
            content_preview=content_preview,
            lines_count=len(lines),
            is_generated=False,
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
            name, kind, _ = _get_definition_name_kind_parent(node, src_bytes)
            if name and name not in seen:
                if kind == "method" and not include_methods:
                    continue
                names.append(name)
                seen.add(name)

        return names

    def extract_imports(self, source: str) -> str:
        if not source.strip():
            return ""
        src_bytes = source.encode("utf-8")
        tree = self._parse(source)
        import_lines = []
        for n in tree.root_node.children:
            if n.type == "import_declaration":
                import_lines.append(_node_text(n, src_bytes))
        return "\n".join(import_lines)

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
        # standardizza l'import
        import_stmt = import_stmt.strip()
        if not import_stmt:
            return source

        for line in source.splitlines():
            if line.strip() == import_stmt:
                return source

        if not source.strip():
            return import_stmt + "\n"

        src_bytes = source.encode("utf-8")
        tree = self._parse(source)

        last_import_end_byte: Optional[int] = None
        package_end_byte: Optional[int] = None

        for node in tree.root_node.children:
            if node.type == "package_clause":
                package_end_byte = node.end_byte
            elif node.type == "import_declaration":
                last_import_end_byte = node.end_byte

        # Se ci sono già import, inserisci dopo l'ultimo
        if last_import_end_byte is not None:
            insert_pos = last_import_end_byte
            if insert_pos < len(src_bytes) and src_bytes[insert_pos:insert_pos + 1] == b"\n":
                insert_pos += 1
            result = (
                src_bytes[:insert_pos]
                + (import_stmt + "\n").encode("utf-8")
                + src_bytes[insert_pos:]
            )
            return result.decode("utf-8")

        # Se non ci sono import, inserisci subito dopo la clausola package
        if package_end_byte is not None:
            insert_pos = package_end_byte
            if insert_pos < len(src_bytes) and src_bytes[insert_pos:insert_pos + 1] == b"\n":
                insert_pos += 1
            result = (
                src_bytes[:insert_pos]
                + ("\n" + import_stmt + "\n").encode("utf-8")
                + src_bytes[insert_pos:]
            )
            return result.decode("utf-8")

        # Fallback altrimenti
        return import_stmt + "\n\n" + source

    def format_import(self, entities: List[str], source_module: str, target_module: str) -> str:
        # Go imports match package paths, no entities destructured
        return f'import "{target_module}"'

    def collect_unresolved_identifiers(self, source: str) -> set:
        """Ritorna identificatori sconosciuti usati nel file."""
        if not source.strip():
            return set()

        src_bytes = source.encode("utf-8")
        tree = self._parse(source)

        local_defs = set(self.collect_definitions(source, include_methods=False))
        imported_aliases: Set[str] = set()
        
        # Raccogli alias dagli import
        for r in _extract_imports(tree.root_node, src_bytes):
            if r.alias:
                imported_aliases.add(r.alias)
            else:
                # prendi l'ultima parte dell'import come alias implicito (es: "math/rand" -> "rand")
                pkg_name = r.module.split("/")[-1]
                imported_aliases.add(pkg_name)

        used_idents: Set[str] = set()
        
        def walk(node):
            if node.type == "import_declaration":
                return
            if node.type == "identifier":
                name = _node_text(node, src_bytes)
                if name:
                    used_idents.add(name)
            for child in node.children:
                walk(child)

        walk(tree.root_node)

        # Go built-in identifiers
        BUILTINS = {
            "append", "cap", "close", "complex", "copy", "delete", "imag", "len",
            "make", "new", "panic", "print", "println", "real", "recover", "nil",
            "true", "false", "iota", "int", "int8", "int16", "int32", "int64",
            "uint", "uint8", "uint16", "uint32", "uint64", "uintptr", "float32",
            "float64", "complex64", "complex128", "bool", "byte", "rune", "string",
            "error",
        }

        unresolved = (
            used_idents
            - local_defs
            - imported_aliases
            - BUILTINS
        )
        return unresolved
