from __future__ import annotations
import sqlite3 as _sq
import json as _json
import html as _h
from pathlib import Path
from datetime import datetime as _dt
from cli.cve_data import _CVE_REGISTRY


def _parse_date(d_str: str) -> _dt:
    d_str = d_str.strip()
    try:
        return _dt.fromisoformat(d_str)
    except ValueError:
        pass
    parts = d_str.split(' ')
    if len(parts) >= 2:
        dt_part = f"{parts[0]}T{parts[1]}"
        try:
            return _dt.fromisoformat(dt_part)
        except ValueError:
            pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S %z", "%Y-%m-%d %H:%M:%S%z"):
        try:
            return _dt.strptime(d_str, fmt)
        except ValueError:
            pass
    return _dt(1970, 1, 1)



def _compute_detection_score(signals: list, stats: dict) -> tuple:
    """
    Returns (score_0_to_97, factors_list).
    """
    factors: list[dict] = []
    score = 0.0

    # A: Institutional Amnesia
    amnesia = [s for s in signals if s["type"] == "amnesia" and s.get("links", 1) == 0]
    a_pts = min(len(amnesia) * 15, 30)
    score += a_pts
    if a_pts:
        factors.append({"label": "A — Institutional Amnesia", "pts": a_pts,
                        "detail": f"{len(amnesia)} file nel percorso vulnerabile con 0 decision link"})

    # B: Blast Radius
    blast = stats["total_blast"]
    b_pts = 15 if blast >= 50 else (12 if blast >= 30 else (7 if blast >= 15 else (3 if blast >= 5 else 0)))
    score += b_pts
    if b_pts:
        factors.append({"label": "B — Blast Radius", "pts": b_pts,
                        "detail": f"{blast} file dipendenti dalla superficie vulnerabile (prod only)"})

    # C: Semantic warning signals
    semantic = [s for s in signals if s["type"] == "pre_cve_warning" and s.get("signal_class") == "semantic"]
    c_pts_raw = sum(s.get("relevance_score", 0.5) * 7 for s in semantic)
    c_pts = round(min(c_pts_raw, 21))
    score += c_pts
    if c_pts:
        avg_rel = (sum(s.get("relevance_score", 0.5) for s in semantic) / len(semantic)) if semantic else 0
        factors.append({"label": "C — Segnali semantici pre-CVE", "pts": c_pts,
                        "detail": f"{len(semantic)} commit, rilevanza media {avg_rel:.0%} (pesata sul dominio di rischio)"})

    # D: General security awareness
    awareness = [s for s in signals if s["type"] == "pre_cve_warning" and s.get("signal_class") == "awareness"]
    d_pts = min(len(awareness) * 3, 9)
    score += d_pts
    if d_pts:
        factors.append({"label": "D — Security awareness", "pts": d_pts,
                        "detail": f"{len(awareness)} commit con concern di sicurezza in moduli adiacenti"})

    # E: Dangerous architectural default
    archi = [s for s in signals if s["type"] == "architectural_decision"]
    e_pts = min(len(archi) * 10, 10)
    score += e_pts
    if e_pts:
        factors.append({"label": "E — Dangerous default documentato", "pts": e_pts,
                        "detail": f"{len(archi)} decisione con default pericoloso senza safeguard documentato"})

    # F: Temporal clustering of semantic signals
    f_pts = 0
    if len(semantic) >= 2:
        dates = sorted(
            _parse_date(s["date"]) for s in semantic if s.get("date")
        )
        if len(dates) >= 2:
            window_days   = (dates[-1] - dates[0]).days
            window_months = window_days / 30.44
            if window_months <= 36:
                f_pts = 10
                score += f_pts
                factors.append({
                    "label": "F — Temporal clustering",
                    "pts":   f_pts,
                    "detail": (
                        f"{len(semantic)} segnali semantici concentrati in finestra "
                        f"{window_months:.0f} mesi "
                        f"({dates[0].strftime('%Y-%m')} to {dates[-1].strftime('%Y-%m')})"
                    ),
                })

    # G: Intent-Implementation Delta
    intent_delta = [s for s in signals if s["type"] == "intent_impl_delta"]
    g_pts = 8 if intent_delta else 0
    score += g_pts
    if g_pts:
        best_delta = max(s["delta_score"] for s in intent_delta)
        factors.append({
            "label": "G — Intent-Implementation Delta",
            "pts":   g_pts,
            "detail": (
                f"{len(intent_delta)} commit con divergenza intent/impl > 35% "
                f"(massimo: {best_delta:.0%}) — il team pensava di fixare, non ha fixato"
            ),
        })

    return min(int(score), 97), factors


def _run_cve_retrospective(db_path: str, cve_id: str) -> dict:
    """Query existing KG for pre-CVE signals. No LLM calls needed."""
    cfg = _CVE_REGISTRY.get(cve_id)
    if not cfg:
        return {"error": f"CVE {cve_id} not in registry"}

    signals: list[dict] = []
    _semantic_kw = cfg.get("semantic_kw",
                            ["serial", "deserial", "dumps", "dumpd", "loads", "load("])
    _fear_likes   = cfg.get("fear_likes",
                             ["dangerous_deserialization", "unsafe%deserialization",
                              "arbitrary code", "bandit", "injection"])

    with _sq.connect(db_path) as c:
        c.row_factory = _sq.Row
        c.executescript("""
            CREATE TABLE IF NOT EXISTS decision_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol_name TEXT,
                symbol_id INTEGER,
                source_type TEXT,
                source_ref TEXT,
                confidence REAL,
                description TEXT,
                mechanism TEXT,
                constraint_text TEXT,
                warning INTEGER DEFAULT 0,
                embedding BLOB,
                FOREIGN KEY (symbol_id) REFERENCES symbols(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS hotspots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT UNIQUE, change_freq INTEGER DEFAULT 0,
                complexity_score REAL DEFAULT 0.0, risk_score REAL DEFAULT 0.0
            );
            CREATE TABLE IF NOT EXISTS commit_reasoning (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                commit_hash TEXT UNIQUE, author TEXT, date TEXT,
                body TEXT, keywords_found TEXT, files_touched TEXT,
                commit_diff TEXT
            );
            CREATE VIEW IF NOT EXISTS symbol_calls AS
            SELECT 
                d.from_symbol_id AS caller_symbol_id,
                s.id AS callee_symbol_id,
                d.to_name AS callee_name,
                d.line AS call_line
            FROM deps d
            LEFT JOIN symbols s ON d.to_name = s.name
            WHERE d.dep_type = 'call';
        """)

        try:
            cols = [col[1] for col in c.execute("PRAGMA table_info(commit_reasoning)").fetchall()]
            if "commit_diff" not in cols:
                c.execute("ALTER TABLE commit_reasoning ADD COLUMN commit_diff TEXT")
        except Exception:
            pass

        # Signal 1: blast radius of vulnerable symbols
        br_details = []
        total_blast = 0
        vuln_syms = cfg.get("vuln_symbols", [])
        
        for vs in vuln_syms:
            rows = c.execute("""
                SELECT DISTINCT caller.name as caller_name, f_caller.path as caller_path
                FROM symbol_calls sc
                JOIN symbols caller ON sc.caller_symbol_id = caller.id
                JOIN files f_caller ON caller.file_id = f_caller.id
                WHERE sc.callee_name = ?
                  AND f_caller.path NOT LIKE '%tests/%'
                  AND f_caller.path NOT LIKE '%test_%'
            """, (vs,)).fetchall()
            
            http_rows = [r for r in rows if any(pat in r["caller_path"].lower() for pat in ("view", "handler", "api", "route"))]
            br_details.append({
                "symbol": vs,
                "callers": len(rows),
                "http_exposed": len(http_rows)
            })
            total_blast += len(rows)

        if total_blast > 0:
            signals.append({
                "type": "blast_radius",
                "severity": "critical" if total_blast >= 30 else "warning",
                "title": f"Blast radius elevato: {total_blast} file dipendenti da simboli vulnerabili",
                "desc": (
                    f"I simboli vulnerabili {', '.join(vuln_syms)} sono chiamati "
                    f"in {total_blast} moduli distinti del progetto (esclusi i test)."
                )
            })

        # Signal 2: Institutional Amnesia on vulnerable files
        vuln_files = cfg.get("vuln_files", [])
        for vf in vuln_files:
            row_f = c.execute("SELECT id FROM files WHERE path = ? OR path = ?", (vf, vf.replace("/", "\\"))).fetchone()
            if row_f:
                f_id = row_f["id"]
                links = c.execute("""
                    SELECT COUNT(*) FROM decision_links dl
                    JOIN symbols s ON dl.symbol_id = s.id
                    WHERE s.file_id = ?
                """, (f_id,)).fetchone()[0]
                
                if links == 0:
                    signals.append({
                        "type": "amnesia",
                        "severity": "critical",
                        "links": 0,
                        "title": f"Institutional Amnesia su {vf.split('/')[-1]}",
                        "desc": (
                            f"Il file '{vf}' contiene simboli critici per la vulnerabilità "
                            f"ma ha ZERO decision link (ADR) indicizzati nel Knowledge Graph. "
                            f"La memoria istituzionale sui vincoli di sicurezza è assente."
                        )
                    })

        # Signal 3: Pre-CVE Warning Commits
        for fl in _fear_likes:
            commits = c.execute("""
                SELECT commit_hash, author, date, body, files_touched 
                FROM commit_reasoning
                WHERE body LIKE ?
            """, (f"%{fl}%",)).fetchall()
            
            for r in commits:
                body_lower = r["body"].lower() if r["body"] else ""
                rel_score = 0.3
                sc_class = "awareness"
                
                # Check semantic overlap
                if any(kw in body_lower for kw in _semantic_kw):
                    rel_score = 0.85
                    sc_class = "semantic"
                    
                touched = r["files_touched"] or ""
                touched_files = [tf.strip().replace("\\", "/") for tf in touched.split(",") if tf.strip()]
                overlaps_vuln = any(any(vf in tf for vf in vuln_files) for tf in touched_files)
                if overlaps_vuln:
                    rel_score = min(1.0, rel_score + 0.15)
                    sc_class = "semantic"
                    
                signals.append({
                    "type": "pre_cve_warning",
                    "severity": "critical" if sc_class == "semantic" else "warning",
                    "signal_class": sc_class,
                    "relevance_score": rel_score,
                    "date": r["date"],
                    "title": f"Commit {r['commit_hash'][:8]} di {r['author']} ({r['date']})",
                    "desc": f"Messaggio: {r['body'].strip()}\nFile modificati: {', '.join(touched_files)}"
                })

        # Signal 4: Intent-Implementation Delta on Decision B Commit
        dc = cfg.get("decision_b_commit")
        if dc:
            row_cmt = c.execute("SELECT commit_diff FROM commit_reasoning WHERE commit_hash LIKE ?", (f"%{dc['hash']}%",)).fetchone()
            if row_cmt and row_cmt["commit_diff"]:
                diff_text = row_cmt["commit_diff"]
                
                from cli.agent_retrieval import _cosine_sim, _make_embed_fn
                embed_fn = _make_embed_fn()
                if embed_fn:
                    score = _compute_intent_delta(dc["title"], diff_text, embed_fn)
                    if score >= 0.35:
                        signals.append({
                            "type": "intent_impl_delta",
                            "severity": "critical",
                            "delta_score": score,
                            "title": f"Intent-Implementation Delta Elevato ({score:.0%}) su commit {dc['hash'][:8]}",
                            "desc": (
                                f"Il commit '{dc['title']}' ha un delta elevato rispetto alle modifiche effettive. "
                                f"L'intento architetturale dichiarato diverge dal codice realmente modificato."
                            )
                        })

        if not signals and dc:
            signals.append({
                "type": "architectural_decision",
                "severity": "warning",
                "title": f"Decisione B commessa — {dc['hash']} ({dc['date']}): {dc['title']}",
                "desc": (
                    f"Commit {dc['hash']} introduce secrets_from_env=True come DEFAULT. "
                    f"Framing: 'opzionalmente disabilitabile' (opt-out). "
                    f"Nessun decision link creato. Nessun warning nel codice. "
                    f"Diff: " + dc["diff"]
                ),
            })

        # Signal 5: hidden coupling
        ve_rows = c.execute("""
            SELECT src_file, dst_file, co_change_rate, shared_commits
            FROM virtual_edges
            WHERE (src_file LIKE '%load/%' OR dst_file LIKE '%load/%')
              AND co_change_rate >= 0.35
            ORDER BY shared_commits DESC LIMIT 4
        """).fetchall()
        for r in ve_rows:
            a = r["src_file"].replace("\\", "/").split("/")[-1]
            b = r["dst_file"].replace("\\", "/").split("/")[-1]
            signals.append({
                "type": "hidden_coupling",
                "severity": "warning",
                "title": f"{a} ↔ {b} — accoppiamento nascosto ({r['co_change_rate']:.0%})",
                "desc": (
                    f"Co-cambiano nel {r['co_change_rate']:.0%} dei commit "
                    f"({r['shared_commits']} volte) senza import diretto. "
                    "Modifiche al protocollo di serializzazione si propagano silenziosamente."
                ),
            })

        signals.sort(key=lambda s: (
            s["type"] != "blast_radius",
            s["type"] != "amnesia",
            s["type"] != "architectural_decision",
            s["type"] != "intent_impl_delta",
            s["type"] != "pre_cve_warning",
            -(s.get("relevance_score", 0.0) or s.get("delta_score", 0.0)),
        ))

        stats = {
            "total_files":   c.execute("SELECT COUNT(*) FROM files").fetchone()[0],
            "total_symbols": c.execute("SELECT COUNT(*) FROM symbols").fetchone()[0],
            "total_commits": c.execute("SELECT COUNT(*) FROM commit_reasoning").fetchone()[0],
            "total_blast":   total_blast,
            "br_details":    br_details,
            "signals":       len(signals),
            "critical":      sum(1 for s in signals if s["severity"] == "critical"),
        }

    detection_score, factors = _compute_detection_score(signals, stats)
    stats["detection_score"] = detection_score
    stats["detection_factors"] = factors
    return {"cve_id": cve_id, "cfg": cfg, "signals": signals, "stats": stats}


def _compute_intent_delta(body: str, files_touched: str, embed_fn) -> float:
    try:
        v_intent = embed_fn(body[:512])
        v_impl   = embed_fn(files_touched[:512])
        if not v_intent or not v_impl:
            return -1.0
        dot = sum(a * b for a, b in zip(v_intent, v_impl))
        na  = sum(x * x for x in v_intent) ** 0.5
        nb  = sum(x * x for x in v_impl)   ** 0.5
        if na == 0 or nb == 0:
            return -1.0
        return round(1.0 - dot / (na * nb), 4)
    except Exception:
        return -1.0


def _build_cve_html(result: dict, project_name: str) -> str:
    import html as _h
    if "error" in result:
        return f"<html><body><pre>{result['error']}</pre></body></html>"

    cve_id  = result["cve_id"]
    cfg     = result["cfg"]
    signals = result["signals"]
    stats   = result["stats"]

    critical_count  = stats["critical"]
    warn_count      = stats["signals"] - critical_count
    detection_pct   = stats["detection_score"]
    det_factors     = stats["detection_factors"]

    _type_label = {
        "blast_radius":          "Blast Radius",
        "amnesia":               "Institutional Amnesia",
        "pre_cve_warning":       "Pre-CVE Warning",
        "architectural_decision":"Architectural Decision Gap",
        "hidden_coupling":       "Hidden Coupling",
        "intent_impl_delta":     "Intent-Implementation Delta",
    }
    _type_color = {
        "blast_radius":          "#EF4444",
        "amnesia":               "#8B5CF6",
        "pre_cve_warning":       "#F59E0B",
        "architectural_decision":"#EF4444",
        "hidden_coupling":       "#3B82F6",
        "intent_impl_delta":     "#7C3AED",
    }
    _sev_bg = {"critical": "#FEF2F2", "warning": "#FFFBEB"}
    _sev_border = {"critical": "#FCA5A5", "warning": "#FCD34D"}

    def _card(s: dict) -> str:
        col  = _type_color.get(s["type"], "#6B7280")
        bg   = _sev_bg.get(s["severity"], "#F9FAFB")
        bord = _sev_border.get(s["severity"], "#E5E7EB")
        rel_badge = ""
        if s["type"] == "intent_impl_delta":
            label = "Intent-Implementation Delta"
            col  = "#7C3AED"
            bg   = "#F5F3FF"
            bord = "#C4B5FD"
            delta_pct = int(s.get("delta_score", 0) * 100)
            rel_badge = (
                f'<span style="margin-left:8px;font-size:10px;font-weight:700;'
                f'color:#fff;background:#7C3AED;padding:2px 7px;'
                f'border-radius:4px;letter-spacing:.04em">'
                f'DELTA {delta_pct}%</span>'
            )
        elif s["type"] == "pre_cve_warning":
            sc = s.get("signal_class", "awareness")
            label = ("Segnale semantico pre-CVE" if sc == "semantic"
                     else "Security awareness (modulo adiacente)")
            col = "#D97706" if sc == "semantic" else "#6B7280"
            bg  = "#FFFBEB" if sc == "semantic" else "#F9FAFB"
            bord= "#FCD34D" if sc == "semantic" else "#E5E7EB"
            rel = s.get("relevance_score", None)
            if rel is not None:
                rel_pct = int(rel * 100)
                if rel >= 0.7:
                    rel_col = "#15803D"; rel_bg = "#DCFCE7"
                elif rel >= 0.4:
                    rel_col = "#B45309"; rel_bg = "#FEF9C3"
                else:
                    rel_col = "#9CA3AF"; rel_bg = "#F3F4F6"
                rel_badge = (
                    f'<span style="margin-left:8px;font-size:10px;font-weight:700;'
                    f'color:{rel_col};background:{rel_bg};padding:2px 7px;'
                    f'border-radius:4px;letter-spacing:.04em">REL {rel_pct}%</span>'
                )
        else:
            label = _type_label.get(s["type"], s["type"])
        title = _h.escape(s.get("title", ""))
        desc  = _h.escape(s.get("desc", ""))
        sym   = (f'<span style="font-size:11px;color:{col};font-weight:700;'
                 f'letter-spacing:.05em;text-transform:uppercase">{label}</span>'
                 + rel_badge)
        return (
            f'<div style="border:1px solid {bord};border-left:4px solid {col};'
            f'background:{bg};border-radius:8px;padding:16px 18px;margin-bottom:12px">'
            f'{sym}<div style="font-weight:600;font-size:15px;margin:6px 0 4px;color:#111">{title}</div>'
            f'<div style="font-size:13px;color:#555;line-height:1.6;white-space:pre-wrap">{desc}</div></div>'
        )

    cards_html = "".join(_card(s) for s in signals)

    br_rows = "".join(
        f'<tr>'
        f'<td style="padding:4px 12px 4px 0;font-family:monospace;color:#6366F1">{d["symbol"]}()</td>'
        f'<td style="padding:4px 8px 4px 0;color:#111;font-weight:600">{d["callers"]} file</td>'
        f'<td style="padding:4px 0;font-size:11px;color:{"#DC2626" if d.get("http_exposed",0) > 0 else "#94A3B8"};font-weight:600">'
        f'{"HTTP " + str(d["http_exposed"]) if d.get("http_exposed", 0) > 0 else "—"}'
        f'</td>'
        f'</tr>'
        for d in stats["br_details"]
    )

    _ALERT_THRESHOLD  = 55
    _REVIEW_THRESHOLD = 35
    if detection_pct >= _ALERT_THRESHOLD:
        verdict_color = "#EF4444"
        verdict_label_tag = "ALERT"
        verdict_text = "SI — Lore avrebbe alzato un alert pre-disclosure"
    elif detection_pct >= _REVIEW_THRESHOLD:
        verdict_color = "#F59E0B"
        verdict_label_tag = "REVIEW"
        verdict_text = "PROBABILMENTE — segnali presenti, revisione manuale raccomandata"
    else:
        verdict_color = "#22C55E"
        verdict_label_tag = "CLEAR"
        verdict_text = "NO — pattern non sufficiente per un alert automatico"

    template_path = Path(__file__).parent / "templates" / "cve_ui.html"
    html = template_path.read_text(encoding="utf-8")
    
    html = html.replace("{{CVE_ID}}", cve_id)
    html = html.replace("{{SEVERITY}}", _h.escape(cfg.get("severity", "")))
    html = html.replace("{{CVSS_SCORE}}", str(cfg.get("cvss_score", 0.0)))
    html = html.replace("{{NAME}}", _h.escape(cfg.get("name", "")))
    html = html.replace("{{DESCRIPTION}}", _h.escape(cfg.get("description", "")))
    html = html.replace("{{TOTAL_BLAST}}", str(stats.get("total_blast", 0)))
    html = html.replace("{{CRITICAL_COUNT}}", str(critical_count))
    html = html.replace("{{WARN_COUNT}}", str(warn_count))
    html = html.replace("{{VERDICT_COLOR}}", verdict_color)
    html = html.replace("{{DETECTION_PCT}}", str(detection_pct))
    html = html.replace("{{VERDICT_LABEL_TAG}}", verdict_label_tag)
    html = html.replace("{{VERDICT_TEXT}}", _h.escape(verdict_text))
    html = html.replace("{{SIGNALS_COUNT}}", str(stats.get("signals", 0)))
    html = html.replace("{{PROJECT_NAME}}", _h.escape(project_name))
    html = html.replace("{{ALERT_THRESHOLD}}", str(_ALERT_THRESHOLD))
    
    factors_rows = "".join(
        f'<tr><td style="padding:5px 8px 5px 0;color:#1E293B;font-weight:500">{_h.escape(f["label"])}</td>'
        f'<td style="text-align:right;font-family:monospace;color:#6366F1;font-weight:700">+{f["pts"]}</td>'
        f'<td style="padding:5px 0 5px 12px;color:#475569">{_h.escape(f["detail"])}</td></tr>'
        for f in det_factors
    )
    html = html.replace("{{FACTORS_ROWS}}", factors_rows)
    html = html.replace("{{DECISION_A}}", _h.escape(cfg.get("decision_a", "")))
    html = html.replace("{{DECISION_B}}", _h.escape(cfg.get("decision_b", "")))
    html = html.replace("{{BR_ROWS}}", br_rows)
    html = html.replace("{{SIGNALS_TOTAL_COUNT}}", str(len(signals)))
    html = html.replace("{{CARDS_HTML}}", cards_html)
    html = html.replace("{{TOTAL_SYMBOLS}}", f"{stats['total_symbols']:,}")
    html = html.replace("{{TOTAL_COMMITS}}", f"{stats['total_commits']:,}")
    
    return html


def _serve_cve_ui(project_root: Path, db_path: Path, cve_id: str) -> None:
    """Serve the unified dashboard in CVE retrospective mode."""
    result = _run_cve_retrospective(str(db_path), cve_id)
    if "error" in result:
        print(f"[cve] {result['error']}")
        return
    extra_data = {"cve_id": cve_id, "cve_results": result}
    from cli.diff_server import _serve_console
    _serve_console(project_root, db_path, "cve", 8787, extra_data)
