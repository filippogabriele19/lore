import pytest
from core.ast_patcher import check_ast_taint, check_ast_taint_interprocedural

def test_local_taint_tracing():
    code = """
def test_func(request):
    user_input = request.GET['q']
    # sink
    eval(user_input)
"""
    flows = check_ast_taint(code)
    assert len(flows) == 1
    assert flows[0]["var_name"] == "user_input"
    assert flows[0]["sink_name"] == "eval"

def test_interprocedural_taint_tracing():
    code = """
def process_data(tainted_arg):
    # sink
    exec(tainted_arg)
"""
    # Traccia assumendo che 'tainted_arg' sia marcato come sorgente esterna
    res = check_ast_taint_interprocedural(code, external_sources={"tainted_arg"})
    flows = res["flows"]
    assert len(flows) == 1
    assert flows[0]["var_name"] == "tainted_arg"
    assert flows[0]["sink_name"] == "exec"

def test_return_value_taint_tracing():
    code_callee = """
def load_input(request):
    val = request.GET['x']
    return val
"""
    res_callee = check_ast_taint_interprocedural(code_callee)
    assert "load_input" in res_callee["tainted_returns"]

    code_caller = """
def handler(request):
    data = load_input(request)
    exec(data)
"""
    res_caller = check_ast_taint_interprocedural(code_caller, external_tainted_functions={"load_input"})
    flows = res_caller["flows"]
    assert len(flows) == 1
    assert flows[0]["var_name"] == "data"
    assert flows[0]["sink_name"] == "exec"

def test_ssa_light_conditional_taint():
    code = """
def test_func(request, cond):
    if cond:
        x = request.GET['q']
    else:
        x = "safe"
    eval(x)
"""
    flows = check_ast_taint(code)
    assert len(flows) == 1
    assert "conditional-taint" in flows[0]["source_desc"]

def test_ssa_light_try_except():
    code = """
def test_func(request):
    try:
        x = request.GET['q']
    except Exception:
        x = "fallback"
    eval(x)
"""
    flows = check_ast_taint(code)
    assert len(flows) == 1
    assert "conditional-taint" in flows[0]["source_desc"]

def test_alias_analysis_subscript():
    code = """
def test_func(request):
    data = {}
    data['user_input'] = request.GET['q']
    eval(data['user_input'])
"""
    flows = check_ast_taint(code)
    assert len(flows) == 1
    assert flows[0]["var_name"] == "data['user_input']"

def test_alias_analysis_attribute():
    code = """
def test_func(request):
    x.val = request.GET['q']
    eval(x.val)
"""
    flows = check_ast_taint(code)
    assert len(flows) == 1
    assert flows[0]["var_name"] == "x.val"

def test_alias_analysis_base_taint():
    code = """
def test_func(request):
    data = request.GET
    eval(data['any_key'])
"""
    flows = check_ast_taint(code)
    assert len(flows) == 1
    assert flows[0]["var_name"] == "data"


