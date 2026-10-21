"""Checks that need no language tooling: secrets and workflows steps, and two suppressions."""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from pathlib import Path

from devctl.check.steps import (
    CheckContext,
    StepResult,
    Tier,
    blocked,
    command_result,
    failed,
    not_applicable,
    output_tail,
    run_tool,
)

# Split so that documents quoting this code do not contain gitleaks's allow comment.
# rapid_ship/kit/adopt.py copies _GITLEAKS_ALLOW.
_GITLEAKS_ALLOW = "gitleaks" + ":allow"
_ZIZMOR = re.compile(r"#\s*zizmor:\s*ignore\[(?P<names>[^\]]+)\]")

# gitleaks and zizmor exit codes, checked against gitleaks 8.30.1 (cmd/root.go, cmd/git.go) and
# zizmor 1.30.1 (docs/usage.md). gitleaks exits 1 for its own errors, so findings get a code of
# their own. zizmor exits 11 to 14 for findings, by severity, and 3 when it collects no input.
GITLEAKS_VERSION = "8.30.1"
ZIZMOR = "zizmor==1.30.1"
_GITLEAKS_FINDING = 10
_ZIZMOR_FINDINGS = frozenset({11, 12, 13, 14})
_ZIZMOR_NO_INPUTS = 3


def text_rules(path: str, text: str) -> list[str]:
    rules = ["gitleaks"] * text.count(_GITLEAKS_ALLOW)
    if path.endswith((".yml", ".yaml")):
        for match in _ZIZMOR.finditer(text):
            names = sorted(name for name in re.split(r"[\s,]+", match.group("names")) if name)
            rules.append("zizmor:" + ",".join(names))
    return rules


class SecretsStep:
    """Scan the commits of the change, or the whole history when there is no base, for secrets.

    Uncommitted changes are not scanned: gitleaks reads commits.
    """

    name = "secrets"
    tier: Tier = "full"

    def run(self, context: CheckContext) -> StepResult:
        if shutil.which("gitleaks") is None:
            return blocked(
                self.name, f"gitleaks is not on PATH; install gitleaks {GITLEAKS_VERSION}"
            )
        with tempfile.TemporaryDirectory(prefix="devctl-secrets-") as scratch:
            report = Path(scratch, "report.json")
            argv = (
                *("gitleaks", "git", "--no-banner", "--redact"),
                *("--exit-code", str(_GITLEAKS_FINDING)),
                *("--report-format", "json", "--report-path", str(report)),
                *((f"--log-opts={context.base}..HEAD",) if context.base else ()),
                ".",
            )
            result = run_tool(context.repo.root, argv, 900)
            if result is None or result.timed_out or result.returncode != _GITLEAKS_FINDING:
                return command_result(self.name, argv, result, frozenset())
            findings = _findings(report)
            if findings is None:
                return blocked(
                    self.name,
                    "gitleaks found secrets but wrote no readable report",
                    *output_tail(result),
                )
            return failed(self.name, *findings)


def _findings(report: Path) -> list[str] | None:
    """One line per finding in gitleaks's JSON report; None when the report cannot be read.

    Exit 10 means gitleaks found something, so an empty list is as unreadable as a damaged file.
    """
    try:
        document = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(document, list) or not document:
        return None
    lines: list[str] = []
    for finding in document:
        if not isinstance(finding, dict):
            return None
        rule, path = finding.get("RuleID"), finding.get("File")
        line, commit = finding.get("StartLine"), finding.get("Commit")
        if not (
            isinstance(rule, str)
            and isinstance(path, str)
            and isinstance(line, int)
            and isinstance(commit, str)
        ):
            return None
        lines.append(f"{rule} {path}:{line} (commit {commit[:8]})")
    return lines


class WorkflowsStep:
    """Audit GitHub Actions workflows and actions in the repository with zizmor, offline."""

    name = "workflows"
    tier: Tier = "full"

    def run(self, context: CheckContext) -> StepResult:
        argv = (
            *("uv", "tool", "run", "--from", ZIZMOR, "zizmor"),
            *("--offline", "--no-progress", "--strict-collection", "--format", "plain", "."),
        )
        result = run_tool(context.repo.root, argv, 600)
        if result is not None and not result.timed_out and result.returncode == _ZIZMOR_NO_INPUTS:
            return not_applicable(self.name, "no workflows or actions found")
        return command_result(self.name, argv, result, _ZIZMOR_FINDINGS)
