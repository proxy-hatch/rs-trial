#!/usr/bin/env python3
"""GIT_ASKPASS helper. git runs this file to read the App token from RAPID_SHIP_GIT_TOKEN.

The token reaches git only through this process's environment and git's prompt pipe.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

ASKPASS_VARIABLE = "RAPID_SHIP_GIT_TOKEN"
ASKPASS_PATH = Path(__file__).resolve()
# An empty credential.helper clears every helper configured before it, including gh's
# URL-scoped one, so the App token is neither replaced by nor stored in the owner's
# credentials. hooksPath=/dev/null keeps repository hooks from running with the token
# in their environment.
GIT_TOKEN_OPTIONS = ("-c", "credential.helper=", "-c", "core.hooksPath=/dev/null")


def answer(prompt: str, environ: Mapping[str, str]) -> str | None:
    token = environ.get(ASKPASS_VARIABLE)
    if not token:
        return None
    if prompt.startswith("Username"):
        return "x-access-token"
    if prompt.startswith("Password"):
        return token
    return None


def main(argv: Sequence[str]) -> int:
    reply = answer(argv[1] if len(argv) > 1 else "", os.environ)
    if reply is None:
        return 1
    sys.stdout.write(reply + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
