

LORE
Architecture Decision Records
+ CLAUDE.md Operativo v6.0




# Indice ADR

REGOLA FONDAMENTALE: Nessuna di queste decisioni può essere modificata o aggirata senza aprire un nuovo ADR, documentare il contesto, e aggiornare tutti i contratti interessati. Claude Code deve rifiutare implementazioni che violano queste ADR.


# ADR-001 — Confini dei Moduli e Contratti

## Contesto
LORE è composto da sei moduli: SCAN, PLAN, WORKER, SAFETY, LOOP, TELEMETRY. Senza confini rigidi, ogni modulo tenderà a importare da altri, creando un grafo di dipendenze ciclico impossibile da testare, deployare o vendere indipendentemente.

## Decisione
REGOLA 1: Ogni modulo è un sistema indipendente. Comunica con gli altri SOLO attraverso contratti tipizzati in shared/contracts.py (Python MVP) o libs/shared/src/contracts/ (TypeScript target).

REGOLA 2: Le interfacce pubbliche di ogni modulo sono immutabili tra una versione major e l'altra. Aggiungere parametri opzionali è permesso. Rimuovere o rinominare parametri richiede un nuovo ADR.

## Esempi Giusto/Sbagliato

## Conseguenze
- Ogni modulo può essere testato in isolamento completo
- Ogni modulo può essere deployato indipendentemente
- In futuro ogni modulo può essere un prodotto separato (SCAN as a Service, ecc.)
- Claude Code può lavorare su un modulo senza rischiare di rompere gli altri


# ADR-002 — Schema KG e Policy di Evoluzione

## Contesto
Il Knowledge Graph è il prodotto. Il suo schema deve poter evolvere senza distruggere dati esistenti, specialmente i dati storici e decisionali che costituiscono il valore istituzionale accumulato.

## Decisione
- MAI modificare lo schema senza ADR firmata
- Tutte le migration sono additive — mai rename/delete di colonne
- I dati storici sono append-only: change_history, task_history, audit_events, decision_links
- Il KG di ogni cliente è un asset privato — mai esposto cross-tenant
- Il L4 Decisional viene popolato obbligatoriamente al primo scan (novità v6.0)

## Schema — I 5 Layer


# ADR-003 — Boundary LLM vs Deterministico

## Contesto
Alcune operazioni non devono mai dipendere dall'LLM. La sintassi è giusta o sbagliata — non è una questione di 'probabilità'. I file write sono atomici o non sono. L'OCC è un controllo binario.

## Decisione — Cosa è sempre deterministico
- Parsing AST e estrazione simboli
- Verifica sintassi (V1 Safety)
- Verifica import (V2 Safety)
- OCC hash check (V3 Safety)
- Test runner (V4 Safety)
- FileTransaction — stage, commit, rollback
- Git mining — co-change, hotspot, mention detection
- Decision linking — meccanismi 1 e 2 (mention + git bridge)

## Decisione — Cosa può usare l'LLM
- Plan draft (P1) — con schema Pydantic/Zod obbligatorio
- Plan validation (P3) — con decision constraints iniettati
- Code generation (WORKER livello 3) — solo per simboli nuovi
- Safety semantica (V5, V6, V7) — con schema output obbligatorio
- Decision extraction da ADR (meccanismo 3 del Decision Linker)


# ADR-004 — FileTransaction e Modello di Atomicità

## Decisione
- Tutti i file write usano FileTransaction. Zero eccezioni.
- Le operazioni sono staged prima di essere committed
- L'OCC hash viene verificato prima del commit
- In caso di errore: rollback automatico, mai stato parziale
- Ogni commit genera un AuditEvent con decision_refs



# ADR-005 — Confidence Scoring e Gate di Esecuzione

## Le Soglie — Non Modificabili Senza Nuovo ADR

## Componenti del Confidence Score
- Symbol resolution score: il simbolo esiste nel KG con firma matching?
- Blast radius score: quanti file vengono toccati? (meno = più confidenza)
- Historical stability: il file è stabile o hotspot? (stabile = più confidenza)
- Decision constraint score: i vincoli ADR sono chiari o ambigui?
- Test coverage: c'è coverage sul codice che viene modificato?


# ADR-006 — Server-Centric (LSP/MCP-First) e Separazione Client/Server

## Decisione
- Tutta la logica di business vive in apps/lore-server (o equivalente Python)
- Il client è THIN: solo UI, zero logica
- La comunicazione client-server usa il protocollo LSP (per integrazione IDE) e il protocollo **MCP (Model Context Protocol)** (per integrazione con agenti/LLM esterni)
- Aggiungere un nuovo IDE = scrivere solo un thin client o configurare il server come MCP Host/Client



# ADR-007 — Decision Linking e Memoria Istituzionale

## Contesto
LORE si posiziona come 'memoria istituzionale del codice enterprise'. Per mantenere questo posizionamento, il sistema deve essere in grado di linkare le decisioni architetturali (ADR, PR, commit con ragionamento) ai simboli del codice che ne sono stati influenzati, e usare questi link al momento dell'esecuzione di ogni task.

Senza questa capacità, LORE è un tool di refactoring intelligente. Con questa capacità, LORE è l'infrastruttura che porta il vibe coding in enterprise.

## Decisione — I Tre Meccanismi di Linking

## Decisione — Combinazione delle Confidenze
Quando più meccanismi concordano sullo stesso link, le confidenze si combinano. La regola generale:

- Solo semantic similarity → confidenza del singolo meccanismo
- Semantic + git bridge → confidenza aumentata di ~0.15
- Semantic + git bridge + mention → confidenza ~0.97
- Sotto 0.50 → link 'probabile non verificato', presentato al team

## Decisione — Acquisizione Ibrida
In accordo con la strategia di go-to-market:

Automatico (zero configurazione):
- Git history completa — commit messages, co-change, hotspot
- File .md, .txt, .rst in docs/, adr/, decisions/ — indicizzati come documenti decisionali
- Mention esplicite nel codice — # ADR-XXX, // see decision:
- CODEOWNERS — ownership per path
- package.json / requirements.txt — stack, librerie, convenzioni implicite

Manuale/configurato (upsell enterprise):
- Jira/Linear — ticket, epic, requisiti
- Confluence/Notion — design doc, spec, runbook
- Slack threads (futuro) — decisioni informali

## Decisione — Uso al Momento dell'Esecuzione (Modello Ibrido Signpost/Pull)
Il decision context è obbligatorio, ma per evitare la diluizione dell'attenzione dell'LLM (prompt pollution), si applica una strategia ibrida:

- **Signpost/Brief (Fase P2/P3)**: LORE inserisce nel codice sorgente o all'inizio del prompt degli indici leggeri di contesto decisionali (Context Anchors/Signposts) generati da `BriefBuilder`. Tali indici informano l'Editor LLM sull'esistenza di ADR, hotspot o commit storici rilevanti senza iniettare il testo grezzo esteso.
- **Active Pull (WORKER/MCP)**: L'Editor LLM interroga attivamente LORE Server tramite gli strumenti MCP (es. `lore_get_adr()`, `lore_get_git_context()`) solo se e quando tocca le parti logiche interessate.
- **WORKER**: ogni modifica cita la fonte del decision context nell'ExecutionExplanation.
- **SAFETY V6**: verifica deterministica che nessun DecisionConstraint sia stato violato.
- **LOOP**: il context_anchor include decision_constraints compressi.

## Decisione — Spiegazione Istituzionale Obbligatoria
Ogni WorkResult deve includere ExecutionExplanation che cita:


## Decisione — Human-in-the-Loop per Link Incerti
Link con confidenza < 0.50 non vengono usati silenziosamente. Vengono:
- Marcati come 'probabili non verificati' nel KG
- Presentati al team in una review dedicata
- Ogni conferma o rigetto umano aggiorna il modello di confidence
- Ogni feedback alimenta il Data Flywheel immediatamente

## Conseguenze
- LORE diventa l'unico sistema che sa perché il codice è fatto così — non solo cosa fa
- Il KG diventa un asset di valore crescente nel tempo — più task, più link, più valore
- Il vantaggio competitivo è non replicabile senza anni di dati per cliente
- Ogni nuovo cliente che installa LORE inizia a costruire un moat privato dal giorno 1

## Violazioni di Questa ADR
Costituisce violazione di ADR-007:
- Eseguire un task senza interrogare il decision_context dei simboli coinvolti
- Ignorare un constraint con confidenza > 0.70
- Produrre un WorkResult senza ExecutionExplanation con fonti citate
- Skippare V6 Safety nella pipeline di verifica
- Trattare il git mining come opzionale invece che obbligatorio

# ADR-008 — Integrazione MCP e JIT Context Pull (Signpost/Pull)

## Contesto
I benchmark su larga scala (114 task Django) dimostrano che inviare grandi volumi di dati di contesto grezzi (Git Blame esteso, documentazione, interi file accoppiati) riduce l'efficacia del ragionamento del modello causa diluizione dell'attenzione (*lost in the middle*).

## Decisione
1. **Just-in-Time Context Retrieval**: Il server LORE si configura come server MCP. L'invio di contesti estesi è disattivato per impostazione predefinita nei primi messaggi.
2. **LORE Signpost Engine**: Il server genera commenti di segnalazione (Signposts) direttamente nel file inviato all'Editor LLM. Questi commenti includono ID univoci di ADR, hotspot ed indici di commit storici.
3. **MCP Tool Loop**: L'LLM deve invocare attivamente gli strumenti di LORE (es. `lore_get_adr`, `lore_get_git_context`) on-demand solo quando sta modificando blocchi di codice vincolati.
4. **Sinergia Piattaforma-IDE**: Quando l'MCP rileva refactoring di disaccoppiamento eseguiti dall'Editor, notifica il server LORE che ricalcola all'istante le metriche di accoppiamento (co-change coupling) nel database locale. I report della Dashboard riflettono immediatamente l'impatto positivo sulle DORA metrics del team.


# § CLAUDE.md Operativo v6.0 — Riferimento Rapido

Questo è il riassunto operativo. Il documento completo è CLAUDE.md alla root del progetto.

## Form Factor del Prodotto
LORE è un server centrale con tre client thin. Il server contiene tutta la logica. I client non fanno nulla da soli.


Aggiungere un nuovo IDE (IntelliJ, Vim) = scrivere solo un thin client. Il server non cambia. Questa è la conseguenza concreta di ADR-006.

## Le 9 Invarianti

## Workflow per Ogni Feature

## Quando Aprire una ADR
- Vuoi cambiare lo schema del KG (anche solo aggiungendo una tabella)
- Vuoi cambiare i contratti in shared/contracts.py
- Vuoi aggiungere un nuovo modulo
- Vuoi cambiare le soglie di confidence (ADR-005)
- Vuoi cambiare il meccanismo di Decision Linking (ADR-007)
- Stai prendendo una decisione che influenza più di un modulo

## Golden Dataset — Gate v6
I 10 task di riferimento devono passare prima di ogni rilascio major. Gate aggiuntivo v6.0: T01 e T07 devono citare almeno una fonte KG (ADR, commit reasoning, o pattern esistente) nella ExecutionExplanation. Se LORE esegue correttamente ma non cita nulla, il test fallisce.


LORE Architecture Decision Records v6.0 + CLAUDE.md Operativo v6.0 — Marzo 2026
Documento Confidenziale — Proprietà Intellettuale LORE