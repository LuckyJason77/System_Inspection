"""验证 Cron 调度、轮转日志、互斥运行和取消传播。"""

from __future__ import annotations

from dataclasses import replace
import importlib
import io
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
import threading

from apscheduler.events import EVENT_JOB_MAX_INSTANCES
from apscheduler.schedulers.base import STATE_RUNNING, STATE_STOPPED
import pytest

from jmeter_suite.models import (
    AppConfig,
    DingTalkConfig,
    JTLParseResult,
    JMeterConfig,
    ScriptReportResult,
    RunnerConfig,
    ScheduleConfig,
    ReportStatus,
    SuiteReportResult,
)


def _make_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        jmeter=JMeterConfig(
            executable=tmp_path / "jmeter.bat",
            properties_file=tmp_path / "jmeter.properties",
        ),
        schedule=ScheduleConfig(
            timezone="Asia/Shanghai",
            cron="15 3 * * 1-5",
        ),
        runner=RunnerConfig(
            output_root=tmp_path / "runs",
            default_timeout_seconds=3600,
        ),
        scripts=(),
    )


def _capture_logger(name: str) -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(logging.StreamHandler(stream))
    return logger, stream


def _make_cancelled_report(
    config: AppConfig,
    cleanup_confirmed: bool | None,
    *,
    script_status: ReportStatus = ReportStatus.CANCELLED,
    error: str | None = None,
) -> SuiteReportResult:
    run_directory = config.runner.output_root / "cancelled-run"
    artifact_directory = run_directory / "artifacts" / "01-login"
    jtl_path = artifact_directory / "result.jtl"
    now = datetime(2026, 8, 3, 4, 0, tzinfo=timezone.utc)
    script = ScriptReportResult(
        script_name="01-login.jmx",
        status=script_status,
        started_at=now,
        finished_at=now,
        exit_code=None,
        error=error or (
            "JMeter execution was cancelled; "
            "process-tree cleanup failure: taskkill denied"
            if cleanup_confirmed is False
            else "JMeter execution was cancelled"
        ),
        jtl_path=jtl_path,
        log_path=artifact_directory / "jmeter.log",
        report_path=(
            run_directory
            / "2026-08-03_12-00-01_Inspection_Report"
            / "report.html"
        ),
        parse_result=JTLParseResult(
            source_path=jtl_path,
            summaries=(),
            complete=False,
            error="JTL 文件不完整",
            total_samples=0,
            leaf_samples=0,
            passed_leaf_samples=0,
            failed_leaf_samples=0,
            assertion_failures=0,
        ),
        sample_paths=(),
        process_tree_termination_confirmed=cleanup_confirmed,
    )
    return SuiteReportResult(
        run_id=run_directory.name,
        status=ReportStatus.FAILED,
        started_at=now,
        finished_at=now,
        run_directory=run_directory,
        report_path=(
            run_directory
            / "2026-08-03_12-00-01_Inspection_Report"
            / "report.html"
        ),
        manifest_path=run_directory / "manifest.json",
        scripts=(script,),
    )


def test_scheduler_logging_uses_one_utf8_rolling_handler(tmp_path: Path):
    """Duplicate or unbounded handlers would duplicate logs and grow forever."""
    scheduler_module = importlib.import_module("jmeter_suite.scheduler")
    config = _make_config(tmp_path)

    logger = scheduler_module.configure_scheduler_logging(config)
    same_logger = scheduler_module.configure_scheduler_logging(config)
    expected_path = (
        config.runner.output_root
        / ".runtime"
        / "scheduler.log"
    ).resolve()
    handlers = [
        handler
        for handler in logger.handlers
        if isinstance(handler, RotatingFileHandler)
        and Path(handler.baseFilename) == expected_path
    ]

    try:
        assert same_logger is logger
        assert len(handlers) == 1
        assert handlers[0].maxBytes == 10 * 1024 * 1024
        assert handlers[0].backupCount == 5
        assert handlers[0].encoding.lower().replace("-", "") == "utf8"
        logger.info("中文调度日志")
        handlers[0].flush()
        assert "中文调度日志" in expected_path.read_text(encoding="utf-8")
    finally:
        for handler in list(logger.handlers):
            logger.removeHandler(handler)
            handler.close()


def test_build_scheduler_configures_cron_without_startup_run(tmp_path: Path):
    scheduler_module = importlib.import_module("jmeter_suite.scheduler")
    config = _make_config(tmp_path)
    logger, _ = _capture_logger("test.scheduler.cron-only")

    scheduler = scheduler_module.build_scheduler(config, lambda: None, logger)
    job = scheduler.get_jobs()[0]

    assert job.max_instances == 1
    assert job.misfire_grace_time == 30
    assert job.coalesce is True
    assert str(job.trigger.timezone) == "Asia/Shanghai"
    assert "hour='3'" in repr(job.trigger)
    assert "minute='15'" in repr(job.trigger)
    assert not hasattr(job, "next_run_time")


def test_scheduled_job_skips_when_another_suite_holds_the_lock(
    tmp_path: Path,
):
    """An external manual run must make the scheduled trigger skip."""
    scheduler_module = importlib.import_module("jmeter_suite.scheduler")
    locking = importlib.import_module("jmeter_suite.locking")
    config = _make_config(tmp_path)
    logger, stream = _capture_logger("test.scheduler.overlap")
    held_lock = locking.FileLock(
        config.runner.output_root / ".runtime" / "suite.lock"
    )
    assert held_lock.acquire() is True

    try:
        result = scheduler_module.run_scheduled_job(
            config,
            threading.Event(),
            logger,
        )
    finally:
        held_lock.release()

    assert result is None
    assert "调度任务跳过" in stream.getvalue()
    assert "已有巡检任务正在执行" in stream.getvalue()


def test_scheduled_job_contains_unexpected_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """One report crash must not terminate the resident scheduler."""
    scheduler_module = importlib.import_module("jmeter_suite.scheduler")
    config = _make_config(tmp_path)
    logger, stream = _capture_logger("test.scheduler.error")

    def crashing_execution(config, cancel_event):
        raise RuntimeError("report exploded")

    monkeypatch.setattr(
        scheduler_module,
        "execute_suite_once",
        crashing_execution,
    )

    result = scheduler_module.run_scheduled_job(
        config,
        threading.Event(),
        logger,
    )

    assert result is None
    assert "调度任务执行异常" in stream.getvalue()
    assert "report exploded" in stream.getvalue()


def test_scheduled_job_notifies_after_report_completion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Scheduled reports must be handed to DingTalk only after generation."""
    scheduler_module = importlib.import_module("jmeter_suite.scheduler")
    config = _make_config(tmp_path)
    logger, _ = _capture_logger("test.scheduler.notification")
    report = _make_cancelled_report(config, True)
    notifications: list[SuiteReportResult] = []
    monkeypatch.setattr(
        scheduler_module,
        "execute_suite_once",
        lambda actual_config, actual_event: report,
    )

    result = scheduler_module.run_scheduled_job(
        config,
        threading.Event(),
        logger,
        report_notifier=notifications.append,
    )

    assert result is report
    assert notifications == [report]


def test_scheduled_job_contains_notification_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A DingTalk outage must not alter or lose the generated suite result."""
    scheduler_module = importlib.import_module("jmeter_suite.scheduler")
    config = _make_config(tmp_path)
    logger, stream = _capture_logger("test.scheduler.notification-error")
    report = _make_cancelled_report(config, True)
    monkeypatch.setattr(
        scheduler_module,
        "execute_suite_once",
        lambda actual_config, actual_event: report,
    )

    def failed_notification(actual_report):
        raise RuntimeError("DingTalk unavailable")

    result = scheduler_module.run_scheduled_job(
        config,
        threading.Event(),
        logger,
        report_notifier=failed_notification,
    )

    assert result is report
    assert "钉钉报告通知失败" in stream.getvalue()
    assert "DingTalk unavailable" in stream.getvalue()


def test_scheduled_job_logs_unconfirmed_cleanup_after_suite_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """An unconfirmed process-tree cleanup must be prominent in scheduler logs."""
    scheduler_module = importlib.import_module("jmeter_suite.scheduler")
    config = _make_config(tmp_path)
    logger, stream = _capture_logger("test.scheduler.cancel-cleanup")
    cancel_event = threading.Event()
    report = _make_cancelled_report(config, False)

    def cancelled_execution(actual_config, actual_event):
        actual_event.set()
        return report

    monkeypatch.setattr(
        scheduler_module,
        "execute_suite_once",
        cancelled_execution,
    )

    result = scheduler_module.run_scheduled_job(
        config,
        cancel_event,
        logger,
    )

    assert result is report
    assert "人工中断：进程树清理未确认" in stream.getvalue()
    assert "01-login.jmx" in stream.getvalue()
    assert "taskkill denied" in stream.getvalue()


def test_scheduled_job_logs_timeout_cleanup_when_cancel_event_is_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """SIGINT during timeout cleanup must stay prominent in scheduler logs."""
    scheduler_module = importlib.import_module("jmeter_suite.scheduler")
    config = _make_config(tmp_path)
    logger, stream = _capture_logger("test.scheduler.timeout-cancel-cleanup")
    cancel_event = threading.Event()
    report = _make_cancelled_report(
        config,
        False,
        script_status=ReportStatus.TIMEOUT,
        error=(
            "JMeter execution timed out; "
            "process-tree cleanup failure: taskkill denied"
        ),
    )

    def interrupted_timeout(actual_config, actual_event):
        actual_event.set()
        return report

    monkeypatch.setattr(
        scheduler_module,
        "execute_suite_once",
        interrupted_timeout,
    )

    result = scheduler_module.run_scheduled_job(
        config,
        cancel_event,
        logger,
    )

    assert result is report
    assert "人工中断：进程树清理未确认" in stream.getvalue()
    assert "01-login.jmx" in stream.getvalue()
    assert "taskkill denied" in stream.getvalue()


def test_scheduled_job_does_not_label_timeout_cleanup_as_manual_interrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """An ordinary timeout cleanup failure must not be labeled SIGINT."""
    scheduler_module = importlib.import_module("jmeter_suite.scheduler")
    config = _make_config(tmp_path)
    logger, stream = _capture_logger("test.scheduler.timeout-cleanup")
    cancel_event = threading.Event()
    report = _make_cancelled_report(
        config,
        False,
        script_status=ReportStatus.TIMEOUT,
        error=(
            "JMeter execution timed out; "
            "process-tree cleanup failure: taskkill denied"
        ),
    )

    monkeypatch.setattr(
        scheduler_module,
        "execute_suite_once",
        lambda actual_config, actual_event: report,
    )

    result = scheduler_module.run_scheduled_job(
        config,
        cancel_event,
        logger,
    )

    assert result is report
    assert "人工中断：进程树清理未确认" not in stream.getvalue()


@pytest.mark.parametrize("cleanup_confirmed", [True, None])
def test_scheduled_job_does_not_warn_without_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_confirmed: bool | None,
):
    """Confirmed or unattempted cleanup must not emit a scheduler warning."""
    scheduler_module = importlib.import_module("jmeter_suite.scheduler")
    config = _make_config(tmp_path)
    logger, stream = _capture_logger(
        f"test.scheduler.cancel-cleanup-{cleanup_confirmed}"
    )
    cancel_event = threading.Event()
    report = _make_cancelled_report(config, cleanup_confirmed)

    def cancelled_execution(actual_config, actual_event):
        actual_event.set()
        return report

    monkeypatch.setattr(
        scheduler_module,
        "execute_suite_once",
        cancelled_execution,
    )

    result = scheduler_module.run_scheduled_job(
        config,
        cancel_event,
        logger,
    )

    assert result is report
    assert "进程树清理未确认" not in stream.getvalue()


def test_max_instance_event_is_logged_as_an_overlap_skip(tmp_path: Path):
    """APScheduler overlap suppression must be visible in the log."""
    scheduler_module = importlib.import_module("jmeter_suite.scheduler")
    logger, stream = _capture_logger("test.scheduler.event")
    event = SimpleNamespace(
        code=EVENT_JOB_MAX_INSTANCES,
        job_id="jmeter-suite",
    )

    scheduler_module.log_scheduler_event(event, logger)

    assert "重叠触发已跳过" in stream.getvalue()


def test_run_scheduler_refuses_a_second_scheduler_process(tmp_path: Path):
    """A second resident process must fail before entering its main loop."""
    scheduler_module = importlib.import_module("jmeter_suite.scheduler")
    locking = importlib.import_module("jmeter_suite.locking")
    config = _make_config(tmp_path)
    logger, _ = _capture_logger("test.scheduler.instance-conflict")
    held_lock = locking.FileLock(
        config.runner.output_root / ".runtime" / "scheduler.lock"
    )
    assert held_lock.acquire() is True

    try:
        with pytest.raises(scheduler_module.SchedulerAlreadyRunningError):
            scheduler_module.run_scheduler(
                config,
                threading.Event(),
                logger,
            )
    finally:
        held_lock.release()


@pytest.mark.parametrize(
    ("interrupt", "expected_interrupted", "expected_shutdown"),
    [
        (False, False, []),
        (True, True, [True]),
    ],
)
def test_run_scheduler_releases_instance_lock_and_propagates_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupt: bool,
    expected_interrupted: bool,
    expected_shutdown: list[bool],
):
    """Ctrl+C must cancel active work, wait for cleanup, and release the lock."""
    scheduler_module = importlib.import_module("jmeter_suite.scheduler")
    locking = importlib.import_module("jmeter_suite.locking")
    config = _make_config(tmp_path)
    logger, _ = _capture_logger(f"test.scheduler.stop.{interrupt}")
    cancel_event = threading.Event()

    class FakeScheduler:
        def __init__(self):
            self.shutdown_calls: list[bool] = []

        def start(self):
            if interrupt:
                raise KeyboardInterrupt

        def shutdown(self, *, wait: bool):
            self.shutdown_calls.append(wait)

    fake_scheduler = FakeScheduler()
    monkeypatch.setattr(
        scheduler_module,
        "build_scheduler",
        lambda actual_config, job, actual_logger: fake_scheduler,
    )

    interrupted = scheduler_module.run_scheduler(
        config,
        cancel_event,
        logger,
    )

    assert interrupted is expected_interrupted
    assert cancel_event.is_set() is interrupt
    assert fake_scheduler.shutdown_calls == expected_shutdown
    replacement = locking.FileLock(
        config.runner.output_root / ".runtime" / "scheduler.lock"
    )
    assert replacement.acquire() is True
    replacement.release()


def test_run_scheduler_job_callback_receives_the_shared_cancellation_event(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A scheduled job must observe the same Event that Ctrl+C will set."""
    scheduler_module = importlib.import_module("jmeter_suite.scheduler")
    config = _make_config(tmp_path)
    logger, _ = _capture_logger("test.scheduler.shared-event")
    cancel_event = threading.Event()
    observed: dict[str, object] = {}

    def capture_job(actual_config, actual_event, actual_logger):
        observed["config"] = actual_config
        observed["event"] = actual_event
        observed["logger"] = actual_logger
        return None

    class CallbackScheduler:
        def __init__(self, job):
            self.job = job

        def start(self):
            self.job()

    monkeypatch.setattr(scheduler_module, "run_scheduled_job", capture_job)
    monkeypatch.setattr(
        scheduler_module,
        "build_scheduler",
        lambda actual_config, job, actual_logger: CallbackScheduler(job),
    )

    interrupted = scheduler_module.run_scheduler(
        config,
        cancel_event,
        logger,
    )

    assert interrupted is False
    assert observed == {
        "config": config,
        "event": cancel_event,
        "logger": logger,
    }


def test_run_scheduler_starts_dingtalk_and_routes_scheduled_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Only the resident scheduler process should own Stream and notifications."""
    scheduler_module = importlib.import_module("jmeter_suite.scheduler")
    config = replace(
        _make_config(tmp_path),
        dingtalk=DingTalkConfig(
            enabled=True,
            client_id="ding-client",
            client_secret="secret",
        ),
    )
    logger, _ = _capture_logger("test.scheduler.dingtalk-runtime")
    cancel_event = threading.Event()
    report = _make_cancelled_report(config, True)

    class FakeDingTalkService:
        def __init__(self):
            self.start_calls = 0
            self.stop_calls = 0
            self.notifications: list[SuiteReportResult] = []

        def start(self):
            self.start_calls += 1

        def stop(self):
            self.stop_calls += 1

        def notify_report(self, actual_report):
            self.notifications.append(actual_report)

    service = FakeDingTalkService()
    factory_calls: list[tuple[AppConfig, object, logging.Logger]] = []

    def service_factory(actual_config, actual_event, actual_logger):
        factory_calls.append((actual_config, actual_event, actual_logger))
        return service

    class CallbackScheduler:
        def __init__(self, job):
            self.job = job

        def start(self):
            self.job()

    monkeypatch.setattr(
        scheduler_module,
        "execute_suite_once",
        lambda actual_config, actual_event: report,
    )
    monkeypatch.setattr(
        scheduler_module,
        "build_scheduler",
        lambda actual_config, job, actual_logger: CallbackScheduler(job),
    )

    interrupted = scheduler_module.run_scheduler(
        config,
        cancel_event,
        logger,
        dingtalk_service_factory=service_factory,
    )

    assert interrupted is False
    assert factory_calls == [(config, cancel_event, logger)]
    assert service.start_calls == 1
    assert service.stop_calls == 1
    assert service.notifications == [report]


def test_run_scheduler_does_not_create_dingtalk_service_when_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Existing deployments without a DingTalk block must stay unchanged."""
    scheduler_module = importlib.import_module("jmeter_suite.scheduler")
    config = _make_config(tmp_path)
    logger, _ = _capture_logger("test.scheduler.dingtalk-disabled")

    class EmptyScheduler:
        def start(self):
            return None

    monkeypatch.setattr(
        scheduler_module,
        "build_scheduler",
        lambda actual_config, job, actual_logger: EmptyScheduler(),
    )

    interrupted = scheduler_module.run_scheduler(
        config,
        threading.Event(),
        logger,
        dingtalk_service_factory=lambda *args: pytest.fail(
            "disabled DingTalk must not create a service"
        ),
    )

    assert interrupted is False


def test_run_scheduler_preserves_start_failure_while_stopped_and_releases_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """An exception before APScheduler reaches RUNNING is a startup failure."""
    scheduler_module = importlib.import_module("jmeter_suite.scheduler")
    locking = importlib.import_module("jmeter_suite.locking")
    config = _make_config(tmp_path)
    logger, stream = _capture_logger("test.scheduler.start-error")

    class BrokenScheduler:
        state = STATE_STOPPED

        def start(self):
            raise RuntimeError("executor initialization failed")

    monkeypatch.setattr(
        scheduler_module,
        "build_scheduler",
        lambda actual_config, job, actual_logger: BrokenScheduler(),
    )

    with pytest.raises(
        RuntimeError,
        match="executor initialization failed",
    ) as error:
        scheduler_module.run_scheduler(config, threading.Event(), logger)

    assert not isinstance(error.value, scheduler_module.SchedulerRuntimeError)
    assert "调度器启动异常" in stream.getvalue()
    replacement = locking.FileLock(
        config.runner.output_root / ".runtime" / "scheduler.lock"
    )
    assert replacement.acquire() is True
    replacement.release()


def test_run_scheduler_wraps_post_start_failure_as_runtime_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Once blocking start is entered, infrastructure failure is runtime error."""
    scheduler_module = importlib.import_module("jmeter_suite.scheduler")
    locking = importlib.import_module("jmeter_suite.locking")
    config = _make_config(tmp_path)
    logger, stream = _capture_logger("test.scheduler.runtime-error")

    class BrokenScheduler:
        state = STATE_STOPPED

        def start(self):
            self.state = STATE_RUNNING
            raise RuntimeError("executor infrastructure failed")

    monkeypatch.setattr(
        scheduler_module,
        "build_scheduler",
        lambda actual_config, job, actual_logger: BrokenScheduler(),
    )

    with pytest.raises(
        scheduler_module.SchedulerRuntimeError,
        match="executor infrastructure failed",
    ) as error:
        scheduler_module.run_scheduler(config, threading.Event(), logger)

    assert isinstance(error.value.__cause__, RuntimeError)
    assert "调度器运行时异常" in stream.getvalue()
    replacement = locking.FileLock(
        config.runner.output_root / ".runtime" / "scheduler.lock"
    )
    assert replacement.acquire() is True
    replacement.release()


def test_run_scheduler_does_not_wrap_scheduler_construction_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A scheduler failure before blocking start remains a startup exception."""
    scheduler_module = importlib.import_module("jmeter_suite.scheduler")
    locking = importlib.import_module("jmeter_suite.locking")
    config = _make_config(tmp_path)
    logger, _ = _capture_logger("test.scheduler.build-error")

    def broken_build(actual_config, job, actual_logger):
        raise RuntimeError("scheduler construction failed")

    monkeypatch.setattr(scheduler_module, "build_scheduler", broken_build)

    with pytest.raises(RuntimeError, match="scheduler construction failed") as error:
        scheduler_module.run_scheduler(config, threading.Event(), logger)

    assert not isinstance(
        error.value,
        scheduler_module.SchedulerRuntimeError,
    )
    replacement = locking.FileLock(
        config.runner.output_root / ".runtime" / "scheduler.lock"
    )
    assert replacement.acquire() is True
    replacement.release()
