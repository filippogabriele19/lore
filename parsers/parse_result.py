"""
parsers/parse_result.py
────────────────────────
Dataclass condivisa tra tutti i parser del modulo.

Il scanner chiama parser.parse(file_path) e si aspetta un oggetto
con questa struttura esatta. Tutti i parser (Python, TypeScript, Generic)
devono restituire un'istanza di ParseResult.

CONTRATTO con scanner.py:
  result.docstring        → str   (descrizione file-level, opzionale)
  result.content_preview  → str   (prime N righe, per UI)
  result.lines_count      → int
  result.is_generated     → bool  (file auto-generato da tool?)
  result.symbols          → List[SymbolRecord]
  result.imports          → List[ImportRecord]
  result.config_keys      → List[ConfigKeyRecord]  (per JSON/YAML/TOML)
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class SymbolRecord:
    """
    Un simbolo estratto dal file (classe, funzione, metodo).

    Il campo `kind` è il discriminatore usato dal planner per filtrare
    i simboli rilevanti. Valori standard:
      "class"     → class declaration (Python/TS)
      "method"    → metodo di istanza dentro una classe
      "function"  → funzione top-level
      "interface" → TypeScript interface
      "type"      → TypeScript type alias
      "const"     → costante esportata (arrow function, oggetto)
    """
    name: str
    kind: str
    line_start: int
    line_end: int
    docstring: Optional[str] = None
    parent_class: Optional[str] = None   # populated for methods and subclasses


@dataclass
class ImportRecord:
    """
    Una dipendenza importata dal file.

    module = path o nome del modulo (es. '@nestjs/common', './article.entity')
    alias  = nome locale se rinominato (es. 'import X as Y' → alias='Y')
    """
    module: str
    alias: Optional[str] = None


@dataclass
class ConfigKeyRecord:
    """
    Una chiave di configurazione (usata per JSON/YAML/TOML).
    Non rilevante per TypeScript/Python — lista vuota di default.
    """
    key_path: str
    value_type: Optional[str] = None


@dataclass
class ParseResult:
    """
    Risultato completo del parsing di un singolo file.
    Restituito da parser.parse(file_path).
    """
    symbols: List[SymbolRecord] = field(default_factory=list)
    imports: List[ImportRecord] = field(default_factory=list)
    config_keys: List[ConfigKeyRecord] = field(default_factory=list)
    docstring: str = ""
    content_preview: str = ""
    lines_count: int = 0
    is_generated: bool = False

    @staticmethod
    def empty() -> "ParseResult":
        """Risultato vuoto — usato come fallback in caso di errore."""
        return ParseResult()