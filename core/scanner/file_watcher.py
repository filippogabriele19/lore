import time
import os
import threading
from pathlib import Path
from core.symbol_map import SymbolDB, scan_files, embed_changed, _is_ignored

# Try importing watchdog
try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    HAS_WATCHDOG = True
except ImportError:
    HAS_WATCHDOG = False

class FileWatcher:
    def __init__(self, project_root: Path, db_path: Path, interval: float = 1.0):
        self.project_root = project_root
        self.db_path = db_path
        self.interval = interval
        self.mtimes = {}
        self.supported_extensions = {".py", ".ts", ".tsx", ".js", ".jsx", ".go"}
        self._init_mtimes()
        
        # Event tracking for watchdog
        self.changed_queue = set()
        self.lock = threading.Lock()

    def _init_mtimes(self):
        for ext in ("*.py", "*.ts", "*.tsx", "*.js", "*.jsx", "*.go"):
            for f in self.project_root.rglob(ext):
                if not _is_ignored(f, self.project_root):
                    try:
                        self.mtimes[f] = f.stat().st_mtime
                    except OSError:
                        pass

    def check_changes_fallback(self) -> list[str]:
        changed_files = []
        current_files = set()
        
        for ext in ("*.py", "*.ts", "*.tsx", "*.js", "*.jsx", "*.go"):
            for f in self.project_root.rglob(ext):
                if _is_ignored(f, self.project_root):
                    continue
                current_files.add(f)
                try:
                    mtime = f.stat().st_mtime
                    if f not in self.mtimes:
                        self.mtimes[f] = mtime
                        changed_files.append(str(f.relative_to(self.project_root)))
                    elif mtime > self.mtimes[f]:
                        self.mtimes[f] = mtime
                        changed_files.append(str(f.relative_to(self.project_root)))
                except OSError:
                    pass
                    
        deleted_files = []
        for f in list(self.mtimes.keys()):
            if f not in current_files:
                del self.mtimes[f]
                deleted_files.append(str(f.relative_to(self.project_root)))
                
        return changed_files + deleted_files

    def start(self, once: bool = False):
        if HAS_WATCHDOG and not once:
            self._start_watchdog()
        else:
            self._start_polling(once)

    def _start_polling(self, once: bool):
        print(f"[WATCH] Monitoraggio incrementale attivo (Polling) su {self.project_root} (interval={self.interval}s)", flush=True)
        db = SymbolDB(self.db_path)
        try:
            while True:
                changed = self.check_changes_fallback()
                if changed:
                    print(f"[WATCH] Rilevate modifiche in {len(changed)} file: {', '.join(changed)}", flush=True)
                    processed = scan_files(db, self.project_root, changed)
                    embed_changed(db, self.project_root, changed)
                    print(f"[WATCH] Indicizzazione incrementale completata per {processed} file.", flush=True)
                if once:
                    break
                time.sleep(self.interval)
        except KeyboardInterrupt:
            print("\n[WATCH] Watcher arrestato.", flush=True)
        finally:
            db.close()

    def _start_watchdog(self):
        print(f"[WATCH] Monitoraggio incrementale attivo (Watchdog Event-driven) su {self.project_root}", flush=True)
        db = SymbolDB(self.db_path)
        
        watcher_self = self
        class LoreHandler(FileSystemEventHandler):
            def on_any_event(self, event):
                if event.is_directory:
                    return
                src_path = Path(event.src_path)
                if src_path.suffix in watcher_self.supported_extensions:
                    if not _is_ignored(src_path, watcher_self.project_root):
                        rel_path = str(src_path.relative_to(watcher_self.project_root))
                        with watcher_self.lock:
                            watcher_self.changed_queue.add(rel_path)

        event_handler = LoreHandler()
        observer = Observer()
        observer.schedule(event_handler, str(self.project_root), recursive=True)
        observer.start()
        
        try:
            while True:
                time.sleep(self.interval)
                with self.lock:
                    if self.changed_queue:
                        changed = list(self.changed_queue)
                        self.changed_queue.clear()
                        print(f"[WATCH] Rilevate modifiche in {len(changed)} file: {', '.join(changed)}", flush=True)
                        processed = scan_files(db, self.project_root, changed)
                        embed_changed(db, self.project_root, changed)
                        print(f"[WATCH] Indicizzazione incrementale completata per {processed} file.", flush=True)
        except KeyboardInterrupt:
            print("\n[WATCH] Watcher arrestato.", flush=True)
        finally:
            observer.stop()
            observer.join()
            db.close()
