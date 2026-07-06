from __future__ import annotations
import re
import json
import html as _html
from pathlib import Path


def _read_template(filename: str) -> str:
    """Read a template file from cli/templates/."""
    template_path = Path(__file__).parent / "templates" / filename
    if not template_path.exists():
        raise FileNotFoundError(f"Template not found: {template_path}")
    return template_path.read_text(encoding="utf-8")


def _html_escape(s: str) -> str:
    return s.replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")


def _build_diff_html(task: str, result: dict) -> str:
    """Build a self-contained two-panel HTML diff viewer."""
    s      = result["stats"]
    staged = result["staged_files"]

    raw_diff = result.get("diff") or ""
    diff_js  = (
        raw_diff
        .replace("\\", "\\\\")
        .replace("`", "\\`")
        .replace("${", "\\${")
    )

    sidebar_items = "".join(
        f'<div class="fi" title="{_html_escape(f["path"])}">'
        f'<span class="fi-dot" style="background:#22C55E"></span>'
        f'<span class="fi-name">{_html_escape(f["path"])}</span>'
        f'</div>'
        for f in staged
    )
    
    risk = result.get("risk", 50)
    risk_hex = "#22C55E" if risk <= 40 else ("#F59E0B" if risk <= 70 else "#EF4444")
    risk_label = "Diff Preview Mode — Contained Impact"
    if risk > 40 and risk <= 70:
        risk_label = "Caution — Review modifications before applying"
    elif risk > 70:
        risk_label = "High risk changes — Review carefully"

    file_count = len(staged)
    sym_count = s.get("symbols", "—")
    link_count = s.get("links", "—")
    hot_count = s.get("hotspots", "—")
    analysis_html = f"<pre><code>{_html_escape(raw_diff)}</code></pre>"
    risk_map_html = ""

    html = _read_template("diff_ui.html")
    html = html.replace("{{RISK}}", str(risk))
    html = html.replace("{{RISK_HEX}}", risk_hex)
    html = html.replace("{{RISK_LABEL}}", risk_label)
    html = html.replace("{{SIDEBAR_ITEMS}}", sidebar_items)
    html = html.replace("{{FILE_COUNT}}", str(file_count))
    html = html.replace("{{TASK_ESC}}", _html_escape(task))
    html = html.replace("{{SYM_COUNT}}", str(sym_count))
    html = html.replace("{{LINK_COUNT}}", str(link_count))
    html = html.replace("{{HOT_COUNT}}", str(hot_count))
    html = html.replace("{{ANALYSIS_HTML}}", analysis_html)
    html = html.replace("{{RISK_MAP_HTML}}", risk_map_html)

    return html


def _build_impact_html(task: str, analysis: str, stats: dict | None = None) -> str:
    """Build a self-contained two-panel HTML impact analysis viewer (LORE design)."""
    task_esc = _html.escape(task)
    stats = stats or {}
    sym_count  = stats.get("symbols", "—")
    link_count = stats.get("links", "—")
    hot_count  = stats.get("hotspots", "—")

    risk = 50
    m = re.search(r"RISK\s+SCORE[:\s]+(\d+)", analysis, re.IGNORECASE)
    if m:
        risk = min(100, max(0, int(m.group(1))))
    else:
        nums = re.findall(r"\b(7[0-9]|8[0-9]|9[0-9]|100)\b", analysis)
        if nums:
            risk = int(nums[0])

    risk_hex = "#22C55E" if risk <= 40 else ("#F59E0B" if risk <= 70 else "#EF4444")
    if risk <= 40:
        risk_label = "Safe to proceed — contained impact"
    elif risk <= 70:
        risk_label = "Caution — review dependencies before proceeding"
    else:
        risk_label = "High risk — wide blast radius, requires review"

    _HIGH = ["high", "critical", "critico", "tier 1", "rottura immediata",
             "breaks immediately", "direct breakage", "core", "epicenter",
             "alto rischio", "severe", "breaking"]
    _MED  = ["medium", "warning", "warn", "moderate", "watch", "indirect",
             "tier 2", "attenzione", "dipendenza", "dependency"]

    _file_pat = re.compile(
        r"[\w./\\-]+\.(?:py|js|ts|tsx|html|yml|yaml|json|txt|md)",
        re.IGNORECASE,
    )
    raw_files: dict[str, str] = {}
    seen_keys: set[str] = set()
    for fm in _file_pat.finditer(analysis):
        path = fm.group(0).replace("\\", "/")
        key  = path.lower()
        if key in seen_keys:
            continue
        seen_keys.add(key)
        ctx = analysis[max(0, fm.start() - 300):fm.end() + 300].lower()
        if any(h in ctx for h in _HIGH):
            raw_files[path] = "critical"
        elif any(w in ctx for w in _MED):
            raw_files[path] = "watch"
        else:
            raw_files[path] = "dep"

    _sev_ord = {"critical": 0, "watch": 1, "dep": 2}
    seen_files = dict(sorted(raw_files.items(), key=lambda x: _sev_ord[x[1]]))

    def _inline_md(s: str) -> str:
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"`(.+?)`", r'<code class="ic">\1</code>', s)
        return s

    def _md_to_html(text: str) -> str:
        out: list[str] = []
        in_pre = in_ul = in_table = False
        for ln in text.split("\n"):
            if ln.startswith("```"):
                if in_pre:
                    out.append("</code></pre>"); in_pre = False
                else:
                    if in_ul: out.append("</ul>"); in_ul = False
                    if in_table: out.append("</tbody></table>"); in_table = False
                    out.append('<pre><code>'); in_pre = True
                continue

            if in_pre:
                out.append(_html.escape(ln))
                continue

            if ln.strip().startswith("|") and not ln.strip().startswith("|---"):
                if not in_table:
                    if in_ul: out.append("</ul>"); in_ul = False
                    out.append('<table class="md-table"><thead>')
                    parts = [p.strip() for p in ln.split("|")[1:-1]]
                    out.append("<tr>" + "".join(f"<th>{_inline_md(p)}</th>" for p in parts) + "</tr>")
                    out.append("</thead><tbody>")
                    in_table = True
                else:
                    parts = [p.strip() for p in ln.split("|")[1:-1]]
                    out.append("<tr>" + "".join(f"<td>{_inline_md(p)}</td>" for p in parts) + "</tr>")
                continue
            elif in_table and (not ln.strip() or ln.strip().startswith("|---") or not ln.strip().startswith("|")):
                if ln.strip().startswith("|---"):
                    continue
                out.append("</tbody></table>")
                in_table = False

            if ln.strip().startswith("- ") or ln.strip().startswith("* "):
                if not in_ul:
                    if in_table: out.append("</tbody></table>"); in_table = False
                    out.append("<ul>")
                    in_ul = True
                content_li = ln.strip()[2:]
                out.append(f"<li>{_inline_md(content_li)}</li>")
                continue
            elif in_ul and not ln.strip().startswith("- ") and not ln.strip().startswith("* "):
                out.append("</ul>")
                in_ul = False

            if ln.startswith("## "):
                out.append(f"<h2>{_inline_md(ln[3:])}</h2>")
                continue
            if ln.startswith("### "):
                out.append(f"<h3>{_inline_md(ln[4:])}</h3>")
                continue

            if ln.strip().startswith(">"):
                val_c = ln.strip().lstrip("> ")
                out.append(f'<div class="callout">{_inline_md(val_c)}</div>')
                continue

            if ln.strip():
                out.append(f"<p>{_inline_md(ln)}</p>")

        if in_pre: out.append("</code></pre>")
        if in_ul: out.append("</ul>")
        if in_table: out.append("</tbody></table>")
        return "\n".join(out)

    for idx, (path, _) in enumerate(seen_files.items()):
        esc_path = _html.escape(path)
        analysis = re.sub(
            rf"\b{re.escape(path)}\b",
            f'<a id="file-{idx}" class="fa">{esc_path}</a>',
            analysis,
            1,
        )

    def _sev_dot(sev: str) -> str:
        colors = {"critical": "#EF4444", "watch": "#F59E0B", "dep": "#22C55E"}
        return f'<span class="fi-dot" style="background:{colors.get(sev, "#22C55E")}"></span>'

    def _sev_tag(sev: str) -> str:
        return f'<span class="fi-tag tag-{sev}">{sev}</span>'

    sidebar_items = "".join(
        f'<div class="fi" onclick="goToFile({idx})" title="{_html.escape(path)}">'
        f'{_sev_dot(sev)}'
        f'<span class="fi-name">{_html.escape("/".join(path.split("/")[-2:]))}</span>'
        f'{_sev_tag(sev)}'
        f'</div>\n'
        for idx, (path, sev) in enumerate(seen_files.items())
    )
    file_count = len(seen_files)
    analysis_html = _md_to_html(analysis)
    analysis_js = json.dumps(analysis)

    html = _read_template("impact_ui.html")
    html = html.replace("{{RISK}}", str(risk))
    html = html.replace("{{RISK_HEX}}", risk_hex)
    html = html.replace("{{RISK_LABEL}}", risk_label)
    html = html.replace("{{SIDEBAR_ITEMS}}", sidebar_items)
    html = html.replace("{{FILE_COUNT}}", str(file_count))
    html = html.replace("{{TASK_ESC}}", task_esc)
    html = html.replace("{{SYM_COUNT}}", str(sym_count))
    html = html.replace("{{LINK_COUNT}}", str(link_count))
    html = html.replace("{{HOT_COUNT}}", str(hot_count))
    html = html.replace("{{ANALYSIS_HTML}}", analysis_html)
    html = html.replace("{{ANALYSIS_JS}}", analysis_js)

    return html


def _build_audit_html(findings: list[dict], stats: dict, project_name: str) -> str:
    """Render the full audit report as a self-contained HTML page."""
    from datetime import datetime as _dt

    n_critical = sum(1 for f in findings if f["severity"] == "critical")
    n_warning  = sum(1 for f in findings if f["severity"] == "warning")
    n_info     = sum(1 for f in findings if f["severity"] == "info")
    date_str   = _dt.now().strftime("%d %b %Y · %H:%M")

    _CAT_LABELS = {
        "compound_risk":         "Compound Risk",
        "institutional_amnesia": "Institutional Amnesia",
        "virtual_edge":          "Hidden Coupling",
        "provider_desync":       "Provider Desync",
        "intent_drift":          "Intent Drift",
        "commit_amnesia":        "Commit Amnesia",
        "structural_spof":       "Structural SPOF",
        "no_test_coverage":      "No Test Coverage",
        "high_coupling":         "High Coupling",
    }

    _CAT_ICON = {
        "compound_risk":         "&#9888;",
        "institutional_amnesia": "&#129504;",
        "virtual_edge":          "&#128279;",
        "provider_desync":       "&#9889;",
        "intent_drift":          "&#127744;",
        "commit_amnesia":        "&#128237;",
        "structural_spof":       "&#128165;",
        "no_test_coverage":      "&#128680;",
        "high_coupling":         "&#128376;",
    }

    from collections import OrderedDict as _OD
    cats = _OD()
    for f in findings:
        cats.setdefault(f["category"], []).append(f)

    nav_items = ""
    for cat, items in cats.items():
        crit = sum(1 for i in items if i["severity"] == "critical")
        warn = sum(1 for i in items if i["severity"] == "warning")
        badge_color = "#EF4444" if crit > 0 else "#F59E0B"
        badge_n = crit or warn
        icon = _CAT_ICON.get(cat, "•")
        label = _CAT_LABELS.get(cat, cat)
        nav_items += (
            f'<a class="nav-item" href="#{cat}">'
            f'<span class="nav-icon">{icon}</span>'
            f'<span class="nav-label">{_html.escape(label)}</span>'
            f'<span class="nav-badge" style="background:{badge_color}">{badge_n}</span>'
            f'</a>\n'
        )

    sections_html = ""
    for cat, items in cats.items():
        label = _CAT_LABELS.get(cat, cat)
        sections_html += f'<section class="audit-section" id="{cat}"><h2>{_html.escape(label)}</h2>'
        for f in items:
            c = "tag-critical" if f["severity"] == "critical" else ("tag-watch" if f["severity"] == "warning" else "tag-dep")
            metric_html = f'<div class="card-metric">Metric: {f["metric"]}</div>' if f.get("metric") else ""
            
            files_html = ""
            for path in f.get("files", []):
                files_html += f'<div class="card-file">{_html.escape(path)}</div>'
            for path in f.get("secondary_files", []):
                files_html += f'<div class="card-file secondary">{_html.escape(path)}</div>'
                
            dot_color = "#EF4444" if f["severity"] == "critical" else ("#F59E0B" if f["severity"] == "warning" else "#22C55E")
            sections_html += (
                f'<div class="card">'
                f'  <div class="card-header">'
                f'    <div class="card-dot" style="background:{dot_color}"></div>'
                f'    <span class="card-title">{_html.escape(f["title"])}</span>'
                f'    <span class="fi-tag {c}">{f["severity"]}</span>'
                f'  </div>'
                f'  {metric_html}'
                f'  {files_html}'
                f'  <div class="card-desc">{_html.escape(f["description"])}</div>'
                f'</div>'
            )
        sections_html += '</section>'

    html = _read_template("audit_ui.html")
    html = html.replace("{{PROJ}}", _html.escape(project_name))
    html = html.replace("{{DATE_STR}}", date_str)
    html = html.replace("{{SYM}}", str(stats.get("symbols", 0)))
    html = html.replace("{{LNK}}", str(stats.get("links", 0)))
    html = html.replace("{{HOT}}", str(stats.get("hotspots", 0)))
    html = html.replace("{{VE}}", str(stats.get("virtual_edges", 0)))
    html = html.replace("{{N_CRITICAL}}", str(n_critical))
    html = html.replace("{{N_WARNING}}", str(n_warning))
    html = html.replace("{{N_INFO}}", str(n_info))
    html = html.replace("{{N_TOTAL}}", str(n_critical + n_warning + n_info))
    html = html.replace("{{NAV_ITEMS}}", nav_items)
    html = html.replace("{{SECTIONS_HTML}}", sections_html)

    return html


def _build_unified_dashboard_html(project_name: str, project_root: Path, db_path: Path, mode: str, extra_data: dict) -> str:
    from cli.diff_server import _get_console_data_dict
    data_dict = _get_console_data_dict(project_name, project_root, db_path, mode, extra_data)
    data_json = json.dumps(data_dict, ensure_ascii=False)
    
    html = _read_template("dashboard_ui.html")
    return html.replace("{{LORE_DATA_JSON}}", data_json)
