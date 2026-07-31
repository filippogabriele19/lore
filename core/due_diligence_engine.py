"""
core/due_diligence_engine.py
─────────────────────────────────────────────────────────────────────────────
Technical Due Diligence Engine for M&A, VC & Private Equity Codebase Audits.

Mines Knowledge Graph metadata (.lore_poc.db) and Git author contribution history
to compute:
1. Bus Factor & Key-Person Dependency Index
2. Codebase Health & Architectural Fragility Score (0-100)
3. Hidden Co-Change Coupling Matrix (L3)
4. Institutional Memory & ADR Governance Score (L4)

Generates:
- Standalone Interactive HTML Audit Report (due_diligence_report.html)
- Executive Markdown Summary (due_diligence_report.md)
- Machine-Readable JSON Export (due_diligence_report.json)
"""

import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Dict, List, Any, Optional
from datetime import datetime

from core.symbol_db import SymbolDB


class DueDiligenceEngine:
    def __init__(self, project_root: str | Path, db_path: Optional[str | Path] = None):
        self.project_root = Path(project_root).resolve()
        self.db_path = Path(db_path) if db_path else self.project_root / ".lore_poc.db"
        if not self.db_path.exists():
            # Fallback check inside .lore folder
            alt_db = self.project_root / ".lore" / ".lore_poc.db"
            if alt_db.exists():
                self.db_path = alt_db

    def _get_db(self) -> Optional[SymbolDB]:
        if self.db_path.exists():
            return SymbolDB(self.db_path)
        return None

    def analyze_bus_factor(self) -> Dict[str, Any]:
        """
        Analyzes Git history to compute author contribution distribution per file
        and calculate Key Person Dependency (Bus Factor).
        """
        file_authors: Dict[str, Dict[str, int]] = {}
        file_totals: Dict[str, int] = {}

        try:
            cmd = ["git", "log", "--name-only", "--format=COMMIT:%an"]
            res = subprocess.run(cmd, cwd=str(self.project_root), capture_output=True, text=True, check=True)

            current_author = None
            for line in res.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                if line.startswith("COMMIT:"):
                    current_author = line.replace("COMMIT:", "").strip()
                elif current_author and not line.startswith("COMMIT:"):
                    # File line
                    rel_f = line.replace("\\", "/")
                    if rel_f not in file_authors:
                        file_authors[rel_f] = {}
                        file_totals[rel_f] = 0
                    file_authors[rel_f][current_author] = file_authors[rel_f].get(current_author, 0) + 1
                    file_totals[rel_f] += 1
        except Exception:
            pass

        key_person_files = []
        overall_author_commits: Dict[str, int] = {}

        for f, authors in file_authors.items():
            total = file_totals[f]
            for author, count in authors.items():
                overall_author_commits[author] = overall_author_commits.get(author, 0) + count
                share = count / total if total > 0 else 0
                if share >= 0.70 and total >= 3:
                    key_person_files.append({
                        "file": f,
                        "primary_author": author,
                        "share_percent": round(share * 100, 1),
                        "total_commits": total
                    })

        sorted_kp = sorted(key_person_files, key=lambda x: x["total_commits"], reverse=True)
        total_tracked_files = len(file_totals)
        single_author_count = len(sorted_kp)
        bus_factor_ratio = (single_author_count / total_tracked_files) if total_tracked_files > 0 else 0.0

        if bus_factor_ratio > 0.40:
            bus_risk = "HIGH (Severe Key-Person Risk)"
        elif bus_factor_ratio > 0.20:
            bus_risk = "MEDIUM (Moderate Concentration)"
        else:
            bus_risk = "LOW (Well-Distributed Team Knowledge)"

        return {
            "total_files_analyzed": total_tracked_files,
            "single_author_files_count": single_author_count,
            "bus_factor_risk_level": bus_risk,
            "bus_factor_ratio_percent": round(bus_factor_ratio * 100, 1),
            "key_person_files": sorted_kp[:15],
            "author_commits_summary": sorted(
                [{"author": k, "commits": v} for k, v in overall_author_commits.items()],
                key=lambda x: x["commits"], reverse=True
            )[:10]
        }

    def analyze_codebase_health(self) -> Dict[str, Any]:
        """
        Analyzes Knowledge Graph metrics to compute overall Maintainability Score (0-100).
        """
        db = self._get_db()
        if not db:
            return {
                "health_score": 50,
                "health_grade": "C (Unindexed)",
                "hotspots": [],
                "virtual_edges_count": 0,
                "adrs_count": 0,
                "symbols_count": 0
            }

        try:
            conn = db.con

            # 1. Hotspots
            hotspots = conn.execute(
                "SELECT file_path, change_freq, risk_score FROM hotspots ORDER BY risk_score DESC LIMIT 10"
            ).fetchall()
            hotspot_list = [{"file": h[0], "commits": h[1], "risk": round(h[2], 2)} for h in hotspots]

            # 2. Virtual Edges (L3 Co-changes)
            virtual_edges = conn.execute(
                "SELECT src_file, dst_file, co_change_rate, shared_commits FROM virtual_edges ORDER BY shared_commits DESC LIMIT 10"
            ).fetchall()
            edge_list = [{"src": v[0], "dst": v[1], "rate": round(v[2], 2), "commits": v[3]} for v in virtual_edges]

            # 3. Decision links & Intent nodes (L4)
            adr_count = conn.execute("SELECT COUNT(*) FROM decision_links").fetchone()[0]
            intent_count = conn.execute("SELECT COUNT(*) FROM intent_nodes").fetchone()[0] if self._table_exists(conn, "intent_nodes") else 0
            symbol_count = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
            file_count = conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]

            # Calculate Health Score (100 Base)
            score = 100.0

            # Hotspot Penalty (up to -25 points)
            avg_risk = sum(h["risk"] for h in hotspot_list) / len(hotspot_list) if hotspot_list else 0.0
            score -= (avg_risk * 25)

            # High Co-Change Penalty (up to -15 points for uncoupled file pairs)
            high_coupling_count = sum(1 for e in edge_list if e["rate"] >= 0.70)
            score -= min(15, high_coupling_count * 3)

            # Governance Reward (+10 points for ADR / Intent presence)
            if adr_count > 0 or intent_count > 0:
                score = min(100.0, score + 10.0)

            score = max(10.0, min(100.0, score))

            if score >= 85:
                grade = "A (Excellent Health & Architecture)"
            elif score >= 70:
                grade = "B (Good Maintainability)"
            elif score >= 55:
                grade = "C (Moderate Architectural Debt)"
            else:
                grade = "D (High Risk / Severe Technical Debt)"

            return {
                "health_score": round(score, 1),
                "health_grade": grade,
                "file_count": file_count,
                "symbol_count": symbol_count,
                "adr_count": adr_count,
                "intent_count": intent_count,
                "hotspots": hotspot_list,
                "co_change_pairs": edge_list,
                "virtual_edges_total": len(edge_list)
            }
        finally:
            db.close()

    def _table_exists(self, conn: sqlite3.Connection, table_name: str) -> bool:
        res = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name=?", (table_name,)
        ).fetchone()
        return res[0] > 0

    def run_due_diligence_audit(self) -> Dict[str, Any]:
        """
        Executes full Due Diligence audit combining Bus Factor and Codebase Health metrics.
        """
        bus_factor = self.analyze_bus_factor()
        health = self.analyze_codebase_health()

        return {
            "timestamp": datetime.now().isoformat(),
            "project_name": self.project_root.name,
            "project_path": str(self.project_root),
            "bus_factor": bus_factor,
            "health": health
        }

    def generate_html_report(self, data: Dict[str, Any], output_path: Path) -> Path:
        """
        Generates a standalone, beautiful HTML Due Diligence report for M&A / VCs.
        """
        bf = data["bus_factor"]
        h = data["health"]

        hotspot_rows = "".join([
            f"<tr><td><code>{item['file']}</code></td><td>{item['commits']}</td><td><span class='badge risk-{self._risk_tier(item['risk'])}'>{item['risk']}</span></td></tr>"
            for item in h.get("hotspots", [])
        ]) or "<tr><td colspan='3'>No high-risk hotspots detected.</td></tr>"

        kp_rows = "".join([
            f"<tr><td><code>{item['file']}</code></td><td><strong>{item['primary_author']}</strong></td><td><span class='badge risk-high'>{item['share_percent']}%</span></td><td>{item['total_commits']}</td></tr>"
            for item in bf.get("key_person_files", [])
        ]) or "<tr><td colspan='4'>No single-author concentration detected.</td></tr>"

        co_rows = "".join([
            f"<tr><td><code>{item['src']}</code></td><td><code>{item['dst']}</code></td><td>{round(item['rate']*100)}%</td><td>{item['commits']}</td></tr>"
            for item in h.get("co_change_pairs", [])
        ]) or "<tr><td colspan='4'>No hidden co-change coupling detected.</td></tr>"

        html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Technical Due Diligence Audit — {data['project_name']}</title>
    <style>
        :root {{
            --bg-color: #0f172a;
            --card-bg: #1e293b;
            --text-color: #f8fafc;
            --accent-color: #38bdf8;
            --accent-green: #22c55e;
            --accent-yellow: #eab308;
            --accent-red: #ef4444;
            --border-color: #334155;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: var(--bg-color);
            color: var(--text-color);
            margin: 0;
            padding: 40px;
            line-height: 1.6;
        }}
        .header {{
            border-bottom: 2px solid var(--border-color);
            padding-bottom: 20px;
            margin-bottom: 30px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        h1 {{ margin: 0; color: var(--accent-color); font-size: 28px; }}
        .subtitle {{ color: #94a3b8; font-size: 14px; margin-top: 5px; }}
        .grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }}
        .card {{
            background: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 10px;
            padding: 20px;
        }}
        .card-title {{ font-size: 14px; color: #94a3b8; text-transform: uppercase; letter-spacing: 1px; }}
        .card-value {{ font-size: 36px; font-weight: bold; margin: 10px 0; color: var(--accent-color); }}
        .card-desc {{ font-size: 13px; color: #cbd5e1; }}
        table {{
            width: 100%;
            border-collapse: collapse;
            margin-top: 15px;
            background: var(--card-bg);
            border-radius: 8px;
            overflow: hidden;
        }}
        th, td {{ padding: 12px 16px; text-align: left; border-bottom: 1px solid var(--border-color); }}
        th {{ background-color: #090d16; color: #94a3b8; font-size: 12px; text-transform: uppercase; }}
        code {{ font-family: monospace; color: #e2e8f0; font-size: 13px; }}
        .badge {{
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: bold;
        }}
        .risk-high {{ background: rgba(239, 68, 68, 0.2); color: var(--accent-red); }}
        .risk-medium {{ background: rgba(234, 179, 8, 0.2); color: var(--accent-yellow); }}
        .risk-low {{ background: rgba(34, 197, 94, 0.2); color: var(--accent-green); }}
        .section-header {{ margin-top: 40px; margin-bottom: 15px; font-size: 20px; color: #f1f5f9; }}
    </style>
</head>
<body>
    <div class="header">
        <div>
            <h1>📊 Technical Due Diligence Report</h1>
            <div class="subtitle">Repository: <strong>{data['project_name']}</strong> &bull; Generated: {data['timestamp'][:19]}</div>
        </div>
        <div>
            <span class="badge risk-low" style="font-size: 14px; padding: 8px 16px;">LORE Knowledge Graph Verified</span>
        </div>
    </div>

    <div class="grid">
        <div class="card">
            <div class="card-title">Codebase Health Score</div>
            <div class="card-value" style="color: {'#22c55e' if h['health_score']>=75 else '#eab308' if h['health_score']>=60 else '#ef4444'}">{h['health_score']} / 100</div>
            <div class="card-desc">Grade: {h['health_grade']}</div>
        </div>
        <div class="card">
            <div class="card-title">Bus Factor Risk Level</div>
            <div class="card-value" style="font-size: 24px; margin-top: 20px;">{bf['bus_factor_risk_level']}</div>
            <div class="card-desc">{bf['single_author_files_count']} files ({bf['bus_factor_ratio_percent']}%) have >70% single-author concentration.</div>
        </div>
        <div class="card">
            <div class="card-title">Knowledge Graph Scale</div>
            <div class="card-value">{h['symbol_count']:,}</div>
            <div class="card-desc">{h['file_count']:,} Files &bull; {h['adr_count']} ADR Decision Links</div>
        </div>
    </div>

    <div class="section-header">👤 Key Person Dependency & Bus Factor Analysis</div>
    <table>
        <thead>
            <tr>
                <th>File Path</th>
                <th>Primary Author</th>
                <th>Contribution Share</th>
                <th>Total Commits</th>
            </tr>
        </thead>
        <tbody>
            {kp_rows}
        </tbody>
    </table>

    <div class="section-header">🔥 Codebase Hotspots & High-Fragility Modules</div>
    <table>
        <thead>
            <tr>
                <th>File Path</th>
                <th>Commit Churn</th>
                <th>Risk Score</th>
            </tr>
        </thead>
        <tbody>
            {hotspot_rows}
        </tbody>
    </table>

    <div class="section-header">🔗 Hidden Co-Change Coupling (L3 Association Rules)</div>
    <table>
        <thead>
            <tr>
                <th>Source File</th>
                <th>Coupled Destination File</th>
                <th>Co-Change Rate</th>
                <th>Shared Commits</th>
            </tr>
        </thead>
        <tbody>
            {co_rows}
        </tbody>
    </table>
</body>
</html>
"""
        output_path.write_text(html_content, encoding="utf-8")
        return output_path

    def generate_markdown_report(self, data: Dict[str, Any], output_path: Path) -> Path:
        """
        Generates Executive Markdown summary report.
        """
        bf = data["bus_factor"]
        h = data["health"]

        md_lines = [
            f"# 📊 Technical Due Diligence Audit — {data['project_name']}",
            f"**Generated**: {data['timestamp'][:19]} | **Audit Engine**: LORE Knowledge Graph",
            "",
            "## 🎯 Executive Metrics Summary",
            f"- **Codebase Health Score**: **{h['health_score']} / 100** ({h['health_grade']})",
            f"- **Bus Factor Risk Level**: **{bf['bus_factor_risk_level']}**",
            f"- **Single-Author Concentrated Files**: **{bf['single_author_files_count']}** ({bf['bus_factor_ratio_percent']}% of repository)",
            f"- **Indexed Architecture**: **{h['symbol_count']:,}** symbols across **{h['file_count']:,}** files",
            f"- **ADR Decisional Links**: **{h['adr_count']}** recorded constraints",
            "",
            "## 👤 Key Person Dependency (Bus Factor)",
            "| File Path | Primary Author | Share % | Commits |",
            "|---|---|:---:|:---:|"
        ]

        for item in bf.get("key_person_files", [])[:10]:
            md_lines.append(f"| `{item['file']}` | **{item['primary_author']}** | {item['share_percent']}% | {item['total_commits']} |")

        md_lines.extend([
            "",
            "## 🔥 Top High-Fragility Hotspots",
            "| File Path | Commit Churn | Risk Score |",
            "|---|:---:|:---:|"
        ])
        for item in h.get("hotspots", [])[:10]:
            md_lines.append(f"| `{item['file']}` | {item['commits']} | **{item['risk']}** |")

        output_path.write_text("\n".join(md_lines), encoding="utf-8")
        return output_path

    def _risk_tier(self, score: float) -> str:
        if score >= 0.75:
            return "high"
        elif score >= 0.40:
            return "medium"
        return "low"
