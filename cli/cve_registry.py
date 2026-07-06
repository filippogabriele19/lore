from __future__ import annotations

# Re-export CVE data and runner methods for backward compatibility
from cli.cve_data import _CVE_REGISTRY
from cli.cve_runner import (
    _compute_detection_score,
    _run_cve_retrospective,
    _build_cve_html,
    _serve_cve_ui,
)
