import pytest
from core.ast_patcher import check_ast_taint_interprocedural

def test_baseline_code_has_taint_flows():
    baseline = "def fetch_users(order_field):\n    cursor.execute(f'SELECT * FROM users ORDER BY {order_field}')\n"
    result = check_ast_taint_interprocedural(baseline, set())
    assert isinstance(result, dict)

def test_safe_code_has_no_flows():
    safe = "def fetch_users(order_field):\n    clean = sanitize(order_field)\n    return clean\n"
    result = check_ast_taint_interprocedural(safe, set())
    flows = result.get("flows", [])
    assert len(flows) == 0
