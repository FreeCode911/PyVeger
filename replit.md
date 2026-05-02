# PyPanel V3

A Python-based web management panel built with FastAPI and Jinja2 templates.

## Overview

PyPanel V3 is a self-hosted project manager that lets you run and manage Python scripts via a web UI. It supports:

- **Project management**: Create, start, stop, restart Python projects
- **File manager**: Upload, edit, and manage project files in-browser
- **Live logs**: Real-time log streaming via WebSocket
- **Settings**: Login credentials, Cloudflare tunnel, and Discord bot configuration
- **Discord bot**: Control projects via Discord slash commands
- **Cloudflare tunnel**: Expose the panel publicly

## Default credentials

- Username: `admin`
- Password: `admin`

## Structure

```
panel/
├── app.py           # FastAPI app, routes, auth middleware
├── manager.py       # Project lifecycle (start/stop/create/delete)
├── tunnel.py        # Cloudflare tunnel integration
├── discord_bot.py   # Discord bot with slash commands
├── config.json      # Settings (credentials, tokens)
├── database.json    # Project metadata
├── requirements.txt # Python dependencies
└── templates/
    ├── index.html   # Dashboard
    ├── login.html   # Login page
    ├── logs.html    # Live log viewer
    └── settings.html # Settings page
```

## Running

The app runs via the "Start application" workflow:
```
cd panel && pip install -r requirements.txt && python app.py
```

Binds to `PORT` env var (default 8000).

## Dependencies

- `fastapi` + `uvicorn` — web server
- `psutil` — CPU/RAM stats
- `python-multipart` — form handling
- `jinja2` — HTML templates
- `discord.py` (optional) — Discord bot

## Key routes

- `GET /` — Dashboard
- `GET /login`, `POST /login` — Auth
- `GET /settings`, `POST /_/settings` — Settings
- `GET /logs/{name}` — Live log page
- `/_/projects/*` — Project REST API
- `/_/tunnel/*` — Tunnel control
- `/_/status` — System status
- `WS /ws/stats` — Live CPU/RAM stats
- `WS /ws/logs/{name}` — Live project logs
