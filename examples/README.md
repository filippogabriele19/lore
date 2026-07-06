# 🎡 LORE Zero-Setup Playground

Welcome to the LORE Playground! This directory contains a pre-built Knowledge Graph and indices for the **LangChain** repository (comprising **16,619 symbols**, **4,222 active decision links**, and **1,267 hotspots**).

This sandbox allows you to explore all of LORE's analytical power **without any setup**, **without scanning**, and **without needing Anthropic API keys**.

---

## 🚀 Quick Start

### 1. Launch the Developer Console (Interactive Graph)
To explore the LangChain index, visual dependencies, hotspots, and design decisions interactively in your browser, run:

```bash
python lore.py audit --project examples/langchain
```

**What happens:**
* LORE starts a local HTTP server at `http://127.0.0.1:8081` and automatically opens it in your web browser.
* You can explore the **2D interactive Force-Directed graph** of files, trace dependencies, and review the architectural health metrics.

---

### 2. Run the CI/CD Pull Request Audit
Simulate the analysis of a Pull Request changes against the Knowledge Graph database:

```bash
python lore.py gh-check --project examples/langchain --changed-files examples/langchain/changed_files.txt
```

**What LORE detects in this test PR:**
1. **🚨 Amnesia Warning**: Flags `libs/core/uv.lock` as a high-risk hotspot with **150+ historical commits** and **0 decision links** (institutional memory gap).
2. **⚠️ Co-change coupling**: Informs you that changing profiles usually requires updating `libs/partners/openrouter/langchain_openrouter/data/_profiles.py` (which is missing from the PR).
3. **📜 Active Design Constraints**: Highlights active ADRs and design choices governing `serializable.py` based on historical commit reasoning (e.g. Nuno Campos's opt-out secret model).

---

### 3. Run the Predictive Zero-Day & Architectural Decay Audit
Scan the entire Knowledge Graph to detect potential taint propagation call paths, severe amnesia, and undocumented architectural decay:

```bash
python lore.py check-vuln --project examples/langchain
```

**What LORE predicts and analyzes in LangChain:**
1. **🚨 Exposed Source-to-Sink Paths**: Traces call-graph paths from public controllers/APIs (Sources) to critical execution sinks (Sinks) like `shell_tool.py` (remote code execution risk).
2. **🚨 Severe Amnesia Hotspots**: Pinpoints critical modules like `load/dump.py` that have high commit frequencies but **zero** architectural documentation (the exact setup behind CVE-2025-68664).
3. **⚠️ Architectural Decay**: Identifies undocumented bypass/hotfix commits that touch sensitive files without updating safety documentation in the Knowledge Graph.

---

## 📂 Sandbox Contents
- `examples/langchain/.lore/lore.db`: The pre-built SQLite Knowledge Graph.
- `examples/langchain/changed_files.txt`: Sample list of changed files for PR check.
- `examples/langchain/libs/`: Selected actual files (such as `serializable.py`, `dump.py`, `uv.lock`) to allow file reading and code inspection in the visual UI.
