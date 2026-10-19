"""Run external commands safely: argv lists (never a shell), deadlines and bounded output.

Every system integration (``osascript``, ``mdfind``, ``shortcuts``, ``defaults``, ``ps`` …) goes
through a :class:`CommandRunner`, so tests can swap in :class:`FakeRunner` and never spawn a process.

Guarantees of :meth:`CommandRunner.run`:

* ``argv`` is executed directly (``shell=False``). Only :func:`axpilot.system.shell.run_shell`
  starts a shell, and it does so explicitly as ``[shell, "-lc", command]``.
* The child runs in its own session (process group) with stdin closed unless ``input_text`` is
  given. When the deadline passes, the **whole group** is killed with ``SIGKILL`` and the result
  has ``timed_out=True``, ``exit_code=-1`` and whatever output was produced until then. The
  deadline covers the process *and* its output pipes, so a background child that keeps stdout
  open cannot hang the caller.
* Output is decoded as UTF-8 (invalid bytes become U+FFFD). Each stream is cut to ``max_output``
  characters followed by a truncation marker, and memory stays bounded however much the child
  writes.
* A missing executable gives exit code ``127`` and a non-executable one ``126`` (the shell
  conventions), so callers handle them like any other failed command. A child killed by a
  signal reports ``128 + signal number``, which keeps ``-1`` unambiguous for timeouts.
"""

from __future__ import annotations

import errno
import logging
import math
import os
import re
import shlex
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from axpilot.errors import AXPilotError, ErrorCode

__all__ = [
    "DEFAULT_MAX_OUTPUT",
    "EXIT_NOT_EXECUTABLE",
    "EXIT_NOT_FOUND",
    "EXIT_TIMED_OUT",
    "CommandResult",
    "CommandRunner",
    "FakeCall",
    "FakeHandler",
    "FakeRunner",
    "ensure_success",
    "excerpt",
    "output_records",
    "truncate_output",
]

_log = logging.getLogger(__name__)

DEFAULT_MAX_OUTPUT = 20_000
"""Default per-stream output limit, in characters."""
EXIT_NOT_FOUND = 127
"""Exit code reported when the executable does not exist (shell convention)."""
EXIT_NOT_EXECUTABLE = 126
"""Exit code reported when the executable exists but cannot be run (shell convention)."""
EXIT_TIMED_OUT = -1
"""Exit code reported when the command was killed because it ran past its deadline."""

STDERR_EXCERPT_CHARS = 500
_READ_CHUNK = 65_536
_MAX_UTF8_BYTES_PER_CHAR = 4
_KILL_GRACE_S = 2.0
_TRUNCATION_RE = re.compile(r"\n\[output truncated after \d+ characters; \d+ bytes in total\]\Z")


@dataclass(slots=True)
class CommandResult:
    """The outcome of one command.

    ``exit_code`` is ``-1`` when the command timed out (``timed_out`` is then true), ``127``
    when the program was not found and ``128 + n`` when it was killed by signal ``n``.
    ``truncated`` is true when either stream was cut to the output limit.
    """

    argv: list[str]
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    truncated: bool = False

    @property
    def ok(self) -> bool:
        """True if the command finished in time with exit code 0."""
        return self.exit_code == 0 and not self.timed_out

    @property
    def program(self) -> str:
        """The executable's base name (``"osascript"`` for ``/usr/bin/osascript``)."""
        return Path(self.argv[0]).name if self.argv else "command"


# --------------------------------------------------------------------------------------------
# Output helpers shared by the integrations


def truncate_output(text: str, max_output: int) -> tuple[str, bool]:
    """Cut ``text`` to ``max_output`` characters plus a truncation marker.

    Returns ``(text, truncated)``. The marker names the limit and the original size in UTF-8
    bytes, e.g. ``"\\n[output truncated after 20000 characters; 81234 bytes in total]"``.
    """
    data = text.encode("utf-8", errors="replace")
    return _finish_stream(data, len(data), max_output)


def output_records(text: str, sep: str = "\n") -> list[str]:
    """Split command output into complete records separated by ``sep``.

    A trailing empty record (output ending with ``sep``) is dropped. If ``text`` ends with the
    runner's truncation marker, the marker and the possibly partial last record are dropped too,
    so parsers only ever see whole records.
    """
    if not sep:
        raise AXPilotError(ErrorCode.INVALID_ARGUMENT, "The record separator must not be empty.")
    match = _TRUNCATION_RE.search(text)
    if match:
        return text[: match.start()].split(sep)[:-1]
    parts = text.split(sep)
    if parts and parts[-1] == "":
        parts.pop()
    return parts


def excerpt(text: str, limit: int = STDERR_EXCERPT_CHARS) -> str:
    """``text`` stripped and shortened to at most ``limit`` characters (with ``…``)."""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + "…"


def _first_line(text: str) -> str:
    stripped = text.strip()
    return stripped.splitlines()[0].strip() if stripped else ""


def ensure_success(
    result: CommandResult,
    *,
    what: str,
    hint: str | None = None,
    timeout_s: float | None = None,
    missing_hint: str | None = None,
) -> CommandResult:
    """Return ``result`` if it succeeded, otherwise raise the matching :class:`AXPilotError`.

    * timed out → ``TIMEOUT``
    * exit 127 (program missing) → ``NOT_SUPPORTED`` (with ``missing_hint`` if given)
    * any other non-zero exit → ``COMMAND_FAILED`` with the exit code and a stderr excerpt
      (at most 500 characters) in ``details``

    ``what`` names the operation for the message (``"Spotlight search"``); ``hint`` is used for
    timeouts and failures. Arguments are never included in the error because they may contain
    user data.
    """
    program = result.program
    if result.timed_out:
        after = f" after {timeout_s:g} s" if timeout_s else ""
        raise AXPilotError(
            ErrorCode.TIMEOUT,
            f"{what} timed out{after}.",
            hint=hint or "The command took too long and was stopped. Retry, or narrow the request.",
            details={"command": program},
        )
    if result.exit_code == EXIT_NOT_FOUND:
        raise AXPilotError(
            ErrorCode.NOT_SUPPORTED,
            f"{what} needs `{program}`, which is not available on this system.",
            hint=missing_hint or "This feature uses a macOS command-line tool, so it only works on macOS.",
            details={"command": program, "stderr": excerpt(result.stderr)},
        )
    if result.exit_code != 0:
        reason = _first_line(result.stderr) or _first_line(result.stdout) or "no error output"
        raise AXPilotError(
            ErrorCode.COMMAND_FAILED,
            f"{what} failed (exit {result.exit_code}): {excerpt(reason, 300)}",
            hint=hint,
            details={
                "command": program,
                "exit_code": result.exit_code,
                "stderr": excerpt(result.stderr),
            },
        )
    return result


def _marker(max_output: int, total_bytes: int) -> str:
    return f"\n[output truncated after {max_output} characters; {total_bytes} bytes in total]"


def _finish_stream(data: bytes, total_bytes: int, max_output: int) -> tuple[str, bool]:
    """Decode captured bytes and apply the character limit. ``total_bytes`` ≥ ``len(data)``."""
    text = data.decode("utf-8", errors="replace")
    if total_bytes <= len(data) and len(text) <= max_output:
        return text, False
    return text[:max_output] + _marker(max_output, total_bytes), True


# --------------------------------------------------------------------------------------------
# Argument validation (shared by the real and the fake runner)


def _invalid(message: str) -> AXPilotError:
    return AXPilotError(ErrorCode.INVALID_ARGUMENT, message)


def _validate_argv(argv: Sequence[str]) -> list[str]:
    if isinstance(argv, str | bytes):
        raise _invalid("argv must be a list of arguments, not a single string (no shell is used).")
    args = list(argv)
    if not args:
        raise _invalid("argv must not be empty.")
    for arg in args:
        if not isinstance(arg, str):
            raise _invalid(f"Every argv element must be a string, got {type(arg).__name__}.")
        if "\x00" in arg:
            raise _invalid("Command arguments cannot contain NUL characters.")
    if not args[0]:
        raise _invalid("The program name (argv[0]) must not be empty.")
    return args


def _validate_limits(timeout_s: float, max_output: int) -> None:
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, int | float):
        raise _invalid("timeout_s must be a number of seconds.")
    if not math.isfinite(timeout_s) or timeout_s <= 0:
        raise _invalid(f"timeout_s must be a positive number of seconds, got {timeout_s!r}.")
    if isinstance(max_output, bool) or not isinstance(max_output, int) or max_output < 0:
        raise _invalid(f"max_output must be a non-negative integer, got {max_output!r}.")


def _validate_env(env: Mapping[str, str] | None) -> dict[str, str] | None:
    if env is None:
        return None
    clean: dict[str, str] = {}
    for key, value in env.items():
        if not isinstance(key, str) or not key or "=" in key or "\x00" in key:
            raise _invalid(f"Invalid environment variable name: {key!r}.")
        if not isinstance(value, str) or "\x00" in value:
            raise _invalid(f"The value of environment variable {key} must be a string without NUL.")
        clean[key] = value
    return clean


def _resolve_cwd(cwd: str | None) -> str | None:
    if cwd is None:
        return None
    if "\x00" in cwd:
        raise _invalid("The working directory cannot contain NUL characters.")
    path = Path(cwd).expanduser()
    if not path.is_dir():
        raise _invalid(f"Working directory does not exist or is not a directory: {path}")
    if not os.access(path, os.X_OK):
        raise _invalid(f"Working directory is not accessible: {path}")
    return str(path)


# --------------------------------------------------------------------------------------------
# The real runner


class _PipeReader:
    """Drains one output pipe on a daemon thread, keeping at most ``limit`` bytes."""

    def __init__(self, stream: IO[bytes], limit: int, name: str) -> None:
        self._stream = stream
        self._limit = limit
        self._data = bytearray()
        self._total = 0
        self._lock = threading.Lock()
        self._thread = threading.Thread(target=self._drain, name=f"axpilot-{name}-reader", daemon=True)
        self._thread.start()

    def _drain(self) -> None:
        read = getattr(self._stream, "read1", self._stream.read)
        try:
            while chunk := read(_READ_CHUNK):
                with self._lock:
                    self._total += len(chunk)
                    room = self._limit - len(self._data)
                    if room > 0:
                        self._data += chunk[:room]
        except (OSError, ValueError) as exc:  # ValueError: the pipe was closed under us
            _log.debug("stopped reading command output: %s", exc)
        finally:
            try:
                self._stream.close()
            except OSError as exc:
                _log.debug("could not close a command output pipe: %s", exc)

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def join(self, timeout_s: float) -> None:
        self._thread.join(max(timeout_s, 0.0))

    def result(self, max_output: int) -> tuple[str, bool]:
        with self._lock:
            data, total = bytes(self._data), self._total
        return _finish_stream(data, total, max_output)


def _start_stdin_writer(stream: IO[bytes], data: bytes) -> threading.Thread:
    def write() -> None:
        try:
            stream.write(data)
        except BrokenPipeError:
            _log.debug("the command exited before reading all of its input")
        except (OSError, ValueError) as exc:
            _log.debug("could not write command input: %s", exc)
        finally:
            try:
                stream.close()
            except OSError as exc:  # flushing the last buffered bytes can hit a closed pipe
                _log.debug("could not close command input: %s", exc)

    thread = threading.Thread(target=write, name="axpilot-stdin-writer", daemon=True)
    thread.start()
    return thread


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    """SIGKILL the child's whole process group (it leads its own session)."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        _log.debug("process group %d already exited", proc.pid)
    except PermissionError as exc:
        _log.warning("could not kill process group %d (%s); killing the child only", proc.pid, exc)
        try:
            proc.kill()
        except ProcessLookupError:
            _log.debug("process %d already exited", proc.pid)


def _reap(proc: subprocess.Popen[bytes]) -> None:
    try:
        proc.wait(timeout=_KILL_GRACE_S)
    except subprocess.TimeoutExpired:
        _log.warning("process %d did not exit %.0f s after SIGKILL", proc.pid, _KILL_GRACE_S)


def _normalize_returncode(code: int | None) -> int:
    if code is None:
        return EXIT_TIMED_OUT
    return 128 - code if code < 0 else code


def _remaining(deadline: float) -> float:
    return max(deadline - time.monotonic(), 0.0)


class CommandRunner:
    """Runs commands as real subprocesses. Stateless and thread-safe."""

    def run(
        self,
        argv: Sequence[str],
        *,
        input_text: str | None = None,
        timeout_s: float = 30,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        max_output: int = DEFAULT_MAX_OUTPUT,
    ) -> CommandResult:
        """Run ``argv`` and return its result. Never raises for a failing command.

        ``input_text`` is written to stdin as UTF-8 (stdin is ``/dev/null`` otherwise). ``env``
        entries are *merged* over the current environment. ``cwd`` may start with ``~``.
        Raises ``INVALID_ARGUMENT`` for malformed arguments and ``COMMAND_FAILED`` only if the
        operating system refuses to start the process for an unexpected reason.
        """
        args = _validate_argv(argv)
        _validate_limits(timeout_s, max_output)
        extra_env = _validate_env(env)
        workdir = _resolve_cwd(cwd)
        child_env = {**os.environ, **extra_env} if extra_env is not None else None
        stdin_data = None if input_text is None else input_text.encode("utf-8", errors="replace")
        try:
            proc = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL if stdin_data is None else subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=workdir,
                env=child_env,
                start_new_session=True,
            )
        except FileNotFoundError:
            return CommandResult(args, EXIT_NOT_FOUND, "", f"{args[0]}: command not found")
        except OSError as exc:
            if isinstance(exc, PermissionError) or exc.errno == errno.ENOEXEC:
                reason = exc.strerror or "cannot execute"
                return CommandResult(args, EXIT_NOT_EXECUTABLE, "", f"{args[0]}: {reason}")
            raise AXPilotError(
                ErrorCode.COMMAND_FAILED,
                f"Could not start `{Path(args[0]).name}`: {exc.strerror or exc}",
            ) from exc
        return self._supervise(proc, args, stdin_data, timeout_s, max_output)

    @staticmethod
    def _supervise(
        proc: subprocess.Popen[bytes],
        args: list[str],
        stdin_data: bytes | None,
        timeout_s: float,
        max_output: int,
    ) -> CommandResult:
        deadline = time.monotonic() + timeout_s
        byte_limit = max_output * _MAX_UTF8_BYTES_PER_CHAR
        if proc.stdout is None or proc.stderr is None:  # pragma: no cover - we always pass PIPE
            raise AXPilotError(ErrorCode.INTERNAL, "The command's output pipes are missing.")
        out = _PipeReader(proc.stdout, byte_limit, "stdout")
        err = _PipeReader(proc.stderr, byte_limit, "stderr")
        if stdin_data is not None and proc.stdin is not None:
            _start_stdin_writer(proc.stdin, stdin_data)

        timed_out = False
        try:
            proc.wait(timeout=timeout_s)
            out.join(_remaining(deadline))
            err.join(_remaining(deadline))
            # The child exited but something it started still holds the pipes open.
            timed_out = out.alive or err.alive
        except subprocess.TimeoutExpired:
            timed_out = True
        except BaseException:
            _kill_group(proc)
            raise

        if timed_out:
            _kill_group(proc)
            _reap(proc)
            out.join(_KILL_GRACE_S)
            err.join(_KILL_GRACE_S)
            _log.info("command %s timed out after %g s", Path(args[0]).name, timeout_s)

        stdout, stdout_cut = out.result(max_output)
        stderr, stderr_cut = err.result(max_output)
        return CommandResult(
            argv=args,
            exit_code=EXIT_TIMED_OUT if timed_out else _normalize_returncode(proc.returncode),
            stdout=stdout,
            stderr=stderr,
            timed_out=timed_out,
            truncated=stdout_cut or stderr_cut,
        )


# --------------------------------------------------------------------------------------------
# The fake runner (tests)


@dataclass(slots=True)
class FakeCall:
    """One command a :class:`FakeRunner` was asked to run, with every keyword argument."""

    argv: list[str]
    input_text: str | None
    timeout_s: float
    cwd: str | None
    env: dict[str, str] | None
    max_output: int


FakeHandler = Callable[[FakeCall], CommandResult]
"""Computes the result of a :class:`FakeCall` (for responses that depend on the arguments)."""


class FakeRunner(CommandRunner):
    """A :class:`CommandRunner` for tests: canned results by argv prefix, every call recorded.

    ``register(["defaults", "read"], stdout="1\\n")`` answers every command whose argv starts
    with ``defaults read``. When several prefixes match, the longest (most specific) wins;
    registering the same prefix again replaces it; the empty prefix matches everything.
    Unregistered commands do not raise: they return exit code 127 with an explanatory stderr,
    exactly like a missing program. Output limits are applied as in the real runner.
    """

    def __init__(self) -> None:
        self.calls: list[FakeCall] = []
        self._routes: dict[tuple[str, ...], FakeHandler] = {}
        self._lock = threading.Lock()

    def register(
        self,
        prefix: Sequence[str],
        *,
        stdout: str = "",
        stderr: str = "",
        exit_code: int = 0,
        timed_out: bool = False,
        handler: FakeHandler | None = None,
    ) -> None:
        """Answer commands starting with ``prefix``, with fixed output or via ``handler``.

        ``timed_out=True`` simulates a timeout (the exit code becomes ``-1``).
        """
        if isinstance(prefix, str | bytes):
            raise _invalid("prefix must be a list of arguments, not a single string.")
        key = tuple(prefix)
        if not all(isinstance(part, str) for part in key):
            raise _invalid("Every prefix element must be a string.")

        def canned(call: FakeCall) -> CommandResult:
            return CommandResult(
                argv=list(call.argv),
                exit_code=EXIT_TIMED_OUT if timed_out else exit_code,
                stdout=stdout,
                stderr=stderr,
                timed_out=timed_out,
            )

        with self._lock:
            self._routes[key] = handler or canned

    def run(
        self,
        argv: Sequence[str],
        *,
        input_text: str | None = None,
        timeout_s: float = 30,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        max_output: int = DEFAULT_MAX_OUTPUT,
    ) -> CommandResult:
        """Record the call and return the canned result of the most specific matching prefix."""
        args = _validate_argv(argv)
        _validate_limits(timeout_s, max_output)
        extra_env = _validate_env(env)
        call = FakeCall(args, input_text, timeout_s, cwd, extra_env, max_output)
        with self._lock:
            self.calls.append(call)
            handler = self._match(args)
        if handler is None:
            shown = excerpt(shlex.join(args), 200)
            return CommandResult(args, EXIT_NOT_FOUND, "", f"FakeRunner: no response registered for {shown}")
        return self._bounded(handler(call), max_output)

    def _match(self, args: list[str]) -> FakeHandler | None:
        best: tuple[str, ...] | None = None
        for prefix in self._routes:
            matches = len(prefix) <= len(args) and tuple(args[: len(prefix)]) == prefix
            if matches and (best is None or len(prefix) > len(best)):
                best = prefix
        return None if best is None else self._routes[best]

    @staticmethod
    def _bounded(result: CommandResult, max_output: int) -> CommandResult:
        stdout, stdout_cut = truncate_output(result.stdout, max_output)
        stderr, stderr_cut = truncate_output(result.stderr, max_output)
        return CommandResult(
            argv=list(result.argv),
            exit_code=result.exit_code,
            stdout=stdout,
            stderr=stderr,
            timed_out=result.timed_out,
            truncated=result.truncated or stdout_cut or stderr_cut,
        )

    @property
    def last_call(self) -> FakeCall:
        """The most recent call. Raises ``LookupError`` if nothing ran yet."""
        with self._lock:
            if not self.calls:
                raise LookupError("FakeRunner has not run any command yet.")
            return self.calls[-1]

    def calls_for(self, prefix: Sequence[str]) -> list[FakeCall]:
        """Recorded calls whose argv starts with ``prefix``."""
        key = list(prefix)
        with self._lock:
            return [call for call in self.calls if call.argv[: len(key)] == key]
