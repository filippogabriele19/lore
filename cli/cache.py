import re
import shutil
import subprocess
from pathlib import Path
from cli.shared import console
from core.symbol_map import SymbolDB, scan as fow_scan, embed_all_symbols, scan_files

def _run_git(cmd_args: list[str], cwd: Path) -> str | None:
    """Execute a git command safely and return its stripped stdout."""
    try:
        result = subprocess.run(
            ["git"] + cmd_args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace"
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None

def _sanitize_repo_key(repo_identifier: str) -> str:
    """Sanitize git repository URL or directory name to use as a filename key."""
    if repo_identifier.endswith(".git"):
        repo_identifier = repo_identifier[:-4]
    
    # Remove protocols and host signatures
    if "://" in repo_identifier:
        repo_identifier = repo_identifier.split("://", 1)[1]
    if "@" in repo_identifier:
        repo_identifier = repo_identifier.split("@", 1)[1]
    if ":" in repo_identifier:
        repo_identifier = repo_identifier.split(":", 1)[1]
        
    # Replace non-alphanumeric/dash/underscore with underscores
    key = re.sub(r'[^a-zA-Z0-9_\-]', '_', repo_identifier)
    return key.strip("_") or "default_project"

def restore_or_create_db(project_root: Path, db_path: Path, rescan: bool = False) -> SymbolDB:
    """
    Restore a cached SymbolDB from a central directory (~/.cache/lore/graphs/) based on git commit hash,
    or update/create it. Supports incremental updates if a nearby commit is cached.
    """
    # 1. Identify repo, commit, and cache directory
    repo_url = _run_git(["config", "--get", "remote.origin.url"], project_root)
    commit_hash = _run_git(["rev-parse", "HEAD"], project_root)
    repo_identifier = repo_url or project_root.name
    repo_key = _sanitize_repo_key(repo_identifier)
    
    cache_dir = Path.home() / ".cache" / "lore" / "graphs"
    cache_dir.mkdir(parents=True, exist_ok=True)
    
    exact_cache_file = cache_dir / f"{repo_key}_{commit_hash}.db" if commit_hash else None

    # Handle force rescan request
    if rescan:
        console.print("[warning]Rescan forzato richiesto. Rimozione indici esistenti...[/]")
        if db_path.exists():
            try:
                db_path.unlink()
            except Exception:
                pass
        if exact_cache_file and exact_cache_file.exists():
            try:
                exact_cache_file.unlink()
            except Exception:
                pass

    # 2. Local DB already exists in the project workspace
    if db_path.exists():
        db = SymbolDB(db_path)
        db_commit = None
        try:
            row = db.con.execute("SELECT value FROM meta WHERE key='last_commit_hash'").fetchone()
            if row:
                db_commit = row["value"]
        except Exception:
            pass
            
        if commit_hash and db_commit == commit_hash:
            console.print(f"[success]✔[/] [bold blue]Knowledge Graph:[/] Database locale è già allineato al commit [bold cyan]{commit_hash[:8]}[/].")
            return db
            
        # If commits differ, attempt incremental update on the existing local DB
        if commit_hash and db_commit:
            diff_str = _run_git(["diff", "--name-only", db_commit, commit_hash], project_root)
            if diff_str is not None:
                changed_files = [line.strip() for line in diff_str.splitlines() if line.strip()]
                console.print(f"[info]Rilevate differenze tra commit locale ({db_commit[:8]}) e HEAD ({commit_hash[:8]}).[/]")
                console.print(f"[info]Aggiornamento incrementale di {len(changed_files)} file...[/]")
                
                with console.status("[accent]Aggiornamento incrementale AST...[/]") as status:
                    scan_files(db, project_root, changed_files)
                with console.status("[accent]Aggiornamento embedding semantici...[/]") as status:
                    embedded = embed_all_symbols(db, project_root)
                    
                if embedded:
                    console.print(f"[success]Aggiornamento completato! Generati {embedded} nuovi embedding.[/]")
                else:
                    console.print("[success]Tutti i simboli sono aggiornati.[/]")
                
                db.set_meta("last_commit_hash", commit_hash)
                db.commit()
                
                # Update central cache file
                if exact_cache_file:
                    db.close()
                    try:
                        shutil.copy2(db_path, exact_cache_file)
                    except Exception:
                        pass
                    db = SymbolDB(db_path)
                    
                return db
        
        # If incremental update isn't possible (e.g. no git history match), delete and recreate
        db.close()
        try:
            db_path.unlink()
        except Exception:
            pass

    # 3. Cache search (exact hit)
    if exact_cache_file and exact_cache_file.exists():
        try:
            shutil.copy2(exact_cache_file, db_path)
            console.print(f"[success]✔ Cache Hit![/] Ripristinato database pre-indicizzato per il commit [bold cyan]{commit_hash[:8]}[/].")
            return SymbolDB(db_path)
        except Exception as e:
            console.print(f"[warning]Impossibile caricare il database da cache: {e}. Ricostruzione in corso...[/]")

    # 4. Cache search (closest/nearby commit hit)
    if commit_hash:
        candidate_files = list(cache_dir.glob(f"{repo_key}_*.db"))
        if candidate_files:
            best_candidate_path = None
            best_distance = float('inf')
            best_hash = None
            
            # Fetch the recent history of the current branch in one go (max 2000 commits for broad cross-commit matching)
            history_str = _run_git(["log", "--format=%H", "-n", "2000"], project_root)
            recent_commits = []
            if history_str:
                recent_commits = [line.strip() for line in history_str.splitlines() if line.strip()]
            commit_to_distance = {h: dist for dist, h in enumerate(recent_commits)}
            
            for file_path in candidate_files:
                name = file_path.stem
                cand_hash = name[len(repo_key) + 1:]
                if cand_hash in commit_to_distance:
                    dist = commit_to_distance[cand_hash]
                    if dist < best_distance:
                        best_distance = dist
                        best_candidate_path = file_path
                        best_hash = cand_hash
            
            # Fallback: if no direct ancestor in the active branch history is cached,
            # look for ANY cached commit that exists in the local git repository objects.
            if not best_candidate_path:
                for file_path in candidate_files:
                    name = file_path.stem
                    cand_hash = name[len(repo_key) + 1:]
                    
                    # Verify if the cached commit object exists in our local repository
                    exists_type = _run_git(["cat-file", "-t", cand_hash], project_root)
                    if exists_type == "commit":
                        best_candidate_path = file_path
                        best_hash = cand_hash
                        best_distance = 999999
                        break
            
            # If a base candidate is found, copy it and apply diff updates
            if best_candidate_path:
                diff_str = _run_git(["diff", "--name-only", best_hash, "HEAD"], project_root)
                if diff_str is not None:
                    changed_files = [line.strip() for line in diff_str.splitlines() if line.strip()]
                    try:
                        shutil.copy2(best_candidate_path, db_path)
                        dist_lbl = f"distanza: {best_distance} commit" if best_distance < 999999 else "commit non-lineare"
                        console.print(f"[success]✔ Cache Hit (vicino)![/] Copiato database dal commit [bold yellow]{best_hash[:8]}[/] ({dist_lbl}).")
                        console.print(f"[info]Applicazione incrementale delle modifiche per {len(changed_files)} file cambiati...[/]")
                        
                        db = SymbolDB(db_path)
                        with console.status("[accent]Aggiornamento incrementale AST...[/]") as status:
                            scan_files(db, project_root, changed_files)
                        with console.status("[accent]Aggiornamento embedding semantici...[/]") as status:
                            embedded = embed_all_symbols(db, project_root)
                        
                        db.set_meta("last_commit_hash", commit_hash)
                        db.commit()
                        db.close()
                        
                        # Cache the newly aligned DB
                        try:
                            shutil.copy2(db_path, exact_cache_file)
                        except Exception:
                            pass
                            
                        return SymbolDB(db_path)
                    except Exception as e:
                        console.print(f"[warning]Aggiornamento incrementale da cache fallito: {e}. Ricostruzione completa...[/]")
                        if db_path.exists():
                            try:
                                db_path.unlink()
                            except Exception:
                                pass

    # 5. Full build (Cache Miss)
    console.print("[warning]Nessun database indicizzato in cache. Avvio indicizzazione completa (solo la prima volta per questo repository)...[/]")
    db = SymbolDB(db_path)
    
    with console.status("[accent]Scansione e parsing AST in corso...[/]") as status:
        fow_scan(project_root, db)
    with console.status("[accent]Generazione embedding semantici in corso...[/]") as status:
        embedded = embed_all_symbols(db, project_root)
    
    if commit_hash:
        db.set_meta("last_commit_hash", commit_hash)
        db.commit()
        db.close()
        
        # Save to cache
        try:
            shutil.copy2(db_path, exact_cache_file)
            console.print(f"[info]Database dei simboli grezzo salvato in cache per il commit {commit_hash[:8]}.[/]")
        except Exception as e:
            console.print(f"[warning]Impossibile salvare il database in cache: {e}[/]")
            
        db = SymbolDB(db_path)
        
    console.print(f"[success]Indicizzazione iniziale completata! Generati {embedded} embedding.[/]")
    return db

def save_db_to_cache(project_root: Path, db_path: Path) -> None:
    """
    Saves the current state of the SymbolDB from the project workspace
    to the central cache directory (~/.cache/lore/graphs/) for the current commit.
    """
    repo_url = _run_git(["config", "--get", "remote.origin.url"], project_root)
    commit_hash = _run_git(["rev-parse", "HEAD"], project_root)
    repo_identifier = repo_url or project_root.name
    repo_key = _sanitize_repo_key(repo_identifier)
    
    cache_dir = Path.home() / ".cache" / "lore" / "graphs"
    cache_dir.mkdir(parents=True, exist_ok=True)
    
    exact_cache_file = cache_dir / f"{repo_key}_{commit_hash}.db" if commit_hash else None
    if exact_cache_file:
        try:
            shutil.copy2(db_path, exact_cache_file)
            console.print(f"[success]✔ Cache Aggiornata![/] Database con metadati git e intenti salvato per il commit [bold cyan]{commit_hash[:8]}[/].")
        except Exception as e:
            console.print(f"[warning]Impossibile aggiornare il database in cache: {e}[/]")
