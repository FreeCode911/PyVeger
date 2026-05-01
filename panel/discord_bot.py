import json
import threading
import logging
from pathlib import Path

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
    if seconds is None:
        return "N/A"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h}h {m}m {s}s"


def _run_bot(token: str):
    global _bot_running
    try:
        import discord
        from discord import app_commands
    except ImportError:
        logger.error("discord.py not installed")
        return

    import manager

    intents = discord.Intents.default()
    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)

    def _allowed(interaction: discord.Interaction) -> bool:
        cfg = _load_config()
        allowed = cfg.get("allowed_users", [])
        uid = str(interaction.user.id)
        guild = interaction.guild
        if guild and str(guild.owner_id) == uid:
            return True
        return uid in allowed

    @tree.command(name="projects", description="List all projects and their status")
    async def cmd_projects(interaction: discord.Interaction):
        if not _allowed(interaction):
            await interaction.response.send_message("❌ Permission denied.", ephemeral=True)
            return
        projects = manager.list_projects()
        if not projects:
            await interaction.response.send_message("No projects found.")
            return
        lines = []
        for p in projects:
            icon = "🟢" if p["status"] == "running" else "🔴"
            uptime = _fmt_uptime(p.get("uptime"))
            lines.append(f"{icon} **{p['name']}** | Start: `{p['start_file']}` | PID: {p['pid'] or 'N/A'} | Uptime: {uptime} | Restarts: {p['restarts']}")
        await interaction.response.send_message("\n".join(lines))

    @tree.command(name="start", description="Start a project")
    @app_commands.describe(project="Project name")
    async def cmd_start(interaction: discord.Interaction, project: str):
        if not _allowed(interaction):
            await interaction.response.send_message("❌ Permission denied.", ephemeral=True)
            return
        result = manager.start_project(project)
        if result.get("ok"):
            await interaction.response.send_message(f"✅ Started **{project}** (PID: {result.get('pid')})")
        else:
            await interaction.response.send_message(f"❌ Error: {result.get('error')}")

    @tree.command(name="stop", description="Stop a project")
    @app_commands.describe(project="Project name")
    async def cmd_stop(interaction: discord.Interaction, project: str):
        if not _allowed(interaction):
            await interaction.response.send_message("❌ Permission denied.", ephemeral=True)
            return
        result = manager.stop_project(project)
        if result.get("ok"):
            await interaction.response.send_message(f"⛔ Stopped **{project}**")
        else:
            await interaction.response.send_message(f"❌ Error: {result.get('error')}")

    @tree.command(name="restart", description="Restart a project")
    @app_commands.describe(project="Project name")
    async def cmd_restart(interaction: discord.Interaction, project: str):
        if not _allowed(interaction):
            await interaction.response.send_message("❌ Permission denied.", ephemeral=True)
            return
        result = manager.restart_project(project)
        if result.get("ok"):
            await interaction.response.send_message(f"🔁 Restarted **{project}** (PID: {result.get('pid')})")
        else:
            await interaction.response.send_message(f"❌ Error: {result.get('error')}")

    @tree.command(name="logs", description="Show last 20 log lines for a project")
    @app_commands.describe(project="Project name")
    async def cmd_logs(interaction: discord.Interaction, project: str):
        if not _allowed(interaction):
            await interaction.response.send_message("❌ Permission denied.", ephemeral=True)
            return
        lines = manager.get_log_lines(project, n=20)
        if not lines:
            await interaction.response.send_message(f"No logs for **{project}**.")
            return
        content = "\n".join(lines)
        if len(content) > 1900:
            content = content[-1900:]
        await interaction.response.send_message(f"```\n{content}\n```")

    @client.event
    async def on_ready():
        await tree.sync()
        logger.info(f"Discord bot logged in as {client.user}")
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
