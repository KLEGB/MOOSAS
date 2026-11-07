"""Shared execution primitives for native simulation tools."""

from __future__ import annotations

from pathlib import Path
import subprocess
from typing import TextIO

from .engine import (
    NativeExecution,
    NativeEngine,
    NativeEngineProcessError,
    NativeEngineTimeoutError,
    SubprocessEngine,
)


CommandResult = NativeExecution


class CommandError(RuntimeError):
    """Raised when a native-tool command cannot be completed."""

    def __init__(self, command: tuple[str, ...], returncode: int, stdout: str, stderr: str):
        self.command = command
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(
            f"Command failed with exit code {returncode}: {' '.join(command)}\n{stderr}"
        )


class CommandTimeoutError(TimeoutError):
    """Raised when a native-tool command exceeds its configured timeout."""

    def __init__(self, command: tuple[str, ...], timeout_seconds: float, stdout: str, stderr: str):
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(f"Command timed out after {timeout_seconds}s: {' '.join(command)}")


class Runner:
    """Execute a native command without mutating process-global state."""

    result_type = CommandResult
    error_type = CommandError
    timeout_error_type = CommandTimeoutError

    def __init__(self, timeout_seconds: float = 300.0, engine: NativeEngine | None = None):
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive.")
        self.timeout_seconds = timeout_seconds
        self.engine = engine or SubprocessEngine()

    def run_command(
        self,
        command: tuple[str, ...] | list[str],
        cwd: str | Path | None = None,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
    ) -> CommandResult:
        """Run one command and return captured diagnostics or raise a typed error."""
        command_tuple = tuple(str(part) for part in command)
        try:
            completed = self.engine.execute(
                command_tuple,
                cwd=str(cwd) if cwd is not None else None,
                stdin=stdin,
                stdout=stdout,
                timeout_seconds=self.timeout_seconds,
            )
        except NativeEngineTimeoutError as error:
            raise self.timeout_error_type(
                command_tuple,
                self.timeout_seconds,
                error.stdout,
                error.stderr,
            ) from error
        except NativeEngineProcessError as error:
            raise self.error_type(command_tuple, error.returncode, error.stdout, error.stderr) from error

        return self.result_type(completed.command, completed.returncode, completed.stdout, completed.stderr)
