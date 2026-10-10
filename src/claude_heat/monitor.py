"""Measure CPU per Claude Code session and mark hot sessions in tab titles.

Every process Claude Code spawns inherits CLAUDE_PID in its environment,
including grandchildren and orphans reparented to systemd. Processes inside
Docker Compose containers descend from containerd instead, so they are credited
to the one session whose working directory is the Compose project directory.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import unicodedata
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import count
from pathlib import Path
from typing import NamedTuple

from .config import Config

# Claude Code's own title glyphs, kept so our writes don't erase them
IDLE_GLYPH = "✳"
BUSY_GLYPHS = ("◐", "◑")

# Seconds per spinner frame; Claude Code's own is about 0.96
SPINNER_PERIOD = 1.0

TICKS = os.sysconf("SC_CLK_TCK")

CLAUDE_HOME = Path.home() / ".claude"
SESSIONS = CLAUDE_HOME / "sessions"
PROJECTS = CLAUDE_HOME / "projects"
TAIL_BYTES = 256 * 1024

# Claude Code's title generator gives up on prompts shorter than this
MIN_PROMPT_CHARS = 10
TITLE_TIMEOUT = 60.0

# Claude Code writes its own ai-title a few seconds after the first prompt
# unless titles are disabled, so generating one waits this long
TITLE_DELAY = 20.0

DOCKER_SCOPE = re.compile(r"docker-([0-9a-f]{64})\.scope")
COMPOSE_DIR_LABEL = '{{index .Config.Labels "com.docker.compose.project.working_dir"}}'
DOCKER_TIMEOUT = 5.0

Session = tuple[int, str]  # (claude pid, pty path)
ProcKey = tuple[int, int]  # (pid, starttime), unique across pid reuse


@dataclass(frozen=True)
class Spawned:
    """A process launched by a Claude session, directly or not."""

    claude_pid: int

    def session(self, by_cwd: dict[str, list[Session]]) -> Session | None:
        return session_pty(self.claude_pid)


@dataclass(frozen=True)
class Container:
    """A process inside a Docker Compose container."""

    workdir: str

    def session(self, by_cwd: dict[str, list[Session]]) -> Session | None:
        # Two sessions in one directory can't be told apart, so skip those
        matches = by_cwd.get(self.workdir, [])
        return matches[0] if len(matches) == 1 else None


Owner = Spawned | Container


class Proc(NamedTuple):
    owner: Owner
    cpu: int  # ticks, including reaped children
    ppid: int


@dataclass(frozen=True)
class Sample:
    now: float  # uptime in clock ticks
    procs: dict[ProcKey, Proc]


def read_stat(pid: int) -> tuple[int, int, int] | None:
    """Return (ppid, starttime, cpu ticks including reaped children)."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as f:
            raw = f.read()
    except OSError:
        return None

    # comm can contain spaces and parens, so split after the last ')'
    fields = raw[raw.rindex(b")") + 2 :].split()
    utime, stime, cutime, cstime = (int(x) for x in fields[11:15])
    return int(fields[1]), int(fields[19]), utime + stime + cutime + cstime


def claude_pid_of(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/environ", "rb") as f:
            env = f.read()
    except OSError:
        return None
    for var in env.split(b"\0"):
        if var.startswith(b"CLAUDE_PID="):
            return int(var[11:])
    return None


def session_pty(cpid: int) -> Session | None:
    """Follow CLAUDE_PID up to the interactive session that owns a pty.

    A headless `claude -p` spawned by a session has no pty of its own, so its
    children's load belongs to the session that launched it.
    """
    try:
        tty = os.readlink(f"/proc/{cpid}/fd/0")
    except OSError:
        return None
    if tty.startswith("/dev/pts/"):
        return cpid, tty
    parent = claude_pid_of(cpid)
    return session_pty(parent) if parent and parent != cpid else None


class Sampler:
    """Snapshots CPU ticks for every process that belongs to a session."""

    def __init__(self, docker: bool):
        self.docker = docker

        # A process's environ and cgroup are fixed for its life
        self.owners: dict[ProcKey, Owner | None] = {}
        self.workdirs: dict[str, str | None] = {}

    def sample(self) -> Sample:
        with open("/proc/uptime") as f:
            now = float(f.read().split()[0]) * TICKS

        procs = {}
        seen = set()
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            pid = int(name)
            stat = read_stat(pid)
            if stat is None:
                continue
            ppid, starttime, cpu = stat
            key = (pid, starttime)
            seen.add(key)
            if key not in self.owners:
                self.owners[key] = self.owner_of(pid)
            owner = self.owners[key]
            if owner is not None:
                procs[key] = Proc(owner, cpu, ppid)

        for key in self.owners.keys() - seen:
            del self.owners[key]
        return Sample(now, procs)

    def owner_of(self, pid: int) -> Owner | None:
        cpid = claude_pid_of(pid)
        if cpid is not None:

            # The session's own Node process is excluded; we want what it spawned
            return Spawned(cpid) if cpid != pid else None
        if not self.docker:
            return None

        try:
            with open(f"/proc/{pid}/cgroup") as f:
                match = DOCKER_SCOPE.search(f.read())
        except OSError:
            return None
        workdir = self.compose_workdir(match[1]) if match else None
        return Container(workdir) if workdir else None

    def compose_workdir(self, container_id: str) -> str | None:
        if container_id not in self.workdirs:
            try:
                result = subprocess.run(
                    ["docker", "inspect", "-f", COMPOSE_DIR_LABEL, container_id],
                    capture_output=True,
                    text=True,
                    timeout=DOCKER_TIMEOUT,
                )
            except (OSError, subprocess.TimeoutExpired) as e:
                log(f"docker inspect failed, so container {container_id[:12]} isn't credited: {e}")
                self.workdirs[container_id] = None
                return None

            # Exits non-zero if the container is already gone
            workdir = result.stdout.strip() if result.returncode == 0 else ""
            self.workdirs[container_id] = workdir or None
        return self.workdirs[container_id]


def reaper(
    key: ProcKey, prev: Sample, cur: Sample, prev_by_pid: dict[int, ProcKey]
) -> Owner | None:
    """Owner of the live, owned ancestor that absorbed a dead process's ticks.

    Reaping adds a child's lifetime total to the parent's reaped-children
    counters. If the parent died too, its total moves up again.
    """
    pid = prev.procs[key].ppid
    while pid in prev_by_pid:
        ancestor = prev_by_pid[pid]
        if ancestor in cur.procs:
            return cur.procs[ancestor].owner
        pid = prev.procs[ancestor].ppid
    return None


def cores_by_owner(prev: Sample, cur: Sample) -> dict[Owner, float]:
    """CPU cores each owner used between two samples.

    Each process counts by its own change in ticks, and a process born since
    the previous sample counts in full. When a process we already counted is
    reaped by an owned ancestor, the ancestor's counter jumps by the child's
    whole lifetime, so the part counted earlier is taken back out.
    """
    used: dict[Owner, int] = defaultdict(int)
    for key, proc in cur.procs.items():
        if key in prev.procs:
            used[proc.owner] += proc.cpu - prev.procs[key].cpu
        elif key[1] >= prev.now:
            used[proc.owner] += proc.cpu

    prev_by_pid = {key[0]: key for key in prev.procs}
    for key in prev.procs.keys() - cur.procs.keys():
        owner = reaper(key, prev, cur, prev_by_pid)
        if owner is not None:
            used[owner] -= prev.procs[key].cpu

    # A parent that ignores SIGCHLD never absorbs its children's ticks
    elapsed = cur.now - prev.now
    return {owner: max(0, ticks) / elapsed for owner, ticks in used.items()}


def read_meta(cpid: int) -> dict | None:
    """Claude Code's metadata file for a live session, or None."""
    try:
        return json.loads((SESSIONS / f"{cpid}.json").read_text())
    except (OSError, ValueError):
        return None  # gone, or caught mid-rewrite


def live_sessions() -> dict[Session, str]:
    """Each live interactive session, with its working directory."""
    sessions = {}
    for path in SESSIONS.glob("*.json"):
        meta = read_meta(int(path.stem))
        if meta is None:
            continue
        session = session_pty(meta["pid"])

        # Headless sessions resolve to their parent's pty and don't count here
        if session and session[0] == meta["pid"]:
            sessions[session] = meta["cwd"]
    return sessions


def cores_by_session(prev: Sample, cur: Sample) -> dict[Session, float]:
    by_cwd: dict[str, list[Session]] = defaultdict(list)
    for session, cwd in live_sessions().items():
        by_cwd[cwd].append(session)

    totals: dict[Session, float] = defaultdict(float)
    for owner, cores in cores_by_owner(prev, cur).items():
        session = owner.session(by_cwd)
        if session:
            totals[session] += cores
    return totals


def flame_level(avg: float, hot: bool, config: Config) -> int:
    """Flames the current average earns, before any ramp-down hold.

    A hot session keeps one flame until its average drops below cool_cores.
    """
    level = sum(avg >= t for t in config.flame_thresholds)
    if hot and avg >= config.cool_cores:
        return max(1, level)
    return level


class Heat:
    """One session's flame level, which rises at once and falls slowly.

    The level shown is the highest one earned in the last ramp_down seconds.
    """

    def __init__(self, config: Config):
        self.config = config
        self.recent: deque[int] = deque(maxlen=config.ramp_down_samples)
        self.level = 0

    def update(self, avg: float, window_full: bool) -> int:
        hot = self.level > 0

        # A session must fill its averaging window before it can turn hot
        earned = flame_level(avg, hot, self.config) if hot or window_full else 0
        self.recent.append(earned)
        self.level = max(self.recent)
        return self.level


def project_name(cpid: int) -> str:
    try:
        return os.path.basename(os.readlink(f"/proc/{cpid}/cwd"))
    except OSError:
        return "?"  # session exited mid-tick


def last_ai_title(path: Path, start: int) -> str | None:
    found = None
    with open(path, "rb") as f:
        f.seek(start)

        # A tail read starts mid-line
        if start > 0:
            f.readline()

        # A line without its newline is still being written
        for line in f:
            if line.endswith(b"\n") and b'"type":"ai-title"' in line:
                found = line
    return json.loads(found)["aiTitle"] if found else None


def message_text(content: str | list) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(part["text"] for part in content if part.get("type") == "text")


class Prompt(NamedTuple):
    text: str
    typed: datetime


def first_human_prompt(path: Path, start: int) -> tuple[Prompt | None, int]:
    """The first prompt a person typed at or after byte `start`, and where the scan stopped.

    Short prompts are skipped, as Claude Code's own title generator skips them.
    """
    end = start
    with open(path, "rb") as f:
        f.seek(start)
        for line in f:
            # A partly written last line waits for the next scan
            if not line.endswith(b"\n"):
                break
            end += len(line)

            if b'"kind":"human"' not in line:
                continue
            entry = json.loads(line)
            if entry["type"] != "user" or entry.get("origin", {}).get("kind") != "human":
                continue
            text = message_text(entry["message"]["content"]).strip()
            if len(text) >= MIN_PROMPT_CHARS:
                return Prompt(text, datetime.fromisoformat(entry["timestamp"])), end
    return None, end


def generate_title(command: list[str], prompt: str) -> str | None:
    """Ask Claude Code's built-in title generator to name a prompt.

    The generator is reachable as the generate_session_title control request
    of a headless `claude`. Claude Code skips it in sessions started with
    CLAUDE_CODE_DISABLE_TERMINAL_TITLE set, so those never get an ai-title.
    """
    request = {
        "type": "control_request",
        "request_id": "title",
        "request": {"subtype": "generate_session_title", "description": prompt, "persist": False},
    }
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        cwd=Path.home(),
        text=True,
        encoding="utf-8",
    )
    timer = threading.Timer(TITLE_TIMEOUT, proc.kill)
    timer.start()
    try:
        # stdin stays open until the reply; claude exits early once it closes
        proc.stdin.write(json.dumps(request) + "\n")
        proc.stdin.flush()
        for line in proc.stdout:
            message = json.loads(line)
            if message["type"] != "control_response":
                continue
            response = message["response"]
            if response["subtype"] != "success":
                log(f"title request failed: {response.get('error')}")
                return None
            return response["response"]["title"]
        log(f"claude exited without naming the session (status {proc.wait()})")
        return None
    finally:
        timer.cancel()
        proc.stdin.close()
        proc.wait()


def title_command() -> list[str] | None:
    # systemd's default PATH leaves out ~/.local/bin, where Claude Code installs itself
    path = os.pathsep.join([os.environ.get("PATH", ""), str(Path.home() / ".local" / "bin")])
    claude = shutil.which("claude", path=path)
    if claude is None:
        log("claude not found, so untitled sessions keep the project name")
        return None
    return [
        claude,
        "--print",
        "--input-format=stream-json",
        "--output-format=stream-json",
        "--verbose",
        "--no-session-persistence",
        "--settings",
        '{"disableAllHooks": true}',
    ]


def log(message: str) -> None:
    print(f"claude-heat: {message}", file=sys.stderr, flush=True)


class Namer:
    """Names untitled sessions in the background, once per session.

    Each request starts a headless `claude` and takes a few seconds, so it
    runs on its own thread and the title loop never waits for it.
    """

    def __init__(self, command: list[str]):
        self.command = command
        self.threads: dict[str, threading.Thread] = {}
        self.names: dict[str, str | None] = {}

    def name(self, session_id: str, prompt: str) -> str | None:
        """The generated name, or None while it's pending or if it failed."""
        if session_id not in self.threads:
            thread = threading.Thread(target=self.generate, args=(session_id, prompt), daemon=True)
            self.threads[session_id] = thread
            thread.start()
        return self.names.get(session_id)

    def generate(self, session_id: str, prompt: str) -> None:
        self.names[session_id] = generate_title(self.command, prompt)

    def wait(self) -> None:
        for thread in list(self.threads.values()):
            thread.join()


class Titles:
    """Rebuilds the title Claude Code shows for a session."""

    def __init__(self, status_glyphs: bool, namer: Namer | None):
        self.status_glyphs = status_glyphs
        self.namer = namer
        self.transcripts: dict[str, Path] = {}

        # Newest ai-title per transcript already read in full
        self.names: dict[Path, str | None] = {}

        # First prompt per transcript, or how far the search for one has read
        self.prompts: dict[Path, Prompt] = {}
        self.scanned: dict[Path, int] = {}

    def default(self, cpid: int, tick: int) -> str:
        meta = read_meta(cpid)
        name = self.session_name(cpid, meta)
        if not self.status_glyphs:
            return name
        busy = meta is not None and meta.get("status") == "busy"
        glyph = BUSY_GLYPHS[tick % 2] if busy else IDLE_GLYPH
        return f"{glyph} {name}"

    def session_name(self, cpid: int, meta: dict | None) -> str:
        """The name Claude Code would show, or the project name before one exists.

        Like Claude Code, this prefers a /rename name, then the generated title.
        """
        name = None
        if meta is not None and meta.get("nameSource") == "user":
            name = meta["name"]
        elif meta is not None:
            try:
                name = self.transcript_name(meta["sessionId"])
            except (OSError, StopIteration):
                pass  # transcript not written yet, or session exited mid-tick
        if name is None:
            name = project_name(cpid)

        # Models, users and directory names all supply names, so drop the
        # C0, DEL and C1 controls a terminal could act on
        return "".join(ch for ch in name if unicodedata.category(ch) != "Cc")

    def transcript_name(self, session_id: str) -> str | None:
        """The session's newest ai-title, or a title generated from its first prompt.

        Claude Code appends an ai-title entry to the transcript as the name
        evolves, so the newest one sits near the end. Only the tail is
        rescanned after the first lookup.
        """
        if session_id not in self.transcripts:
            self.transcripts[session_id] = next(PROJECTS.glob(f"*/{session_id}.jsonl"))
        path = self.transcripts[session_id]

        if path in self.names:
            tail = max(0, path.stat().st_size - TAIL_BYTES)
            name = last_ai_title(path, tail) or self.names[path]
        else:
            name = last_ai_title(path, 0)
        self.names[path] = name
        if name is not None:
            return name

        if self.namer is None:
            return None
        if path not in self.prompts:
            prompt, self.scanned[path] = first_human_prompt(path, self.scanned.get(path, 0))
            if prompt is None:
                return None
            self.prompts[path] = prompt
        prompt = self.prompts[path]

        # Give Claude Code the chance to write its own ai-title first
        waited = (datetime.now(timezone.utc) - prompt.typed).total_seconds()
        if waited < TITLE_DELAY:
            return None
        return self.namer.name(session_id, prompt.text)

    def wait_for_names(self) -> None:
        if self.namer is not None:
            self.namer.wait()


def set_title(tty: str, title: str) -> None:
    try:
        with open(tty, "w") as f:
            f.write(f"\033]0;{title}\007")
    except OSError:
        pass  # session exited between sample and write


def make_titles(config: Config) -> Titles:
    command = title_command() if config.generate_titles else None
    return Titles(config.status_glyphs, Namer(command) if command else None)


def print_once(config: Config) -> None:
    sampler = Sampler(config.docker)
    titles = make_titles(config)
    prev = sampler.sample()
    time.sleep(config.interval)

    # A session with no processes of its own has no total but still belongs here
    totals = {session: 0.0 for session in live_sessions()} | cores_by_session(prev, sampler.sample())

    # Start naming any untitled sessions, then wait so the printout shows the names
    for cpid, _ in totals:
        titles.default(cpid, 0)
    titles.wait_for_names()

    for (cpid, tty), cores in sorted(totals.items(), key=lambda x: -x[1]):
        print(f"{cores:5.2f} cores  {tty:<11} pid {cpid:<8} {titles.default(cpid, 0)}")


def run(config: Config) -> None:
    sampler = Sampler(config.docker)
    titles = make_titles(config)
    history: dict[Session, deque] = defaultdict(lambda: deque(maxlen=config.window_samples))
    heat: dict[Session, Heat] = defaultdict(lambda: Heat(config))

    # Last title written per pty, so unchanged titles aren't resent
    written: dict[str, str] = {}

    tick_period = min(config.interval, SPINNER_PERIOD)
    prev = sampler.sample()
    next_sample = time.monotonic() + config.interval

    for tick in count():
        time.sleep(tick_period)
        now = time.monotonic()
        if now >= next_sample:
            # Skip ahead rather than catch up, as after a suspend
            next_sample = max(next_sample + config.interval, now)
            cur = sampler.sample()
            update_heat(history, heat, cores_by_session(prev, cur), config)
            prev = cur

        sessions = live_sessions()
        for tty in written.keys() - {tty for _, tty in sessions}:
            del written[tty]

        for session in sessions:
            cpid, tty = session
            level = heat[session].level if session in heat else 0
            title = titles.default(cpid, tick)
            if level:
                title = f"{config.flame * level} {title}"

            if config.write_all_titles:
                if written.get(tty) != title:
                    set_title(tty, title)
                    written[tty] = title
            elif level:
                # Re-assert every tick so Claude Code's own title updates don't win
                set_title(tty, title)
                written[tty] = title
            elif tty in written:
                # Cooled down: hand the title back to Claude Code
                set_title(tty, title)
                del written[tty]


def update_heat(
    history: dict[Session, deque],
    heat: dict[Session, Heat],
    totals: dict[Session, float],
    config: Config,
) -> None:
    for session in list(history):
        if not os.path.exists(f"/proc/{session[0]}"):
            del history[session]
            heat.pop(session, None)
    for session in set(history) | set(totals):
        history[session].append(totals.get(session, 0.0))

    for session, samples in history.items():
        avg = sum(samples) / len(samples)
        heat[session].update(avg, len(samples) == config.window_samples)
