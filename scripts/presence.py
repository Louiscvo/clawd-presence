import glob
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import time
import uuid
from datetime import datetime, timedelta

IMAGE_URL = "https://raw.githubusercontent.com/Louiscvo/clawd-presence/main/images/{}.gif"
DEFAULT_CLIENT_ID = "1506443909406920948"
POLL_SECONDS = 4
REFRESH_SECONDS = 60
IDLE_SECONDS = 15 * 60
ORPHAN_SECONDS = 30 * 60
EXIT_AFTER_SECONDS = 10 * 60
SUMMARY_TIMEOUT = 90
AGENT_SECONDS = 5 * 60
FILE_AGENT_SECONDS = 90
STALE_AGENT_SECONDS = 30 * 60
PROMPT_REFRESH_SECONDS = 30
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")

LABELS = {
    "fr": {
        "thinking": "Réfléchit", "code": "Écrit du code", "commands": "Lance des commandes",
        "reading": "Lit le code", "web": "Cherche sur internet", "browser": "Navigue sur le web",
        "computer": "Utilise l'ordinateur", "agents": "Dirige des assistants", "planning": "Planifie",
        "tools": "Utilise un outil", "working": "Travaille", "permission": "Attend une permission",
        "question": "Attend une réponse", "done": "A fini, attend la suite", "error": "Bloqué sur une erreur",
        "ready": "Session ouverte", "idle": "En pause",
        "sessions": "{} sessions : {}", "helper": "{} assistant", "helpers": "{} assistants", "tokens": "{} tokens",
        "out": "Plus de tokens", "back": "Reprise à {when}", "back_week": "Reprise le {when}",
    },
    "en": {
        "thinking": "Thinking", "code": "Writing code", "commands": "Running commands",
        "reading": "Reading code", "web": "Searching the web", "browser": "Browsing the web",
        "computer": "Using the computer", "agents": "Directing helpers", "planning": "Planning",
        "tools": "Using a tool", "working": "Working", "permission": "Waiting for permission",
        "question": "Waiting for an answer", "done": "Done, waiting for more", "error": "Stopped on an error",
        "ready": "Session open", "idle": "Idle",
        "sessions": "{} sessions: {}", "helper": "{} helper", "helpers": "{} helpers", "tokens": "{} tokens",
        "out": "Out of tokens", "back": "Back at {when}", "back_week": "Back on {when}",
    },
}
SUMMARY_PROMPTS = {
    "fr": "Tu reçois les derniers messages qu'un développeur a envoyés à son assistant de code. "
          "Donne en français un titre très court (3 à 7 mots, 40 caractères maximum) qui dit sur quoi il travaille, "
          "sans guillemets, sans point final, sans nom de fichier. Réponds uniquement par le titre.",
    "en": "You receive the latest messages a developer sent to their coding assistant. "
          "Give a very short English title (3 to 7 words, 40 characters max) saying what they are working on, "
          "no quotes, no final period, no file names. Reply with the title only.",
}
IMAGES = {
    "agents": {"agents"},
    "working-building": {"code", "commands", "working"},
    "notification": {"permission", "question", "done", "error"},
    "sleeping": {"ready", "idle"},
}
RESET_PATTERN = re.compile(
    r"resets\s+(?:(?P<month>[A-Za-z]{3,9})\s+(?P<day>\d{1,2})\s+at\s+)?(?P<hour>\d{1,2})(?::(?P<minute>\d{2}))?\s*"
    r"(?P<ampm>am|pm)(?:\s*\((?P<zone>[^)]+)\))?", re.IGNORECASE)
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
PRIORITY = {"waiting": 3, "working": 2, "error": 1, "done": 1, "ready": 0}

summaries = {}
pending = {}
checked_pids = {}
launch_times = {}
finished_agents = set()
prompt_cache = {}
token_files = {}
transcripts = {}


def default_data_dir():
    return os.environ.get("CLAUDE_PLUGIN_DATA") or os.path.join(os.path.expanduser("~"), ".claude", "clawd-presence")


def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as file:
            return json.load(file)
    except (OSError, ValueError):
        return default


def is_claude(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    if pid not in checked_pids:
        try:
            out = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=2).stdout
        except (OSError, subprocess.SubprocessError):
            return True
        checked_pids[pid] = "claude" in out.lower()
    return checked_pids[pid]


def launch_time(pid):
    if pid not in launch_times:
        try:
            out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True, timeout=2,
                                 env=dict(os.environ, LC_ALL="C")).stdout.strip()
            launch_times[pid] = time.mktime(time.strptime(out, "%a %b %d %H:%M:%S %Y"))
        except (OSError, ValueError, subprocess.SubprocessError):
            launch_times[pid] = None
    return launch_times[pid]


def live_sessions(folder, now):
    sessions = []
    registry = registry_sessions()
    directory = os.path.join(folder, "sessions")
    try:
        names = os.listdir(directory)
    except OSError:
        names = []
    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(directory, name)
        record = read_json(path, None)
        if not isinstance(record, dict):
            continue
        pid = record.get("pid")
        if isinstance(pid, int) and pid > 1 and os.name != "nt":
            alive = is_claude(pid)
            record["started"] = launch_time(pid) or record.get("started")
        else:
            alive = now - record.get("t", 0) < ORPHAN_SECONDS
        if alive:
            sessions.append(record)
        else:
            try:
                os.remove(path)
            except OSError:
                pass
    known = {record.get("id") for record in sessions}
    for session, entry in registry.items():
        if session in known:
            continue
        busy = entry.get("status") == "busy"
        sessions.append({
            "id": session,
            "pid": entry.get("pid"),
            "state": "working" if busy else "done",
            "action": "working" if busy else "done",
            "t": (entry.get("statusUpdatedAt") or entry.get("updatedAt") or now * 1000) / 1000,
        })
    for record in sessions:
        entry = registry.get(record.get("id"))
        if entry and entry.get("startedAt"):
            record["started"] = entry["startedAt"] / 1000
        if not record.get("prompts"):
            record["prompts"] = transcript_prompts(record, now)
    return sessions


def find_claude():
    found = shutil.which("claude")
    if found:
        return found
    for name in ("claude.exe", "claude"):
        path = os.path.join(os.path.expanduser("~"), ".local", "bin", name)
        if os.path.exists(path):
            return path
    return None


def request_summary(record, language):
    session = record.get("id")
    prompts = record.get("prompts") or []
    key = (language, tuple(prompts))
    if not session or not prompts or session in pending or summaries.get(session, (None,))[0] == key:
        return
    claude = find_claude()
    if not claude:
        summaries[session] = (key, None)
        return
    message = "\n\n".join(f"Message {index} : {text}" for index, text in enumerate(prompts, 1))
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    flags = 0x08000000 if os.name == "nt" else 0
    try:
        process = subprocess.Popen(
            [claude, "-p", "--model", "haiku", "--no-session-persistence", "--strict-mcp-config",
             "--tools", "", "--disable-slash-commands", "--settings", '{"disableAllHooks":true}',
             "--system-prompt", SUMMARY_PROMPTS[language]],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, creationflags=flags,
        )
        process.stdin.write(message.encode("utf-8"))
        process.stdin.close()
    except OSError:
        summaries[session] = (key, None)
        return
    pending[session] = (key, process, time.time())


def collect_summaries():
    for session, (key, process, started) in list(pending.items()):
        if process.poll() is None:
            if time.time() - started > SUMMARY_TIMEOUT:
                process.kill()
                process.wait()
                del pending[session]
            continue
        output = process.stdout.read().decode("utf-8", "ignore").strip()
        lines = [line for line in output.splitlines() if line.strip()]
        title = lines[-1].strip(" \"'«».") if process.returncode == 0 and lines else ""
        previous = summaries.get(session, (None, None))[1]
        summaries[session] = (key, title[:128] if len(title) >= 2 else previous)
        del pending[session]


def image_for(action):
    for image, actions in IMAGES.items():
        if action in actions:
            return IMAGE_URL.format(image)
    return IMAGE_URL.format("working-typing")


def agents_folder(record):
    transcript = transcript_of(record)
    return os.path.join(os.path.splitext(transcript)[0], "subagents") if transcript else None


def active_agents(record, now):
    if "_agents" in record:
        return record["_agents"]
    seen = record.get("agents") or {}
    running = {a for a, t in seen.items() if now - t < AGENT_SECONDS} if isinstance(seen, dict) else set(seen)
    folder = agents_folder(record)
    try:
        names = set(os.listdir(folder)) if folder else set()
    except OSError:
        names = set()
    for name in names:
        if not name.startswith("agent-") or not name.endswith(".jsonl"):
            continue
        agent = name[6:-6]
        meta = f"agent-{agent}.meta.json"
        try:
            age = now - os.path.getmtime(os.path.join(folder, name))
        except OSError:
            continue
        if meta in names:
            if agent not in finished_agents and read_json(os.path.join(folder, meta), {}).get("finished"):
                finished_agents.add(agent)
            if agent not in finished_agents and age < STALE_AGENT_SECONDS:
                running.add(agent)
        elif age < FILE_AGENT_SECONDS:
            running.add(agent)
    record["_agents"] = len(running - finished_agents - set(record.get("ended") or []))
    return record["_agents"]


def prompt_text(line):
    if b'"type":"user"' not in line or b'"tool_use_id"' in line:
        return None
    try:
        entry = json.loads(line)
    except ValueError:
        return None
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isSidechain") or entry.get("isCompactSummary"):
        return None
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, list):
        content = " ".join(part.get("text", "") for part in content if isinstance(part, dict) and part.get("type") == "text")
    if not isinstance(content, str):
        return None
    text = " ".join(content.split())
    if not text or text.startswith(("<", "[Request interrupted", "This session is being continued")):
        return None
    return text[:1500]


def transcript_prompts(record, now):
    path = transcript_of(record)
    if not path:
        return []
    cached = prompt_cache.get(path)
    if cached and now - cached[0] < PROMPT_REFRESH_SECONDS:
        return cached[1]
    prompts = []
    try:
        size = os.path.getsize(path)
        window = 4 << 20
        while True:
            with open(path, "rb") as file:
                file.seek(max(0, size - window))
                lines = file.read().split(b"\n")
            if size > window:
                lines = lines[1:]
            prompts = [text for text in map(prompt_text, lines) if text][-2:]
            if len(prompts) >= 2 or window >= size:
                break
            window *= 4
    except OSError:
        pass
    prompt_cache[path] = (now, prompts)
    return prompts


def pid_alive(pid):
    if os.name == "nt":
        import ctypes
        kernel = ctypes.windll.kernel32
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        code = ctypes.c_ulong()
        ok = kernel.GetExitCodeProcess(handle, ctypes.byref(code))
        kernel.CloseHandle(handle)
        return bool(ok) and code.value == 259
    return is_claude(pid)


def registry_sessions():
    root = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    found = {}
    for path in glob.glob(os.path.join(root, "sessions", "*.json")):
        entry = read_json(path, None)
        if not isinstance(entry, dict) or entry.get("kind", "interactive") != "interactive":
            continue
        pid, session = entry.get("pid"), entry.get("sessionId")
        if isinstance(pid, int) and session and pid_alive(pid):
            found[session] = entry
    return found


def action_of(record, now):
    if record.get("state") in ("done", "ready") and active_agents(record, now):
        return "agents"
    if record.get("state") in ("done", "ready") and now - record.get("t", 0) > IDLE_SECONDS:
        return "idle"
    return record.get("action") or "working"


def shorten(text, limit=128):
    return text if len(text) <= limit else text[:limit - 1] + "…"


def compact(number, language):
    for limit, suffix in ((1e9, "G"), (1e6, "M"), (1e3, "k")):
        if number >= limit:
            text = f"{number / limit:.1f}".rstrip("0").rstrip(".")
            return f"{text.replace('.', ',')} {suffix}" if language == "fr" else f"{text}{suffix}"
    return str(number)


def entry_time(entry):
    try:
        return datetime.fromisoformat(str(entry.get("timestamp")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0


def message_text(message):
    content = message.get("content")
    if isinstance(content, list):
        return " ".join(part.get("text", "") for part in content if isinstance(part, dict))
    return content if isinstance(content, str) else ""


def parse_reset(text, hit_at):
    match = RESET_PATTERN.search(text)
    if not match:
        return None
    zone = None
    if match.group("zone"):
        try:
            from zoneinfo import ZoneInfo
            zone = ZoneInfo(match.group("zone"))
        except Exception:
            zone = None
    base = datetime.fromtimestamp(hit_at, zone) if zone else datetime.fromtimestamp(hit_at)
    hour = int(match.group("hour")) % 12 + (12 if match.group("ampm").lower() == "pm" else 0)
    minute = int(match.group("minute") or 0)
    if match.group("month"):
        month = MONTHS.index(match.group("month")[:3].lower()) + 1
        reset = base.replace(month=month, day=int(match.group("day")), hour=hour, minute=minute, second=0, microsecond=0)
        if reset < base - timedelta(days=1):
            reset = reset.replace(year=reset.year + 1)
    else:
        reset = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if reset <= base:
            reset += timedelta(days=1)
    return reset.timestamp()


def scan_file(path):
    state = token_files.setdefault(path, {"offset": 0, "seen": set(), "entries": [], "ok": 0, "limit": None})
    try:
        size = os.path.getsize(path)
    except OSError:
        return state
    if size < state["offset"]:
        state.update(offset=0, seen=set(), entries=[], ok=0, limit=None)
    if size > state["offset"]:
        with open(path, "rb") as file:
            file.seek(state["offset"])
            chunk = file.read(size - state["offset"])
        end = chunk.rfind(b"\n") + 1
        for line in chunk[:end].split(b"\n"):
            if b'"assistant"' not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if entry.get("type") != "assistant":
                continue
            message = entry.get("message") or {}
            when = entry_time(entry)
            if entry.get("isApiErrorMessage"):
                if entry.get("error") == "rate_limit":
                    text = message_text(message)
                    state["limit"] = (when, parse_reset(text, when), "weekly" in text)
                continue
            state["ok"] = max(state["ok"], when)
            usage = message.get("usage")
            key = (message.get("id"), entry.get("requestId"))
            if not isinstance(usage, dict) or key in state["seen"]:
                continue
            state["seen"].add(key)
            state["entries"].append((when, sum(usage.get(f) for f in TOKEN_FIELDS if isinstance(usage.get(f), int))))
        state["offset"] += end
    return state


def transcript_of(record):
    if record.get("transcript"):
        return record["transcript"]
    session = record.get("id")
    if session not in transcripts:
        root = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
        found = glob.glob(os.path.join(root, "projects", "*", f"{session}.jsonl"))
        transcripts[session] = found[0] if found else None
    return transcripts[session]


def session_files(record):
    transcript = transcript_of(record)
    if not transcript:
        return []
    files = [scan_file(transcript)]
    agents = agents_folder(record)
    try:
        names = os.listdir(agents)
    except OSError:
        names = []
    files += [scan_file(os.path.join(agents, name)) for name in names if name.endswith(".jsonl")]
    return files


def usage_window(files, now):
    limits = [f["limit"] for f in files if f["limit"] and f["limit"][1]]
    last_ok = max((f["ok"] for f in files), default=0)
    if not limits:
        return None, 0
    hit_at, reset, weekly = max(limits)
    if reset > now and last_ok <= hit_at:
        return (reset, weekly), 0
    return None, reset if reset <= now else 0


def build_activity(folder, options):
    now = time.time()
    sessions = live_sessions(folder, now)
    ids = {record.get("id") for record in sessions}
    for session in [s for s in summaries if s not in ids]:
        del summaries[session]
    if not sessions:
        return None, 0
    language = options.get("language") if options.get("language") in LABELS else "en"
    labels = LABELS[language]
    ordered = sorted(sessions, key=lambda r: (PRIORITY.get(r.get("state"), 0), r.get("t") or 0), reverse=True)
    actions = [action_of(r, now) for r in ordered]
    record = ordered[0]
    texts = {}
    if options.get("summary", True):
        collect_summaries()
        for r in ordered:
            request_summary(r, language)
            texts[r.get("id")] = summaries.get(r.get("id"), (None, None))[1]
    summary = texts.get(record.get("id"))
    names = [labels.get(a, labels["working"]) for a in actions]
    parts = []
    if len(ordered) > 1:
        parts.append(labels["sessions"].format(len(ordered), ", ".join(names)))
    elif summary:
        parts.append(names[0])
    files = [f for r in ordered for f in session_files(r)]
    blocked, restart = usage_window(files, now)
    hover = " · ".join(t for t in (texts.get(r.get("id")) for r in ordered) if t)
    if blocked:
        reset, weekly = blocked
        return {
            "details": labels["out"],
            "state": labels["back_week" if weekly else "back"].format(when=reset_label(reset, language, weekly)),
            "assets": {"large_image": IMAGE_URL.format("out"), "large_text": shorten(hover or "Claude Code")},
            "timestamps": {"end": int(reset)},
        }, len(sessions)
    tokens = sum(t for f in files for when, t in f["entries"] if when >= restart)
    if tokens:
        parts.append(labels["tokens"].format(compact(tokens, language)))
    activity = {
        "details": (summary or names[0])[:128],
        "assets": {"large_image": image_for(actions[0]), "large_text": shorten(hover or "Claude Code")},
    }
    if parts:
        activity["state"] = shorten(" · ".join(parts))
    started = max(min(r.get("started") or r.get("t") or now for r in sessions), restart)
    activity["timestamps"] = {"start": int(started)}
    return activity, len(sessions)


def reset_label(reset, language, weekly):
    moment = datetime.fromtimestamp(reset)
    if language == "fr":
        hour = f"{moment.hour}h{moment.minute:02d}" if moment.minute else f"{moment.hour}h"
        return f"{moment.day}/{moment.month} à {hour}" if weekly else hour
    hour = moment.strftime("%I:%M %p").lstrip("0").replace(":00 ", " ").lower()
    return f"{moment.strftime('%b')} {moment.day} at {hour}" if weekly else hour


def socket_paths():
    folders = [os.environ.get(name) for name in ("XDG_RUNTIME_DIR", "TMPDIR", "TMP", "TEMP")]
    folders.append(f"/run/user/{os.getuid()}")
    if sys.platform == "darwin":
        try:
            folders.append(subprocess.check_output(["getconf", "DARWIN_USER_TEMP_DIR"], text=True).strip())
        except (OSError, subprocess.SubprocessError):
            pass
    folders.append("/tmp")
    for folder in dict.fromkeys(f for f in folders if f):
        for sub in ("", "app/com.discordapp.Discord", "snap.discord", ".flatpak/com.discordapp.Discord/xdg-run"):
            for index in range(10):
                yield os.path.join(folder, sub, f"discord-ipc-{index}")


class Discord:
    def __init__(self, client_id):
        self.pipe = None
        self.sock = None
        if os.name == "nt":
            for index in range(10):
                try:
                    self.pipe = open(rf"\\.\pipe\discord-ipc-{index}", "r+b", buffering=0)
                    break
                except OSError:
                    continue
        else:
            for path in socket_paths():
                if not os.path.exists(path):
                    continue
                sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                sock.settimeout(10)
                try:
                    sock.connect(path)
                    self.sock = sock
                    break
                except OSError:
                    sock.close()
        if not self.pipe and not self.sock:
            raise OSError("Discord is not running")
        self.send(0, {"v": 1, "client_id": client_id})

    def write(self, data):
        if self.pipe:
            self.pipe.write(data)
        else:
            self.sock.sendall(data)

    def read(self, size):
        data = b""
        while len(data) < size:
            chunk = self.pipe.read(size - len(data)) if self.pipe else self.sock.recv(size - len(data))
            if not chunk:
                raise OSError("Discord closed the connection")
            data += chunk
        return data

    def send(self, op, payload):
        data = json.dumps(payload).encode()
        self.write(struct.pack("<II", op, len(data)) + data)
        _, length = struct.unpack("<II", self.read(8))
        reply = json.loads(self.read(length))
        if reply.get("evt") == "ERROR":
            raise OSError(reply.get("data"))
        return reply

    def set_activity(self, activity):
        args = {"pid": os.getpid()}
        if activity:
            args["activity"] = activity
        self.send(1, {"cmd": "SET_ACTIVITY", "args": args, "nonce": str(uuid.uuid4())})

    def close(self):
        (self.pipe or self.sock).close()


def acquire_lock(folder):
    handle = open(os.path.join(folder, "daemon.lock"), "a+")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def log(folder, message):
    with open(os.path.join(folder, "presence.log"), "a", encoding="utf-8") as file:
        file.write(time.strftime("%Y-%m-%d %H:%M:%S ") + message + "\n")


def run(folder):
    client = None
    client_id = None
    shown = None
    last_refresh = 0
    empty_since = None
    last_error = None
    while True:
        options = read_json(os.path.join(folder, "options.json"), {})
        activity, count = build_activity(folder, options)
        now = time.time()
        if count:
            empty_since = None
        elif empty_since is None:
            empty_since = now
        elif now - empty_since > EXIT_AFTER_SECONDS:
            if client:
                try:
                    client.set_activity(None)
                except (OSError, ValueError):
                    pass
            return
        wanted_id = options.get("client_id") or DEFAULT_CLIENT_ID
        if client and wanted_id != client_id:
            client.close()
            client = None
            shown = None
        if activity != shown or (activity and now - last_refresh > REFRESH_SECONDS):
            try:
                if not client:
                    client = Discord(wanted_id)
                    client_id = wanted_id
                client.set_activity(activity)
                shown = activity
                last_refresh = now
                last_error = None
            except (OSError, ValueError) as error:
                if str(error) != last_error:
                    last_error = str(error)
                    log(folder, last_error)
                if client:
                    client.close()
                client = None
                shown = None
        time.sleep(POLL_SECONDS)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    folder = args[0] if args else default_data_dir()
    os.makedirs(os.path.join(folder, "sessions"), exist_ok=True)
    if "--print" in sys.argv:
        activity, _ = build_activity(folder, read_json(os.path.join(folder, "options.json"), {}))
        while pending:
            time.sleep(1)
            collect_summaries()
            activity, _ = build_activity(folder, read_json(os.path.join(folder, "options.json"), {}))
        print(json.dumps(activity, ensure_ascii=False, indent=2))
        return
    lock = acquire_lock(folder)
    if lock:
        run(folder)


if __name__ == "__main__":
    main()
