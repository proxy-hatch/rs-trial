"""devctl's command line: core commands, plus the project's verbs from devctl_project."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

import devctl_project
from devctl import __version__, doctor, identity
from devctl.check import ChangeError, exit_code, resolve_change, run_check
from devctl.check.steps import CheckContext, Status, StepResult
from devctl.repo import Repo, RepoError
from devctl.settings import SettingsError, load_project

Handler = Callable[[argparse.Namespace], int]

LABELS: dict[Status, str] = {
    "passed": "passed",
    "failed": "failed",
    "blocked": "blocked",
    "not_applicable": "not applicable",
}


def render_text(results: Sequence[StepResult]) -> str:
    lines: list[str] = []
    for result in results:
        lines.append(f"{LABELS[result.status]:<15}{result.name}")
        lines.extend(f"    {detail}" for detail in result.details)
    return "".join(f"{line}\n" for line in lines)


def render_json(results: Sequence[StepResult]) -> str:
    data = [{"name": r.name, "status": r.status, "details": list(r.details)} for r in results]
    return json.dumps(data, indent=2) + "\n"


def check_command(args: argparse.Namespace) -> int:
    try:
        repo = Repo.discover(Path.cwd())
        base, pr_body = resolve_change(repo, args.base, args.pr_body, os.environ)
        context = CheckContext(repo, load_project(repo.root), base, pr_body)
        results = run_check(context, args.tier)
    except (RepoError, SettingsError, ChangeError, OSError, ValueError) as error:
        sys.stderr.write(f"devctl check: {error}\n")
        return 2
    sys.stdout.write(render_json(results) if args.json else render_text(results))
    return exit_code(results)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="devctl", description="This repository's developer CLI.")
    parser.add_argument("--version", action="version", version=f"devctl {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="<command>", required=True)
    check = commands.add_parser(
        "check",
        help="Run the project's checks",
        description="Exit 0 when every applicable step passed, 1 when one failed, 2 when one "
        "could not run.",
    )
    check.add_argument(
        "--tier",
        choices=("fast", "full"),
        default="full",
        help="fast runs the quick steps; full (the default) adds the slower ones",
    )
    check.add_argument("--json", action="store_true", help="print the results as JSON")
    check.add_argument(
        "--base", metavar="REV", help="compare against this commit instead of finding the base"
    )
    check.add_argument(
        "--pr-body", type=Path, metavar="PATH", help="read the pull request body from this file"
    )
    check.set_defaults(handler=check_command)
    doctor.register(commands)
    identity.register(commands)
    devctl_project.register(commands)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler: Handler | None = getattr(args, "handler", None)
    if handler is None:
        parser.error(f"{args.command} does not set a handler")
    try:
        return handler(args)
    except (RepoError, SettingsError) as error:
        sys.stderr.write(f"devctl: {error}\n")
        return 2
