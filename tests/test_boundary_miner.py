import pytest
from pathlib import Path
from core.boundary_miner import check_boundary_mutations

def test_check_boundary_mutations_python(tmp_path):
    py_file = tmp_path / "validator.py"
    py_file.write_text("def validate(val):\n    if val > 0:\n        return True\n", encoding="utf-8")
    
    diff_text = """--- a/validator.py
+++ b/validator.py
@@ -2,2 +2,2 @@
-    if val > 0:
+    if val >= 0:
"""
    
    alerts = check_boundary_mutations(py_file, diff_text)
    assert len(alerts) == 1
    assert alerts[0]["old_op"] == ">"
    assert alerts[0]["new_op"] == ">="
    assert "Strict inequality '>' weakened to '>='" in alerts[0]["msg"]

def test_check_boundary_mutations_no_change(tmp_path):
    py_file = tmp_path / "validator.py"
    py_file.write_text("def validate(val):\n    if val > 0:\n        return True\n", encoding="utf-8")
    
    diff_text = """--- a/validator.py
+++ b/validator.py
@@ -2,2 +2,2 @@
     # Added comment
-    if val > 0:
+    if val > 0:
"""
    alerts = check_boundary_mutations(py_file, diff_text)
    assert len(alerts) == 0
