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

LABELS = {
    "fr": {
        "thinking": "Réfléchit", "code": "Écrit du code", "commands": "Lance des commandes",
        "reading": "Lit le code", "web": "Cherche sur internet", "browser": "Navigue sur le web",
        "computer": "Utilise l'ordinateur", "agents": "Dirige des assistants", "planning": "Planifie",
        "tools": "Utilise un outil", "working": "Travaille", "permission": "Attend une permission",
        "question": "Attend une réponse", "done": "A fini, attend la suite", "error": "Bloqué sur une erreur",
        "ready": "Session ouverte", "idle": "En pause",
        "sessions": "{} sessions", "helper": "{} assistant", "helpers": "{} assistants",
    },
    "en": {
        "thinking": "Thinking", "code": "Writing code", "commands": "Running commands",
        "reading": "Reading code", "web": "Searching the web", "browser": "Browsing the web",
        "computer": "Using the computer", "agents": "Directing helpers", "planning": "Planning",
        "tools": "Using a tool", "working": "Working", "permission": "Waiting for permission",
        "question": "Waiting for an answer", "done": "Done, waiting for more", "error": "Stopped on an error",
        "ready": "Session open", "idle": "Idle",
        "sessions": "{} sessions", "helper": "{} helper", "helpers": "{} helpers",
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
    record = max(sessions, key=lambda r: (PRIORITY.get(r.get("state"), 0), r.get("t") or 0))
    action = record.get("action") or "working"
    if record.get("state") in ("done", "ready") and now - record.get("t", 0) > IDLE_SECONDS:
        action = "idle"
    summary = None
    if options.get("summary", True):
        collect_summaries()
        request_summary(record, language)
        summary = summaries.get(record.get("id"), (None, None))[1]
    parts = [labels.get(action, labels["working"])]
    if len(sessions) > 1:
        parts.append(labels["sessions"].format(len(sessions)))
    helpers = len(record.get("agents") or [])
    if helpers:
        parts.append(labels["helpers" if helpers > 1 else "helper"].format(helpers))
    activity = {"assets": {"large_image": image_for(action), "large_text": "Claude Code"}}
    if summary:
        activity["details"] = summary[:128]
        activity["state"] = " · ".join(parts)[:128]
    else:
        activity["details"] = parts[0]
        if len(parts) > 1:
            activity["state"] = " · ".join(parts[1:])
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
