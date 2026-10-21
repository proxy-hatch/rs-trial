"""The evidence step: a pull request says what was run, what it showed and what it did not.

The pull request body needs an `## Evidence` section with a command and its result, and a
`Not covered:` line. The section may also list removed tests under `Removed tests:`; the diff
step reads those declarations through `removed_test_declarations`.

    ## Evidence

    - `./devctl check`: exit 0; every step passed except evidence, not applicable
    - **Before:** test_parse_dates failed. **After:** it passes.

    Removed tests:
    - `tests/test_flags.py::test_legacy_flag`: the flag was deleted in this PR

    Not covered: behaviour on Windows.

Everything is read from the section only, with fenced code blanked, except a fenced transcript
that starts with `$ `, which counts as a command and its output.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from devctl.check.markdown import fenced_blocks, section, without_fences
from devctl.check.steps import CheckContext, StepResult, Tier, failed, not_applicable, passed

_DECLARATION = re.compile(r"^\s*[-*]\s+`?(?P<test>[^\s`]+::[^\s`]+?)`?:\s+(?P<reason>\S.*)$")
_COMMAND = re.compile(r"^\s*[-*]\s+`[^`]+`:\s*\S")
_ITEM = re.compile(r"^\s*[-*+]\s")

_REMOVED_TESTS = "Removed tests:"
_NOT_COVERED = "Not covered:"

_NO_SECTION = "add an '## Evidence' section with a command, its result and a 'Not covered:' line"
_NO_COMMAND = "Evidence: list a command you ran with its result, as '- `<command>`: <result>'"
_NO_NOT_COVERED = (
    "Evidence: add a line starting 'Not covered:' that names what the commands do not show"
)


def evidence_section(body: str) -> str | None:
    """The text under the body's `## Evidence` heading; None when there is no such heading."""
    return section(body, "Evidence", 2)


def _removed_items(lines: Sequence[str]) -> dict[int, str]:
    """The list items right under each `Removed tests:` line, by line index.

    A list ends at the first blank line or line that is not an item.
    """
    items: dict[int, str] = {}
    listing = False
    for index, line in enumerate(lines):
        if line.strip() == _REMOVED_TESTS:
            listing = True
        elif listing and _ITEM.match(line):
            items[index] = line.strip()
        else:
            listing = False
    return items


def removed_test_declarations(body: str) -> tuple[dict[str, str], list[str]]:
    """The removed tests the body declares, as {"path::name": reason}, and the unreadable items.

    Only the Evidence section is read. Each item is `- `<path>::<name>`: <reason>`, with or
    without the backticks.
    """
    text = evidence_section(body)
    if text is None:
        return {}, []
    declared: dict[str, str] = {}
    unreadable: list[str] = []
    for item in _removed_items(without_fences(text).splitlines()).values():
        if match := _DECLARATION.match(item):
            declared[match["test"]] = match["reason"]
        else:
            unreadable.append(item)
    return declared, unreadable


def _is_transcript(block: Sequence[str]) -> bool:
    """A fenced block that starts with a `$ ` command line and shows at least one more line."""
    content = [line.strip() for line in block if line.strip()]
    return len(content) >= 2 and content[0].startswith("$ ")


def _has_command(text: str, lines: Sequence[str]) -> bool:
    removed = _removed_items(lines)
    if any(_COMMAND.match(line) for index, line in enumerate(lines) if index not in removed):
        return True
    return any(_is_transcript(block) for block in fenced_blocks(text))


def _has_not_covered(lines: Sequence[str]) -> bool:
    stripped = (line.strip() for line in lines)
    return any(
        line.startswith(_NOT_COVERED) and line.removeprefix(_NOT_COVERED).strip()
        for line in stripped
    )


class EvidenceStep:
    name = "evidence"
    tier: Tier = "full"

    def run(self, context: CheckContext) -> StepResult:
        body = context.pr_body
        if body is None:
            return not_applicable(self.name, "not a pull request")
        text = evidence_section(body)
        if text is None:
            return failed(self.name, _NO_SECTION)
        lines = without_fences(text).splitlines()
        problems: list[str] = []
        if not _has_command(text, lines):
            problems.append(_NO_COMMAND)
        if not _has_not_covered(lines):
            problems.append(_NO_NOT_COVERED)
        # Whether each removed test is declared is the diff step's check, so it is not repeated.
        problems += [
            f"Removed tests: cannot read '{item}'; write '- `<path>::<test>`: <reason>'"
            for item in removed_test_declarations(body)[1]
        ]
        return failed(self.name, *problems) if problems else passed(self.name)
