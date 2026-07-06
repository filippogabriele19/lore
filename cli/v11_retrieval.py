import json
import logging
import re
from pathlib import Path

from core.symbol_map import SymbolDB, SymbolRetriever
from core.llm_client import get_llm_client
from cli.agent_stage import _extract_target_files
from cli.agent_retrieval import _cosine_sim, _get_embed_model, _get_data_containers

logger = logging.getLogger(__name__)

DECONSTRUCTOR_PROMPT = """Sei un Senior Software Architect. Il tuo compito è estrarre l'essenza di questo bug report per formulare delle parole chiave di ricerca ottimali.

BUG REPORT ORIGINALE:
{problem}

ISTRUZIONI:
1. Riscrivi mentalmente questo bug report in 5 maniere diverse, focalizzandoti rispettivamente su: 
   - Sintomi (cosa vede l'utente finale)
   - Architettura (layer del framework coinvolti)
   - Componenti (moduli specifici citati o implicati)
   - Flusso dei Dati (da dove entra l'input a dove si verifica l'errore)
   - Contesto Operativo / Edge Cases (condizioni specifiche in cui si verifica)
2. Fai una media semantica dei concetti emersi e restituisci SOLO ed esclusivamente le 3-5 query di ricerca testuali (max 5 parole ciascuna) ottimali per trovare il VERO file sorgente (root cause) usando un motore di ricerca per codice (BM25 / FTS). 
3. Ignora i file menzionati come "proposta di soluzione" dal bug-reporter se ti sembrano sbagliati architetturalmente.
4. Cerca i nomi delle classi interne e delle funzioni (es. se si parla di migrazioni e di serializzazione degli enum, tira fuori la parola "serializer").

Devi restituire ESATTAMENTE un array JSON di stringhe, ad esempio:
["query uno", "query due", "query tre"]
Non aggiungere testo fuori dal JSON.
"""

VERIFICATION_PROMPT = """Sei un Senior Software Architect. Hai ricevuto un bug report e un insieme di snippet di codice estratti dal repository che POTREBBERO contenere la causa alla radice del problema.

BUG REPORT:
{problem}

CANDIDATI ESTRATTI:
{snippets}

ISTRUZIONI:
1. Leggi attentamente ogni candidato. Molti potrebbero essere falsi positivi (hallucinations o corrispondenze puramente lessicali).
2. Scegli quali di questi candidati hanno la più alta probabilità di contenere il bug o di dover essere modificati per risolverlo.
3. Restituisci l'identificativo esatto del simbolo (es. "file.py::NomeClasse") per i top candidati.

Devi restituire ESATTAMENTE un array JSON di stringhe, in ordine di importanza decrescente (dal più rilevante al meno), ad esempio:
["django/db/models/query.py::QuerySet", "django/core/handlers/base.py::BaseHandler"]
Non aggiungere testo fuori dal JSON.
"""

def _run_deconstructor(task: str, project_root: Path) -> list[str]:
    """Stage 2B: Deconstructor (Semantic Prior)"""
    client = get_llm_client(project_root)
    prompt = DECONSTRUCTOR_PROMPT.format(problem=task)
    
    try:
        response = client.messages.create(
            model="default", # Use the user's preferred fast model (e.g. deepseek-chat or sonnet)
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )
        # Parse JSON array robustly
        text = response.content[0].text
        # extract json array from text if wrapped in markdown
        m = re.search(r'\[.*\]', text, re.DOTALL)
        if m:
            queries = json.loads(m.group(0))
            if isinstance(queries, list):
                return [str(q) for q in queries]
    except Exception as e:
        logger.warning(f"Deconstructor failed: {e}")
        
    return []

def _structural_seeding(task: str, db: SymbolDB) -> set[str]:
    """Stage 2A: Structural Seeding (Co-change graph)"""
    # Extract explicitly mentioned files
    explicit_files = _extract_target_files(task)
    norm_explicit = [f.replace("\\", "/") for f in explicit_files]
    
    candidates = set()
    try:
        from core.decision_linker import DecisionLinker
        dl = DecisionLinker(str(db.db_path))
        
        # 1. Add all symbols from explicit files
        for fpath in norm_explicit:
            rows = db.con.execute("SELECT name FROM symbols JOIN files ON symbols.file_id = files.id WHERE files.path = ?", (fpath,)).fetchall()
            for r in rows:
                candidates.add(f"{fpath}::{r['name']}")
                
        # 2. Use Intent Nodes / Hotspots to expand
        hotspots = dl.get_hotspot_files()
        for h in hotspots:
            if any(fpath in h["file_path"] for fpath in norm_explicit):
                # File is a hotspot, add its symbols
                pass # Already handled implicitly by explicit files, but we could expand to co-changed files here
                
    except Exception as e:
        logger.debug(f"Structural seeding failed: {e}")
        
    return candidates

def _candidate_pool(task: str, queries: list[str], db: SymbolDB) -> dict[str, str]:
    """Stage 3: Build Candidate Pool (BM25 + Semantic + Structural)"""
    candidates_scores = {}
    
    # 1. BM25 Search using Deconstructor queries
    k = 60
    for q in queries:
        fts_res = db.search_fts(q, limit=20)
        for rank, row in enumerate(fts_res):
            sym_key = f"{row['file_path'].replace(chr(92), '/')}::{row['name']}"
            score = 1.0 / (k + rank + 1)
            candidates_scores[sym_key] = candidates_scores.get(sym_key, 0) + score
            
    # 2. Add Original Semantic Search as baseline
    model = _get_embed_model()
    if model:
        task_vec = model.encode([task], normalize_embeddings=True, show_progress_bar=False)[0]
        all_emb = db.all_embeddings_with_role()
        data_containers = _get_data_containers(db)
        
        semantic_scored = []
        for name, emb_bytes, role, file_path, kind in all_emb:
            if emb_bytes:
                norm_path = file_path.replace("\\", "/")
                sym_key = f"{norm_path}::{name}"
                sim = _cosine_sim(emb_bytes, task_vec)
                if role == "test":
                    sim *= 0.5
                if kind == "class" and name in data_containers:
                    sim *= 0.5
                semantic_scored.append((sim, sym_key))
                
        semantic_ranked = sorted(semantic_scored, reverse=True)[:50]
        for rank, (sim, sym_key) in enumerate(semantic_ranked):
            score = 1.0 / (k + rank + 1)
            # Boost semantic by 0.6 relative to FTS
            candidates_scores[sym_key] = candidates_scores.get(sym_key, 0) + (score * 0.6)
            
    return candidates_scores

def _snippet_verification(task: str, top_candidates: list[str], retriever: SymbolRetriever) -> list[str]:
    """Stage 4: Snippet Verification (LLM Reading Comprehension)"""
    if not top_candidates:
        return []
        
    snippets_text = []
    for sym_key in top_candidates:
        if "::" in sym_key:
            fpath, sym_name = sym_key.split("::", 1)
            block = retriever.get_symbol_block(sym_name, fpath)
            if block:
                snippets_text.append(f"--- {sym_key} ---\n{block.get('signature', block.get('kind', ''))}")
                
    client = get_llm_client(retriever.project_root)
    prompt = VERIFICATION_PROMPT.format(
        problem=task,
        snippets="\n\n".join(snippets_text)
    )
    
    try:
        response = client.messages.create(
            model="default",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )
        text = response.content[0].text
        m = re.search(r'\[.*\]', text, re.DOTALL)
        if m:
            ranked = json.loads(m.group(0))
            if isinstance(ranked, list):
                return [str(r) for r in ranked]
    except Exception as e:
        logger.warning(f"Snippet verification failed: {e}")
        
    # Fallback to returning original top candidates if LLM fails
    return top_candidates[:5]

def _symbol_grounding_check(ranked_symbols: list[str], db: SymbolDB) -> list[str]:
    """Stage 5: Symbol Grounding Check (Deterministic)"""
    grounded = []
    for sym_key in ranked_symbols:
        if "::" not in sym_key:
            continue
        fpath, sym_name = sym_key.split("::", 1)
        # Check if it actually exists in DB
        row = db.con.execute(
            "SELECT 1 FROM symbols s JOIN files f ON s.file_id = f.id WHERE s.name = ? AND f.path = ?",
            (sym_name, fpath.replace("/", "\\"))
        ).fetchone()
        
        # Check with normalized path
        if not row:
            row = db.con.execute(
                "SELECT 1 FROM symbols s JOIN files f ON s.file_id = f.id WHERE s.name = ? AND f.path = ?",
                (sym_name, fpath)
            ).fetchone()
            
        if row:
            grounded.append(sym_key)
        else:
            logger.info(f"Hallucination dropped: {sym_key}")
            
    return grounded
class ContextBudget:
    def __init__(self, budget: int = 15000):
        self.budget = budget
        self.used = 0
        try:
            import tiktoken
            self.enc = tiktoken.get_encoding("cl100k_base")
        except ImportError:
            self.enc = None

    def count(self, text: str) -> int:
        if self.enc:
            return len(self.enc.encode(text))
        return int(len(text) / 3.5)

    def fits(self, text: str) -> bool:
        return self.used + self.count(text) <= self.budget

    def add(self, text: str):
        self.used += self.count(text)
        
    def truncate_ast(self, block: dict) -> str:
        body = block["body"]
        lines = body.splitlines()
        if len(lines) <= 40:
            return body
            
        try:
            import ast
            import textwrap
            
            # The body might have a base indentation. dedent removes common leading whitespace.
            dedented_body = textwrap.dedent(body)
            tree = ast.parse(dedented_body)
            
            valid_end_line = -1
            target_max_line = 40
            
            for node in tree.body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    if isinstance(node, ast.ClassDef):
                        # Ensure we at least keep the class signature
                        valid_end_line = max(valid_end_line, getattr(node, 'lineno', 1))
                        for child in node.body:
                            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                                if hasattr(child, "end_lineno") and child.end_lineno <= target_max_line:
                                    valid_end_line = max(valid_end_line, child.end_lineno)
                    else:
                        if hasattr(node, "end_lineno") and node.end_lineno <= target_max_line:
                            valid_end_line = max(valid_end_line, node.end_lineno)
            
            if valid_end_line > 0:
                truncated = lines[:valid_end_line] + ["\n    ... [AST TRUNCATED DUE TO BUDGET] ...\n"]
                return "\n".join(truncated)
        except Exception:
            pass
            
        # Fallback if parsing fails (e.g., non-Python or syntax error)
        truncated = lines[:25] + ["\n    ... [AST TRUNCATED DUE TO BUDGET] ...\n"] + lines[-10:]
        return "\n".join(truncated)

def v11_retrieve_context(
    task: str,
    db: SymbolDB,
    retriever: SymbolRetriever,
    token_budget: int = 15000,
) -> tuple[str, list[str]]:
    """Main entrypoint for LORE V11 Retrieval Architecture."""
    
    # 1. Input & Parallel Seeding
    structural_cands = _structural_seeding(task, db)
    deconstructor_queries = _run_deconstructor(task, retriever.project_root)
    logger.info(f"Deconstructor Queries: {deconstructor_queries}")
    
    # 3. Candidate Pool
    candidates_scores = _candidate_pool(task, deconstructor_queries, db)
    
    # Merge Structural (Boost score for explicit hits)
    for sym_key in structural_cands:
        candidates_scores[sym_key] = candidates_scores.get(sym_key, 0) + 0.5
        
    # Get top 30 candidates for Verification
    top_30_keys = [k for k, v in sorted(candidates_scores.items(), key=lambda item: item[1], reverse=True)[:30]]
    
    # 4. Snippet Verification (LLM)
    # We pass the top 30 signatures to the LLM to rank
    llm_ranked = _snippet_verification(task, top_30_keys, retriever)
    logger.info(f"LLM Ranked Candidates: {llm_ranked}")
    
    # 5. Grounding Check
    grounded_top = _symbol_grounding_check(llm_ranked, db)
    
    # Fallback to top_30 if grounding wiped everything
    if not grounded_top:
        logger.warning("Grounding check wiped all LLM suggestions. Falling back to heuristic top 5.")
        grounded_top = top_30_keys[:5]
        
    # 6. Micro-Agentic Refinement (TODO: Expand hop 1 dependencies of grounded_top)
    # For now, we will add the grounded top and their immediate dependencies
    
    # Build final bundle with Two-Pass Context Budget
    visited = set()
    bundle_parts = []
    budget = ContextBudget(token_budget)
    
    # PASS 1: Mandatory Top-K Grounded Symbols
    K = 2
    top_k_keys = grounded_top[:K]
    for sym_key in top_k_keys:
        if sym_key in visited:
            continue
        visited.add(sym_key)
        fpath, sym_name = sym_key.split("::", 1)
        
        block = retriever.get_symbol_block(sym_name, fpath)
        if not block:
            continue
            
        body_text = block["body"]
        # If it severely exceeds the budget, truncate it, but NEVER skip it.
        if budget.count(body_text) > budget.budget * 1.5:
            body_text = budget.truncate_ast(block)
            
        bundle_parts.append(
            f"SYMBOL: {sym_name}  [{block['kind']}]\n"
            f"FILE:   {block['file']}  (lines {block['lines']})\n"
            f"\n{body_text}\n"
        )
        budget.add(body_text)

    # PASS 2: Greedy remaining symbols and dependencies
    remaining_keys = grounded_top[K:]
    
    # Collect 1-hop dependencies from the top_k_keys to prioritize them
    dep_keys = []
    for sym_key in top_k_keys:
        fpath, sym_name = sym_key.split("::", 1)
        block = retriever.get_symbol_block(sym_name, fpath)
        if block:
            for dep in block.get("depends_on", []):
                dep_name = dep["name"]
                dep_rows = db.con.execute(
                    "SELECT f.path FROM symbols s JOIN files f ON s.file_id = f.id WHERE s.name = ?",
                    (dep_name,)
                ).fetchall()
                for r in dep_rows:
                    dep_key = f"{r['path'].replace(chr(92), '/')}::{dep_name}"
                    if dep_key not in visited and dep_key not in dep_keys:
                        dep_keys.append(dep_key)
                        
    # Evaluate dependencies then the rest of grounded symbols
    for sym_key in dep_keys + remaining_keys:
        if sym_key in visited:
            continue
            
        fpath, sym_name = sym_key.split("::", 1)
        block = retriever.get_symbol_block(sym_name, fpath)
        if not block:
            continue
            
        body_text = block["body"]
        
        if budget.fits(body_text):
            # Fits perfectly
            bundle_parts.append(
                f"SYMBOL: {sym_name}  [{block['kind']}]\n"
                f"FILE:   {block['file']}  (lines {block['lines']})\n"
                f"\n{body_text}\n"
            )
            budget.add(body_text)
            visited.add(sym_key)
        else:
            # Doesn't fit, truncate greedily instead of `continue` (which drops it)
            trunc_text = budget.truncate_ast(block)
            if budget.fits(trunc_text):
                bundle_parts.append(
                    f"SYMBOL: {sym_name}  [{block['kind']}]\n"
                    f"FILE:   {block['file']}  (lines {block['lines']})\n"
                    f"\n{trunc_text}\n"
                )
                budget.add(trunc_text)
                visited.add(sym_key)

    bundle = "\n".join(f"{'─'*60}\n{p}" for p in bundle_parts)
    return bundle, list(visited)
