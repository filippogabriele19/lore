import json
import logging
import sqlite3
import re
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

class BriefBuilder:
    """Builds a structured analysis brief for the Editor LLM.
    
    Collects intent, decision context, hotspot data, and git history
    for a target file, and formats it as a compact Markdown section
    ready to be injected into the Editor prompt.
    """
    
    def __init__(self, db_path: Path, project_root: Path):
        self.db_path = Path(db_path)
        self.project_root = Path(project_root)

    def _git(self, *args) -> str:
        import subprocess
        result = subprocess.run(
            ["git"] + list(args),
            cwd=str(self.project_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if result.returncode != 0:
            return ""
        return result.stdout

    def build_brief(self, file_path: str, task: str, focus_lines: list[range] = None) -> str:
        """Build a structured brief for a single target file.
        
        Collects from (in priority order):
        1. Intent Nodes (canonical_intent, integrity_score, evolution_log)
        2. Decision Links (ADR, commit reasoning, chat-derived rules)
        3. Hotspot data (change_freq, risk_score, co-change partners)
        4. Related test files (test oracle — expected behavior)
        5. Git Blame (stabilisation signal on focus lines)
        6. Recent commits touching this file
        7. Similar fixes semantically matched
        
        Returns a compact Markdown block (~500-1500 tokens).
        Falls back to empty string if KG is not populated.
        """
        if not self.db_path.exists():
            return ""

        file_path_norm = file_path.replace("\\", "/")
        
        intent = self._get_intent_context(file_path_norm)
        decisions = self._get_decision_context(file_path_norm)
        risk = self._get_risk_context(file_path_norm)
        test_oracle = self._get_test_oracle(file_path_norm)
        
        # Git Blame context (Phase B)
        blame_parts = []
        if focus_lines:
            for r in focus_lines:
                blame_info = self._get_git_blame_context(file_path_norm, r)
                if blame_info:
                    blame_parts.append(blame_info)
        blame_context = "\n\n".join(blame_parts) if blame_parts else ""

        recent_changes = self._get_recent_changes(file_path_norm)
        similar_fixes = self._get_similar_fixes(file_path_norm, task)
        
        # If we have absolutely no metadata, return empty string for graceful degradation
        has_any_data = any([
            "No intent data" not in intent,
            decisions != "",
            "No risk metrics" not in risk,
            test_oracle != "",
            blame_context != "",
            recent_changes != "",
            similar_fixes != ""
        ])
        
        if not has_any_data:
            return ""

        brief_lines = [
            f"=== ANALYSIS BRIEF for {file_path_norm} ===",
            "",
            intent,
            "",
            f"DECISIONS & CONSTRAINTS:\n{decisions}" if decisions else "DECISIONS & CONSTRAINTS:\n  No decision constraints recorded.",
            "",
            risk,
            "",
            f"EXPECTED BEHAVIOR (from tests):\n{test_oracle}" if test_oracle else "EXPECTED BEHAVIOR (from tests):\n  No test assertions found in DB.",
            ""
        ]

        if blame_context:
            brief_lines.extend([blame_context, ""])
            
        if recent_changes:
            brief_lines.extend([recent_changes, ""])
            
        if similar_fixes:
            brief_lines.extend([similar_fixes, ""])

        brief_lines.append(f"=== END BRIEF ===")
        
        brief_text = "\n".join(brief_lines)
        
        # Token budget limit (~1500 tokens -> ~6000 chars)
        if len(brief_text) > 6000:
            brief_text = brief_text[:5900] + "\n... [Brief truncated due to token budget] ...\n=== END BRIEF ==="
            
        return brief_text

    def build_signpost_brief(self, file_path: str, task: str) -> str:
        """Build a lightweight, context-anchored signpost brief for a single target file.
        
        This is designed to avoid LLM context pollution / lost-in-the-middle issues.
        It only includes:
        - Brief intent context
        - Compact references (ID/titles) of active ADRs/Constraints
        - Brief risk profile (hotspot status and main co-change partner)
        - Call to Action to pull detailed context via MCP tools on-demand.
        """
        if not self.db_path.exists():
            return ""

        file_path_norm = file_path.replace("\\", "/")
        
        # 1. Intent Context (Sintetico)
        intent_summary = "No intent data available."
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT intent_json FROM intent_nodes WHERE file_path = ? OR file_path = ?",
                    (file_path_norm, file_path_norm.replace("/", "\\"))
                ).fetchone()
                if row:
                    intent_json = json.loads(row["intent_json"])
                    canonical_intent = intent_json.get("canonical_intent", "")
                    if canonical_intent:
                        # Prendi solo la prima frase o limita la lunghezza
                        first_sentence = canonical_intent.split(".")[0].strip()
                        intent_summary = first_sentence + "." if first_sentence else canonical_intent
        except Exception as e:
            logger.debug(f"Failed to query intent summary for {file_path_norm}: {e}")

        # 2. Decision Links (Solo codici e simboli, niente descrizione estesa)
        decisions_summary = []
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute("""
                    SELECT dl.symbol_name, dl.source_type, dl.source_ref, dl.confidence 
                    FROM decision_links dl
                    JOIN symbols s ON dl.symbol_name = s.name
                    JOIN files f ON s.file_id = f.id
                    WHERE f.path = ? OR f.path = ?
                    ORDER BY dl.confidence DESC LIMIT 5
                """, (file_path_norm, file_path_norm.replace("/", "\\"))).fetchall()
                for r in rows:
                    decisions_summary.append(f"  - [{r['source_type'].upper()} {r['source_ref']}] governs Symbol '{r['symbol_name']}' (confidence: {r['confidence']:.2f})")
        except Exception as e:
            logger.debug(f"Failed to query decision references for {file_path_norm}: {e}")

        # 3. Risk Context (Sintetico)
        risk_summary = "No risk metrics available."
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT change_freq FROM hotspots WHERE file_path = ? OR file_path = ?",
                    (file_path_norm, file_path_norm.replace("/", "\\"))
                ).fetchone()
                
                ve_row = conn.execute("""
                    SELECT dst_file, co_change_rate, shared_commits FROM virtual_edges 
                    WHERE src_file = ? OR src_file = ?
                    UNION
                    SELECT src_file, co_change_rate, shared_commits FROM virtual_edges
                    WHERE dst_file = ? OR dst_file = ?
                    ORDER BY shared_commits DESC LIMIT 1
                """, (file_path_norm, file_path_norm.replace("/", "\\"), file_path_norm, file_path_norm.replace("/", "\\"))).fetchone()
                
                parts = []
                if row:
                    freq = row["change_freq"]
                    severity = "High Hotspot" if freq >= 15 else "Medium Hotspot" if freq >= 5 else "Low Activity"
                    parts.append(f"{severity} ({freq} commits)")
                if ve_row:
                    partner = ve_row[0].replace("\\", "/").split('/')[-1]
                    parts.append(f"Co-change partner: {partner} ({ve_row[1]:.0%})")
                if parts:
                    risk_summary = " | ".join(parts)
        except Exception as e:
            logger.debug(f"Failed to query risk summary for {file_path_norm}: {e}")

        brief_lines = [
            f"=== LORE CONTEXT SIGNPOST for {file_path_norm} ===",
            f"INTENT: {intent_summary}",
            f"RISK PROFILE: {risk_summary}",
        ]
        
        if decisions_summary:
            brief_lines.append("ACTIVE DECISIONS & CONSTRAINTS:")
            brief_lines.extend(decisions_summary)
        else:
            brief_lines.append("ACTIVE DECISIONS & CONSTRAINTS: None recorded.")
            
        brief_lines.extend([
            "",
            "IMPORTANT: If you need to inspect details, you MUST invoke the corresponding LORE MCP tool. Do not guess:",
            "- To check tests or expected behaviors: call `lore_get_related_tests(file_path)`",
            "- To check similar previous fixes: call `lore_get_similar_fixes(file_path, task)`",
            "- To check full ADR/compliance text: call `lore_get_adr(adr_id)`",
            "- To inspect symbol callers or dependencies: call `lore_get_symbol_context(symbol_name)`",
            "- To get git log or line blame: call `lore_get_git_context(file_path, focus_lines)`",
            "=== END SIGNPOST ==="
        ])
        
        return "\n".join(brief_lines)

    def _get_intent_context(self, file_path: str) -> str:
        """Query intent_nodes table for this file.
        Returns: canonical_intent, integrity_score, evolution summary."""
        file_path = file_path.replace("\\", "/")
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT intent_json, integrity_score FROM intent_nodes WHERE file_path = ? OR file_path = ?",
                    (file_path, file_path.replace("/", "\\"))
                ).fetchone()
                
                if row:
                    intent_json = json.loads(row["intent_json"])
                    canonical_intent = intent_json.get("canonical_intent", "No intent data available")
                    integrity_score = row["integrity_score"]
                    status = "Healthy" if integrity_score >= 0.8 else "Neutral" if integrity_score >= 0.65 else "Weakened"
                    
                    evolution = intent_json.get("evolution_log", [])
                    evolution_lines = []
                    for entry in evolution[:3]:
                        if isinstance(entry, dict):
                            version = entry.get("version", "?")
                            desc = entry.get("description", entry.get("desc", ""))
                            evolution_lines.append(f"    - [v{version}] {desc}")
                        else:
                            evolution_lines.append(f"    - {entry}")
                            
                    evo_str = "\n" + "\n".join(evolution_lines) if evolution_lines else ""
                    
                    return (
                        f"INTENT (why this code exists):\n"
                        f"  {canonical_intent}\n"
                        f"  Integrity: {integrity_score:.0%} — {status}{evo_str}"
                    )
        except Exception as e:
            logger.debug(f"Failed to query intent context for {file_path}: {e}")
            
        return "INTENT (why this code exists):\n  No intent data available."

    def _get_decision_context(self, file_path: str) -> str:
        """Query decision_links table for symbols in this file."""
        file_path = file_path.replace("\\", "/")
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute("""
                    SELECT dl.symbol_name, dl.source_type, dl.source_ref, dl.confidence, dl.description 
                    FROM decision_links dl
                    JOIN symbols s ON dl.symbol_name = s.name
                    JOIN files f ON s.file_id = f.id
                    WHERE f.path = ? OR f.path = ?
                    ORDER BY dl.confidence DESC LIMIT 10
                """, (file_path, file_path.replace("/", "\\"))).fetchall()
                
                if rows:
                    lines = []
                    for r in rows:
                        lines.append(f"  - [{r['source_type'].upper()} {r['source_ref']}] \"{r['description']}\" (confidence: {r['confidence']:.2f}) [Symbol: {r['symbol_name']}]")
                    return "\n".join(lines)
        except Exception as e:
            logger.debug(f"Failed to query decision context for {file_path}: {e}")
            
        return ""

    def _get_risk_context(self, file_path: str) -> str:
        """Query hotspots and virtual_edges.
        Returns: change frequency, risk score, co-change partners."""
        file_path = file_path.replace("\\", "/")
        change_freq = 0
        risk_score = 0.0
        
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT change_freq, risk_score FROM hotspots WHERE file_path = ? OR file_path = ?",
                    (file_path, file_path.replace("/", "\\"))
                ).fetchone()
                if row:
                    change_freq = row["change_freq"]
                    risk_score = row["risk_score"]
                    
                # Virtual edges
                ve_rows = conn.execute("""
                    SELECT dst_file, co_change_rate, shared_commits FROM virtual_edges 
                    WHERE src_file = ? OR src_file = ?
                    UNION
                    SELECT src_file, co_change_rate, shared_commits FROM virtual_edges
                    WHERE dst_file = ? OR dst_file = ?
                    ORDER BY shared_commits DESC LIMIT 5
                """, (file_path, file_path.replace("/", "\\"), file_path, file_path.replace("/", "\\"))).fetchall()
                
                if not row and not ve_rows:
                    return "RISK PROFILE:\n  No risk metrics available."

                co_partners = []
                for r in ve_rows:
                    partner = r[0].replace("\\", "/")
                    if partner != file_path:
                        co_partners.append(f"{partner.split('/')[-1]} ({r[1]:.0%})")
                        
                co_str = ", ".join(co_partners) if co_partners else "None detected"
                severity = "HIGH" if change_freq >= 15 else "MEDIUM" if change_freq >= 5 else "LOW"
                
                return (
                    f"RISK PROFILE:\n"
                    f"  Change frequency: {change_freq} commits ({severity} hotspot)\n"
                    f"  Co-change partners: {co_str}"
                )
        except Exception as e:
            logger.debug(f"Failed to query risk context for {file_path}: {e}")
            
        return "RISK PROFILE:\n  No risk metrics available."

    def _get_test_oracle(self, file_path: str) -> str:
        """Find and extract test expectations for this module."""
        from cli.agent_retrieval import _find_related_tests
        try:
            test_oracles = _find_related_tests(self.db_path, file_path, self.project_root)
            if test_oracles:
                lines = [f"  - {t['test_name']} ({t['test_file'].split('/')[-1]}): \"{t['docstring']}\"" for t in test_oracles]
                return "\n".join(lines)
        except Exception as e:
            logger.debug(f"Failed to query test oracle for {file_path}: {e}")
            
        return ""

    def _get_git_blame_context(self, file_path: str, focus_lines: range = None) -> str:
        """Run git blame on the target file (or specific line range)."""
        file_path = file_path.replace("\\", "/")
        args = ["blame", "--date=short"]
        if focus_lines:
            args.extend(["-L", f"{focus_lines.start},{focus_lines.stop}"])
        else:
            return ""
            
        args.append(file_path)
        raw_blame = self._git(*args)
        if not raw_blame:
            return ""
            
        lines = raw_blame.splitlines()
        entries = []
        seen_commits = set()
        for line in lines[:15]:
            m = re.match(r"^([0-9a-f^]+)\s+\((.*?)\s+(\d{4}-\d{2}-\d{2})", line)
            if m:
                commit_hash = m.group(1)
                author = m.group(2)
                date = m.group(3)
                if commit_hash not in seen_commits:
                    seen_commits.add(commit_hash)
                    subject = self._git("log", "-1", "--format=%s", commit_hash).strip()
                    entries.append(f"  - Commit {commit_hash[:8]} by {author} on {date}: \"{subject}\"")
                    
        if entries:
            return "RELEVANT BLAME CONTEXT:\n" + "\n".join(entries)
        return ""

    def _get_recent_changes(self, file_path: str, max_commits: int = 5) -> str:
        """Get the last N commits that touched this file."""
        file_path = file_path.replace("\\", "/")
        raw_log = self._git("log", "-n", str(max_commits), "--format=%h|%an|%ad|%s", "--date=short", "--", file_path)
        if not raw_log:
            return ""
        
        lines = []
        for line in raw_log.splitlines():
            parts = line.split("|", 3)
            if len(parts) == 4:
                h, author, date, subject = parts
                lines.append(f"  - {date} [{h}]: {subject} (by {author})")
                
        if lines:
            return "RECENT COMMITS ON THIS FILE:\n" + "\n".join(lines)
        return ""

    def _get_similar_fixes(self, file_path: str, task: str) -> str:
        """Search commit_reasoning for commits with similar intent to the task."""
        from cli.agent_retrieval import _get_embed_model, _cosine_sim
        model = _get_embed_model()
        if model is None:
            return ""
            
        file_path = file_path.replace("\\", "/")
        dir_path = str(Path(file_path).parent).replace("\\", "/")
        
        candidates = []
        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute("SELECT commit_hash, body, files_touched, commit_diff FROM commit_reasoning").fetchall()
                for r in rows:
                    try:
                        files = json.loads(r["files_touched"] or "[]")
                    except Exception:
                        files = []
                    
                    matches = False
                    for f in files:
                        f_norm = f.replace("\\", "/")
                        if f_norm == file_path or f_norm.startswith(dir_path + "/"):
                            matches = True
                            break
                    if matches and r["body"]:
                        candidates.append(r)
        except Exception as e:
            logger.debug(f"Failed to fetch commit reasoning for similar fixes: {e}")
            return ""
            
        if not candidates:
            return ""
            
        try:
            task_vec = model.encode([task], normalize_embeddings=True, show_progress_bar=False)[0]
        except Exception:
            return ""
            
        scored = []
        for c in candidates:
            body_text = c["body"]
            try:
                body_vec = model.encode([body_text], normalize_embeddings=True, show_progress_bar=False)[0]
                sim = sum(x * y for x, y in zip(task_vec, body_vec))
                scored.append((sim, c))
            except Exception:
                task_words = set(task.lower().split())
                body_words = set(body_text.lower().split())
                overlap = len(task_words & body_words)
                score = overlap / max(len(task_words), 1)
                scored.append((score, c))
                
        scored.sort(key=lambda x: -x[0])
        
        results = []
        for score, c in scored[:1]:
            lines = c["body"].strip().splitlines()
            subject = lines[0] if lines else "Similar Fix"
            body = "\n".join(lines[1:]) if len(lines) > 1 else ""
            diff = c["commit_diff"] or ""
            diff_snippet = "\n".join(diff.splitlines()[:20])
            
            results.append(
                f"SIMILAR PREVIOUS FIX (confidence: {score:.2f}):\n"
                f"  Commit: {c['commit_hash'][:8]} - {subject}\n"
                f"  Reasoning: {body[:300]}...\n"
                f"  Diff snippet:\n"
                f"```diff\n{diff_snippet}\n```"
            )
            
        return "\n\n".join(results)

    def build_audit_brief(self, file_path: str) -> dict:
        """Build a machine-readable brief for audit/DORA services."""
        file_path_norm = file_path.replace("\\", "/")
        brief = {
            "intent_health": {"score": 0.7, "status": "Neutral", "drift_direction": "Unknown"},
            "risk_profile": {"change_freq": 0, "risk_score": 0.0, "hotspot_rank": "None"},
            "coupling": {"co_change_partners": [], "fan_in": 0, "fan_out": 0},
            "documentation": {"has_intent_node": False, "decision_links_count": 0, "test_coverage": 0.0},
            "dora_signals": {"change_lead_time_estimate": "Unknown", "deployment_frequency_proxy": "Unknown"}
        }

        if not self.db_path.exists():
            return brief

        try:
            with sqlite3.connect(str(self.db_path)) as conn:
                conn.row_factory = sqlite3.Row
                
                # Intent Nodes
                row_intent = conn.execute(
                    "SELECT intent_json, integrity_score FROM intent_nodes WHERE file_path = ? OR file_path = ?",
                    (file_path_norm, file_path_norm.replace("/", "\\"))
                ).fetchone()
                if row_intent:
                    brief["intent_health"]["score"] = row_intent["integrity_score"]
                    brief["intent_health"]["status"] = "Healthy" if row_intent["integrity_score"] >= 0.8 else "Neutral" if row_intent["integrity_score"] >= 0.65 else "Weakened"
                    brief["documentation"]["has_intent_node"] = True
                    
                # Risk Profile
                row_hot = conn.execute(
                    "SELECT change_freq, risk_score FROM hotspots WHERE file_path = ? OR file_path = ?",
                    (file_path_norm, file_path_norm.replace("/", "\\"))
                ).fetchone()
                if row_hot:
                    brief["risk_profile"]["change_freq"] = row_hot["change_freq"]
                    brief["risk_profile"]["risk_score"] = row_hot["risk_score"]
                    brief["risk_profile"]["hotspot_rank"] = "High" if row_hot["change_freq"] >= 15 else "Medium" if row_hot["change_freq"] >= 5 else "Low"
                    
                # Decision links
                row_dl = conn.execute("""
                    SELECT COUNT(*) FROM decision_links dl
                    JOIN symbols s ON dl.symbol_name = s.name
                    JOIN files f ON s.file_id = f.id
                    WHERE f.path = ? OR f.path = ?
                """, (file_path_norm, file_path_norm.replace("/", "\\"))).fetchone()
                if row_dl:
                    brief["documentation"]["decision_links_count"] = row_dl[0]
                    
                # Coupling
                ve_rows = conn.execute("""
                    SELECT dst_file, co_change_rate FROM virtual_edges 
                    WHERE src_file = ? OR src_file = ?
                    UNION
                    SELECT src_file, co_change_rate FROM virtual_edges
                    WHERE dst_file = ? OR dst_file = ?
                """, (file_path_norm, file_path_norm.replace("/", "\\"), file_path_norm, file_path_norm.replace("/", "\\"))).fetchall()
                for r in ve_rows:
                    partner = r[0].replace("\\", "/")
                    if partner != file_path_norm:
                        brief["coupling"]["co_change_partners"].append(partner)
                        
                # DORA / Git Stats
                raw_commits = self._git("log", "--format=%ad", "--date=short", "--", file_path_norm)
                if raw_commits:
                    dates = [d.strip() for d in raw_commits.splitlines() if d.strip()]
                    if dates:
                        brief["dora_signals"]["deployment_frequency_proxy"] = f"{len(dates)} deployments"
                        
        except Exception as e:
            logger.debug(f"Failed to build audit brief for {file_path_norm}: {e}")

        return brief
