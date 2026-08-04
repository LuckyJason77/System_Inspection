"""编排一次性运行和调度服务的命令行生命周期、校验、取消与结果输出。"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
import signal
import sys
from threading import Event
from types import FrameType

from .config import (
    ConfigError,
    JMeterValidationCancelled,
    load_config,
    validate_jmeter_executable,
)
from .locking import LockUnavailableError
from .models import AppConfig, ReportStatus, SuiteReportResult
from .orchestration import SuiteStartupError, execute_suite_once
from .report import status_label
from .scheduler import (
    SchedulerAlreadyRunningError,
    SchedulerRuntimeError,
    run_scheduler,
)


JMeterValidator = Callable[[AppConfig, Event], str]
SuiteExecutor = Callable[[AppConfig, Event], SuiteReportResult]
SchedulerRunner = Callable[[AppConfig, Event], bool]


class _ApplicationCancelled(RuntimeError):
    pass


def _error(message: str, code: int) -> int:
    print(message, file=sys.stderr)
    return code


def _print_validation_summary(config: AppConfig, version: str) -> None:
    print("配置校验通过")
    print(f"JMeter 版本: {version}")
    print(f"JMeter 路径: {config.jmeter.executable}")
    print(f"JMeter properties: {config.jmeter.properties_file}")
    print(f"时区: {config.schedule.timezone}")
    print(f"Cron: {config.schedule.cron}")
    print(f"默认超时: {config.runner.default_timeout_seconds} 秒")
    print(f"输出目录: {config.runner.output_root}")
    print(f"脚本目录: {config.scripts[0].working_directory}")
    print(f"发现脚本: {len(config.scripts)}")
    for index, script in enumerate(config.scripts, start=1):
        print(f"{index}. {script.name}（超时 {script.timeout_seconds} 秒）")


def _print_run_summary(result: SuiteReportResult) -> None:
    print(f"套件状态: {status_label(result.status)}")
    print(f"运行 ID: {result.run_id}")
    print(f"运行目录: {result.run_directory}")
    print(f"报告: {result.report_path}")


def _warn_unconfirmed_cancelled_cleanup(
    result: SuiteReportResult,
    cancel_event: Event,
) -> None:
    for script in result.scripts:
        if (
            script.process_tree_termination_confirmed is False
            and (
                cancel_event.is_set()
                or script.status is ReportStatus.CANCELLED
            )
        ):
            detail = script.error or "未提供清理错误详情"
            print(
                "人工中断：进程树清理未确认: "
                f"{script.script_name}: {detail}",
                file=sys.stderr,
            )


@contextmanager
def _signal_cancellation(cancel_event: Event) -> Iterator[None]:
    previous_handler = signal.getsignal(signal.SIGINT)

    def request_cancel(
        signal_number: int,
        frame: FrameType | None,
    ) -> None:
        if not cancel_event.is_set():
            print(
                "已收到人工中断，正在终止 JMeter 进程树……",
                file=sys.stderr,
            )
        cancel_event.set()

    installed = False
    try:
        signal.signal(signal.SIGINT, request_cancel)
        installed = True
    except (OSError, ValueError):
        # Embedded callers may invoke the application outside the main thread.
        pass
    try:
        yield
    finally:
        if installed:
            signal.signal(signal.SIGINT, previous_handler)


def _raise_if_cancelled(cancel_event: Event) -> None:
    if cancel_event.is_set():
        raise _ApplicationCancelled


def _preflight(
    config_path: Path,
    cancel_event: Event,
    validator: JMeterValidator,
) -> tuple[AppConfig, str]:
    try:
        config = load_config(config_path)
        _raise_if_cancelled(cancel_event)
        version = validator(config, cancel_event)
        _raise_if_cancelled(cancel_event)
        return config, version
    except (
        ConfigError,
        JMeterValidationCancelled,
        _ApplicationCancelled,
    ):
        raise
    except Exception as error:
        raise ConfigError(f"启动检查失败: {error}") from error


def _cancelled_validation_error(error: JMeterValidationCancelled) -> int:
    if not error.cleanup_confirmed:
        print(
            "人工中断：进程树清理未确认: "
            f"{error.cleanup_error}",
            file=sys.stderr,
        )
    return 130


def run_once_application(
    config_path: Path,
    *,
    jmeter_validator: JMeterValidator = validate_jmeter_executable,
    suite_executor: SuiteExecutor = execute_suite_once,
) -> int:
    cancel_event = Event()
    try:
        with _signal_cancellation(cancel_event):
            config, version = _preflight(
                config_path,
                cancel_event,
                jmeter_validator,
            )
            _print_validation_summary(config, version)
            _raise_if_cancelled(cancel_event)
            result = suite_executor(config, cancel_event)
            _warn_unconfirmed_cancelled_cleanup(result, cancel_event)
            _raise_if_cancelled(cancel_event)
            _print_run_summary(result)
            _raise_if_cancelled(cancel_event)
        if cancel_event.is_set() or result.status is ReportStatus.CANCELLED:
            return 130
        return 0 if result.status is ReportStatus.PASSED else 1
    except JMeterValidationCancelled as error:
        return _cancelled_validation_error(error)
    except _ApplicationCancelled:
        return 130
    except ConfigError as error:
        if cancel_event.is_set():
            return 130
        return _error(f"配置错误：{error}", 2)
    except (LockUnavailableError, SuiteStartupError) as error:
        if cancel_event.is_set():
            return 130
        return _error(f"启动失败：{error}", 2)
    except KeyboardInterrupt:
        cancel_event.set()
        return _error("已收到人工中断", 130)
    except Exception as error:
        if cancel_event.is_set():
            return 130
        return _error(f"运行失败：{error}", 1)

def run_scheduler_application(
    config_path: Path,
    *,
    jmeter_validator: JMeterValidator = validate_jmeter_executable,
    scheduler_runner: SchedulerRunner = run_scheduler,
) -> int:
    cancel_event = Event()
    try:
        with _signal_cancellation(cancel_event):
            config, version = _preflight(
                config_path,
                cancel_event,
                jmeter_validator,
            )
            _print_validation_summary(config, version)
            _raise_if_cancelled(cancel_event)
    except JMeterValidationCancelled as error:
        return _cancelled_validation_error(error)
    except _ApplicationCancelled:
        return 130
    except ConfigError as error:
        if cancel_event.is_set():
            return 130
        return _error(f"配置错误：{error}", 2)
    except KeyboardInterrupt:
        cancel_event.set()
        return _error("已收到人工中断", 130)
    except Exception as error:
        if cancel_event.is_set():
            return 130
        return _error(f"调度器启动失败：{error}", 2)

    try:
        interrupted = scheduler_runner(config, cancel_event)
        return 130 if interrupted or cancel_event.is_set() else 0
    except SchedulerRuntimeError as error:
        if cancel_event.is_set():
            return 130
        return _error(f"调度器运行失败：{error}", 1)
    except SchedulerAlreadyRunningError:
        if cancel_event.is_set():
            return 130
        return _error("调度器启动失败：已有调度器进程正在运行", 2)
    except KeyboardInterrupt:
        cancel_event.set()
        return _error("已收到人工中断", 130)
    except Exception as error:
        if cancel_event.is_set():
            return 130
        return _error(f"调度器启动失败：{error}", 2)
