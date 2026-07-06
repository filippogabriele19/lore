"""
core/_batch_consolidator.py — Lambda Architecture: Batch Layer.

Speed Layer (existing): mine_last_commit() — incremental per-commit update.
Batch Layer (this module): run_batch_consolidation() — periodic full re-evaluation.

Detects "silent weakening": architectural degradation distributed across many
small commits, invisible to the per-commit speed layer.

Entry point: run_batch_consolidation(db_path, git_fn, window_days=30)
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List

from core._intent_miner import _extract_json, _get_downstream_symbols
from core._macro_change import build_history_markdown, group_macro_changes

logger = logging.getLogger(__name__)

_SILENT_WEAKENING_THRESHOLD = 0.20   # score drop fraction that triggers an alert
_SIGNIFICANT_CHANGE         = 0.10   # minimum drop to update intent_nodes row


_BATCH_CONSOLIDATOR_SYSTEM = (
    "Sei il Batch Consolidator di LORE — Lambda Architecture Batch Layer. "
    "Il tuo compito e rilevare 'silent weakening': degrado architetturale "
    "distribuito su N commit piccoli, invisibile al Speed Layer. "
    "Riceverai: l'intento canonico originale del file (baseline), "
    "i MacroChange recenti nella finestra temporale, il subgrafo downstream. "
    "Valuta se l'intento e stato preservato, parzialmente eroso, o degradato. "
    "Segnali di weakening: bypass di vincoli, cambio di responsabilita, "
    "accoppiamento nuovo non previsto, rimozione di guard_rules. "
    "Restituisci ESCLUSIVAMENTE un oggetto JSON con: "
    "integrity_score (0.0-1.0), weakening_detected (bool), "
    "weakening_signals ([str] — max 3, concreti), "
    "consolidation_summary (str — una frase sull'evoluzione recente)."
)


def _build_batch_prompt(
    file_path: str,
    canonical_intent: str,
    window_days: int,
    macro_changes: List[Dict[str, Any]],
    downstream: List[Dict[str, str]],
) -> str:
    """
    Prompt for the Batch Consolidator.
    Shows the stored canonical_intent as the baseline, then only recent
    MacroChanges from the analysis window for comparison.
    """
    if not macro_changes:
        return ""

    lines: List[str] = [
        "# LORE Batch Consolidator — Silent Weakening Analysis",
        "",
        "## Target File",
        f"- **Path**: `{file_path}`",
        f"- **Analysis window**: last {window_days} days",
        f"- **MacroChange groups in window**: {len(macro_changes)}",
        "",
        "## Original Intent (established baseline — do NOT change this)",
        "",
        canonical_intent or "_No canonical intent stored yet._",
        "",
        "---",
        "",
        f"## Recent Changes ({window_days}d window, newest first)",
        "",
        "_Each MacroChange = commits by the same author within 48h._",
        "",
    ]

    for i, mc in enumerate(macro_changes[:8], 1):
        n     = mc["commit_count"]
        label = (
            f"MacroChange {i} \u2014 {mc['author']} \u2014 {mc['date_range']}"
            f" ({n} commit{'s' if n > 1 else ''})"
        )
        lines += [f"### {label}", ""]
        for subj in mc["subjects"][:4]:
            lines.append(f"- {subj}")
        lines.append("")

    if downstream:
        lines += ["## Downstream Dependencies (blast radius context)", ""]
        by_file: Dict[str, List[str]] = {}
        for d in downstream:
            by_file.setdefault(d["callee_file"], []).append(d["callee_name"])
        for callee_file, names in sorted(by_file.items()):
            lines.append(f"- `{callee_file}`: {', '.join(names[:6])}")
        lines.append("")

    lines += [
        "## Task",
        "",
        "Compare the recent changes against the original intent. Evaluate:",
        "- Did the commits respect the original design constraints?",
        "- Were any guard_rules bypassed or responsibilities shifted?",
        "- Is there new coupling not foreseen by the canonical intent?",
    ]

    return "\n".join(lines)


def run_batch_consolidation(
    db_path: str, git_fn: Callable, window_days: int = 30
) -> Dict[str, Any]:
    """
    Batch Layer: re-evaluate integrity_score for all intent_nodes in the window.

    For each file with an existing intent_node:
      1. Fetch recent MacroChanges (window-scoped via --after=cutoff)
      2. Skip if no activity in the window (nothing to re-evaluate)
      3. Call Claude Haiku with original intent + recent changes
      4. Record score in intent_node_history (always — for trend charts)
      5. Flag silent_weakening_alerts if score dropped > 20%
      6. Update intent_nodes.integrity_score if change > 10%

    Returns summary dict with evaluation counts and alert list.
    """
    import os
    from pathlib import Path
    try:
        from core.llm_client import get_llm_client
        client = get_llm_client(Path(db_path).parent)
    except Exception as e:
        logger.debug(f"[BATCH] LLM client init failed: {e}")
        return {"files_evaluated": 0, "silent_weakening_detected": 0, "alerts": []}
    cutoff_date = (datetime.utcnow() - timedelta(days=window_days)).strftime("%Y-%m-%d")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
        except Exception:
            pass
        conn.execute("PRAGMA foreign_keys=ON;")
    except Exception:
        pass
    alerts: List[Dict[str, Any]] = []
    files_evaluated = 0

    try:
        candidates = conn.execute(
            "SELECT file_path, intent_json, integrity_score FROM intent_nodes"
        ).fetchall()

        for row in candidates:
            file_path    = row["file_path"]
            score_before = float(row["integrity_score"] or 0.7)
            try:
                intent_data      = json.loads(row["intent_json"] or "{}")
                canonical_intent = intent_data.get("canonical_intent", "")
            except (json.JSONDecodeError, TypeError):
                intent_data, canonical_intent = {}, ""

            try:
                # Only commits in the analysis window
                macro_changes = group_macro_changes(
                    git_fn, file_path, after_date=cutoff_date
                )
                if not macro_changes:
                    continue   # no recent activity — nothing to consolidate

                downstream = _get_downstream_symbols(conn, file_path)
                prompt = _build_batch_prompt(
                    file_path, canonical_intent, window_days,
                    macro_changes, downstream,
                )
                if not prompt:
                    continue

                response = client.messages.create(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=512,
                    system=_BATCH_CONSOLIDATOR_SYSTEM,
                    messages=[{"role": "user", "content": prompt[:5000]}],
                )
                result = _extract_json(response.content[0].text)
                if not result:
                    continue

                score_after       = float(result.get("integrity_score", score_before))
                weakening         = bool(result.get("weakening_detected", False))
                weakening_signals = result.get("weakening_signals", [])
                summary           = result.get("consolidation_summary", "")
                computed_at       = datetime.utcnow().isoformat()

                # Always record history (used for trend charts)
                conn.execute(
                    "INSERT INTO intent_node_history "
                    "(file_path, integrity_score, window_days, computed_at,"
                    " macro_changes, commit_count) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        file_path, score_after, window_days, computed_at,
                        len(macro_changes),
                        sum(m["commit_count"] for m in macro_changes),
                    ),
                )

                # Silent weakening check: > 20% drop OR LLM says weakening
                drop = (score_before - score_after) / score_before if score_before > 0 else 0.0
                if drop > _SILENT_WEAKENING_THRESHOLD or weakening:
                    drop_pct = drop * 100
                    conn.execute(
                        "INSERT INTO silent_weakening_alerts "
                        "(file_path, score_before, score_after, drop_pct,"
                        " window_days, flagged_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (file_path, score_before, score_after,
                         drop_pct, window_days, computed_at),
                    )
                    alert = {
                        "file":         file_path,
                        "score_before": round(score_before, 3),
                        "score_after":  round(score_after,  3),
                        "drop_pct":     round(drop_pct, 1),
                        "signals":      weakening_signals[:3],
                    }
                    alerts.append(alert)
                    logger.warning(
                        "[BATCH] SILENT WEAKENING %s: %.2f -> %.2f (%.0f%% drop)",
                        file_path, score_before, score_after, drop_pct,
                    )
                    print(
                        f"   [SILENT WEAKENING] {file_path}: "
                        f"{score_before:.2f} -> {score_after:.2f} "
                        f"({drop_pct:.0f}% drop)"
                    )

                # Update stored score if significant change (> 10%)
                if abs(score_after - score_before) > _SIGNIFICANT_CHANGE:
                    updated = dict(intent_data)
                    updated["integrity_score"] = score_after
                    if summary:
                        updated["consolidation_summary"] = summary
                    conn.execute(
                        "UPDATE intent_nodes "
                        "SET integrity_score = ?, intent_json = ? "
                        "WHERE file_path = ?",
                        (score_after, json.dumps(updated), file_path),
                    )

                files_evaluated += 1
                logger.info(
                    "[BATCH] %s: %.2f -> %.2f (window=%dd)",
                    file_path, score_before, score_after, window_days,
                )

            except Exception as exc:
                logger.debug("[BATCH] %s failed: %s", file_path, exc)

        try:
            conn.commit()
        except Exception:
            pass

    finally:
        conn.close()

    return {
        "window_days":               window_days,
        "files_evaluated":           files_evaluated,
        "silent_weakening_detected": len(alerts),
        "alerts":                    alerts,
    }
