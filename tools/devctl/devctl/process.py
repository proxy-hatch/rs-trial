"""Bounded, credential-free subprocess execution with process-group supervision."""

from __future__ import annotations

import math
import os
import signal
import subprocess
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

MAX_OUTPUT_BYTES = 1024 * 1024
_GRACE_SECONDS = 0.15
# How a tool that reaches the network finds the owner's proxy and extra certificate
# authorities. A caller passes them with passed_variables; none is a token devctl mints.
NETWORK_VARIABLES = (
    "HTTPS_PROXY",
    "https_proxy",
    "HTTP_PROXY",
    "http_proxy",
    "ALL_PROXY",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "NODE_EXTRA_CA_CERTS",
)


@dataclass(frozen=True)
class ProcessResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    stdout_truncated: bool = False
    stderr_truncated: bool = False


def safe_environment(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Pass PATH and HOME, which tools need to start, and nothing else unless supplied."""
    result = {"PATH": os.environ.get("PATH", os.defpath), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
    if "HOME" in os.environ:
        result["HOME"] = os.environ["HOME"]
    if env:
        result.update(env)
    return result


def passed_variables(names: Iterable[str]) -> dict[str, str]:
    """This process's value of each named variable that is set, for a child's env."""
    return {name: os.environ[name] for name in names if name in os.environ}


def first_line(text: str) -> str:
    """The first line of text, cut to 300 characters: what a message quotes of a tool's stderr."""
    lines = text.strip().splitlines()
    return lines[0][:300] if lines else ""


class _Capture:
    def __init__(self, stream: IO[bytes]) -> None:
        self.stream = stream
        self.data = bytearray()
        self.truncated = False

    def read(self) -> None:
        try:
            while chunk := self.stream.read(65536):
                remaining = MAX_OUTPUT_BYTES - len(self.data)
                self.data.extend(chunk[:remaining])
                self.truncated |= len(chunk) > remaining
        finally:
            self.stream.close()

    def text(self) -> str:
        # Invalid UTF-8 is replaced with ASCII to keep the byte bound as well.
        return bytes(self.data).decode("utf-8", errors="replace").replace("\ufffd", "?")


def _kill_group(pid: int, sig: int) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        pass
    except PermissionError as denied:
        # Darwin reports EPERM for a group whose members have exited but are not yet reaped:
        # a TERM-killed group, or a child that outlived its leader, as git upload-pack outlives
        # a failed fetch from a local repository. Probe existence only; never repeat the
        # signal or accept a persistent denial.
        deadline = time.monotonic() + 0.05
        while True:
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                return
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise denied from None
                time.sleep(0.001)
            else:
                raise denied from None


def _try_kill_group(pid: int, sig: int, failures: list[OSError]) -> None:
    """Signal the group; keep a refusal for the caller, since a timer thread cannot raise it."""
    try:
        _kill_group(pid, sig)
    except OSError as refused:
        failures.append(refused)


def _check_arguments(argv: Sequence[object], timeout_seconds: float) -> None:
    """Refuse arguments run_process cannot supervise.

    Each argv item is checked at run time, because a caller without type checks can pass any
    object.
    """
    if not argv or any(not isinstance(a, str) or "\0" in a for a in argv):
        raise ValueError("argv must contain non-NUL strings and a command")
    if not argv[0] or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("command and positive finite timeout_seconds required")
    if os.name != "posix":
        raise RuntimeError("process-group supervision requires POSIX")


def _start_writer(stdin: IO[bytes] | None, input_text: str) -> threading.Thread:
    """Feed input_text to the child on a thread, so a full pipe cannot block the timeout."""
    if stdin is None:
        raise RuntimeError("process pipes were not created")
    content = input_text.encode("utf-8")

    def write_input() -> None:
        try:
            stdin.write(content)
            stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            stdin.close()

    writer = threading.Thread(target=write_input, daemon=True)
    writer.start()
    return writer


def run_process(
    argv: Sequence[str],
    cwd: Path,
    timeout_seconds: float,
    env: Mapping[str, str] | None = None,
    input_text: str | None = None,
) -> ProcessResult:
    """Run argv without a shell. Supervise the entire POSIX process group.

    Retention is capped separately for stdout/stderr; readers keep draining after
    the cap, and the result says which stream was cut. Native execution is a
    project-authorized trust boundary, not a filesystem sandbox. The child gets PATH
    and HOME from the caller; callers pass any other environment explicitly.
    """
    _check_arguments(argv, timeout_seconds)
    start = time.monotonic()
    proc = subprocess.Popen(  # noqa: S603  # rs-exception: EX-1
        argv,
        cwd=cwd,
        env=safe_environment(env),
        start_new_session=True,
        stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if proc.stdout is None or proc.stderr is None:
        raise RuntimeError("process pipes were not created")
    captures = [_Capture(proc.stdout), _Capture(proc.stderr)]
    readers = [threading.Thread(target=c.read, daemon=True) for c in captures]
    for reader in readers:
        reader.start()
    writer = None if input_text is None else _start_writer(proc.stdin, input_text)
    expired = threading.Event()
    failures: list[OSError] = []

    def expire() -> None:
        # A refused SIGTERM must not stop the SIGKILL, or the timeout would never end the group.
        expired.set()
        _try_kill_group(proc.pid, signal.SIGTERM, failures)
        time.sleep(_GRACE_SECONDS)
        _try_kill_group(proc.pid, signal.SIGKILL, failures)

    timer = threading.Timer(timeout_seconds, expire)
    timer.daemon = True
    timer.start()
    try:
        proc.wait()  # Blocks in waitpid; on timeout the timer ends the group.
    finally:
        timer.cancel()
        timer.join()
        if not expired.is_set():
            # Descendants can outlive the leader and hold the pipes, so signal the group.
            # After a timeout the timer already did, and macOS refuses SIGTERM to a group
            # of killed, unreaped processes.
            _kill_group(proc.pid, signal.SIGTERM)
        proc.poll()  # Reap an exited leader before signaling remaining descendants.
        _kill_group(proc.pid, signal.SIGKILL)
        proc.wait()
        if writer:
            writer.join(timeout=1)
        for reader in readers:
            reader.join(timeout=1)
    if failures:  # The timer has finished: timer.join() ran above.
        raise failures[0]
    return ProcessResult(
        list(argv),
        proc.returncode,
        captures[0].text(),
        captures[1].text(),
        time.monotonic() - start,
        expired.is_set(),
        captures[0].truncated,
        captures[1].truncated,
    )
