# LORE: The Definitive Exhaustive Map (CLI & CORE)

Questo documento scheda in modo esatto e categorizzato **TUTTI i 65 moduli** sviluppati all'interno delle directory `cli/` e `core/` di LORE, affinché nulla rimanga nascosto.

---

## 🏗️ 1. CORE MODULES (`core/`) - *Il Motore Interno*
Questi moduli (29 file) costituiscono la logica di business, il parsing e le strutture dati di LORE.

### 🧠 Knowledge Graph & Symbol Mapping
* `symbol_map.py`: API ad alto livello per interfacciarsi con il Knowledge Graph (FOW).
* `symbol_db.py`: Gestione del database SQLite locale e dei vector embeddings (`vec0`).
* `symbol_scanner.py`: Naviga il file system per scoprire nuovi file sorgente.
* `symbol_extractor.py`: Parsing misto regex/AST per identificare classi, metodi e firme.
* `symbol_retriever.py`: Il motore di RAG (Retrieval-Augmented Generation) per estrarre le dipendenze semantiche.
* `symbol_types.py`: Definizioni dei tipi, classi e alias per la tipizzazione del grafo.
* `ast_extractor.py`: Parsing puro basato sull'Abstract Syntax Tree di Python.

### 🕵️ Security, Taint & Guardian
* `ast_taint.py`: Motore di Data Flow Analysis. Traccia le variabili inaffidabili ("sporche") lungo tutto il codice per prevenire iniezioni o falle.
* `ast_taint_helpers.py`: Classi di supporto per il tracer dell'AST.
* `base_tracer.py`: Classe base per l'esplorazione gerarchica dell'AST.
* `guardian.py`: L'"Intent Guardian". Un LLM parallelo che blocca la CI/CD se un Diff viola un ADR o le `guard_rules`.

### 🧩 Patching Deterministico (Transactional S/R)
* `agent_delta.py` & `agent_stage.py`: **Atomic Transaction Patcher** e gestore dello staging. Cuore della V11, applica i blocchi testuali SEARCH/REPLACE in modo rigoroso, revertendo l'intera transazione al primo mismatch per evitare corruzioni silenziose (Silent Corruption).
* `cst_patcher.py`, `ast_patcher.py` & `ast_patcher_core.py`: *(Legacy/Specializzati)* Strumenti matematici per applicare i cambiamenti a livello di sintassi (AST) originari della V10. In via di superamento per aggirare i fallimenti sui file non-Python e il context breaking sui file estesi.

### 🕰️ Git, Intent & History Mining
* `git_miner.py` & `git_historian.py`: Estraggono in massa le `git blame` e i `git log` per tracciare la storia di ogni singola riga di codice.
* `_intent_miner.py`: Analizza log di commit ed estrae la "volontà" di business originale (invarianti).
* `chat_miner.py`: Fantastico modulo per estrarre log da Slack/Discord e trasformare discussioni di team in vincoli architetturali.
* `decision_linker.py`: Collega il codice (simboli) agli ADR (Architectural Decision Records).
* `_macro_change.py` & `_batch_consolidator.py`: Strumenti per raggruppare i refactoring enormi ed evitare overhead nel database.

### 🚀 Bootstrapping & LLM
* `_cold_start.py` & `_cold_start_intent.py`: Moduli che permettono a LORE di avviarsi su repository "legacy" (vecchi, senza test), ricostruendo forzatamente il contesto.
* `_dl_link_builders.py` & `_dl_mention_builder.py`: Deep Learning linkers. Cercano correlazioni nascoste e riferimenti incrociati nelle issue di Github o nei nomi delle classi.
* `qa_engine.py`: Motore per l'esecuzione dei Test Locali per validare il TDD.
* `reconciler.py`: Mantiene il Database SQLite sincronizzato coi cambiamenti live del file system.
* `llm_client.py`: Wrapper multi-API unificato (OpenRouter, Anthropic, OpenAI).

---

## 🛠️ 2. CLI MODULES (`cli/`) - *L'Interfaccia Esterna & Agenti*
Questi moduli (36 file) definiscono l'eseguibile a riga di comando `lore` e i vari agenti/server.

### 🤖 Agent Orchestra
* `agent_runner.py`: Il cuore dell'orchestrazione. Esegue il Localizer, l'Architect e l'Editor in sequenza. Genera inoltre il "Byte-Exact Windowing Context" per prevenire le allucinazioni testuali.
* `v11_retrieval.py`: Il nuovo motore di Context Retrieval V11. Usa un `ContextBudget` basato su `ast.parse` per troncare i file in maniera semanticamente perfetta ai confini di metodi e classi.
* `agent_retrieval.py`: Gestore dell'estrazione semantica dei nodi per l'agente (V10 Legacy).
* `agent_tools.py`: Definizione degli Strumenti Funzionali (Tool Calling) a disposizione dell'LLM.
* `prompts.py`: Raccolta di tutti i System Prompt coercitivi e delle istruzioni di LORE.

### 🎯 Contesto & Esecuzione
* `brief_builder.py`: Costruisce il "Signpost Brief" prima del prompt (Dossier con dipendenze).
* `sandbox_evaluator.py`: Il manager dell'isolamento Docker per eseguire i Test in TDD e generare gli Stack Trace.
* `patch_validator.py`: Effettua validazioni statiche sulle patch proposte prima di applicarle.

### 🛡️ Vulnerabilità & CVE (Cybersecurity Suite)
* `vuln_analysis.py`: Il radar. Scansiona l'Amnesia Architetturale e il Drift (Decay).
* `vuln_cure.py`: L'infermiere. Autogenera ADR curativi per cristallizzare i vincoli quando rileva l'Amnesia.
* `check_vuln.py`: Il comando `lore check-vuln` per lanciare la scansione manuale.
* `cve.py`, `cve_data.py`, `cve_registry.py`, `cve_runner.py`: Un ecosistema intero dedicato alla mappatura, al tracciamento e al patching dei bollettini CVE (Common Vulnerabilities and Exposures).
* `vuln_cache.py`: Sistema di caching per velocizzare gli scan ricorsivi.

### 🌐 Server & Integrazioni CI/CD
* `mcp_server.py`: Il Server Model Context Protocol. Espone LORE come "Tool API" per client tipo Claude Desktop.
* `lsp.py`: Il Server Language Server Protocol. Si aggancia nativamente agli IDE come VSCode per suggerimenti in-editor.
* `diff_server.py` & `html_builders.py`: Server web locale che renderizza graficamente in HTML le patch generate da LORE per la revisione umana.
* `gh_check.py`: Espone LORE per bloccare automaticamente le PR nelle **GitHub Actions**.
* `git_hook.py`: Implementa LORE come **Pre-commit Hook** locale.

### 🔧 Utilità Operative
* `init.py`: Inizializza il DB SQLite (`lore init`).
* `query.py`: Permette all'umano di fare Query sul DB semantico da riga di comando.
* `apply.py`: Applica fisicamente il Diff generato sui file sorgente.
* `watch.py`: Demone (Daemon) che ascolta in realtime i salvataggi dei file (File Watcher) e aggiorna il grafo.
* `audit.py` & `audit_runner.py`: Moduli per condurre audizioni profonde della codebase, per esempio per compliance o refactoring di massa.
* `batch.py`: Esegue LORE su liste multiple di task in contemporanea.
* `benchmark.py`: Integrazione col framework ufficiale SWE-Bench per automatizzare l'esecuzione delle valutazioni.
* `cache.py`: Gestisce i token e le risposte vecchie dell'LLM.
* `adr.py`: Comandi per creare/gestire gli Architectural Decision Records (`lore adr ...`).
* `ingest_chat.py`: Innesca l'ingestione manuale del modulo `chat_miner.py`.
