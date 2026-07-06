import json
from io import StringIO
from core.symbol_map import SymbolDB, SymbolRetriever

TOOLS = [
    {
        "name": "fow_search",
        "description": "Search for symbols by name keyword. Returns up to 20 matches.",
        "input_schema": {
            "type": "object",
            "properties": {"keyword": {"type": "string", "description": "Partial name keyword"}},
            "required": ["keyword"],
        },
    },
    {
        "name": "fow_frontier",
        "description": "Get symbol's body + dependency frontier. Small deps show signatures. Large deps marked [LAZY].",
        "input_schema": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string", "description": "Exact symbol name"},
                "depth": {"type": "integer", "description": "Depth (default 1)", "default": 1}
            },
            "required": ["symbol"],
        },
    },
    {
        "name": "fow_expand",
        "description": "Expand a LAZY symbol to get its full body.",
        "input_schema": {
            "type": "object",
            "properties": {"symbol": {"type": "string", "description": "Exact lazy symbol name"}},
            "required": ["symbol"],
        },
    },
    {
        "name": "write_staged_file",
        "description": "Write complete new content of a modified file to stage. Call once per file.",
        "input_schema": {
            "type": "object",
            "properties": {
                "relative_path": {"type": "string", "description": "File path relative to project root"},
                "content": {"type": "string", "description": "Complete new file content"},
                "reason": {"type": "string", "description": "One-line explanation"}
            },
            "required": ["relative_path", "content"],
        },
    },
    {
        "name": "done",
        "description": "Signal that all modifications have been written.",
        "input_schema": {
            "type": "object",
            "properties": {"summary": {"type": "string", "description": "Summary of changes"}},
            "required": ["summary"],
        },
    },
]

class FowExecutor:
    def __init__(self, retriever: SymbolRetriever, db: SymbolDB):
        self.retriever, self.db = retriever, db
        self.files_accessed, self.body_seen, self.sig_seen = set(), set(), set()

    def _fmt(self, block: dict) -> str:
        out = StringIO()
        name = block["symbol"]
        out.write(f"SYMBOL: {name}  [{block['kind']}]\nFILE:   {block['file']}  (lines {block['lines']})\n\n--- BODY ---\n{block['body']}\n")
        self.body_seen.add(name)
        self.sig_seen.discard(name)
        
        eager = [d for d in block["depends_on"] if not d.get("lazy")]
        lazy  = [d for d in block["depends_on"] if d.get("lazy")]
        new_eager = [d for d in eager if d["name"] not in self.body_seen and d["name"] not in self.sig_seen]
        known_eager = [d for d in eager if d["name"] in self.body_seen or d["name"] in self.sig_seen]

        if new_eager:
            out.write(f"\n--- EAGER DEPS (new: {len(new_eager)}) ---\n")
            for d in new_eager:
                loc = f"  @ {d['location']}" if d["location"] else ""
                out.write(f"  [{d['type']:14s}] {d['name']}{loc}\n")
                if d["signature"]:
                    out.write(f"               > {d['signature'].strip()}\n")
                self.sig_seen.add(d["name"])

        if known_eager:
            out.write(f"\n--- DEPS (already in context): {', '.join(d['name'] for d in known_eager)}\n")

        new_lazy = [d for d in lazy if d["name"] not in self.body_seen]
        if new_lazy:
            out.write(f"\n--- LAZY DEPS ({len(new_lazy)}) — use fow_expand only if needed ---\n")
            for d in new_lazy:
                loc = f"  @ {d['location']}" if d["location"] else ""
                out.write(f"  [LAZY {d['size']:>4} lines] {d['name']}{loc}\n")
                if d.get("methods"):
                    methods = ", ".join(d["methods"][:8])
                    if len(d["methods"]) > 8:
                        methods += f" ... +{len(d['methods'])-8}"
                    out.write(f"               methods: {methods}\n")

        new_cb = [c for c in block["called_by"] if c.get("caller") not in self.body_seen and c.get("caller") not in self.sig_seen]
        known_cb = [c for c in block["called_by"] if c.get("caller") in self.body_seen or c.get("caller") in self.sig_seen]

        if new_cb:
            out.write(f"\n--- CALLED BY (new: {len(new_cb)}) ---\n")
            for c in new_cb[:8]:
                out.write(f"  {c['caller'] or '(module)'}  @ {c['file']}:{c['line']}\n")

        if known_cb:
            out.write(f"--- CALLED BY (already known): {', '.join(c.get('caller', '?') for c in known_cb[:6])}\n")
        return out.getvalue()

    def search(self, keyword: str) -> str:
        rows = self.db.con.execute(
            "SELECT s.name, s.kind, s.line_start, s.line_end, f.path "
            "FROM symbols s JOIN files f ON s.file_id=f.id "
            "WHERE s.name LIKE ? ORDER BY s.kind, s.name LIMIT 20",
            (f"%{keyword}%",),
        ).fetchall()
        if not rows:
            return f"No symbols found matching '{keyword}'"
        lines = [f"{len(rows)} results for '{keyword}':"]
        for r in rows:
            lines.append(f"  [{r['kind']:8}] {r['name']:40s}  {r['path']}:{r['line_start']}-{r['line_end']}")
        return "\n".join(lines)

    def frontier(self, symbol: str, depth: int = 1) -> str:
        depth = max(1, min(depth, 3))
        result = self.retriever.traverse(symbol, max_depth=depth)
        for f in result["summary"].get("files_that_would_be_read", []):
            self.files_accessed.add(f)
        if not result["hops"]:
            return f"Symbol '{symbol}' not found. Try fow_search to discover the correct name."

        out = StringIO()
        for hop in result["hops"]:
            if hop.get("status") == "not_found":
                out.write(f"[hop {hop['hop']}] {hop['symbol']} — not found\n")
                continue

            hop_name, indent, marker = hop["symbol"], "  " * hop["depth"], ">" if hop["depth"] == 0 else "+"
            out.write(f"\n{indent}[{marker}] {hop_name}  [{hop.get('kind','')}]  {hop['file']} lines {hop.get('lines',0)}\n")

            if hop["depth"] == 0:
                block = self.retriever.get_symbol_block(hop_name)
                if block:
                    out.write(self._fmt(block))
            else:
                if hop_name in self.body_seen:
                    out.write(f"{indent}  [body already in context — skip]\n")
                elif hop_name in self.sig_seen:
                    out.write(f"{indent}  [signature already known — call fow_expand to get body]\n")
                else:
                    block = self.retriever.get_symbol_block(hop_name)
                    if block:
                        out.write(f"{indent}  body ({hop.get('lines',0)} lines):\n")
                        body_lines = block["body"].splitlines()
                        out.write("\n".join(f"{indent}  {l}" for l in body_lines[:20]))
                        if len(body_lines) > 20:
                            out.write(f"\n{indent}  ... [{len(body_lines)-20} more lines]\n")
                        out.write("\n")
                        self.sig_seen.add(hop_name)

            if hop.get("lazy_deps"):
                new_lazy = [ld for ld in hop["lazy_deps"] if ld["name"] not in self.body_seen]
                if new_lazy:
                    out.write(f"{indent}  [LAZY — available but not expanded]:\n")
                    for ld in new_lazy:
                        methods = f"  methods: {', '.join(ld['methods'][:5])}" if ld.get("methods") else ""
                        out.write(f"{indent}    {ld['name']}  [{ld['size']} lines]{methods}\n")

        s = result["summary"]
        out.write(f"\n[FOW STATS] {s['symbols_visited']} symbols visited, {s['total_lines_read']} lines read, {s['reduction_pct']}% reduction\n")
        return out.getvalue()

    def expand(self, symbol: str) -> str:
        block = self.retriever.get_symbol_block(symbol)
        if not block:
            return f"Symbol '{symbol}' not found."
        self.files_accessed.add(block["file"])
        return self._fmt(block)

    def full_file_tokens_if_read_whole(self) -> int:
        total = 0
        for rel in self.files_accessed:
            row = self.db.con.execute("SELECT lines FROM files WHERE path=?", (rel,)).fetchone()
            if row:
                total += (row["lines"] * 40) // 4
        return total
