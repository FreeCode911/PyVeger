# PyVegar

A self-hosted Python web management panel built with FastAPI and Jinja2.

## Overview

PyVegar lets you run and monitor multiple Python (and other language) projects from a clean dark/light web UI. Features:

- **Multi-project management** — create, start, stop, restart projects; UUID-based IDs
- **File manager** — recursive folder tree, create/rename/move/delete/upload files, in-browser editor
- **Live logs & stats** — WebSocket log streaming, real-time CPU/RAM and per-project status (1 s interval)
- **Package manager** — install/uninstall pip packages from the panel
- **Cloudflare tunnels** — quick (trycloudflare.com) and account-managed named tunnels via CF API
- **Discord bot** — control projects via slash commands
- **Session auth** — cookie-based login with configurable credentials

## Default credentials

- Username: `admin`
- Password: `admin`

Change in **Settings → Security** on first use.

## Structure

```
PyVegar/
├── app.py              # FastAPI app — routes, auth middleware, WebSockets
├── manager.py          # Project lifecycle, file ops, process control
├── tunnel.py           # Cloudflare quick tunnel + account tunnel API
├── discord_bot.py      # Discord bot slash commands
├── config.json         # Credentials, tokens, CF tunnel data
├── database.json       # Project metadata (status, PID, start file, restarts)
├── runtimes_cache.json # Detected language runtimes (auto-generated)
├── requirements.txt    # Python dependencies
├── templates/          # Jinja2 HTML (login, index, server, logs, settings)
├── static/             # tw.css, logo.svg
├── scripts/            # Per-project files (UUID-named subfolders)
└── logs/               # Per-project log files (UUID-named)
```

## Running

The app is started by the "Start application" workflow:
```
pip install -q -r requirements.txt && python app.py
```

Binds to `PORT` env var (default 8000).

## Key routes

| Method | Path | Description |
|--------|------|-------------|
| GET | `/` | Dashboard |
| GET/POST | `/login` | Auth |
| GET | `/logout` | Logout |
| GET | `/server/{id}` | Server console + file manager |
| GET | `/logs/{id}` | Full-screen live log viewer |
| GET/POST | `/settings` | Settings page |
| GET | `/_/projects` | List all projects |
| POST | `/_/projects/create` | Create project |
| POST/DELETE | `/_/projects/{id}/start\|stop\|restart` | Control project |
| GET | `/_/runtimes` | Detected runtimes |
| POST | `/_/tunnel/start\|stop` | Cloudflare quick tunnel |
| GET | `/_/status` | System stats (JSON) |
| WS | `/ws/stats` | Live CPU/RAM + project statuses |
| WS | `/ws/logs/{id}` | Live project log stream |

## Dependencies

- `fastapi` + `uvicorn` — web server
- `psutil` — system stats
- `python-multipart` — form/file upload handling
- `jinja2` — HTML templates
- `discord.py` (optional) — Discord bot
