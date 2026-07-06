import os
import sys
import json
import math
import hashlib
import sqlite3
from pathlib import Path
from collections import defaultdict

from cli.shared import _get_db_path


def _run_full_audit(db_path: Path) -> dict:
    """
    Run the 7-category battery of deterministic queries against the KG.
    Returns a dict with 'stats' and 'findings' (list of finding dicts).
    """
    import sqlite3 as _sq

    findings: list[dict] = []
    stats: dict = {}

    with _sq.connect(str(db_path)) as c:
        c.row_factory = _sq.Row

        # KG header stats
        try:
            stats["files"]    = c.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            stats["symbols"]  = c.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
            stats["links"]    = c.execute("SELECT COUNT(*) FROM decision_links").fetchone()[0]
            stats["hotspots"] = c.execute("SELECT COUNT(*) FROM hotspots WHERE change_freq >= 10").fetchone()[0]
            stats["virtual_edges"] = c.execute("SELECT COUNT(*) FROM virtual_edges").fetchone()[0]
        except Exception:
            pass

        # ── Hotspot data (internal only — feeds Compound Risk + Amnesia) ──
        _hotspot_data: dict[str, dict] = {}
        try:
            rows = c.execute("""
                SELECT file_path, change_freq, risk_score FROM hotspots
                WHERE change_freq >= 15
                  AND file_path NOT LIKE '%lock%'
                  AND file_path NOT LIKE '%pyproject%'
                  AND file_path NOT LIKE '%README%'
                  AND file_path NOT LIKE '%.yml'
                  AND file_path NOT LIKE '%.json'
                  AND file_path NOT LIKE '%test%'
            """).fetchall()
            for r in rows:
                _hotspot_data[r["file_path"]] = {
                    "change_freq": r["change_freq"],
                    "risk_score": r["risk_score"],
                }
        except Exception:
            pass

        # ── Category 1: Institutional Amnesia ────────────────────────────
        _amnesia_files: set[str] = set()
        try:
            rows = c.execute("""
                SELECT h.file_path, h.change_freq, h.risk_score
                FROM hotspots h
                WHERE h.change_freq >= 15
                  AND h.file_path NOT LIKE '%test%'
                  AND h.file_path NOT LIKE '%lock%'
                  AND h.file_path NOT LIKE '%pyproject%'
                  AND h.file_path NOT LIKE '%README%'
                  AND h.file_path NOT LIKE '%.yml'
                  AND h.file_path NOT LIKE '%.json'
                  AND NOT EXISTS (
                      SELECT 1 FROM symbols s2
                      JOIN files f2 ON s2.file_id = f2.id
                      JOIN decision_links dl ON dl.symbol_name = s2.name
                      WHERE f2.path = h.file_path
                  )
                ORDER BY h.change_freq DESC
                LIMIT 10
            """).fetchall()
            for r in rows:
                _amnesia_files.add(r["file_path"])
                findings.append({
                    "category": "institutional_amnesia",
                    "severity": "critical" if r["change_freq"] >= 80 else "warning",
                    "title": f"{r['file_path'].split('/')[-1]} — {r['change_freq']} commits, 0 documented decisions",
                    "file": r["file_path"],
                    "metric": f"{r['change_freq']} commits · risk_score {r['risk_score']:.2f} · 0 decision links",
                    "desc": None,  # filled in post-processing with cross-category info
                    "_change_freq": r["change_freq"],
                    "_risk_score": r["risk_score"],
                })
        except Exception:
            pass

        # ── Category 2: Virtual Edge Violations ──────────────────────────
        try:
            rows = c.execute("""
                SELECT src_file, dst_file, co_change_rate, shared_commits, virtual_depth
                FROM virtual_edges
                WHERE shared_commits >= 4
                  AND src_file NOT LIKE '%test%'
                  AND dst_file NOT LIKE '%test%'
                  AND src_file NOT LIKE '%_profiles%'
                ORDER BY shared_commits DESC, co_change_rate DESC
                LIMIT 8
            """).fetchall()
            for r in rows:
                sev = "critical" if r["co_change_rate"] >= 0.85 else "warning"
                a = r["src_file"].split("/")[-1]
                b = r["dst_file"].split("/")[-1]
                findings.append({
                    "category": "virtual_edge",
                    "severity": sev,
                    "title": f"{a} ↔ {b} — accoppiamento nascosto ({r['co_change_rate']:.0%})",
                    "file": r["src_file"],
                    "file_b": r["dst_file"],
                    "metric": f"{r['shared_commits']} commit condivisi · co-change rate {r['co_change_rate']:.0%}",
                    "desc": (
                        f"Questi due file cambiano insieme nel {r['co_change_rate']:.0%} dei commit "
                        f"({r['shared_commits']} volte) ma non hanno nessun import diretto tra loro. "
                        "Un cambiamento in uno non genera nessun warning sull'altro — rischio di desync silenzioso."
                    ),
                })
        except Exception:
            pass

        # ── Category 3: Provider Profile Desync ──────────────────────────
        try:
            rows = c.execute("""
                SELECT src_file, dst_file, co_change_rate, shared_commits
                FROM virtual_edges
                WHERE src_file LIKE '%_profiles%' AND dst_file LIKE '%_profiles%'
                ORDER BY shared_commits DESC
                LIMIT 6
            """).fetchall()
            for r in rows:
                parts_a = r["src_file"].split("/")
                parts_b = r["dst_file"].split("/")
                pkg_a = parts_a[-3] if len(parts_a) >= 3 else r["src_file"]
                pkg_b = parts_b[-3] if len(parts_b) >= 3 else r["dst_file"]
                findings.append({
                    "category": "provider_desync",
                    "severity": "warning",
                    "title": f"{pkg_a} ↔ {pkg_b} profile desync",
                    "file": r["src_file"],
                    "file_b": r["dst_file"],
                    "metric": f"{r['shared_commits']} commit condivisi · rate {r['co_change_rate']:.0%}",
                    "desc": (
                        f"I file _profiles.py di {pkg_a} e {pkg_b} co-cambiano "
                        f"nel {r['co_change_rate']:.0%} dei commit ma non hanno import tra loro. "
                        "Quando un provider aggiunge un modello, gli altri rischiano di restare indietro silenziosamente."
                    ),
                })
        except Exception:
            pass

        # ── Category 4: Low Integrity Intent Nodes ───────────────────────
        try:
            rows = c.execute("""
                SELECT file_path, integrity_score, intent_json
                FROM intent_nodes
                WHERE integrity_score < 0.65
                ORDER BY integrity_score ASC
                LIMIT 6
            """).fetchall()
            import json as _j
            for r in rows:
                node = _j.loads(r["intent_json"]) if r["intent_json"] else {}
                title_node = node.get("title", r["file_path"].split("/")[-1])
                findings.append({
                    "category": "intent_drift",
                    "severity": "warning",
                    "title": f"{title_node} — intent integrity {r['integrity_score']:.0%}",
                    "file": r["file_path"],
                    "metric": f"integrity_score={r['integrity_score']:.2f}",
                    "desc": (
                        f"L'intento originale di questo modulo e' stato preservato solo al "
                        f"{r['integrity_score']:.0%}. Il codice si e' evoluto in una direzione "
                        "diversa da quella per cui era stato progettato."
                    ),
                })
        except Exception:
            pass

        # ── Category 5: Undocumented commits ─────────────────────────────
        try:
            total_commits = c.execute("SELECT COUNT(*) FROM commit_reasoning").fetchone()[0]
            reasoned = c.execute(
                "SELECT COUNT(*) FROM commit_reasoning WHERE body != '' AND body IS NOT NULL"
            ).fetchone()[0]
            if total_commits > 0:
                pct = reasoned / total_commits
                if pct < 0.3:
                    findings.append({
                        "category": "commit_amnesia",
                        "severity": "critical" if pct < 0.1 else "warning",
                        "title": f"Solo il {pct:.0%} dei commit ha reasoning documentato",
                        "file": None,
                        "metric": f"{reasoned}/{total_commits} commit con corpo significativo",
                        "desc": (
                            f"Il {1-pct:.0%} dei commit ({total_commits - reasoned}) "
                            "non spiega il perche' della modifica. "
                            "Rende impossibile ricostruire la storia decisionale del progetto."
                        ),
                    })
        except Exception:
            pass

        # ── Category 6: Structural SPOF (high fan-in symbols) ────────────
        _spof_files: set[str] = set()
        try:
            rows = c.execute("""
                SELECT s.name, s.kind, f.path,
                       COUNT(DISTINCT d.from_file_id) AS caller_files
                FROM symbols s
                JOIN files f ON s.file_id = f.id
                JOIN deps d ON d.to_name = s.name
                WHERE s.kind IN ('class', 'function')
                  AND f.path NOT LIKE '%test%'
                GROUP BY s.id
                HAVING caller_files >= 5
                ORDER BY caller_files DESC
                LIMIT 15
            """).fetchall()
            for r in rows:
                _spof_files.add(r["path"])
                findings.append({
                    "category": "structural_spof",
                    "severity": "critical" if r["caller_files"] >= 15 else "warning",
                    "title": f"{r['name']} — usato da {r['caller_files']} file distinti",
                    "file": r["path"],
                    "metric": f"{r['caller_files']} file dipendenti · kind={r['kind']}",
                    "desc": (
                        f"`{r['name']}` e' un single point of failure strutturale: "
                        f"qualsiasi modifica alla sua firma o comportamento impatta {r['caller_files']} file. "
                        "Nessun refactoring qui e' a basso rischio."
                    ),
                    "_caller_files": r["caller_files"],
                })
        except Exception:
            pass

        # ── Category 7: No Test Coverage (exclude .github/ and CI scripts) ─
        try:
            all_files = [r[0] for r in c.execute(
                "SELECT path FROM files WHERE path LIKE '%.py' OR path LIKE '%.ts'"
            ).fetchall()]
            test_paths = {p for p in all_files if "test" in p.lower()}
            tested_basenames = {p.split("/")[-1].replace("test_", "").replace("_test", "")
                                for p in test_paths}
            untested = [
                p for p in all_files
                if "test" not in p.lower()
                and p.split("/")[-1] not in tested_basenames
                and ".github" not in p
                and "scripts/" not in p
                and "script/" not in p
            ]
            for fp in untested[:5]:
                sym_count_file = c.execute(
                    "SELECT COUNT(*) FROM symbols s JOIN files f ON s.file_id=f.id "
                    "WHERE f.path=? AND s.kind IN ('class','function','method')",
                    (fp,)
                ).fetchone()[0]
                if sym_count_file >= 5:
                    findings.append({
                        "category": "no_test_coverage",
                        "severity": "warning",
                        "title": f"{fp.split('/')[-1]} — nessun test file corrispondente",
                        "file": fp,
                        "metric": f"{sym_count_file} simboli pubblici senza copertura",
                        "desc": (
                            f"Il file contiene {sym_count_file} simboli pubblici "
                            "ma non esiste nessun file di test corrispondente. "
                            "Qualsiasi modifica e' cieca — nessuna rete di sicurezza."
                        ),
                    })
        except Exception:
            pass

        # ── Category 8: High coupling ─────────────────────────────────────
        _coupling_files: set[str] = set()
        try:
            rows = c.execute("""
                SELECT f.path, COUNT(DISTINCT d.to_name) AS dep_count
                FROM files f
                JOIN symbols s ON s.file_id = f.id
                JOIN deps d ON d.from_symbol_id = s.id
                WHERE f.path NOT LIKE '%test%'
                  AND f.path NOT LIKE '%__init__%'
                GROUP BY f.id
                HAVING dep_count >= 20
                ORDER BY dep_count DESC
                LIMIT 15
            """).fetchall()
            for r in rows:
                _coupling_files.add(r["path"])
                findings.append({
                    "category": "high_coupling",
                    "severity": "warning",
                    "title": f"{r['path'].split('/')[-1]} — {r['dep_count']} dipendenze esterne",
                    "file": r["path"],
                    "metric": f"{r['dep_count']} simboli importati da file diversi",
                    "desc": (
                        f"Questo file dipende da {r['dep_count']} simboli definiti altrove. "
                        "Alto accoppiamento = difficile da testare, refactorare, o isolare in caso di bug."
                    ),
                    "_dep_count": r["dep_count"],
                })
        except Exception:
            pass

    # ── Post-processing ───────────────────────────────────────────────────────

    # 0. Normalize all file paths to forward slashes (hotspots uses '/', files uses '\')
    for f in findings:
        if f.get("file"):
            f["file"] = f["file"].replace("\\", "/")
        if f.get("file_b"):
            f["file_b"] = f["file_b"].replace("\\", "/")

    # 1. Build file → categories map
    from collections import defaultdict as _dd
    file_cats: dict[str, list[str]] = _dd(list)
    for f in findings:
        if f.get("file"):
            file_cats[f["file"]].append(f["category"])

    # 2. Fill Institutional Amnesia descriptions with cross-category context
    _cat_label = {
        "structural_spof": "Structural SPOF",
        "high_coupling":   "High Coupling",
        "virtual_edge":    "Hidden Coupling",
        "intent_drift":    "Intent Drift",
    }
    for f in findings:
        if f["category"] == "institutional_amnesia":
            other_cats = [c for c in file_cats.get(f["file"], [])
                          if c != "institutional_amnesia"]
            extra = ""
            if other_cats:
                labels = " · ".join(_cat_label.get(c, c) for c in other_cats)
                extra = f" Appare anche in: {labels}."
            freq = f.pop("_change_freq", "?")
            risk = f.pop("_risk_score", 0)
            f["desc"] = (
                f"{freq} commits · risk score {risk:.2f} · 0 decision links documented.{extra}"
            )

    # 3. Build Compound Risk findings (files in 2+ categories)
    compound: list[dict] = []
    seen_compound: set[str] = set()
    for fp, cats in sorted(file_cats.items(),
                           key=lambda x: (-len(set(x[1])), 0)):
        unique_cats = list(dict.fromkeys(cats))  # dedup preserving order
        if len(unique_cats) < 2 or fp in seen_compound:
            continue
        seen_compound.add(fp)
        _parts = fp.replace("\\", "/").split("/")
        fname = "/".join(_parts[-2:]) if len(_parts) >= 2 else _parts[-1]
        hot = _hotspot_data.get(fp, {})
        freq = hot.get("change_freq", "?")
        risk = hot.get("risk_score", 0.0)
        cat_labels = " · ".join(_cat_label.get(c, c.replace("_", " ").title())
                                for c in unique_cats)
        parts: list[str] = []
        if freq != "?":
            parts.append(f"{freq} commits")
        # find max caller_files for this file
        spof_f = next((x for x in findings
                       if x["file"] == fp and x["category"] == "structural_spof"), None)
        coup_f = next((x for x in findings
                       if x["file"] == fp and x["category"] == "high_coupling"), None)
        if spof_f:
            parts.append(f"usato da {spof_f.get('_caller_files', '?')} file")
        if coup_f:
            parts.append(f"{coup_f.get('_dep_count', '?')} dipendenze esterne")
        metric_str = " · ".join(parts) if parts else cat_labels
        compound.append({
            "category": "compound_risk",
            "severity": "critical",
            "title": f"{fname} — {len(unique_cats)} risk signals",
            "file": fp,
            "metric": metric_str,
            "desc": f"Questo file appare in {len(unique_cats)} categorie di rischio distinte: {cat_labels}. "
                    "Il rischio combinato e' maggiore della somma delle parti.",
            "_cat_count": len(unique_cats),
        })

    compound.sort(key=lambda x: -x["_cat_count"])

    # 4. Remove internal-only keys, exclude hotspot standalone
    keep_cats = {
        "compound_risk", "institutional_amnesia", "virtual_edge",
        "provider_desync", "intent_drift", "commit_amnesia",
        "structural_spof", "no_test_coverage", "high_coupling",
    }
    clean: list[dict] = []
    for f in findings:
        if f["category"] not in keep_cats:
            continue
        f.pop("_change_freq", None)
        f.pop("_risk_score", None)
        f.pop("_caller_files", None)
        f.pop("_dep_count", None)
        f.pop("_cat_count", None)
        clean.append(f)

    # 5. Final order: compound first, then critical→warning within each category
    _ord = {"critical": 0, "warning": 1, "info": 2}
    clean.sort(key=lambda x: _ord.get(x["severity"], 2))
    all_findings = compound + clean
    return {"stats": stats, "findings": all_findings}
