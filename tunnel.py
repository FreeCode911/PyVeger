import os
import re
import json
import time
import base64
import subprocess
import threading
import logging
import stat
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path

BASE_DIR = Path(__file__).parent
LOGS_DIR = BASE_DIR / "logs"
CONFIG_FILE = BASE_DIR / "config.json"
CLOUDFLARED_BIN = BASE_DIR / "cloudflared"
TUNNEL_LOG = LOGS_DIR / "tunnel.log"
QUICK_LOG = LOGS_DIR / "tunnel_quick.log"

logger = logging.getLogger("tunnel")

# ── Named tunnel (token-based) ────────────────────────────────────────────────
_proc: subprocess.Popen | None = None
_lock = threading.Lock()
_status = {"status": "stopped", "pid": None, "error": None}
_watcher_thread: threading.Thread | None = None
_stop_requested = False

# ── Quick tunnel (trycloudflare.com) ──────────────────────────────────────────
_quick_proc: subprocess.Popen | None = None
_quick_lock = threading.Lock()
_quick_status: dict = {"status": "stopped", "url": None, "error": None, "pid": None}

# ── CF Account tunnels (API-managed) ─────────────────────────────────────────
_cf_procs: dict = {}   # tunnel_id -> {"proc", "status", "pid"}
_cf_lock = threading.Lock()


def _load_config() -> dict:
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_config(cfg: dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


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


# ── Cloudflare REST API helper ────────────────────────────────────────────────

def _cf_api(method: str, path: str, token: str, data: dict | None = None) -> dict:
    """Make a Cloudflare API request."""
    url = f"https://api.cloudflare.com/client/v4{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"success": False, "errors": [{"message": str(e)}]}
    except Exception as e:
        return {"success": False, "errors": [{"message": str(e)}]}


def _cf_err(result: dict) -> str:
    errors = result.get("errors") or []
    if errors:
        return errors[0].get("message", "Cloudflare API error")
    return "Cloudflare API error"


# ── CF Account: list zones ────────────────────────────────────────────────────

def list_cf_zones(account_id: str, api_token: str) -> dict:
    result = _cf_api("GET", f"/zones?account.id={account_id}&per_page=50&status=active", api_token)
    if not result.get("success"):
        err = _cf_err(result)
        if "10000" in str(result.get("errors", "")) or "Authentication" in err or "Invalid" in err:
            return {"ok": False, "error": "Invalid API token — check your Account ID and token are correct"}
        if "403" in err or "permission" in err.lower() or "not allowed" in err.lower():
            return {"ok": False, "error": "Token missing Zone:Read permission — add it in your CF token settings"}
        return {"ok": False, "error": err}
    zones = [{"id": z["id"], "name": z["name"]} for z in result.get("result", [])]
    if not zones:
        return {"ok": False, "error": "No domains found — make sure your token has Zone:Read permission and your account has active domains"}
    return {"ok": True, "zones": zones}


# ── CF Account: managed tunnels ───────────────────────────────────────────────

def get_cf_tunnels_status() -> list:
    """Return local CF tunnel configs merged with live running status."""
    cfg = _load_config()
    out = []
    for t in cfg.get("cf_tunnels", []):
        tid = t["id"]
        with _cf_lock:
            proc_info = dict(_cf_procs.get(tid, {}))
        out.append({
            **t,
            "running_status": proc_info.get("status", "stopped"),
            "pid": proc_info.get("pid"),
        })
    return out


def create_cf_tunnel(
    account_id: str,
    api_token: str,
    name: str,
    port: int,
    subdomain: str,
    zone_id: str,
    zone_name: str,
) -> dict:
    """
    Full tunnel creation flow:
    1. Create named tunnel via CF API
    2. Configure ingress rules via CF API
    3. Get tunnel run token
    4. Create CNAME DNS record
    5. Save to local config
    """
    import secrets as _sec
    tunnel_secret = base64.b64encode(_sec.token_bytes(32)).decode()

    # 1. Create tunnel
    r = _cf_api("POST", f"/accounts/{account_id}/cfd_tunnel", api_token, {
        "name": name,
        "tunnel_secret": tunnel_secret,
        "config_src": "cloudflare",
    })
    if not r.get("success"):
        return {"ok": False, "error": _cf_err(r)}

    tunnel_data = r["result"]
    tunnel_id = tunnel_data["id"]
    hostname = f"{subdomain}.{zone_name}" if subdomain else zone_name
    logger.info(f"Created CF tunnel {tunnel_id} ({name})")

    # 2. Configure ingress
    cfg_r = _cf_api(
        "PUT",
        f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/configurations",
        api_token,
        {
            "config": {
                "ingress": [
                    {"hostname": hostname, "service": f"http://localhost:{port}"},
                    {"service": "http_status:404"},
                ]
            }
        },
    )
    if not cfg_r.get("success"):
        logger.warning(f"Tunnel ingress config failed: {_cf_err(cfg_r)}")

    # 3. Get tunnel token
    tok_r = _cf_api("GET", f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}/token", api_token)
    if not tok_r.get("success"):
        # Cleanup
        _cf_api("DELETE", f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}?cascade=true", api_token)
        return {"ok": False, "error": f"Failed to get token: {_cf_err(tok_r)}"}
    tunnel_token = tok_r["result"]

    # 4. Create DNS CNAME record
    cname_name    = hostname
    cname_target  = f"{tunnel_id}.cfargotunnel.com"
    dns_r = _cf_api("POST", f"/zones/{zone_id}/dns_records", api_token, {
        "type": "CNAME",
        "name": cname_name,
        "content": cname_target,
        "proxied": True,
        "ttl": 1,
    })
    dns_record_id  = None
    dns_auto       = False
    dns_error      = None
    if dns_r.get("success"):
        dns_record_id = dns_r["result"]["id"]
        dns_auto      = True
        logger.info(f"Created DNS record {dns_record_id} for {hostname}")
    else:
        dns_error = _cf_err(dns_r)
        logger.warning(f"DNS record creation failed: {dns_error}")

    # 5. Save to config
    cfg = _load_config()
    entry = {
        "id": tunnel_id,
        "name": name,
        "token": tunnel_token,
        "subdomain": subdomain,
        "zone_id": zone_id,
        "zone_name": zone_name,
        "hostname": hostname,
        "port": port,
        "dns_record_id": dns_record_id,
    }
    cfg.setdefault("cf_tunnels", []).append(entry)
    _save_config(cfg)

    return {
        "ok": True,
        "tunnel": entry,
        "dns_auto": dns_auto,
        "dns_manual": not dns_auto,
        "cname_name": cname_name,
        "cname_target": cname_target,
        "dns_error": dns_error,
    }


def delete_cf_tunnel(account_id: str, api_token: str, tunnel_id: str) -> dict:
    """Stop process, delete DNS record, delete from CF API, remove from local config."""
    cfg = _load_config()
    tunnels = cfg.get("cf_tunnels", [])
    entry = next((t for t in tunnels if t["id"] == tunnel_id), None)

    # Stop running process first
    stop_cf_tunnel_process(tunnel_id)

    # Delete DNS record
    if entry and entry.get("dns_record_id") and entry.get("zone_id"):
        _cf_api("DELETE", f"/zones/{entry['zone_id']}/dns_records/{entry['dns_record_id']}", api_token)
        logger.info(f"Deleted DNS record {entry['dns_record_id']}")

    # Delete tunnel from CF (cascade deletes connections)
    r = _cf_api("DELETE", f"/accounts/{account_id}/cfd_tunnel/{tunnel_id}?cascade=true", api_token)
    if not r.get("success"):
        logger.warning(f"CF tunnel delete: {_cf_err(r)}")

    # Remove from local config
    cfg["cf_tunnels"] = [t for t in tunnels if t["id"] != tunnel_id]
    _save_config(cfg)
    return {"ok": True}


def start_cf_tunnel_process(tunnel_id: str) -> dict:
    """Start cloudflared for a named CF tunnel using its stored token."""
    cfg = _load_config()
    entry = next((t for t in cfg.get("cf_tunnels", []) if t["id"] == tunnel_id), None)
    if not entry:
        return {"ok": False, "error": "Tunnel not found in local config"}

    with _cf_lock:
        existing = _cf_procs.get(tunnel_id, {})
        if existing.get("status") == "running":
            return {"ok": False, "error": "Tunnel already running"}

    try:
        ensure_cloudflared()
    except Exception as e:
        return {"ok": False, "error": f"Failed to get cloudflared: {e}"}

    LOGS_DIR.mkdir(exist_ok=True)
    log_path = LOGS_DIR / f"cf_{tunnel_id[:8]}.log"
    log_file = open(log_path, "a")
    try:
        proc = subprocess.Popen(
            [str(CLOUDFLARED_BIN), "tunnel", "run", "--token", entry["token"]],
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except Exception as e:
        log_file.close()
        return {"ok": False, "error": str(e)}

    with _cf_lock:
        _cf_procs[tunnel_id] = {"proc": proc, "status": "running", "pid": proc.pid}

    def _watch():
        proc.wait()
        with _cf_lock:
            if _cf_procs.get(tunnel_id, {}).get("pid") == proc.pid:
                _cf_procs[tunnel_id] = {"status": "stopped", "pid": None, "proc": None}
        log_file.close()

    threading.Thread(target=_watch, daemon=True).start()
    logger.info(f"Started CF tunnel {tunnel_id[:8]} (pid {proc.pid})")
    return {"ok": True, "pid": proc.pid}


def stop_cf_tunnel_process(tunnel_id: str) -> dict:
    """Stop the cloudflared process for a named CF tunnel."""
    with _cf_lock:
        info = _cf_procs.get(tunnel_id)
        if not info or info.get("status") != "running":
            _cf_procs[tunnel_id] = {"status": "stopped", "pid": None, "proc": None}
            return {"ok": True, "message": "Not running"}
        proc = info.get("proc")
        _cf_procs[tunnel_id] = {"status": "stopped", "pid": None, "proc": None}
    if proc:
        try:
            proc.terminate()
        except Exception:
            pass
    return {"ok": True}


# ── Named tunnel (run-token) ───────────────────────────────────────────────────

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
        msg = f"Tunnel exited after {runtime:.1f}s (exit code {exit_code}) — invalid token or auth error, not restarting"
        logger.warning(msg)
        try:
            with open(TUNNEL_LOG, "a") as f:
                f.write(f"\n[PyVegar] {msg}\n")
        except Exception:
            pass
        with _lock:
            _status["status"] = "stopped"
            _status["error"] = msg
        return
    logger.info(f"Tunnel disconnected after {runtime:.0f}s, restarting in 5s…")
    with _lock:
        _status["status"] = "stopped"
        _status["error"] = None
    time.sleep(5)
    if _load_config().get("cloudflare_token"):
        start_tunnel()


def start_tunnel() -> dict:
    global _proc, _watcher_thread, _stop_requested
    token = _load_config().get("cloudflare_token", "")
    if not token:
        return {"ok": False, "error": "No Cloudflare token configured"}
    with _lock:
        if _status.get("status") == "running":
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
    return [ln.rstrip() for ln in lines[-n:]]


# ── Quick tunnel (no token needed) ────────────────────────────────────────────

def _watch_quick_tunnel(proc: subprocess.Popen):
    found_url = False
    LOGS_DIR.mkdir(exist_ok=True)
    try:
        with open(QUICK_LOG, "a") as log:
            for line in proc.stdout:
                log.write(line)
                log.flush()
                if not found_url:
                    m = re.search(r'https://[a-z0-9\-]+\.trycloudflare\.com', line)
                    if m:
                        url = m.group(0)
                        found_url = True
                        with _quick_lock:
                            _quick_status.update({"status": "running", "url": url})
                        logger.info(f"Quick tunnel URL: {url}")
    except Exception as e:
        logger.warning(f"Quick tunnel watcher error: {e}")
    proc.wait()
    with _quick_lock:
        if _quick_status.get("status") != "stopped":
            _quick_status.update({"status": "stopped", "url": None, "pid": None})


def start_quick_tunnel(port: int = 8000) -> dict:
    global _quick_proc
    with _quick_lock:
        if _quick_status.get("status") in ("running", "starting"):
            return {"ok": False, "error": "Quick tunnel already running"}
    try:
        ensure_cloudflared()
    except Exception as e:
        return {"ok": False, "error": f"Failed to get cloudflared: {e}"}
    LOGS_DIR.mkdir(exist_ok=True)
    try:
        proc = subprocess.Popen(
            [str(CLOUDFLARED_BIN), "tunnel", "--url", f"http://localhost:{port}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            text=True,
            bufsize=1,
        )
        _quick_proc = proc
        with _quick_lock:
            _quick_status.update({"status": "starting", "url": None, "error": None, "pid": proc.pid})
        threading.Thread(target=_watch_quick_tunnel, args=(proc,), daemon=True).start()
        return {"ok": True, "pid": proc.pid}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def stop_quick_tunnel() -> dict:
    global _quick_proc
    with _quick_lock:
        proc = _quick_proc
        _quick_proc = None
        _quick_status.update({"status": "stopped", "url": None, "error": None, "pid": None})
    if proc:
        try:
            proc.terminate()
        except Exception:
            pass
    return {"ok": True}


def get_quick_status() -> dict:
    with _quick_lock:
        return dict(_quick_status)
