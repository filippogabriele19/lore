# LORE — Project Structure v6.0
## Mappa completa del progetto con responsabilità per file

> **Come usare questo documento**
> Ogni file ha: percorso, stato, responsabilità, contratto pubblico, dipendenze.
> Prima di creare un file nuovo: cercalo qui. Se non c'è, aggiungilo prima di scriverlo.
> Se modifichi le responsabilità di un file: aggiorna questo documento.

> **Stack:** Python MVP. Target finale TypeScript. Non mescolare.

---

## Legenda stati

| Simbolo | Significato |
|---------|-------------|
| ✅ | Implementato e testato |
| 🔨 | In sviluppo |
| 📋 | Placeholder — struttura definita, da implementare |
| 🔒 | Non toccare senza ADR |

---

## Struttura Root

```
ase/
├── PROJECT_STRUCTURE.md     ← questo file
├── CLAUDE.md                ← istruzioni per Claude Code (leggi sempre)
├── README.md                ← documentazione pubblica
├── requirements.txt         ← dipendenze Python
├── pyproject.toml           ← configurazione progetto, linting, testing
├── .env.example             ← variabili d'ambiente richieste (mai committare .env)
│
├── core/                    ← moduli core di LORE
│   ├── scanner/             ← MODULO SCAN (L1-L3 + git mining L4)
│   ├── parsers/             ← parser per linguaggio
│   ├── knowledge/           ← Knowledge Graph — query, retrieval, decision linking
│   ├── planner/             ← MODULO PLAN
│   ├── worker/              ← MODULO WORKER
│   ├── safety/              ← MODULO SAFETY
│   ├── loop/                ← MODULO LOOP (orchestrator)
│   ├── brain/               ← LLM Gateway
│   ├── platform/            ← FileTransaction, OCC, Sandbox
│   ├── compliance/          ← AuditEvent, Policy Engine
│   └── telemetry/           ← ROI, Session Replay
│
├── shared/                  ← contratti, tipi, schemi condivisi
│   ├── contracts.py         ← interfacce pubbliche tra moduli
│   ├── schemas.py           ← Pydantic schemas per validazione output LLM
│   ├── types.py             ← tipi condivisi (Result, ScanResult, ecc.)
│   └── errors.py            ← eccezioni custom del sistema
│
├── server/                  ← API HTTP (FastAPI) — cervello esposto via REST + LSP + MCP
│   ├── api.py               ← app FastAPI, router principale, server MCP
│   ├── routes/              ← endpoint per modulo
│   └── middleware/          ← auth, logging, rate limit

│
├── clients/                 ← client thin — zero logica di business
│   ├── cli/                 ← CLI (MVP attuale — `lore scan .` / `lore apply "..."`)
│   ├── vscode/              ← VS Code extension (pannello laterale, TypeScript)
│   └── dashboard/           ← Web dashboard (CTO/manager — ROI, KG, hotspot)
│
├── tests/                   ← tutti i test
│   ├── unit/                ← test unitari per modulo
│   ├── integration/         ← test di integrazione tra moduli
│   └── eval/                ← golden dataset — 10 task di riferimento
│
└── docs/
    └── adr/                 ← Architecture Decision Records
        ├── ADR-001-module-boundaries.md
        ├── ADR-002-kg-schema.md
        ├── ADR-003-llm-vs-deterministic.md
        ├── ADR-004-file-transaction.md
        ├── ADR-005-confidence-scoring.md
        ├── ADR-006-lsp-first.md
        └── ADR-007-decision-linking.md   ← NUOVO
```

---

## `shared/` — Contratti e Tipi Condivisi

> **Regola:** gli altri moduli importano DA qui. MAI importare tra moduli direttamente.

---

### `shared/contracts.py` 📋
**È:** l'unico punto di comunicazione tra moduli.
**Fa:** definisce le interfacce pubbliche di ogni modulo come dataclass.
**Espone pubblicamente:**
```python
class ScanContract       # .trigger(ScanOptions) -> ScanResult
class PlanContract       # .trigger(PlanOptions) -> PlanResult
class WorkContract       # .trigger(WorkOptions) -> WorkResult
class SafetyContract     # .trigger(SafetyOptions) -> SafetyResult
class LoopContract       # .trigger(LoopOptions) -> LoopResult
class DecisionContract   # .get_context(symbols) -> DecisionContext  ← NUOVO
```
**Dipendenze:** solo `shared/types.py`

---

### `shared/types.py` 📋
**È:** definizione di tutti i tipi di dato che attraversano il sistema.
**Espone pubblicamente:**
```python
@dataclass ScanOptions
@dataclass ScanResult
@dataclass ScanStats

@dataclass PlanOptions
@dataclass PlanResult
@dataclass PlanChange
@dataclass StructuredPlan

@dataclass WorkOptions
@dataclass WorkResult
@dataclass Artifact
@dataclass ExecutionExplanation   # ← NUOVO: citazione fonti KG

@dataclass SafetyOptions
@dataclass SafetyResult
@dataclass VerificationReport
@dataclass StructuredFeedback

@dataclass LoopOptions
@dataclass LoopResult
@dataclass CompletionSignal
@dataclass ContextAnchor          # include decision_constraints ← AGGIORNATO

@dataclass SemanticDiff
@dataclass ConflictPrediction

# NUOVI per Decision Linking
@dataclass DecisionLink           # link tra decisione e simbolo
@dataclass DecisionContext        # tutti i vincoli per un set di simboli
@dataclass DecisionConstraint     # singolo vincolo (warning, must-use, avoid)
@dataclass GitCommitReasoning     # commit con ragionamento estratto
@dataclass HotspotAnalysis        # file ad alto rischio da git analysis
```
**Dipendenze:** nessuna (solo stdlib)

---

### `shared/schemas.py` 📋
**Espone pubblicamente:**
```python
class PlanDraftSchema
class PlanValidatedSchema
class ModifyOutputSchema
class SafetyScoreSchema
class CompletionSchema
class DecisionExtractionSchema    # ← NUOVO: LLM estrae vincoli da ADR
class CommitReasoningSchema       # ← NUOVO: LLM identifica commit significativi
```
**Dipendenze:** `pydantic`, `shared/types.py`

---

### `shared/errors.py` 📋
**Espone pubblicamente:**
```python
class LOREError
class LowConfidenceError
class OCCConflictError
class ScanError
class PlanError
class WorkError
class SafetyError
class SchemaValidationError
class PolicyViolationError
class MigrationError
class DecisionConstraintViolationError  # ← NUOVO: vincolo ADR violato
```

---

## `core/scanner/` — Modulo SCAN

> **Invariante:** SCAN non modifica mai file del progetto cliente.
> **Invariante:** SCAN non chiama mai LOOP, PLAN, WORKER o BRAIN.
> **Invariante:** scan() è idempotente.
> **Invariante:** il git mining L4 è obbligatorio, non opzionale.

---

### `core/scanner/__init__.py` 📋
```python
from core.scanner.scanner import ScanContract
__all__ = ["ScanContract"]
```

---

### `core/scanner/scanner.py` ✅
**È:** implementazione principale del modulo SCAN.
**Fa:**
- Scansione incrementale del filesystem (L1)
- Estrazione simboli tramite parser (L2)
- Gestione DB SQLite con migration system
- Orchestrazione di git_miner per L4
- Tracking sessioni scan per audit e debug
- Cleanup automatico di file cancellati
**Espone pubblicamente:**
```python
class ScanContract
    .trigger(ScanOptions) -> ScanResult
    .get_db_path(ScanOptions) -> Path
def get_symbols_without_embedding(db_path, limit) -> list[dict]
def get_file_symbols(db_path, file_path) -> list[dict]
def get_scan_stats(db_path) -> dict
```
**Dipendenze:** `core/parsers/`, `core/scanner/git_miner.py`, `shared/types.py`, `shared/errors.py`

---

### `core/scanner/db.py` 📋
**È:** layer di accesso al database.
**Fa:**
- Gestione connessioni SQLite (WAL mode, foreign keys, cache)
- Sistema di migration (additive only — ADR-002)
- Query helpers per gli altri moduli
**Espone pubblicamente:**
```python
def get_connection(db_path) -> sqlite3.Connection
def apply_migrations(conn) -> None
def load_db_state(conn) -> dict
def cleanup_deleted(conn, db_state, current_files) -> int
```

---

### `core/scanner/git_miner.py` 📋
**È:** mining del repository git per costruire il L4 del Knowledge Graph.
**Fa:**
- Estrae commit history completa con metadati
- Identifica commit con ragionamento significativo (corpo > 100 chars, keyword: "because", "decided", "avoid", "warning", "tradeoff", "do not", "never")
- Calcola co-change patterns: coppie di file committate insieme frequentemente
- Calcola hotspot: file con alta frequenza modifica × alta complessità ciclomatica
- Estrae mention esplicite di ADR/decisioni nel codice (`# ADR-003`, `// see decision:`)
- Mappa ownership reale per file/directory (blame semantico aggregato)

**Espone pubblicamente:**
```python
def mine_git_history(repo_path: str, db_path: str) -> GitMiningResult
def get_commit_reasoning(repo_path: str, since_days: int = 365) -> list[GitCommitReasoning]
def get_cochange_patterns(db_path: str, min_cooccurrence: int = 3) -> list[CochangePair]
def get_hotspots(db_path: str, top_n: int = 20) -> list[HotspotAnalysis]
def get_mention_links(repo_path: str) -> list[MentionLink]
def get_ownership_map(repo_path: str) -> dict[str, OwnershipRecord]
```
**Dipendenze:** `gitpython`, `shared/types.py`, `shared/errors.py`
**NON dipende da:** PLAN, WORKER, SAFETY, BRAIN, LOOP
**Note performance:** mine_git_history può richiedere minuti su repo grandi. Gira in background, non blocca il task. Risultati cachati nel DB, aggiornati weekly.

---

### `core/scanner/file_watcher.py` 📋
**È:** watch mode incrementale.
**Fa:**
- Monitora il filesystem con `watchdog`
- Su modifica file: triggera scan incrementale solo per quel file
- Debounce per evitare scan multipli su save rapidi
**Espone pubblicamente:**
```python
class FileWatcher
    .start(project_root: str, db_path: str) -> None
    .stop() -> None
```

---

## `core/knowledge/` — Knowledge Graph

> **Invariante:** il KG è append-only per dati storici e decisionali.
> **Invariante:** MAI esporre dati cross-tenant.

---

### `core/knowledge/knowledge_base.py` 📋
**È:** interfaccia principale di accesso al KG.
**Fa:**
- Query per simboli, dipendenze, ownership
- Hybrid retrieval pipeline (embedding + graph + historical + decisional)
- Attach decision context a ogni simbolo nei risultati
**Espone pubblicamente:**
```python
class KnowledgeBase
    .query_symbols(query: str, top_k: int) -> list[Symbol]
    .retrieve_context(task: str, budget_lines: int) -> RetrievalResult
    .attach_decision_context(symbols: list[Symbol]) -> list[SymbolWithDecisions]
    .get_hotspots(top_n: int) -> list[HotspotAnalysis]
    .get_cochange_partners(file_path: str) -> list[CochangePair]
```

---

### `core/knowledge/decision_linker.py` 📋
**È:** costruisce e mantiene i link tra decisioni architetturali e simboli del codice.
**Fa:**
- Meccanismo 1 (Mention detection): cerca pattern `# ADR-XXX` nel codice
- Meccanismo 2 (Git bridge): trova commit vicini alla data ADR con keyword match
- Meccanismo 3 (Semantic embedding): cosine similarity tra chunk ADR e simboli
- Combina le confidenze dei tre meccanismi
- Gestisce link "probabili non verificati" per revisione umana
- Alimenta il Data Flywheel con le conferme umane

**Espone pubblicamente:**
```python
class DecisionLinker
    .link_document(doc_path: str, doc_type: str, db_path: str) -> list[DecisionLink]
    .get_decision_context(symbol_ids: list[str]) -> DecisionContext
    .confirm_link(link_id: str, confirmed: bool) -> None  # human feedback
    .get_unverified_links(min_confidence: float = 0.30) -> list[DecisionLink]

def compute_link_confidence(
    mention_found: bool,
    git_bridge_score: float,
    semantic_score: float
) -> float
```
**Dipendenze:** `core/knowledge/embeddings.py`, `core/scanner/git_miner.py`, `shared/types.py`

---

### `core/knowledge/embeddings.py` 📋
**È:** generazione e storage di embedding vettoriali.
**Fa:**
- Genera embedding per simboli (nome + firma + docstring)
- Genera embedding per chunk di documenti decisionali (ADR, spec, PR)
- Batch processing (mai uno alla volta)
- Storage in SQLite con sqlite-vss o fallback numpy

**Espone pubblicamente:**
```python
def generate_symbol_embeddings(symbols: list[dict], db_path: str) -> None
def generate_document_embeddings(doc_path: str, db_path: str) -> list[str]  # chunk ids
def cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float
def search_similar(query_vec: list[float], db_path: str, top_k: int) -> list[SimilarityResult]
```

---

### `core/knowledge/retrieval.py` 📋
**È:** hybrid retrieval pipeline.
**Fa:**
- Step 1: embedding search top-K
- Step 2: BFS graph expansion
- Step 3: historical boost (co-change)
- Step 4: decision context attachment ← NUOVO
- Step 5: risk flags (hotspot, warnings)
- Step 6: budget allocator (max 800 righe all'LLM)

---

### `core/knowledge/alias_detector.py` 📋
**È:** rileva simboli duplicati con nomi diversi.
**Fa:**
- Cerca nel KG simboli con similarity > 0.85 in file correlati
- Warning obbligatorio nel piano se trovato

---

## `core/planner/` — Modulo PLAN

> **Invariante:** PLAN legge il KG ma non lo modifica.
> **Invariante:** ogni PlanChange include il decision_context dei simboli coinvolti.

---

### `core/planner/planner.py` 📋
**Orchestratore delle 4 fasi P1-P4.**

### `core/planner/draft.py` 📋
**P1:** LLM Heavy → plan_draft (PlanDraftSchema)

### `core/planner/enricher.py` 📋
**P2:** deterministico → plan_enriched.
**NUOVO:** include chiamata a `decision_linker.get_decision_context()` per ogni simbolo del draft.

### `core/planner/validator.py` 📋
**P3:** LLM Medium → plan_validated.
**NUOVO:** il prompt include i decision constraints estratti in P2.

### `core/planner/preflight.py` 📋
**P4:** deterministico → pre_flight check (OCC, blast radius, approval, budget).

### `core/planner/repository.py` 📋
**Query helper:** estrae dal KG i simboli reali per ogni change nel draft.

---

## `core/worker/` — Modulo WORKER

> **Invariante:** fs.write diretti sono VIETATI. Sempre FileTransaction.
> **Invariante:** ogni WorkResult include ExecutionExplanation con fonti citate.

---

### `core/worker/worker.py` 📋
**Orchestratore:** esegue le strategie nell'ordine corretto.
**NUOVO:** dopo l'esecuzione, costruisce `ExecutionExplanation` citando ADR, pattern, hotspot.

### `core/worker/strategies.py` 📋
**Gerarchia:** AST extraction → fuzzy SEARCH/REPLACE → LLM generation.

### `core/worker/semantic_diff.py` 📋
**Produce:** diff semantico (non testuale) per ogni artifact modificato.

---

## `core/safety/` — Modulo SAFETY

### `core/safety/safety.py` 📋
**Orchestratore V1-V7.**

### `core/safety/verifiers.py` 📋
**V1-V4:** deterministici (sintassi, import, OCC, test).
**V6:** ← NUOVO: verifica che nessun DecisionConstraint sia stato violato.

### `core/safety/feedback_builder.py` 📋
**Costruisce:** StructuredFeedback per l'iterazione successiva del LOOP.

---

## `core/brain/` — LLM Gateway

### `core/brain/llm_gateway.py` 📋
**Espone:**
```python
class LLMGateway
    .generate(prompt: str, schema: Type[T], model: ModelTier) -> T
    .get_cost_summary() -> CostSummary

class ModelTier(Enum)
    HEAVY    # Claude Opus — plan draft, worker
    MEDIUM   # Claude Sonnet — validate, safety semantico
    LIGHT    # Claude Haiku — metadata, label, classificazione
    LOCAL    # Ollama — air-gap deployment
```

### `core/brain/prompt_builder.py` 📋
**Fa:**
- Template per ogni tipo di chiamata
- Inietta sempre `context_anchor` + `decision_constraints` nel prompt
- Anti-injection: codice cliente dentro tag `<CODE>...</CODE>`
**NUOVO:**
```python
def build_plan_prompt(task, context, anchor, decision_context) -> str
def build_modify_prompt(change, source_code, anchor, decision_context) -> str
def build_safety_prompt(artifact, objective, conventions, decision_context) -> str
# decision_context è obbligatorio in tutti e tre
```

---

## `core/platform/` — FileTransaction e OCC

> **Invariante:** fs.write diretti sono VIETATI in tutto il codebase.

### `core/platform/file_transaction.py` 📋
```python
class FileTransaction
    .stage(op: FileOperation) -> None
    .commit() -> TransactionResult
    .rollback() -> None
```

### `core/platform/occ.py` 📋
```python
def take_snapshot(file_paths: list[str]) -> dict[str, str]
def verify_snapshot(snapshot: dict[str, str]) -> list[str]
def predict_conflicts(file_paths: list[str], active_tasks: list) -> list[ConflictPrediction]
```

### `core/platform/sandbox.py` 📋
```python
def run_in_sandbox(cmd: list[str], cwd: str, timeout: int = 30) -> SandboxResult
```

---

## `core/compliance/` — Audit e Policy

### `core/compliance/audit.py` 📋
**NUOVO:** `AuditEvent` include `decision_refs: list[str]` — le ADR/PR/commit citati nell'esecuzione.

### `core/compliance/policy_engine.py` 📋
```python
class PolicyEngine
    .evaluate(task_context: TaskContext) -> PolicyResult
```

---

## `core/telemetry/` — ROI e Session Tracking

### `core/telemetry/roi_tracker.py` 📋
**NUOVO:** traccia anche `decisions_cited_count` — quante fonti istituzionali ha citato LORE per task. Metrica di qualità del KG.

---

---

## `clients/` — Client Thin

> **Invariante assoluta:** i client non contengono logica di business.
> **Invariante assoluta:** i client non accedono mai direttamente al DB o al KG.
> **Invariante assoluta:** ogni client parla col server via REST API, LSP o protocollo MCP. Niente altro.
>
> Aggiungere un nuovo client (IntelliJ, Vim, JetBrains) non deve richiedere
> nessuna modifica al server. Se lo richiede, la logica è nel posto sbagliato.

---

### `clients/cli/` ✅ — CLI (MVP attuale)

**Chi lo usa:** developer, script CI/CD, demo.
**Tecnologia:** Python (Click o Typer).
**Cosa fa:** invia comandi al server via REST, stampa output nel terminale.

**Comandi principali:**
```bash
lore scan .                                    # indicizza il progetto
lore apply "aggiungi logging alle funzioni db" # esegue un task
lore apply "..." --dry-run                     # mostra il piano senza eseguire
lore status                                    # stato del KG e ultimo scan
lore history                                   # ultimi task eseguiti
lore decisions list                            # ADR e decisioni indicizzate
```

**Output del comando `apply` (esempio):**
```
[SCAN]   127 simboli · 23 file · KG aggiornato
[PLAN]   34 funzioni · blast radius: 6 test suite · confidence: 0.87
         Fonte: ADR-003 (Winston, confidenza 0.97)
         Warning: db_legacy.js — hotspot (47 commit/30gg), 0% coverage

Applicare le modifiche? [Y/n]

[WORKER] ████████████ 34/34 · 0 errori
[SAFETY] sintassi ✓ · import ✓ · OCC ✓ · test ✓ · semantica ✓ · decisions ✓

Completato in 28s.
Spiegazione: Ho usato Winston (ADR-003, 0.97). Saltato db_legacy.js — segnalato.
```

**Dipendenze:** `requests`, `click` o `typer`, `rich` (per output colorato)
**NON dipende da:** nessun modulo `core/` — solo HTTP calls al server

---

### `clients/vscode/` 📋 — VS Code Extension

**Chi lo usa:** developer — esegue task senza lasciare l'editor.
**Tecnologia:** TypeScript (VS Code Extension API).
**Quando costruirlo:** dopo i primi design partner che lo chiedono esplicitamente.

**Cosa mostra nel pannello laterale:**
- Campo testo per inserire il task
- Dry-run: confidence score, blast radius, lista file che verranno toccati
- Diff affiancato per ogni file (prima / dopo)
- Pulsante Approva / Rigetta per ogni file individualmente
- Spiegazione istituzionale (ADR citate, hotspot evitati)
- Progress bar durante l'esecuzione

**Comunicazione col server:**
```typescript
// Tutto via REST — zero logica locale
const plan = await fetch('http://localhost:7891/api/plan', {
  method: 'POST',
  body: JSON.stringify({ task, project_root: workspace.rootPath })
})
// Mostra il piano, aspetta approvazione, poi:
await fetch('http://localhost:7891/api/execute', { method: 'POST', body: ... })
```

**NON implementare nel client:** parsing del codice, chiamate LLM, accesso al DB, logica di confidence

---

### `clients/dashboard/` 📋 — Web Dashboard

**Chi lo usa:** CTO, VP Engineering, manager — monitorano il valore generato da LORE.
**Tecnologia:** React + FastAPI (riusa il server esistente, aggiunge route `/dashboard/*`).
**Quando costruirlo:** abbozzo per il demo agli acceleratori, completo dopo il funding.

**Le 4 sezioni principali:**

**1. ROI Overview** — "Quanto vale LORE per questa organizzazione?"
- Ore developer risparmiate (task completati × tempo medio stimato)
- Bug bloccati prima del commit (failure V1-V4 intercettati)
- Costo LLM vs valore generato
- Rejection rate nel tempo (deve scendere — Data Flywheel al lavoro)

**2. Knowledge Graph** — "Cosa sa LORE del nostro codebase?"
- Decisioni indicizzate: N ADR, M commit con reasoning, K PR
- Link confermati vs probabili (con call-to-action per review)
- Ownership map: chi possiede cosa
- Hotspot: i 10 file più rischiosi

**3. Task History** — "Cosa ha fatto LORE?"
- Lista task con outcome (completato / rifiutato / escalato)
- Per ogni task: simboli toccati, ADR citate, test rotti (sempre 0)
- Filtri per data, team, tipo di task

**4. Compliance Audit** — "Posso mostrarlo al revisore?"
- Audit trail immutabile con firma HMAC
- Export per SIEM (SOX, GDPR, HIPAA)
- Ogni azione tracciata con sha256(codice), mai il codice grezzo

**Dipendenze:** React, Recharts (grafici ROI), `server/routes/dashboard.py` (nuove route)
**NON contiene:** nessuna logica — solo visualizzazione di dati dal server

---

## `tests/`

### `tests/unit/test_scanner.py` 📋
- Test idempotenza scan
- Test incrementale
- Test cleanup file cancellati
- Test migration system

### `tests/unit/test_git_miner.py` 📋 ← NUOVO
- Test estrazione commit reasoning
- Test co-change pattern detection
- Test hotspot calculation
- Test mention detection (`# ADR-XXX`)
- Test ownership mapping

### `tests/unit/test_decision_linker.py` 📋 ← NUOVO
- Test mention detection (confidenza 0.95)
- Test git bridge (confidenza 0.70-0.85)
- Test semantic similarity (confidenza 0.55-0.75)
- Test confidence combination
- Test link "probabili non verificati"
- Test human confirmation flow

### `tests/unit/test_python_parser.py` 📋
- Test estrazione funzioni con tipi
- Test call graph
- Test auto-summary
- Test complessità ciclomatica

### `tests/unit/test_file_transaction.py` 📋
- Test atomicità (rollback su errore)
- Test OCC conflict detection

### `tests/eval/golden_dataset.py` 📋
- 10 task di riferimento da CLAUDE.md §16
- **Gate v6:** T01 e T07 devono citare almeno una fonte KG nell'explanation

---

## Ordine di Implementazione Consigliato

```
F0  shared/errors.py → shared/types.py → shared/contracts.py
F1  core/parsers/base.py → core/parsers/python_parser.py ✅
    core/scanner/scanner.py ✅ → core/scanner/db.py
    core/knowledge/knowledge_base.py

F2  core/scanner/git_miner.py                ← NUOVO — priorità alta
    core/knowledge/decision_linker.py        ← NUOVO — priorità alta (il moat)
    core/knowledge/embeddings.py
    core/brain/llm_gateway.py → core/brain/prompt_builder.py
    core/planner/repository.py → core/planner/enricher.py
    core/planner/draft.py → core/planner/validator.py → core/planner/preflight.py
    core/planner/planner.py

F3  core/platform/occ.py → core/platform/file_transaction.py
    core/worker/semantic_diff.py
    core/worker/strategies.py → core/worker/worker.py

F4  core/platform/sandbox.py
    core/safety/verifiers.py → core/safety/feedback_builder.py → core/safety/safety.py

F5  core/loop/convergence.py → core/loop/session_replay.py → core/loop/loop.py
POC tests/eval/golden_dataset.py  ← NON procedere senza 70%+ precision

F6  core/knowledge/retrieval.py → core/knowledge/alias_detector.py
    core/scanners/file_watcher.py
    core/parsers/typescript_parser.py   ← Tree-sitter, non regex

F7  core/compliance/audit.py → core/compliance/policy_engine.py
    core/telemetry/roi_tracker.py
    server/api.py

F8+ core/parsers/java_parser.py
    core/loop/session_replay.py (persistenza completa)
    Integrazioni Jira/Confluence (manuale L5)
```

**Nota F2:** `git_miner.py` e `decision_linker.py` sono stati spostati in F2 (da F6 nella versione precedente) perché sono il cuore differenziante del prodotto. Il demo per gli acceleratori non funziona senza di loro.
