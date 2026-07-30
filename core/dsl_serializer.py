"""
core/dsl_serializer.py
───────────────────────
LORE 2.0 BPE-Friendly Graph DSL Serializer.
Replaces verbose JSON representations with ultra-dense, low-token single-character
BPE delimiters (@SYM, ↳ CALLERS, ↳ CO_CHG, ↳ RULE(L4), ↳ TAINT(L5)).
"""

from typing import Any, Dict, List, Optional


def serialize_symbol_dsl(
    symbol_name: str,
    kind: str,
    file_path: str,
    line_start: int,
    line_end: int,
    callers: Optional[List[Dict[str, Any]]] = None,
    dependencies: Optional[List[Dict[str, Any]]] = None,
    co_changes: Optional[List[Dict[str, Any]]] = None,
    adrs: Optional[List[Dict[str, Any]]] = None,
    taint_paths: Optional[List[str]] = None,
    fragility_score: Optional[float] = None
) -> str:
    """Format a single symbol and its 5-layer graph connections into Graph DSL."""
    file_basename = file_path.replace("\\", "/").split("/")[-1]
    header = f"@SYM:{file_basename}#{symbol_name} [{kind}, L{line_start}-{line_end}]"
    if fragility_score is not None:
        header += f" (fragility:{fragility_score:.2f})"
        
    lines = [header]

    if callers:
        caller_str = ", ".join(f"{c.get('file', '').split('/')[-1]}#{c.get('caller', 'anon')}" for c in callers[:5])
        lines.append(f"↳ CALLERS:[{caller_str}]")

    if dependencies:
        dep_str = ", ".join(f"{d.get('name', '')}" for d in dependencies[:5])
        lines.append(f"↳ DEPENDS:[{dep_str}]")

    if co_changes:
        co_str = ", ".join(f"{c.get('file', '').split('/')[-1]}(n:{c.get('count', 1)})" for c in co_changes[:5])
        lines.append(f"↳ CO_CHG:[{co_str}]")

    if adrs:
        for a in adrs:
            src = a.get('source_ref', 'ADR')
            desc = a.get('description', '')
            lines.append(f"↳ RULE(L4):{src}[{desc[:60]}]")

    if taint_paths:
        for tp in taint_paths[:3]:
            lines.append(f"↳ TAINT(L5):{tp}")

    return "\n".join(lines)


def format_graph_payload_dsl(graph_data: Dict[str, Any]) -> str:
    """Serialize a full Knowledge Graph query payload into Graph DSL format."""
    output_blocks = []
    
    symbols = graph_data.get("symbols", [])
    for sym in symbols:
        dsl_str = serialize_symbol_dsl(
            symbol_name=sym.get("name", ""),
            kind=sym.get("kind", "symbol"),
            file_path=sym.get("file", ""),
            line_start=sym.get("line_start", 1),
            line_end=sym.get("line_end", 1),
            callers=sym.get("called_by"),
            dependencies=sym.get("depends_on"),
            co_changes=sym.get("co_changes"),
            adrs=sym.get("adrs"),
            taint_paths=sym.get("taints"),
            fragility_score=sym.get("fragility")
        )
        output_blocks.append(dsl_str)
        
    return "\n\n".join(output_blocks)
