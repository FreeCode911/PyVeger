import json
import threading
import logging
from pathlib import Path

import psutil

logger = logging.getLogger("discord_bot")
BASE_DIR = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "config.json"

_bot_thread: threading.Thread | None = None
_bot_running = False


def _load_config() -> dict:
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _fmt_uptime(seconds: int | None) -> str:
    if not seconds:
        return "—"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def _status_color(status: str) -> int:
    return {"running": 0x22c55e, "stopped": 0xef4444, "restarting": 0xf59e0b}.get(status, 0x6366f1)


def _status_emoji(status: str) -> str:
    return {"running": "🟢", "stopped": "🔴", "restarting": "🟡"}.get(status, "⚪")


def _bar(pct: float, length: int = 12) -> str:
    filled = round(pct / 100 * length)
    return "█" * filled + "░" * (length - filled) + f"  {pct:.1f}%"


def _resolve_project(project_ref: str) -> dict | None:
    import manager
    if not project_ref:
        return None
    proj = manager.get_project(project_ref)
    if proj:
        return proj
    ref = project_ref.strip().lower()
    for item in manager.list_projects():
        if item["name"].lower() == ref:
            return item
    return None


def _project_label(project: dict) -> str:
    return project.get("name") or project.get("id", "unknown")


def _presence_messages() -> list[tuple[str, str]]:
    import manager
    import tunnel as tun

    projects = manager.list_projects()
    total = len(projects)
    running = sum(1 for p in projects if p["status"] == "running")
    tunnel_status = tun.get_status().get("status", "stopped")

    return [
        ("watching", f"{running}/{total} servers running"),
        ("playing", "/help for commands"),
        ("watching", f"Tunnel {tunnel_status}"),
        ("playing", "PyVegar panel"),
    ]


def _run_bot(token: str):
    global _bot_running
    try:
        import discord
        from discord import app_commands
    except ImportError:
        logger.error("discord.py not installed")
        return

    import manager
    import tunnel as tun

    intents = discord.Intents.default()
    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)
    _presence_task = None

    # ── Permission check ──────────────────────────────────────────────────────

    def _allowed(interaction: discord.Interaction) -> bool:
        cfg = _load_config()
        allowed = [u.strip().lower() for u in cfg.get("allowed_users", [])]
        uid = str(interaction.user.id)
        identities = {
            uid,
            interaction.user.name.lower(),
            getattr(interaction.user, "display_name", "").lower(),
            (getattr(interaction.user, "global_name", "") or "").lower(),
            str(interaction.user).lower(),
        }
        if interaction.guild and str(interaction.guild.owner_id) == uid:
            return True
        return any(identity for identity in identities if identity in allowed)

    async def _deny(interaction: discord.Interaction):
        embed = discord.Embed(
            title="🚫 Permission Denied",
            description="You are not authorized to use this command.\nContact the panel owner to be added to the allowed users list.",
            color=0xef4444,
        )
        embed.set_footer(text="PyVegar")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    ANSI = {
        "reset": "\u001b[0m",
        "red": "\u001b[1;31m",
        "green": "\u001b[1;32m",
        "yellow": "\u001b[1;33m",
        "blue": "\u001b[1;34m",
        "cyan": "\u001b[1;36m",
        "gray": "\u001b[0;37m",
    }

    def _embed(title: str, description: str = "", color: int = 0x6366f1, footer: str = "PyVegar"):
        embed = discord.Embed(
            title=title,
            description=description,
            color=color,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text=footer)
        return embed

    def _ok_embed(title: str, description: str = "", footer: str = "PyVegar"):
        return _embed(title, description, 0x22c55e, footer)

    def _err_embed(title: str, description: str = "", footer: str = "PyVegar"):
        return _embed(title, description, 0xef4444, footer)

    def _warn_embed(title: str, description: str = "", footer: str = "PyVegar"):
        return _embed(title, description, 0xf59e0b, footer)

    def _ansi_block(lines: list[str]) -> str:
        return "```ansi\n" + "\n".join(lines)[:3900] + "\n```"

    def _ansi_status(status: str, text: str) -> str:
        color = {
            "running": ANSI["green"],
            "stopped": ANSI["red"],
            "restarting": ANSI["yellow"],
            "error": ANSI["red"],
            "installing": ANSI["blue"],
        }.get(status, ANSI["gray"])
        return f"{color}{text}{ANSI['reset']}"

    def _ansi_bar(label: str, pct: float) -> str:
        color = ANSI["green"] if pct < 60 else ANSI["yellow"] if pct < 85 else ANSI["red"]
        return f"{ANSI['cyan']}{label:<6}{ANSI['reset']} {color}{_bar(pct)}{ANSI['reset']}"

    def _trim_code(text: str, limit: int = 3800) -> str:
        text = text or ""
        return text if len(text) <= limit else "…\n" + text[-(limit - 2):]

    # ── Autocomplete ──────────────────────────────────────────────────────────

    async def _project_ac(interaction: discord.Interaction, current: str):
        projects = manager.list_projects()
        return [
            app_commands.Choice(name=p["name"][:100], value=p["id"])
            for p in projects
            if current.lower() in p["name"].lower() or current.lower() in p["id"].lower()
        ][:25]

    async def _file_ac(interaction: discord.Interaction, current: str):
        project_ref = getattr(interaction.namespace, "project", "")
        proj = _resolve_project(project_ref)
        if not proj:
            return []
        tree = manager.list_project_tree(proj["id"])
        files: list[str] = []
        stack = list(tree)
        while stack:
            node = stack.pop()
            if node.get("type") == "file":
                files.append(node["path"])
            else:
                stack.extend(node.get("children", []))
        return [
            app_commands.Choice(name=f, value=f)
            for f in files if current.lower() in f.lower()
        ][:25]

    # ── Interactive View ──────────────────────────────────────────────────────

    class ProjectView(discord.ui.View):
        def __init__(self, project_id: str, project_name: str, status: str):
            super().__init__(timeout=180)
            self.project_id = project_id
            self.project_name = project_name
            self._refresh_buttons(status)

        def _refresh_buttons(self, status: str):
            is_running = status == "running"
            self.start_btn.disabled  = is_running
            self.stop_btn.disabled   = not is_running
            self.restart_btn.disabled = not is_running

        @discord.ui.button(label="Start", style=discord.ButtonStyle.success, emoji="▶️", row=0)
        async def start_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
            await interaction.response.defer()
            result = manager.start_project(self.project_id)
            if result.get("ok"):
                embed = _ok_embed("✅ Server Started", f"**{self.project_name}** is now running.")
                embed.add_field(name="PID", value=f"`{result.get('pid')}`")
                self._refresh_buttons("running")
            else:
                embed = _err_embed("❌ Failed to Start", _ansi_block([f"{ANSI['red']}{result.get('error', 'Unknown error')}{ANSI['reset']}"]))
            await interaction.message.edit(view=self)
            await interaction.followup.send(embed=embed, ephemeral=True)

        @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, emoji="⏹️", row=0)
        async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
            await interaction.response.defer()
            result = manager.stop_project(self.project_id)
            if result.get("ok"):
                embed = _warn_embed("⏹️ Server Stopped", f"**{self.project_name}** has been stopped.")
                self._refresh_buttons("stopped")
            else:
                embed = _err_embed("❌ Failed to Stop", _ansi_block([f"{ANSI['red']}{result.get('error', 'Unknown error')}{ANSI['reset']}"]))
            await interaction.message.edit(view=self)
            await interaction.followup.send(embed=embed, ephemeral=True)

        @discord.ui.button(label="Restart", style=discord.ButtonStyle.secondary, emoji="🔄", row=0)
        async def restart_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
            await interaction.response.defer()
            result = manager.restart_project(self.project_id)
            if result.get("ok"):
                embed = _warn_embed("🔄 Server Restarted", f"**{self.project_name}** has been restarted.")
                embed.add_field(name="New PID", value=f"`{result.get('pid')}`")
                self._refresh_buttons("running")
            else:
                embed = _err_embed("❌ Failed to Restart", _ansi_block([f"{ANSI['red']}{result.get('error', 'Unknown error')}{ANSI['reset']}"]))
            await interaction.message.edit(view=self)
            await interaction.followup.send(embed=embed, ephemeral=True)

        @discord.ui.button(label="View Logs", style=discord.ButtonStyle.primary, emoji="📋", row=0)
        async def logs_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
            await interaction.response.defer(ephemeral=True)
            log_lines = manager.get_log_lines(self.project_id, n=25)
            content = "\n".join(log_lines) if log_lines else "No logs yet."
            embed = _embed(
                f"📋 Logs — {self.project_name}",
                _ansi_block([_trim_code(content)]),
                0x6366f1,
                "Last 25 lines · PyVegar",
            )
            await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /projects ─────────────────────────────────────────────────────────────

    @tree.command(name="projects", description="List all servers and their current status")
    async def cmd_projects(interaction: discord.Interaction):
        if not _allowed(interaction):
            await _deny(interaction); return
        await interaction.response.defer()
        projects = manager.list_projects()
        if not projects:
            embed = _embed("⚡ PyVegar — Servers", "No servers found. Create one in the web panel.")
            await interaction.followup.send(embed=embed); return

        running = sum(1 for p in projects if p["status"] == "running")
        embed = _embed("⚡ PyVegar — Servers", f"**{running}** running · **{len(projects) - running}** stopped · **{len(projects)}** total")
        for p in projects:
            uptime = _fmt_uptime(p.get("uptime"))
            pid = p.get("pid") or "—"
            cmd = p.get("startup_command") or f"python3 {p.get('start_file', 'main.py')}"
            if len(cmd) > 45:
                cmd = cmd[:42] + "…"
            embed.add_field(
                name=f"{_status_emoji(p['status'])}  {p['name']}",
                value=_ansi_block([
                    _ansi_status(p["status"], f"STATUS   {p['status']}"),
                    f"{ANSI['cyan']}PID      {ANSI['reset']}{pid}",
                    f"{ANSI['cyan']}UPTIME   {ANSI['reset']}{uptime}",
                    f"{ANSI['cyan']}RESTARTS {ANSI['reset']}{p.get('restarts', 0)}",
                    f"{ANSI['cyan']}CMD      {ANSI['reset']}{cmd}",
                ]),
                inline=False,
            )
        embed.set_footer(text="PyVegar · Use /status <name> for details")
        await interaction.followup.send(embed=embed)

    # ── /status ───────────────────────────────────────────────────────────────

    @tree.command(name="status", description="Detailed status of a specific server with action buttons")
    @app_commands.describe(project="Server name")
    @app_commands.autocomplete(project=_project_ac)
    async def cmd_status(interaction: discord.Interaction, project: str):
        if not _allowed(interaction):
            await _deny(interaction); return
        await interaction.response.defer()
        p = _resolve_project(project)
        if not p:
            embed = discord.Embed(
                title="Not Found",
                description=f"Server `{project}` does not exist.",
                color=0xef4444,
            )
            await interaction.followup.send(embed=embed, ephemeral=True); return

        embed = _embed(f"{_status_emoji(p['status'])}  {p['name']}", color=_status_color(p["status"]))
        embed.description = _ansi_block([
            _ansi_status(p["status"], f"STATUS    {p['status']}"),
            f"{ANSI['cyan']}PID       {ANSI['reset']}{p.get('pid') or '—'}",
            f"{ANSI['cyan']}UPTIME    {ANSI['reset']}{_fmt_uptime(p.get('uptime'))}",
            f"{ANSI['cyan']}RESTARTS  {ANSI['reset']}{p.get('restarts', 0)}",
            f"{ANSI['cyan']}FILES     {ANSI['reset']}{len(p.get('files', []))}",
            f"{ANSI['cyan']}START     {ANSI['reset']}{p.get('start_file', '—')}",
        ])
        if p.get("startup_command"):
            embed.add_field(name="Startup Command", value=f"```sh\n{p['startup_command'][:120]}\n```", inline=False)
        if p.get("start_time"):
            embed.add_field(name="Last Started", value=f"<t:{int(p['start_time'])}:R>", inline=True)
        embed.set_footer(text="PyVegar · Use the buttons below to control the server")
        view = ProjectView(p["id"], p["name"], p["status"])
        await interaction.followup.send(embed=embed, view=view)

    # ── /start ────────────────────────────────────────────────────────────────

    @tree.command(name="start", description="Start a server")
    @app_commands.describe(project="Server name")
    @app_commands.autocomplete(project=_project_ac)
    async def cmd_start(interaction: discord.Interaction, project: str):
        if not _allowed(interaction):
            await _deny(interaction); return
        await interaction.response.defer()
        proj = _resolve_project(project)
        if not proj:
            await interaction.followup.send(embed=discord.Embed(title="❌ Project Not Found", description=f"No server named `{project}`.", color=0xef4444), ephemeral=True); return
        result = manager.start_project(proj["id"])
        if result.get("ok"):
            embed = _ok_embed("✅ Server Started", f"**{_project_label(proj)}** is now running.")
            embed.add_field(name="PID", value=f"`{result.get('pid')}`", inline=True)
        else:
            embed = _err_embed("❌ Failed to Start", _ansi_block([f"{ANSI['red']}{result.get('error', 'Unknown error')}{ANSI['reset']}"]))
        await interaction.followup.send(embed=embed)

    # ── /stop ─────────────────────────────────────────────────────────────────

    @tree.command(name="stop", description="Stop a server")
    @app_commands.describe(project="Server name")
    @app_commands.autocomplete(project=_project_ac)
    async def cmd_stop(interaction: discord.Interaction, project: str):
        if not _allowed(interaction):
            await _deny(interaction); return
        await interaction.response.defer()
        proj = _resolve_project(project)
        if not proj:
            await interaction.followup.send(embed=discord.Embed(title="❌ Project Not Found", description=f"No server named `{project}`.", color=0xef4444), ephemeral=True); return
        result = manager.stop_project(proj["id"])
        if result.get("ok"):
            embed = _warn_embed("⏹️ Server Stopped", f"**{_project_label(proj)}** has been stopped.")
        else:
            embed = _err_embed("❌ Failed to Stop", _ansi_block([f"{ANSI['red']}{result.get('error', 'Unknown error')}{ANSI['reset']}"]))
        await interaction.followup.send(embed=embed)

    # ── /restart ──────────────────────────────────────────────────────────────

    @tree.command(name="restart", description="Restart a server")
    @app_commands.describe(project="Server name")
    @app_commands.autocomplete(project=_project_ac)
    async def cmd_restart(interaction: discord.Interaction, project: str):
        if not _allowed(interaction):
            await _deny(interaction); return
        await interaction.response.defer()
        proj = _resolve_project(project)
        if not proj:
            await interaction.followup.send(embed=discord.Embed(title="❌ Project Not Found", description=f"No server named `{project}`.", color=0xef4444), ephemeral=True); return
        result = manager.restart_project(proj["id"])
        if result.get("ok"):
            embed = _warn_embed("🔄 Server Restarted", f"**{_project_label(proj)}** has been restarted.")
            embed.add_field(name="New PID", value=f"`{result.get('pid')}`", inline=True)
        else:
            embed = _err_embed("❌ Failed to Restart", _ansi_block([f"{ANSI['red']}{result.get('error', 'Unknown error')}{ANSI['reset']}"]))
        await interaction.followup.send(embed=embed)

    # ── /restart_all ──────────────────────────────────────────────────────────

    @tree.command(name="restart_all", description="Restart all currently running servers")
    async def cmd_restart_all(interaction: discord.Interaction):
        if not _allowed(interaction):
            await _deny(interaction); return
        await interaction.response.defer()
        results = manager.restart_all()
        restarted = [list(r.keys())[0] for r in results if list(r.values())[0].get("ok")]
        failed    = [list(r.keys())[0] for r in results if not list(r.values())[0].get("ok")]
        embed = _warn_embed("🔄 Restart All")
        if restarted:
            embed.add_field(name=f"✅ Restarted ({len(restarted)})", value="\n".join(f"`{n}`" for n in restarted), inline=False)
        if failed:
            embed.add_field(name=f"❌ Failed ({len(failed)})", value="\n".join(f"`{n}`" for n in failed), inline=False)
        if not restarted and not failed:
            embed.description = "No running servers to restart."
        embed.set_footer(text="PyVegar")
        await interaction.followup.send(embed=embed)

    # ── /logs ─────────────────────────────────────────────────────────────────

    @tree.command(name="logs", description="Show recent log lines for a server")
    @app_commands.describe(project="Server name", lines="Number of lines to show (1–50, default 20)")
    @app_commands.autocomplete(project=_project_ac)
    async def cmd_logs(interaction: discord.Interaction, project: str, lines: int = 20):
        if not _allowed(interaction):
            await _deny(interaction); return
        await interaction.response.defer()
        proj = _resolve_project(project)
        if not proj:
            await interaction.followup.send(embed=discord.Embed(title="❌ Project Not Found", description=f"No server named `{project}`.", color=0xef4444), ephemeral=True); return
        lines = max(1, min(lines, 50))
        log_lines = manager.get_log_lines(proj["id"], n=lines)
        if not log_lines:
            embed = discord.Embed(
                title=f"📋 Logs — {_project_label(proj)}",
                description="No logs found for this server yet.",
                color=0x6366f1,
            )
            await interaction.followup.send(embed=embed, ephemeral=True); return

        content = "\n".join(log_lines)
        embed = _embed(
            f"📋 Logs — {_project_label(proj)}",
            _ansi_block([_trim_code(content)]),
            0x6366f1,
            f"Last {lines} lines · Status: {proj['status']} · PyVegar",
        )
        await interaction.followup.send(embed=embed)

    # ── /system ───────────────────────────────────────────────────────────────

    @tree.command(name="system", description="Show system resource usage and panel overview")
    async def cmd_system(interaction: discord.Interaction):
        if not _allowed(interaction):
            await _deny(interaction); return
        await interaction.response.defer()

        cpu  = psutil.cpu_percent(interval=0.4)
        mem  = psutil.virtual_memory()
        disk = psutil.disk_usage("/")
        projects  = manager.list_projects()
        running   = sum(1 for p in projects if p["status"] == "running")
        total     = len(projects)
        tunnel_st = tun.get_status()

        color = 0xef4444 if (cpu > 85 or mem.percent > 85) else (0xf59e0b if (cpu > 60 or mem.percent > 60) else 0x22c55e)

        embed = _embed("⚡ System Overview", color=color)
        embed.description = _ansi_block([
            _ansi_bar("CPU", cpu),
            _ansi_bar("RAM", mem.percent),
            _ansi_bar("DISK", disk.percent),
        ])
        tun_emoji = "🟢" if tunnel_st.get("status") == "running" else "🔴"
        embed.add_field(name="🖧 Servers",  value=f"`{running}/{total}` running",                            inline=True)
        embed.add_field(name="🌐 Tunnel",   value=f"{tun_emoji} `{tunnel_st.get('status', 'stopped')}`",   inline=True)
        embed.add_field(name="🔢 CPU Cores", value=f"`{psutil.cpu_count()}`",                               inline=True)
        embed.add_field(name="Memory", value=f"`{mem.used // 1024**2} MB / {mem.total // 1024**2} MB`", inline=True)
        embed.add_field(name="Disk", value=f"`{disk.used / 1024**3:.1f} GB / {disk.total / 1024**3:.1f} GB`", inline=True)
        embed.set_footer(text="PyVegar")
        await interaction.followup.send(embed=embed)

    # ── /files ────────────────────────────────────────────────────────────────

    @tree.command(name="files", description="List all files in a server project")
    @app_commands.describe(project="Server name")
    @app_commands.autocomplete(project=_project_ac)
    async def cmd_files(interaction: discord.Interaction, project: str):
        if not _allowed(interaction):
            await _deny(interaction); return
        await interaction.response.defer()

        proj = _resolve_project(project)
        if not proj:
            embed = discord.Embed(title="❌ Project Not Found", description=f"No server named `{project}`.", color=0xef4444)
            await interaction.followup.send(embed=embed, ephemeral=True); return

        files = manager.list_project_tree(proj["id"])
        flat_files: list[str] = []
        stack = list(files)
        while stack:
            node = stack.pop()
            if node.get("type") == "file":
                flat_files.append(node["path"])
            else:
                stack.extend(node.get("children", []))
        flat_files.sort()
        desc = "\n".join(f"📄 `{f}`" for f in flat_files[:50]) if flat_files else "_No files yet._"
        if len(flat_files) > 50:
            desc += f"\n...and `{len(flat_files) - 50}` more"
        embed = _embed(
            f"📁 Files — {_project_label(proj)}",
            desc,
            _status_color(proj["status"]),
            f"PyVegar · {len(flat_files)} file{'s' if len(flat_files) != 1 else ''}",
        )
        await interaction.followup.send(embed=embed)

    # ── /editfile ─────────────────────────────────────────────────────────────

    @tree.command(name="editfile", description="Edit a file in a server project via Discord")
    @app_commands.describe(project="Server name", filename="File to edit")
    @app_commands.autocomplete(project=_project_ac, filename=_file_ac)
    async def cmd_editfile(interaction: discord.Interaction, project: str, filename: str):
        if not _allowed(interaction):
            await _deny(interaction); return

        proj = _resolve_project(project)
        if not proj:
            embed = discord.Embed(title="❌ Project Not Found", description=f"No server named `{project}`.", color=0xef4444)
            await interaction.response.send_message(embed=embed, ephemeral=True); return

        current_content = manager.get_file_content(proj["id"], filename)
        if current_content is None:
            embed = discord.Embed(title="❌ File Not Found", description=f"`{filename}` not found in `{project}`.", color=0xef4444)
            await interaction.response.send_message(embed=embed, ephemeral=True); return

        class EditModal(discord.ui.Modal):
            def __init__(self_m):
                super().__init__(title=f"✏️ {filename}"[:45])
                self_m.file_content = discord.ui.TextInput(
                    label=filename[:45],
                    style=discord.TextStyle.paragraph,
                    default=current_content[:4000],
                    max_length=4000,
                    required=True,
                )
                self_m.add_item(self_m.file_content)

            async def on_submit(self_m, inter: discord.Interaction):
                result = manager.save_file_content(proj["id"], filename, self_m.file_content.value)
                if result.get("ok"):
                    embed = _ok_embed("✅ File Saved", f"`{filename}` in **{_project_label(proj)}** has been updated.")
                    embed.add_field(name="Size", value=f"`{len(self_m.file_content.value)} chars`", inline=True)
                    await inter.response.send_message(embed=embed, ephemeral=True)
                else:
                    embed = _err_embed("❌ Save Error", _ansi_block([f"{ANSI['red']}{result.get('error', 'Unknown error')}{ANSI['reset']}"]))
                    await inter.response.send_message(embed=embed, ephemeral=True)

        await interaction.response.send_modal(EditModal())

    # ── /help ─────────────────────────────────────────────────────────────────

    @tree.command(name="help", description="Show all available PyVegar commands")
    async def cmd_help(interaction: discord.Interaction):
        embed = _embed("⚡ PyVegar — Help", "Remote control your server panel from Discord.")
        cmds = [
            ("📋 /projects",                        "List all servers with status, uptime & PID"),
            ("🔍 /status `<project>`",               "Detailed info + Start / Stop / Restart / Logs buttons"),
            ("▶️ /start `<project>`",                "Start a stopped server"),
            ("⏹️ /stop `<project>`",                 "Stop a running server"),
            ("🔄 /restart `<project>`",              "Restart a server"),
            ("🔄 /restart_all",                      "Restart all currently running servers"),
            ("📄 /logs `<project>` `[lines]`",       "View recent logs (up to 50 lines)"),
            ("📁 /files `<project>`",                "List all files in a server project"),
            ("✏️ /editfile `<project>` `<file>`",    "Edit a file directly from Discord"),
            ("⚡ /system",                           "CPU, RAM, disk usage + tunnel status"),
            ("❓ /help",                             "Show this message"),
        ]
        for name, desc in cmds:
            embed.add_field(name=name, value=desc, inline=False)
        embed.add_field(
            name="Output Styles",
            value="`ansi` logs, colored status blocks, buttons, modals, autocompletes, embeds, timestamps, and rotating presence are enabled.",
            inline=False,
        )
        embed.set_footer(text="PyVegar · All commands require permission except /help")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tree.error
    async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
        logger.warning(f"Discord command error: {error}")
        msg = "Something went wrong while running that command."
        if isinstance(error, app_commands.CommandInvokeError) and error.original:
            msg = str(error.original)
        embed = discord.Embed(title="❌ Command Error", description=msg[:4000], color=0xef4444)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(embed=embed, ephemeral=True)
            else:
                await interaction.response.send_message(embed=embed, ephemeral=True)
        except Exception:
            pass

    # ── Events ────────────────────────────────────────────────────────────────

    async def _rotate_presence():
        idx = 0
        while not client.is_closed():
            try:
                mode, text = _presence_messages()[idx % 4]
                if mode == "watching":
                    activity = discord.Activity(type=discord.ActivityType.watching, name=text)
                else:
                    activity = discord.Game(name=text)
                await client.change_presence(status=discord.Status.online, activity=activity)
                idx += 1
            except Exception as e:
                logger.debug(f"Presence update skipped: {e}")
            await asyncio.sleep(45)

    @client.event
    async def on_ready():
        nonlocal _presence_task
        global _bot_running
        await tree.sync()
        logger.info(f"Discord bot logged in as {client.user} — slash commands synced")
        _bot_running = True
        if _presence_task is None or _presence_task.done():
            _presence_task = asyncio.create_task(_rotate_presence())

    @client.event
    async def on_disconnect():
        nonlocal _presence_task
        global _bot_running
        _bot_running = False
        if _presence_task and not _presence_task.done():
            _presence_task.cancel()

    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(client.start(token))
    except Exception as e:
        logger.error(f"Discord bot error: {e}")
    finally:
        _bot_running = False


def start_bot():
    global _bot_thread, _bot_running
    cfg = _load_config()
    token = cfg.get("discord_token", "")
    if not token:
        logger.info("No Discord token configured, bot not started")
        return
    if _bot_running:
        return
    _bot_thread = threading.Thread(target=_run_bot, args=(token,), daemon=True)
    _bot_thread.start()
    logger.info("Discord bot thread started")


def get_bot_status() -> dict:
    return {"running": _bot_running}
