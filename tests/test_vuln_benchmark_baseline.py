"""
tests/test_vuln_benchmark_baseline.py
─────────────────────────────────────────────────────────────────────────────
Unit tests for LORE's real baseline CVE benchmark harness and temporal leakage sanitizer.
"""

from pathlib import Path
import pytest
from scripts.vuln_benchmark_baseline import (
    TemporalLeakageSanitizer,
    RealLoreBenchmarkEvaluator,
    BenchmarkItem,
    BUNDLED_CVE_BENCHMARK_SAMPLE,
)

def test_temporal_leakage_sanitizer():
    sanitizer = TemporalLeakageSanitizer()

    dirty_text = (
        "Fixes CVE-2023-28856 and GHSA-xxxx-yyyy-zzzz: Heap buffer overflow vulnerability "
        "and arbitrary code execution in Redis server. Fixes #1234."
    )
    sanitized, replacements = sanitizer.sanitize_text(dirty_text)

    assert replacements >= 4
    assert "CVE-2023-28856" not in sanitized
    assert "GHSA-xxxx-yyyy-zzzz" not in sanitized
    assert "vulnerability" not in sanitized.lower()
    assert "Fixes #1234" not in sanitized


def test_real_baseline_benchmark_evaluator():
    items = [BenchmarkItem(**it) for it in BUNDLED_CVE_BENCHMARK_SAMPLE if it["project"] == "redis"]
    project_roots = {"redis": Path("_scan_targets/redis")}

    evaluator = RealLoreBenchmarkEvaluator(items, project_roots)
    summary = evaluator.evaluate()

    assert summary["total_samples"] == 3
    assert 0.0 <= summary["recall_top1"] <= 1.0
    assert 0.0 <= summary["recall_top3"] <= 1.0
    assert 0.0 <= summary["recall_top5"] <= 1.0
    assert summary["avg_latency_ms"] > 10.0  # Real DB execution latency > 10ms
