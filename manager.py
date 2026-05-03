import os
import json
import shlex
import shutil
import time
import uuid
import signal
import subprocess
import threading
import logging
from pathlib import Path

BASE_DIR = Path(__file__).parent
SCRIPTS_DIR = BASE_DIR / "scripts"
LOGS_DIR = BASE_DIR / "logs"
DB_FILE = BASE_DIR / "database.json"

SCRIPTS_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)

logger = logging.getLogger("manager")

_db_lock = threading.Lock()
_procs: dict[str, subprocess.Popen] = {}
_watchers: dict[str, threading.Thread] = {}

_DEFAULT_PROJECT = {
    "status": "stopped",
    "pid": None,
    "start_file": "main.py",
    "startup_command": "",
    "start_time": None,
    "restarts": 0,
    "language": "",
}


# ─── Database ─────────────────────────────────────────────────────────────────

def _load_db() -> dict:
    if not DB_FILE.exists():
        return {}
    try:
        with open(DB_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_db(data: dict):
    with open(DB_FILE, "w") as f:
        json.dump(data, f, indent=2)


# ─── Path Helpers ─────────────────────────────────────────────────────────────

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _safe_display_name(name: str) -> str:
    return name.strip().replace("/", "").replace("\\", "")[:128]


def _safe_id(project_id: str) -> str | None:
    try:
        return str(uuid.UUID(str(project_id)))
    except (ValueError, AttributeError):
        return None


def _safe_filename(name: str) -> str:
    return os.path.basename(name)


def _safe_rel_path(proj_dir: Path, rel: str) -> Path | None:
    rel = rel.strip().lstrip("/\\").replace("\\", "/")
    if not rel:
        return None
    parts = rel.split("/")
    if any(p == ".." or p == "." for p in parts if p):
        return None
    try:
        candidate = (proj_dir / rel).resolve()
        candidate.relative_to(proj_dir.resolve())
        return candidate
    except (ValueError, OSError):
        return None


def _project_dir(project_id: str) -> Path:
    return SCRIPTS_DIR / project_id


def _log_path(project_id: str) -> Path:
    return LOGS_DIR / f"{project_id}.log"


def _reconcile():
    with _db_lock:
        db = _load_db()
        changed = False
        for proj_id, info in db.items():
            if info.get("status") == "running":
                pid = info.get("pid")
                if pid and not _pid_alive(pid):
                    db[proj_id]["status"] = "stopped"
                    db[proj_id]["pid"] = None
                    changed = True
        if changed:
            _save_db(db)


# ─── Project Management ───────────────────────────────────────────────────────

def create_project(name: str, startup_command: str = "", start_file: str = "", language: str = "") -> dict:
    name = _safe_display_name(name)
    if not name:
        return {"ok": False, "error": "Invalid project name"}
    project_id = str(uuid.uuid4())
    proj_dir = _project_dir(project_id)
    proj_dir.mkdir(parents=True)
    with _db_lock:
        db = _load_db()
        defaults = dict(_DEFAULT_PROJECT)
        defaults["id"] = project_id
        defaults["name"] = name
        if startup_command:
            defaults["startup_command"] = startup_command
        if start_file:
            defaults["start_file"] = start_file
        if language:
            defaults["language"] = language
        db[project_id] = defaults
        _save_db(db)
    return {"ok": True, "id": project_id, "name": name}


def list_projects() -> list[dict]:
    _reconcile()
    db = _load_db()
    projects = []
    for proj_id, info in db.items():
        proj_dir = _project_dir(proj_id)
        if not proj_dir.exists():
            continue
        start_time = info.get("start_time")
        uptime = None
        if start_time and info.get("status") == "running":
            uptime = int(time.time() - start_time)
        files = list_project_files(proj_id)
        projects.append({
            "id": proj_id,
            "name": info.get("name", proj_id),
            "status": info.get("status", "stopped"),
            "pid": info.get("pid"),
            "uptime": uptime,
            "restarts": info.get("restarts", 0),
            "start_time": start_time,
            "start_file": info.get("start_file", "main.py"),
            "startup_command": info.get("startup_command", ""),
            "language": info.get("language", ""),
            "files": files,
        })
    return sorted(projects, key=lambda x: x["name"].lower())


def get_project(project_id: str) -> dict | None:
    pid_val = _safe_id(project_id)
    if not pid_val:
        return None
    proj_dir = _project_dir(pid_val)
    if not proj_dir.exists():
        return None
    _reconcile()
    db = _load_db()
    info = db.get(pid_val, {})
    start_time = info.get("start_time")
    uptime = None
    if start_time and info.get("status") == "running":
        uptime = int(time.time() - start_time)
    return {
        "id": pid_val,
        "name": info.get("name", pid_val),
        "status": info.get("status", "stopped"),
        "pid": info.get("pid"),
        "uptime": uptime,
        "restarts": info.get("restarts", 0),
        "start_time": start_time,
        "start_file": info.get("start_file", "main.py"),
        "startup_command": info.get("startup_command", ""),
        "language": info.get("language", ""),
        "files": list_project_files(pid_val),
    }


def set_start_file(project_id: str, filename: str) -> dict:
    pid_val = _safe_id(project_id)
    if not pid_val:
        return {"ok": False, "error": "Invalid project ID"}
    filename = _safe_filename(filename)
    proj_dir = _project_dir(pid_val)
    if not proj_dir.exists():
        return {"ok": False, "error": "Project not found"}
    if not (proj_dir / filename).exists():
        return {"ok": False, "error": f"{filename} not found in project"}
    with _db_lock:
        db = _load_db()
        if pid_val not in db:
            return {"ok": False, "error": "Project not found"}
        db[pid_val]["start_file"] = filename
        _save_db(db)
    return {"ok": True}


def set_startup_command(project_id: str, command: str) -> dict:
    pid_val = _safe_id(project_id)
    if not pid_val:
        return {"ok": False, "error": "Invalid project ID"}
    if not _project_dir(pid_val).exists():
        return {"ok": False, "error": "Project not found"}
    with _db_lock:
        db = _load_db()
        if pid_val not in db:
            return {"ok": False, "error": "Project not found"}
        db[pid_val]["startup_command"] = command.strip()
        _save_db(db)
    return {"ok": True}


def delete_project(project_id: str) -> dict:
    pid_val = _safe_id(project_id)
    if not pid_val:
        return {"ok": False, "error": "Invalid project ID"}
    stop_project(pid_val)
    proj_dir = _project_dir(pid_val)
    log = _log_path(pid_val)
    if proj_dir.exists():
        shutil.rmtree(proj_dir)
    if log.exists():
        log.unlink()
    with _db_lock:
        db = _load_db()
        db.pop(pid_val, None)
        _save_db(db)
    return {"ok": True}


# ─── File & Folder Management ─────────────────────────────────────────────────

def list_project_files(project_id: str) -> list[str]:
    proj_dir = _project_dir(project_id)
    if not proj_dir.exists():
        return []
    return sorted(f.name for f in proj_dir.iterdir() if f.is_file())


def _tree_node(base: Path, path: Path, depth: int = 0, max_depth: int = 10) -> list:
    if depth >= max_depth:
        return []
    result = []
    try:
        entries = sorted(path.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
        for entry in entries:
            rel = str(entry.relative_to(base))
            if entry.is_dir():
                result.append({
                    "name": entry.name,
                    "path": rel,
                    "type": "dir",
                    "children": _tree_node(base, entry, depth + 1, max_depth),
                })
            else:
                result.append({"name": entry.name, "path": rel, "type": "file"})
    except PermissionError:
        pass
    return result


def list_project_tree(project_id: str) -> list:
    pid_val = _safe_id(project_id)
    if not pid_val:
        return []
    proj_dir = _project_dir(pid_val)
    if not proj_dir.exists():
        return []
    return _tree_node(proj_dir, proj_dir)


def create_project_file(project_id: str, filepath: str) -> dict:
    pid_val = _safe_id(project_id)
    if not pid_val:
        return {"ok": False, "error": "Invalid project ID"}
    proj_dir = _project_dir(pid_val)
    if not proj_dir.exists():
        return {"ok": False, "error": "Project not found"}
    target = _safe_rel_path(proj_dir, filepath)
    if not target:
        return {"ok": False, "error": "Invalid filepath"}
    if target.exists():
        return {"ok": False, "error": "File already exists"}
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("")
    return {"ok": True, "name": str(target.relative_to(proj_dir))}


def create_project_folder(project_id: str, folder_path: str) -> dict:
    pid_val = _safe_id(project_id)
    if not pid_val:
        return {"ok": False, "error": "Invalid project ID"}
    proj_dir = _project_dir(pid_val)
    if not proj_dir.exists():
        return {"ok": False, "error": "Project not found"}
    target = _safe_rel_path(proj_dir, folder_path)
    if not target:
        return {"ok": False, "error": "Invalid folder path"}
    if target.exists():
        return {"ok": False, "error": "Already exists"}
    target.mkdir(parents=True, exist_ok=True)
    return {"ok": True, "path": str(target.relative_to(proj_dir))}


def delete_project_item(project_id: str, item_path: str) -> dict:
    pid_val = _safe_id(project_id)
    if not pid_val:
        return {"ok": False, "error": "Invalid project ID"}
    proj_dir = _project_dir(pid_val)
    if not proj_dir.exists():
        return {"ok": False, "error": "Project not found"}
    target = _safe_rel_path(proj_dir, item_path)
    if not target:
        return {"ok": False, "error": "Invalid path"}
    if not target.exists():
        return {"ok": False, "error": "Not found"}
    if target.resolve() == proj_dir.resolve():
        return {"ok": False, "error": "Cannot delete project root"}
    rel = str(target.relative_to(proj_dir.resolve()))
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
        with _db_lock:
            db = _load_db()
            info = db.get(pid_val, {})
            if info.get("start_file") in (rel, target.name):
                remaining = list_project_files(pid_val)
                db[pid_val]["start_file"] = remaining[0] if remaining else "main.py"
                _save_db(db)
    return {"ok": True}


def delete_project_file(project_id: str, filename: str) -> dict:
    return delete_project_item(project_id, filename)


def move_project_item(project_id: str, src: str, dest: str) -> dict:
    pid_val = _safe_id(project_id)
    if not pid_val:
        return {"ok": False, "error": "Invalid project ID"}
    proj_dir = _project_dir(pid_val)
    src_path = _safe_rel_path(proj_dir, src)
    dest_path = _safe_rel_path(proj_dir, dest)
    if not src_path or not dest_path:
        return {"ok": False, "error": "Invalid path"}
    if not src_path.exists():
        return {"ok": False, "error": "Source not found"}
    if dest_path.exists():
        return {"ok": False, "error": "Destination already exists"}
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    src_path.rename(dest_path)
    src_rel = str(src_path.relative_to(proj_dir))
    dest_rel = str(dest_path.relative_to(proj_dir))
    with _db_lock:
        db = _load_db()
        info = db.get(pid_val, {})
        sf = info.get("start_file", "")
        if sf and (sf == src_rel or sf == src_path.name):
            db[pid_val]["start_file"] = dest_path.name
            _save_db(db)
    return {"ok": True, "src": src_rel, "dest": dest_rel}


def upload_file_to_project(project_id: str, filename: str, content: bytes) -> dict:
    pid_val = _safe_id(project_id)
    if not pid_val:
        return {"ok": False, "error": "Invalid project ID"}
    filename = _safe_filename(filename)
    if ".." in filename or "/" in filename or "\\" in filename:
        return {"ok": False, "error": "Invalid filename"}
    proj_dir = _project_dir(pid_val)
    if not proj_dir.exists():
        return {"ok": False, "error": "Project not found"}
    with open(proj_dir / filename, "wb") as f:
        f.write(content)
    return {"ok": True, "name": filename}


def get_file_content(project_id: str, filepath: str) -> str | None:
    pid_val = _safe_id(project_id)
    if not pid_val:
        return None
    proj_dir = _project_dir(pid_val)
    fpath = _safe_rel_path(proj_dir, filepath)
    if not fpath or not fpath.is_file():
        return None
    try:
        return fpath.read_text(errors="replace")
    except Exception:
        return None


def save_file_content(project_id: str, filepath: str, content: str) -> dict:
    pid_val = _safe_id(project_id)
    if not pid_val:
        return {"ok": False, "error": "Invalid project ID"}
    proj_dir = _project_dir(pid_val)
    fpath = _safe_rel_path(proj_dir, filepath)
    if not fpath:
        return {"ok": False, "error": "Invalid path"}
    fpath.parent.mkdir(parents=True, exist_ok=True)
    fpath.write_text(content)
    return {"ok": True}


# ─── Package Management ───────────────────────────────────────────────────────

def _pip_cmd() -> list[str]:
    import sys
    return [sys.executable, "-m", "pip"]


def install_package(project_id: str, package: str) -> dict:
    pid_val = _safe_id(project_id)
    if not pid_val or not _project_dir(pid_val).exists():
        return {"ok": False, "error": "Project not found", "output": ""}
    package = package.strip()
    if not package or any(c in package for c in [";", "&", "|", "`", "$", "(", ")", "\n", "\r"]):
        return {"ok": False, "error": "Invalid package name", "output": ""}
    try:
        result = subprocess.run(
            _pip_cmd() + ["install", "--break-system-packages", package],
            capture_output=True, text=True, timeout=120,
        )
        output = result.stdout + result.stderr
        return {"ok": result.returncode == 0, "output": output, "returncode": result.returncode}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Installation timed out", "output": ""}
    except Exception as e:
        return {"ok": False, "error": str(e), "output": ""}


def uninstall_package(project_id: str, package: str) -> dict:
    pid_val = _safe_id(project_id)
    if not pid_val:
        return {"ok": False, "error": "Invalid project ID", "output": ""}
    package = package.strip()
    if not package or any(c in package for c in [";", "&", "|", "`", "$", "(", ")", "\n"]):
        return {"ok": False, "error": "Invalid package name", "output": ""}
    try:
        result = subprocess.run(
            _pip_cmd() + ["uninstall", "--break-system-packages", "-y", package],
            capture_output=True, text=True, timeout=60,
        )
        output = result.stdout + result.stderr
        return {"ok": result.returncode == 0, "output": output}
    except Exception as e:
        return {"ok": False, "error": str(e), "output": ""}


# ─── Process Control ──────────────────────────────────────────────────────────

def _write_system(project_id: str, msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"\n{'─'*60}\n[SYSTEM] {ts}  {msg}\n{'─'*60}\n\n"
    try:
        with open(_log_path(project_id), "a") as f:
            f.write(line)
    except Exception:
        pass


def clear_log(project_id: str) -> dict:
    pid_val = _safe_id(project_id)
    if not pid_val:
        return {"ok": False, "error": "Invalid project ID"}
    log = _log_path(pid_val)
    try:
        log.write_text("")
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True}


def _build_command(info: dict) -> list[str]:
    start_file = info.get("start_file", "main.py")
    custom = info.get("startup_command", "").strip()
    if custom:
        cmd_str = custom.replace("{start_file}", start_file)
        try:
            return shlex.split(cmd_str)
        except Exception:
            return cmd_str.split()
    return ["python3", "-u", start_file]


def _watch_project(project_id: str, proc: subprocess.Popen):
    start_ts = time.time()
    proc.wait()
    run_time = time.time() - start_ts
    with _db_lock:
        db = _load_db()
        if project_id not in db:
            return
        info = db[project_id]
        if info.get("status") != "running":
            return
        restarts = info.get("restarts", 0) + 1
        if run_time < 5:
            crash_count = info.get("crash_count", 0) + 1
        else:
            crash_count = 0
        if crash_count >= 2:
            db[project_id]["status"] = "error"
            db[project_id]["pid"] = None
            db[project_id]["restarts"] = restarts
            db[project_id]["crash_count"] = crash_count
            _save_db(db)
            logger.warning(f"Project {project_id} crashed {crash_count} times in < 5 s — giving up")
            _write_system(project_id, f"ERROR — crashed {crash_count} times in < 5 s, giving up. Fix the code and start manually.")
            return
        db[project_id]["restarts"] = restarts
        db[project_id]["crash_count"] = crash_count
        db[project_id]["status"] = "restarting"
        _save_db(db)
    logger.info(f"Project {project_id} crashed (uptime {run_time:.1f}s), auto-restarting in 2s…")
    _write_system(project_id, f"Crashed (uptime {run_time:.1f}s) — auto-restarting in 2 s…")
    time.sleep(2)
    with _db_lock:
        db = _load_db()
        if db.get(project_id, {}).get("status") != "restarting":
            return
    start_project(project_id, auto_restart=True)


def start_project(project_id: str, auto_restart: bool = False) -> dict:
    pid_val = _safe_id(project_id)
    if not pid_val:
        return {"ok": False, "error": "Invalid project ID"}
    proj_dir = _project_dir(pid_val)
    if not proj_dir.exists():
        return {"ok": False, "error": f"Project not found"}
    with _db_lock:
        db = _load_db()
        info = db.get(pid_val, {})
    start_file = info.get("start_file", "main.py")
    entry = proj_dir / start_file
    custom_cmd = info.get("startup_command", "").strip()
    if not custom_cmd and not entry.exists():
        return {"ok": False, "error": f"Start file '{start_file}' not found in project"}
    if not auto_restart:
        pid = info.get("pid")
        if info.get("status") == "running" and pid and _pid_alive(pid):
            return {"ok": False, "error": "Already running"}
        log_p = _log_path(pid_val)
        if log_p.exists() and (time.time() - log_p.stat().st_mtime) > 300:
            log_p.write_text("")
    cmd = _build_command(info)
    action = "Auto-restarted" if auto_restart else "Started"
    _write_system(pid_val, f"{action}  ·  command: {' '.join(cmd)}")
    log_file = open(_log_path(pid_val), "a")
    proc = subprocess.Popen(
        cmd,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        cwd=str(proj_dir),
        start_new_session=True,
    )
    _procs[pid_val] = proc
    with _db_lock:
        db = _load_db()
        existing = db.get(pid_val, {})
        db[pid_val] = {
            **existing,
            "pid": proc.pid,
            "status": "running",
            "start_time": time.time(),
            "restarts": existing.get("restarts", 0) if auto_restart else 0,
            "crash_count": existing.get("crash_count", 0) if auto_restart else 0,
        }
        _save_db(db)
    t = threading.Thread(target=_watch_project, args=(pid_val, proc), daemon=True)
    _watchers[pid_val] = t
    t.start()
    return {"ok": True, "pid": proc.pid}


def stop_project(project_id: str) -> dict:
    pid_val = _safe_id(project_id)
    if not pid_val:
        return {"ok": False, "error": "Invalid project ID"}
    with _db_lock:
        db = _load_db()
        info = db.get(pid_val, {})
        pid = info.get("pid")
        db[pid_val] = {**info, "status": "stopped", "pid": None, "crash_count": 0}
        _save_db(db)
    _procs.pop(pid_val, None)
    if not pid or not _pid_alive(pid):
        _write_system(pid_val, "Stopped")
        return {"ok": True, "message": "Already stopped"}
    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGTERM)
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            pass
    for _ in range(30):
        time.sleep(0.1)
        if not _pid_alive(pid):
            _write_system(pid_val, "Stopped")
            return {"ok": True}
    try:
        pgid = os.getpgid(pid)
        os.killpg(pgid, signal.SIGKILL)
    except Exception:
        try:
            os.kill(pid, signal.SIGKILL)
        except Exception:
            pass
    _write_system(pid_val, "Stopped (force-killed)")
    return {"ok": True}


def restart_project(project_id: str) -> dict:
    stop_project(project_id)
    time.sleep(0.5)
    return start_project(project_id)


def restart_all() -> list[dict]:
    results = []
    for p in list_projects():
        if p["status"] == "running":
            results.append({p["id"]: restart_project(p["id"])})
    return results


def get_log_lines(project_id: str, n: int = 20) -> list[str]:
    pid_val = _safe_id(project_id)
    if not pid_val:
        return []
    log = _log_path(pid_val)
    if not log.exists():
        return []
    with open(log) as f:
        lines = f.readlines()
    return [ln.rstrip() for ln in lines[-n:]]


_reconcile()
