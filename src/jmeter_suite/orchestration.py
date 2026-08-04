"""在持有套件锁的完整生命周期内串联 JMeter 执行与报告生成。"""

from __future__ import annotations

from pathlib import Path
from threading import Event

from .locking import FileLock, LockUnavailableError
from .models import AppConfig, SuiteReportResult
from .report import generate_suite_report
from .runner import run_suite_processes


class SuiteStartupError(RuntimeError):
    """Raised when the execution lock cannot be initialized."""


def runtime_directory(config: AppConfig) -> Path:
    return config.runner.output_root / ".runtime"


def suite_lock_path(config: AppConfig) -> Path:
    return runtime_directory(config) / "suite.lock"


def execute_suite_once(
    config: AppConfig,
    cancel_event: Event | None = None,
) -> SuiteReportResult:
    """Execute and report one suite while holding the global run lock."""
    lock = FileLock(suite_lock_path(config))
    try:
        acquired = lock.acquire()
    except OSError as error:
        raise SuiteStartupError(
            f"无法初始化运行锁 {lock.path}: {error}"
        ) from error
    if not acquired:
        raise LockUnavailableError(lock.path)

    try:
        process_result = run_suite_processes(
            config,
            cancel_event=cancel_event,
        )
        return generate_suite_report(
            process_result,
            excluded_url_keywords=(
                config.report.excluded_url_keywords
            ),
            additional_date_parameter_names=(
                config.report.additional_date_parameter_names
            ),
        )
    finally:
        lock.release()
