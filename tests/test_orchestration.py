"""验证套件锁覆盖执行与报告生成的完整编排生命周期。"""

from datetime import datetime, timezone
from dataclasses import replace
import importlib
from pathlib import Path
import threading

import pytest

from jmeter_suite.models import (
    AppConfig,
    ExecutionStatus,
    JMeterConfig,
    JMeterExecutionResult,
    ReportConfig,
    ReportStatus,
    RunnerConfig,
    ScheduleConfig,
    ScriptConfig,
    SuiteProcessResult,
)


def _make_config(tmp_path: Path) -> AppConfig:
    script = ScriptConfig(
        name="巡检",
        jmx=tmp_path / "plan.jmx",
        working_directory=tmp_path,
        timeout_seconds=60,
    )
    return AppConfig(
        jmeter=JMeterConfig(
            executable=tmp_path / "jmeter.bat",
            properties_file=tmp_path / "jmeter.properties",
        ),
        schedule=ScheduleConfig(
            timezone="Asia/Shanghai",
            cron="0 2 * * *",
        ),
        runner=RunnerConfig(
            output_root=tmp_path / "runs",
            default_timeout_seconds=3600,
        ),
        scripts=(script,),
    )


def _successful_process_result(
    config: AppConfig,
    *,
    run_id: str = "20260731-120000-12345678",
) -> SuiteProcessResult:
    run_directory = config.runner.output_root / run_id
    artifact_directory = run_directory / "artifacts" / "01-inspection"
    artifact_directory.mkdir(parents=True)
    jtl_path = artifact_directory / "result.jtl"
    jtl_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<testResults version="1.2">
  <httpSample s="true" lb="健康检查" rc="200">
    <responseData>正常</responseData>
  </httpSample>
</testResults>
""",
        encoding="utf-8",
    )
    log_path = artifact_directory / "jmeter.log"
    log_path.write_text("done\n", encoding="utf-8")
    started_at = datetime(2026, 7, 31, 4, 0, tzinfo=timezone.utc)
    execution = JMeterExecutionResult(
        script_name="巡检",
        status=ExecutionStatus.COMPLETED,
        started_at=started_at,
        finished_at=datetime(2026, 7, 31, 4, 0, 1, tzinfo=timezone.utc),
        exit_code=0,
        error=None,
        jtl_path=jtl_path,
        log_path=log_path,
        stdout="",
        stderr="",
    )
    return SuiteProcessResult(
        run_id=run_id,
        run_directory=run_directory,
        started_at=started_at,
        finished_at=datetime(2026, 7, 31, 4, 0, 1, tzinfo=timezone.utc),
        executions=(execution,),
    )


def test_execute_suite_once_holds_the_lock_through_report_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Dropping the lock early would allow a second run during reporting."""
    orchestration = importlib.import_module("jmeter_suite.orchestration")
    locking = importlib.import_module("jmeter_suite.locking")
    config = _make_config(tmp_path)
    cancel_event = threading.Event()
    observed: dict[str, object] = {}

    def fake_process_runner(actual_config, *, cancel_event=None):
        observed["config"] = actual_config
        observed["cancel_event"] = cancel_event
        return _successful_process_result(config)

    real_reporter = orchestration.generate_suite_report

    def checking_reporter(
        process_result,
        *,
        excluded_url_keywords=(),
        additional_date_parameter_names=(),
    ):
        competing = locking.FileLock(
            config.runner.output_root / ".runtime" / "suite.lock"
        )
        observed["competing_acquired"] = competing.acquire()
        competing.release()
        return real_reporter(
            process_result,
            excluded_url_keywords=excluded_url_keywords,
            additional_date_parameter_names=(
                additional_date_parameter_names
            ),
        )

    monkeypatch.setattr(
        orchestration,
        "run_suite_processes",
        fake_process_runner,
    )
    monkeypatch.setattr(
        orchestration,
        "generate_suite_report",
        checking_reporter,
    )

    result = orchestration.execute_suite_once(config, cancel_event)

    assert result.status is ReportStatus.PASSED
    assert result.report_path.is_file()
    assert result.manifest_path.is_file()
    assert observed == {
        "config": config,
        "cancel_event": cancel_event,
        "competing_acquired": False,
    }
    replacement = locking.FileLock(
        config.runner.output_root / ".runtime" / "suite.lock"
    )
    assert replacement.acquire() is True
    replacement.release()


def test_execute_suite_with_acquired_lock_releases_after_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A bot-accepted run must own the cross-process lock before replying."""
    orchestration = importlib.import_module("jmeter_suite.orchestration")
    locking = importlib.import_module("jmeter_suite.locking")
    config = _make_config(tmp_path)
    acquired_lock = locking.FileLock(
        config.runner.output_root / ".runtime" / "suite.lock"
    )
    assert acquired_lock.acquire() is True
    observed_locked: list[bool] = []

    monkeypatch.setattr(
        orchestration,
        "run_suite_processes",
        lambda actual_config, *, cancel_event=None: (
            _successful_process_result(config)
        ),
    )
    real_reporter = orchestration.generate_suite_report

    def checking_reporter(process_result, **kwargs):
        competitor = locking.FileLock(acquired_lock.path)
        observed_locked.append(competitor.acquire())
        competitor.release()
        return real_reporter(process_result, **kwargs)

    monkeypatch.setattr(
        orchestration,
        "generate_suite_report",
        checking_reporter,
    )

    result = orchestration.execute_suite_with_acquired_lock(
        config,
        acquired_lock,
        threading.Event(),
    )

    assert result.status is ReportStatus.PASSED
    assert observed_locked == [False]
    replacement = locking.FileLock(acquired_lock.path)
    assert replacement.acquire() is True
    replacement.release()


def test_execute_suite_once_rejects_overlap_before_starting_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Ignoring lock conflict would run manual and scheduled suites together."""
    orchestration = importlib.import_module("jmeter_suite.orchestration")
    locking = importlib.import_module("jmeter_suite.locking")
    config = _make_config(tmp_path)
    held_lock = locking.FileLock(
        config.runner.output_root / ".runtime" / "suite.lock"
    )
    assert held_lock.acquire() is True
    runner_called = False

    def forbidden_runner(*args, **kwargs):
        nonlocal runner_called
        runner_called = True
        raise AssertionError("runner must not start while lock is held")

    monkeypatch.setattr(
        orchestration,
        "run_suite_processes",
        forbidden_runner,
    )
    try:
        with pytest.raises(locking.LockUnavailableError):
            orchestration.execute_suite_once(config, threading.Event())
    finally:
        held_lock.release()

    assert runner_called is False


def test_execute_suite_once_releases_lock_when_reporting_crashes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A report exception must not permanently block future inspections."""
    orchestration = importlib.import_module("jmeter_suite.orchestration")
    locking = importlib.import_module("jmeter_suite.locking")
    config = _make_config(tmp_path)
    monkeypatch.setattr(
        orchestration,
        "run_suite_processes",
        lambda actual_config, *, cancel_event=None: (
            _successful_process_result(config)
        ),
    )

    def crashing_reporter(process_result, **kwargs):
        raise RuntimeError("template failure")

    monkeypatch.setattr(
        orchestration,
        "generate_suite_report",
        crashing_reporter,
    )

    with pytest.raises(RuntimeError, match="template failure"):
        orchestration.execute_suite_once(config, threading.Event())

    replacement = locking.FileLock(
        config.runner.output_root / ".runtime" / "suite.lock"
    )
    assert replacement.acquire() is True
    replacement.release()


def test_execute_suite_once_passes_report_filter_config_to_reporter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    orchestration = importlib.import_module("jmeter_suite.orchestration")
    config = replace(
        _make_config(tmp_path),
        report=ReportConfig(
            excluded_url_keywords=("/health",),
            additional_date_parameter_names=("account_period",),
        ),
    )
    process_result = _successful_process_result(config)
    observed: dict[str, object] = {}

    monkeypatch.setattr(
        orchestration,
        "run_suite_processes",
        lambda actual_config, *, cancel_event=None: process_result,
    )

    def capture_reporter(
        actual_result,
        *,
        excluded_url_keywords: tuple[str, ...],
        additional_date_parameter_names: tuple[str, ...],
    ):
        observed["process_result"] = actual_result
        observed["excluded_url_keywords"] = excluded_url_keywords
        observed["additional_date_parameter_names"] = (
            additional_date_parameter_names
        )
        raise RuntimeError("captured")

    monkeypatch.setattr(
        orchestration,
        "generate_suite_report",
        capture_reporter,
    )

    with pytest.raises(RuntimeError, match="captured"):
        orchestration.execute_suite_once(config, threading.Event())

    assert observed == {
        "process_result": process_result,
        "excluded_url_keywords": ("/health",),
        "additional_date_parameter_names": ("account_period",),
    }


def test_execute_suite_once_classifies_lock_initialization_as_startup_error(
    tmp_path: Path,
):
    """A lock-path filesystem failure belongs to exit-code-2 startup errors."""
    orchestration = importlib.import_module("jmeter_suite.orchestration")
    config = _make_config(tmp_path)
    blocking_file = tmp_path / "not-a-directory"
    blocking_file.write_text("blocked", encoding="utf-8")
    config = replace(
        config,
        runner=replace(
            config.runner,
            output_root=blocking_file / "runs",
        ),
    )

    with pytest.raises(orchestration.SuiteStartupError) as captured:
        orchestration.execute_suite_once(config, threading.Event())

    assert "无法初始化运行锁" in str(captured.value)
    assert str(blocking_file / "runs" / ".runtime") in str(captured.value)
