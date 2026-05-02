import os
import json
import time
import subprocess
import threading
import logging
import stat
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).parent
LOGS_DIR = BASE_DIR / "logs"
CONFIG_FILE = BASE_DIR / "config.json"
CLOUDFLARED_BIN = BASE_DIR / "cloudflared"
TUNNEL_LOG = LOGS_DIR / "tunnel.log"

logger = logging.getLogger("tunnel")

_proc: subprocess.Popen | None = None
_lock = threading.Lock()
_status = {"status": "stopped", "pid": None, "error": None}
_watcher_thread: threading.Thread | None = None
_stop_requested = False


def _load_token() -> str:
    try:
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
        return cfg.get("cloudflare_token", "")
    except Exception:
        return ""


def _download_cloudflared():
    import platform
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "linux":
        if "arm" in machine or "aarch64" in machine:
            url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64"
        else:
            url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64"
    elif system == "darwin":
        url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-amd64.tgz"
    else:
        raise RuntimeError(f"Unsupported OS: {system}")

    logger.info(f"Downloading cloudflared from {url}")
    urllib.request.urlretrieve(url, str(CLOUDFLARED_BIN))
    CLOUDFLARED_BIN.chmod(CLOUDFLARED_BIN.stat().st_mode | stat.S_IEXEC)
    logger.info("cloudflared downloaded and made executable")


def ensure_cloudflared():
    if not CLOUDFLARED_BIN.exists():
        _download_cloudflared()


def _watch_tunnel(proc: subprocess.Popen, started_at: float):
    global _stop_requested
    proc.wait()
    exit_code = proc.returncode
    runtime = time.time() - started_at

    with _lock:
        if _status.get("pid") != proc.pid:
            return
        _status["pid"] = None

    if _stop_requested:
        with _lock:
            _status["status"] = "stopped"
            _status["error"] = None
        _stop_requested = False
        return

    if runtime < 10:
        msg = f"Tunnel exited after {runtime:.1f}s (exit code {exit_code}) — check your token"
        logger.warning(msg)
        with _lock:
            _status["status"] = "error"
            _status["error"] = msg
        return

    logger.info(f"Tunnel disconnected after {runtime:.0f}s, restarting in 5s…")
    with _lock:
        _status["status"] = "stopped"
        _status["error"] = None
    time.sleep(5)
    if _load_token():
        start_tunnel()


def start_tunnel() -> dict:
    global _proc, _watcher_thread, _stop_requested
    token = _load_token()
    if not token:
        return {"ok": False, "error": "No Cloudflare token configured"}

    with _lock:
        current = _status.get("status")
        if current == "running":
            return {"ok": False, "error": "Tunnel already running"}

    _stop_requested = False

    try:
        ensure_cloudflared()
    except Exception as e:
        return {"ok": False, "error": f"Failed to download cloudflared: {e}"}

    LOGS_DIR.mkdir(exist_ok=True)
    log_file = open(TUNNEL_LOG, "a")
    started_at = time.time()
    proc = subprocess.Popen(
        [str(CLOUDFLARED_BIN), "tunnel", "run", "--token", token],
        stdout=log_file,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    _proc = proc
    with _lock:
        _status["status"] = "running"
        _status["pid"] = proc.pid
        _status["error"] = None

    _watcher_thread = threading.Thread(
        target=_watch_tunnel, args=(proc, started_at), daemon=True
    )
    _watcher_thread.start()
    return {"ok": True, "pid": proc.pid}


def stop_tunnel() -> dict:
    global _proc, _stop_requested
    _stop_requested = True
    with _lock:
        if _status.get("status") not in ("running", "error") or not _proc:
            _status["status"] = "stopped"
            _status["pid"] = None
            _status["error"] = None
            return {"ok": True, "message": "Already stopped"}
        proc = _proc
        _status["status"] = "stopped"
        _status["pid"] = None
        _status["error"] = None
        _proc = None
    try:
        proc.terminate()
    except Exception:
        pass
    return {"ok": True}


def get_status() -> dict:
    with _lock:
        return dict(_status)


def get_tunnel_logs(n: int = 50) -> list[str]:
    if not TUNNEL_LOG.exists():
        return []
    with open(TUNNEL_LOG) as f:
        lines = f.readlines()
    return [l.rstrip() for l in lines[-n:]]
