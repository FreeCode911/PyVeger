import os
import json
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
_watchers: dict[str, threading.Thread] = {}
_procs: dict[str, subprocess.Popen] = {}


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


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


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


def _watch_script(name: str, proc: subprocess.Popen, log_file_path: Path):
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

    logger.info(f"Script {name} crashed, auto-restarting...")
    time.sleep(2)
    start_script(name, auto_restart=True)


def list_scripts() -> list[dict]:
    _reconcile()
    db = _load_db()
    scripts = []
    for f in SCRIPTS_DIR.iterdir():
        if f.suffix == ".py":
            info = db.get(f.name, {})
            pid = info.get("pid")
            start_time = info.get("start_time")
            uptime = None
            if start_time and info.get("status") == "running":
                uptime = int(time.time() - start_time)
            scripts.append({
                "name": f.name,
                "status": info.get("status", "stopped"),
                "pid": pid,
                "uptime": uptime,
                "restarts": info.get("restarts", 0),
                "start_time": start_time,
            })
    return scripts


def start_script(name: str, auto_restart: bool = False) -> dict:
    safe = _safe_name(name)
    script_path = SCRIPTS_DIR / safe
    if not script_path.exists():
        return {"ok": False, "error": f"Script {safe} not found"}
    log_path = LOGS_DIR / f"{safe}.log"

    with _db_lock:
        db = _load_db()
        info = db.get(safe, {})
        if not auto_restart and info.get("status") == "running":
            pid = info.get("pid")
            if pid and _pid_alive(pid):
                return {"ok": False, "error": "Already running"}

    log_file = open(log_path, "a")
    proc = subprocess.Popen(
        ["python3", str(script_path)],
        stdout=log_file,
        stderr=subprocess.STDOUT,
        cwd=str(SCRIPTS_DIR),
        start_new_session=True,
    )
    _procs[safe] = proc

    with _db_lock:
        db = _load_db()
        existing = db.get(safe, {})
        db[safe] = {
            "pid": proc.pid,
            "status": "running",
            "start_time": time.time(),
            "restarts": existing.get("restarts", 0) if auto_restart else 0,
        }
        _save_db(db)

    t = threading.Thread(target=_watch_script, args=(safe, proc, log_path), daemon=True)
    _watchers[safe] = t
    t.start()

    return {"ok": True, "pid": proc.pid}


def stop_script(name: str) -> dict:
    safe = _safe_name(name)
    with _db_lock:
        db = _load_db()
        info = db.get(safe, {})
        pid = info.get("pid")
        if not pid or not _pid_alive(pid):
            db[safe] = {**info, "status": "stopped", "pid": None}
            _save_db(db)
            return {"ok": True, "message": "Already stopped"}
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except Exception:
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception as e:
                return {"ok": False, "error": str(e)}
        db[safe] = {**info, "status": "stopped", "pid": None}
        _save_db(db)
    if safe in _procs:
        del _procs[safe]
    return {"ok": True}


def restart_script(name: str) -> dict:
    stop_script(name)
    time.sleep(0.5)
    return start_script(name)


def get_script_status(name: str) -> dict:
    safe = _safe_name(name)
    _reconcile()
    db = _load_db()
    info = db.get(safe, {})
    pid = info.get("pid")
    start_time = info.get("start_time")
    uptime = None
    if start_time and info.get("status") == "running":
        uptime = int(time.time() - start_time)
    return {
        "name": safe,
        "status": info.get("status", "stopped"),
        "pid": pid,
        "uptime": uptime,
        "restarts": info.get("restarts", 0),
    }


def get_log_lines(name: str, n: int = 20) -> list[str]:
    safe = _safe_name(name)
    log_path = LOGS_DIR / f"{safe}.log"
    if not log_path.exists():
        return []
    with open(log_path) as f:
        lines = f.readlines()
    return [l.rstrip() for l in lines[-n:]]


def delete_script(name: str) -> dict:
    safe = _safe_name(name)
    stop_script(safe)
    script_path = SCRIPTS_DIR / safe
    log_path = LOGS_DIR / f"{safe}.log"
    if script_path.exists():
        script_path.unlink()
    if log_path.exists():
        log_path.unlink()
    with _db_lock:
        db = _load_db()
        db.pop(safe, None)
        _save_db(db)
    return {"ok": True}


def restart_all() -> list[dict]:
    results = []
    for s in list_scripts():
        if s["status"] == "running":
            results.append({s["name"]: restart_script(s["name"])})
    return results


def _safe_name(name: str) -> str:
    name = os.path.basename(name)
    if not name.endswith(".py"):
        name = name + ".py"
    return name


_reconcile()
