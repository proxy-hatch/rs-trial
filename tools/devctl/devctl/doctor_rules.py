"""What doctor expects of a ruleset, CODEOWNERS and an App installation.

These checks are pure: they take what GitHub returned and name each way it differs from Spec 1
section 3 (the Apps' permissions) and section 8 (the ruleset and CODEOWNERS). doctor_github.py
reads GitHub and builds the items; plan 6's test checks rs-setup's ruleset.json with
ruleset_problems.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from devctl.doctor_local import CI_JOB_NAME
from devctl.github.app import APP_PERMISSIONS
from devctl.settings import Role, Stack

ADMIN_ROLE_ID = 5  # the repository admin role; plan 4's live trial reads it back
GITHUB_ACTIONS_APP_ID = 15368  # gh api /apps/github-actions
# The Protected set of the seeded CODEOWNERS (Spec 1 section 4), in its order, with the devctl
# shim. Codex's wiring adds .codex/, and agy's widens .agents/skills/ to .agents/.
PROTECTED_PATHS = (
    ".github/",
    ".githooks/",
    "devctl",
    "tools/devctl/",
    ".agents/skills/",
    ".claude/",
    "**/AGENTS.md",
    "**/CLAUDE.md",
    ".rapid-ship/",
    ".gitleaks.toml",
    ".gitleaksignore",
    "zizmor.yml",
    "zizmor.yaml",
)
# The Python guardrail settings: every file Ruff, mypy, pytest and import-linter read settings
# from, in the template's last lines. Ruff reads the nearest ruff.toml, so those match at any depth.
PYTHON_PROTECTED_PATHS = (
    "pyproject.toml",
    "**/ruff.toml",
    "**/.ruff.toml",
    "mypy.ini",
    ".mypy.ini",
    "pytest.ini",
    ".pytest.ini",
    "pytest.toml",
    ".pytest.toml",
    "setup.cfg",
    "tox.ini",
    ".importlinter",
)
NO_APPROVAL = "no pull_request rule requires an approval"
NO_PULL_REQUEST_RULE = "the ruleset has no pull_request rule"
NO_RULESET = "no ruleset targets the default branch"
_COMMENT = re.compile(r"\s+#.*$")


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _strings(value: object) -> set[str]:
    return {s for s in value if isinstance(s, str)} if isinstance(value, list) else set()


def _target_problems(ruleset: Mapping[str, object], default_branch: str) -> list[str]:
    ref_name = _mapping(_mapping(ruleset.get("conditions")).get("ref_name"))
    names = {"~DEFAULT_BRANCH", "~ALL", f"refs/heads/{default_branch}"}
    if names & _strings(ref_name.get("include")) and not names & _strings(ref_name.get("exclude")):
        return []
    return ["the ruleset does not target the default branch"]


def _approves(count: object) -> bool:
    return isinstance(count, int) and not isinstance(count, bool) and count >= 1


def _pull_request_problems(params: Mapping[str, object], *, review: bool) -> list[str]:
    """The pull_request rule's problems. review=False leaves out the review requirement, which
    Flight without Agent identity drops: an approval, code-owner review and approval of the most
    recent push (Spec 1 section 8, step 8)."""
    holds = [
        (not review or _approves(params.get("required_approving_review_count")), NO_APPROVAL),
        (
            not review or params.get("require_code_owner_review") is True,
            "the pull_request rule does not require code-owner review",
        ),
        (
            params.get("dismiss_stale_reviews_on_push") is True,
            "the pull_request rule does not dismiss stale approvals on push",
        ),
        (
            not review or params.get("require_last_push_approval") is True,
            "the pull_request rule does not require approval of the most recent push",
        ),
        (
            params.get("allowed_merge_methods") == ["squash"],
            "the pull_request rule must allow squash merges only",
        ),
        (
            params.get("required_review_thread_resolution") is True,
            "the pull_request rule does not require review threads to be resolved",
        ),
    ]
    return [problem for ok, problem in holds if not ok]


def _status_check_problems(params: Mapping[str, object]) -> list[str]:
    checks = params.get("required_status_checks")
    bound = isinstance(checks, list) and any(
        isinstance(check, Mapping)
        and check.get("context") == CI_JOB_NAME
        and check.get("integration_id") == GITHUB_ACTIONS_APP_ID
        for check in checks
    )
    if bound:
        return []
    return [
        f"the {CI_JOB_NAME} status check is not bound to GitHub Actions "
        f"(integration {GITHUB_ACTIONS_APP_ID})"
    ]


def _bypass_problems(actors: object) -> list[str]:
    found = (
        [
            (a.get("actor_id"), a.get("actor_type"), a.get("bypass_mode"))
            if isinstance(a, Mapping)
            else None
            for a in actors
        ]
        if isinstance(actors, list)
        else None
    )
    if found == [(ADMIN_ROLE_ID, "RepositoryRole", "pull_request")]:
        return []
    return ["the bypass list must hold only the admin role in pull_request mode"]


def _rules(ruleset: Mapping[str, object]) -> dict[str, Mapping[str, object]]:
    """The ruleset's rules: type -> parameters, empty for a rule such as deletion that has none."""
    rules = ruleset.get("rules")
    found: dict[str, Mapping[str, object]] = {}
    for entry in rules if isinstance(rules, list) else []:
        kind = _mapping(entry).get("type")
        if isinstance(kind, str):
            found[kind] = _mapping(_mapping(entry).get("parameters"))
    return found


def _one_ruleset_problems(
    ruleset: Mapping[str, object], default_branch: str, *, review: bool
) -> list[str]:
    rules = _rules(ruleset)
    pull_request = rules.get("pull_request")
    # Without the rule, its other settings have nothing to describe.
    missing_rule = NO_APPROVAL if review else NO_PULL_REQUEST_RULE
    return [
        *_target_problems(ruleset, default_branch),
        *([] if ruleset.get("enforcement") == "active" else ["the ruleset is not active"]),
        *(
            [missing_rule]
            if pull_request is None
            else _pull_request_problems(pull_request, review=review)
        ),
        *_status_check_problems(rules.get("required_status_checks", {})),
        *([] if "non_fast_forward" in rules else ["force pushes are not blocked"]),
        *([] if "deletion" in rules else ["deletion is not blocked"]),
        *_bypass_problems(ruleset.get("bypass_actors")),
    ]


def _closest(
    rulesets: Sequence[Mapping[str, object]], default_branch: str, *, review: bool
) -> list[str]:
    """No problem when one ruleset has none, because GitHub applies every ruleset that matches.
    Otherwise the problems of the ruleset with the fewest."""
    found = [_one_ruleset_problems(r, default_branch, review=review) for r in rulesets]
    return [] if any(not problems for problems in found) else min(found, key=len)


def ruleset_problems(
    rulesets: Sequence[Mapping[str, object]],
    default_branch: str,
    *,
    every_pr: bool,
    agent_identity: bool,
) -> list[str]:
    """What keeps the default branch from matching Spec 1 section 8's ruleset for the Mode.

    rulesets are in the shape GET /repos/{o}/{r}/rulesets/{id} returns. Walk, and Flight with
    Agent identity, need the whole ruleset. Flight without Agent identity also accepts it without
    the review requirement, because the owner cannot approve a pull request from their own
    account (Spec 1 section 8, step 8).
    """
    if not rulesets:
        return [NO_RULESET]
    strict = _closest(rulesets, default_branch, review=True)
    if not strict or every_pr or agent_identity:
        return strict
    return _closest(rulesets, default_branch, review=False)


def _active_lines(text: str) -> list[tuple[str, tuple[str, ...]]]:
    """Each active CODEOWNERS line as (pattern, owners), the owners in lower case.

    GitHub compares logins, an organisation's included, without regard to case; doctor compares a
    team's name the same way. Blank lines and comments, whole-line or after the owners, are not
    active.
    """
    lines: list[tuple[str, tuple[str, ...]]] = []
    for raw in text.splitlines():
        line = _COMMENT.sub("", raw.strip())
        if line and not line.startswith("#"):
            pattern, *owners = line.split()
            lines.append((pattern, tuple(owner.lower() for owner in owners)))
    return lines


def _walk_problems(lines: Sequence[tuple[str, tuple[str, ...]]], code_owner: str) -> list[str]:
    wanted = f"* {code_owner}"
    if len(lines) != 1:
        return [f"Walk needs exactly one active CODEOWNERS line, {wanted}; found {len(lines)}"]
    if lines[0] != ("*", (code_owner.lower(),)):
        return [f"the one active CODEOWNERS line is not {wanted}"]
    return []


def _protected_set_problems(
    lines: Sequence[tuple[str, tuple[str, ...]]], code_owner: str, stack: Stack
) -> list[str]:
    problems: list[str] = []
    if any(pattern == "*" for pattern, _ in lines):
        problems.append(
            "with every_pr = false, CODEOWNERS must have no * line; only the Protected set is owned"
        )
    # The last line for a pattern is the one GitHub applies.
    owned = {pattern.removeprefix("/"): owners for pattern, owners in lines}
    required = (*PROTECTED_PATHS, *(PYTHON_PROTECTED_PATHS if stack == "python" else ()))
    unowned = [path for path in required if owned.get(path) != (code_owner.lower(),)]
    if unowned:
        problems.append(f"CODEOWNERS must name only {code_owner} for: {', '.join(unowned)}")
    return problems


def codeowners_problems(text: str, every_pr: bool, code_owner: str, stack: Stack) -> list[str]:
    """What keeps CODEOWNERS from matching the Mode (Spec 1 section 6): the * line when every_pr
    is true, the Protected set otherwise. code_owner is [github] code_owner, with its @: a login
    or an organisation's team."""
    lines = _active_lines(text)
    if every_pr:
        return _walk_problems(lines, code_owner)
    return _protected_set_problems(lines, code_owner, stack)


def installation_problems(found: Mapping[str, object], role: Role) -> list[str]:
    """How an installation's permissions differ from the role's, which must match exactly."""
    granted = found.get("permissions")
    if not isinstance(granted, Mapping):
        return ["the installation lists no permissions"]
    wanted = APP_PERMISSIONS[role]
    problems: list[str] = []
    for name, level in sorted(wanted.items()):
        has = granted.get(name)
        if has is None:
            problems.append(f"{name}: {level} is not granted")
        elif has != level:
            problems.append(f"{name} is {has}, not {level}")
    problems.extend(
        f"{name}: {has} must not be granted"
        for name, has in sorted(granted.items(), key=lambda entry: str(entry[0]))
        if name not in wanted
    )
    return problems
