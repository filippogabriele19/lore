from __future__ import annotations
from pathlib import Path
from datetime import datetime as _dt
from cli.shared import console


def _cure_decay_and_amnesia(project_root: Path, conn, decay_events: list[dict], amnesia_hotspots: list[dict]) -> tuple[int, int]:
    """Cures detected architectural decay and amnesia hotspots by generating draft ADR files and indexing them."""
    console.print("[info]🔮 Running LORE Auto-Cure...[/]")
    adr_dir = project_root / ".lore" / "adr"
    adr_dir.mkdir(parents=True, exist_ok=True)
    
    # 1. Cure Decay Events
    cured_decay_count = 0
    for e in decay_events:
        h = e["hash"]
        adr_filename = f"adr_decay_{h}.md"
        adr_path = adr_dir / adr_filename
        
        symbol_name = "global"
        for f in e["files"]:
            if f.endswith((".py", ".go")):
                symbol_name = f.replace("\\", "/").split("/")[-1]
                break
        
        adr_content = f"""# ADR: Cure Architectural Drift for Commit {h}

## Metadata
- **Date**: {_dt.now().strftime("%Y-%m-%d")}
- **Source Commit**: {h}
- **Author**: {e["author"]}
- **Date of Commit**: {e["date"]}
- **Files Touched**: {", ".join(e["files"])}

## Context
This commit was identified as a potential architectural drift/decay event because it modified critical security-sensitive files with description:
> {e["body"]}

## Decision
[Draft] Verify that the changes conform to the security and design invariants of the touched files. Ensure no temporary bypass or workaround remains undocumented.

## Consequences
Developers must review this change to ensure alignment with project standards.
"""
        adr_path.write_text(adr_content, encoding="utf-8")
        
        conn.execute(
            "INSERT INTO decision_links (symbol_name, source_type, source_ref, confidence, description) "
            "VALUES (?, ?, ?, ?, ?)",
            (symbol_name, "adr", h, 1.0, f"Cure Architectural Drift: {e['body']}")
        )
        cured_decay_count += 1
        
    # 2. Cure Severe Amnesia Hotspots
    cured_amnesia_count = 0
    for h_spot in amnesia_hotspots:
        f_path = h_spot["path"]
        symbol_name = f_path.replace("\\", "/").split("/")[-1]
        try:
            row_sym = conn.execute(
                "SELECT name FROM symbols WHERE file_id = (SELECT id FROM files WHERE path = ? OR path = ?) "
                "AND kind IN ('class', 'function', 'method') LIMIT 1",
                (f_path, f_path.replace("/", "\\"))
            ).fetchone()
            if row_sym:
                symbol_name = row_sym["name"]
        except Exception as e:
            console.print(f"[warning]⚠️ Failed to fetch symbol for amnesia cure: {e}[/]")
            
        clean_basename = f_path.replace("\\", "_").replace("/", "_").replace(".", "_")
        adr_filename = f"adr_amnesia_{clean_basename}.md"
        adr_path = adr_dir / adr_filename
        
        adr_content = f"""# ADR: Document Critical Subsystem {f_path}

## Metadata
- **Date**: {_dt.now().strftime("%Y-%m-%d")}
- **Target File**: {f_path}
- **Target Symbol**: {symbol_name}
- **Status**: Proposed

## Context
This file performs sensitive security actions (sinks) and has high commit activity ({h_spot["change_freq"]} commits), but lacked documented design decisions.

## Decision
[Draft] Document the design rules and security invariants governing {symbol_name} to prevent institutional amnesia.

## Consequences
Clears the Severe Amnesia warning and ensures design constraints are indexed.
"""
        adr_path.write_text(adr_content, encoding="utf-8")
        
        conn.execute(
            "INSERT INTO decision_links (symbol_name, source_type, source_ref, confidence, description) "
            "VALUES (?, ?, ?, ?, ?)",
            (symbol_name, "adr", f".lore/adr/{adr_filename}", 1.0, f"Cure Severe Amnesia: {f_path}")
        )
        cured_amnesia_count += 1
        
    conn.commit()
    console.print(f"[success]✔ Auto-Cure complete! Generated and indexed {cured_decay_count} decay ADRs and {cured_amnesia_count} amnesia ADRs.[/]\n")
    return cured_decay_count, cured_amnesia_count
