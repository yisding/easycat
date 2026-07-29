"""Run teaching scripts without paying for a fresh interpreter each time.

Teaching tests mostly execute a Python file, capture its text output, and
assert on the resulting JSON.  Starting a subprocess for every such probe is
unnecessary: ``run()`` preserves the familiar ``subprocess.run`` result while
executing those simple calls through ``runpy``.  Commands that test a real CLI
boundary, request a timeout, or use unsupported subprocess options still go
through the standard library unchanged.
"""

from __future__ import annotations

import asyncio
import io
import os
import runpy
import subprocess as _subprocess
import sys
import traceback
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, TypeAlias, cast

CompletedProcess = _subprocess.CompletedProcess

_CommandPart: TypeAlias = str | os.PathLike[str]


def _is_simple_python_script(
    command: Sequence[_CommandPart],
    *,
    capture_output: bool,
    text: bool,
    timeout: float | None,
    extra_options: Mapping[str, Any],
) -> bool:
    return (
        len(command) >= 2
        and os.fspath(command[0]) == sys.executable
        and Path(command[1]).suffix == ".py"
        and capture_output
        and text
        and timeout is None
        and not extra_options
    )


def _system_exit_code(exc: SystemExit, stderr: io.StringIO) -> int:
    if exc.code is None:
        return 0
    if isinstance(exc.code, int):
        return exc.code
    print(exc.code, file=stderr)
    return 1


def _execute_script(
    command: list[str],
    *,
    cwd: str | os.PathLike[str] | None,
    env: Mapping[str, str] | None,
) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    old_argv = sys.argv[:]
    old_cwd = Path.cwd()
    old_environ = os.environ.copy()
    old_path = sys.path[:]
    old_modules = sys.modules.copy()

    workdir = Path(cwd).resolve() if cwd is not None else old_cwd
    script = Path(command[1])
    if not script.is_absolute():
        script = workdir / script
    script = script.resolve()

    try:
        if env is not None:
            os.environ.clear()
            os.environ.update(env)
        os.chdir(workdir)
        sys.argv = [str(script), *command[2:]]
        sys.path.insert(0, str(script.parent))
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                runpy.run_path(str(script), run_name="__main__")
            except SystemExit as exc:
                returncode = _system_exit_code(exc, stderr)
            except BaseException:
                traceback.print_exc(file=stderr)
                returncode = 1
            else:
                returncode = 0
    finally:
        sys.argv = old_argv
        os.chdir(old_cwd)
        os.environ.clear()
        os.environ.update(old_environ)
        sys.path[:] = old_path
        # Probe scripts deliberately install fake optional dependencies such as
        # ``openai``. Restore every added or replaced module so one in-process
        # probe cannot change import/skip decisions in a later test.
        for name in sys.modules.keys() - old_modules.keys():
            sys.modules.pop(name, None)
        for name, module in old_modules.items():
            if sys.modules.get(name) is not module:
                sys.modules[name] = module

    return returncode, stdout.getvalue(), stderr.getvalue()


def _run_script_with_compatible_event_loop(
    command: list[str],
    *,
    cwd: str | os.PathLike[str] | None,
    env: Mapping[str, str] | None,
) -> tuple[int, str, str]:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        running_loop = False
    else:
        running_loop = True

    if not running_loop:
        return _execute_script(command, cwd=cwd, env=env)

    # Probe scripts commonly call asyncio.run(). When the pytest test itself
    # is async, give the script a thread with no already-running event loop.
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(_execute_script, command, cwd=cwd, env=env).result()


def run(
    args: Sequence[_CommandPart],
    *,
    cwd: str | os.PathLike[str] | None = None,
    check: bool = False,
    capture_output: bool = False,
    text: bool = False,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    **kwargs: Any,
) -> CompletedProcess[str]:
    """Execute simple Python probes in-process and delegate other commands."""
    if not _is_simple_python_script(
        args,
        capture_output=capture_output,
        text=text,
        timeout=timeout,
        extra_options=kwargs,
    ):
        return cast(
            CompletedProcess[str],
            _subprocess.run(
                args,
                cwd=cwd,
                check=check,
                capture_output=capture_output,
                text=text,
                env=env,
                timeout=timeout,
                **kwargs,
            ),
        )

    command = [os.fspath(part) for part in args]
    returncode, stdout, stderr = _run_script_with_compatible_event_loop(
        command,
        cwd=cwd,
        env=env,
    )
    completed = CompletedProcess(command, returncode, stdout, stderr)
    if check and returncode:
        raise _subprocess.CalledProcessError(
            returncode,
            command,
            output=stdout,
            stderr=stderr,
        )
    return completed
