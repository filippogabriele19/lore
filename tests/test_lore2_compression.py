"""
tests/test_lore2_compression.py
──────────────────────────────────
Unit tests for LORE 2.0 Context Compression suite:
1. AST Skeletonization (parsers/ast_skeleton.py)
2. BPE-Friendly Graph DSL (core/dsl_serializer.py)
3. Dynamic Co-Change Sparsification (core/cochange_sparsifier.py)
4. Syntax & Comment Pruner (core/comment_pruner.py)
5. LoD Graph Builder & Prompt Caching Layout (core/lod_graph_builder.py)
"""

import pytest
from parsers.ast_skeleton import skeletonize_python, skeletonize_generic
from core.dsl_serializer import serialize_symbol_dsl
from core.cochange_sparsifier import filter_co_changes_by_fragility
from core.comment_pruner import prune_code_context
from core.lod_graph_builder import LoDGraphBuilder


def test_ast_skeletonization():
    code = """
def calculate_tax(user_id: str, amount: float) -> float:
    \"\"\"Calculates sales tax for user.\"\"\"
    tax_rate = 0.22
    logger.info("Calculating tax")
    return amount * tax_rate
"""
    skel = skeletonize_python(code)
    assert "def calculate_tax" in skel
    assert "..." in skel
    assert "tax_rate = 0.22" not in skel


def test_bpe_graph_dsl_serializer():
    dsl = serialize_symbol_dsl(
        symbol_name="process_order",
        kind="function",
        file_path="orders/processor.py",
        line_start=10,
        line_end=50,
        callers=[{"file": "api/views.py", "caller": "checkout"}],
        co_changes=[{"file": "billing/invoice.py", "count": 12}],
        adrs=[{"source_ref": "ADR-005", "description": "Idempotent payment tokens"}],
        fragility_score=0.85
    )
    assert "@SYM:processor.py#process_order" in dsl
    assert "↳ CALLERS:[views.py#checkout]" in dsl
    assert "↳ CO_CHG:[invoice.py(n:12)]" in dsl
    assert "↳ RULE(L4):ADR-005" in dsl


def test_cochange_sparsification():
    co_changes = [
        {"file": f"file_{i}.py", "count": i * 2} for i in range(10)
    ]
    # Low fragility (< 0.3) -> 1 rule
    low = filter_co_changes_by_fragility(co_changes, fragility_score=0.1)
    assert len(low) == 1

    # Medium fragility (0.5) -> 3 rules
    med = filter_co_changes_by_fragility(co_changes, fragility_score=0.5)
    assert len(med) == 3

    # High fragility (0.9) -> 6 rules
    high = filter_co_changes_by_fragility(co_changes, fragility_score=0.9)
    assert len(high) == 6


def test_syntax_and_comment_pruning():
    code = """# Temporary debug line
def execute_trade():
    # [ADR-002] Critical risk check mandatory
    risk = check_risk()  # trailing debug comment
    return risk
"""
    pruned = prune_code_context(code, language="python")
    assert "# Temporary debug line" not in pruned
    assert "# [ADR-002]" in pruned
    assert "def execute_trade():" in pruned


def test_lod_graph_builder_prompt_caching():
    builder = LoDGraphBuilder()
    output = builder.build_lod_context(
        focal_symbol="authenticate",
        focal_code="def authenticate(token):\n    # debug\n    return True\n",
        file_path="auth/verifier.py",
        dependencies=[{"name": "validate_jwt", "body": "def validate_jwt(t: str) -> bool:\n    return True\n"}],
        co_changes=[{"file": "audit.py", "count": 5}],
        adrs=[{"source_ref": "ADR-001", "description": "Opaque tokens required"}],
        fragility_score=0.4
    )
    assert "[LORE_STATIC_GRAPH_CACHE_BLOCK]" in output
    assert "[LORE_DYNAMIC_DELTA_BLOCK]" in output
    assert "@SYM:verifier.py#authenticate" in output
    assert "# Dependency Stub: validate_jwt" in output
    assert "def validate_jwt" in output
    assert "..." in output
