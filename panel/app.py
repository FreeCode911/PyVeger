import os
import json
import time
import asyncio
import logging
import mimetypes
from pathlib import Path
from typing import AsyncGenerator

import psutil
from fastapi import FastAPI, UploadFile, File, Request, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import manager
import tunnel
import discord_bot

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
logger = logging.getLogger("app")

BASE_DIR = Path(__file__).parent
SCRIPTS_DIR = BASE_DIR / "scripts"
LOGS_DIR = BASE_DIR / "logs"
CONFIG_FILE = BASE_DIR / "config.json"

app = FastAPI(title="Python Web Panel V3")

static_dir = BASE_DIR / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _load_config() -> dict:
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_config(cfg: dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


@app.on_event("startup")
async def on_startup():
    discord_bot.start_bot()
    cfg = _load_config()
    if cfg.get("cloudflare_token"):
        tunnel.start_tunnel()


# ─── Page Routes ──────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    scripts = manager.list_scripts()
    tunnel_status = tunnel.get_status()
    bot_status = discord_bot.get_bot_status()
    return templates.TemplateResponse("index.html", {
        "request": request,
        "scripts": scripts,
        "tunnel": tunnel_status,
        "bot": bot_status,
    })


@app.get("/logs/{script_name}", response_class=HTMLResponse)
async def logs_page(request: Request, script_name: str):
    return templates.TemplateResponse("logs.html", {
        "request": request,
        "script_name": script_name,
    })


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    cfg = _load_config()
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "cloudflare_token": cfg.get("cloudflare_token", ""),
        "discord_token": cfg.get("discord_token", ""),
        "allowed_users": ", ".join(cfg.get("allowed_users", [])),
    })


# ─── REST API ─────────────────────────────────────────────────────────────────

@app.get("/api/scripts")
async def api_list_scripts():
    return manager.list_scripts()


@app.get("/api/status")
async def api_status():
    cpu = psutil.cpu_percent(interval=0.1)
    mem = psutil.virtual_memory()
    scripts = manager.list_scripts()
    running = sum(1 for s in scripts if s["status"] == "running")
    return {
        "cpu": cpu,
        "ram": mem.percent,
        "active_scripts": running,
        "tunnel": tunnel.get_status(),
        "bot": discord_bot.get_bot_status(),
    }


@app.post("/api/start/{name}")
async def api_start(name: str):
    result = manager.start_script(name)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.post("/api/stop/{name}")
async def api_stop(name: str):
    result = manager.stop_script(name)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.post("/api/restart/{name}")
async def api_restart(name: str):
    result = manager.restart_script(name)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.post("/api/restart-all")
async def api_restart_all():
    return manager.restart_all()


@app.post("/api/upload")
async def api_upload(file: UploadFile = File(...)):
    filename = os.path.basename(file.filename or "")
    if not filename.endswith(".py"):
        raise HTTPException(status_code=400, detail="Only .py files allowed")
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    dest = SCRIPTS_DIR / filename
    content = await file.read()
    with open(dest, "wb") as f:
        f.write(content)
    return {"ok": True, "name": filename}


@app.delete("/api/delete/{name}")
async def api_delete(name: str):
    result = manager.delete_script(name)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.post("/api/tunnel/start")
async def api_tunnel_start():
    result = tunnel.start_tunnel()
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error"))
    return result


@app.post("/api/tunnel/stop")
async def api_tunnel_stop():
    return tunnel.stop_tunnel()


@app.post("/api/settings")
async def api_save_settings(request: Request):
    form = await request.form()
    cfg = _load_config()
    cf_token = str(form.get("cloudflare_token", "")).strip()
    dc_token = str(form.get("discord_token", "")).strip()
    allowed_raw = str(form.get("allowed_users", "")).strip()
    allowed = [u.strip() for u in allowed_raw.split(",") if u.strip()]

    cfg["cloudflare_token"] = cf_token
    cfg["discord_token"] = dc_token
    cfg["allowed_users"] = allowed
    _save_config(cfg)

    if cf_token and tunnel.get_status()["status"] != "running":
        tunnel.start_tunnel()
    elif not cf_token:
        tunnel.stop_tunnel()

    return RedirectResponse(url="/settings?saved=1", status_code=303)


# ─── WebSocket: Live Logs ─────────────────────────────────────────────────────

@app.websocket("/ws/logs/{script_name}")
async def ws_logs(websocket: WebSocket, script_name: str):
    await websocket.accept()
    log_path = LOGS_DIR / f"{script_name}.log"
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


# ─── WebSocket: Live Stats ────────────────────────────────────────────────────

@app.websocket("/ws/stats")
async def ws_stats(websocket: WebSocket):
    await websocket.accept()
    try:
        while True:
            cpu = psutil.cpu_percent(interval=None)
            mem = psutil.virtual_memory()
            scripts = manager.list_scripts()
            running = sum(1 for s in scripts if s["status"] == "running")
            data = {
                "cpu": cpu,
                "ram": mem.percent,
                "active": running,
            }
            import json as _json
            await websocket.send_text(_json.dumps(data))
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning(f"ws_stats error: {e}")


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, reload=False)
