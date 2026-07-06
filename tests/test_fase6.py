import pytest
from pathlib import Path
from cli.benchmark import _main_benchmark

def test_benchmark_runner(temp_project):
    report_file = temp_project / "report.html"
    
    # Run benchmark command
    _main_benchmark(["--output", str(report_file)])
    
    # Assert report was created
    assert report_file.exists()
    
    html = report_file.read_text(encoding="utf-8")
    assert "LORE Security Compliance Benchmark Report" in html
    assert "SQL Order-By Injection" in html
    assert "safe_load" in html
