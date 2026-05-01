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


def _format_uptime(seconds: int | None) -> str:
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

    def _is_allowed(interaction: discord.Interaction) -> bool:
        cfg = _load_config()
        allowed = cfg.get("allowed_users", [])
        guild = interaction.guild
        uid = str(interaction.user.id)
        if guild and str(guild.owner_id) == uid:
            return True
        return uid in allowed

    @tree.command(name="scripts", description="List all scripts and their status")
    async def cmd_scripts(interaction: discord.Interaction):
        if not _is_allowed(interaction):
            await interaction.response.send_message("❌ Permission denied.", ephemeral=True)
            return
        scripts = manager.list_scripts()
        if not scripts:
            await interaction.response.send_message("No scripts found.")
            return
        lines = []
        for s in scripts:
            icon = "🟢" if s["status"] == "running" else "🔴"
            uptime = _format_uptime(s.get("uptime"))
            lines.append(f"{icon} **{s['name']}** | PID: {s['pid'] or 'N/A'} | Uptime: {uptime} | Restarts: {s['restarts']}")
        await interaction.response.send_message("\n".join(lines))

    @tree.command(name="start", description="Start a script")
    @app_commands.describe(script="Script filename (e.g. bot.py)")
    async def cmd_start(interaction: discord.Interaction, script: str):
        if not _is_allowed(interaction):
            await interaction.response.send_message("❌ Permission denied.", ephemeral=True)
            return
        result = manager.start_script(script)
        if result.get("ok"):
            await interaction.response.send_message(f"✅ Started **{script}** (PID: {result.get('pid')})")
        else:
            await interaction.response.send_message(f"❌ Error: {result.get('error')}")

    @tree.command(name="stop", description="Stop a script")
    @app_commands.describe(script="Script filename (e.g. bot.py)")
    async def cmd_stop(interaction: discord.Interaction, script: str):
        if not _is_allowed(interaction):
            await interaction.response.send_message("❌ Permission denied.", ephemeral=True)
            return
        result = manager.stop_script(script)
        if result.get("ok"):
            await interaction.response.send_message(f"⛔ Stopped **{script}**")
        else:
            await interaction.response.send_message(f"❌ Error: {result.get('error')}")

    @tree.command(name="restart", description="Restart a script")
    @app_commands.describe(script="Script filename (e.g. bot.py)")
    async def cmd_restart(interaction: discord.Interaction, script: str):
        if not _is_allowed(interaction):
            await interaction.response.send_message("❌ Permission denied.", ephemeral=True)
            return
        result = manager.restart_script(script)
        if result.get("ok"):
            await interaction.response.send_message(f"🔁 Restarted **{script}** (PID: {result.get('pid')})")
        else:
            await interaction.response.send_message(f"❌ Error: {result.get('error')}")

    @tree.command(name="logs", description="Show last 20 log lines for a script")
    @app_commands.describe(script="Script filename (e.g. bot.py)")
    async def cmd_logs(interaction: discord.Interaction, script: str):
        if not _is_allowed(interaction):
            await interaction.response.send_message("❌ Permission denied.", ephemeral=True)
            return
        lines = manager.get_log_lines(script, n=20)
        if not lines:
            await interaction.response.send_message(f"No logs for **{script}**.")
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
