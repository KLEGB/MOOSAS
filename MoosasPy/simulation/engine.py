"""Adapters for executing native simulation engines."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import subprocess
from typing import Callable, Protocol, TextIO


@dataclass(frozen=True)
class NativeExecution:
    command: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class NativeEngineError(RuntimeError):
    def __init__(self, command: tuple[str, ...], stdout: str = "", stderr: str = ""):
        self.command = command
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(stderr)


class NativeEngineProcessError(NativeEngineError):
    def __init__(self, command: tuple[str, ...], returncode: int, stdout: str, stderr: str):
        self.returncode = returncode
        super().__init__(command, stdout, stderr)


class NativeEngineTimeoutError(NativeEngineError):
    def __init__(self, command: tuple[str, ...], timeout_seconds: float, stdout: str, stderr: str):
        self.timeout_seconds = timeout_seconds
        super().__init__(command, stdout, stderr)


class NativeEngine(Protocol):
    """Execution boundary implemented by local or remote native providers."""

    name: str

    def open_binary_session(self, command, *, stdin, stdout, stderr, bufsize=0): ...

    def execute(
        self,
        command: tuple[str, ...],
        *,
        cwd: str | Path | None = None,
        stdin: TextIO | None = None,
        stdout: TextIO | None = None,
        timeout_seconds: float = 300.0,
    ) -> NativeExecution: ...


class SubprocessEngine:
    """Local-process implementation of :class:`NativeEngine`."""

    name = "subprocess"

    def __init__(self, run_process: Callable | None = None, *, env=None):
        self._run_process = run_process
        self.env = dict(env) if env is not None else None

    def open_binary_session(self, command, *, stdin, stdout, stderr, bufsize=0):
        """Open a persistent numeric-protocol process without text transcoding."""
        return subprocess.Popen(command, stdin=stdin, stdout=stdout, stderr=stderr, bufsize=bufsize,
                                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))

    def execute(self, command, *, cwd=None, stdin=None, stdout=None, timeout_seconds=300.0):
        run_process = self._run_process or subprocess.run
        launch_command = command
        launch_options = {}
        if self.env is not None:
            launch_options['env'] = self.env
        if os.name == 'nt' and self._run_process is None and Path(command[0]).is_file():
            # Legacy Radiance copies argv[0] into a short fixed-size buffer.
            # Preserve the audited command, but launch its exact application
            # path with a short program name in the native command line.
            application = Path(command[0]).resolve()
            launch_command = (application.name,*command[1:])
            launch_options['executable'] = str(application)
        try:
            if stdin is not None and self._run_process is None:
                payload=stdin.read()
                launch_options['input']=payload if isinstance(payload,bytes) else payload.encode('utf-8')
                stdin=None
            completed = run_process(
                launch_command,
                cwd=str(cwd) if cwd is not None else None,
                stdin=stdin,
                stdout=stdout if stdout is not None else subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=self._run_process is not None,
                timeout=timeout_seconds,
                check=False,
                **launch_options,
            )
        except subprocess.TimeoutExpired as error:
            raise NativeEngineTimeoutError(
                command, timeout_seconds,
                self._decode_output(error.stdout), self._decode_output(error.stderr),
            ) from error
        except OSError as error:
            raise NativeEngineProcessError(command, -1, "", str(error)) from error

        stdout_text=self._decode_output(completed.stdout)
        stderr_text=self._decode_output(completed.stderr)
        if completed.returncode != 0:
            raise NativeEngineProcessError(command, completed.returncode, stdout_text, stderr_text)
        return NativeExecution(command, completed.returncode, stdout_text, stderr_text)

    @staticmethod
    def _decode_output(output: str | bytes | None) -> str:
        if isinstance(output, bytes):
            try:return output.decode('utf-8')
            except UnicodeDecodeError:return output.decode('gb18030',errors='replace')
        return output or ""
