"""
core/cochange_sparsifier.py
────────────────────────────
LORE 2.0 Dynamic Co-Change Sparsifier.
Filters co-change association rules dynamically based on symbol/file Fragility Score,
preventing defensive context clutter on stable, low-risk modules.
"""

from typing import Any, Dict, List


def filter_co_changes_by_fragility(
    co_changes: List[Dict[str, Any]],
    fragility_score: float = 0.5
) -> List[Dict[str, Any]]:
    """Sparsify co-change rules based on file/symbol fragility score.

    - Low Fragility (< 0.3): Max 1 rule (stable file, minimal risk)
    - Medium Fragility (0.3 - 0.7): Max 3 rules
    - High Fragility (>= 0.7): Max 6 rules (hotspot / bug-prone module)
    """
    if not co_changes:
        return []

    # Sort co-changes by association count descending
    sorted_co = sorted(co_changes, key=lambda x: x.get("count", 0), reverse=True)

    if fragility_score < 0.3:
        max_rules = 1
    elif fragility_score < 0.7:
        max_rules = 3
    else:
        max_rules = 6

    return sorted_co[:max_rules]
