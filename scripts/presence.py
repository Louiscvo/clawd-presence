import glob
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time
import uuid

IMAGE_URL = "https://raw.githubusercontent.com/Louiscvo/clawd-presence/main/images/{}.gif"
DEFAULT_CLIENT_ID = "1506443909406920948"
POLL_SECONDS = 4
REFRESH_SECONDS = 60
IDLE_SECONDS = 15 * 60
ORPHAN_SECONDS = 30 * 60
EXIT_AFTER_SECONDS = 10 * 60
SUMMARY_TIMEOUT = 90
AGENT_SECONDS = 5 * 60
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
    },
    "en": {
        "thinking": "Thinking", "code": "Writing code", "commands": "Running commands",
        "reading": "Reading code", "web": "Searching the web", "browser": "Browsing the web",
        "computer": "Using the computer", "agents": "Directing helpers", "planning": "Planning",
        "tools": "Using a tool", "working": "Working", "permission": "Waiting for permission",
        "question": "Waiting for an answer", "done": "Done, waiting for more", "error": "Stopped on an error",
        "ready": "Session open", "idle": "Idle",
        "sessions": "{} sessions: {}", "helper": "{} helper", "helpers": "{} helpers", "tokens": "{} tokens",
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
    "working-building": {"code", "commands", "agents", "working"},
    "notification": {"permission", "question", "done", "error"},
    "sleeping": {"ready", "idle"},
}
PRIORITY = {"waiting": 3, "working": 2, "error": 1, "done": 1, "ready": 0}

summaries = {}
pending = {}
checked_pids = {}
launch_times = {}
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
    directory = os.path.join(folder, "sessions")
    try:
        names = os.listdir(directory)
    except OSError:
        return sessions
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


def active_agents(record, now):
    agents = record.get("agents") or {}
    if isinstance(agents, dict):
        return sum(1 for seen in agents.values() if now - seen < AGENT_SECONDS)
    return len(agents)


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


def file_tokens(path):
    state = token_files.setdefault(path, [0, 0, set()])
    try:
        size = os.path.getsize(path)
    except OSError:
        return state[1]
    if size < state[0]:
        state[:] = [0, 0, set()]
    if size > state[0]:
        with open(path, "rb") as file:
            file.seek(state[0])
            chunk = file.read(size - state[0])
        end = chunk.rfind(b"\n") + 1
        for line in chunk[:end].split(b"\n"):
            if b'"usage"' not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            message = entry.get("message") or {}
            usage = message.get("usage")
            if entry.get("type") != "assistant" or not isinstance(usage, dict):
                continue
            key = (message.get("id"), entry.get("requestId"))
            if key in state[2]:
                continue
            state[2].add(key)
            state[1] += sum(usage.get(field) for field in TOKEN_FIELDS if isinstance(usage.get(field), int))
        state[0] += end
    return state[1]


def transcript_of(record):
    if record.get("transcript"):
        return record["transcript"]
    session = record.get("id")
    if session not in transcripts:
        root = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
        found = glob.glob(os.path.join(root, "projects", "*", f"{session}.jsonl"))
        transcripts[session] = found[0] if found else None
    return transcripts[session]


def session_tokens(record):
    transcript = transcript_of(record)
    if not transcript:
        return 0
    total = file_tokens(transcript)
    agents = os.path.join(os.path.splitext(transcript)[0], "subagents")
    try:
        names = os.listdir(agents)
    except OSError:
        names = []
    for name in names:
        if name.endswith(".jsonl"):
            total += file_tokens(os.path.join(agents, name))
    return total


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
    helpers = sum(active_agents(r, now) for r in ordered)
    if helpers:
        parts.append(labels["helpers" if helpers > 1 else "helper"].format(helpers))
    tokens = sum(session_tokens(r) for r in ordered)
    if tokens:
        parts.append(labels["tokens"].format(compact(tokens, language)))
    hover = " · ".join(t for t in (texts.get(r.get("id")) for r in ordered) if t)
    activity = {
        "details": (summary or names[0])[:128],
        "assets": {"large_image": image_for(actions[0]), "large_text": shorten(hover or "Claude Code")},
    }
    if parts:
        activity["state"] = shorten(" · ".join(parts))
    started = min(r.get("started") or r.get("t") or now for r in sessions)
    activity["timestamps"] = {"start": int(started)}
    return activity, len(sessions)


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
