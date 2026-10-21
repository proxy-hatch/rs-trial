"""The headless Claude Code Session that doctor --skills starts, and how it reads its output.

The Session spends a little of the owner's quota, whatever the Provider, to ask which skills
Claude Code loads. It runs Claude Code's cheapest model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from devctl import process
from devctl.process import ProcessResult

# Claude Code's cheapest model, as the Checked facts of plan 4 (Doctor and Agent identity) record
# it. That plan's live trial confirms it.
PROBE_MODEL = "haiku"
PROBE_TIMEOUT_SECONDS = 120.0
SKILLS_PROMPT = "List the name of every skill you can load, one name per line, and nothing else."


@dataclass(frozen=True)
class SkillReport:
    names: frozenset[str] | None  # None when the Session gave no skill list
    error: str | None  # why not, cut to 300 characters


def skills_args() -> list[str]:
    """The arguments after `claude` that make a new headless Session list its skills."""
    return [
        "-p",
        SKILLS_PROMPT,
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        PROBE_MODEL,
    ]


def _events(stdout: str) -> list[dict[str, object]]:
    """Each stdout line that is a JSON object, in order. Other lines are skipped."""
    found: list[dict[str, object]] = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            found.append(event)
    return found


def _strings(value: object) -> list[str] | None:
    """value when it is a list of strings, else None."""
    if not isinstance(value, list):
        return None
    texts = [item for item in value if isinstance(item, str)]
    return texts if len(texts) == len(value) else None


def _reply_name(line: str) -> str:
    """One skill name from a reply line: without a bullet, backticks or a leading / or $."""
    name = line.strip()
    if name.startswith(("- ", "* ")):
        name = name[2:]
    name = name.strip().strip("`")
    if name[:1] in ("/", "$"):
        name = name[1:]
    return name.strip()


def _reply_names(text: str) -> frozenset[str]:
    """The names in a reply: one per non-empty line."""
    return frozenset(name for name in map(_reply_name, text.splitlines()) if name)


def parse_skills(result: ProcessResult) -> SkillReport:
    """The skills the Session listed, or why it listed none. Cut output is never a list.

    The list is the `init` event's skills, else the final reply.
    """
    if result.timed_out or result.stdout_truncated or result.stderr_truncated:
        detail = (
            f"claude did not finish within {PROBE_TIMEOUT_SECONDS:.0f} s, or its output was cut"
        )
        return SkillReport(None, detail)
    events = _events(result.stdout)
    for event in events:
        if event.get("type") == "system" and event.get("subtype") == "init":
            listed = _strings(event.get("skills"))
            if listed is not None:
                return SkillReport(frozenset(listed), None)
    final = next((event for event in reversed(events) if event.get("type") == "result"), {})
    text = final.get("result")
    if final and final.get("is_error") is False and isinstance(text, str):
        return SkillReport(_reply_names(text), None)
    detail = process.first_line(text) if isinstance(text, str) else ""
    return SkillReport(None, detail or f"claude exited {result.returncode} with no skill list")
