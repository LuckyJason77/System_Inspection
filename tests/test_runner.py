"""验证 JMeter 命令执行、超时取消、进程树清理和运行目录分配。"""

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import importlib
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from jmeter_suite.models import (
    AppConfig,
    JMeterConfig,
    RunnerConfig,
    ScheduleConfig,
    ScriptConfig,
)


class FakeProcess:
    """Small Popen-compatible process double used at the external boundary."""

    def __init__(
        self,
        *,
        returncode: int | None = 0,
        stdout: str = "",
        stderr: str = "",
        pid: int = 4321,
    ) -> None:
        self.returncode = returncode
        self.stdout_text = stdout
        self.stderr_text = stderr
        self.pid = pid
        self.killed = False
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        return self.stdout_text, self.stderr_text

    def wait(self, timeout: float | None = None) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15


class HangingProcess(FakeProcess):
    def __init__(
        self,
        *,
        stdout: str = "partial stdout",
        stderr: str = "partial stderr",
        on_first_poll: object | None = None,
    ) -> None:
        super().__init__(
            returncode=None,
            stdout=stdout,
            stderr=stderr,
        )
        self.communication_timeouts: list[float | None] = []
        self._on_first_poll = on_first_poll

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        self.communication_timeouts.append(timeout)
        if self.returncode is not None:
            return self.stdout_text, self.stderr_text
        if timeout is None:
            raise AssertionError("running processes must be polled with a timeout")
        if callable(self._on_first_poll):
            callback = self._on_first_poll
            self._on_first_poll = None
            callback()
        raise subprocess.TimeoutExpired(
            cmd=["jmeter.bat"],
            timeout=timeout,
            output=self.stdout_text,
            stderr=self.stderr_text,
        )


class StubbornProcess(FakeProcess):
    def __init__(self) -> None:
        super().__init__(returncode=None)
        self.wait_timeouts: list[float | None] = []

    def wait(self, timeout: float | None = None) -> int | None:
        self.wait_timeouts.append(timeout)
        if not self.killed:
            raise subprocess.TimeoutExpired(
                cmd=["jmeter.bat"],
                timeout=timeout,
            )
        return self.returncode


class NeverExitsProcess(FakeProcess):
    """Process double that survives taskkill, kill, and both waits."""

    def __init__(self) -> None:
        super().__init__(
            returncode=None,
            stdout="uncollected stdout",
            stderr="uncollected stderr",
            pid=9753,
        )
        self.wait_timeouts: list[float | None] = []
        self.communication_timeouts: list[float | None] = []

    def wait(self, timeout: float | None = None) -> int | None:
        self.wait_timeouts.append(timeout)
        raise subprocess.TimeoutExpired(
            cmd=["jmeter.bat"],
            timeout=timeout,
        )

    def kill(self) -> None:
        self.killed = True
        self.returncode = None

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        self.communication_timeouts.append(timeout)
        if timeout is None:
            raise AssertionError("output collection must always be bounded")
        raise subprocess.TimeoutExpired(
            cmd=["jmeter.bat"],
            timeout=timeout,
            output=self.stdout_text,
            stderr=self.stderr_text,
        )


def make_config(
    root: Path,
    scripts: tuple[ScriptConfig, ...],
    *,
    timezone_name: str = "Asia/Shanghai",
) -> AppConfig:
    return AppConfig(
        jmeter=JMeterConfig(
            executable=root / "tools" / "jmeter.bat",
            properties_file=root / "tools" / "jmeter.properties",
        ),
        schedule=ScheduleConfig(
            timezone=timezone_name,
            cron="0 2 * * *",
        ),
        runner=RunnerConfig(
            output_root=root / "runs",
            default_timeout_seconds=60,
        ),
        scripts=scripts,
    )


def make_script(
    root: Path,
    *,
    name: str = "登录",
    timeout_seconds: int = 30,
) -> ScriptConfig:
    return ScriptConfig(
        name=name,
        jmx=root / "plans" / "login.jmx",
        working_directory=root / "work",
        timeout_seconds=timeout_seconds,
    )


def make_execution_result(
    script: ScriptConfig,
    artifact_directory: Path,
    *,
    status: object,
    error: str | None = None,
    process_tree_termination_confirmed: bool | None = None,
):
    from jmeter_suite.models import JMeterExecutionResult

    started_at = datetime(2026, 7, 31, 0, 0, tzinfo=timezone.utc)
    return JMeterExecutionResult(
        script_name=script.name,
        status=status,
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=1),
        exit_code=0 if error is None else 1,
        error=error,
        jtl_path=artifact_directory / "result.jtl",
        log_path=artifact_directory / "jmeter.log",
        stdout=f"stdout:{script.name}",
        stderr=f"stderr:{script.name}",
        process_tree_termination_confirmed=(
            process_tree_termination_confirmed
        ),
    )


def test_run_jmeter_script_uses_argument_contract_and_returns_completed(
    tmp_path: Path,
):
    runner = importlib.import_module("jmeter_suite.runner")
    from jmeter_suite.models import ExecutionStatus

    script = make_script(tmp_path)
    config = make_config(tmp_path, (script,))
    artifact_directory = tmp_path / "run" / "artifacts" / "01-登录"
    observed: dict[str, object] = {}

    def popen_factory(command: list[str], **kwargs: object) -> FakeProcess:
        observed["command"] = command
        observed["kwargs"] = kwargs
        observed["artifact_exists_at_start"] = artifact_directory.is_dir()
        result_path = Path(command[command.index("-l") + 1])
        result_path.write_text("<testResults />", encoding="utf-8")
        return FakeProcess(
            returncode=0,
            stdout="JMeter standard output",
            stderr="JMeter standard error",
        )

    moments = iter(
        (
            datetime(2026, 7, 31, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 31, 0, 0, 2, tzinfo=timezone.utc),
        )
    )

    result = runner.run_jmeter_script(
        config,
        script,
        artifact_directory,
        popen_factory=popen_factory,
        now=lambda: next(moments),
    )

    expected_flags = (
        subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    )
    assert observed["command"] == [
        str(config.jmeter.executable),
        "-n",
        "-t",
        str(script.jmx),
        "-l",
        str(artifact_directory / "result.jtl"),
        "-j",
        str(artifact_directory / "jmeter.log"),
        "-q",
        str(config.jmeter.properties_file),
    ]
    command = observed["command"]
    assert isinstance(command, list)
    assert command[-2:] == ["-q", str(config.jmeter.properties_file)]
    assert not any(argument.startswith("-J") for argument in command)
    assert observed["artifact_exists_at_start"] is True
    assert observed["kwargs"] == {
        "cwd": script.working_directory,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "shell": False,
        "creationflags": expected_flags,
    }
    assert result.script_name == "登录"
    assert result.status is ExecutionStatus.COMPLETED
    assert result.started_at == datetime(
        2026,
        7,
        31,
        0,
        0,
        tzinfo=timezone.utc,
    )
    assert result.finished_at == datetime(
        2026,
        7,
        31,
        0,
        0,
        2,
        tzinfo=timezone.utc,
    )
    assert result.duration_seconds == 2.0
    assert result.exit_code == 0
    assert result.error is None
    assert result.jtl_path == artifact_directory / "result.jtl"
    assert result.log_path == artifact_directory / "jmeter.log"
    assert result.stdout == "JMeter standard output"
    assert result.stderr == "JMeter standard error"
    assert [status.value for status in ExecutionStatus] == [
        "completed",
        "error",
        "timeout",
        "cancelled",
    ]
    with pytest.raises(FrozenInstanceError):
        result.status = ExecutionStatus.ERROR


@pytest.mark.parametrize(
    ("returncode", "artifact_content", "expected_error_fragment"),
    [
        (7, "<testResults />", "exit code 7"),
        (0, None, "result.jtl was not created"),
        (0, "", "result.jtl is empty"),
    ],
)
def test_run_jmeter_script_reports_process_and_artifact_errors(
    tmp_path: Path,
    returncode: int,
    artifact_content: str | None,
    expected_error_fragment: str,
):
    runner = importlib.import_module("jmeter_suite.runner")
    from jmeter_suite.models import ExecutionStatus

    script = make_script(tmp_path)
    config = make_config(tmp_path, (script,))
    artifact_directory = tmp_path / "artifacts" / "01-登录"

    def popen_factory(command: list[str], **kwargs: object) -> FakeProcess:
        if artifact_content is not None:
            result_path = Path(command[command.index("-l") + 1])
            result_path.write_text(artifact_content, encoding="utf-8")
        return FakeProcess(
            returncode=returncode,
            stdout="captured stdout",
            stderr="captured stderr",
        )

    result = runner.run_jmeter_script(
        config,
        script,
        artifact_directory,
        popen_factory=popen_factory,
    )

    assert result.status is ExecutionStatus.ERROR
    assert result.exit_code == returncode
    assert expected_error_fragment in result.error
    assert result.stdout == "captured stdout"
    assert result.stderr == "captured stderr"


def test_run_jmeter_script_turns_start_oserror_into_error_result(
    tmp_path: Path,
):
    runner = importlib.import_module("jmeter_suite.runner")
    from jmeter_suite.models import ExecutionStatus

    script = make_script(tmp_path)
    config = make_config(tmp_path, (script,))
    artifact_directory = tmp_path / "artifacts" / "01-登录"

    def popen_factory(command: list[str], **kwargs: object) -> FakeProcess:
        raise OSError("executable format is invalid")

    result = runner.run_jmeter_script(
        config,
        script,
        artifact_directory,
        popen_factory=popen_factory,
    )

    assert result.status is ExecutionStatus.ERROR
    assert result.exit_code is None
    assert "executable format is invalid" in result.error
    assert result.stdout == ""
    assert result.stderr == ""
    assert result.started_at.tzinfo is not None
    assert result.finished_at.tzinfo is not None
    assert artifact_directory.is_dir()


def test_run_jmeter_script_times_out_and_terminates_the_process_tree(
    tmp_path: Path,
):
    runner = importlib.import_module("jmeter_suite.runner")
    from jmeter_suite.models import ExecutionStatus

    script = make_script(tmp_path, timeout_seconds=1)
    config = make_config(tmp_path, (script,))
    process = HangingProcess()
    terminated: list[FakeProcess] = []

    def terminate_tree(target: FakeProcess) -> SimpleNamespace:
        terminated.append(target)
        target.returncode = -9
        return SimpleNamespace(confirmed=True, error=None)

    monotonic_values = iter((100.0, 100.0, 100.4, 101.0))

    result = runner.run_jmeter_script(
        config,
        script,
        tmp_path / "artifacts" / "01-登录",
        popen_factory=lambda command, **kwargs: process,
        terminate_tree=terminate_tree,
        monotonic=lambda: next(monotonic_values),
    )

    assert result.status is ExecutionStatus.TIMEOUT
    assert result.exit_code == -9
    assert result.error is not None
    assert "timed out after 1 seconds" in result.error
    assert result.stdout == "partial stdout"
    assert result.stderr == "partial stderr"
    assert result.process_tree_termination_confirmed is True
    assert terminated == [process]
    polling_timeouts = [
        timeout
        for timeout in process.communication_timeouts
        if timeout is not None
    ]
    assert polling_timeouts
    assert max(polling_timeouts) <= 1


def test_run_jmeter_script_cancels_and_terminates_the_process_tree(
    tmp_path: Path,
):
    runner = importlib.import_module("jmeter_suite.runner")
    from jmeter_suite.models import ExecutionStatus

    cancel_event = threading.Event()
    process = HangingProcess(on_first_poll=cancel_event.set)
    script = make_script(tmp_path, timeout_seconds=60)
    config = make_config(tmp_path, (script,))
    terminated: list[FakeProcess] = []

    def terminate_tree(target: FakeProcess) -> SimpleNamespace:
        terminated.append(target)
        target.returncode = -15
        return SimpleNamespace(confirmed=True, error=None)

    result = runner.run_jmeter_script(
        config,
        script,
        tmp_path / "artifacts" / "01-登录",
        cancel_event=cancel_event,
        popen_factory=lambda command, **kwargs: process,
        terminate_tree=terminate_tree,
        monotonic=lambda: 100.0,
    )

    assert result.status is ExecutionStatus.CANCELLED
    assert result.exit_code == -15
    assert result.error is not None
    assert "cancelled" in result.error
    assert result.stdout == "partial stdout"
    assert result.stderr == "partial stderr"
    assert result.process_tree_termination_confirmed is True
    assert terminated == [process]


def test_terminate_process_tree_uses_taskkill_on_windows(
    monkeypatch: pytest.MonkeyPatch,
):
    runner = importlib.import_module("jmeter_suite.runner")
    process = FakeProcess(returncode=None, pid=2468)
    observed: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object):
        observed["command"] = command
        observed["kwargs"] = kwargs
        process.returncode = -9
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    monkeypatch.setattr(runner.os, "name", "nt")

    result = runner.terminate_process_tree(process)

    assert observed == {
        "command": ["taskkill", "/PID", "2468", "/T", "/F"],
        "kwargs": {
            "capture_output": True,
            "text": True,
            "timeout": 10,
            "check": False,
        },
    }
    assert process.killed is False
    assert result.confirmed is True
    assert result.error is None


def test_terminate_process_tree_kills_when_process_does_not_exit(
    monkeypatch: pytest.MonkeyPatch,
):
    runner = importlib.import_module("jmeter_suite.runner")
    process = StubbornProcess()

    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            0,
            "",
            "",
        ),
    )
    monkeypatch.setattr(runner.os, "name", "nt")

    result = runner.terminate_process_tree(process)

    assert process.wait_timeouts == [10, 10]
    assert process.killed is True
    assert result.confirmed is False
    assert result.error is not None
    assert "remained alive after successful taskkill" in result.error


def test_terminate_process_tree_times_out_taskkill_and_returns_unconfirmed(
    monkeypatch: pytest.MonkeyPatch,
):
    runner = importlib.import_module("jmeter_suite.runner")
    process = StubbornProcess()
    observed_timeouts: list[float | None] = []

    def hanging_taskkill(command: list[str], **kwargs: object):
        observed_timeouts.append(kwargs.get("timeout"))
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))

    monkeypatch.setattr(runner.subprocess, "run", hanging_taskkill)
    monkeypatch.setattr(runner.os, "name", "nt")

    result = runner.terminate_process_tree(process)

    assert observed_timeouts == [10]
    assert process.killed is True
    assert result.confirmed is False
    assert result.error is not None
    assert "taskkill timed out after 10 seconds" in result.error


def test_terminate_process_tree_nonzero_taskkill_returns_unconfirmed(
    monkeypatch: pytest.MonkeyPatch,
):
    runner = importlib.import_module("jmeter_suite.runner")
    process = StubbornProcess()

    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command,
            1,
            "",
            "Access is denied.",
        ),
    )
    monkeypatch.setattr(runner.os, "name", "nt")

    result = runner.terminate_process_tree(process)

    assert process.killed is True
    assert result.confirmed is False
    assert result.error is not None
    assert "taskkill failed with exit code 1" in result.error
    assert "Access is denied." in result.error


@pytest.mark.integration
@pytest.mark.skipif(
    os.name != "nt",
    reason="requires Windows taskkill process-tree semantics",
)
def test_terminate_process_tree_kills_real_batch_python_child(
    tmp_path: Path,
):
    import ctypes
    from ctypes import wintypes

    runner = importlib.import_module("jmeter_suite.runner")
    child_script = tmp_path / "delayed_child.py"
    child_pid_path = tmp_path / "child.pid"
    marker_path = tmp_path / "child-marker.txt"
    batch_path = tmp_path / "parent.bat"
    process: subprocess.Popen[bytes] | None = None
    child_pid: int | None = None

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    def process_is_alive(pid: int) -> bool:
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(
                handle,
                ctypes.byref(exit_code),
            ):
                return False
            return exit_code.value == 259
        finally:
            kernel32.CloseHandle(handle)

    child_script.write_text(
        "\n".join(
            (
                "import os",
                "from pathlib import Path",
                "import sys",
                "import time",
                "Path(sys.argv[1]).write_text(str(os.getpid()), encoding='ascii')",
                "time.sleep(1.5)",
                "Path(sys.argv[2]).write_text('child survived', encoding='utf-8')",
            )
        ),
        encoding="utf-8",
    )
    batch_path.write_text(
        "\r\n".join(
            (
                "@echo off",
                (
                    f'start "" /b "{sys.executable}" "%~dp0delayed_child.py" '
                    '"%~dp0child.pid" "%~dp0child-marker.txt"'
                ),
                ":keep_parent_alive",
                "ping -n 2 127.0.0.1 >nul",
                "goto keep_parent_alive",
                "",
            )
        ),
        encoding="utf-8",
    )

    try:
        process = subprocess.Popen(
            ["cmd.exe", "/d", "/c", batch_path.name],
            cwd=tmp_path,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=(
                subprocess.CREATE_NEW_PROCESS_GROUP
                | subprocess.CREATE_NO_WINDOW
            ),
        )
        pid_deadline = time.monotonic() + 5
        while time.monotonic() < pid_deadline:
            if child_pid_path.is_file():
                try:
                    child_pid = int(
                        child_pid_path.read_text(encoding="ascii")
                    )
                except (OSError, ValueError):
                    pass
                else:
                    break
            if process.poll() is not None:
                break
            time.sleep(0.05)

        assert child_pid is not None
        assert process_is_alive(child_pid)

        termination = runner.terminate_process_tree(process)

        assert termination.confirmed is True
        assert termination.error is None
        assert process.poll() is not None
        assert not process_is_alive(child_pid)

        marker_deadline = time.monotonic() + 2
        while time.monotonic() < marker_deadline and not marker_path.exists():
            time.sleep(0.05)
        assert not marker_path.exists()
    finally:
        cleanup_pids = [
            None if process is None else process.pid,
            child_pid,
        ]
        for cleanup_pid in cleanup_pids:
            if cleanup_pid is None or not process_is_alive(cleanup_pid):
                continue
            try:
                subprocess.run(
                    [
                        "taskkill",
                        "/PID",
                        str(cleanup_pid),
                        "/T",
                        "/F",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        if process is not None:
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)


def test_run_jmeter_script_records_unconfirmed_cleanup_when_taskkill_times_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    runner = importlib.import_module("jmeter_suite.runner")
    from jmeter_suite.models import ExecutionStatus

    process = StubbornProcess()
    script = make_script(tmp_path, timeout_seconds=1)
    config = make_config(tmp_path, (script,))

    def hanging_taskkill(command: list[str], **kwargs: object):
        raise subprocess.TimeoutExpired(command, kwargs.get("timeout"))

    monkeypatch.setattr(runner.subprocess, "run", hanging_taskkill)
    monkeypatch.setattr(runner.os, "name", "nt")
    monotonic_values = iter((100.0, 101.0))

    result = runner.run_jmeter_script(
        config,
        script,
        tmp_path / "artifacts" / "01-timeout",
        popen_factory=lambda command, **kwargs: process,
        monotonic=lambda: next(monotonic_values),
    )

    assert result.status is ExecutionStatus.TIMEOUT
    assert result.process_tree_termination_confirmed is False
    assert result.error is not None
    assert "process-tree cleanup failure" in result.error
    assert "taskkill timed out after 10 seconds" in result.error


@pytest.mark.parametrize(
    ("cancelled", "expected_status"),
    [
        (False, "timeout"),
        (True, "cancelled"),
    ],
)
def test_run_jmeter_script_returns_when_forced_termination_never_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cancelled: bool,
    expected_status: str,
):
    runner = importlib.import_module("jmeter_suite.runner")

    process = NeverExitsProcess()
    script = make_script(tmp_path, timeout_seconds=1)
    config = make_config(tmp_path, (script,))
    cancel_event = threading.Event()
    if cancelled:
        cancel_event.set()
    taskkill_calls: list[list[str]] = []

    def ineffective_taskkill(command: list[str], **kwargs: object):
        taskkill_calls.append(command)
        return subprocess.CompletedProcess(
            command,
            1,
            "",
            "taskkill could not terminate the process tree",
        )

    monkeypatch.setattr(runner.subprocess, "run", ineffective_taskkill)
    monkeypatch.setattr(runner.os, "name", "nt")
    monotonic_values = iter((100.0, 101.0))

    result = runner.run_jmeter_script(
        config,
        script,
        tmp_path / "artifacts" / "01-never-exits",
        cancel_event=cancel_event,
        popen_factory=lambda command, **kwargs: process,
        monotonic=lambda: next(monotonic_values),
    )

    assert result.status.value == expected_status
    assert result.exit_code is None
    assert result.error is not None
    assert result.process_tree_termination_confirmed is False
    assert "process-tree cleanup failure" in result.error
    assert "taskkill failed with exit code 1" in result.error
    assert "forced termination/output collection failed" in result.error
    assert result.stdout == "uncollected stdout"
    assert result.stderr == "uncollected stderr"
    assert taskkill_calls == [
        ["taskkill", "/PID", "9753", "/T", "/F"]
    ]
    assert process.wait_timeouts
    assert len(process.wait_timeouts) <= 2
    assert set(process.wait_timeouts) == {10}
    assert process.killed is True
    assert process.poll() is None
    assert process.communication_timeouts
    assert None not in process.communication_timeouts
    assert max(process.communication_timeouts) <= 1


def test_run_suite_processes_preserves_discovered_script_order(
    tmp_path: Path,
):
    runner = importlib.import_module("jmeter_suite.runner")
    from jmeter_suite.models import ExecutionStatus

    scripts = (
        make_script(tmp_path, name="01-login"),
        make_script(tmp_path, name="02-query"),
    )
    config = make_config(tmp_path, scripts)

    def script_runner(
        actual_config: AppConfig,
        script: ScriptConfig,
        artifact_directory: Path,
        **kwargs: object,
    ):
        return make_execution_result(
            script,
            artifact_directory,
            status=ExecutionStatus.COMPLETED,
        )

    suite = runner.run_suite_processes(
        config,
        now=lambda: datetime(
            2026,
            7,
            31,
            0,
            0,
            tzinfo=timezone.utc,
        ),
        script_runner=script_runner,
    )

    assert [result.script_name for result in suite.executions] == [
        "01-login",
        "02-query",
    ]


def test_run_suite_processes_preserves_script_order_and_safe_directories(
    tmp_path: Path,
):
    runner = importlib.import_module("jmeter_suite.runner")
    from jmeter_suite.models import ExecutionStatus, SuiteProcessResult

    long_name = "测" * 100
    scripts = (
        make_script(tmp_path, name=" 登录 / Smoke:*? . "),
        make_script(tmp_path, name="订单_查询-02"),
        make_script(tmp_path, name='<>:"/\\|?*   .'),
        make_script(tmp_path, name=long_name),
    )
    config = make_config(tmp_path, scripts)
    calls: list[tuple[str, Path]] = []

    def script_runner(
        actual_config: AppConfig,
        script: ScriptConfig,
        artifact_directory: Path,
        **kwargs: object,
    ):
        assert actual_config is config
        assert artifact_directory.is_dir()
        calls.append((script.name, artifact_directory))
        if script.name == " 登录 / Smoke:*? . ":
            return make_execution_result(
                script,
                artifact_directory,
                status=ExecutionStatus.ERROR,
                error="JMeter exited with exit code 3",
            )
        return make_execution_result(
            script,
            artifact_directory,
            status=ExecutionStatus.COMPLETED,
        )

    moments = iter(
        (
            datetime(2026, 7, 31, 16, 5, 6, tzinfo=timezone.utc),
            datetime(2026, 7, 31, 16, 5, 11, tzinfo=timezone.utc),
        )
    )

    result = runner.run_suite_processes(
        config,
        now=lambda: next(moments),
        script_runner=script_runner,
    )

    assert isinstance(result, SuiteProcessResult)
    assert result.run_id == "2026-08-01_00-05"
    assert result.run_directory == config.runner.output_root / result.run_id
    assert result.run_directory.is_dir()
    assert result.started_at == datetime(
        2026,
        7,
        31,
        16,
        5,
        6,
        tzinfo=timezone.utc,
    )
    assert result.finished_at == datetime(
        2026,
        7,
        31,
        16,
        5,
        11,
        tzinfo=timezone.utc,
    )
    assert result.started_at.tzinfo is not None
    assert result.finished_at.tzinfo is not None
    assert isinstance(result.executions, tuple)
    assert [execution.script_name for execution in result.executions] == [
        " 登录 / Smoke:*? . ",
        "订单_查询-02",
        '<>:"/\\|?*   .',
        long_name,
    ]
    assert [execution.status for execution in result.executions] == [
        ExecutionStatus.ERROR,
        ExecutionStatus.COMPLETED,
        ExecutionStatus.COMPLETED,
        ExecutionStatus.COMPLETED,
    ]
    assert [name for name, _ in calls] == [
        " 登录 / Smoke:*? . ",
        "订单_查询-02",
        '<>:"/\\|?*   .',
        long_name,
    ]
    artifacts_directory = result.run_directory / "artifacts"
    assert [path.name for path in artifacts_directory.iterdir()] == [
        "01-登录-Smoke",
        "02-订单_查询-02",
        "03-script",
        f"04-{'测' * 64}",
    ]
    assert [path for _, path in calls] == [
        artifacts_directory / "01-登录-Smoke",
        artifacts_directory / "02-订单_查询-02",
        artifacts_directory / "03-script",
        artifacts_directory / f"04-{'测' * 64}",
    ]
    assert {path.name for path in result.run_directory.iterdir()} == {
        "artifacts"
    }
    with pytest.raises(FrozenInstanceError):
        result.run_id = "changed"


def test_run_suite_processes_stops_after_cancelled_execution(
    tmp_path: Path,
):
    runner = importlib.import_module("jmeter_suite.runner")
    from jmeter_suite.models import ExecutionStatus

    cancel_event = threading.Event()
    scripts = (
        make_script(tmp_path, name="first"),
        make_script(tmp_path, name="second"),
    )
    config = make_config(tmp_path, scripts)
    calls: list[str] = []

    def script_runner(
        actual_config: AppConfig,
        script: ScriptConfig,
        artifact_directory: Path,
        **kwargs: object,
    ):
        calls.append(script.name)
        cancel_event.set()
        return make_execution_result(
            script,
            artifact_directory,
            status=ExecutionStatus.CANCELLED,
            error="JMeter execution was cancelled",
        )

    result = runner.run_suite_processes(
        config,
        cancel_event=cancel_event,
        now=lambda: datetime(
            2026,
            7,
            31,
            0,
            0,
            tzinfo=timezone.utc,
        ),
        script_runner=script_runner,
    )

    assert calls == ["first"]
    assert len(result.executions) == 1
    assert result.executions[0].status is ExecutionStatus.CANCELLED
    assert [
        path.name for path in (result.run_directory / "artifacts").iterdir()
    ] == ["01-first"]


@pytest.mark.parametrize(
    "cleanup_error",
    [
        "taskkill timed out after 10 seconds",
        "taskkill failed with exit code 1",
    ],
)
def test_run_suite_processes_stops_after_unconfirmed_tree_cleanup(
    tmp_path: Path,
    cleanup_error: str,
):
    runner = importlib.import_module("jmeter_suite.runner")
    from jmeter_suite.models import ExecutionStatus

    scripts = (
        make_script(tmp_path, name="first"),
        make_script(tmp_path, name="second"),
    )
    config = make_config(tmp_path, scripts)
    calls: list[str] = []

    def script_runner(
        actual_config: AppConfig,
        script: ScriptConfig,
        artifact_directory: Path,
        **kwargs: object,
    ):
        calls.append(script.name)
        return make_execution_result(
            script,
            artifact_directory,
            status=ExecutionStatus.TIMEOUT,
            error=(
                "JMeter execution timed out; process-tree cleanup failure: "
                f"{cleanup_error}"
            ),
            process_tree_termination_confirmed=False,
        )

    result = runner.run_suite_processes(
        config,
        now=lambda: datetime(
            2026,
            7,
            31,
            0,
            0,
            tzinfo=timezone.utc,
        ),
        script_runner=script_runner,
    )

    assert calls == ["first"]
    assert len(result.executions) == 1
    assert result.executions[0].process_tree_termination_confirmed is False


def test_run_suite_processes_continues_after_confirmed_timeout(
    tmp_path: Path,
):
    runner = importlib.import_module("jmeter_suite.runner")
    from jmeter_suite.models import ExecutionStatus

    scripts = (
        make_script(tmp_path, name="first"),
        make_script(tmp_path, name="second"),
    )
    config = make_config(tmp_path, scripts)
    calls: list[str] = []

    def script_runner(
        actual_config: AppConfig,
        script: ScriptConfig,
        artifact_directory: Path,
        **kwargs: object,
    ):
        calls.append(script.name)
        if script.name == "first":
            return make_execution_result(
                script,
                artifact_directory,
                status=ExecutionStatus.TIMEOUT,
                error="JMeter execution timed out after 30 seconds",
                process_tree_termination_confirmed=True,
            )
        return make_execution_result(
            script,
            artifact_directory,
            status=ExecutionStatus.COMPLETED,
        )

    result = runner.run_suite_processes(
        config,
        now=lambda: datetime(
            2026,
            7,
            31,
            0,
            0,
            tzinfo=timezone.utc,
        ),
        script_runner=script_runner,
    )

    assert calls == ["first", "second"]
    assert [execution.status for execution in result.executions] == [
        ExecutionStatus.TIMEOUT,
        ExecutionStatus.COMPLETED,
    ]


def test_run_suite_processes_turns_unexpected_errors_into_results_and_continues(
    tmp_path: Path,
):
    runner = importlib.import_module("jmeter_suite.runner")
    from jmeter_suite.models import ExecutionStatus

    scripts = (
        make_script(tmp_path, name="first"),
        make_script(tmp_path, name="second"),
    )
    config = make_config(tmp_path, scripts)
    calls: list[str] = []

    def script_runner(
        actual_config: AppConfig,
        script: ScriptConfig,
        artifact_directory: Path,
        **kwargs: object,
    ):
        calls.append(script.name)
        if script.name == "first":
            raise RuntimeError("unexpected fake failure")
        return make_execution_result(
            script,
            artifact_directory,
            status=ExecutionStatus.COMPLETED,
        )

    result = runner.run_suite_processes(
        config,
        now=lambda: datetime(
            2026,
            7,
            31,
            0,
            0,
            tzinfo=timezone.utc,
        ),
        script_runner=script_runner,
    )

    assert calls == ["first", "second"]
    assert [execution.status for execution in result.executions] == [
        ExecutionStatus.ERROR,
        ExecutionStatus.COMPLETED,
    ]
    failed_execution = result.executions[0]
    assert failed_execution.exit_code is None
    assert failed_execution.error is not None
    assert "unexpected fake failure" in failed_execution.error
    assert failed_execution.stdout == ""
    assert failed_execution.stderr == ""
    assert failed_execution.started_at.tzinfo is not None
    assert failed_execution.finished_at.tzinfo is not None


def test_run_suite_processes_uses_ordered_suffix_when_directory_exists(
    tmp_path: Path,
):
    runner = importlib.import_module("jmeter_suite.runner")
    from jmeter_suite.models import ExecutionStatus

    script = make_script(tmp_path, name="only")
    config = make_config(tmp_path, (script,))
    existing_run_ids = (
        "2026-07-31_08-00",
        "2026-07-31_08-00-02",
    )
    for existing_run_id in existing_run_ids:
        (config.runner.output_root / existing_run_id).mkdir(parents=True)

    def script_runner(
        actual_config: AppConfig,
        actual_script: ScriptConfig,
        artifact_directory: Path,
        **kwargs: object,
    ):
        return make_execution_result(
            actual_script,
            artifact_directory,
            status=ExecutionStatus.COMPLETED,
        )

    result = runner.run_suite_processes(
        config,
        now=lambda: datetime(
            2026,
            7,
            31,
            0,
            0,
            tzinfo=timezone.utc,
        ),
        script_runner=script_runner,
    )

    assert result.run_id == "2026-07-31_08-00-03"
    assert result.run_directory.is_dir()
    for existing_run_id in existing_run_ids:
        assert (config.runner.output_root / existing_run_id).is_dir()


def test_run_suite_processes_always_names_directory_in_beijing_time(
    tmp_path: Path,
):
    runner = importlib.import_module("jmeter_suite.runner")
    from jmeter_suite.models import ExecutionStatus, ScheduleConfig

    script = make_script(tmp_path, name="only")
    original = make_config(tmp_path, (script,))
    config = AppConfig(
        jmeter=original.jmeter,
        schedule=ScheduleConfig(timezone="UTC", cron=original.schedule.cron),
        runner=original.runner,
        scripts=original.scripts,
    )

    def script_runner(
        actual_config: AppConfig,
        actual_script: ScriptConfig,
        artifact_directory: Path,
        **kwargs: object,
    ):
        return make_execution_result(
            actual_script,
            artifact_directory,
            status=ExecutionStatus.COMPLETED,
        )

    moments = iter(
        (
            datetime(2026, 7, 31, 16, 5, 59, tzinfo=timezone.utc),
            datetime(2026, 7, 31, 16, 6, 1, tzinfo=timezone.utc),
        )
    )
    result = runner.run_suite_processes(
        config,
        now=lambda: next(moments),
        script_runner=script_runner,
    )

    assert result.run_id == "2026-08-01_00-05"


def test_run_suite_processes_fails_after_100_same_minute_directories(
    tmp_path: Path,
):
    runner = importlib.import_module("jmeter_suite.runner")

    script = make_script(tmp_path, name="only")
    config = make_config(tmp_path, (script,))
    prefix = "2026-07-31_08-00"
    existing_names = [prefix, *(f"{prefix}-{index:02d}" for index in range(2, 101))]
    for name in existing_names:
        (config.runner.output_root / name).mkdir(parents=True, exist_ok=True)

    called = False

    def script_runner(*args: object, **kwargs: object):
        nonlocal called
        called = True
        raise AssertionError("script runner must not start without a run directory")

    with pytest.raises(
        RuntimeError,
        match="unable to allocate a unique run directory",
    ):
        runner.run_suite_processes(
            config,
            now=lambda: datetime(
                2026,
                7,
                31,
                0,
                0,
                tzinfo=timezone.utc,
            ),
            script_runner=script_runner,
        )

    assert called is False
    assert sorted(path.name for path in config.runner.output_root.iterdir()) == sorted(existing_names)
