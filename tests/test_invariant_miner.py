import ast
import tempfile
from pathlib import Path
from core.invariant_miner import (
    extract_guard_clauses,
    mine_sibling_conventions,
    check_guard_stability,
)


def test_extract_guard_clauses_isinstance():
    code = """
def sample_func(arg):
    if not isinstance(arg, str):
        raise TypeError("expected str")
    return arg.upper()
"""
    tree = ast.parse(code)
    fn_node = tree.body[0]
    guards = extract_guard_clauses(fn_node)
    assert len(guards) > 0
    assert any("isinstance" in g for g in guards)


def test_extract_guard_clauses_assert():
    code = """
def sample_func(x):
    assert x > 0, "x must be positive"
    return x * 2
"""
    tree = ast.parse(code)
    fn_node = tree.body[0]
    guards = extract_guard_clauses(fn_node)
    assert len(guards) > 0
    assert any("assert" in g for g in guards)


def test_mine_sibling_conventions(tmp_path):
    # Create a Python file where >70% of functions use isinstance guard, but 1 function deviates
    sample_py = tmp_path / "validators.py"
    sample_py.write_text(
        """
def validate_a(val):
    if not isinstance(val, str):
        raise TypeError()
    return val.strip()

def validate_b(val):
    if not isinstance(val, int):
        raise TypeError()
    return val * 2

def validate_c(val):
    if not isinstance(val, dict):
        raise TypeError()
    return list(val.keys())

def validate_d(val):
    # Missing guard!
    return val.upper()
""",
        encoding="utf-8",
    )

    warnings = mine_sibling_conventions(sample_py)
    assert len(warnings) > 0
    symbols_flagged = [w["symbol"] for w in warnings]
    assert "validate_d" in symbols_flagged


def test_check_guard_stability(tmp_path):
    dummy_file = tmp_path / "sample.py"
    dummy_file.write_text("def foo(x):\n    pass\n", encoding="utf-8")

    patch_diff = """--- a/sample.py
+++ b/sample.py
@@ -1,4 +1,3 @@
 def foo(x):
-    assert isinstance(x, str), "invalid type"
     return x.lower()
"""
    alerts = check_guard_stability(dummy_file, patch_diff)
    assert len(alerts) > 0
    assert any("isinstance" in a["guard"] or "assert" in a["guard"] for a in alerts)


def test_mine_go_sibling_conventions(tmp_path):
    sample_go = tmp_path / "handlers.go"
    sample_go.write_text(
        """package main
import "errors"

func HandleA(req string) error {
    if req == "" { return errors.New("empty") }
    return nil
}

func HandleB(req string) error {
    if req == "" { return errors.New("empty") }
    return nil
}

func HandleC(req string) error {
    if req == "" { return errors.New("empty") }
    return nil
}

func HandleD(req string) error {
    // Missing guard
    return nil
}
""",
        encoding="utf-8",
    )

    warnings = mine_sibling_conventions(sample_go)
    assert len(warnings) > 0
    flagged = [w["symbol"] for w in warnings]
    assert "HandleD" in flagged


def test_mine_ts_sibling_conventions(tmp_path):
    sample_ts = tmp_path / "service.ts"
    sample_ts.write_text(
        """
export function processA(val: any) {
    if (!val) throw new Error("invalid");
    return val;
}

export function processB(val: any) {
    if (!val) throw new Error("invalid");
    return val;
}

export function processC(val: any) {
    if (!val) throw new Error("invalid");
    return val;
}

export function processD(val: any) {
    return val;
}
""",
        encoding="utf-8",
    )

    warnings = mine_sibling_conventions(sample_ts)
    assert len(warnings) > 0
    flagged = [w["symbol"] for w in warnings]
    assert "processD" in flagged

