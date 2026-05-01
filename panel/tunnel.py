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
_status = {"status": "stopped", "pid": None}
_watcher_thread: threading.Thread | None = None


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


def _watch_tunnel(proc: subprocess.Popen):
    proc.wait()
    with _lock:
        _status["status"] = "stopped"
        _status["pid"] = None
    token = _load_token()
    if token:
        logger.info("Tunnel crashed, restarting in 5s...")
        time.sleep(5)
        start_tunnel()


def start_tunnel() -> dict:
    global _proc, _watcher_thread
    token = _load_token()
    if not token:
        return {"ok": False, "error": "No Cloudflare token configured"}

    with _lock:
        if _status.get("status") == "running":
            return {"ok": False, "error": "Tunnel already running"}

    try:
        ensure_cloudflared()
    except Exception as e:
        return {"ok": False, "error": f"Failed to download cloudflared: {e}"}

    log_file = open(TUNNEL_LOG, "a")
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

    _watcher_thread = threading.Thread(target=_watch_tunnel, args=(proc,), daemon=True)
    _watcher_thread.start()
    return {"ok": True, "pid": proc.pid}


def stop_tunnel() -> dict:
    global _proc
    with _lock:
        if _status.get("status") != "running" or not _proc:
            _status["status"] = "stopped"
            _status["pid"] = None
            return {"ok": True, "message": "Already stopped"}
        try:
            _proc.terminate()
        except Exception as e:
            return {"ok": False, "error": str(e)}
        _status["status"] = "stopped"
        _status["pid"] = None
        _proc = None
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
