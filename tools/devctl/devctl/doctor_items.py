"""What every doctor item shares: its type, its context, and the owner's one way to read GitHub.

doctor reads GitHub with the owner's gh sign-in, always as GET (gh api defaults to POST once it
is given a field). Each tool doctor runs gets, besides PATH, HOME and the locale, only what
owner_environment names for it, so a sign-in the owner's shell holds counts as it would there.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from devctl import process
from devctl.github.app import GitHubAccess
from devctl.process import ProcessResult
from devctl.settings import ProjectSettings

Status = Literal["ok", "missing", "unknown"]
TOOL_TIMEOUT_SECONDS = 30.0
_GH_TIMEOUT_SECONDS = 60.0
_HTTP_STATUS = re.compile(r"\(HTTP (\d{3})\)")
# Where the owner's own settings and temporary files live, for every tool doctor runs.
_OWNER_VARIABLES = (
    "USER",
    "LOGNAME",
    "TMPDIR",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
    "XDG_CACHE_HOME",
)
_AWS = (
    "AWS_PROFILE",
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_BEARER_TOKEN_BEDROCK",
    "AWS_CONFIG_FILE",
    "AWS_SHARED_CREDENTIALS_FILE",
)
_GOOGLE_CLOUD = (
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
    "CLOUDSDK_CONFIG",
)
# The variables that hold or locate each tool's sign-in, so any sign-in method is ok (Spec 1
# section 6): a subscription, an API key, a gateway or a cloud Provider. A tool gets only its
# own. GH_HOST and the enterprise tokens stay out, because doctor reads github.com, and devctl
# as's RAPID_SHIP_GIT_TOKEN and GIT_ASKPASS are never passed.
SIGN_IN_VARIABLES: Mapping[str, tuple[str, ...]] = {
    "gh": ("GH_TOKEN", "GITHUB_TOKEN", "GH_CONFIG_DIR"),
    "claude": (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_CUSTOM_HEADERS",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL",
        "ANTHROPIC_BEDROCK_BASE_URL",
        "ANTHROPIC_VERTEX_BASE_URL",
        "ANTHROPIC_VERTEX_PROJECT_ID",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CONFIG_DIR",
        "CLOUD_ML_REGION",
        *_AWS,
        *_GOOGLE_CLOUD,
    ),
}


@dataclass(frozen=True)
class Item:
    area: str  # toolchain, guardrails, kit, github, agent identity, skills
    name: str
    status: Status
    detail: str


OwnerGet = Callable[[str], object]  # GET with the owner's gh sign-in; raises OwnerReadError


class OwnerReadError(Exception):
    """gh could not read a path: its HTTP status when it printed one, and what it said."""

    def __init__(self, status: int | None, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class DoctorContext:
    root: Path
    project: ProjectSettings
    owner_get: OwnerGet
    access: GitHubAccess | None  # None while Agent identity is off


def owner_environment(tool: str) -> dict[str, str]:
    """What doctor passes tool from the owner's environment: the network settings, where the
    owner's files live, and the tool's own sign-in variables."""
    return process.passed_variables(
        (*process.NETWORK_VARIABLES, *_OWNER_VARIABLES, *SIGN_IN_VARIABLES.get(tool, ()))
    )


def _json_message(stdout: str) -> str:
    """The `message` of the JSON body that gh api printed for a failed request, or ""."""
    try:
        body = json.loads(stdout)
    except ValueError:
        return ""
    message = body.get("message") if isinstance(body, dict) else None
    return process.first_line(message) if isinstance(message, str) else ""


def gh_get(path: str) -> object:
    """GET path with the owner's gh sign-in and return the parsed JSON. Raises OwnerReadError."""
    try:
        result = process.run_process(
            ["gh", "api", "--method", "GET", path],
            Path(tempfile.gettempdir()),
            _GH_TIMEOUT_SECONDS,
            env=owner_environment("gh"),
        )
    except OSError:  # gh is not installed, or cannot start
        raise OwnerReadError(None, "gh could not start") from None
    if result.timed_out:
        raise OwnerReadError(None, "gh api timed out")
    if result.stdout_truncated:
        raise OwnerReadError(None, f"gh api output exceeded {process.MAX_OUTPUT_BYTES} bytes")
    if result.returncode != 0:
        status = _HTTP_STATUS.search(result.stderr)
        message = _json_message(result.stdout) or process.first_line(result.stderr)
        raise OwnerReadError(
            int(status[1]) if status else None, message or f"gh api exited {result.returncode}"
        )
    try:
        return json.loads(result.stdout)
    except ValueError:
        raise OwnerReadError(None, "gh api printed no JSON") from None


def run_installed(
    tool: str, *args: str, cwd: Path | None = None, timeout: float = TOOL_TIMEOUT_SECONDS
) -> ProcessResult | None:
    """Run an installed tool with owner_environment(tool); None when it is not on PATH.

    A tool that cannot start gets exit 126, as a shell reports one.
    """
    path = shutil.which(tool)
    if path is None:
        return None
    argv = [path, *args]
    try:
        return process.run_process(
            argv, cwd or Path(tempfile.gettempdir()), timeout, env=owner_environment(tool)
        )
    except OSError:
        return ProcessResult(argv, 126, "", "", 0.0)


def finished(result: ProcessResult) -> bool:
    """False for a command that timed out or whose output was cut: it proves nothing."""
    return not (result.timed_out or result.stdout_truncated or result.stderr_truncated)
