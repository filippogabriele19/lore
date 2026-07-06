import sqlite3
import sys
import os
from pathlib import Path
from typing import Optional, Any

from core.symbol_db import SymbolDB

LAZY_THRESHOLD = 80



class SymbolRetriever:
    def __init__(self, db: SymbolDB, project_path: Path):
        self.db = db
        self.project_root = project_path

    def _read_lines(self, rel_path: str, start: int, end: int) -> str:
        """Legge esattamente le righe [start, end] di un file."""
        fpath = self.project_root / rel_path
        try:
            lines = fpath.read_text(encoding="utf-8", errors="replace").splitlines()
            chunk = lines[start - 1: end]
            return "\n".join(chunk)
        except Exception:
            return f"[cannot read {rel_path}:{start}-{end}]"

    def find_symbol(self, name: str) -> list[sqlite3.Row]:
        return self.db.con.execute(
            "SELECT s.*, f.path FROM symbols s JOIN files f ON s.file_id=f.id "
            "WHERE s.name=? ORDER BY s.kind",
            (name,)
        ).fetchall()

    def get_symbol_block(self, name: str, file_path: Optional[str] = None) -> Optional[dict]:
        """
        Ritorna il blocco di codice di un simbolo:
        body + dipendenze dirette (solo firme, non body).
        """
        rows = self.find_symbol(name)
        if not rows:
            return None
        if file_path:
            norm_path = file_path.replace("\\", "/").strip().lstrip("./").lower()
            filtered_rows = [
                r for r in rows
                if r["path"].replace("\\", "/").strip().lstrip("./").lower() == norm_path
            ]
            if filtered_rows:
                rows = filtered_rows
        # Preferisci function/method su variable
        row = next((r for r in rows if r["kind"] in ("function", "method")), rows[0])

        body = self._read_lines(row["path"], row["line_start"], row["line_end"])
        sym_id = row["id"]

        # Fetch all dependency details in a single query to avoid N+1 query problem
        deps_data = self.db.con.execute("""
            SELECT d.to_name, d.dep_type, s.signature, s.kind, f.path, s.line_start, s.line_end
            FROM deps d
            LEFT JOIN symbols s ON d.to_name = s.name
            LEFT JOIN files f ON s.file_id = f.id
            WHERE d.from_symbol_id = ?
            ORDER BY d.dep_type, d.to_name
        """, (sym_id,)).fetchall()

        grouped_deps = {}
        for r in deps_data:
            dname = r["to_name"]
            dtype = r["dep_type"]
            if dname not in grouped_deps or (grouped_deps[dname]["path"] is None and r["path"] is not None):
                grouped_deps[dname] = {
                    "to_name": dname,
                    "dep_type": dtype,
                    "signature": r["signature"],
                    "kind": r["kind"],
                    "path": r["path"],
                    "line_start": r["line_start"],
                    "line_end": r["line_end"]
                }

        dep_details = []
        for dname, info in sorted(grouped_deps.items(), key=lambda x: (x[1]["dep_type"], x[0])):
            dtype = info["dep_type"]
            has_sym = info["path"] is not None
            size = (info["line_end"] - info["line_start"] + 1) if has_sym else 0
            
            methods = []
            if has_sym and info["kind"] == "class" and size >= LAZY_THRESHOLD:
                method_rows = self.db.con.execute(
                    "SELECT name FROM symbols WHERE parent_class=? AND kind='method' "
                    "ORDER BY line_start",
                    (dname,)
                ).fetchall()
                methods = [mr["name"] for mr in method_rows]

            is_lazy = (
                dtype not in ("read_global", "write_global")
                and has_sym
                and size >= LAZY_THRESHOLD
            )

            dep_details.append({
                "name":      dname,
                "type":      dtype,
                "signature": info["signature"],
                "location":  f"{info['path']}:{info['line_start']}" if has_sym else None,
                "kind":      info["kind"] if has_sym else "external",
                "size":      size,
                "lazy":      is_lazy,
                "methods":   methods,
            })

        # Chi chiama questo simbolo (dipendenze inverse)
        callers = self.db.con.execute(
            "SELECT d.to_name, f.path, d.line, s.name as caller_name "
            "FROM deps d "
            "JOIN files f ON d.from_file_id=f.id "
            "LEFT JOIN symbols s ON d.from_symbol_id=s.id "
            "WHERE d.to_name=? LIMIT 10",
            (name,)
        ).fetchall()

        return {
            "symbol":    name,
            "kind":      row["kind"],
            "file":      row["path"],
            "lines":     f"{row['line_start']}-{row['line_end']}",
            "line_count": row["line_end"] - row["line_start"] + 1,
            "body":      body,
            "depends_on": dep_details,
            "called_by": [
                {"caller": r["caller_name"] or "(module)", "file": r["path"], "line": r["line"]}
                for r in callers
            ],
        }

    def compare(self, name: str) -> dict:
        """
        Confronta token usati con approccio file-intero vs approccio point-cloud.
        """
        block = self.get_symbol_block(name)
        if not block:
            return {"error": f"Symbol '{name}' not found"}

        # --- Approccio vecchio: quali file dovresti leggere? ---
        # Il file del simbolo + i file di tutte le sue dipendenze
        files_needed: set[str] = {block["file"]}
        for dep in block["depends_on"]:
            if dep["location"]:
                files_needed.add(dep["location"].split(":")[0])

        old_lines = 0
        old_token_estimate = 0
        file_details = []
        for rel in files_needed:
            row = self.db.con.execute(
                "SELECT lines FROM files WHERE path=?", (rel,)
            ).fetchone()
            n = row["lines"] if row else 0
            old_lines += n
            old_token_estimate += (n * 40) // 4  # ~40 chars/riga, 4 chars/token
            file_details.append({"file": rel, "lines": n})

        # --- Approccio nuovo: solo il blocco + firme delle dipendenze ---
        new_lines = block["line_count"]
        dep_sig_chars = sum(len(d["signature"] or "") for d in block["depends_on"])
        new_token_estimate = (new_lines * 40 + dep_sig_chars) // 4

        reduction_pct = 0
        if old_token_estimate > 0:
            reduction_pct = 100 * (1 - new_token_estimate / old_token_estimate)

        return {
            "symbol": name,
            "old_approach": {
                "files_to_read": file_details,
                "total_lines":   old_lines,
                "token_estimate": old_token_estimate,
            },
            "new_approach": {
                "symbol_lines":    block["line_count"],
                "dep_signatures":  len(block["depends_on"]),
                "token_estimate":  new_token_estimate,
            },
            "reduction_pct": round(reduction_pct, 1),
        }

    def traverse(self, start: str, max_depth: int = 3) -> dict:
        """
        Simula il traversal incrementale: parte da A, espande le dipendenze
        rilevanti hop per hop, tenendo conto dei token accumulati.

        Rappresenta il flusso reale: "capisco che ho bisogno di B, espando B,
        poi magari anche C" — ma mai un file intero.

        Ritorna il log di ogni hop con token cumulativi.
        """
        visited: set[str] = set()
        hops: list[dict] = []
        cumulative_tokens = 0
        cumulative_lines = 0

        # Calcola token totali se leggessi tutti i file coinvolti
        all_files_touched: set[str] = set()

        queue = [(start, 0, "root")]  # (symbol, depth, reason)

        while queue:
            name, depth, reason = queue.pop(0)
            if name in visited or depth > max_depth:
                continue
            visited.add(name)

            block = self.get_symbol_block(name)
            if not block:
                hops.append({
                    "hop": len(hops) + 1,
                    "depth": depth,
                    "symbol": name,
                    "reason": reason,
                    "status": "not_found",
                    "lines": 0,
                    "tokens": 0,
                    "cumulative_tokens": cumulative_tokens,
                })
                continue

            # Token di questo hop: body del simbolo (non le firme — già viste)
            hop_tokens = (block["line_count"] * 40) // 4
            if depth > 0:
                # Agli hop successivi aggiungiamo solo il body, le firme le avevamo già
                hop_tokens = (block["line_count"] * 40) // 4
            else:
                # Al primo hop includiamo anche le firme delle dipendenze
                sig_chars = sum(len(d["signature"] or "") for d in block["depends_on"])
                hop_tokens = (block["line_count"] * 40 + sig_chars) // 4

            cumulative_tokens += hop_tokens
            cumulative_lines += block["line_count"]
            all_files_touched.add(block["file"])

            # Separa le dipendenze in eager (auto-espandi) e lazy (ferma qui)
            eager_deps = [
                d for d in block["depends_on"]
                if not d["lazy"] and d["kind"] not in ("external",) and d["name"] not in visited
            ]
            lazy_deps = [
                d for d in block["depends_on"]
                if d["lazy"] and d["name"] not in visited
            ]

            hops.append({
                "hop": len(hops) + 1,
                "depth": depth,
                "symbol": name,
                "kind": block["kind"],
                "file": block["file"],
                "lines": block["line_count"],
                "reason": reason,
                "tokens_this_hop": hop_tokens,
                "cumulative_tokens": cumulative_tokens,
                "cumulative_lines": cumulative_lines,
                "eager_deps":  [d["name"] for d in eager_deps],
                "lazy_deps":   [
                    {"name": d["name"], "size": d["size"],
                     "kind": d["kind"], "methods": d.get("methods", [])}
                    for d in lazy_deps
                ],
                "globals_touched": [
                    d["name"] for d in block["depends_on"]
                    if d["type"] in ("read_global", "write_global")
                ],
            })

            # Accoda SOLO le dipendenze eager
            for dep in eager_deps[:5]:
                queue.append((dep["name"], depth + 1, f"dep of {name}"))

        # Calcola token se avessi letto tutti i file coinvolti per intero
        old_tokens = 0
        for rel in all_files_touched:
            row = self.db.con.execute(
                "SELECT lines FROM files WHERE path=?", (rel,)
            ).fetchone()
            if row:
                old_tokens += (row["lines"] * 40) // 4

        reduction = round(100 * (1 - cumulative_tokens / old_tokens), 1) if old_tokens else 0

        return {
            "start": start,
            "hops": hops,
            "summary": {
                "symbols_visited": len(visited),
                "total_lines_read": cumulative_lines,
                "total_tokens_point_cloud": cumulative_tokens,
                "total_tokens_file_approach": old_tokens,
                "files_that_would_be_read": list(all_files_touched),
                "reduction_pct": reduction,
            },
        }


# ---------------------------------------------------------------------------
# Rendering CLI
# ---------------------------------------------------------------------------

