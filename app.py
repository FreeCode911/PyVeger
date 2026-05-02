import os
import json
import asyncio
import logging
import secrets
import hashlib
import time
from contextlib import asynccontextmanager
from pathlib import Path

import psutil
from fastapi import FastAPI, UploadFile, File, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

import manager
import tunnel
import discord_bot

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("app")

BASE_DIR = Path(__file__).parent
SCRIPTS_DIR = BASE_DIR / "scripts"
LOGS_DIR = BASE_DIR / "logs"
CONFIG_FILE = BASE_DIR / "config.json"

# Active sessions: token -> expiry timestamp
_sessions: dict[str, float] = {}
SESSION_TTL = 60 * 60 * 24 * 7   # 7 days
SESSION_COOKIE = "pv3_session"

# Routes that do NOT require auth
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


def _hash(pw: str) -> str:
    return hashlib.sha256(pw.encode()).hexdigest()


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


# ── Auth middleware ────────────────────────────────────────────────────────────
class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        # Allow public paths
        if path == "/login" or path.startswith("/static"):
            return await call_next(request)
        # Allow WebSocket upgrades (they carry session via cookie too but handled separately)
        if request.headers.get("upgrade", "").lower() == "websocket":
            token = request.cookies.get(SESSION_COOKIE)
            if not _valid_session(token):
                return JSONResponse({"detail": "Unauthorized"}, status_code=401)
            return await call_next(request)
        # Check session cookie
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
    yield


app = FastAPI(title="Python Web Panel V3", lifespan=lifespan)
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


@app.get("/server/{name}", response_class=HTMLResponse)
async def server_page(request: Request, name: str):
    project = manager.get_project(name)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return templates.TemplateResponse(request, "server.html", {"project": project})


@app.get("/logs/{project_name}", response_class=HTMLResponse)
async def logs_page(request: Request, project_name: str):
    return templates.TemplateResponse(request, "logs.html", {
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
        "saved": request.query_params.get("saved"),
    })


# ── REST API  (prefix /_/ avoids conflict with /api Express server) ─────────

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


@app.get("/_/projects/{name}")
async def api_get_project(name: str):
    proj = manager.get_project(name)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    return proj


@app.post("/_/projects/create")
async def api_create_project(request: Request):
    body = await request.json()
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name is required")
    result = manager.create_project(name)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.delete("/_/projects/{name}")
async def api_delete_project(name: str):
    result = manager.delete_project(name)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.post("/_/projects/{name}/start")
async def api_start(name: str):
    result = manager.start_project(name)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.post("/_/projects/{name}/stop")
async def api_stop(name: str):
    result = manager.stop_project(name)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.post("/_/projects/{name}/restart")
async def api_restart(name: str):
    result = manager.restart_project(name)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.post("/_/restart-all")
async def api_restart_all():
    return manager.restart_all()


@app.post("/_/projects/{name}/start-file")
async def api_set_start_file(name: str, request: Request):
    body = await request.json()
    filename = body.get("filename", "").strip()
    if not filename:
        raise HTTPException(status_code=400, detail="filename is required")
    result = manager.set_start_file(name, filename)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.get("/_/projects/{name}/files")
async def api_list_files(name: str):
    proj = manager.get_project(name)
    if not proj:
        raise HTTPException(status_code=404, detail="Project not found")
    return {"files": proj["files"], "start_file": proj["start_file"]}


@app.post("/_/projects/{name}/files/new")
async def api_create_file(name: str, request: Request):
    body = await request.json()
    filename = body.get("filename", "").strip()
    if not filename:
        raise HTTPException(status_code=400, detail="filename is required")
    result = manager.create_project_file(name, filename)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.get("/_/projects/{name}/files/{filename}")
async def api_get_file(name: str, filename: str):
    content = manager.get_file_content(name, filename)
    if content is None:
        raise HTTPException(status_code=404, detail="File not found")
    return {"content": content}


@app.post("/_/projects/{name}/files/{filename}")
async def api_save_file(name: str, filename: str, request: Request):
    body = await request.json()
    content = body.get("content", "")
    result = manager.save_file_content(name, filename, content)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.post("/_/projects/{name}/upload")
async def api_upload_file(name: str, file: UploadFile = File(...)):
    filename = os.path.basename(file.filename or "")
    if not filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    content = await file.read()
    result = manager.upload_file_to_project(name, filename, content)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.delete("/_/projects/{name}/files/{filename}")
async def api_delete_file(name: str, filename: str):
    result = manager.delete_project_file(name, filename)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.post("/_/projects/{name}/command")
async def api_set_command(name: str, request: Request):
    body = await request.json()
    command = body.get("command", "")
    result = manager.set_startup_command(name, command)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.post("/_/projects/{name}/packages/install")
async def api_install_package(name: str, request: Request):
    body = await request.json()
    package = body.get("package", "").strip()
    if not package:
        raise HTTPException(status_code=400, detail="package is required")
    result = manager.install_package(name, package)
    return result


@app.delete("/_/projects/{name}/packages/{package}")
async def api_uninstall_package(name: str, package: str):
    result = manager.uninstall_package(name, package)
    return result


@app.post("/_/tunnel/start")
async def api_tunnel_start():
    result = tunnel.start_tunnel()
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.post("/_/tunnel/stop")
async def api_tunnel_stop():
    return tunnel.stop_tunnel()


@app.post("/_/settings")
async def api_save_settings(request: Request):
    form = await request.form()
    cfg = _load_config()
    old_dc_token = cfg.get("discord_token", "")
    old_cf_token = cfg.get("cloudflare_token", "")
    cf_token = str(form.get("cloudflare_token", "")).strip()
    dc_token = str(form.get("discord_token", "")).strip()
    allowed_raw = str(form.get("allowed_users", "")).strip()
    new_user = str(form.get("username", "")).strip()
    new_pass = str(form.get("password", "")).strip()
    cfg["cloudflare_token"] = cf_token
    cfg["discord_token"] = dc_token
    cfg["allowed_users"] = [u.strip() for u in allowed_raw.split(",") if u.strip()]
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
    return RedirectResponse(url="/settings?saved=1", status_code=303)


# ── WebSocket: Live Logs ───────────────────────────────────────────────────────

@app.websocket("/ws/logs/{project_name}")
async def ws_logs(websocket: WebSocket, project_name: str):
    token = websocket.cookies.get(SESSION_COOKIE)
    if not _valid_session(token):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    log_path = LOGS_DIR / f"{project_name}.log"
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
            await websocket.send_text(json.dumps({"cpu": cpu, "ram": mem.percent, "active": running}))
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning(f"ws_stats error: {e}")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
