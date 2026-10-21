"""Markdown parsing for the map checks.

Supports inline Markdown links and full, collapsed, or shortcut references with
single-line definitions, optional titles, angle-bracket paths, escapes, and balanced
parentheses. Fenced and inline code are skipped. HTML links, autolinks, and heading
anchors are not checked; use Markdown links for maintained local navigation.

Also reads YAML front matter, headings, and the items of lists under a "Rules" heading.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import yaml

LABEL = r"\[((?:\\.|[^\]\\\n])*)\]"
TITLE = r"""(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\((?:\\.|[^)\\])*\))"""

_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def blank(text: str) -> str:
    return re.sub(r"[^\n]", " ", text)


def without_fences(text: str) -> str:
    """The text with fenced code blanked. Line numbers and line lengths stay."""
    lines = []
    fence = ""
    for line in text.splitlines(keepends=True):
        opening = _FENCE.match(line)
        if fence:
            lines.append(blank(line))
            if re.fullmatch(rf" {{0,3}}{fence[0]}{{{len(fence)},}}\s*", line):
                fence = ""
        elif opening:
            fence = opening.group(1)
            lines.append(blank(line))
        else:
            lines.append(line)
    return "".join(lines)


def without_code(text: str) -> str:
    """The text with fenced code and inline code spans blanked."""
    return re.sub(
        r"(?<![\\`])(`+)(?!`)[\s\S]*?(?<!`)\1(?!`)",
        lambda match: blank(match.group()),
        without_fences(text),
    )


def fenced_blocks(text: str) -> list[list[str]]:
    """Return the content lines of each closed fenced code block."""
    blocks: list[list[str]] = []
    fence = ""
    current: list[str] = []
    for line in text.splitlines():
        if not fence:
            if opening := _FENCE.match(line):
                fence = opening.group(1)
            continue
        if re.fullmatch(rf" {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}\s*", line):
            blocks.append(current)
            fence, current = "", []
        else:
            current.append(line)
    return blocks


def destination(text: str) -> tuple[str, str]:
    text = text.lstrip()
    angle = re.match(r"<((?:\\.|[^<>\\\n])*)>", text)
    if angle:
        return angle.group(1), text[angle.end() :]
    depth = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            index += 2
            continue
        if char.isspace() or (char == ")" and depth == 0):
            break
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif char in "<>":
            raise ValueError("invalid navigation link syntax; enclose spaced paths in <...>")
        index += 1
    if depth:
        raise ValueError("invalid navigation link syntax; balance destination parentheses")
    return text[:index], text[index:]


def reference_key(label: str) -> str:
    return " ".join(label.split()).casefold()


def _reference_definitions(text: str) -> tuple[dict[str, str], str]:
    """The targets of the single-line reference definitions, and the text with them blanked."""
    references: dict[str, str] = {}
    definitions = list(re.finditer(rf"(?m)^ {{0,3}}{LABEL}:[ \t]*(.*)$", text))
    for definition in definitions:
        line = text.count("\n", 0, definition.start()) + 1
        try:
            target, rest = destination(definition.group(2))
            if not target or not re.fullmatch(rf"\s*(?:{TITLE})?\s*", rest):
                raise ValueError(
                    "invalid navigation reference syntax; use a single-line definition"
                )
        except ValueError as error:
            raise ValueError(f"{line}: {error}") from error
        references.setdefault(reference_key(definition.group(1)), target)
    for definition in reversed(definitions):
        text = text[: definition.start()] + blank(definition.group()) + text[definition.end() :]
    return references, text


def _inline_link(text: str, position: int, line: int) -> tuple[str, int]:
    """The target of the inline link whose "(" is at position, and where scanning resumes."""
    try:
        target, rest = destination(text[position + 1 :])
        closing = re.match(rf"\s*(?:{TITLE}\s*)?\)", rest)
        if not closing:
            raise ValueError("invalid navigation link syntax; use a path and optional title")
        return target, len(text) - len(rest) + closing.end()
    except ValueError as error:
        raise ValueError(f"{line}: {error}") from error


def navigation_links(text: str) -> list[tuple[int, str]]:
    """The (line, target) of every Markdown link outside code. ValueError names a bad line."""
    references, text = _reference_definitions(without_code(text))
    links: list[tuple[int, str]] = []
    position = 0
    while match := re.search(rf"(?<!\\){LABEL}", text[position:]):
        start = position + match.start()
        position += match.end()
        line = text.count("\n", 0, start) + 1
        if text[position : position + 1] == "(":
            target, position = _inline_link(text, position, line)
            links.append((line, target))
            continue
        reference = re.match(LABEL, text[position:])
        label = match.group(1)
        if reference:
            label = reference.group(1) or label
            position += reference.end()
        key = reference_key(label)
        if key in references:
            links.append((line, references[key]))
        elif reference:
            raise ValueError(
                f"{line}: undefined navigation reference {label!r}; add its definition"
            )
    return links


def front_matter(text: str) -> object | None:
    """The parsed YAML between the opening --- and the closing --- line.

    None when the text does not start with ---. ValueError when the block is not closed;
    yaml.YAMLError when it is not valid YAML.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() in ("---", "..."):
            data: object = yaml.safe_load("\n".join(lines[1:index]))  # yaml.YAMLError propagates
            return data
    raise ValueError("front matter is not closed with ---")


_HEADING = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?(?:[ \t]+#+)?[ \t]*$")
_ITEM = re.compile(r"^(?P<indent> *)(?:[-*+]|\d{1,9}[.)])[ \t]+(?P<text>\S.*)$")


def _headings(text: str) -> list[tuple[int, int, str]]:
    """(line index, level, title) for each ATX heading outside fenced code."""
    found = []
    for index, line in enumerate(without_fences(text).splitlines()):
        if match := _HEADING.match(line):
            found.append((index, len(match.group(1)), (match.group(2) or "").strip()))
    return found


def section(text: str, title: str, level: int) -> str | None:
    """The lines under the first heading of this title and level, up to the next heading of
    the same or a higher level. None when there is no such heading."""
    lines = text.splitlines()
    marks = _headings(text)
    for position, (index, depth, name) in enumerate(marks):
        if depth == level and name == title:
            ends = [i for i, d, _ in marks[position + 1 :] if d <= level]
            return "\n".join(lines[index + 1 : ends[0] if ends else len(lines)])
    return None


@dataclass(frozen=True)
class ListItem:
    line: int  # line of the item's marker
    text: str  # the item's own text, without nested lists or code, joined by spaces


class _RulesScanner:
    """Collect the top-level items of lists under a "Rules" heading.

    An item's text is its first line plus its own continuation lines. Nested list
    items and their continuation lines belong to the rule above but are not rules.
    Fenced code is blanked before scanning, so a list inside code is ignored.
    """

    def __init__(self) -> None:
        self.items: list[ListItem] = []
        self.rules_level: int | None = None
        self.base: int | None = None  # marker indent of this section's top-level items
        self.nested: int | None = None  # marker indent of the open nested item
        self.current: tuple[int, list[str]] | None = None
        self.blank = False

    def heading(self, level: int, title: str) -> None:
        self.close()
        if self.rules_level is not None and level <= self.rules_level:
            self.rules_level = None
        if title == "Rules":
            self.rules_level, self.base = level, None

    def line(self, number: int, text: str) -> None:
        if self.rules_level is None:
            return
        if not text.strip():
            self.blank = True
            return
        indent = len(text) - len(text.lstrip(" "))
        item = _ITEM.match(text)
        if item and (self.base is None or indent <= self.base):
            self.close()
            self.base = indent if self.base is None else self.base
            self.current = (number, [item.group("text").strip()])
        elif self.current is not None:
            self.continuation(indent, text, item is not None)
        self.blank = False

    def continuation(self, indent: int, text: str, is_item: bool) -> None:
        if self.current is None or self.base is None:
            return
        if is_item:
            self.nested = indent
        elif self.nested is not None and indent > self.nested:
            return
        elif self.blank and indent <= self.base:
            self.close()  # a paragraph after the list ends it
        else:
            self.nested = None
            self.current[1].append(text.strip())

    def close(self) -> None:
        if self.current is not None:
            self.items.append(ListItem(self.current[0], " ".join(self.current[1])))
        self.current, self.nested = None, None


def rules_items(text: str) -> list[ListItem]:
    """The top-level list items under each heading titled Rules, with their marker lines."""
    scanner = _RulesScanner()
    for number, line in enumerate(without_fences(text).splitlines(), start=1):
        if heading := _HEADING.match(line):
            scanner.heading(len(heading.group(1)), (heading.group(2) or "").strip())
        else:
            scanner.line(number, line)
    scanner.close()
    return scanner.items
