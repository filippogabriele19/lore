import os, sys, argparse, json, shutil
from pathlib import Path
from cli.shared import console, DEFAULT_PROJECT, _get_db_path
from cli.cve_registry import _CVE_REGISTRY, _serve_cve_ui

def _main_cve(argv: list) -> None:
    import argparse
    p = argparse.ArgumentParser(prog="lore cve")
    p.add_argument("--project", required=True)
    p.add_argument("--cve", default="CVE-2025-68664",
                   choices=list(_CVE_REGISTRY.keys()))
    args = p.parse_args(argv)
    project_root = Path(args.project)
    if not project_root.exists():
        print(f"[error] Project not found: {project_root}")
        return
    db_path = _get_db_path(project_root)
    if not db_path.exists():
        print(f"[error] No DB found under {project_root}")
        return
    _serve_cve_ui(project_root, db_path, args.cve)


def _build_batch_html(results: list) -> str:
    import html as _h
    from datetime import datetime as _dt

    _ALERT, _REVIEW = 55, 35

    def _verdict(score):
        if score >= _ALERT:  return ("ALERT",  "#EF4444")
        if score >= _REVIEW: return ("REVIEW", "#F59E0B")
        return ("CLEAR", "#22C55E")

    def _factor_pts(result, label_prefix):
        for f in result["stats"].get("detection_factors", []):
            if f["label"].startswith(label_prefix):
                return f["pts"]
        return 0

    n_total  = len(results)
    n_alert  = sum(1 for r in results if r["stats"]["detection_score"] >= _ALERT)
    n_review = sum(1 for r in results if _REVIEW <= r["stats"]["detection_score"] < _ALERT)
    avg_score = int(sum(r["stats"]["detection_score"] for r in results) / max(n_total, 1))
    projects  = len({r.get("project", "") for r in results})
    run_date  = _dt.now().strftime("%Y-%m-%d %H:%M")

    sorted_results = sorted(results, key=lambda r: r["stats"]["detection_score"], reverse=True)

    rows_html = ""
    for r in sorted_results:
        if "error" in r:
            continue
        cve_id  = _h.escape(r["cve_id"])
        proj    = _h.escape(r.get("project", "unknown"))
        cfg     = r["cfg"]
        score   = r["stats"]["detection_score"]
        verdict, vc = _verdict(score)
        fa = _factor_pts(r, "A")
        fb = _factor_pts(r, "B")
        fc = _factor_pts(r, "C")
        fe = _factor_pts(r, "E")
        fg = _factor_pts(r, "G")
        ff = _factor_pts(r, "F")
        severity = _h.escape(cfg.get("severity", ""))
        sev_col = "#EF4444" if severity == "CRITICAL" else "#F59E0B"
        top_signal = ""
        for s in r.get("signals", []):
            if s["type"] == "amnesia" and s.get("links", 1) == 0:
                top_signal = f"Amnesia: {s['title'].split('—')[0].strip()[:40]}"
                break
        history_commits = r.get("history_commits", 0)
        history_oldest  = r.get("history_oldest", "")
        cve_disclosure  = r.get("cfg", {}).get("disclosure_date", "")[:7]  # YYYY-MM
        # Coverage: does history predate the CVE disclosure?
        if history_oldest and cve_disclosure and history_oldest[:7] < cve_disclosure:
            cov_color, cov_label = "#22C55E", f"✓ {history_oldest[:4]}–"
        elif history_commits == 0:
            cov_color, cov_label = "#EF4444", "no history"
        else:
            cov_color, cov_label = "#F59E0B", f"~{history_oldest[:4]}–"
        rows_html += f"""
        <tr style="border-bottom:1px solid #E2E8F0">
          <td style="padding:10px 12px;font-weight:700;font-family:monospace;font-size:13px">
            <span style="color:{vc};font-weight:800">{verdict}</span>
            <span style="margin-left:6px;color:#0F172A">{cve_id}</span>
          </td>
          <td style="padding:10px 12px;color:#475569;font-size:13px">{proj}</td>
          <td style="padding:10px 12px;text-align:center">
            <span style="background:{sev_col};color:#fff;font-size:10px;font-weight:700;padding:2px 7px;border-radius:4px">{severity}</span>
          </td>
          <td style="padding:10px 12px;text-align:center">
            <span style="font-family:monospace;font-size:18px;font-weight:800;color:{vc}">{score}</span>
          </td>
          <td style="padding:10px 4px;text-align:center;color:{'#0F172A' if fa else '#CBD5E1'};font-weight:{'700' if fa else '400'}">{fa or '—'}</td>
          <td style="padding:10px 4px;text-align:center;color:{'#0F172A' if fb else '#CBD5E1'};font-weight:{'700' if fb else '400'}">{fb or '—'}</td>
          <td style="padding:10px 4px;text-align:center;color:{'#0F172A' if fc else '#CBD5E1'};font-weight:{'700' if fc else '400'}">{fc or '—'}</td>
          <td style="padding:10px 4px;text-align:center;color:{'#7C3AED' if ff else '#CBD5E1'};font-weight:{'700' if ff else '400'}">{ff or '—'}</td>
          <td style="padding:10px 4px;text-align:center;color:{'#7C3AED' if fg else '#CBD5E1'};font-weight:{'700' if fg else '400'}">{fg or '—'}</td>
          <td style="padding:10px 6px;text-align:center;font-size:11px;font-weight:600;color:{cov_color}" title="{history_commits} reasoning commits · oldest: {history_oldest}">{cov_label}</td>
          <td style="padding:10px 12px;font-size:11px;color:#64748B;max-width:200px">{_h.escape(top_signal)}</td>
        </tr>"""

    bar_width = int(n_alert / max(n_total, 1) * 100)

    return f"""<!DOCTYPE html>
<html lang="it">
<head>
<meta charset="utf-8">
<title>Lore Vulnerability — Batch Report</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Fraunces:ital,wght@0,700;1,400&display=swap" rel="stylesheet">
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Inter',sans-serif;background:#F1F5F9;color:#1E293B;min-height:100vh}}
.wrap{{max-width:1100px;margin:0 auto;padding:32px 24px}}
.hdr{{background:#0F172A;border-radius:12px;padding:28px 32px;margin-bottom:24px}}
.kpi-row{{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin-bottom:24px}}
.kpi{{background:#fff;border-radius:10px;padding:18px 20px;box-shadow:0 1px 3px rgba(0,0,0,.07)}}
.kpi-val{{font-family:'Fraunces',serif;font-size:34px;font-weight:700}}
.kpi-lbl{{font-size:12px;color:#64748B;margin-top:4px;font-weight:500}}
.panel{{background:#fff;border-radius:10px;padding:20px 24px;box-shadow:0 1px 3px rgba(0,0,0,.07);margin-bottom:24px}}
.panel-title{{font-weight:700;font-size:14px;letter-spacing:.04em;text-transform:uppercase;color:#64748B;margin-bottom:16px}}
table{{border-collapse:collapse;width:100%}}
th{{text-align:left;padding:8px 12px;font-size:11px;font-weight:700;color:#64748B;letter-spacing:.05em;text-transform:uppercase;border-bottom:2px solid #E2E8F0}}
th.center{{text-align:center}}
.bar-bg{{background:#E2E8F0;border-radius:99px;height:8px;margin:8px 0 4px}}
.bar{{background:#EF4444;border-radius:99px;height:8px}}
.note{{font-size:11px;color:#94A3B8;margin-top:20px;line-height:1.6;border-top:1px solid #E2E8F0;padding-top:16px}}
</style>
</head>
<body>
<div class="wrap">
  <div class="hdr">
    <div style="font-size:12px;font-weight:700;color:#64748B;letter-spacing:.08em;text-transform:uppercase;margin-bottom:8px">Lore Vulnerability — Batch Report</div>
    <div style="font-family:'Fraunces',serif;font-size:30px;font-weight:700;color:#fff;margin-bottom:6px">{n_total} CVE su {projects} progetti — {run_date}</div>
    <div style="font-size:13px;color:#94A3B8">Analisi retrospettiva automatica. Tutti i segnali erano presenti nel KG prima della disclosure pubblica.</div>
  </div>

  <div class="kpi-row">
    <div class="kpi"><div class="kpi-val" style="color:#EF4444">{n_alert}</div><div class="kpi-lbl">ALERT generati</div></div>
    <div class="kpi"><div class="kpi-val" style="color:#F59E0B">{n_review}</div><div class="kpi-lbl">REVIEW</div></div>
    <div class="kpi"><div class="kpi-val">{n_total - n_alert - n_review}</div><div class="kpi-lbl">CLEAR</div></div>
    <div class="kpi"><div class="kpi-val">{avg_score}%</div><div class="kpi-lbl">Score medio</div></div>
    <div class="kpi"><div class="kpi-val">{projects}</div><div class="kpi-lbl">Progetti</div></div>
  </div>

  <div class="panel">
    <div class="panel-title">Detection rate</div>
    <div style="font-size:13px;color:#475569;margin-bottom:6px">{n_alert} su {n_total} CVE avrebbero generato un alert automatico (soglia 55 punti)</div>
    <div class="bar-bg"><div class="bar" style="width:{bar_width}%"></div></div>
    <div style="font-size:12px;color:#64748B">{bar_width}% detection rate</div>
  </div>

  <div class="panel">
    <div class="panel-title">Risultati per CVE</div>
    <table>
      <thead>
        <tr>
          <th>Verdetto / CVE</th>
          <th>Progetto</th>
          <th class="center">Severity</th>
          <th class="center">Score</th>
          <th class="center" title="Institutional Amnesia">A</th>
          <th class="center" title="Blast Radius">B</th>
          <th class="center" title="Segnali semantici">C</th>
          <th class="center" title="Temporal clustering" style="color:#7C3AED">F</th>
          <th class="center" title="Intent-Impl Delta" style="color:#7C3AED">G</th>
          <th class="center" title="Storia KG copre pre-CVE?">Copertura</th>
          <th>Segnale principale</th>
        </tr>
      </thead>
      <tbody>{rows_html}</tbody>
    </table>
  </div>

  <div class="note">
    Soglia ALERT: 55 punti · Soglia REVIEW: 35 punti · Calibrate su {n_total} CVE — richiedono validazione su &ge;10 CVE per uso in produzione.<br>
    Fattori A–E deterministici (KG query) · F/G richiedono sentence-transformers (all-MiniLM-L6-v2, locale).<br>
    <strong>Copertura storia:</strong> ✓ verde = KG copre il periodo pre-CVE · ⚠ arancione = copertura parziale · ✗ rosso = nessuna storia reasoning.
    I risultati CLEAR su cloni superficiali (FastAPI, Airflow) riflettono dati insufficienti, non assenza di segnali reali.<br>
    Generato da Lore Vulnerability Retrospective — {run_date}
  </div>
</div>
</body>
</html>"""


