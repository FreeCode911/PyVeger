import logging
import re
import sys
from datetime import datetime

RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"

# Foreground colours
BLACK   = "\033[30m"
RED     = "\033[31m"
GREEN   = "\033[32m"
YELLOW  = "\033[33m"
BLUE    = "\033[34m"
MAGENTA = "\033[35m"
CYAN    = "\033[36m"
WHITE   = "\033[37m"
GREY    = "\033[90m"

BANNER = f"""{BOLD}{RED}
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    ██████╗ ██╗   ██╗██╗   ██╗███████╗ ██████╗  █████╗ ██████╗ 
    ██╔══██╗╚██╗ ██╔╝╚██╗ ██╔╝██╔════╝██╔════╝ ██╔══██╗██╔══██╗
    ██████╔╝ ╚████╔╝  ╚████╔╝ █████╗  ██║  ███╗███████║██████╔╝
    ██╔═══╝   ╚██╔╝    ╚██╔╝  ██╔══╝  ██║   ██║██╔══██║██╔══██╗
    ██║        ██║      ██║   ███████╗╚██████╔╝██║  ██║██║  ██║
    ╚═╝        ╚═╝      ╚═╝   ╚══════╝ ╚═════╝ ╚═╝  ╚═╝╚═╝  ╚═╝
{RESET}{DIM}    Server Management Panel  ·  Python · FastAPI · SQLite{RESET}{BOLD}{RED}
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
{RESET}"""

_banner_printed = False

_LEVEL_FMT = {
    "DEBUG":    f"{DIM}{BLUE}  ◌ DEBUG {RESET}",
    "INFO":     f"{BOLD}{BLUE}  ● INFO  {RESET}",
    "WARNING":  f"{BOLD}{YELLOW}  ▲ WARN  {RESET}",
    "ERROR":    f"{BOLD}{RED}  ✖ ERROR {RESET}",
    "CRITICAL": f"{BOLD}{RED}  ✖✖ CRIT {RESET}",
}

_METHOD_COLOURS = {
    "GET":     GREEN,
    "POST":    CYAN,
    "PUT":     YELLOW,
    "PATCH":   YELLOW,
    "DELETE":  RED,
    "HEAD":    GREY,
    "OPTIONS": GREY,
}

_STATUS_COLOUR = {
    "1": CYAN,
    "2": GREEN,
    "3": BLUE,
    "4": YELLOW,
    "5": RED,
}

# Matches uvicorn access log lines:
# 172.31.x.x:PORT - "METHOD /path HTTP/1.1" STATUS_CODE ...
_ACCESS_RE = re.compile(
    r'(?P<ip>[\d\.]+):\d+ - "(?P<method>[A-Z]+) (?P<path>\S+) HTTP/[\d\.]+" (?P<status>\d{3})'
)
# WebSocket lines
_WS_RE = re.compile(
    r'(?P<ip>[\d\.]+):\d+ - "WebSocket (?P<path>\S+)"'
)


def _fmt_access(msg: str) -> str | None:
    m = _ACCESS_RE.search(msg)
    if m:
        method  = m.group("method")
        path    = m.group("path")
        status  = m.group("status")
        mc = _METHOD_COLOURS.get(method, WHITE)
        sc = _STATUS_COLOUR.get(status[0], WHITE)
        return (
            f"{GREY}http      {RESET} "
            f"{mc}{BOLD}{method:<7}{RESET} "
            f"{WHITE}{path:<40}{RESET} "
            f"→  {sc}{BOLD}{status}{RESET}"
        )
    return None


def _fmt_ws(msg: str) -> str | None:
    m = _WS_RE.search(msg)
    if m:
        path = m.group("path")
        if "[accepted]" in msg:
            state = f"{GREEN}OPEN{RESET}"
        elif "rejected" in msg.lower() or "403" in msg:
            state = f"{RED}REJECTED{RESET}"
        else:
            state = f"{GREY}CONNECT{RESET}"
        return (
            f"{GREY}uvicorn   {RESET} "
            f"{MAGENTA}WS{RESET}       "
            f"{WHITE}{path:<40}{RESET} "
            f"→  {state}"
        )
    return None


class PyVegarFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ts    = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        level = _LEVEL_FMT.get(record.levelname, f"  {record.levelname:<8}")
        display_name = "uvicorn" if record.name == "uvicorn.error" else record.name
        name  = f"{GREY}{display_name:<12}{RESET}"
        msg   = record.getMessage()

        # Special formatting for uvicorn access/ws lines
        if record.name in ("uvicorn.access", "uvicorn"):
            ws_fmt = _fmt_ws(msg)
            if ws_fmt:
                return f"  {GREY}{ts}{RESET}  {level} {ws_fmt}"
            acc_fmt = _fmt_access(msg)
            if acc_fmt:
                return f"  {GREY}{ts}{RESET}  {level} {acc_fmt}"

        # Startup lifecycle lines
        if "Started server process" in msg:
            pid = re.search(r'\[(\d+)\]', msg)
            pid_str = pid.group(1) if pid else "?"
            return f"  {GREY}{ts}{RESET}  {level} {name} {GREEN}▶ Started{RESET}   PID {BOLD}{pid_str}{RESET}"
        if "Waiting for application startup" in msg:
            return f"  {GREY}{ts}{RESET}  {level} {name} Waiting for startup…"
        if "Application startup complete" in msg:
            return f"  {GREY}{ts}{RESET}  {level} {name} {GREEN}✔ Ready{RESET}     application startup complete"
        if "Uvicorn running on" in msg:
            addr = re.search(r'http://\S+', msg)
            addr_str = addr.group(0) if addr else msg
            return f"  {GREY}{ts}{RESET}  {level} {name} {CYAN}◉ Listening{RESET}  {addr_str}"
        if "Shutting down" in msg or "shutdown" in msg.lower():
            return f"  {GREY}{ts}{RESET}  {level} {name} {YELLOW}◎ Shutdown{RESET}"

        # WebSocket events (non-access lines)
        if "WebSocket" in msg and "rejected" in msg.lower():
            return f"  {GREY}{ts}{RESET}  {level} {GREY}uvicorn   {RESET} WebSocket rejected"
        if "WebSocket" in msg and "closed" in msg.lower():
            return f"  {GREY}{ts}{RESET}  {level} {GREY}uvicorn   {RESET} WebSocket closed"

        return f"  {GREY}{ts}{RESET}  {level} {name} {msg}"


def setup_logging():
    global _banner_printed
    if not _banner_printed:
        print(BANNER, flush=True)
        _banner_printed = True

    formatter = PyVegarFormatter()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    for name in (
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "fastapi",
        "app",
        "manager",
        "tunnel",
        "discord_bot",
    ):
        log = logging.getLogger(name)
        log.handlers = [handler]
        log.propagate = False

    logging.getLogger().setLevel(logging.INFO)
