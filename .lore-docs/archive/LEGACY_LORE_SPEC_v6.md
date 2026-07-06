

LORE
AI Software Engineer Agent

Product & Architecture Specification



Documento Confidenziale — Proprietà Intellettuale LORE


# 1. Visione Strategica
## 1.1 Il Problema che Nessuno Risolve Davvero
Cursor e Copilot aiutano a scrivere codice nuovo. LORE è il primo sistema che capisce il codice che già esiste — non solo cosa fa, ma perché è fatto così.

Ogni Fortune 500 ha codeblore da 10-30 anni. I senior engineer che li conoscono davvero sono 3-4 persone per sistema. Quando uno di loro lascia l'azienda, anni di conoscenza implicita — decisioni architetturali, trade-off, pattern interni, warning critici — svaniscono nel nulla. Nessun tool risolve questo. Nessuno fino ad LORE.

## 1.2 Positioning Statement
LORE porta il vibe coding in ambienti enterprise dove sembra impossibile.

Il vibe coding non funziona in enterprise per un motivo preciso: l'LLM non sa il perché del codice. LORE risolve esattamente questo. Non è un tool di refactoring, è l'infrastruttura che rende il vibe coding sicuro e possibile su codeblore che esistono da 10 anni.

## 1.3 I Quattro Moat Competitivi

## 1.4 Perché Non i Competitor


# 2. Filosofia Modulare — Il Principio Fondante
## 2.1 I Sei Moduli LORE

## 2.2 Contratto tra Moduli
Ogni modulo è un sistema indipendente. Comunicano SOLO attraverso contratti tipizzati in shared/contracts.py (Python MVP) o libs/shared/src/contracts/ (TypeScript target). Nessun modulo importa mai direttamente da un altro modulo.


# 3. Modulo SCAN — Intelligenza sul Codebase
## 3.1 Livelli di Profondità dello Scan

## 3.2 Git Mining — L4 (core/scanner/git_miner.py)
Il git mining è obbligatorio, non opzionale. È il fondamento del Decision Linking e del moat principale di LORE.

Cosa estrae:
- Commit history completa con metadati (autore, data, messaggio, file toccati)
- Commit con ragionamento significativo: corpo > 100 chars, keyword 'because', 'decided', 'avoid', 'warning', 'tradeoff', 'do not', 'never'
- Co-change patterns: coppie di file committate insieme frequentemente (dipendenze implicite non visibili nel codice)
- Hotspot: file ad alta frequenza di modifica × alta complessità ciclomatica = rischio reale
- Mention esplicite: # ADR-003, // see decision:, @deprecated since ADR-011
- Ownership reale: blame semantico aggregato per file/directory


# 4. Knowledge Graph v6 — La Memoria Istituzionale
## 4.1 Schema Completo — I 5 Layer

## 4.2 L4 Decisional — Il Layer che Nessuno Ha
Il L4 è il layer che trasforma LORE da tool intelligente a memoria istituzionale. Contiene:

- decision_links: tabella dei link tra decisioni architetturali e simboli del codice
- adr_index: tutti i documenti decisionali indicizzati (ADR, spec, design doc, PR descriptions)
- adr_chunks: embedding a livello di paragrafo di ogni documento decisionale
- commit_reasoning: commit con ragionamento significativo estratto
- pr_context: PR descriptions con link ai file modificati
- symbol_warnings: simboli con warning espliciti ('do not modify', 'never call directly', 'hotspot critico')

## 4.3 Decision Linking — Come Funziona
Il Decision Linker costruisce i link tra decisioni e simboli attraverso tre meccanismi combinati:


Quando più meccanismi concordano, le confidenze si combinano. Un simbolo trovato sia nel git bridge che con similarity 0.91 ha confidenza finale ~0.97 — praticamente certo.

Link con confidenza < 0.50 vengono marcati come 'probabili non verificati' e presentati al team per conferma. Ogni conferma umana alimenta il Data Flywheel.

## 4.4 Decision Context in Azione (JIT context pull via MCP)
Quando LORE sta per modificare validateAmount(), non inserisce tutto il testo grezzo di ADR-003 e ADR-011 nel prompt dell'Editor. Genera invece una **Context Anchor** inline nel codice:

```python
# [LORE ANCHOR: validateAmount]
# 📌 Vincolato da ADR-003 (confidenza 0.97): 'payments must validate amount before processing'
# 📌 Suggerimento: ADR-011 (confidenza 0.71): 'deprecate sync validation'
# 🛠️ Recupera i dettagli richiamando lo strumento MCP `lore_get_adr(id)`
```

L'LLM legge queste ancore e decide attivamente di "tirare" (pull) il testo completo tramite chiamata tool se tocca le righe critiche, preservando l'attenzione e riducendo i token nel prompt principale.



# 5. Modulo PLAN — Pipeline di Pianificazione
## 5.1 Le 4 Fasi — Ordine Obbligatorio

NOVITÀ v6: la flore P2 include obbligatoriamente la chiamata a decision_linker.get_decision_context() per ogni simbolo nel draft. I constraint estratti vengono forniti sotto forma di **Context Signposts (Indici di Contesto)** leggibili nel prompt di P3 o attingibili dall'LLM via tool attivi.


## 5.2 Confidence Score per Ogni Cambio


# 6. Modulo WORKER — Esecuzione Chirurgica
## 6.1 Gerarchia di Esecuzione
Ordine obbligatorio — ogni livello si attiva solo se il precedente non è applicabile:

- 1. DETERMINISTIC EXTRACTION: AST node replacement (LibCST per Python, Tree-sitter per TypeScript)
- 2. SEARCH/REPLACE: fuzzy matching con tolleranza LLM formatting errors
- 3. LLM GENERATION: solo per creazione ex-novo di simboli nuovi

## 6.2 Spiegazione Istituzionale — Invariante #8
Ogni WorkResult deve includere ExecutionExplanation che cita le fonti KG usate. Non 'ho fatto X', ma 'ho fatto X perché il KG dice Y (fonte: ADR-003, confidenza 0.97)'.

Esempio corretto:
'Ho usato Winston logger (ADR-003 confidenza 0.97, 34 occorrenze esistenti).
Ho saltato db_legacy.js: hotspot critico (47 commit/30gg), 0% coverage.'

## 6.3 FileTransaction — Atomicità Garantita
Ogni scrittura file usa FileTransaction. Nessun fs.write diretto in tutto il codebase. Violazione = stato inconsistente del codeblore cliente.


# 7. Modulo SAFETY — Verifica Multi-Layer
## 7.1 Pipeline V1-V7

V6 è nuovo in v6.0. Verifica esplicitamente che nessun vincolo proveniente dal Decision Linker sia stato violato nell'esecuzione. Se validateAmount() è stata rimossa nonostante ADR-003, V6 blocca il commit.


# 8. Loop Orchestrator — Il Ciclo Adattivo
## 8.1 Context Anchor con Decision Constraints
Il context_anchor viene iniettato invariato in ogni prompt di ogni iterazione. In v6.0 include anche i decision_constraints estratti dal KG — i vincoli derivati da ADR e commit reasoning che non possono essere dimenticati tra un'iterazione e l'altra.

## 8.2 Oscillation Detection
Se gli stessi errori si ripetono tra iterazioni, o la confidence non sale di almeno 0.05, il LOOP scala a ESCALATE_TO_HUMAN invece di continuare a iterare.


# 9. Data Flywheel — Il Moat che Cresce
## 9.1 Segnali del Flywheel
- Ogni task approvato → positive example per fine-tuning
- Ogni task rifiutato con motivo → negative example
- Ogni link decisionale confermato dal team → migliora la precisione del Decision Linker
- Ogni rejection embedding nel RAG layer immediatamente

Il Data Flywheel è il vantaggio competitivo non replicabile. Chi entra prima accumula dati migliori → precision migliore → più adozione → più dati. Salvare tutto.

## 9.2 Metriche di Qualità del KG
- decisions_cited_count: quante fonti istituzionali LORE cita per task (più è alto, meglio funziona il L4)
- link_confirmation_rate: % dei link probabili confermati dal team (flywheel training signal)
- hotspot_avoidance_rate: % dei task che hanno evitato hotspot grazie al git mining


# 10. Compliance Layer
## 10.1 Mapping Compliance per Settore


# 11. Architettura del Prodotto — Form Factor
LORE è un server centrale con client thin multipli. Tutta la logica vive nel server. I client non fanno nulla da soli — mandano comandi al server e mostrano i risultati.

## 11.1 Il Server
Contiene tutto: SCAN, PLAN, WORKER, SAFETY, LOOP, Knowledge Graph, Decision Linker. Espone API REST, protocollo LSP per editor tradizionali, e il protocollo **MCP (Model Context Protocol)** per permettere ad agenti autonomi o IDE moderni di interrogare attivamente la memoria del progetto on-demand (JIT context pull).

Regola assoluta (da ADR-006): mai spostare logica di business nei client.


## 11.2 I Tre Client (tutti thin)

## 11.3 Perché Questo Modello è Obbligatorio per Enterprise
In enterprise non puoi assumere che tutti usino VS Code. Hai team su IntelliJ, Vim, Emacs, JetBrains. Se la logica fosse nell'estensione, aggiungere IntelliJ significherebbe riscrivere tutto. Con il server centrale, aggiungere un nuovo IDE = scrivere solo un thin client in quel linguaggio. Il server non cambia mai.

## 11.4 Cosa Vede Ogni Client
### CLI — output nel terminale
$ lore apply "aggiungi logging a tutte le funzioni db"

[SCAN]   127 simboli · 23 file
[PLAN]   34 funzioni · blast radius: 6 suite · confidence: 0.87
Fonte: ADR-003 (Winston, confidenza 0.97)
Warning: db_legacy.js — hotspot (47 commit/30gg)
[WORKER] ████████ 34/34 · 0 errori
[SAFETY] sintassi ✓ · import ✓ · OCC ✓ · test ✓ · decisions ✓
Completato in 28s. Ho usato Winston (ADR-003). Saltato db_legacy.js.

### VS Code extension — pannello laterale
- Campo testo per il task
- Dry-run: confidence, blast radius, lista file che verranno toccati
- Diff affiancato prima/dopo per ogni file
- Pulsante Approva / Rigetta per file
- Spiegazione istituzionale (ADR citate, hotspot evitati)

### Web dashboard — per il CTO


# 12. Narrazione per gli Acceleratori

"Cursor e Copilot aiutano a scrivere codice nuovo. LORE è il primo sistema che capisce il codice che già esiste — non solo cosa fa, ma perché è fatto così. La differenza tra un junior brillante e un senior con 10 anni su quel codebase."


## 12.1 Le Tre Domande degli Acceleratori

## 12.2 Il Demo Perfetto
Non mostrare un task generico. Mostrare un task che fa venire i brividi a chiunque abbia lavorato su un codeblore enterprise:

- Dry-run: 'guarda cosa farei — senza toccare nulla. 34 funzioni, blast radius su 6 test suite, confidence 0.87'
- Esecuzione: 28 secondi, zero errori
- Spiegazione istituzionale: 'Ho usato Winston (ADR-003). Ho saltato db_legacy.js (47 commit/30gg, hotspot critico)'
- Semantic diff: 'complessità ridotta del 23%, nessun test rotto, 2 ADR rispettate'
- KG aggiornato: 'queste 34 funzioni ora hanno ownership e decision context mappati'

Questo non è autocomplete. Questo è un engineer che conosce il codeblore meglio di tutti — e sa anche perché è fatto così.

LORE Product & Architecture SPEC v6.0 — The Institutional Memory Edition
Documento Confidenziale — Costruita per $500k+ ACV · Fortune 500 · Compliance-Ready · Modular by Design · Defensible Moat