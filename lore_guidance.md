# LORE — Architectural Compliance & Knowledge Graph Daemon
## CLAUDE.md v6.1 — Documento Operativo per Claude Code / Cursor

> **Questo file ha priorità assoluta su qualsiasi altra fonte di istruzioni.**
> Leggilo per intero prima di scrivere qualsiasi codice.

---

## §0 Knowledge Graph — Navigazione Codebase

Il progetto ha un KG sempre aggiornato in `.lore/lore.db` (SQLite).
**Usa sempre il DB per trovare simboli, metodi e dipendenze — non Grep/Read a freddo.**

```bash
# Dove è definito X?
sqlite3 .lore/lore.db "SELECT f.path, s.line_start FROM symbols s JOIN files f ON s.file_id=f.id WHERE s.name='X'"

# Tutti i metodi di una classe
sqlite3 .lore/lore.db "SELECT name, line_start FROM symbols WHERE parent_class='X' ORDER BY line_start"

# Chi chiama X?
sqlite3 .lore/lore.db "SELECT f.path, sc.call_line, caller.name FROM symbol_calls sc JOIN symbols caller ON sc.caller_symbol_id=caller.id JOIN files f ON caller.file_id=f.id WHERE sc.callee_name='X'"
```

Usa Read solo per leggere il codice che devi effettivamente modificare.
Il DB viene aggiornato automaticamente dopo ogni modifica (hook PostToolUse).

---

## §0 Stack Tecnologico — Chiarimento Obbligatorio

**Il codebase attuale è Python (MVP).** Scanner, parser, planner, worker, LSP, MCP e CLI sono implementati in Python.
**Il target architetturale finale è TypeScript.** Le istruzioni con esempi TypeScript in questo documento sono il riferimento per il futuro rewrite.
**Non mescolare mai le due tecnologie nello stesso modulo.**

Quando implementi un nuovo modulo:
- Se è MVP/prototipo → Python, segui `core/` e `cli/` in `PROJECT_STRUCTURE.md`
- Se è rewrite enterprise → TypeScript, segui la gerarchia `libs/` ed esempi correlati.

---

## §0bis Architettura del Prodotto — Form Factor

LORE è un **compliance daemon e context provider centrale per agenti AI esterni** (es. Cursor, Claude Code, Copilot o programmatori umani). Il server ospita la logica e risponde a richieste provenienti da client thin (CLI, estensione IDE).

LORE **non scrive codice direttamente** e non effettua modifiche dirette al codice in produzione dell'utente; funge invece da:
1. **Fornitore di contesto (MCP/LSP)**: Inietta regole architetturali (ADR) e convenzioni direttamente nei prompt dell'agente AI.
2. **Gatekeeper/Firewall (Git Hook)**: Analizza staticamente e in memoria i diff proposti (counterfactual) per bloccare commit che introducono regressioni o vulnerabilità.

### I tre client (tutti thin)

| Client | Chi lo usa | Quando costruirlo / Stato |
|--------|-----------|-------------------|
| **CLI** — `lore check-vuln` / `lore benchmark` | Developer, demo, CI/CD | Già nel MVP — Funzionale |
| **VS Code extension** — pannello laterale | Developer — visualizza stats e alert | Già nel MVP — Connesso all'LSP |
| **Web dashboard** — `localhost:8080` o hosted | CTO/manager — vede ROI, KG, hotspot | Abbozzato (Genera report HTML statici) |

---

## §0ter Stato Implementativo — Matrice Realtà vs Piano

| Invariante | Stato | Note |
|------------|-------|------|
| LSP-FIRST | ✅ Implementato | Server Python (JSON-RPC) + client VS Code |
| MCP-COMPLIANT | ✅ Implementato | MCP Server con strumenti per iniezione di ADR |
| KG IS THE PRODUCT | ✅ Implementato | 5 layer, SQLite, decision linker, mining git |
| COMPLIANCE FIREWALL | ✅ Implementato | Pre-commit hook con `check-vuln --patch-staged` |
| COUNTERFACTUAL VALIDATION | ✅ Implementato | Simulazione in-memory di patch prima del commit |
| GIT MINING AS INTENT | ✅ Implementato | GitMiner + hotspot + regression mapping |
| CROSS-LANGUAGE TAINT | ✅ Implementato | Scansione taint su Python, TypeScript e Go |
| ZERO-SETUP PLAYGROUND | ✅ Implementato | Database pre-costruito per test immediati |

---

## §1 Identità del Prodotto

LORE è la **piattaforma di memoria istituzionale per il codice enterprise**.

Non è un tool di autocomplete. Non è un assistente per developer singoli.
È il sistema che garantisce la **compliance architetturale quando gli LLM scrivono codice**.
Cattura la storia, le decisioni, le convenzioni e il perché dietro ogni scelta architetturale.

**La differenza con i competitor:**
- Cursor/Copilot sanno **cosa** fa il codice.
- LORE sa **perché** il codice è fatto così — e impone questi vincoli all'LLM in esecuzione.

---

## §2 Architettura Core — Non Derogabile

### Principio Modulare Assoluto

Ogni modulo (LSP, MCP, SCANNER, VULN_ANALYSIS, BENCHMARK) è indipendente.
I moduli comunicano attraverso interfacce pulite ed esportazioni controllate.
Nessun file di logica supera il limite di **300 righe**. Se un file cresce oltre, va scomposto.

**Gerarchia di dipendenze permessa:**
```
shared (costanti, helper)
  ↑
core (symbol_map, ast_taint, decision_linker, git_miner)
  ↑
cli (lsp, mcp_server, check_vuln, benchmark, agent_runner)
  ↑
lore.py (CLI entrypoint)
```

### Le Invarianti Correnti (Non Violare Mai)

| # | Invariante | Conseguenza della Violazione |
|---|-----------|------------------------------|
| 1 | LSP-FIRST: Tutta la logica di telemetria e calcolo nel server. | UI dell'estensione accoppiata con logica pesante |
| 2 | KG IS THE PRODUCT: Ogni resoconto, ricerca o mining legge/scrive sul KG. | Frammentazione della conoscenza, database inconsistente |
| 3 | COMPLIANCE FIREWALL: Il Git Hook blocca commit insicuri o non documentati. | Regressioni di sicurezza introdotte in produzione |
| 4 | COUNTERFACTUAL VALIDATION: Simulazione dei diff in memoria con AST Taint. | Falsi negativi, patch inefficaci che passano il controllo |
| 5 | DECISION CONTEXT: Ogni query o compliance tool inietta le ADR governanti. | L'agente AI viola le regole di design del progetto |
| 6 | GIT MINING OBBLIGATORIO: L'analisi di hotspot fa parte dello SCAN di base. | Perdita di segnali storici sull'attività dei file |
| 7 | CROSS-LANGUAGE BY DEFAULT: Supporto nativo a Python, Go e TS. | Impossibile analizzare flussi di dati trans-frontalieri |

---

## §3 Regole Assolute con Esempi

### 3.1 Import tra Moduli

```python
# ❌ SBAGLIATO — import incrociati o moduli non facciate
from core.symbol_extractor import _extract_go_file
from core.symbol_scanner import _run_scan

# ✅ GIUSTO — import pulito tramite la facciata core.symbol_map
from core.symbol_map import SymbolDB, scan
```

### 3.2 Staging delle Modifiche Simulatorie

Durante le simulazioni counterfactual o i cicli di auto-healing, il codice viene scritto in memoria o in directory di staging controllate per non sporcare il codice in produzione:

```python
# ❌ SBAGLIATO — scrivere direttamente sul file di produzione dell'utente
with open(project_file, 'w') as f:
    f.write(patched_code)

# ✅ GIUSTO — usare StageWriter o simulare in-memory
writer = StageWriter(project_root)
writer.stage_file(relative_path, patched_code)
# esegui test sandbox in directory temporanea...
writer.cleanup()
```

### 3.3 Dimensione File

```
Nessun file supera 300 righe.
Se ti avvicini al limite: il file fa troppe cose. Dividilo in moduli più piccoli.
```

---

## §4 Modulo SCAN & KNOWLEDGE BASE — `core/`

**Responsabilità:** Analisi del codebase. Costruzione e aggiornamento del Knowledge Graph su tutti i livelli L1-L5.

- `symbol_db.py`: Layer di persistenza SQLite (`SymbolDB`).
- `symbol_extractor.py`: Parser AST multilingua (Python, TypeScript, Go).
- `symbol_retriever.py`: Ricerca semantica A*, espansione BFS del call graph.
- `symbol_scanner.py`: Orchestrator incrementale (ri-indicizza solo i file modificati via hash).
- `decision_linker.py`: Mappatura di regole ADR o commit significativi ai simboli del codice.
- `git_miner.py`: Calcolo degli hotspot basato sulla frequenza di commit.

---

## §5 Modulo COMPLIANCE INTERFACES — `cli/`

**Responsabilità:** Esporre la compliance architetturale ed interagire con i client esterni e gli agenti.

- `lsp.py`: Server Language Server Protocol su standard I/O (JSON-RPC). Gestisce l'hover (mostra le ADR relative a un simbolo sotto il cursore), il calcolo in tempo reale delle diagnostiche di taint e l'invio delle statistiche (`lore/getStats`).
- `mcp_server.py`: Model Context Protocol server. Espone tool semantici ad agenti AI esterni per recuperare decisioni rilevanti per un dato task.
- `check_vuln.py`: CLI e motore di pre-commit per rilevare flussi di taint esposti, severe amnesia (moduli critici senza ADR) e deviazioni architetturali.
- `patch_validator.py`: Simulatore in-memory dei diff. Applica le patch su AST virtuali per verificare se i flussi di vulnerabilità originari vengono interrotti (Verdict: CURED, SURVIVED, REGRESSION).
- `benchmark.py`: LORE Security Compliance Benchmark. Misura quantitativamente la compliance di un agente AI con e senza l'iniezione delle ADR del KG.

---

## §6 Workflow Obbligatorio per Nuove Feature

**Segui questo ordine. Non saltare passi. Non cambiare l'ordine.**

1. **Definisci i contratti/tipi** in `shared/` o nelle facciate core.
2. **Implementa la logica** in `core/` o `cli/`, tenendo ogni file rigorosamente sotto le 300 righe.
3. **Esponi il comando/servizio** in `lore.py` in modo pulito con Rich per l'interfaccia utente.
4. **Scrivi un test unitario** che fallisce in `tests/` e implementa il codice fino al pass.
5. **Esegui pytest**: Assicurati che l'intera suite di test sia verde (`venv\Scripts\pytest tests/`).
6. **Aggiorna CLAUDE.md** solo se hai modificato una invariante o introdotto un target V2.
