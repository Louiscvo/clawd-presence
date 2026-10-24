import json
import locale
import os
import re
import subprocess
import sys
import time

START = time.time()
DEFAULT_CLIENT_ID = "1506443909406920948"
AGENT_SECONDS = 5 * 60
WAITING_TOOLS = {"AskUserQuestion", "ExitPlanMode"}
TOOL_ACTIONS = {
    "Edit": "code", "MultiEdit": "code", "Write": "code", "NotebookEdit": "code",
    "Bash": "commands", "PowerShell": "commands",
    "Read": "reading", "NotebookRead": "reading", "Grep": "reading", "Glob": "reading", "LSP": "reading",
    "WebSearch": "web", "WebFetch": "web",
    "Agent": "agents", "Task": "agents", "Workflow": "agents",
    "TodoWrite": "planning", "TaskCreate": "planning", "TaskUpdate": "planning", "EnterPlanMode": "planning",
}


def data_dir():
    path = os.environ.get("CLAUDE_PLUGIN_DATA") or os.path.join(os.path.expanduser("~"), ".claude", "clawd-presence")
    os.makedirs(os.path.join(path, "sessions"), exist_ok=True)
    return path


def tool_action(tool):
    if tool in TOOL_ACTIONS:
        return TOOL_ACTIONS[tool]
    lowered = tool.lower()
    if "chrome" in lowered or "browser" in lowered or "playwright" in lowered:
        return "browser"
    if "computer" in lowered:
        return "computer"
    return "tools" if tool.startswith("mcp__") else "working"


def clean_prompt(text):
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\[Image #\d+\]", " ", text)
    return " ".join(text.split())[:1500]


def claude_pid():
    if os.name == "nt":
        return None
    pid = os.getppid()
    for _ in range(6):
        try:
            out = subprocess.run(["ps", "-o", "ppid=,command=", "-p", str(pid)],
                                 capture_output=True, text=True, timeout=2).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return None
        parts = out.split(None, 1)
        if len(parts) < 2:
            return None
        if "claude" in parts[1].lower() and "hook.py" not in parts[1]:
            return pid
        pid = int(parts[0])
        if pid <= 1:
            return None
    return None


def option(name, default):
    value = os.environ.get(f"CLAUDE_PLUGIN_OPTION_{name.upper()}")
    return default if value in (None, "") else value


def resolve_language(value):
    if value in ("fr", "en"):
        return value
    lang = os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES") or os.environ.get("LANG") or ""
    if not lang:
        try:
            lang = locale.getlocale()[0] or ""
        except ValueError:
            lang = ""
    return "fr" if lang.lower().startswith("fr") else "en"


def save_options(folder):
    options = {
        "summary": str(option("summary", "true")).lower() in ("1", "true", "yes", "on"),
        "language": resolve_language(option("language", "auto")),
        "client_id": option("client_id", DEFAULT_CLIENT_ID),
    }
    write_json(os.path.join(folder, "options.json"), options)


def write_json(path, data):
    temp = f"{path}.{os.getpid()}.tmp"
    with open(temp, "w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False)
    for _ in range(10):
        try:
            os.replace(temp, path)
            return
        except PermissionError:
            time.sleep(0.02)
    os.remove(temp)


class Lock:
    def __init__(self, path):
        self.fd = os.open(path, os.O_CREAT | os.O_RDWR)

    def __enter__(self):
        if os.name == "nt":
            import msvcrt
            for _ in range(300):
                try:
                    msvcrt.locking(self.fd, msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.01)
        else:
            import fcntl
            fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *_):
        if os.name == "nt":
            import msvcrt
            try:
                os.lseek(self.fd, 0, 0)
                msvcrt.locking(self.fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        os.close(self.fd)


def daemon_running(folder):
    try:
        handle = open(os.path.join(folder, "daemon.lock"), "a+")
    except OSError:
        return True
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        handle.close()


def start_daemon(folder):
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "presence.py")
    python = sys.executable
    options = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        windowless = os.path.join(os.path.dirname(python), "pythonw.exe")
        if os.path.exists(windowless):
            python = windowless
        options["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    subprocess.Popen([python, script, folder], **options)


def update(record, payload, event):
    agent = payload.get("agent_id")
    tool = payload.get("tool_name") or ""
    if agent:
        seen = record.get("agents")
        seen = {k: v for k, v in seen.items() if START - v < AGENT_SECONDS} if isinstance(seen, dict) else {}
        if event == "SubagentStop":
            seen.pop(agent, None)
        else:
            seen[agent] = START
        record["agents"] = seen
        if "state" not in record:
            record.update(state="working", action="agents", t=START)
            return True
        return False
    if START < record.get("t", 0):
        return False
    if event == "SessionStart":
        if payload.get("source") == "compact":
            return False
        record.update(state="ready", action="ready")
    elif event == "UserPromptSubmit":
        prompt = payload.get("prompt") or ""
        if not prompt.lstrip().startswith("<task-notification>"):
            text = clean_prompt(prompt)
            if text:
                record["prompts"] = (record.get("prompts", []) + [text])[-2:]
            record["turn_start"] = START
        record.update(state="working", action="thinking")
    elif event == "PreToolUse":
        if tool in WAITING_TOOLS:
            record.update(state="waiting", action="question")
        else:
            record.update(state="working", action=tool_action(tool))
    elif event in ("PostToolUse", "PostToolUseFailure"):
        record.update(state="working", action="thinking")
    elif event == "Notification":
        kind = payload.get("notification_type") or ""
        if kind == "permission_prompt":
            record.update(state="waiting", action="permission")
        elif kind in ("elicitation_dialog", "elicitation_url_dialog", "agent_needs_input"):
            record.update(state="waiting", action="question")
        else:
            return False
    elif event == "Stop":
        record.update(state="done", action="done")
    elif event == "StopFailure":
        record.update(state="error", action="error")
    else:
        return False
    record["t"] = START
    return True


def main():
    try:
        payload = json.load(sys.stdin)
    except ValueError:
        return
    session = str(payload.get("session_id") or "")
    event = payload.get("hook_event_name") or ""
    if not session or not all(c.isalnum() or c in "-_" for c in session):
        return
    folder = data_dir()
    path = os.path.join(folder, "sessions", f"{session}.json")
    with Lock(os.path.join(folder, "sessions.lock")):
        if event == "SessionEnd":
            try:
                os.remove(path)
            except OSError:
                pass
            return
        try:
            with open(path, encoding="utf-8") as file:
                record = json.load(file)
        except (OSError, ValueError):
            record = {"id": session}
        if "pid" not in record:
            record["pid"] = claude_pid()
        record.setdefault("started", START)
        record["cwd"] = payload.get("cwd") or record.get("cwd")
        if not payload.get("agent_id") and payload.get("transcript_path"):
            record["transcript"] = payload["transcript_path"]
        update(record, payload, event)
        record.setdefault("t", START)
        write_json(path, record)
    if event in ("SessionStart", "UserPromptSubmit") or not os.path.exists(os.path.join(folder, "options.json")):
        save_options(folder)
    if not daemon_running(folder):
        start_daemon(folder)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
