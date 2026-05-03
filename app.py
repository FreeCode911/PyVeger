import os
import json
import shutil
import subprocess
import asyncio
import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

import psutil
from fastapi import FastAPI, UploadFile, File, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from log_config import setup_logging

import manager
import tunnel
import discord_bot

setup_logging()
logger = logging.getLogger("app")

BASE_DIR = Path(__file__).parent
LOGS_DIR = BASE_DIR / "logs"
CONFIG_FILE = BASE_DIR / "config.json"

_sessions: dict[str, float] = {}
SESSION_TTL = 60 * 60 * 24 * 7
SESSION_COOKIE = "pv3_session"

_PUBLIC = {"/login", "/static"}


def _load_config() -> dict:
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_config(cfg: dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


def _valid_session(token: str | None) -> bool:
    if not token:
        return False
    expiry = _sessions.get(token)
    if expiry is None:
        return False
    if time.time() > expiry:
        _sessions.pop(token, None)
        return False
    return True


def _new_session() -> str:
    token = secrets.token_hex(32)
    _sessions[token] = time.time() + SESSION_TTL
    return token


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path == "/login" or path.startswith("/static"):
            return await call_next(request)
        if request.headers.get("upgrade", "").lower() == "websocket":
            token = request.cookies.get(SESSION_COOKIE)
            if not _valid_session(token):
                return JSONResponse({"detail": "Unauthorized"}, status_code=401)
            return await call_next(request)
        token = request.cookies.get(SESSION_COOKIE)
        if not _valid_session(token):
            return RedirectResponse(url="/login", status_code=302)
        return await call_next(request)


@asynccontextmanager
async def lifespan(app: FastAPI):
    discord_bot.start_bot()
    cfg = _load_config()
    if cfg.get("cloudflare_token"):
        tunnel.start_tunnel()
    asyncio.create_task(_build_runtimes_cache())
    yield


app = FastAPI(title="PyVegar", lifespan=lifespan)
app.add_middleware(AuthMiddleware)

static_dir = BASE_DIR / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# ── Login / Logout ─────────────────────────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    return templates.TemplateResponse(request, "login.html", {"error": error})


@app.post("/login")
async def login_submit(request: Request):
    form = await request.form()
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    cfg = _load_config()
    if username == cfg.get("username", "admin") and password == cfg.get("password", "admin"):
        token = _new_session()
        resp = RedirectResponse(url="/", status_code=303)
        resp.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=SESSION_TTL)
        return resp
    return RedirectResponse(url="/login?error=1", status_code=303)


@app.get("/logout")
async def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        _sessions.pop(token, None)
    resp = RedirectResponse(url="/login", status_code=302)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


# ── Pages ──────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    projects = manager.list_projects()
    tunnel_status = tunnel.get_status()
    bot_status = discord_bot.get_bot_status()
    return templates.TemplateResponse(request, "index.html", {
        "projects": projects,
        "tunnel": tunnel_status,
        "bot": bot_status,
    })


@app.get("/server/{project_id}", response_class=HTMLResponse)
async def server_page(request: Request, project_id: str):
    project = manager.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return templates.TemplateResponse(request, "server.html", {"project": project})


@app.get("/logs/{project_id}", response_class=HTMLResponse)
async def logs_page(request: Request, project_id: str):
    project = manager.get_project(project_id)
    project_name = project["name"] if project else project_id
    return templates.TemplateResponse(request, "logs.html", {
        "project_id": project_id,
        "project_name": project_name,
    })


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    cfg = _load_config()
    return templates.TemplateResponse(request, "settings.html", {
        "cloudflare_token": cfg.get("cloudflare_token", ""),
        "discord_token": cfg.get("discord_token", ""),
        "allowed_users": ", ".join(cfg.get("allowed_users", [])),
        "username": cfg.get("username", "admin"),
        "cf_account_id": cfg.get("cf_account_id", ""),
        "cf_api_token": cfg.get("cf_api_token", ""),
        "tunnel": tunnel.get_status(),
        "saved": request.query_params.get("saved"),
    })


# ── REST API ───────────────────────────────────────────────────────────────────

@app.get("/_/status")
async def api_status():
    cpu = psutil.cpu_percent(interval=0.1)
    mem = psutil.virtual_memory()
    projects = manager.list_projects()
    running = sum(1 for p in projects if p["status"] == "running")
    return {
        "cpu": cpu,
        "ram": mem.percent,
        "active_projects": running,
        "tunnel": tunnel.get_status(),
        "bot": discord_bot.get_bot_status(),
    }


@app.get("/_/projects")
async def api_list_projects():
    return manager.list_projects()


@app.get("/_/projects/{project_id}")
async def api_get_project(project_id: str):
    proj = manager.get_project(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    return proj


_RUNTIMES_CACHE: list | None = None
_RUNTIMES_CACHE_FILE = BASE_DIR / "runtimes_cache.json"

_RUNTIME_DEFS = [
    {"name": "Python",  "value": "python",  "bin": "python3", "start_file": "main.py",   "command": "python3 -u {start_file}"},
    {"name": "Node.js", "value": "nodejs",  "bin": "node",    "start_file": "index.js",  "command": "node {start_file}"},
    {"name": "Go",      "value": "go",       "bin": "go",      "start_file": "main.go",   "command": "go run {start_file}"},
    {"name": "Ruby",    "value": "ruby",     "bin": "ruby",    "start_file": "main.rb",   "command": "ruby {start_file}"},
    {"name": "PHP",     "value": "php",      "bin": "php",     "start_file": "index.php", "command": "php {start_file}"},
    {"name": "Deno",    "value": "deno",     "bin": "deno",    "start_file": "main.ts",   "command": "deno run --allow-all {start_file}"},
    {"name": "Bun",     "value": "bun",      "bin": "bun",     "start_file": "index.ts",  "command": "bun {start_file}"},
]

def _load_runtimes_from_disk() -> list | None:
    try:
        if _RUNTIMES_CACHE_FILE.exists():
            return json.loads(_RUNTIMES_CACHE_FILE.read_text())
    except Exception:
        pass
    return None

def _save_runtimes_to_disk(data: list) -> None:
    try:
        _RUNTIMES_CACHE_FILE.write_text(json.dumps(data))
    except Exception:
        pass

async def _probe_runtime(rt: dict) -> dict | None:
    if not shutil.which(rt["bin"]):
        return None
    try:
        r = await asyncio.wait_for(
            asyncio.to_thread(subprocess.run, [rt["bin"], "--version"],
                              capture_output=True, text=True),
            timeout=2.0
        )
        version = (r.stdout.strip() or r.stderr.strip()).split("\n")[0]
    except Exception:
        version = ""
    return {"name": rt["name"], "value": rt["value"], "version": version,
            "start_file": rt["start_file"], "command": rt["command"]}

async def _build_runtimes_cache() -> list:
    global _RUNTIMES_CACHE
    results = await asyncio.gather(*[_probe_runtime(rt) for rt in _RUNTIME_DEFS])
    _RUNTIMES_CACHE = [r for r in results if r is not None]
    _RUNTIMES_CACHE.append({"name": "Other", "value": "other", "version": "",
                             "start_file": "main.py", "command": ""})
    _save_runtimes_to_disk(_RUNTIMES_CACHE)
    return _RUNTIMES_CACHE

@app.get("/_/runtimes")
async def api_runtimes():
    global _RUNTIMES_CACHE
    if _RUNTIMES_CACHE is not None:
        return {"runtimes": _RUNTIMES_CACHE}
    disk = _load_runtimes_from_disk()
    if disk is not None:
        _RUNTIMES_CACHE = disk
        return {"runtimes": _RUNTIMES_CACHE}
    return {"runtimes": await _build_runtimes_cache()}

@app.post("/_/runtimes/refresh")
async def api_runtimes_refresh():
    global _RUNTIMES_CACHE
    _RUNTIMES_CACHE = None
    return {"runtimes": await _build_runtimes_cache()}


@app.post("/_/projects/create")
async def api_create_project(request: Request):
    body = await request.json()
    name = body.get("name", "").strip()
    startup_command = body.get("startup_command", "").strip()
    start_file = body.get("start_file", "").strip()
    language = body.get("language", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    result = manager.create_project(name, startup_command=startup_command, start_file=start_file, language=language)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.delete("/_/projects/{project_id}")
async def api_delete_project(project_id: str):
    result = manager.delete_project(project_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.post("/_/projects/{project_id}/start")
async def api_start(project_id: str):
    result = manager.start_project(project_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.post("/_/projects/{project_id}/stop")
async def api_stop(project_id: str):
    result = manager.stop_project(project_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.post("/_/projects/{project_id}/restart")
async def api_restart(project_id: str):
    result = manager.restart_project(project_id)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.post("/_/restart-all")
async def api_restart_all():
    return manager.restart_all()


@app.post("/_/projects/{project_id}/start-file")
async def api_set_start_file(project_id: str, request: Request):
    body = await request.json()
    filename = body.get("filename", "").strip()
    if not filename:
        raise HTTPException(status_code=400, detail="filename is required")
    result = manager.set_start_file(project_id, filename)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


# ── File & Folder API ──────────────────────────────────────────────────────────

@app.get("/_/projects/{project_id}/files")
async def api_list_files(project_id: str):
    proj = manager.get_project(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    return {"files": proj["files"], "start_file": proj["start_file"]}


@app.get("/_/projects/{project_id}/tree")
async def api_file_tree(project_id: str):
    proj = manager.get_project(project_id)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    tree = manager.list_project_tree(project_id)
    return {"tree": tree, "start_file": proj["start_file"]}


@app.post("/_/projects/{project_id}/folders")
async def api_create_folder(project_id: str, request: Request):
    body = await request.json()
    path = body.get("path", "").strip()
    if not path:
        raise HTTPException(status_code=400, detail="path is required")
    result = manager.create_project_folder(project_id, path)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.delete("/_/projects/{project_id}/items/{itempath:path}")
async def api_delete_item(project_id: str, itempath: str):
    result = manager.delete_project_item(project_id, itempath)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


# NOTE: /files/new must be BEFORE /files/{filepath:path}
@app.post("/_/projects/{project_id}/files/new")
async def api_create_file(project_id: str, request: Request):
    body = await request.json()
    filename = body.get("filename", "").strip()
    if not filename:
        raise HTTPException(status_code=400, detail="filename is required")
    result = manager.create_project_file(project_id, filename)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.get("/_/projects/{project_id}/files/{filepath:path}")
async def api_get_file(project_id: str, filepath: str):
    content = manager.get_file_content(project_id, filepath)
    if content is None:
        raise HTTPException(status_code=404, detail="File not found")
    return {"content": content}


@app.post("/_/projects/{project_id}/files/{filepath:path}")
async def api_save_file(project_id: str, filepath: str, request: Request):
    body = await request.json()
    content = body.get("content", "")
    result = manager.save_file_content(project_id, filepath, content)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.post("/_/projects/{project_id}/move")
async def api_move_item(project_id: str, request: Request):
    body = await request.json()
    src = body.get("src", "").strip()
    dest = body.get("dest", "").strip()
    if not src or not dest:
        raise HTTPException(status_code=400, detail="src and dest are required")
    result = manager.move_project_item(project_id, src, dest)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.post("/_/projects/{project_id}/upload")
async def api_upload_file(project_id: str, file: UploadFile = File(...)):
    filename = os.path.basename(file.filename or "")
    if not filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    content = await file.read()
    result = manager.upload_file_to_project(project_id, filename, content)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.delete("/_/projects/{project_id}/files/{filepath:path}")
async def api_delete_file(project_id: str, filepath: str):
    result = manager.delete_project_file(project_id, filepath)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.post("/_/projects/{project_id}/command")
async def api_set_command(project_id: str, request: Request):
    body = await request.json()
    command = body.get("command", "")
    result = manager.set_startup_command(project_id, command)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.post("/_/projects/{project_id}/packages/install")
async def api_install_package(project_id: str, request: Request):
    body = await request.json()
    package = body.get("package", "").strip()
    if not package:
        raise HTTPException(status_code=400, detail="package is required")
    result = manager.install_package(project_id, package)
    return result


@app.delete("/_/projects/{project_id}/packages/{package}")
async def api_uninstall_package(project_id: str, package: str):
    result = manager.uninstall_package(project_id, package)
    return result


# ── Tunnel API ────────────────────────────────────────────────────────────────

@app.post("/_/tunnel/start")
async def api_tunnel_start():
    result = tunnel.start_tunnel()
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.post("/_/tunnel/stop")
async def api_tunnel_stop():
    return tunnel.stop_tunnel()


@app.post("/_/tunnel/clear-log")
async def api_tunnel_clear_log():
    tlog = BASE_DIR / "logs" / "tunnel.log"
    try:
        tlog.write_text("")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True}


@app.post("/_/projects/{project_id}/clear-log")
async def api_clear_project_log(project_id: str):
    result = manager.clear_log(project_id)
    if not result["ok"]:
        raise HTTPException(status_code=500, detail=result.get("error", "Failed"))
    return {"ok": True}




# ── Settings ──────────────────────────────────────────────────────────────────

@app.post("/_/settings")
async def api_save_settings(request: Request):
    is_json = "application/json" in request.headers.get("content-type", "")
    if is_json:
        data = await request.json()
        cf_token      = str(data.get("cloudflare_token", "")).strip()
        dc_token      = str(data.get("discord_token", "")).strip()
        allowed_raw   = str(data.get("allowed_users", "")).strip()
        new_user      = str(data.get("username", "")).strip()
        new_pass      = str(data.get("password", "")).strip()
        cf_account_id = str(data.get("cf_account_id", "")).strip()
        cf_api_token  = str(data.get("cf_api_token", "")).strip()
    else:
        form = await request.form()
        cf_token      = str(form.get("cloudflare_token", "")).strip()
        dc_token      = str(form.get("discord_token", "")).strip()
        allowed_raw   = str(form.get("allowed_users", "")).strip()
        new_user      = str(form.get("username", "")).strip()
        new_pass      = str(form.get("password", "")).strip()
        cf_account_id = str(form.get("cf_account_id", "")).strip()
        cf_api_token  = str(form.get("cf_api_token", "")).strip()
    cfg = _load_config()
    old_dc_token = cfg.get("discord_token", "")
    old_cf_token = cfg.get("cloudflare_token", "")
    cfg["cloudflare_token"] = cf_token
    cfg["discord_token"] = dc_token
    cfg["allowed_users"] = [u.strip() for u in allowed_raw.split(",") if u.strip()]
    cfg["cf_account_id"] = cf_account_id
    cfg["cf_api_token"] = cf_api_token
    if new_user:
        cfg["username"] = new_user
    if new_pass:
        cfg["password"] = new_pass
    _save_config(cfg)
    current_tunnel = tunnel.get_status()["status"]
    if cf_token:
        if current_tunnel == "running" and cf_token != old_cf_token:
            tunnel.stop_tunnel()
            time.sleep(0.5)
            tunnel.start_tunnel()
        elif current_tunnel != "running":
            tunnel.start_tunnel()
    else:
        tunnel.stop_tunnel()
    if dc_token and dc_token != old_dc_token:
        discord_bot.start_bot()
    if is_json:
        return {"ok": True}
    return RedirectResponse(url="/settings?saved=1", status_code=303)


# ── WebSocket: Live Logs ───────────────────────────────────────────────────────

@app.websocket("/ws/logs/{project_id}")
async def ws_logs(websocket: WebSocket, project_id: str):
    token = websocket.cookies.get(SESSION_COOKIE)
    if not _valid_session(token):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    log_path = LOGS_DIR / f"{project_id}.log"
    try:
        offset = 0
        if log_path.exists():
            with open(log_path) as f:
                initial = f.read()
            if initial:
                await websocket.send_text(initial)
            offset = log_path.stat().st_size
        while True:
            await asyncio.sleep(0.5)
            if not log_path.exists():
                continue
            size = log_path.stat().st_size
            if size < offset:
                # File was truncated (cleared) — signal client then reset
                await websocket.send_text("\x00CLEAR\x00")
                offset = 0
            if size > offset:
                with open(log_path) as f:
                    f.seek(offset)
                    new_data = f.read()
                offset = size
                if new_data:
                    await websocket.send_text(new_data)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning(f"ws_logs error: {e}")


# ── WebSocket: Live Stats ──────────────────────────────────────────────────────

@app.websocket("/ws/stats")
async def ws_stats(websocket: WebSocket):
    token = websocket.cookies.get(SESSION_COOKIE)
    if not _valid_session(token):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    try:
        while True:
            cpu = psutil.cpu_percent(interval=None)
            mem = psutil.virtual_memory()
            projects = manager.list_projects()
            running = sum(1 for p in projects if p["status"] == "running")
            await websocket.send_text(json.dumps({
                "cpu": cpu,
                "ram": mem.percent,
                "active": running,
                "tunnel": tunnel.get_status().get("status", "stopped"),
                "projects": [
                    {
                        "name": p["name"],
                        "status": p["status"],
                        "pid": p["pid"],
                        "uptime": p.get("uptime"),
                        "restarts": p.get("restarts", 0),
                        "language": p.get("language", ""),
                        "startup_command": p.get("startup_command", ""),
                        "start_file": p.get("start_file", "main.py"),
                        "id": p["id"],
                    }
                    for p in projects
                ],
            }))
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning(f"ws_stats error: {e}")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False, log_config=None)
