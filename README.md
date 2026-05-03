<div align="center">
  <img src="static/logo.svg" width="96" height="96" alt="PyVegar Logo"/>
  <h1>PyVegar V3</h1>
</div>

[![License](https://img.shields.io/badge/license-Personal-informational)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11-blue?style=flat-square)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110-009688?style=flat-square)](https://fastapi.tiangolo.com)
[![Stars](https://img.shields.io/github/stars/FreeCode911/PyVeger?style=flat-square)](https://github.com/FreeCode911/PyVeger/stargazers)
[![Last Commit](https://img.shields.io/github/last-commit/FreeCode911/PyVeger?style=flat-square)](https://github.com/FreeCode911/PyVeger/commits/main)

> **PyVegar V3** is a sleek, self-hosted web management panel for running and monitoring Python bots, scripts, and services. Manage multiple projects, edit files in-browser, stream live logs, expose via Cloudflare tunnels, and control everything from Discord — all from a beautiful dark/light UI that works on desktop and mobile.

---

## ✨ Features

- ⚡ **Modern Responsive UI** — Dark/light theme with localStorage persistence, works on mobile and desktop
- 🐍 **Multi-Project Management** — Create, start, stop, restart, and delete Python projects; UUID-based project IDs
- 📁 **Full File Manager** — Recursive folder tree, create files/folders inside subfolders, rename, move, delete, upload
- 📝 **In-Browser Editor** — Syntax-aware textarea with Tab indentation and Ctrl+S save
- 📊 **Live Stats** — Real-time CPU/RAM via WebSocket (1 s interval)
- 🟢 **Real-Time Status Badge** — Project status updates live — running, restarting, error, stopped
- ⏱️ **Live Uptime Tile** — Server info card shows formatted uptime updated every second
- 📋 **Live Console** — WebSocket log streaming with clear button and auto-scroll
- 📦 **Package Manager** — Install/uninstall pip packages directly from the panel
- 🌐 **Cloudflare Quick Tunnel** — No account needed, one-click temporary public URL via trycloudflare.com
- 🔒 **Cloudflare Account Tunnels** — Create named tunnels via CF API, persistent URLs on your own domain; start/stop/delete from the panel
- 🤖 **Discord Bot** — Full slash command suite: start, stop, restart, logs, files, edit files, system stats
- 🔐 **Session Auth** — Cookie-based login (7-day TTL) with configurable credentials
- 🎨 **Modern Server Logs** — Coloured, structured output with PyVegar ASCII branding banner; HTTP lines parsed; WebSocket events formatted
- 🖼️ **Favicon** — PyVegar logo shown in browser tab on all pages

---

## 🖼️ Screenshots

| Dashboard | Server Manager | Settings |
|-----------|---------------|----------|
| ![Dashboard](screenshots/dashboard.png) | ![Server](screenshots/server.png) | ![Settings](screenshots/settings.png) |

| File Manager | Live Logs | Mobile Dashboard | Mobile Files |
|-------------|-----------|-----------------|--------------|
| ![Files](screenshots/files.png) | ![Logs](screenshots/logs.png) | ![Mobile](screenshots/mobile.png) | ![Mobile Files](screenshots/mobile_files.png) |

---

## 🚦 Quickstart

```bash
git clone https://github.com/FreeCode911/PyVeger.git
cd PyVeger
pip install -r requirements.txt
python app.py
```

Then open [http://localhost:8000](http://localhost:8000) — default login is `admin` / `admin`.

---

## 🔑 Default Credentials

| Field | Value |
|-------|-------|
| Username | `admin` |
| Password | `admin` |

> Change these in **Settings → Panel Login** immediately after first use.

---

## 🧭 Project Structure

```
PyVegar/
├── app.py              # FastAPI app — routes, auth middleware, WebSockets
├── manager.py          # Project lifecycle, file/folder ops, process control
├── log_config.py       # Coloured log formatter + PyVegar startup banner
├── tunnel.py           # Cloudflare quick tunnel + account tunnel API
├── discord_bot.py      # Discord bot slash commands
├── config.json         # Credentials, tokens, CF tunnel data
├── database.json       # Project metadata (status, PID, start file, restarts)
├── runtimes_cache.json # Detected runtimes (auto-generated on first boot)
├── requirements.txt    # Python dependencies
├── templates/          # Jinja2 HTML (login, index, server, logs, settings)
├── static/             # Static assets (tw.css, logo.svg)
├── scripts/            # Project files — each project in its own UUID subfolder
└── logs/               # Per-project log files (UUID-named)
```

---

## 🛠️ Tech Stack

| Layer | Tech |
|-------|------|
| Backend | Python 3.11, [FastAPI](https://fastapi.tiangolo.com/) |
| Frontend | HTML5, CSS variables, [Tailwind CDN](https://tailwindcss.com/), [Lucide Icons](https://lucide.dev/) |
| Realtime | FastAPI WebSockets — logs + stats, 1 s push interval |
| Tunneling | Cloudflare cloudflared + Cloudflare API |
| Bot | [discord.py](https://discordpy.readthedocs.io/) |
| Process | psutil, subprocess |

---

## 🎨 Server Log Format

PyVegar V3 uses a structured, colour-coded log format with an ASCII banner on every startup:

```
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    ██████╗ ██╗   ██╗██╗   ██╗███████╗ ██████╗  █████╗ ██████╗
    ██╔══██╗╚██╗ ██╔╝╚██╗ ██╔╝██╔════╝██╔════╝ ██╔══██╗██╔══██╗
    ██████╔╝ ╚████╔╝  ╚████╔╝ █████╗  ██║  ███╗███████║██████╔╝
    ██╔═══╝   ╚██╔╝    ╚██╔╝  ██╔══╝  ██║   ██║██╔══██║██╔══██╗
    ██║        ██║      ██║   ███████╗╚██████╔╝██║  ██║██║  ██║
    ╚═╝        ╚═╝      ╚═╝   ╚══════╝ ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝
    Server Management Panel  ·  Python · FastAPI · SQLite
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  10:33:49  ● INFO   uvicorn  ▶ Started   PID 42053
  10:33:49  ● INFO   uvicorn  ✔ Ready     application startup complete
  10:33:49  ● INFO   uvicorn  ◉ Listening  http://0.0.0.0:8000
  10:33:50  ● INFO   http     GET     /login                 →  200
  10:33:52  ● INFO   http     POST    /login                 →  303
  10:33:53  ▲ WARN   tunnel   Tunnel exited after 0.1s — invalid token
```

| Symbol | Level |
|--------|-------|
| `●` blue | INFO |
| `▲` yellow | WARNING |
| `✖` red | ERROR / CRITICAL |

HTTP status colours: `2xx` green · `3xx` blue · `4xx` yellow · `5xx` red

---

## 🌐 Cloudflare Tunnels

### Quick Tunnel (no account)
1. Go to **Settings → Quick Tunnel**
2. Enter your local port and click **Start**
3. A temporary `*.trycloudflare.com` URL is generated — changes on every restart

### Account Tunnels (persistent URL on your domain)
1. Go to **Settings → Cloudflare Account Tunnels**
2. Enter your **Account ID** (CF Dashboard → right sidebar) and an **API Token** with:
   - `Cloudflare Tunnel:Edit`
   - `DNS:Edit`
   - `Zone:Read`
3. Click **Save & Connect** — your domains load automatically
4. Click **New Tunnel**, pick a subdomain + domain + port
5. The panel creates the tunnel, configures ingress, and adds the DNS CNAME record
6. Click **Start** — your service is live at `https://subdomain.yourdomain.com`

---

## 🤖 Discord Bot

1. Create a bot at [discord.com/developers/applications](https://discord.com/developers/applications)
2. Copy the bot token and paste it in **Settings → Discord Bot**
3. Add allowed Discord user IDs or usernames (comma-separated)
4. Invite the bot to your server

Available slash commands:

| Command | Description |
|---------|-------------|
| `/projects` | List all servers with status, uptime, PID |
| `/status <project>` | Detailed info + Start / Stop / Restart / Logs buttons |
| `/start <project>` | Start a stopped server |
| `/stop <project>` | Stop a running server |
| `/restart <project>` | Restart a server |
| `/restart_all` | Restart all running servers |
| `/logs <project> [lines]` | View recent log lines (up to 50) |
| `/files <project>` | List all files in a project |
| `/editfile <project> <file>` | Edit a file directly from Discord |
| `/system` | CPU, RAM, disk usage + tunnel status |
| `/help` | Show all commands |

---

## 📱 Mobile Support

PyVegar V3 is fully responsive:
- **Dashboard** — card grid adapts to single column on small screens
- **File Manager** — master-detail view: tap a file to open the editor full-screen
- **Settings** — all forms stack to single column
- **Logs** — full-screen terminal view on any device

---

## 📚 Usage Examples

```bash
# Start the panel
python app.py

# Access locally
http://localhost:8000

# Make public via quick tunnel
Settings → Quick Tunnel → Start

# Create a project
Dashboard → New Server → name it → Manage → upload/create files → Start

# Install a package into a project
Server page → Packages tab → type package name → Install
```

---

## 📋 Changelog

### V3 — Current

- **Modern server logs** — colour-coded output (`log_config.py`): level badges (`●` `▲` `✖`), parsed HTTP lines, WebSocket events, PyVegar ASCII banner on startup
- **Real-time status badge** — WebSocket stats payload includes project `id`; status badge updates every 1 s (was stuck after start/stop)
- **Live uptime tile** — server info card shows formatted uptime (`2h 14m`, `45s`) refreshed every second
- **UUID project IDs** — projects keyed and folder-named by UUID for full portability
- **Favicon** — PyVegar logo in browser tab on all pages
- **Code cleanup** — removed dead `_hash()`, `SCRIPTS_DIR` (app.py), `_migrate_db()` (manager.py), and unused `status_txt` (discord_bot.py)

---

## 🧑‍💻 Contributing

Pull requests are welcome. For significant changes, open an issue first to discuss what you'd like to change.

---

## 📝 License

**Personal Use Only.** Commercial, educational, or organizational use requires the author's permission. See [`LICENSE`](LICENSE) for full terms.

---

## 📫 Contact

Questions or bug reports? [Open an issue](https://github.com/FreeCode911/PyVeger/issues)
