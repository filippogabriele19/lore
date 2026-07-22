import json
from pathlib import Path
from core.symbol_db import SymbolDB
from cli.gh_check import _generate_sarif_report
from cli.feedback import _main_dismiss


def test_generate_sarif_report():
    sarif_str = _generate_sarif_report(
        changed_files=["sample.py"],
        hotspots=[],
        fragile_symbols=[{"file": "sample.py", "symbol": "foo", "score": 4}],
        sibling_warnings=[],
        symbol_co_change_warnings=[],
        co_change_warnings=[],
        invariant_alerts=[{"file": "sample.py", "msg": "Guard removed"}],
        has_test_coupling_warning=False,
    )

    sarif_obj = json.loads(sarif_str)
    assert sarif_obj["version"] == "2.1.0"
    assert len(sarif_obj["runs"]) == 1
    results = sarif_obj["runs"][0]["results"]
    assert len(results) == 2
    rule_ids = {r["ruleId"] for r in results}
    assert "LORE001" in rule_ids
    assert "LORE002" in rule_ids


def test_dismiss_finding(tmp_path):
    db_path = tmp_path / "lore.db"
    db = SymbolDB(db_path)
    try:
        db.dismiss_finding("invariant", "sample.py", "foo", "intentional refactoring")
        dismissed = db.get_dismissed_findings()
        assert ("invariant", "sample.py", "foo") in dismissed
    finally:
        db.close()


def test_dismiss_cli_command(tmp_path):
    db_path = tmp_path / ".lore" / "lore.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = SymbolDB(db_path)
    db.close()

    _main_dismiss(["--type", "sibling", "--file", "utils.py", "--symbol", "bar", "--project", str(tmp_path)])

    db_check = SymbolDB(db_path)
    try:
        dismissed = db_check.get_dismissed_findings()
        assert ("sibling", "utils.py", "bar") in dismissed
    finally:
        db_check.close()
