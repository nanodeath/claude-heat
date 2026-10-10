# claude-heat

When several [Claude Code](https://claude.com/claude-code) sessions run at once and the fans spin up, claude-heat shows which session is responsible. It watches CPU use per session and adds flames to the tab title of any session whose work is heavy.

```
✳ Update dependencies
🔥🔥 ◐ Fix flaky integration tests
◐ Refactor auth middleware
```

It counts the processes a session launched, not the Claude Code process itself. Builds, test runs, background jobs, and Docker Compose containers all count.

claude-heat is an unofficial tool. It isn't made or endorsed by Anthropic.

## Requirements

- Linux, since it reads `/proc`
- Python 3.11 or newer
- A terminal that sets tab titles from the OSC 0 escape sequence. It was developed with Ghostty, and most modern terminals support the sequence.
- Docker, only for crediting container load (optional)

## Install

```sh
uv tool install git+https://github.com/nanodeath/claude-heat
```

`pipx install git+https://github.com/nanodeath/claude-heat` works too.

Check that it sees your sessions:

```sh
claude-heat --print
```

```
 4.12 cores  /dev/pts/2   pid 48213    ◐ Fix flaky integration tests
 0.03 cores  /dev/pts/4   pid 51877    ◐ Refactor auth middleware
 0.00 cores  /dev/pts/1   pid 47160    ✳ Update dependencies
```

## Run it in the background

A systemd user unit is included:

```sh
mkdir -p ~/.config/systemd/user
curl -o ~/.config/systemd/user/claude-heat.service \
  https://raw.githubusercontent.com/nanodeath/claude-heat/main/contrib/claude-heat.service
systemctl --user daemon-reload
systemctl --user enable --now claude-heat
```

The unit expects the executable at `~/.local/bin/claude-heat`, which is where `uv tool` and `pipx` put it. Edit `ExecStart` if yours is elsewhere. claude-heat uses under 1% of one core.

## Configuration

Settings are read from `~/.config/claude-heat/config.toml`, or from `$XDG_CONFIG_HOME/claude-heat/config.toml` when that variable is set. Pass `--config PATH` to use another file. Every setting is optional. [`config.example.toml`](config.example.toml) lists them all with their defaults.

| Setting | Default | Effect |
|---|---|---|
| `interval` | `2.0` | Seconds between CPU samples |
| `window` | `10.0` | Seconds of samples averaged before a session turns hot or cools down |
| `flame_thresholds` | `[1.5, 4.0, 8.0]` | Cores needed for one, two, three… flames. The first value marks a session hot. |
| `cool_cores` | `0.5` | A hot session cools down below this average |
| `ramp_down` | `30.0` | Seconds a lower flame level must hold before the title drops to it |
| `flame` | `"🔥"` | The marker, repeated once per threshold reached |
| `status_glyphs` | `true` | Keep Claude Code's ✳ / ◐ / ◑ status glyph in titles |
| `docker` | `true` | Credit Docker Compose containers to sessions |
| `write_all_titles` | `false` | Write every session's title, not only hot ones. See below. |
| `generate_titles` | `true` | Name untitled sessions with Claude Code's title generator. See [Session names](#session-names). |

Unknown settings and bad values stop the program with an error.

Restart the service after editing:

```sh
systemctl --user restart claude-heat
```

### Stopping the flicker

While a session is busy, Claude Code redraws its own title about once a second to animate its spinner. Each redraw replaces claude-heat's title, so the flames blink on and off. To stop that, turn off Claude Code's title and let claude-heat write every title, spinner included.

In `~/.claude/settings.json`:

```json
{
  "env": {
    "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1"
  }
}
```

In `~/.config/claude-heat/config.toml`:

```toml
write_all_titles = true
```

Sessions started before the settings change keep writing their own titles until you restart them. While claude-heat isn't running, tabs keep their last title and the spinner stops.

`CLAUDE_CODE_DISABLE_TERMINAL_TITLE` also stops Claude Code from generating a name for the session. claude-heat makes up for that with `generate_titles`, described under [Session names](#session-names).

## How it works

### Finding a session's processes

Claude Code sets `CLAUDE_PID` in the environment of every process it launches. Child processes inherit their environment, so the variable reaches grandchildren too. It also survives a process being reparented to init when its parent exits. Every 2 seconds, claude-heat groups all processes by that variable.

Processes inside Docker containers descend from Docker's own daemon, so they never inherit the variable. claude-heat finds the container through the process's cgroup, then reads the directory that Docker Compose started it from. If exactly one session is working in that directory, the container's CPU counts toward it. Containers started without Compose are ignored, and so are containers whose directory has more than one session in it.

### Counting CPU

For each process, claude-heat reads its CPU time plus the CPU time of children it has already reaped. That makes short-lived processes like compiler jobs count even when they start and exit between two samples. When a process that was already counted exits, its parent's counter grows by the child's whole lifetime. claude-heat subtracts the part it counted earlier, so nothing gets counted twice.

### Setting titles

claude-heat writes a title escape sequence to the session's terminal device, such as `/dev/pts/5`. It rebuilds Claude Code's title from the session's name and its busy or idle status, then puts the flames in front.

Flames rise as soon as the average load crosses a threshold. They fall only after the lower level has held for `ramp_down` seconds, so short pauses in a build or test run don't make them blink.

With `write_all_titles` off, claude-heat rewrites a hot session's title every second so it wins over Claude Code's own title. When the session cools down, it writes the plain title once and hands the title back to Claude Code. With `write_all_titles` on, claude-heat writes every session's title, and only when it changes.

### Session names

claude-heat picks a session's name in the same order Claude Code does:

1. The name given with `/rename`
2. The newest `ai-title` entry in the session's transcript
3. A name claude-heat generates, when `generate_titles` is on
4. The project directory's name

Claude Code skips step 2 for sessions started with `CLAUDE_CODE_DISABLE_TERMINAL_TITLE` set. For those sessions, claude-heat takes the first prompt you typed and runs a headless `claude` that sends it to Claude Code's own title generator. The prompt goes through the `generate_session_title` control request, so the names match the ones Claude Code would have made. Each session is named once per claude-heat run. claude-heat waits 20 seconds after the first prompt before asking, so sessions that get their own `ai-title` in that time are never named twice.

The headless `claude` runs with hooks disabled and doesn't save a transcript. claude-heat finds it on `PATH` or in `~/.local/bin`.

## Limitations

- **Undocumented internals.** claude-heat relies on details of Claude Code that aren't a public interface. These include the `CLAUDE_PID` variable, the session files in `~/.claude/sessions/`, the `ai-title` entries in transcripts, the `generate_session_title` control request, and the title glyphs. A Claude Code update could change any of them.
- **Remote work isn't counted.** Load on other machines, from `ssh` for example, doesn't count. It doesn't heat this machine either.
- **Some containers can't be credited.** That covers containers started without Compose, and containers whose directory has more than one session in it.
- **Flicker without `write_all_titles`.** Unless you turn off Claude Code's title as described in [Stopping the flicker](#stopping-the-flicker), a busy, hot session's flames blink about once a second.

## Privacy

claude-heat makes no network connections itself, and it writes only to your terminals. With `generate_titles` on, it runs `claude`, which sends the first prompt of each untitled session to Anthropic to be named. That uses your Claude account, the same way Claude Code's own titles do. Set `generate_titles = false` to keep claude-heat offline.

It reads three things:

- The CPU counters and cgroup of every process, which any user can read
- The environment of your own processes, to find `CLAUDE_PID`
- Claude Code's local session files and transcripts, for session names and status

## Development

```sh
uv run pytest
uv run claude-heat --print
```

## License

[MIT](LICENSE)
