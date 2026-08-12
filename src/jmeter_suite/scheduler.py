"""配置 APScheduler、轮转日志和互斥锁，按 Cron 触发巡检任务。"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import Event
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.events import (
    EVENT_JOB_ERROR,
    EVENT_JOB_EXECUTED,
    EVENT_JOB_MAX_INSTANCES,
    EVENT_JOB_MISSED,
)
from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.schedulers.base import STATE_STOPPED, SchedulerNotRunningError
from apscheduler.triggers.cron import CronTrigger

from .dingtalk_service import create_dingtalk_service
from .locking import FileLock, LockUnavailableError
from .models import AppConfig, ReportStatus, SuiteReportResult
from .orchestration import execute_suite_once, runtime_directory


_LOGGER_NAME = "jmeter_suite.scheduler"
_MANAGED_HANDLER_ATTRIBUTE = "_jmeter_suite_scheduler_handler"
_LOG_MAX_BYTES = 10 * 1024 * 1024
_LOG_BACKUP_COUNT = 5
_SCHEDULER_JOB_ID = "jmeter-suite"


class SchedulerAlreadyRunningError(RuntimeError):
    def __init__(self, path: Path):
        self.path = path
        super().__init__(f"已有调度器进程正在运行: {path}")


class SchedulerRuntimeError(RuntimeError):
    """The scheduler entered its blocking runtime and then failed."""


def scheduler_lock_path(config: AppConfig) -> Path:
    return runtime_directory(config) / "scheduler.lock"


def scheduler_log_path(config: AppConfig) -> Path:
    return runtime_directory(config) / "scheduler.log"


def configure_scheduler_logging(config: AppConfig) -> logging.Logger:
    log_path = scheduler_log_path(config).resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(_LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    matching_handler: RotatingFileHandler | None = None
    for handler in list(logger.handlers):
        if not getattr(handler, _MANAGED_HANDLER_ATTRIBUTE, False):
            continue
        if (
            isinstance(handler, RotatingFileHandler)
            and Path(handler.baseFilename) == log_path
        ):
            matching_handler = handler
            continue
        logger.removeHandler(handler)
        handler.close()

    if matching_handler is None:
        matching_handler = RotatingFileHandler(
            log_path,
            maxBytes=_LOG_MAX_BYTES,
            backupCount=_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        setattr(
            matching_handler,
            _MANAGED_HANDLER_ATTRIBUTE,
            True,
        )
        matching_handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s %(message)s"
            )
        )
        logger.addHandler(matching_handler)
    return logger


def log_scheduler_event(event: Any, logger: logging.Logger) -> None:
    if event.code == EVENT_JOB_MAX_INSTANCES:
        logger.warning(
            "调度任务 %s 重叠触发已跳过：max_instances=1",
            event.job_id,
        )
    elif event.code == EVENT_JOB_MISSED:
        logger.warning("调度任务 %s 已错过执行窗口", event.job_id)
    elif event.code == EVENT_JOB_ERROR:
        logger.error(
            "调度任务 %s 发生未捕获异常: %s",
            event.job_id,
            getattr(event, "exception", "未知错误"),
        )
    elif event.code == EVENT_JOB_EXECUTED:
        logger.info("调度任务 %s 本次触发结束", event.job_id)


def build_scheduler(
    config: AppConfig,
    job_function: Callable[[], object],
    logger: logging.Logger,
) -> BlockingScheduler:
    timezone = ZoneInfo(config.schedule.timezone)
    scheduler = BlockingScheduler(
        jobstores={"default": MemoryJobStore()},
        timezone=timezone,
    )
    trigger = CronTrigger.from_crontab(
        config.schedule.cron,
        timezone=timezone,
    )
    scheduler.add_job(
        job_function,
        trigger=trigger,
        id=_SCHEDULER_JOB_ID,
        max_instances=1,
        misfire_grace_time=30,
        coalesce=True,
    )
    scheduler.add_listener(
        partial(log_scheduler_event, logger=logger),
        EVENT_JOB_MAX_INSTANCES
        | EVENT_JOB_MISSED
        | EVENT_JOB_ERROR
        | EVENT_JOB_EXECUTED,
    )
    return scheduler


def run_scheduled_job(
    config: AppConfig,
    cancel_event: Event,
    logger: logging.Logger,
    *,
    report_notifier: Callable[[SuiteReportResult], None] | None = None,
) -> SuiteReportResult | None:
    if cancel_event.is_set():
        logger.info("调度任务跳过：调度器正在停止")
        return None
    try:
        result = execute_suite_once(config, cancel_event)
    except LockUnavailableError:
        logger.warning("调度任务跳过：已有巡检任务正在执行")
        return None
    except Exception:
        logger.exception("调度任务执行异常")
        return None

    for script in result.scripts:
        if (
            script.process_tree_termination_confirmed is False
            and (
                cancel_event.is_set()
                or script.status is ReportStatus.CANCELLED
            )
        ):
            logger.error(
                "人工中断：进程树清理未确认: script=%s detail=%s",
                script.script_name,
                script.error or "未提供清理错误详情",
            )
    logger.info(
        "调度任务完成：status=%s run_directory=%s report=%s",
        result.status.value,
        result.run_directory,
        result.report_path,
    )
    if report_notifier is not None:
        try:
            report_notifier(result)
        except Exception:
            logger.exception(
                "钉钉报告通知失败：run_id=%s",
                result.run_id,
            )
    return result


def run_scheduler(
    config: AppConfig,
    cancel_event: Event | None = None,
    logger: logging.Logger | None = None,
    *,
    dingtalk_service_factory: Callable[
        [AppConfig, Event, logging.Logger],
        Any,
    ] = create_dingtalk_service,
) -> bool:
    """Run until shutdown; return True only when stopped by Ctrl+C."""
    cancellation = cancel_event or Event()
    scheduler_logger = logger or configure_scheduler_logging(config)
    instance_lock = FileLock(scheduler_lock_path(config))
    if not instance_lock.acquire():
        raise SchedulerAlreadyRunningError(instance_lock.path)

    dingtalk_service = None
    try:
        if config.dingtalk.enabled:
            dingtalk_service = dingtalk_service_factory(
                config,
                cancellation,
                scheduler_logger,
            )
            dingtalk_service.start()
            scheduled_job = partial(
                run_scheduled_job,
                config,
                cancellation,
                scheduler_logger,
                report_notifier=dingtalk_service.notify_report,
            )
        else:
            scheduled_job = partial(
                run_scheduled_job,
                config,
                cancellation,
                scheduler_logger,
            )
        scheduler = build_scheduler(
            config,
            scheduled_job,
            scheduler_logger,
        )
        scheduler_logger.info(
            "调度器启动：cron=%s timezone=%s",
            config.schedule.cron,
            config.schedule.timezone,
        )
        print(
            "调度器启动："
            f"cron={config.schedule.cron} "
            f"timezone={config.schedule.timezone}"
        )
        try:
            scheduler.start()
        except KeyboardInterrupt:
            cancellation.set()
            scheduler_logger.warning("收到人工中断，正在取消运行中的任务")
            try:
                scheduler.shutdown(wait=True)
            except SchedulerNotRunningError:
                pass
            return True
        except Exception as error:
            if getattr(scheduler, "state", STATE_STOPPED) == STATE_STOPPED:
                scheduler_logger.exception("调度器启动异常")
                raise
            scheduler_logger.exception("调度器运行时异常")
            raise SchedulerRuntimeError(str(error)) from error
        scheduler_logger.info("调度器正常退出")
        return False
    finally:
        if dingtalk_service is not None:
            dingtalk_service.stop()
        instance_lock.release()
