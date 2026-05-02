import os
import json
import shlex
import time
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


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _safe_project_name(name: str) -> str:
    name = os.path.basename(name.strip().replace(" ", "_"))
    return name


def _safe_filename(name: str) -> str:
    return os.path.basename(name)


def _project_dir(name: str) -> Path:
    return SCRIPTS_DIR / name


def _log_path(name: str) -> Path:
    return LOGS_DIR / f"{name}.log"


def _reconcile():
    with _db_lock:
        db = _load_db()
        changed = False
        for name, info in db.items():
            if info.get("status") == "running":
                pid = info.get("pid")
                if pid and not _pid_alive(pid):
                    db[name]["status"] = "stopped"
                    db[name]["pid"] = None
                    changed = True
        if changed:
            _save_db(db)


# ─── Project Management ───────────────────────────────────────────────────────

def create_project(name: str) -> dict:
    name = _safe_project_name(name)
    if not name:
        return {"ok": False, "error": "Invalid project name"}
    proj_dir = _project_dir(name)
    if proj_dir.exists():
        return {"ok": False, "error": "Project already exists"}
    proj_dir.mkdir(parents=True)
    with _db_lock:
        db = _load_db()
        db[name] = dict(_DEFAULT_PROJECT)
        _save_db(db)
    return {"ok": True, "name": name}


def list_projects() -> list[dict]:
    _reconcile()
    db = _load_db()
    projects = []
    for proj_dir in sorted(SCRIPTS_DIR.iterdir()):
        if not proj_dir.is_dir():
            continue
        name = proj_dir.name
        info = db.get(name, {})
        if name not in db:
            with _db_lock:
                db2 = _load_db()
                db2[name] = dict(_DEFAULT_PROJECT)
                _save_db(db2)
            info = db2[name]

        pid = info.get("pid")
        start_time = info.get("start_time")
        uptime = None
        if start_time and info.get("status") == "running":
            uptime = int(time.time() - start_time)

        files = list_project_files(name)
        projects.append({
            "name": name,
            "status": info.get("status", "stopped"),
            "pid": pid,
            "uptime": uptime,
            "restarts": info.get("restarts", 0),
            "start_time": start_time,
            "start_file": info.get("start_file", "main.py"),
            "startup_command": info.get("startup_command", ""),
            "files": files,
        })
    return projects


def get_project(name: str) -> dict | None:
    name = _safe_project_name(name)
    proj_dir = _project_dir(name)
    if not proj_dir.exists():
        return None
    _reconcile()
    db = _load_db()
    info = db.get(name, {})
    pid = info.get("pid")
    start_time = info.get("start_time")
    uptime = None
    if start_time and info.get("status") == "running":
        uptime = int(time.time() - start_time)
    return {
        "name": name,
        "status": info.get("status", "stopped"),
        "pid": pid,
        "uptime": uptime,
        "restarts": info.get("restarts", 0),
        "start_time": start_time,
        "start_file": info.get("start_file", "main.py"),
        "startup_command": info.get("startup_command", ""),
        "files": list_project_files(name),
    }


def set_start_file(project: str, filename: str) -> dict:
    project = _safe_project_name(project)
    filename = _safe_filename(filename)
    proj_dir = _project_dir(project)
    if not proj_dir.exists():
        return {"ok": False, "error": "Project not found"}
    if not (proj_dir / filename).exists():
        return {"ok": False, "error": f"{filename} not found in project"}
    with _db_lock:
        db = _load_db()
        if project not in db:
            db[project] = dict(_DEFAULT_PROJECT)
        db[project]["start_file"] = filename
        _save_db(db)
    return {"ok": True}


def set_startup_command(project: str, command: str) -> dict:
    project = _safe_project_name(project)
    if not _project_dir(project).exists():
        return {"ok": False, "error": "Project not found"}
    with _db_lock:
        db = _load_db()
        if project not in db:
            db[project] = dict(_DEFAULT_PROJECT)
        db[project]["startup_command"] = command.strip()
        _save_db(db)
    return {"ok": True}


def delete_project(name: str) -> dict:
    name = _safe_project_name(name)
    stop_project(name)
    proj_dir = _project_dir(name)
    log = _log_path(name)
    import shutil
    if proj_dir.exists():
        shutil.rmtree(proj_dir)
    if log.exists():
        log.unlink()
    with _db_lock:
        db = _load_db()
        db.pop(name, None)
        _save_db(db)
    return {"ok": True}


# ─── File Management ──────────────────────────────────────────────────────────

def list_project_files(project: str) -> list[str]:
    proj_dir = _project_dir(_safe_project_name(project))
    if not proj_dir.exists():
        return []
    return sorted(f.name for f in proj_dir.iterdir() if f.is_file())


def create_project_file(project: str, filename: str) -> dict:
    project = _safe_project_name(project)
    filename = _safe_filename(filename)
    if not filename or ".." in filename or "/" in filename or "\\" in filename:
        return {"ok": False, "error": "Invalid filename"}
    proj_dir = _project_dir(project)
    if not proj_dir.exists():
        return {"ok": False, "error": "Project not found"}
    fpath = proj_dir / filename
    if fpath.exists():
        return {"ok": False, "error": "File already exists"}
    fpath.write_text("")
    return {"ok": True, "name": filename}


def upload_file_to_project(project: str, filename: str, content: bytes) -> dict:
    project = _safe_project_name(project)
    filename = _safe_filename(filename)
    if ".." in filename or "/" in filename or "\\" in filename:
        return {"ok": False, "error": "Invalid filename"}
    proj_dir = _project_dir(project)
    if not proj_dir.exists():
        return {"ok": False, "error": "Project not found"}
    with open(proj_dir / filename, "wb") as f:
        f.write(content)
    return {"ok": True, "name": filename}


def delete_project_file(project: str, filename: str) -> dict:
    project = _safe_project_name(project)
    filename = _safe_filename(filename)
    proj_dir = _project_dir(project)
    fpath = proj_dir / filename
    if not fpath.exists():
        return {"ok": False, "error": "File not found"}
    fpath.unlink()
    with _db_lock:
        db = _load_db()
        info = db.get(project, {})
        if info.get("start_file") == filename:
            remaining = list_project_files(project)
            db[project]["start_file"] = remaining[0] if remaining else "main.py"
            _save_db(db)
    return {"ok": True}


def get_file_content(project: str, filename: str) -> str | None:
    project = _safe_project_name(project)
    filename = _safe_filename(filename)
    fpath = _project_dir(project) / filename
    if not fpath.exists():
        return None
    try:
        return fpath.read_text(errors="replace")
    except Exception:
        return None


def save_file_content(project: str, filename: str, content: str) -> dict:
    project = _safe_project_name(project)
    filename = _safe_filename(filename)
    fpath = _project_dir(project) / filename
    if not fpath.parent.exists():
        return {"ok": False, "error": "Project not found"}
    fpath.write_text(content)
    return {"ok": True}


# ─── Package Management ───────────────────────────────────────────────────────

def _pip_cmd() -> list[str]:
    import sys
    return [sys.executable, "-m", "pip"]


def install_package(project: str, package: str) -> dict:
    project = _safe_project_name(project)
    if not _project_dir(project).exists():
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


def uninstall_package(project: str, package: str) -> dict:
    project = _safe_project_name(project)
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


def _watch_project(name: str, proc: subprocess.Popen):
    proc.wait()
    with _db_lock:
        db = _load_db()
        if name not in db:
            return
        info = db[name]
        if info.get("status") != "running":
            return
        restarts = info.get("restarts", 0)
        db[name]["restarts"] = restarts + 1
        db[name]["status"] = "restarting"
        _save_db(db)
    logger.info(f"Project {name} crashed, auto-restarting in 2s…")
    time.sleep(2)
    start_project(name, auto_restart=True)


def start_project(name: str, auto_restart: bool = False) -> dict:
    name = _safe_project_name(name)
    proj_dir = _project_dir(name)
    if not proj_dir.exists():
        return {"ok": False, "error": f"Project '{name}' not found"}

    with _db_lock:
        db = _load_db()
        info = db.get(name, {})

    start_file = info.get("start_file", "main.py")
    entry = proj_dir / start_file

    custom_cmd = info.get("startup_command", "").strip()
    if not custom_cmd and not entry.exists():
        return {"ok": False, "error": f"Start file '{start_file}' not found in project"}

    if not auto_restart:
        pid = info.get("pid")
        if info.get("status") == "running" and pid and _pid_alive(pid):
            return {"ok": False, "error": "Already running"}

    cmd = _build_command(info)
    log_file = open(_log_path(name), "a")
    proc = subprocess.Popen(
        cmd,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        cwd=str(proj_dir),
        start_new_session=True,
    )
    _procs[name] = proc

    with _db_lock:
        db = _load_db()
        existing = db.get(name, {})
        db[name] = {
            **existing,
            "pid": proc.pid,
            "status": "running",
            "start_time": time.time(),
            "restarts": existing.get("restarts", 0) if auto_restart else 0,
        }
        _save_db(db)

    t = threading.Thread(target=_watch_project, args=(name, proc), daemon=True)
    _watchers[name] = t
    t.start()
    return {"ok": True, "pid": proc.pid}


def stop_project(name: str) -> dict:
    name = _safe_project_name(name)
    with _db_lock:
        db = _load_db()
        info = db.get(name, {})
        pid = info.get("pid")
        if not pid or not _pid_alive(pid):
            if name in db:
                db[name] = {**info, "status": "stopped", "pid": None}
                _save_db(db)
            return {"ok": True, "message": "Already stopped"}
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception as e:
                return {"ok": False, "error": str(e)}
        db[name] = {**info, "status": "stopped", "pid": None}
        _save_db(db)
    _procs.pop(name, None)
    return {"ok": True}


def restart_project(name: str) -> dict:
    stop_project(name)
    time.sleep(0.5)
    return start_project(name)


def restart_all() -> list[dict]:
    results = []
    for p in list_projects():
        if p["status"] == "running":
            results.append({p["name"]: restart_project(p["name"])})
    return results


def get_log_lines(name: str, n: int = 20) -> list[str]:
    name = _safe_project_name(name)
    log = _log_path(name)
    if not log.exists():
        return []
    with open(log) as f:
        lines = f.readlines()
    return [l.rstrip() for l in lines[-n:]]


_reconcile()
