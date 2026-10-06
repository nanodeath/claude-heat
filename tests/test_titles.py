import json
import sys

import pytest

from claude_heat import monitor
from claude_heat.monitor import Titles, first_human_prompt, generate_title

SESSION = "abc-123"


def entry(**fields) -> str:
    # Compact, as Claude Code writes it; the scanners prefilter on raw bytes
    return json.dumps(fields, separators=(",", ":")) + "\n"


def human(content) -> str:
    return entry(type="user", origin={"kind": "human"}, message={"role": "user", "content": content})


def test_first_human_prompt_skips_commands_tool_results_and_short_prompts(tmp_path):
    path = tmp_path / "t.jsonl"
    path.write_text(
        entry(type="user", isMeta=True, message={"content": "<local-command-caveat>…"})
        + entry(type="user", message={"content": "<command-name>/model</command-name>"})
        + entry(type="user", message={"content": [{"type": "tool_result", "content": "ok"}]})
        + human("hi")
        + human([{"type": "image"}, {"type": "text", "text": "Why is the build slow?"}])
    )
    prompt, _ = first_human_prompt(path, 0)
    assert prompt == "Why is the build slow?"


def test_first_human_prompt_resumes_after_a_partly_written_line(tmp_path):
    path = tmp_path / "t.jsonl"
    line = human("Fix the flaky integration tests")
    path.write_text(entry(type="mode") + line[:20])

    prompt, offset = first_human_prompt(path, 0)
    assert prompt is None

    path.write_text(entry(type="mode") + line)
    prompt, _ = first_human_prompt(path, offset)
    assert prompt == "Fix the flaky integration tests"


def fake_claude(reply: dict) -> list[str]:
    """A stand-in for headless claude that replies to one control request."""
    script = f"""
import json, sys
request = json.loads(sys.stdin.readline())
print(json.dumps({{"type": "system", "subtype": "init"}}))
reply = {json.dumps(reply)}
reply["response"]["request_id"] = request["request_id"]
print(json.dumps(reply), flush=True)
sys.stdin.read()
"""
    return [sys.executable, "-c", script]


def test_generate_title_reads_the_control_response():
    command = fake_claude(
        {"type": "control_response", "response": {"subtype": "success", "response": {"title": "Auth middleware"}}}
    )
    assert generate_title(command, "Refactor the auth middleware") == "Auth middleware"


def test_generate_title_returns_none_on_an_error_response():
    command = fake_claude({"type": "control_response", "response": {"subtype": "error", "error": "nope"}})
    assert generate_title(command, "Refactor the auth middleware") is None


class FakeNamer:
    def __init__(self):
        self.asked: list[tuple[str, str]] = []

    def name(self, session_id: str, prompt: str) -> str:
        self.asked.append((session_id, prompt))
        return "Generated name"


@pytest.fixture
def transcript(tmp_path, monkeypatch):
    monkeypatch.setattr(monitor, "PROJECTS", tmp_path)
    path = tmp_path / "-home-max-Projects-app" / f"{SESSION}.jsonl"
    path.parent.mkdir()
    path.write_text(human("Refactor the auth middleware"))
    return path


def meta(**fields) -> dict:
    return {"sessionId": SESSION, "name": "app-3f", "nameSource": "derived", **fields}


def test_rename_wins_over_ai_title(transcript):
    with open(transcript, "a") as f:
        f.write(entry(type="ai-title", aiTitle="Auth middleware"))
    titles = Titles(status_glyphs=False, namer=FakeNamer())
    assert titles.session_name(1, meta(name="auth", nameSource="user")) == "auth"


def test_ai_title_wins_over_generating_one(transcript):
    with open(transcript, "a") as f:
        f.write(entry(type="ai-title", aiTitle="Auth middleware"))
    namer = FakeNamer()
    titles = Titles(status_glyphs=False, namer=namer)
    assert titles.session_name(1, meta()) == "Auth middleware"
    assert namer.asked == []


def test_untitled_session_is_named_from_its_first_prompt(transcript):
    namer = FakeNamer()
    titles = Titles(status_glyphs=False, namer=namer)
    assert titles.session_name(1, meta()) == "Generated name"
    assert namer.asked == [(SESSION, "Refactor the auth middleware")]
