"""在常驻调度进程中协调钉钉 Stream、手动巡检和报告通知。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
import json
import logging
from pathlib import Path
import platform
import requests
from threading import Event, Lock, Thread
from typing import Any, Protocol
from urllib.parse import quote_plus

try:
    import dingtalk_stream
    import websockets
except ImportError:  # pragma: no cover - exercised by startup validation
    dingtalk_stream = None
    websockets = None

from .dingtalk_api import (
    DingTalkApiClient,
    DingTalkCredentialError,
    DingTalkTransientError,
)
from .dingtalk_binding import (
    DingTalkBindingError,
    DingTalkBindingStore,
)
from .dingtalk_commands import (
    DingTalkCommandController,
    DingTalkIncomingMessage,
)
from .dingtalk_notification import (
    DingTalkDeliveryResult,
    DingTalkNotifier,
)
from .locking import FileLock
from .models import AppConfig, SuiteReportResult
from .orchestration import (
    execute_suite_with_acquired_lock,
    suite_lock_path,
)


_STREAM_RECONNECT_SECONDS = 10.0
_STREAM_ERROR_RETRY_SECONDS = 3.0
_STREAM_OPEN_TIMEOUT_SECONDS = 10.0
_STREAM_STOP_TIMEOUT_SECONDS = 15.0


class DingTalkStartupError(RuntimeError):
    """钉钉功能无法安全启动。"""


class _StreamLifecycle(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...


class _Notifier(Protocol):
    def notify(self, report, binding) -> object: ...


LockedSuiteExecutor = Callable[
    [AppConfig, FileLock, Event],
    SuiteReportResult,
]


_ChatbotHandlerBase = (
    dingtalk_stream.ChatbotHandler
    if dingtalk_stream is not None
    else object
)


class DingTalkChatbotHandler(_ChatbotHandlerBase):
    """把 SDK 消息转换为不依赖 SDK 的命令模型。"""

    def __init__(
        self,
        controller: DingTalkCommandController,
        logger: logging.Logger,
    ):
        if dingtalk_stream is None:
            raise DingTalkStartupError("缺少 dingtalk-stream 依赖")
        super().__init__()
        self._controller = controller
        self.logger = logger

    async def process(self, callback: Any) -> tuple[int, str]:
        try:
            incoming = dingtalk_stream.ChatbotMessage.from_dict(
                callback.data
            )
            message = DingTalkIncomingMessage(
                message_id=incoming.message_id or "",
                conversation_id=incoming.conversation_id or "",
                conversation_title=incoming.conversation_title or "",
                sender_staff_id=incoming.sender_staff_id or "",
                sender_nick=incoming.sender_nick or "",
                is_admin=incoming.is_admin is True,
                is_in_at_list=incoming.is_in_at_list is True,
                conversation_type=str(incoming.conversation_type or ""),
                message_type=incoming.message_type or "",
                text=(
                    incoming.text.content
                    if incoming.text is not None
                    and incoming.text.content is not None
                    else ""
                ),
            )

            def reply(text: str) -> None:
                try:
                    self.reply_text(text, incoming)
                except Exception:
                    self.logger.exception("钉钉机器人命令回复失败")

            self._controller.handle(message, reply)
            return dingtalk_stream.AckMessage.STATUS_OK, "OK"
        except Exception:
            self.logger.exception("钉钉机器人消息处理失败")
            return (
                dingtalk_stream.AckMessage.STATUS_SYSTEM_EXCEPTION,
                "ERROR",
            )


if dingtalk_stream is not None:

    class _StoppableDingTalkStreamClient(
        dingtalk_stream.DingTalkStreamClient
    ):
        """为稳定版 SDK 补充可由 Ctrl+C 驱动的停止生命周期。"""

        def __init__(self, *args: Any, **kwargs: Any):
            super().__init__(*args, **kwargs)
            self._jmeter_stop_event: asyncio.Event | None = None
            self._jmeter_background_tasks: set[asyncio.Task[Any]] = set()

        def open_connection(self) -> dict[str, Any] | None:
            """Open Stream with a bounded request and secret-safe logging."""
            topics: list[dict[str, str]] = []
            if self._is_event_required:
                topics.append({"type": "EVENT", "topic": "*"})
            topics.extend(
                {"type": "CALLBACK", "topic": topic}
                for topic in self.callback_handler_map
            )
            try:
                local_ip = self.get_host_ip()
            except Exception:
                local_ip = ""
            request_body = {
                "clientId": self.credential.client_id,
                "clientSecret": self.credential.client_secret,
                "subscriptions": topics,
                "ua": "jmeter-suite-runner/dingtalk-stream",
                "localIp": local_ip,
            }
            try:
                response = requests.post(
                    self.OPEN_CONNECTION_API,
                    headers={
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "User-Agent": (
                            "JMeterSuiteRunner Python/"
                            f"{platform.python_version()}"
                        ),
                    },
                    data=json.dumps(request_body).encode("utf-8"),
                    timeout=_STREAM_OPEN_TIMEOUT_SECONDS,
                )
            except requests.RequestException as error:
                self.logger.warning(
                    "钉钉 Stream 建立连接失败: %s",
                    type(error).__name__,
                )
                return None
            if response.status_code >= 400:
                self.logger.error(
                    "钉钉 Stream 建立连接失败: HTTP %s",
                    response.status_code,
                )
                return None
            try:
                connection = response.json()
            except (TypeError, ValueError):
                self.logger.error("钉钉 Stream 建立连接返回了无效 JSON")
                return None
            if (
                not isinstance(connection, dict)
                or not isinstance(connection.get("endpoint"), str)
                or not connection["endpoint"]
                or not isinstance(connection.get("ticket"), str)
                or not connection["ticket"]
            ):
                self.logger.error("钉钉 Stream 建立连接响应缺少必要字段")
                return None
            return connection

        async def start(self) -> None:
            self.pre_start()
            self._jmeter_stop_event = asyncio.Event()
            try:
                while not self._jmeter_stop_event.is_set():
                    try:
                        loop = asyncio.get_running_loop()
                        connection = await loop.run_in_executor(
                            None,
                            self.open_connection,
                        )
                        if self._jmeter_stop_event.is_set():
                            break
                        if not connection:
                            await self._wait_for_stop(
                                _STREAM_RECONNECT_SECONDS
                            )
                            continue
                        uri = (
                            f'{connection["endpoint"]}?ticket='
                            f'{quote_plus(connection["ticket"])}'
                        )
                        async with websockets.connect(uri) as websocket:
                            self.websocket = websocket
                            keepalive = asyncio.create_task(
                                self.keepalive(websocket)
                            )
                            try:
                                await self._receive_until_stopped(websocket)
                            finally:
                                keepalive.cancel()
                                await asyncio.gather(
                                    keepalive,
                                    return_exceptions=True,
                                )
                                if self.websocket is websocket:
                                    self.websocket = None
                    except asyncio.CancelledError:
                        raise
                    except websockets.exceptions.ConnectionClosed:
                        if not self._jmeter_stop_event.is_set():
                            self.logger.warning("钉钉 Stream 连接已断开，准备重连")
                            await self._wait_for_stop(
                                _STREAM_RECONNECT_SECONDS
                            )
                    except Exception:
                        if not self._jmeter_stop_event.is_set():
                            self.logger.exception(
                                "钉钉 Stream 连接异常，准备重连"
                            )
                            await self._wait_for_stop(
                                _STREAM_ERROR_RETRY_SECONDS
                            )
            finally:
                for task in tuple(self._jmeter_background_tasks):
                    task.cancel()
                if self._jmeter_background_tasks:
                    await asyncio.gather(
                        *self._jmeter_background_tasks,
                        return_exceptions=True,
                    )
                self._jmeter_background_tasks.clear()
                websocket = self.websocket
                self.websocket = None
                if websocket is not None:
                    await websocket.close()
                self._jmeter_stop_event = None

        async def stop(self) -> None:
            if self._jmeter_stop_event is not None:
                self._jmeter_stop_event.set()
            if self.websocket is not None:
                await self.websocket.close()

        async def _wait_for_stop(self, timeout: float) -> None:
            if self._jmeter_stop_event is None:
                return
            try:
                await asyncio.wait_for(
                    self._jmeter_stop_event.wait(),
                    timeout=timeout,
                )
            except TimeoutError:
                pass

        async def _receive_until_stopped(self, websocket: Any) -> None:
            assert self._jmeter_stop_event is not None
            while not self._jmeter_stop_event.is_set():
                receive_task = asyncio.create_task(websocket.recv())
                stop_task = asyncio.create_task(
                    self._jmeter_stop_event.wait()
                )
                done, pending = await asyncio.wait(
                    {receive_task, stop_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(
                        *pending,
                        return_exceptions=True,
                    )
                if stop_task in done and stop_task.result():
                    return
                raw_message = receive_task.result()
                try:
                    json_message = json.loads(raw_message)
                except (TypeError, json.JSONDecodeError):
                    self.logger.warning("钉钉 Stream 收到无效 JSON 消息")
                    continue
                task = asyncio.create_task(
                    self.background_task(json_message)
                )
                self._jmeter_background_tasks.add(task)
                task.add_done_callback(
                    self._jmeter_background_tasks.discard
                )


class DingTalkStreamRuntime:
    """在独立线程中托管 SDK 的 asyncio Stream 客户端。"""

    def __init__(self, client: Any, logger: logging.Logger):
        self._client = client
        self._logger = logger
        self._thread: Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_ready = Event()
        self._lifecycle_lock = Lock()

    @property
    def is_alive(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def start(self) -> None:
        with self._lifecycle_lock:
            if self.is_alive:
                return
            self._loop_ready.clear()
            self._thread = Thread(
                target=self._run,
                name="dingtalk-stream",
                daemon=True,
            )
            self._thread.start()
        if not self._loop_ready.wait(timeout=5):
            raise DingTalkStartupError("钉钉 Stream 线程启动超时")
        self._logger.info("钉钉 Stream 监听已启动")

    def stop(self) -> None:
        with self._lifecycle_lock:
            thread = self._thread
            loop = self._loop
        if thread is None:
            return
        if loop is not None and loop.is_running():
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._client.stop(),
                    loop,
                )
                future.result(timeout=_STREAM_STOP_TIMEOUT_SECONDS)
            except Exception:
                self._logger.exception("停止钉钉 Stream 连接失败")
        thread.join(timeout=_STREAM_STOP_TIMEOUT_SECONDS)
        if thread.is_alive():
            self._logger.error("钉钉 Stream 线程未在超时内停止")
        else:
            self._logger.info("钉钉 Stream 监听已停止")
        with self._lifecycle_lock:
            if self._thread is thread and not thread.is_alive():
                self._thread = None

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        with self._lifecycle_lock:
            self._loop = loop
        self._loop_ready.set()
        try:
            loop.run_until_complete(self._client.start())
        except Exception:
            self._logger.exception("钉钉 Stream 监听线程异常退出")
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            loop.close()
            with self._lifecycle_lock:
                self._loop = None


class DingTalkInspectionService:
    """协调群命令执行、绑定读取、报告通知和 Stream 生命周期。"""

    def __init__(
        self,
        *,
        config: AppConfig,
        cancel_event: Event,
        logger: logging.Logger,
        binding_store: DingTalkBindingStore,
        notifier: _Notifier,
        stream_factory: Callable[[Callable[[], bool]], _StreamLifecycle],
        suite_executor: LockedSuiteExecutor = (
            execute_suite_with_acquired_lock
        ),
    ):
        self._config = config
        self._cancel_event = cancel_event
        self._logger = logger
        self._binding_store = binding_store
        self._notifier = notifier
        self._suite_executor = suite_executor
        self._manual_gate = Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="dingtalk-inspection",
        )
        self._stream = stream_factory(self.submit_manual_run)
        self._stopped = False
        self._lifecycle_lock = Lock()

    def start(self) -> None:
        self._stream.start()

    def stop(self) -> None:
        with self._lifecycle_lock:
            if self._stopped:
                return
            self._stopped = True
        self._stream.stop()
        self._executor.shutdown(wait=True, cancel_futures=False)

    def submit_manual_run(self) -> bool:
        if self._cancel_event.is_set() or self._stopped:
            return False
        if not self._manual_gate.acquire(blocking=False):
            return False
        lock = FileLock(suite_lock_path(self._config))
        try:
            acquired = lock.acquire()
        except OSError:
            self._logger.exception("钉钉手动巡检无法初始化运行锁")
            self._manual_gate.release()
            return False
        if not acquired:
            self._manual_gate.release()
            return False
        try:
            self._executor.submit(self._execute_manual_run, lock)
        except Exception:
            lock.release()
            self._manual_gate.release()
            self._logger.exception("钉钉手动巡检任务提交失败")
            return False
        return True

    def notify_report(self, report: SuiteReportResult) -> None:
        try:
            binding = self._binding_store.load()
        except DingTalkBindingError:
            self._logger.exception("读取钉钉巡检群绑定失败")
            return
        if binding is None:
            self._logger.warning(
                "钉钉报告未发送：尚未绑定巡检群，run_id=%s",
                report.run_id,
            )
            return
        try:
            delivery = self._notifier.notify(report, binding)
            self._logger.info(
                "钉钉报告发送结束：run_id=%s group=%s result=%s",
                report.run_id,
                binding.open_conversation_id,
                delivery,
            )
        except Exception:
            self._logger.exception(
                "钉钉报告发送发生未捕获异常：run_id=%s",
                report.run_id,
            )

    def _execute_manual_run(self, lock: FileLock) -> None:
        try:
            report = self._suite_executor(
                self._config,
                lock,
                self._cancel_event,
            )
            self._logger.info(
                "钉钉手动巡检完成：status=%s run_directory=%s report=%s",
                report.status.value,
                report.run_directory,
                report.report_path,
            )
            self.notify_report(report)
        except Exception:
            self._logger.exception("钉钉手动巡检执行异常")
        finally:
            lock.release()
            self._manual_gate.release()


def notify_report_to_bound_group(
    config: AppConfig,
    report: SuiteReportResult,
    logger: logging.Logger | None = None,
) -> DingTalkDeliveryResult | None:
    """Send one existing report without starting a DingTalk Stream client."""
    notification_logger = logger or logging.getLogger(
        "jmeter_suite.dingtalk.manual"
    )
    binding_store = DingTalkBindingStore(
        config.runner.output_root
        / ".runtime"
        / "dingtalk-binding.json"
    )
    binding = binding_store.load()
    if binding is None:
        notification_logger.warning(
            "钉钉报告未发送：尚未绑定巡检群，run_id=%s",
            report.run_id,
        )
        return None

    api = DingTalkApiClient(
        client_id=config.dingtalk.client_id,
        client_secret=config.dingtalk.client_secret,
    )
    api.validate_credentials()
    delivery = DingTalkNotifier(
        api,
        logger=notification_logger,
    ).notify(report, binding)
    notification_logger.info(
        "钉钉手动巡检报告发送结束：run_id=%s group=%s result=%s",
        report.run_id,
        binding.open_conversation_id,
        delivery,
    )
    return delivery


def create_dingtalk_service(
    config: AppConfig,
    cancel_event: Event,
    logger: logging.Logger,
) -> DingTalkInspectionService:
    if not config.dingtalk.enabled:
        raise DingTalkStartupError("钉钉功能未启用")
    if dingtalk_stream is None or websockets is None:
        raise DingTalkStartupError("缺少 dingtalk-stream 运行依赖")

    api = DingTalkApiClient(
        client_id=config.dingtalk.client_id,
        client_secret=config.dingtalk.client_secret,
    )
    try:
        api.validate_credentials()
    except DingTalkCredentialError as error:
        raise DingTalkStartupError(str(error)) from error
    except DingTalkTransientError as error:
        logger.warning(
            "钉钉启动联网检查暂时失败，Stream 将继续重连: %s",
            error,
        )

    binding_store = DingTalkBindingStore(
        config.runner.output_root
        / ".runtime"
        / "dingtalk-binding.json"
    )
    notifier = DingTalkNotifier(api, logger=logger)

    def stream_factory(
        submit_run: Callable[[], bool],
    ) -> DingTalkStreamRuntime:
        controller = DingTalkCommandController(
            binding_store=binding_store,
            submit_run=submit_run,
            logger=logger,
        )
        handler = DingTalkChatbotHandler(controller, logger)
        credential = dingtalk_stream.Credential(
            config.dingtalk.client_id,
            config.dingtalk.client_secret,
        )
        client = _StoppableDingTalkStreamClient(
            credential,
            logger=logger,
        )
        client.register_callback_handler(
            dingtalk_stream.ChatbotMessage.TOPIC,
            handler,
        )
        return DingTalkStreamRuntime(client, logger)

    return DingTalkInspectionService(
        config=config,
        cancel_event=cancel_event,
        logger=logger,
        binding_store=binding_store,
        notifier=notifier,
        stream_factory=stream_factory,
    )
