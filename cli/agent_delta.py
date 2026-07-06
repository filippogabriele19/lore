import re
from pathlib import Path
from cli.agent_stage import StageWriter

def _fallback_python_patch(repo_dir: Path, patch_string: str, log_fn=None) -> bool:
    import difflib

    lines = patch_string.splitlines()
    current_file = None
    hunk_old = []
    hunk_new = []
    in_hunk = False
    file_patches = {}

    for line in lines:
        if line.startswith('--- a/') or line.startswith('--- '):
            continue
        if line.startswith('+++ b/') or line.startswith('+++ '):
            p_target = line[6:].strip().split('\t')[0] if 'b/' in line else line[4:].strip()
            current_file = repo_dir / p_target
            file_patches[current_file] = []
            in_hunk = False
            continue
        if line.startswith('@@'):
            if in_hunk and current_file and (hunk_old or hunk_new):
                file_patches[current_file].append((list(hunk_old), list(hunk_new)))
            hunk_old = []
            hunk_new = []
            in_hunk = True
            continue

        if in_hunk and current_file:
            if line.startswith('-'):
                hunk_old.append(line[1:])
            elif line.startswith('+'):
                hunk_new.append(line[1:])
            else:
                ctx = line[1:] if line.startswith(' ') else line
                hunk_old.append(ctx)
                hunk_new.append(ctx)

    if in_hunk and current_file and (hunk_old or hunk_new):
        file_patches[current_file].append((list(hunk_old), list(hunk_new)))

    def best_match_index(file_lines, old_lines, threshold=0.82):
        if not old_lines:
            return -1
        n = len(old_lines)
        best_idx, best_score = -1, 0.0
        old_stripped = [l.strip() for l in old_lines]
        for i in range(max(1, len(file_lines) - n + 1)):
            window = file_lines[i:i + n]
            if len(window) < n:
                continue
            win_stripped = [l.strip() for l in window]
            quick_hits = sum(1 for a, b in zip(old_stripped, win_stripped) if a == b)
            if quick_hits < n * 0.5:
                continue
            score = difflib.SequenceMatcher(None, old_lines, window).ratio()
            if score > best_score:
                best_score, best_idx = score, i
        return best_idx if best_score >= threshold else -1

    applied_any = False
    for filepath, hunks in file_patches.items():
        if not filepath.exists():
            if log_fn:
                log_fn(f"      [Fallback] File not found: {filepath}")
            continue
        try:
            content = filepath.read_text(encoding='utf-8', errors='replace')
            file_lines = content.splitlines()
            modified = False

            for hunk_idx, (old, new) in enumerate(hunks):
                if not old:
                    continue

                old_str = "\n".join(old)
                new_str = "\n".join(new)
                if old_str in content:
                    content = content.replace(old_str, new_str, 1)
                    file_lines = content.splitlines()
                    modified = True
                    if log_fn:
                        log_fn(f"      [Fallback] Hunk {hunk_idx+1}: exact match applied in {filepath.name}")
                    continue

                idx = best_match_index(file_lines, old)
                if idx != -1:
                    file_lines[idx:idx + len(old)] = new
                    content = "\n".join(file_lines)
                    modified = True
                    if log_fn:
                        log_fn(f"      [Fallback] Hunk {hunk_idx+1}: fuzzy match at line {idx+1} in {filepath.name}")
                    continue

                old_key = "\n".join(l.strip() for l in old)
                for i in range(len(file_lines) - len(old) + 1):
                    window_key = "\n".join(file_lines[i + j].strip() for j in range(len(old)))
                    if window_key == old_key:
                        file_lines[i:i + len(old)] = new
                        content = "\n".join(file_lines)
                        modified = True
                        if log_fn:
                            log_fn(f"      [Fallback] Hunk {hunk_idx+1}: strip-match at line {i+1} in {filepath.name}")
                        break
                else:
                    if log_fn:
                        log_fn(f"      [Fallback] Hunk {hunk_idx+1}: NO MATCH for block starting with: {repr(old[0][:80])}")

            if modified:
                filepath.write_text(content, encoding='utf-8', newline='\n')
                applied_any = True
        except Exception as e:
            if log_fn:
                log_fn(f"      [Fallback Matcher Error] Failed updating {filepath.name}: {e}")

    return applied_any

def _replace_method_in_class(source: str, class_name: str, method_name: str, new_code: str, log_fn=None) -> str:
    import libcst as cst
    from core.cst_patcher import RemovalTransformer

    tree = cst.parse_module(source)
    remover = RemovalTransformer([method_name])
    tree = tree.visit(remover)
    if remover.removed_count == 0 and log_fn:
        log_fn(f"  [DELTA] replace_symbol: '{method_name}' not found in class '{class_name}' — will append")

    try:
        new_stmts = cst.parse_module(new_code.strip() + "\n").body
    except Exception as exc:
        raise RuntimeError(f"Could not parse replacement code: {exc}")

    class _MethodAppender(cst.CSTTransformer):
        def __init__(self, target_class: str, stmts):
            self._target = target_class
            self._stmts  = stmts
            self.applied = False

        def leave_ClassDef(self, original_node, updated_node):
            if original_node.name.value != self._target:
                return updated_node
            old_body = updated_node.body
            if not isinstance(old_body, cst.IndentedBlock):
                return updated_node
            combined = list(old_body.body) + list(self._stmts)
            self.applied = True
            return updated_node.with_changes(body=old_body.with_changes(body=combined))

    appender = _MethodAppender(class_name, new_stmts)
    tree = tree.visit(appender)
    if not appender.applied:
        raise RuntimeError(f"Class '{class_name}' not found in source")
    return tree.code

class DeltaApplicator:
    def apply(self, response_text: str, project_root: Path, stage: StageWriter, log_fn=None) -> int:
        # ── Priority 1: Search/Replace blocks (new primary format) ──
        sr_blocks = self._parse_search_replace(response_text)
        new_files = self._parse_new_files(response_text)
        if sr_blocks or new_files:
            if log_fn:
                log_fn(f"[DELTA] Search/Replace: {len(sr_blocks)} block(s), {len(new_files)} new file(s)")
            return self._apply_search_replace(sr_blocks, new_files, project_root, stage, log_fn)

        # ── Priority 2: Unified diff ──
        diff_text = self._extract_unified_diff(response_text)
        if diff_text:
            if log_fn:
                log_fn("[DELTA] Unified diff detected, applying using git apply...")
            return self._apply_unified_diff(diff_text, project_root, stage, log_fn)

        # ── Priority 3: Full file rewrite (FILE: ... ``` format — legacy fallback) ──
        file_pattern = re.compile(r"FILE:\s*(\S+)\n```[^\n]*\n(.*?)```", re.DOTALL)
        matches = list(file_pattern.finditer(response_text))
        if matches:
            if log_fn:
                log_fn(f"[DELTA] Full-file fallback: {len(matches)} file(s) detected")
            files_written = set()
            for m in matches:
                rel_path, content = m.group(1), m.group(2)
                stage.write(rel_path, content, "full-file fallback")
                files_written.add(rel_path)
                if log_fn:
                    log_fn(f"  >> Full-file staged: {rel_path}")
            return len(files_written)

        # ── Priority 4: Legacy PATCH/OP format ──
        ops = self._parse(response_text)
        if ops:
            if log_fn:
                log_fn(f"[DELTA] Legacy PATCH/OP: {len(ops)} operation(s)")
            return self._apply_legacy_ops(ops, project_root, stage, log_fn)

        if log_fn:
            log_fn("[DELTA] No parseable output format found in response")
        return 0

    # ── Search/Replace parsing & application ──

    def _parse_search_replace(self, text: str) -> list[dict]:
        """Parse SEARCH/REPLACE blocks with <<< >>> delimiters."""
        blocks = []
        current_file = None
        lines = text.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            # Detect FILE: header
            m_file = re.match(r"^FILE:\s*(.+)$", line.strip())
            if m_file:
                current_file = m_file.group(1).strip()
                i += 1
                continue

            line_strip = line.strip()
            # Detect SEARCH: marker (allowing inline code or delimiters after search/search<<<)
            m_search = re.match(r"^(?:SEARCH|Search|search)(?::|<<<|:<<<)\s*(.*)$", line_strip)
            if not m_search:
                m_search = re.match(r"^(?:SEARCH|Search|search)\s*$", line_strip)

            if m_search and current_file:
                inline_content = m_search.group(1).strip() if len(m_search.groups()) > 0 else ""
                i += 1
                search_lines = []
                if inline_content:
                    if inline_content.startswith("<<<"):
                        idx_delimiter = line.find("<<<")
                        search_lines.append(line[idx_delimiter + 3:])
                    else:
                        m_prefix = re.match(r"^(?:SEARCH|Search|search)(?::|<<<|:<<<)?\s*", line)
                        search_lines.append(line[m_prefix.end():])
                else:
                    if i < len(lines) and (lines[i].strip() == "<<<" or lines[i].strip().startswith("<<<")):
                        next_line_strip = lines[i].strip()
                        if next_line_strip == "<<<":
                            i += 1
                        else:
                            idx_delimiter = lines[i].find("<<<")
                            search_lines.append(lines[i][idx_delimiter + 3:])
                            i += 1

                while i < len(lines):
                    next_line_strip = lines[i].strip()
                    # Terminate search-block only on REPLACE marker, >>>, or conflict separator =======
                    if re.match(r"^(?:REPLACE|Replace|replace|>>>)\s*$", next_line_strip) or re.match(r"^(?:REPLACE|Replace|replace)(?::|<<<|:<<<)?\s*$", next_line_strip) or next_line_strip == "=======":
                        break
                    search_lines.append(lines[i])
                    i += 1

                if i < len(lines) and lines[i].strip() == ">>>":
                    i += 1  # skip >>>

                # Now expect REPLACE:
                if i < len(lines):
                    next_line_strip = lines[i].strip()
                    m_replace = re.match(r"^(?:REPLACE|Replace|replace)(?::|<<<|:<<<)\s*(.*)$", next_line_strip)
                    if not m_replace:
                        m_replace = re.match(r"^(?:REPLACE|Replace|replace|>>>)\s*$", next_line_strip)

                    if m_replace:
                        inline_rep = m_replace.group(1).strip() if len(m_replace.groups()) > 0 else ""
                        i += 1
                        replace_lines = []
                        if inline_rep:
                            if inline_rep.startswith("<<<"):
                                idx_delimiter = lines[i-1].find("<<<")
                                replace_lines.append(lines[i-1][idx_delimiter + 3:])
                            else:
                                m_prefix = re.match(r"^(?:REPLACE|Replace|replace)(?::|<<<|:<<<)?\s*", lines[i-1])
                                replace_lines.append(lines[i-1][m_prefix.end():])
                        else:
                            if i < len(lines) and (lines[i].strip() == "<<<" or lines[i].strip().startswith("<<<")):
                                next_line_strip = lines[i].strip()
                                if next_line_strip == "<<<":
                                    i += 1
                                else:
                                    idx_delimiter = lines[i].find("<<<")
                                    replace_lines.append(lines[i][idx_delimiter + 3:])
                                    i += 1

                        while i < len(lines):
                            next_line_strip = lines[i].strip()
                            if (next_line_strip == ">>>" or next_line_strip == ">>>>>>>" or 
                                (i+1 < len(lines) and (
                                    re.match(r"^(?:SEARCH|Search|search)(?:<<<|:<<<|:)\s*.*$", lines[i+1].strip()) or 
                                    re.match(r"^(?:SEARCH|Search|search)\s*$", lines[i+1].strip())
                                )) or 
                                next_line_strip.startswith("FILE:")):
                                break
                            replace_lines.append(lines[i])
                            i += 1

                        if i < len(lines) and lines[i].strip() in (">>>", ">>>>>>>"):
                            i += 1  # skip >>>

                        blocks.append({
                            "file": current_file,
                            "search": "\n".join(search_lines),
                            "replace": "\n".join(replace_lines),
                        })
                continue
            i += 1
        return blocks

    def _parse_new_files(self, text: str) -> list[dict]:
        """Parse NEW_FILE: blocks."""
        results = []
        pattern = re.compile(r"NEW_FILE:\s*(\S+)\s*\n```[^\n]*\n(.*?)```", re.DOTALL)
        for m in pattern.finditer(text):
            results.append({"path": m.group(1).strip(), "content": m.group(2)})
        return results

    def _apply_search_replace(self, blocks: list[dict], new_files: list[dict],
                              project_root: Path, stage: StageWriter, log_fn=None) -> int:
        import difflib
        file_sources: dict[str, str] = {}
        files_modified: set[str] = set()

        for block in blocks:
            rel_path = block["file"]
            if rel_path not in file_sources:
                abs_path = project_root / rel_path
                try:
                    file_sources[rel_path] = abs_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    if log_fn:
                        log_fn(f"  [DELTA] File not found: {rel_path}")
                    continue

            source = file_sources[rel_path]
            search = block["search"]
            replace = block["replace"]

            # Normalize trailing whitespace to handle spaces/tabs inconsistencies
            search = "\n".join(line.rstrip() for line in search.splitlines())
            source = "\n".join(line.rstrip() for line in source.splitlines())
            replace = "\n".join(line.rstrip() for line in replace.splitlines())

            # Exact match
            if search in source:
                source = source.replace(search, replace, 1)
                file_sources[rel_path] = source
                files_modified.add(rel_path)
                if log_fn:
                    log_fn(f"  [S/R] Exact match applied in {rel_path}")
                continue

            # Strip-whitespace match (tolerates indentation differences)
            src_lines = source.splitlines()
            search_lines = search.splitlines()
            search_stripped = [l.strip() for l in search_lines]
            matched = False
            for idx in range(max(1, len(src_lines) - len(search_lines) + 1)):
                window = [src_lines[idx + j].strip() for j in range(len(search_lines))]
                if window == search_stripped:
                    replace_lines = replace.splitlines()
                    src_lines[idx:idx + len(search_lines)] = replace_lines
                    source = "\n".join(src_lines)
                    file_sources[rel_path] = source
                    files_modified.add(rel_path)
                    matched = True
                    if log_fn:
                        log_fn(f"  [S/R] Strip-match at line {idx+1} in {rel_path}")
                    break

            if matched:
                continue

            # Match failed! Abort transaction.
            if log_fn:
                preview = search.splitlines()[0][:80] if search else "(empty)"
                log_fn(f"  [S/R] NO MATCH (Exact/Strip) in {rel_path}: {repr(preview)}")
                log_fn("  [DELTA] Transaction aborted: not all blocks matched exactly.")
            return 0  # Revert all changes in this patch attempt

        for rel_path, source in file_sources.items():
            if rel_path in files_modified:
                stage.write(rel_path, source, "patched by search/replace")
                if log_fn:
                    log_fn(f"  >> Staged: {rel_path}")

        for nf in new_files:
            stage.write(nf["path"], nf["content"], "new file")
            files_modified.add(nf["path"])
            if log_fn:
                log_fn(f"  >> New file: {nf['path']}")

        return len(files_modified)

    def _find_file_candidate(self, text: str, project_root: Path) -> str | None:
        import os
        paths = re.findall(r"[\w\-\.\\/]+\.(?:py|ts|tsx|js|jsx|go)", text)
        if not paths:
            return None
        
        # Check files from last to first
        for path_str in reversed(paths):
            cleaned = path_str.replace("\\", "/").strip("`*() ")
            if (project_root / cleaned).exists():
                return cleaned
            # Remove a/ or b/ prefixes commonly found in git diffs
            if cleaned.startswith("a/") or cleaned.startswith("b/"):
                sub_path = cleaned[2:]
                if (project_root / sub_path).exists():
                    return sub_path
            
            # If it's a partial path (e.g., db/models/fields.py)
            # check if we can find a matching file in the workspace
            basename = cleaned.split("/")[-1]
            ignore_dirs = {".git", "venv", ".venv", "__pycache__", "node_modules", "build", "dist"}
            try:
                for root, dirs, files in os.walk(str(project_root)):
                    dirs[:] = [d for d in dirs if d not in ignore_dirs]
                    for f in files:
                        if f == basename:
                            cand = Path(root) / f
                            cand_rel = str(cand.relative_to(project_root)).replace("\\", "/")
                            if cand_rel.endswith(cleaned):
                                return cand_rel
            except Exception:
                pass
        return None

    def _apply_code_block_fallback(self, rel_path: str, code_content: str, project_root: Path, stage: StageWriter, log_fn=None) -> bool:
        abs_path = project_root / rel_path
        try:
            content = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
            
        modified = False
        orig_lines = content.splitlines()
        code_lines = code_content.splitlines()
        
        # Normalize code lines
        code_lines = [l.rstrip() for l in code_lines]
        
        # Helper for indent extraction
        def _extract_indent_block(lines: list[str], start_idx: int) -> tuple[int, int]:
            first_line = lines[start_idx]
            base_indent = len(first_line) - len(first_line.lstrip())
            end_idx = start_idx + 1
            while end_idx < len(lines):
                line = lines[end_idx]
                if not line.strip():
                    end_idx += 1
                    continue
                indent = len(line) - len(line.lstrip())
                if indent <= base_indent:
                    break
                end_idx += 1
            while end_idx > start_idx + 1 and not lines[end_idx - 1].strip():
                end_idx -= 1
            return start_idx, end_idx

        # 1. Definition-based replacement (classes/methods)
        defs_in_code = []
        for idx, line in enumerate(code_lines):
            m = re.match(r"^(\s*)(def|class)\s+(\w+)\b", line)
            if m:
                defs_in_code.append((idx, m.group(1), m.group(2), m.group(3)))
                
        for code_idx, indent, kind, name in defs_in_code:
            orig_idx = -1
            for o_idx, o_line in enumerate(orig_lines):
                if re.match(rf"^\s*(def|class)\s+{name}\b", o_line):
                    orig_idx = o_idx
                    break
            
            if orig_idx != -1:
                c_start, c_end = _extract_indent_block(code_lines, code_idx)
                o_start, o_end = _extract_indent_block(orig_lines, orig_idx)
                replacement = code_lines[c_start:c_end]
                orig_lines[o_start:o_end] = replacement
                orig_lines = "\n".join(orig_lines).splitlines()
                modified = True
                if log_fn:
                    log_fn(f"    [Fallback] Replaced {kind} '{name}' in {rel_path} using indentation block replacement")

        # 2. Anchor-based replacement (if definition matching did not apply)
        if not modified:
            code_lines_norm = [l.strip() for l in code_lines if l.strip()]
            if len(code_lines_norm) >= 2:
                start_anchor = code_lines_norm[0]
                end_anchor = code_lines_norm[-1]
                
                start_candidates = [o_idx for o_idx, o_line in enumerate(orig_lines) if o_line.strip() == start_anchor]
                end_candidates = [o_idx for o_idx, o_line in enumerate(orig_lines) if o_line.strip() == end_anchor]
                
                best_pair = None
                min_dist = 999999
                for s_cand in start_candidates:
                    for e_cand in end_candidates:
                        if e_cand >= s_cand and (e_cand - s_cand) < min_dist:
                            min_dist = e_cand - s_cand
                            best_pair = (s_cand, e_cand)
                            
                if best_pair and min_dist < 150:
                    s_idx, e_idx = best_pair
                    orig_lines[s_idx:e_idx + 1] = code_lines
                    modified = True
                    if log_fn:
                        log_fn(f"    [Fallback] Replaced lines {s_idx+1} to {e_idx+1} in {rel_path} by matching anchors")

        # 3. Fuzzy block-matching replacement (DISABLED)
        # We no longer apply fuzzy matching to prevent hallucinated patches.
        if not modified:
            if log_fn:
                log_fn(f"    [Fallback] NO MATCH (Exact/Anchors) for block replacement in {rel_path}")

        if modified:
            stage.write(rel_path, "\n".join(orig_lines) + "\n", "fallback block replacement")
            return True
        return False

    def fallback_reparse(self, response_text: str, project_root: Path, stage: StageWriter, log_fn=None) -> int:
        if log_fn:
            log_fn("[DELTA] Starting fallback re-parse...")
        
        files_modified = set()
        
        # 1. Try loose SEARCH/REPLACE blocks
        lines = response_text.splitlines()
        current_file = None
        i = 0
        loose_blocks = []
        while i < len(lines):
            line = lines[i].strip()
            # Detect file header
            m_file = re.match(r"^(?:FILE|File|Patch for|Patch):\s*(.+)$", line)
            if m_file:
                current_file = m_file.group(1).strip("`* ")
                i += 1
                continue
            
            # Detect SEARCH
            if re.match(r"^(?:SEARCH|Search|search)(?::|<<<|:<<<)?\s*$", line.strip()):
                if not current_file:
                    preceding_lines = lines[:i]
                    preceding_text = "\n".join(preceding_lines)
                    current_file = self._find_file_candidate(preceding_text, project_root)
                if current_file:
                    i += 1
                    if not "<<<" in line.strip():
                        if i < len(lines) and lines[i].strip() in ("<<<", "<<<<<<<"):
                            i += 1
                    search_lines = []
                    while i < len(lines) and not (re.match(r"^(?:REPLACE|Replace|replace)(?::|<<<|:<<<)?\s*$", lines[i].strip()) or lines[i].strip() in (">>>", "=======")):
                        search_lines.append(lines[i])
                        i += 1
                    
                    # Now expect REPLACE
                    if i < len(lines) and (re.match(r"^(?:REPLACE|Replace|replace)(?::|<<<|:<<<)?\s*$", lines[i].strip()) or lines[i].strip() == "======="):
                        rep_marker = lines[i].strip()
                        if rep_marker == "=======":
                            i += 1
                        else:
                            i += 1
                            if not "<<<" in rep_marker:
                                if i < len(lines) and lines[i].strip() in ("<<<", "======="):
                                    i += 1
                        replace_lines = []
                        while i < len(lines) and not (lines[i].strip() in (">>>", ">>>>>>>") or (i+1 < len(lines) and re.match(r"^(?:SEARCH|Search|search)(?::|<<<|:<<<)?\s*$", lines[i+1].strip())) or lines[i].strip().startswith("FILE:")):
                            replace_lines.append(lines[i])
                            i += 1
                        if i < len(lines) and lines[i].strip() in (">>>", ">>>>>>>"):
                            i += 1
                        
                        loose_blocks.append({
                            "file": current_file,
                            "search": "\n".join(search_lines),
                            "replace": "\n".join(replace_lines)
                        })
                    continue
            i += 1
            
        if loose_blocks:
            if log_fn:
                log_fn(f"  [Fallback] Found {len(loose_blocks)} loose SEARCH/REPLACE block(s)")
            n_applied = self._apply_search_replace(loose_blocks, [], project_root, stage, log_fn)
            if n_applied > 0:
                return n_applied

        # 2. Try conflict markers directly
        conflict_pattern = re.compile(r"<<<<<<<.*?\n(.*?)\n=======\n(.*?)\n>>>>>>>", re.DOTALL)
        conflict_matches = list(conflict_pattern.finditer(response_text))
        if conflict_matches:
            if log_fn:
                log_fn(f"  [Fallback] Found {len(conflict_matches)} conflict marker block(s)")
            blocks = []
            for match in conflict_matches:
                search_content = match.group(1)
                replace_content = match.group(2)
                preceding_text = response_text[:match.start()]
                file_candidate = self._find_file_candidate(preceding_text, project_root)
                if file_candidate:
                    blocks.append({
                        "file": file_candidate,
                        "search": search_content,
                        "replace": replace_content
                    })
            if blocks:
                n_applied = self._apply_search_replace(blocks, [], project_root, stage, log_fn)
                if n_applied > 0:
                    return n_applied

        # 3. Code-block signature-based or fuzzy match fallback
        code_block_pattern = re.compile(r"```(?:python|py|ts|js|go)?\s*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)
        code_blocks = list(code_block_pattern.finditer(response_text))
        if code_blocks:
            if log_fn:
                log_fn(f"  [Fallback] Found {len(code_blocks)} code block(s)")
            for match in code_blocks:
                code_content = match.group(1).strip()
                if not code_content:
                    continue
                if "diff --git" in code_content or "--- a/" in code_content or "<<<<<<<" in code_content:
                    continue
                preceding_text = response_text[:match.start()]
                file_candidate = self._find_file_candidate(preceding_text, project_root)
                if not file_candidate:
                    file_candidate = self._find_file_candidate(response_text, project_root)
                
                if file_candidate:
                    if log_fn:
                        log_fn(f"  [Fallback] Code block mapped to file: {file_candidate}")
                    if self._apply_code_block_fallback(file_candidate, code_content, project_root, stage, log_fn):
                        files_modified.add(file_candidate)

        return len(files_modified)

    def _extract_unified_diff(self, text: str) -> str:
        # Check for diff/patch code block
        fenced_match = re.search(r"```(?:diff|patch)?\s*\n(.*?)\n```", text, re.DOTALL | re.IGNORECASE)
        if fenced_match:
            content = fenced_match.group(1).strip()
            if "---" in content or "+++" in content or "diff --git" in content:
                return content
        # Check raw lines
        lines = text.splitlines()
        start_idx = -1
        for idx, line in enumerate(lines):
            if line.startswith("diff --git") or line.startswith("--- a/") or line.startswith("--- "):
                start_idx = idx
                break
        if start_idx != -1:
            return "\n".join(lines[start_idx:]).strip()
        return ""

    def _get_modified_files_from_diff(self, diff_text: str) -> list[str]:
        files = []
        for line in diff_text.splitlines():
            if line.startswith("+++ "):
                path = line[4:].strip()
                if path.startswith("b/"):
                    path = path[2:]
                path = path.split("\t")[0].strip()
                path = path.replace("\\", "/")
                if path != "dev/null":
                    files.append(path)
        return list(set(files))

    def _apply_unified_diff(self, diff_text: str, project_root: Path, stage: StageWriter, log_fn=None) -> int:
        modified_files = self._get_modified_files_from_diff(diff_text)
        if log_fn:
            log_fn(f"[DELTA] Files modified in diff: {modified_files}")
        if not modified_files:
            if log_fn:
                log_fn("[DELTA] No files modified parsed from diff")
            return 0

        for rel_path in modified_files:
            abs_orig = project_root / rel_path
            abs_stage = stage.stage_dir / rel_path
            if abs_orig.exists():
                abs_stage.parent.mkdir(parents=True, exist_ok=True)
                import shutil
                try:
                    shutil.copy2(abs_orig, abs_stage)
                except Exception as e:
                    if log_fn:
                        log_fn(f"  [DELTA] Error copying {rel_path} to staging: {e}")
            else:
                abs_stage.parent.mkdir(parents=True, exist_ok=True)
            if not any(entry["path"] == rel_path for entry in stage.written):
                stage.written.append({"path": rel_path, "reason": "modified by unified diff", "staged": str(abs_stage)})

        temp_diff_file = stage.stage_dir / "temp_git_apply.patch"
        try:
            temp_diff_file.write_text(diff_text.replace("\r\n", "\n"), encoding="utf-8")
            import subprocess
            cmd = ["git", "apply", "--no-index", "--ignore-space-change", "--ignore-whitespace", "temp_git_apply.patch"]
            res = subprocess.run(cmd, cwd=stage.stage_dir, capture_output=True, text=True)
            if res.returncode == 0:
                if log_fn:
                    log_fn("  [DELTA] git apply --no-index succeeded")
                return len(modified_files)
            if log_fn:
                log_fn(f"  [DELTA] git apply --no-index failed: {res.stderr.strip()}")
            cmd2 = ["git", "apply", "--ignore-space-change", "--ignore-whitespace", "temp_git_apply.patch"]
            res2 = subprocess.run(cmd2, cwd=stage.stage_dir, capture_output=True, text=True)
            if res2.returncode == 0:
                if log_fn:
                    log_fn("  [DELTA] git apply succeeded")
                return len(modified_files)
            if log_fn:
                log_fn(f"  [DELTA] git apply failed: {res2.stderr.strip()}, trying Python fallback...")
            if _fallback_python_patch(stage.stage_dir, diff_text, log_fn):
                if log_fn:
                    log_fn("  [DELTA] Python structural block matcher succeeded")
                return len(modified_files)
            return 0
        except Exception as e:
            if log_fn:
                log_fn(f"  [DELTA] Error applying unified diff: {e}")
            return 0
        finally:
            if temp_diff_file.exists():
                temp_diff_file.unlink()

    def _apply_legacy_ops(self, ops: list[dict], project_root: Path, stage: StageWriter, log_fn=None) -> int:
        file_patches: dict[str, list[dict]] = {}
        new_files: list[dict] = []
        for op in ops:
            if op["type"] == "new_file":
                new_files.append(op)
            else:
                file_patches.setdefault(op["path"], []).append(op)
        files_written: set[str] = set()
        for rel_path, patches in file_patches.items():
            abs_path = project_root / rel_path
            try:
                source = abs_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                source = ""
            for patch in patches:
                source = self._apply_patch(source, patch, rel_path, log_fn)
            stage.write(rel_path, source, "patched by delta applicator")
            files_written.add(rel_path)
            if log_fn:
                log_fn(f"  >> Patched: {rel_path}")
        for nf in new_files:
            stage.write(nf["path"], nf["content"], "new file")
            files_written.add(nf["path"])
            if log_fn:
                log_fn(f"  >> New file: {nf['path']}")
        return len(files_written)

    def _parse(self, text: str) -> list[dict]:
        ops: list[dict] = []
        lines = text.splitlines()
        i = 0
        while i < len(lines):
            stripped = lines[i].strip()
            if stripped.startswith("PATCH:"):
                path = stripped[6:].strip()
                i += 1
                while i < len(lines) and not lines[i].strip():
                    i += 1
                if i >= len(lines) or not lines[i].strip().startswith("OP:"):
                    continue
                op_spec = lines[i].strip()[3:].strip()
                i += 1
                payload, i = self._read_payload(lines, i, op_spec)
                ops.append({"type": "patch", "path": path, "op": op_spec, "payload": payload})
            elif stripped.startswith("NEW_FILE:"):
                path = stripped[9:].strip()
                i += 1
                content, i = self._read_fenced(lines, i)
                ops.append({"type": "new_file", "path": path, "content": content})
            else:
                i += 1
        return ops

    def _read_payload(self, lines: list[str], i: int, op_spec: str) -> tuple[str, int]:
        if op_spec == "import":
            imp_lines: list[str] = []
            while i < len(lines):
                s = lines[i].strip()
                if (s.startswith("PATCH:") or s.startswith("NEW_FILE:")
                        or s.startswith("SUMMARY:") or s.startswith("```")):
                    break
                if s.startswith("import ") or s.startswith("from "):
                    imp_lines.append(s)
                i += 1
            return "\n".join(imp_lines), i
        while i < len(lines) and not lines[i].strip():
            i += 1
        return self._read_fenced(lines, i)

    def _read_fenced(self, lines: list[str], i: int) -> tuple[str, int]:
        if i < len(lines) and lines[i].strip().startswith("```"):
            i += 1
        content_lines: list[str] = []
        while i < len(lines):
            if lines[i].strip() == "```":
                i += 1
                break
            content_lines.append(lines[i])
            i += 1
        return "\n".join(content_lines), i

    def _apply_patch(self, source: str, patch: dict, rel_path: str, log_fn=None) -> str:
        op, payload = patch["op"], patch["payload"].strip()
        if op == "import":
            try:
                from core.ast_patcher import inject_import_at_top
                for line in payload.splitlines():
                    if line.strip():
                        source = inject_import_at_top(source, line.strip())
            except Exception as exc:
                if log_fn:
                    log_fn(f"  [DELTA] import injection failed ({rel_path}): {exc}")
        elif op == "append":
            source = source.rstrip() + "\n\n\n" + payload + "\n"
        elif op.startswith("prepend_to"):
            target = op[len("prepend_to"):].strip()
            class_name, method_name = target.rsplit(".", 1) if "." in target else ("", target)
            if rel_path.endswith(".py"):
                try:
                    from core.cst_patcher import prepend_to_method
                    source = prepend_to_method(source, method_name, payload, class_name)
                except Exception as exc:
                    if log_fn:
                        log_fn(f"  [DELTA] prepend_to '{target}' failed ({rel_path}): {exc} — falling back to append")
                    source = source.rstrip() + f"\n\n# TODO: insert inside {target}\n" + payload + "\n"
            elif log_fn:
                log_fn(f"  [DELTA] prepend_to skipped for non-Python file {rel_path}")
        elif op.startswith("replace_symbol"):
            symbol_target = op[len("replace_symbol"):].strip()
            if not rel_path.endswith(".py"):
                if log_fn:
                    log_fn(f"  [DELTA] replace_symbol skipped for non-Python file {rel_path}")
            elif not payload.strip():
                if log_fn:
                    log_fn(f"  [DELTA] replace_symbol '{symbol_target}' skipped — payload vuoto, simbolo preservato")
            else:
                try:
                    from core.cst_patcher import replace_symbol_in_place
                    source = replace_symbol_in_place(source, symbol_target, payload)
                    if log_fn:
                        log_fn(f"  [DELTA] replace_symbol '{symbol_target}' in-place OK")
                except Exception as exc:
                    if log_fn:
                        log_fn(f"  [DELTA] replace_symbol '{symbol_target}' in-place failed: {exc} — trying fallback...")
                    try:
                        from core.cst_patcher import remove_definitions_cst
                        if "." in symbol_target:
                            class_name, method_name = symbol_target.rsplit(".", 1)
                            source = _replace_method_in_class(source, class_name, method_name, payload, log_fn)
                        else:
                            source = remove_definitions_cst(source, [symbol_target])
                            source = source.rstrip() + "\n\n\n" + payload.strip() + "\n"
                        if log_fn:
                            log_fn(f"  [DELTA] replace_symbol '{symbol_target}' fallback OK")
                    except Exception as exc2:
                        if log_fn:
                            log_fn(f"  [DELTA] replace_symbol '{symbol_target}' fallback failed ({rel_path}): {exc2} — appending to end of file")
                        source = source.rstrip() + f"\n\n# REPLACED {symbol_target}:\n" + payload.strip() + "\n"
        else:
            if log_fn:
                log_fn(f"  [DELTA] unknown op '{op}' — skipped")
        return source
