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
                embed = discord.Embed(
                    title="✅ Server Started",
                    description=f"**{self.project_name}** is now running.",
                    color=0x22c55e, timestamp=discord.utils.utcnow(),
                )
                embed.add_field(name="PID", value=f"`{result.get('pid')}`")
                self._refresh_buttons("running")
            else:
                embed = discord.Embed(
                    title="❌ Failed to Start",
                    description=f"```{result.get('error', 'Unknown error')}```",
                    color=0xef4444,
                )
            embed.set_footer(text="PyVegar")
            await interaction.message.edit(view=self)
            await interaction.followup.send(embed=embed, ephemeral=True)

        @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger, emoji="⏹️", row=0)
        async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
            await interaction.response.defer()
            result = manager.stop_project(self.project_id)
            if result.get("ok"):
                embed = discord.Embed(
                    title="⏹️ Server Stopped",
                    description=f"**{self.project_name}** has been stopped.",
                    color=0xef4444, timestamp=discord.utils.utcnow(),
                )
                self._refresh_buttons("stopped")
            else:
                embed = discord.Embed(
                    title="❌ Failed to Stop",
                    description=f"```{result.get('error', 'Unknown error')}```",
                    color=0xef4444,
                )
            embed.set_footer(text="PyVegar")
            await interaction.message.edit(view=self)
            await interaction.followup.send(embed=embed, ephemeral=True)

        @discord.ui.button(label="Restart", style=discord.ButtonStyle.secondary, emoji="🔄", row=0)
        async def restart_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
            await interaction.response.defer()
            result = manager.restart_project(self.project_id)
            if result.get("ok"):
                embed = discord.Embed(
                    title="🔄 Server Restarted",
                    description=f"**{self.project_name}** has been restarted.",
                    color=0xf59e0b, timestamp=discord.utils.utcnow(),
                )
                embed.add_field(name="New PID", value=f"`{result.get('pid')}`")
                self._refresh_buttons("running")
            else:
                embed = discord.Embed(
                    title="❌ Failed to Restart",
                    description=f"```{result.get('error', 'Unknown error')}```",
                    color=0xef4444,
                )
            embed.set_footer(text="PyVegar")
            await interaction.message.edit(view=self)
            await interaction.followup.send(embed=embed, ephemeral=True)

        @discord.ui.button(label="View Logs", style=discord.ButtonStyle.primary, emoji="📋", row=0)
        async def logs_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
            await interaction.response.defer(ephemeral=True)
            log_lines = manager.get_log_lines(self.project_id, n=25)
            content = "\n".join(log_lines) if log_lines else "No logs yet."
            if len(content) > 3800:
                content = "…" + content[-3797:]
            embed = discord.Embed(
                title=f"📋 Logs — {self.project_name}",
                description=f"```\n{content}\n```",
                color=0x6366f1, timestamp=discord.utils.utcnow(),
            )
            embed.set_footer(text="Last 25 lines · PyVegar")
            await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /projects ─────────────────────────────────────────────────────────────

    @tree.command(name="projects", description="List all servers and their current status")
    async def cmd_projects(interaction: discord.Interaction):
        if not _allowed(interaction):
            await _deny(interaction); return
        await interaction.response.defer()
        projects = manager.list_projects()
        if not projects:
            embed = discord.Embed(
                title="⚡ PyVegar — Servers",
                description="No servers found. Create one in the web panel.",
                color=0x6366f1,
            )
            await interaction.followup.send(embed=embed); return

        running = sum(1 for p in projects if p["status"] == "running")
        embed = discord.Embed(
            title="⚡ PyVegar — Servers",
            description=f"**{running}** running · **{len(projects) - running}** stopped · **{len(projects)}** total",
            color=0x6366f1,
            timestamp=discord.utils.utcnow(),
        )
        for p in projects:
            emoji = _status_emoji(p["status"])
            uptime = _fmt_uptime(p.get("uptime"))
            pid = p.get("pid") or "—"
            cmd = p.get("startup_command") or f"python3 {p.get('start_file', 'main.py')}"
            if len(cmd) > 45:
                cmd = cmd[:42] + "…"
            embed.add_field(
                name=f"{emoji}  {p['name']}",
                value=(
                    f"**Status** `{p['status']}`  **PID** `{pid}`\n"
                    f"**Uptime** `{uptime}`  **Restarts** `{p.get('restarts', 0)}`\n"
                    f"**Cmd** `{cmd}`"
                ),
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

        embed = discord.Embed(
            title=f"{_status_emoji(p['status'])}  {p['name']}",
            color=_status_color(p["status"]),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Status",   value=f"`{p['status']}`",                  inline=True)
        embed.add_field(name="PID",      value=f"`{p.get('pid') or '—'}`",          inline=True)
        embed.add_field(name="Uptime",   value=f"`{_fmt_uptime(p.get('uptime'))}`", inline=True)
        embed.add_field(name="Restarts", value=f"`{p.get('restarts', 0)}`",         inline=True)
        embed.add_field(name="Start File", value=f"`{p.get('start_file', '—')}`",  inline=True)
        embed.add_field(name="Files",    value=f"`{len(p.get('files', []))}`",      inline=True)
        if p.get("startup_command"):
            embed.add_field(name="Startup Command", value=f"```{p['startup_command'][:120]}```", inline=False)
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
            embed = discord.Embed(
                title="✅ Server Started",
                description=f"**{_project_label(proj)}** is now running.",
                color=0x22c55e, timestamp=discord.utils.utcnow(),
            )
            embed.add_field(name="PID", value=f"`{result.get('pid')}`", inline=True)
        else:
            embed = discord.Embed(
                title="❌ Failed to Start",
                description=f"```{result.get('error', 'Unknown error')}```",
                color=0xef4444,
            )
        embed.set_footer(text="PyVegar")
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
            embed = discord.Embed(
                title="⏹️ Server Stopped",
                description=f"**{_project_label(proj)}** has been stopped.",
                color=0xef4444, timestamp=discord.utils.utcnow(),
            )
        else:
            embed = discord.Embed(
                title="❌ Failed to Stop",
                description=f"```{result.get('error', 'Unknown error')}```",
                color=0x6366f1,
            )
        embed.set_footer(text="PyVegar")
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
            embed = discord.Embed(
                title="🔄 Server Restarted",
                description=f"**{_project_label(proj)}** has been restarted.",
                color=0xf59e0b, timestamp=discord.utils.utcnow(),
            )
            embed.add_field(name="New PID", value=f"`{result.get('pid')}`", inline=True)
        else:
            embed = discord.Embed(
                title="❌ Failed to Restart",
                description=f"```{result.get('error', 'Unknown error')}```",
                color=0xef4444,
            )
        embed.set_footer(text="PyVegar")
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
        embed = discord.Embed(
            title="🔄 Restart All",
            color=0xf59e0b, timestamp=discord.utils.utcnow(),
        )
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
        if len(content) > 3800:
            content = "…(trimmed)\n" + content[-3787:]
        embed = discord.Embed(
            title=f"📋 Logs — {_project_label(proj)}",
            description=f"```\n{content}\n```",
            color=0x6366f1, timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text=f"Last {lines} lines · Status: {proj['status']} · PyVegar")
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

        embed = discord.Embed(
            title="⚡ System Overview",
            color=color, timestamp=discord.utils.utcnow(),
        )
        embed.add_field(
            name="🖥️ CPU",
            value=f"```{_bar(cpu)}```",
            inline=False,
        )
        embed.add_field(
            name="💾 RAM",
            value=f"```{_bar(mem.percent)}  {mem.used // 1024**2} MB / {mem.total // 1024**2} MB```",
            inline=False,
        )
        embed.add_field(
            name="💿 Disk",
            value=f"```{_bar(disk.percent)}  {disk.used / 1024**3:.1f} GB / {disk.total / 1024**3:.1f} GB```",
            inline=False,
        )
        tun_emoji = "🟢" if tunnel_st.get("status") == "running" else "🔴"
        embed.add_field(name="🖧 Servers",  value=f"`{running}/{total}` running",                            inline=True)
        embed.add_field(name="🌐 Tunnel",   value=f"{tun_emoji} `{tunnel_st.get('status', 'stopped')}`",   inline=True)
        embed.add_field(name="🔢 CPU Cores", value=f"`{psutil.cpu_count()}`",                               inline=True)
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
        embed = discord.Embed(
            title=f"📁 Files — {_project_label(proj)}",
            description=desc,
            color=_status_color(proj["status"]),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_footer(text=f"PyVegar · {len(flat_files)} file{'s' if len(flat_files) != 1 else ''}")
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
                    embed = discord.Embed(
                        title="✅ File Saved",
                        description=f"`{filename}` in **{_project_label(proj)}** has been updated.",
                        color=0x22c55e, timestamp=discord.utils.utcnow(),
                    )
                    embed.add_field(name="Size", value=f"`{len(self_m.file_content.value)} chars`", inline=True)
                    embed.set_footer(text="PyVegar")
                    await inter.response.send_message(embed=embed, ephemeral=True)
                else:
                    embed = discord.Embed(title="❌ Save Error", description=result.get("error", "Unknown error"), color=0xef4444)
                    await inter.response.send_message(embed=embed, ephemeral=True)

        await interaction.response.send_modal(EditModal())

    # ── /help ─────────────────────────────────────────────────────────────────

    @tree.command(name="help", description="Show all available PyVegar commands")
    async def cmd_help(interaction: discord.Interaction):
        embed = discord.Embed(
            title="⚡ PyVegar — Help",
            description="Remote control your server panel from Discord.",
            color=0x6366f1, timestamp=discord.utils.utcnow(),
        )
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

    @client.event
    async def on_ready():
        global _bot_running
        await tree.sync()
        logger.info(f"Discord bot logged in as {client.user} — slash commands synced")
        _bot_running = True

    @client.event
    async def on_disconnect():
        global _bot_running
        _bot_running = False

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
