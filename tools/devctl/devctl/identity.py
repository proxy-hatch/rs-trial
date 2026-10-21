"""devctl as: an agent's GitHub writes, made as the Worker App or the Reviewer App.

Agent identity is on when .rapid-ship/project.toml records both Apps (Spec 1 section 6). Each run
mints one installation token, keeps it in memory and gives it to one child only: gh through
GH_TOKEN, or git push through askpass.py. Nothing here writes the token to disk, puts it in argv
or prints it. devctl refuses (exit 1) before it mints a token wherever it can.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from functools import partial
from pathlib import Path

from devctl import process
from devctl.github.api import json_object
from devctl.github.app import DevctlError, GitHubAccess, project_access, run_git, run_guarded
from devctl.process import ProcessResult
from devctl.repo import Repo
from devctl.settings import ROLES, App, ProjectSettings, Role, load_project

ACTIONS = ("push", "gh")
GH_COMMANDS = frozenset({"pr", "issue", "api"})
GH_TIMEOUT_SECONDS = 600.0
PUSH_TIMEOUT_SECONDS = 300.0
OFF = (
    "Agent identity is off: .rapid-ship/project.toml records no Apps. Push and open pull "
    "requests under your own sign-in, or turn Agent identity on with rs-setup: run rapid-ship "
    "app, install both Apps, and record each App's ID and slug under [github]"
)
# GitHub's reason when an App without the Workflows permission pushes a workflow change.
_WORKFLOW_REFUSAL = "refusing to allow a GitHub App to create or update workflow"


class Refusal(Exception):
    """devctl refuses the request; the command exits 1."""


def gh_refusal(arguments: Sequence[str]) -> str | None:
    """Why devctl will not run gh with these arguments, or None.

    gh auth can print the token, and gh alias and gh extension can run other code with it, so
    only gh pr, gh issue and gh api run (Spec 1 section 6).
    """
    if not arguments or arguments[0] not in GH_COMMANDS:
        named = f"gh {arguments[0]}" if arguments else "gh with no command"
        return f"{named} is not allowed; devctl as runs only gh pr, gh issue and gh api"
    admin = any(a == "--admin" or a.startswith("--admin=") for a in arguments)
    if arguments[0] == "pr" and "merge" in arguments[1:] and admin:
        return "gh pr merge --admin is not allowed; merge through the ruleset"
    return None


def push_hint(stderr: str) -> str | None:
    """What to do when GitHub refused an App's push of a workflow change, else None."""
    if _WORKFLOW_REFUSAL not in stderr:
        return None
    return (
        "neither App may push a change under .github/workflows/; that change is in the "
        "Protected set, so the owner pushes it under their own sign-in"
    )


def _hide(text: str, token: str) -> str:
    return text.replace(token, "[token]") if token else text


def _relay(tool: str, result: ProcessResult, token: str, timeout_seconds: float) -> int:
    """Write the tool's output with the token hidden, and return its exit code.

    A timeout or cut output is 2: devctl cannot show the tool's whole answer.
    """
    sys.stdout.write(_hide(result.stdout, token))
    sys.stderr.write(_hide(result.stderr, token))
    if result.timed_out:
        sys.stderr.write(f"devctl: {tool} did not finish within {timeout_seconds:g} seconds\n")
        return 2
    if result.stdout_truncated or result.stderr_truncated:
        sys.stderr.write(
            f"devctl: {tool} printed more than {process.MAX_OUTPUT_BYTES} bytes, so its output "
            f"was cut; it exited {result.returncode}\n"
        )
        return 2
    return result.returncode


def _current_branch(root: Path) -> str:
    """The branch HEAD names; a Refusal when HEAD is detached."""
    result = process.run_process(["git", "symbolic-ref", "--quiet", "--short", "HEAD"], root, 30)
    if result.timed_out or result.stdout_truncated or result.stderr_truncated:
        raise DevctlError("git symbolic-ref did not finish")
    if result.returncode == 1:
        raise Refusal("HEAD is detached; switch to the branch to push")
    if result.returncode != 0:
        raise DevctlError(f"git symbolic-ref failed: {process.first_line(result.stderr)}")
    return result.stdout.strip()


def _default_branch(access: GitHubAccess, token: str) -> str:
    found = json_object(
        access.api(token).request("GET", f"/repos/{access.repository}"), "the repository"
    )
    name = found.get("default_branch")
    if not isinstance(name, str) or not name:
        raise DevctlError(f"GitHub did not name the default branch of {access.repository}")
    return name


def _push(repo: Repo, role: Role, arguments: Sequence[str], access: GitHubAccess, app: App) -> int:
    if role != "worker":
        raise Refusal("only the Worker App pushes; run ./devctl as worker push")
    if arguments:
        raise Refusal(f"push takes no arguments and never forces; got {' '.join(arguments)}")
    branch = _current_branch(repo.root)
    token = access.token(role, app.id)
    if branch == _default_branch(access, token):
        raise Refusal(f"{branch} is the default branch; push a task branch and open a pull request")
    url = access.remote
    result = run_git(
        repo.root,
        ["push", url, f"refs/heads/{branch}:refs/heads/{branch}"],
        token,
        url=url,
        timeout_seconds=PUSH_TIMEOUT_SECONDS,
    )
    code = _relay("git push", result, token, PUSH_TIMEOUT_SECONDS)
    hint = push_hint(result.stderr)
    if hint is not None:
        sys.stderr.write(f"devctl: {hint}\n")
    return code


def _gh(role: Role, arguments: Sequence[str], access: GitHubAccess, app: App) -> int:
    refusal = gh_refusal(arguments)
    if refusal is not None:
        raise Refusal(refusal)
    token = access.token(role, app.id)
    # The owner's proxy settings, but none of the owner's gh sign-in or hosts: gh acts as the App.
    env = {
        **process.passed_variables(process.NETWORK_VARIABLES),
        "GH_TOKEN": token,
        "GH_REPO": access.repository,
        "GH_PROMPT_DISABLED": "1",
    }
    try:
        result = process.run_process(["gh", *arguments], Path.cwd(), GH_TIMEOUT_SECONDS, env=env)
    except OSError:  # gh is not installed, or cannot start
        raise DevctlError("gh could not start; install gh") from None
    return _relay("gh", result, token, GH_TIMEOUT_SECONDS)


def _act(
    repo: Repo,
    project: ProjectSettings,
    role: Role,
    action: str,
    arguments: Sequence[str],
    app: App,
) -> int:
    access = project_access(project)
    try:
        if action == "push":
            return _push(repo, role, arguments, access, app)
        return _gh(role, arguments, access, app)
    except Refusal as refusal:
        sys.stderr.write(f"devctl as: refused: {refusal}\n")
        return 1


def run_as(
    repo: Repo, project: ProjectSettings, role: Role, action: str, arguments: Sequence[str]
) -> int:
    """Run one devctl as request and return its exit code (Spec 1 section 6).

    2 while Agent identity is off, or when no token can be minted or the tool cannot start; 1
    when devctl refuses; otherwise the exit code of gh or git.
    """
    app = project.worker_app if role == "worker" else project.reviewer_app
    if app is None or not project.agent_identity:
        sys.stderr.write(f"devctl: {OFF}\n")
        return 2
    return run_guarded(partial(_act, repo, project, role, action, arguments, app), root=repo.root)


def as_command(args: argparse.Namespace) -> int:
    repo = Repo.discover(Path.cwd())  # cli.main maps RepoError and SettingsError to exit 2
    return run_as(repo, load_project(repo.root), args.role, args.action, args.arguments)


def register(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = commands.add_parser(
        "as",
        help="Run git push or gh as the Worker App or the Reviewer App (Agent identity)",
        description="push: push the current branch to the branch of the same name on GitHub, "
        "as the Worker App; it never forces. gh: run gh pr, gh issue or gh api as the role's "
        "App. Exit 1 when devctl refuses, 2 when it cannot mint a token or start the tool, "
        "otherwise the tool's exit code.",
    )
    parser.add_argument("role", choices=ROLES)
    parser.add_argument("action", choices=ACTIONS)
    parser.add_argument(
        "arguments", nargs=argparse.REMAINDER, help="gh's arguments; push takes none"
    )
    parser.set_defaults(handler=as_command)
