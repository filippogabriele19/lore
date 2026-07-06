from dataclasses import dataclass, field
from typing import Optional

@dataclass
class SymbolInfo:
    name:         str
    kind:         str          # function | class | method | variable
    line_start:   int
    line_end:     int
    signature:    str
    parent_class: Optional[str] = None
    calls:        list = field(default_factory=list)      # nomi chiamati
    reads_global: list = field(default_factory=list)      # variabili globali lette
    writes_global: list = field(default_factory=list)     # variabili globali scritte
    is_source:    int = 0
    role:         str = "source"   # source | test | config
