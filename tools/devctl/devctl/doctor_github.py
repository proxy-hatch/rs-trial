"""doctor's GitHub items: the ruleset, CODEOWNERS, repository settings and, while Agent identity is
on, both Apps.

Repository settings are read with the owner's gh sign-in (OwnerGet, always GET). Each App is read
as the App itself: its installation, and one installation token that proves its Keychain key can
mint. Those POSTs are the only writes, they change nothing in the repository, and each token is
discarded unprinted. doctor_rules.py holds the checks that compare what GitHub returned.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Mapping
from urllib.parse import quote

from devctl.doctor_items import DoctorContext, Item, OwnerReadError, Status
from devctl.doctor_rules import codeowners_problems, installation_problems, ruleset_problems
from devctl.github.api import GitHubError, json_list, json_object
from devctl.github.app import (
    AppError,
    AppNotInstalled,
    GitHubAccess,
    KeychainMissing,
    guarded_message,
    installation,
)
from devctl.settings import PROJECT, App, Role

PLAN_LIMIT = "Upgrade to GitHub Pro"  # GitHub's 403 for rulesets on a private repository
REPOSITORY_ITEMS = ("ruleset", "CODEOWNERS", "delete branch on merge", "fork PR workflow approval")
IDENTITY = "agent identity"  # the area of the App items
_LABELS: dict[Role, str] = {"worker": "Worker", "reviewer": "Reviewer"}


def _github(name: str, status: Status, detail: str) -> Item:
    return Item("github", name, status, detail)


def _identity(name: str, status: Status, detail: str) -> Item:
    return Item(IDENTITY, name, status, detail)


def _unreadable(name: str, what: str, error: OwnerReadError) -> Item:
    http = f" (HTTP {error.status})" if error.status else ""
    return _github(name, "unknown", f"cannot read {what}: {error.message}{http}")


def _guarded(name: str, build: Callable[[], Item]) -> Item:
    """build() with a response that is not the shape GitHub documents made unknown."""
    try:
        return build()
    except GitHubError as error:
        return _github(name, "unknown", str(error))


def _field(repo: Mapping[str, object], key: str) -> str:
    value = repo.get(key)
    if isinstance(value, str) and value:
        return value
    raise GitHubError(None, f"the repository has no {key}")


def _mode(every_pr: bool) -> str:
    return "Walk" if every_pr else "the Protected set (every_pr = false)"


def _repository_rulesets(ctx: DoctorContext, repository: str) -> list[Mapping[str, object]]:
    """The repository's own rulesets on branches, each read by ID; inherited ones are left out."""
    listed = json_list(
        ctx.owner_get(f"repos/{repository}/rulesets?targets=branch"), "the ruleset list"
    )
    found: list[Mapping[str, object]] = []
    for entry in listed:
        summary = json_object(entry, "a listed ruleset")
        if summary.get("source_type") != "Repository":
            continue
        ruleset_id = summary.get("id")
        if isinstance(ruleset_id, bool) or not isinstance(ruleset_id, int):
            raise GitHubError(None, "a listed ruleset has no numeric id")
        found.append(
            json_object(ctx.owner_get(f"repos/{repository}/rulesets/{ruleset_id}"), "the ruleset")
        )
    return found


def _ruleset_item(ctx: DoctorContext, repository: str, repo: Mapping[str, object]) -> Item:
    name = "ruleset"
    default = _field(repo, "default_branch")
    try:
        rulesets = _repository_rulesets(ctx, repository)
    except OwnerReadError as error:
        if error.status == 403 and PLAN_LIMIT in error.message:
            detail = "rulesets need a public repository or a paid GitHub plan (GitHub returned 403)"
            return _github(name, "missing", detail)
        return _unreadable(name, "the rulesets", error)
    if any("bypass_actors" not in ruleset for ruleset in rulesets):
        detail = "the bypass list is visible only to users who can edit the ruleset"
        return _github(name, "unknown", detail)
    every_pr, identity = ctx.project.escalation.every_pr, ctx.project.agent_identity
    problems = ruleset_problems(rulesets, default, every_pr=every_pr, agent_identity=identity)
    if problems:
        return _github(name, "missing", "; ".join(problems))
    if ruleset_problems(rulesets, default, every_pr=True, agent_identity=True):
        detail = (
            f"an active ruleset on {default} has rs-setup's settings without the review "
            "requirement, as Flight without Agent identity needs; the Protected set is advice"
        )
        return _github(name, "ok", detail)
    return _github(name, "ok", f"an active ruleset on {default} has every setting rs-setup applies")


def _decoded(contents: Mapping[str, object]) -> str:
    content = contents.get("content")
    if contents.get("encoding") != "base64" or not isinstance(content, str):
        raise GitHubError(None, "the CODEOWNERS contents are not base64 text")
    try:
        # GitHub wraps the base64 at 60 characters.
        return base64.b64decode(content.replace("\n", ""), validate=True).decode()
    except ValueError:  # binascii.Error and UnicodeDecodeError
        raise GitHubError(None, "the CODEOWNERS contents are not UTF-8 text") from None


def _codeowners_item(ctx: DoctorContext, repository: str, repo: Mapping[str, object]) -> Item:
    name = "CODEOWNERS"
    # An organisation cannot be a code owner, so the repository's owner is never the default.
    code_owner = ctx.project.code_owner
    if code_owner is None:
        detail = f'set [github] code_owner in {PROJECT}: "@login" or "@organisation/team"'
        return _github(name, "missing", detail)
    default = _field(repo, "default_branch")
    # GitHub reads CODEOWNERS from the base branch, so that is the file doctor reads.
    path = f"repos/{repository}/contents/.github/CODEOWNERS?ref={quote(default, safe='/')}"
    try:
        text = _decoded(json_object(ctx.owner_get(path), "the CODEOWNERS contents"))
    except OwnerReadError as error:
        if error.status == 404:
            return _github(name, "missing", f".github/CODEOWNERS is not on {default}")
        return _unreadable(name, ".github/CODEOWNERS", error)
    every_pr = ctx.project.escalation.every_pr
    problems = codeowners_problems(text, every_pr, code_owner, ctx.project.stack)
    if problems:
        return _github(name, "missing", "; ".join(problems))
    return _github(name, "ok", f".github/CODEOWNERS on {default} matches {_mode(every_pr)}")


def _delete_branch_item(repo: Mapping[str, object]) -> Item:
    name = "delete branch on merge"
    setting = repo.get("delete_branch_on_merge")
    if setting is True:
        return _github(name, "ok", "head branches are deleted after merge")
    if setting is False:
        detail = "turn on Automatically delete head branches in the repository settings"
        return _github(name, "missing", detail)
    return _github(name, "unknown", "GitHub did not report delete_branch_on_merge")


def _fork_item(ctx: DoctorContext, repository: str, repo: Mapping[str, object]) -> Item:
    name = "fork PR workflow approval"
    visibility = _field(repo, "visibility")
    if visibility != "public":
        return _github(name, "ok", f"not applicable: {visibility} repository")
    path = f"repos/{repository}/actions/permissions/fork-pr-contributor-approval"
    try:
        setting = json_object(ctx.owner_get(path), "the fork approval setting")
    except OwnerReadError as error:
        return _unreadable(name, "the fork pull request approval setting", error)
    policy = setting.get("approval_policy")
    if policy == "all_external_contributors":
        return _github(name, "ok", "workflows from all outside contributors need approval")
    found = policy if isinstance(policy, str) else "unreadable"
    detail = (
        f"approval_policy is {found}; require approval for all outside contributors "
        "under Settings, Actions, General"
    )
    return _github(name, "missing", detail)


def _repository_items(ctx: DoctorContext, repository: str) -> list[Item]:
    try:
        repo = json_object(ctx.owner_get(f"repos/{repository}"), "the repository")
    except OwnerReadError as error:
        return [_unreadable(name, f"repos/{repository}", error) for name in REPOSITORY_ITEMS]
    except GitHubError as error:
        return [_github(name, "unknown", str(error)) for name in REPOSITORY_ITEMS]
    return [
        _guarded("ruleset", lambda: _ruleset_item(ctx, repository, repo)),
        _guarded("CODEOWNERS", lambda: _codeowners_item(ctx, repository, repo)),
        _guarded("delete branch on merge", lambda: _delete_branch_item(repo)),
        _guarded("fork PR workflow approval", lambda: _fork_item(ctx, repository, repo)),
    ]


def _installation_item(
    ctx: DoctorContext, access: GitHubAccess, repository: str, role: Role, app: App
) -> Item:
    name = f"{_LABELS[role]} App installation"
    try:
        found = installation(
            role, repository, app.id, keychain=access.keychain, api_url=access.api_url
        )
    except AppError as error:
        return _identity(name, "missing", guarded_message(error, ctx.root))
    except GitHubError as error:
        return _identity(name, "unknown", str(error))
    problems: list[str] = []
    granted = installation_problems(found, role)
    if granted:
        problems.append(f"permissions differ from Spec 1 section 3: {'; '.join(granted)}")
    slug = found.get("app_slug")
    if slug != app.slug:
        # Spec 3 finds agent pull requests by this slug, so a wrong one hides them all.
        shown = slug if isinstance(slug, str) else "unreadable"
        problems.append(f"the installed App's slug is {shown}, but {PROJECT} records {app.slug}")
    if problems:
        return _identity(name, "missing", "; ".join(problems))
    detail = f"{app.slug} is installed on {repository} with exactly the {role} App's permissions"
    return _identity(name, "ok", detail)


def _key_item(ctx: DoctorContext, access: GitHubAccess, role: Role, app: App) -> Item:
    name = f"{_LABELS[role]} App key"
    try:
        access.token(role, app.id)  # proves the key; the token is dropped unprinted
    except KeychainMissing as error:
        return _identity(name, "missing", guarded_message(error, ctx.root))
    except AppNotInstalled:
        return _identity(name, "unknown", "key found; minting needs the App installed")
    except AppError as error:
        return _identity(name, "unknown", f"cannot mint a token: {error}")
    except GitHubError as error:
        return _identity(name, "unknown", str(error))
    detail = "Keychain item found; token minted (changes nothing in the repository)"
    return _identity(name, "ok", detail)


def _app_items(ctx: DoctorContext, repository: str) -> list[Item]:
    """Both Apps' installation and key items; none while Agent identity is off."""
    worker, reviewer = ctx.project.worker_app, ctx.project.reviewer_app
    if worker is None or reviewer is None:
        return []
    apps: tuple[tuple[Role, App], ...] = (("worker", worker), ("reviewer", reviewer))
    access = ctx.access
    if access is None:
        detail = "no GitHub access is set up for this run, so the Apps cannot be checked"
        return [
            *(_identity(f"{_LABELS[r]} App installation", "unknown", detail) for r, _ in apps),
            *(_identity(f"{_LABELS[r]} App key", "unknown", detail) for r, _ in apps),
        ]
    return [
        *(_installation_item(ctx, access, repository, role, app) for role, app in apps),
        *(_key_item(ctx, access, role, app) for role, app in apps),
    ]


def github_items(ctx: DoctorContext) -> list[Item]:
    """The repository's settings, and both Apps while Agent identity is on. Nothing in the
    repository changes."""
    repository = ctx.project.repository
    if repository is None:
        return [_github("repository", "missing", f"repository is not set in {PROJECT}")]
    return [*_repository_items(ctx, repository), *_app_items(ctx, repository)]
