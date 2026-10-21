"""Minimal GitHub REST client.

It never follows a redirect, which could carry the token elsewhere.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from urllib.parse import urlsplit

API_URL = "https://api.github.com"
API_VERSION = "2026-03-10"
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
TIMEOUT_SECONDS = 30.0
MAX_MESSAGE_CHARACTERS = 300
_LOOPBACK = {"127.0.0.1", "localhost"}
_DEFAULT_PORTS = {"https": 443, "http": 80}


class GitHubError(Exception):
    def __init__(self, status: int | None, message: str) -> None:
        prefix = f"GitHub returned {status}" if status else "GitHub request failed"
        super().__init__(f"{prefix}: {message}")
        self.status = status


def json_object(value: object, what: str) -> dict[str, object]:
    """Narrow a parsed JSON value to an object, or raise GitHubError with no status."""
    if isinstance(value, dict):
        return value
    raise GitHubError(None, f"{what} is not a JSON object")


def json_list(value: object, what: str) -> list[object]:
    """Narrow a parsed JSON value to a list, or raise GitHubError with no status."""
    if isinstance(value, list):
        return value
    raise GitHubError(None, f"{what} is not a JSON list")


@dataclass(frozen=True)
class _Response:
    status: int
    reason: str
    data: bytes


class GitHubApi:
    def __init__(self, token: str, *, base_url: str = API_URL) -> None:
        parts = urlsplit(base_url)
        loopback = parts.scheme == "http" and parts.hostname in _LOOPBACK
        if not (parts.scheme == "https" or loopback) or not parts.hostname:
            raise ValueError("the GitHub API URL must use https (http only on 127.0.0.1)")
        self._token = token
        self._scheme = parts.scheme
        self._host = parts.hostname
        self._port = parts.port or _DEFAULT_PORTS[parts.scheme]
        self._prefix = parts.path.rstrip("/")

    def __repr__(self) -> str:
        return f"GitHubApi({self._host})"

    def request(self, method: str, path: str, body: object | None = None) -> object:
        """Send one request and return its parsed JSON (None for an empty body)."""
        return self._parse(self._send(method, self._prefix + path, body))

    def _send(self, method: str, target: str, body: object | None) -> _Response:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "devctl",
        }
        payload = None
        if body is not None:
            payload = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        factory = HTTPSConnection if self._scheme == "https" else HTTPConnection
        connection = factory(self._host, self._port, timeout=TIMEOUT_SECONDS)
        try:
            connection.request(method, target, payload, headers)
            reply = connection.getresponse()
            data = reply.read(MAX_RESPONSE_BYTES + 1)
            return _Response(reply.status, reply.reason, data)
        except (OSError, HTTPException, ValueError) as failure:
            # Only the class name: a message could quote a header, and so the token.
            raise GitHubError(None, type(failure).__name__) from None
        finally:
            connection.close()

    def _parse(self, response: _Response) -> object:
        if len(response.data) > MAX_RESPONSE_BYTES:
            raise GitHubError(response.status, f"response is over {MAX_RESPONSE_BYTES} bytes")
        if not 200 <= response.status < 300:
            raise GitHubError(response.status, self._clean(_message(response)))
        if not response.data:
            return None
        try:
            return json.loads(response.data)
        except (ValueError, RecursionError):
            raise GitHubError(response.status, "response is not JSON") from None

    def _clean(self, text: str) -> str:
        """Hide the token first, then cut, so a cut cannot leave part of it behind."""
        if self._token:
            text = text.replace(self._token, "[token]")
        return text[:MAX_MESSAGE_CHARACTERS]


def _message(response: _Response) -> str:
    """GitHub's own message for an error reply, else the HTTP reason."""
    try:
        parsed = json.loads(response.data)
    except (ValueError, RecursionError):
        return response.reason
    message = parsed.get("message") if isinstance(parsed, dict) else None
    return message if isinstance(message, str) and message else response.reason
