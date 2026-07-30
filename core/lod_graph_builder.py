"""
core/lod_graph_builder.py
───────────────────────────
LORE 2.0 Concentric Level of Detail (LoD) Graph Builder & Prompt Cache Layer.
Assembles the 5-layer Knowledge Graph into concentric resolution tiers:
- Tier 0 (Focal Node): Pruned full code or diff
- Tier 1 (1st Degree Dependencies): AST Skeleton stubs (.pyi / .d.ts)
- Tier 2 (Co-changes / ADRs): BPE-Friendly Graph DSL tags
- Tier 3 (Taint Traces): 1-line Source -> Sink path summaries

Structured into [LORE_STATIC_GRAPH_CACHE_BLOCK] and [LORE_DYNAMIC_DELTA_BLOCK]
for 90% Prompt Caching cost reduction.
"""

from typing import Any, Dict, List, Optional
from parsers.ast_skeleton import skeletonize_code
from core.dsl_serializer import serialize_symbol_dsl
from core.cochange_sparsifier import filter_co_changes_by_fragility
from core.comment_pruner import prune_code_context


class LoDGraphBuilder:
    """Concentric Level-of-Detail Knowledge Graph Context Builder."""

    def __init__(self, project_root_str: str = "."):
        self.project_root_str = project_root_str

    def build_lod_context(
        self,
        focal_symbol: str,
        focal_code: str,
        file_path: str,
        language: str = "python",
        dependencies: Optional[List[Dict[str, Any]]] = None,
        callers: Optional[List[Dict[str, Any]]] = None,
        co_changes: Optional[List[Dict[str, Any]]] = None,
        adrs: Optional[List[Dict[str, Any]]] = None,
        taint_paths: Optional[List[str]] = None,
        fragility_score: float = 0.5,
        user_diff: Optional[str] = None
    ) -> str:
        """Constructs a compressed, tiered LoD Knowledge Graph context block."""

        # 1. Tier 2: Graph DSL Tags (Co-changes & ADRs) with Sparsification
        filtered_co_changes = filter_co_changes_by_fragility(co_changes or [], fragility_score)

        dsl_metadata = serialize_symbol_dsl(
            symbol_name=focal_symbol,
            kind="symbol",
            file_path=file_path,
            line_start=1,
            line_end=len(focal_code.splitlines()) if focal_code else 1,
            callers=callers,
            dependencies=dependencies,
            co_changes=filtered_co_changes,
            adrs=adrs,
            taint_paths=taint_paths,
            fragility_score=fragility_score
        )

        # 2. Tier 1: AST Skeleton Stubs for 1st-Degree Dependencies & Callers
        skeleton_stubs = []
        if dependencies:
            for dep in dependencies[:3]:
                dep_code = dep.get("body") or dep.get("code", "")
                if dep_code:
                    skel = skeletonize_code(dep_code, language=language)
                    skeleton_stubs.append(f"# Dependency Stub: {dep.get('name', 'dep')}\n{skel}")

        if callers:
            for c in callers[:3]:
                c_code = c.get("body") or c.get("code", "")
                if c_code:
                    skel = skeletonize_code(c_code, language=language)
                    skeleton_stubs.append(f"# Caller Stub: {c.get('caller', 'caller')}\n{skel}")

        stubs_text = "\n\n".join(skeleton_stubs) if skeleton_stubs else ""

        # 3. Assemble Static Cache Block
        static_lines = [
            "[LORE_STATIC_GRAPH_CACHE_BLOCK]",
            "=== LORE KNOWLEDGE GRAPH TOPOLOGY & CONSTRAINTS ===",
            dsl_metadata
        ]
        if stubs_text:
            static_lines.extend(["", "--- DEPENDENCY & CALLER AST SKELETONS ---", stubs_text])
            
        static_block = "\n".join(static_lines)

        # 4. Tier 0: Focal Node Code & Diff (Comment Pruned)
        pruned_focal_code = prune_code_context(focal_code, language=language)
        
        dynamic_lines = [
            "[LORE_DYNAMIC_DELTA_BLOCK]",
            f"=== TARGET CODE CONTEXT ({file_path}) ===",
            pruned_focal_code
        ]
        if user_diff:
            dynamic_lines.extend(["", "--- ACTIVE USER DIFF ---", user_diff])

        dynamic_block = "\n".join(dynamic_lines)

        return f"{static_block}\n\n{dynamic_block}"
