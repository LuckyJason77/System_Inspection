"""串行启动 JMeter 脚本、管理进程树并保存每次执行的原始产物。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import time
from typing import Any
from zoneinfo import ZoneInfo

from .models import (
    AppConfig,
    ExecutionStatus,
    JMeterExecutionResult,
    ScriptConfig,
    SuiteProcessResult,
)


NowFactory = Callable[[], datetime]
PopenFactory = Callable[..., Any]
@dataclass(frozen=True, slots=True)
class ProcessTreeTerminationResult:
    confirmed: bool
    error: str | None = None


TerminateTree = Callable[[Any], ProcessTreeTerminationResult]
MonotonicFactory = Callable[[], float]
ScriptRunner = Callable[..., JMeterExecutionResult]

_POLL_INTERVAL_SECONDS = 0.2
_TASKKILL_TIMEOUT_SECONDS = 10
_TERMINATION_WAIT_SECONDS = 10
_OUTPUT_COLLECTION_TIMEOUT_SECONDS = 1
_SAFE_NAME_MAX_LENGTH = 64
_RUN_DIRECTORY_ATTEMPTS = 100
_BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("execution timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _creation_flags() -> int:
    if os.name == "nt":
        return subprocess.CREATE_NEW_PROCESS_GROUP
    return 0


def _build_jmeter_command(
    config: AppConfig,
    script: ScriptConfig,
    jtl_path: Path,
    log_path: Path,
) -> list[str]:
    return [
        str(config.jmeter.executable),
        "-n",
        "-t",
        str(script.jmx),
        "-l",
        str(jtl_path),
        "-j",
        str(log_path),
        "-q",
        str(config.jmeter.properties_file),
    ]


def _direct_process_exited(process: Any) -> bool:
    try:
        return process.poll() is not None
    except OSError:
        return False


def _force_kill_direct_process(
    process: Any,
    errors: list[str],
) -> None:
    if _direct_process_exited(process):
        return

    try:
        process.kill()
    except OSError as error:
        errors.append(f"direct process kill failed: {error}")

    try:
        process.wait(timeout=_TERMINATION_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        errors.append(
            "direct process remained alive after forced kill"
        )
    except OSError as error:
        errors.append(f"direct process wait failed: {error}")


def terminate_process_tree(
    process: Any,
) -> ProcessTreeTerminationResult:
    if process.poll() is not None:
        return ProcessTreeTerminationResult(
            confirmed=False,
            error=(
                "process tree termination was not attempted because the "
                "direct process had already exited"
            ),
        )

    errors: list[str] = []

    if os.name == "nt":
        taskkill_succeeded = False
        try:
            completed = subprocess.run(
                [
                    "taskkill",
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                ],
                capture_output=True,
                text=True,
                timeout=_TASKKILL_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            errors.append(
                "taskkill timed out after "
                f"{_TASKKILL_TIMEOUT_SECONDS} seconds"
            )
        except OSError as error:
            errors.append(f"taskkill failed to start: {error}")
        else:
            if completed.returncode == 0:
                taskkill_succeeded = True
            else:
                detail = _coerce_output(completed.stderr).strip()
                message = (
                    "taskkill failed with exit code "
                    f"{completed.returncode}"
                )
                if detail:
                    message = f"{message}: {detail}"
                errors.append(message)

        exited_after_taskkill = False
        if taskkill_succeeded:
            try:
                process.wait(timeout=_TERMINATION_WAIT_SECONDS)
            except subprocess.TimeoutExpired:
                errors.append(
                    "direct process remained alive after successful taskkill"
                )
            except OSError as error:
                errors.append(f"direct process wait failed: {error}")
            exited_after_taskkill = _direct_process_exited(process)

        if not exited_after_taskkill:
            _force_kill_direct_process(process, errors)

        direct_process_exited = _direct_process_exited(process)
        confirmed = taskkill_succeeded and exited_after_taskkill
        if not direct_process_exited:
            errors.append("final direct process state is still alive")
        if not confirmed and not errors:
            errors.append("process tree termination could not be confirmed")
        return ProcessTreeTerminationResult(
            confirmed=confirmed,
            error=None if confirmed else "; ".join(errors),
        )

    try:
        process.terminate()
    except OSError as error:
        errors.append(f"direct process termination failed: {error}")

    try:
        process.wait(timeout=_TERMINATION_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        errors.append("direct process remained alive after terminate")
    except OSError as error:
        errors.append(f"direct process wait failed: {error}")

    _force_kill_direct_process(process, errors)
    if not _direct_process_exited(process):
        errors.append("final direct process state is still alive")
    errors.append(
        "process tree termination cannot be confirmed on this platform"
    )
    return ProcessTreeTerminationResult(
        confirmed=False,
        error="; ".join(errors),
    )


def _coerce_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _collect_after_termination(
    process: Any,
) -> tuple[str, str, str | None]:
    try:
        stdout, stderr = process.communicate(
            timeout=_OUTPUT_COLLECTION_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired as error:
        return (
            _coerce_output(error.output),
            _coerce_output(error.stderr),
            (
                "forced termination/output collection failed: "
                "process remained alive after forced termination"
            ),
        )
    except Exception as error:
        return (
            "",
            "",
            (
                "forced termination/output collection failed: "
                f"{type(error).__name__}: {error}"
            ),
        )

    warning = None
    if process.poll() is None:
        warning = (
            "forced termination/output collection failed: "
            "process remained alive after output collection"
        )
    return _coerce_output(stdout), _coerce_output(stderr), warning


def _execution_result(
    *,
    script: ScriptConfig,
    status: ExecutionStatus,
    started_at: datetime,
    finished_at: datetime,
    process: Any | None,
    error: str | None,
    jtl_path: Path,
    log_path: Path,
    stdout: str = "",
    stderr: str = "",
    process_tree_termination_confirmed: bool | None = None,
) -> JMeterExecutionResult:
    return JMeterExecutionResult(
        script_name=script.name,
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        exit_code=None if process is None else process.returncode,
        error=error,
        jtl_path=jtl_path,
        log_path=log_path,
        stdout=stdout,
        stderr=stderr,
        process_tree_termination_confirmed=(
            process_tree_termination_confirmed
        ),
    )


def _append_cleanup_failure(
    error: str,
    termination: ProcessTreeTerminationResult,
) -> str:
    if termination.confirmed:
        return error
    detail = termination.error or "process tree termination was unconfirmed"
    return f"{error}; process-tree cleanup failure: {detail}"


def _process_error(process: Any, jtl_path: Path) -> str | None:
    if process.returncode != 0:
        return f"JMeter exited with exit code {process.returncode}"
    if not jtl_path.is_file():
        return "result.jtl was not created"
    try:
        if jtl_path.stat().st_size == 0:
            return "result.jtl is empty"
    except OSError as error:
        return f"result.jtl could not be inspected: {error}"
    return None


def run_jmeter_script(
    config: AppConfig,
    script: ScriptConfig,
    artifact_directory: Path,
    *,
    cancel_event: Any | None = None,
    popen_factory: PopenFactory = subprocess.Popen,
    terminate_tree: TerminateTree = terminate_process_tree,
    now: NowFactory = _now_utc,
    monotonic: MonotonicFactory = time.monotonic,
) -> JMeterExecutionResult:
    artifact_directory.mkdir(parents=True, exist_ok=True)
    jtl_path = artifact_directory / "result.jtl"
    log_path = artifact_directory / "jmeter.log"
    started_at = _as_utc(now())
    try:
        process = popen_factory(
            _build_jmeter_command(config, script, jtl_path, log_path),
            cwd=script.working_directory,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            creationflags=_creation_flags(),
        )
    except OSError as error:
        return _execution_result(
            script=script,
            status=ExecutionStatus.ERROR,
            started_at=started_at,
            finished_at=_as_utc(now()),
            process=None,
            error=f"Unable to start JMeter: {error}",
            jtl_path=jtl_path,
            log_path=log_path,
        )

    started_monotonic = monotonic()
    while True:
        if cancel_event is not None and cancel_event.is_set():
            termination = terminate_tree(process)
            stdout, stderr, termination_warning = (
                _collect_after_termination(process)
            )
            error = _append_cleanup_failure(
                "JMeter execution was cancelled",
                termination,
            )
            if termination_warning is not None:
                error = f"{error}; {termination_warning}"
            return _execution_result(
                script=script,
                status=ExecutionStatus.CANCELLED,
                started_at=started_at,
                finished_at=_as_utc(now()),
                process=process,
                error=error,
                jtl_path=jtl_path,
                log_path=log_path,
                stdout=stdout,
                stderr=stderr,
                process_tree_termination_confirmed=(
                    termination.confirmed
                ),
            )

        elapsed = monotonic() - started_monotonic
        remaining = script.timeout_seconds - elapsed
        if remaining <= 0:
            termination = terminate_tree(process)
            stdout, stderr, termination_warning = (
                _collect_after_termination(process)
            )
            error = _append_cleanup_failure(
                (
                    "JMeter execution timed out after "
                    f"{script.timeout_seconds} seconds"
                ),
                termination,
            )
            if termination_warning is not None:
                error = f"{error}; {termination_warning}"
            return _execution_result(
                script=script,
                status=ExecutionStatus.TIMEOUT,
                started_at=started_at,
                finished_at=_as_utc(now()),
                process=process,
                error=error,
                jtl_path=jtl_path,
                log_path=log_path,
                stdout=stdout,
                stderr=stderr,
                process_tree_termination_confirmed=(
                    termination.confirmed
                ),
            )

        try:
            stdout_value, stderr_value = process.communicate(
                timeout=min(_POLL_INTERVAL_SECONDS, remaining)
            )
        except subprocess.TimeoutExpired:
            continue
        stdout = _coerce_output(stdout_value)
        stderr = _coerce_output(stderr_value)
        error = _process_error(process, jtl_path)
        status = (
            ExecutionStatus.COMPLETED
            if error is None
            else ExecutionStatus.ERROR
        )
        return _execution_result(
            script=script,
            status=status,
            started_at=started_at,
            finished_at=_as_utc(now()),
            process=process,
            error=error,
            jtl_path=jtl_path,
            log_path=log_path,
            stdout=stdout,
            stderr=stderr,
        )


def _safe_script_name(name: str) -> str:
    safe_characters: list[str] = []
    needs_separator = False
    for character in name.rstrip(" ."):
        if character.isalnum() or character in "-_":
            if (
                needs_separator
                and safe_characters
                and safe_characters[-1] != "-"
            ):
                safe_characters.append("-")
            safe_characters.append(character)
            needs_separator = False
        else:
            needs_separator = True

    safe_name = "".join(safe_characters)[:_SAFE_NAME_MAX_LENGTH]
    return safe_name.rstrip(" .") or "script"


def _create_run_directory(
    output_root: Path,
    timestamp_prefix: str,
) -> tuple[str, Path]:
    output_root.mkdir(parents=True, exist_ok=True)
    for attempt in range(1, _RUN_DIRECTORY_ATTEMPTS + 1):
        run_id = (
            timestamp_prefix
            if attempt == 1
            else f"{timestamp_prefix}-{attempt:02d}"
        )
        run_directory = output_root / run_id
        try:
            run_directory.mkdir()
        except FileExistsError:
            continue
        return run_id, run_directory
    raise RuntimeError("unable to allocate a unique run directory")


def _unexpected_execution_result(
    script: ScriptConfig,
    artifact_directory: Path,
    error: Exception,
    timestamp: datetime,
) -> JMeterExecutionResult:
    return JMeterExecutionResult(
        script_name=script.name,
        status=ExecutionStatus.ERROR,
        started_at=timestamp,
        finished_at=timestamp,
        exit_code=None,
        error=(
            "Unexpected execution error: "
            f"{type(error).__name__}: {error}"
        ),
        jtl_path=artifact_directory / "result.jtl",
        log_path=artifact_directory / "jmeter.log",
        stdout="",
        stderr="",
    )


def run_suite_processes(
    config: AppConfig,
    *,
    cancel_event: Any | None = None,
    now: NowFactory = _now_utc,
    script_runner: ScriptRunner | None = None,
) -> SuiteProcessResult:
    started_at = _as_utc(now())
    local_started_at = started_at.astimezone(_BEIJING_TIMEZONE)
    timestamp_prefix = local_started_at.strftime("%Y-%m-%d_%H-%M")
    run_id, run_directory = _create_run_directory(
        config.runner.output_root,
        timestamp_prefix,
    )
    artifacts_directory = run_directory / "artifacts"
    artifacts_directory.mkdir()
    execute_script = script_runner or run_jmeter_script
    executions: list[JMeterExecutionResult] = []

    for index, script in enumerate(config.scripts, start=1):
        if cancel_event is not None and cancel_event.is_set():
            break

        artifact_directory = artifacts_directory / (
            f"{index:02d}-{_safe_script_name(script.name)}"
        )
        try:
            artifact_directory.mkdir()
            execution = execute_script(
                config,
                script,
                artifact_directory,
                cancel_event=cancel_event,
                now=now,
            )
        except Exception as error:
            timestamp = _as_utc(now())
            execution = _unexpected_execution_result(
                script,
                artifact_directory,
                error,
                timestamp,
            )
        executions.append(execution)

        if execution.process_tree_termination_confirmed is False:
            break
        if execution.status is ExecutionStatus.CANCELLED:
            break
        if cancel_event is not None and cancel_event.is_set():
            break

    return SuiteProcessResult(
        run_id=run_id,
        run_directory=run_directory,
        started_at=started_at,
        finished_at=_as_utc(now()),
        executions=tuple(executions),
    )
