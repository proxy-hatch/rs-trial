"""GitHub App keys, App JWTs, installation tokens and token-bearing git.

Each App's private key lives in the macOS Keychain, in an item named for the role and the App's
ID: rapid-ship/<role>-app/<App ID>. A command reads it, signs a short-lived JWT and mints one
installation token, restricted to one repository and the App's permissions. Keys and tokens stay
in memory: nothing here writes one to disk, puts one in argv or prints one. git receives a token
only from askpass.py, through the environment of that one git command.
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import jwt
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from devctl import process
from devctl.check.kit import KitLockError, rapid_ship_command
from devctl.github import askpass
from devctl.github.api import API_URL, GitHubApi, GitHubError, json_object
from devctl.process import ProcessResult
from devctl.settings import ProjectSettings, Role, SettingsError

# Spec 1 section 3. Every token asks for exactly these, never the installation's whole grant.
APP_PERMISSIONS: dict[Role, dict[str, str]] = {
    "worker": {
        "contents": "write",
        "pull_requests": "write",
        "issues": "write",
        "metadata": "read",
    },
    "reviewer": {"pull_requests": "write", "contents": "read", "metadata": "read"},
}
_KEYCHAIN_TIMEOUT_SECONDS = 120.0
_ITEM_NOT_FOUND = 44  # the exit code of `security find-generic-password` for a missing item
_REWRITE_RULES = r"^url\..*\.(push)?insteadof$"  # git lowercases section and key names


class DevctlError(Exception):
    """The base for errors that make a devctl command exit 2."""


class AppError(DevctlError):
    """An App, its key or its installation is missing or unusable."""


class KeychainMissing(AppError):
    """The Keychain has no item for an App's key.

    With the role, the message names the rapid-ship commands that store the key.
    """

    def __init__(self, service: str, role: Role | None = None) -> None:
        self.service = service
        self.role = role
        super().__init__(self.message())

    def message(self, app_command: str = "rapid-ship app") -> str:
        """The text, with app_command as the command that runs `rapid-ship app`."""
        missing = f"Keychain item {self.service} not found"
        if self.role is None:
            return missing
        # Plan 4 named the item without the App ID; `rapid-ship app` re-stores such a key.
        return (
            f"{missing}; run {app_command}, which re-stores a key kept under "
            f"rapid-ship/{self.role}-app, or {app_command} --import-key {self.role}"
        )


class AppNotInstalled(AppError):
    """GitHub has no installation of an App on the repository."""


class GitError(DevctlError):
    """A token-bearing git command cannot run safely, or git failed."""


class Keychain(Protocol):
    def read(self, service: str) -> bytes: ...


def _run_security(argv: list[str]) -> ProcessResult:
    return process.run_process(argv, Path(tempfile.gettempdir()), _KEYCHAIN_TIMEOUT_SECONDS)


def _decode_secret(output: str) -> bytes:
    text = output.rstrip("\n")
    # `security -w` prints the item as lowercase hex when any byte is not printable, as a
    # PEM's newlines are (keychain_find.c:429-437).
    data = bytes.fromhex(text) if re.fullmatch(r"(?:[0-9a-f]{2})+", text) else text.encode()
    if not data.startswith(b"-----BEGIN "):
        raise AppError("the Keychain item is not a PEM private key")
    return data


class MacKeychain:
    """Reads generic-password items with `security`, the tool that `rapid-ship app` wrote them with.

    No message includes the tool's output, which is the key.
    """

    def __init__(self, run: Callable[[list[str]], ProcessResult] | None = None) -> None:
        self._run = run or _run_security

    def read(self, service: str) -> bytes:
        argv = ["security", "find-generic-password", "-s", service, "-w"]
        try:
            result = self._run(argv)
        except OSError:  # `security` is missing or cannot start
            raise AppError(f"could not read Keychain item {service}") from None
        cut = result.timed_out or result.stdout_truncated or result.stderr_truncated
        if not cut and result.returncode == _ITEM_NOT_FOUND:
            raise KeychainMissing(service)
        if cut or result.returncode != 0:
            raise AppError(f"could not read Keychain item {service}")
        return _decode_secret(result.stdout)


def keychain_service(role: Role, app_id: int) -> str:
    """The Keychain item that holds the key of the role's App with app_id.

    The ID in the name lets one machine hold the keys of several Apps for one role.
    """
    return f"rapid-ship/{role}-app/{app_id}"


def keychain_private_key(role: Role, app_id: int, keychain: Keychain | None = None) -> bytes:
    """The PEM private key of the role's App, whose ID [github] records."""
    store = keychain if keychain is not None else MacKeychain()
    service = keychain_service(role, app_id)
    try:
        return store.read(service)
    except KeychainMissing:
        raise KeychainMissing(service, role) from None  # the store knows no role to advise on


def _signing_key(private_key: bytes) -> RSAPrivateKey:
    # Loaded here, so an unusable key is an AppError: PyJWT raises AttributeError for a
    # public key, and its other errors are not devctl's.
    try:
        key = load_pem_private_key(private_key, password=None)
    except (ValueError, TypeError, UnsupportedAlgorithm):
        raise AppError("the App key is not a readable, unencrypted PEM private key") from None
    if not isinstance(key, RSAPrivateKey):
        raise AppError("the App key is not an RSA private key")
    return key


def app_jwt(private_key: bytes, app_id: int, now: float | None = None) -> str:
    issued = int(time.time() if now is None else now)
    # 60 s back for clock drift; 9 minutes ahead stays under GitHub's 10-minute limit.
    # PyJWT requires a string issuer.
    return jwt.encode(
        {"iat": issued - 60, "exp": issued + 540, "iss": str(app_id)},
        _signing_key(private_key),
        algorithm="RS256",
    )


def _app_client(role: Role, app_id: int, keychain: Keychain | None, api_url: str) -> GitHubApi:
    """A REST client that acts as the App itself, with a fresh JWT."""
    return GitHubApi(
        app_jwt(keychain_private_key(role, app_id, keychain), app_id), base_url=api_url
    )


def _installation(app: GitHubApi, role: Role, repository: str) -> dict[str, object]:
    try:
        found = app.request("GET", f"/repos/{repository}/installation")
    except GitHubError as error:
        if error.status == 404:
            raise AppNotInstalled(f"the {role} App is not installed on {repository}") from None
        raise
    return json_object(found, "the installation")


def installation(
    role: Role,
    repository: str,
    app_id: int,
    *,
    keychain: Keychain | None = None,
    api_url: str = API_URL,
) -> dict[str, object]:
    """GitHub's record of the role's App installation on repository, permissions included."""
    return _installation(_app_client(role, app_id, keychain, api_url), role, repository)


def installation_token(
    role: Role,
    repository: str,
    app_id: int,
    *,
    keychain: Keychain | None = None,
    api_url: str = API_URL,
) -> str:
    # One Keychain read and one JWT per mint. An item written by `security` trusts it, so
    # the read shows no dialog, and any process the owner runs can read it (spec section 6).
    app = _app_client(role, app_id, keychain, api_url)
    found = _installation(app, role, repository)
    installation_id = found.get("id")
    if isinstance(installation_id, bool) or not isinstance(installation_id, int):
        raise GitHubError(None, "the installation has no numeric id")
    issued = app.request(
        "POST",
        f"/app/installations/{installation_id}/access_tokens",
        {"repositories": [repository.split("/", 1)[1]], "permissions": APP_PERMISSIONS[role]},
    )
    token = json_object(issued, "the token response").get("token")
    if not isinstance(token, str) or not token:
        raise AppError("GitHub returned no installation token")
    return token


@dataclass(frozen=True)
class GitHubAccess:
    """Where a project's GitHub repository is, and how its Apps' keys are read."""

    repository: str  # "owner/name"
    api_url: str = API_URL
    remote_url: str | None = None  # None means https://github.com/<repository>.git
    keychain: Keychain | None = None  # None means MacKeychain()

    @property
    def remote(self) -> str:
        return self.remote_url or f"https://github.com/{self.repository}.git"

    def token(self, role: Role, app_id: int) -> str:
        """Mint one installation token. A command calls this once and keeps the token."""
        return installation_token(
            role, self.repository, app_id, keychain=self.keychain, api_url=self.api_url
        )

    def api(self, token: str) -> GitHubApi:
        """A REST client for a token that token() already minted."""
        return GitHubApi(token, base_url=self.api_url)


def project_access(project: ProjectSettings) -> GitHubAccess:
    if project.repository is None:
        raise AppError(".rapid-ship/project.toml names no repository; run rs-setup")
    return GitHubAccess(project.repository)


def guarded_message(error: Exception, root: Path | None) -> str:
    """The error's text. With root, a missing Keychain item's remedy pins rapid-ship's version."""
    if root is None or not isinstance(error, KeychainMissing):
        return str(error)
    # The Keychain code does not know the project, so the pinned command is added here.
    try:
        return error.message(rapid_ship_command(root, "app"))
    except KitLockError:
        return str(error)


def run_guarded(action: Callable[[], int], *, root: Path | None = None) -> int:
    """Run a command's action. A known error goes to stderr, and the command exits 2.

    With root, a missing Keychain item names the rapid-ship commands that store the key.
    """
    try:
        return action()
    except (DevctlError, GitHubError, SettingsError, OSError) as error:
        sys.stderr.write(f"devctl: {guarded_message(error, root)}\n")
        return 2


def git_environment(token: str) -> dict[str, str]:
    """What a token-bearing git command gets on top of run_process's safe environment.

    git sees only these, the caller's proxy and certificate settings, HOME, LANG and LC_ALL, so
    the caller's GIT_TRACE*, GIT_CURL_VERBOSE and GIT_CONFIG_* never reach it.
    """
    path = os.environ.get("PATH", os.defpath)
    # askpass.py's shebang finds python3 on PATH, so this interpreter's directory goes first and
    # the python3 is devctl's own. sys.executable is empty when Python cannot find itself; PATH
    # then stays as it is, because Path("").parent would add ".", git's working directory.
    if sys.executable:
        path = os.pathsep.join([str(Path(sys.executable).parent), path])
    return {
        **process.passed_variables(process.NETWORK_VARIABLES),
        "PATH": path,
        "GIT_ASKPASS": str(askpass.ASKPASS_PATH),
        "GIT_TERMINAL_PROMPT": "0",
        askpass.ASKPASS_VARIABLE: token,
    }


def token_options(url: str) -> tuple[str, ...]:
    # -c http.extraHeader= clears only unscoped headers; a header scoped to this URL, as
    # actions/checkout writes, survives it. A reset scoped to the exact URL clears both
    # (Checked facts).
    return (*askpass.GIT_TOKEN_OPTIONS, "-c", f"http.{url}.extraHeader=")


def require_exact_remote(cwd: Path, url: str) -> None:
    """Refuse url when an insteadOf or pushInsteadOf rule in cwd's git config would rewrite it."""
    # `git ls-remote --get-url` applies insteadOf but not pushInsteadOf, so read the rules.
    found = process.run_process(["git", "config", "-z", "--get-regexp", _REWRITE_RULES], cwd, 30)
    cut = found.timed_out or found.stdout_truncated or found.stderr_truncated
    if not cut and found.returncode == 1:  # no rule at all
        return
    if cut or found.returncode != 0:
        raise GitError(f"could not read git config: {process.first_line(found.stderr)}")
    for entry in filter(None, found.stdout.split("\0")):
        key, _, prefix = entry.partition("\n")
        # git matches the value as a URL prefix, so an empty value rewrites every URL.
        if url.startswith(prefix):
            raise GitError(f"git config rewrites {url} through {key}; remove that rule")


def run_git(
    cwd: Path, args: Sequence[str], token: str, *, url: str, timeout_seconds: float = 120.0
) -> ProcessResult:
    """Run `git <args>` against url, with token as git's only credential.

    url is the remote the command talks to and must be one of args. The caller reads the exit
    code and output; a GitError means git did not run.
    """
    if not os.access(askpass.ASKPASS_PATH, os.X_OK):
        raise GitError(
            f"{askpass.ASKPASS_PATH} is not executable; check kit.toml's executable list"
        )
    if url not in args:
        raise ValueError("run_git needs the remote URL among its arguments")
    require_exact_remote(cwd, url)
    return process.run_process(
        ["git", *token_options(url), *args], cwd, timeout_seconds, env=git_environment(token)
    )
