"""验证钉钉手动执行槽、Stream 回调和服务关闭流程。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import io
import logging
from pathlib import Path
from types import SimpleNamespace
import threading
from zipfile import ZipFile

import pytest

import jmeter_suite.dingtalk_service as service_module
from jmeter_suite.dingtalk_api import (
    DingTalkCredentialError,
    DingTalkTransientError,
)

from jmeter_suite.dingtalk_binding import (
    DingTalkBinding,
    DingTalkBindingStore,
)
from jmeter_suite.dingtalk_service import (
    DingTalkChatbotHandler,
    DingTalkInspectionService,
    DingTalkStartupError,
    DingTalkStreamRuntime,
    create_dingtalk_service,
)
from jmeter_suite.locking import FileLock
from jmeter_suite.models import (
    AppConfig,
    DingTalkConfig,
    JMeterConfig,
    ReportStatus,
    RunnerConfig,
    ScheduleConfig,
    SuiteReportResult,
)


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        jmeter=JMeterConfig(
            executable=tmp_path / "jmeter.bat",
            properties_file=tmp_path / "jmeter.properties",
        ),
        schedule=ScheduleConfig(
            timezone="Asia/Shanghai",
            cron="0 8 * * mon-fri",
        ),
        runner=RunnerConfig(
            output_root=tmp_path / "runs",
            default_timeout_seconds=3600,
        ),
        scripts=(),
        dingtalk=DingTalkConfig(
            enabled=True,
            client_id="ding-client",
            client_secret="secret",
        ),
    )


def _report(config: AppConfig) -> SuiteReportResult:
    run_directory = config.runner.output_root / "2026-08-12_08-00"
    report_path = run_directory / "report" / "report.html"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("report", encoding="utf-8")
    now = datetime(2026, 8, 12, tzinfo=timezone.utc)
    return SuiteReportResult(
        run_id=run_directory.name,
        status=ReportStatus.PASSED,
        started_at=now,
        finished_at=now,
        run_directory=run_directory,
        report_path=report_path,
        manifest_path=run_directory / "manifest.json",
        scripts=(),
    )


class RecordingNotifier:
    def __init__(self):
        self.calls: list[tuple[SuiteReportResult, DingTalkBinding]] = []

    def notify(
        self,
        report: SuiteReportResult,
        binding: DingTalkBinding,
    ) -> object:
        self.calls.append((report, binding))
        return object()


class RecordingStream:
    def __init__(self):
        self.started = 0
        self.stopped = 0

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1


def _bound_store(config: AppConfig) -> DingTalkBindingStore:
    store = DingTalkBindingStore(
        config.runner.output_root / ".runtime" / "dingtalk-binding.json"
    )
    store.save(
        DingTalkBinding(
            open_conversation_id="cid-main",
            conversation_title="巡检群",
            bound_by_staff_id="staff-1",
            bound_at=datetime.fromisoformat("2026-08-12T07:00:00+08:00"),
        )
    )
    return store


def test_manual_submit_acquires_process_lock_before_accepting(
    tmp_path: Path,
):
    """An external run_once process must receive an immediate busy reply."""
    config = _config(tmp_path)
    notifier = RecordingNotifier()
    store = _bound_store(config)
    stream = RecordingStream()
    suite_calls = 0

    def suite_executor(actual_config, lock, cancel_event):
        nonlocal suite_calls
        suite_calls += 1
        lock.release()
        return _report(config)

    service = DingTalkInspectionService(
        config=config,
        cancel_event=threading.Event(),
        logger=logging.getLogger("test.dingtalk.external-lock"),
        binding_store=store,
        notifier=notifier,
        suite_executor=suite_executor,
        stream_factory=lambda submit_run: stream,
    )
    external_lock = FileLock(
        config.runner.output_root / ".runtime" / "suite.lock"
    )
    assert external_lock.acquire() is True

    try:
        accepted = service.submit_manual_run()
    finally:
        external_lock.release()
        service.stop()

    assert accepted is False
    assert suite_calls == 0
    assert notifier.calls == []


def test_manual_submit_executes_and_notifies_bound_group(
    tmp_path: Path,
):
    """An accepted command must generate once and publish after completion."""
    config = _config(tmp_path)
    notifier = RecordingNotifier()
    store = _bound_store(config)
    stream = RecordingStream()
    suite_calls = 0

    def suite_executor(actual_config, lock, cancel_event):
        nonlocal suite_calls
        suite_calls += 1
        assert actual_config is config
        assert cancel_event.is_set() is False
        assert lock.acquire() is True
        report = _report(config)
        lock.release()
        return report

    service = DingTalkInspectionService(
        config=config,
        cancel_event=threading.Event(),
        logger=logging.getLogger("test.dingtalk.manual"),
        binding_store=store,
        notifier=notifier,
        suite_executor=suite_executor,
        stream_factory=lambda submit_run: stream,
    )
    service.start()

    assert service.submit_manual_run() is True
    assert service.submit_manual_run() is False
    service.stop()

    assert stream.started == 1
    assert stream.stopped == 1
    assert suite_calls == 1
    assert len(notifier.calls) == 1
    assert notifier.calls[0][0].run_id == "2026-08-12_08-00"
    assert notifier.calls[0][1].open_conversation_id == "cid-main"


def test_notify_report_without_binding_only_logs(tmp_path: Path):
    """Scheduled execution must still succeed before a group is bound."""
    config = _config(tmp_path)
    notifier = RecordingNotifier()
    stream = io.StringIO()
    logger = logging.getLogger("test.dingtalk.unbound")
    logger.handlers.clear()
    logger.addHandler(logging.StreamHandler(stream))
    logger.setLevel(logging.INFO)
    service = DingTalkInspectionService(
        config=config,
        cancel_event=threading.Event(),
        logger=logger,
        binding_store=DingTalkBindingStore(
            config.runner.output_root
            / ".runtime"
            / "dingtalk-binding.json"
        ),
        notifier=notifier,
        suite_executor=lambda *args: _report(config),
        stream_factory=lambda submit_run: RecordingStream(),
    )

    service.notify_report(_report(config))
    service.stop()

    assert notifier.calls == []
    assert "尚未绑定巡检群" in stream.getvalue()


def test_manual_report_publisher_uses_saved_binding_without_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """run_once must reuse the bound group and send the generated HTML ZIP."""
    config = _config(tmp_path)
    _bound_store(config)
    report = _report(config)
    observed: dict[str, object] = {"markdown": [], "files": []}

    class RecordingApi:
        def __init__(self, *, client_id: str, client_secret: str):
            observed["credentials"] = (client_id, client_secret)

        def validate_credentials(self) -> None:
            observed["validated"] = True

        def send_markdown(self, conversation_id, title, text) -> None:
            observed["markdown"].append(
                (conversation_id, title, text)
            )

        def upload_file(self, path: Path) -> str:
            with ZipFile(path) as archive:
                observed["archive_names"] = archive.namelist()
            return "@media-manual"

        def send_file(
            self,
            conversation_id,
            media_id,
            file_name,
            file_type,
        ) -> None:
            observed["files"].append(
                (
                    conversation_id,
                    media_id,
                    file_name,
                    file_type,
                )
            )

    monkeypatch.setattr(service_module, "DingTalkApiClient", RecordingApi)

    result = service_module.notify_report_to_bound_group(config, report)

    assert result.summary_sent is True
    assert result.attachment_sent is True
    assert observed["credentials"] == ("ding-client", "secret")
    assert observed["validated"] is True
    assert observed["archive_names"] == ["report.html"]
    assert observed["markdown"][0][0] == "cid-main"
    assert observed["files"][0][0] == "cid-main"


def test_manual_report_publisher_skips_network_without_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """A fresh project cannot send until a group has been explicitly bound."""
    config = _config(tmp_path)

    def forbidden_api(**kwargs):
        pytest.fail("no binding must not request a DingTalk token")

    monkeypatch.setattr(service_module, "DingTalkApiClient", forbidden_api)

    with caplog.at_level(logging.WARNING):
        result = service_module.notify_report_to_bound_group(
            config,
            _report(config),
        )

    assert result is None
    assert "尚未绑定巡检群" in caplog.text


def test_chatbot_handler_maps_sdk_message_and_replies():
    """SDK message fields must reach command filtering without distortion."""
    observed: dict[str, object] = {}

    class Controller:
        def handle(self, message, reply):
            observed["message"] = message
            reply("已接收")

    handler = DingTalkChatbotHandler(
        Controller(),
        logging.getLogger("test.dingtalk.handler"),
    )
    replies: list[tuple[str, object]] = []
    handler.reply_text = lambda text, incoming: replies.append(
        (text, incoming)
    )
    callback = SimpleNamespace(
        data={
            "msgId": "msg-1",
            "conversationId": "cid-main",
            "conversationTitle": "巡检群",
            "senderStaffId": "staff-1",
            "senderNick": "普通成员",
            "isAdmin": False,
            "isInAtList": True,
            "conversationType": "2",
            "msgtype": "text",
            "text": {"content": " 执行巡检 "},
        }
    )

    status, message = asyncio.run(handler.process(callback))

    incoming = observed["message"]
    assert incoming.message_id == "msg-1"
    assert incoming.conversation_id == "cid-main"
    assert incoming.sender_staff_id == "staff-1"
    assert incoming.is_admin is False
    assert incoming.is_in_at_list is True
    assert incoming.text == " 执行巡检 "
    assert replies[0][0] == "已接收"
    assert status == 200
    assert message == "OK"


def test_stream_runtime_starts_and_stops_its_async_client():
    """Ctrl+C cleanup must terminate the long connection thread."""
    client_started = threading.Event()
    client_stopped = threading.Event()

    class AsyncClient:
        def __init__(self):
            self._stop: asyncio.Event | None = None

        async def start(self):
            self._stop = asyncio.Event()
            client_started.set()
            await self._stop.wait()

        async def stop(self):
            assert self._stop is not None
            self._stop.set()
            client_stopped.set()

    runtime = DingTalkStreamRuntime(
        AsyncClient(),
        logging.getLogger("test.dingtalk.stream-runtime"),
    )

    runtime.start()
    assert client_started.wait(timeout=2)
    runtime.stop()

    assert client_stopped.is_set()
    assert runtime.is_alive is False


def test_stream_connection_open_uses_bounded_http_timeout(
    monkeypatch: pytest.MonkeyPatch,
):
    """The SDK's unbounded default request must not delay Ctrl+C forever."""
    observed: dict[str, object] = {}

    class Response:
        text = ""
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {
                "endpoint": "wss://example.invalid/stream",
                "ticket": "ticket-1",
            }

    request_module = (
        service_module.dingtalk_stream.DingTalkStreamClient
        .open_connection.__globals__["requests"]
    )

    def fake_post(url: str, **kwargs):
        observed["url"] = url
        observed.update(kwargs)
        return Response()

    monkeypatch.setattr(request_module, "post", fake_post)
    client = service_module._StoppableDingTalkStreamClient(
        service_module.dingtalk_stream.Credential(
            "ding-client",
            "secret",
        )
    )
    client.register_callback_handler("/test/topic", SimpleNamespace())

    connection = client.open_connection()

    assert connection["ticket"] == "ticket-1"
    assert observed["timeout"] == 10.0


def test_service_factory_rejects_invalid_credentials_without_secret(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A confirmed credential error is a startup error and never echoes Secret."""

    class InvalidApi:
        def __init__(self, *, client_id: str, client_secret: str):
            assert client_id == "ding-client"
            assert client_secret == "secret"

        def validate_credentials(self) -> None:
            raise DingTalkCredentialError("应用凭据无效")

    monkeypatch.setattr(service_module, "DingTalkApiClient", InvalidApi)

    with pytest.raises(DingTalkStartupError) as captured:
        create_dingtalk_service(
            _config(tmp_path),
            threading.Event(),
            logging.getLogger("test.dingtalk.invalid-credential"),
        )

    assert "应用凭据无效" in str(captured.value)
    assert "secret" not in str(captured.value)


def test_service_factory_continues_after_transient_network_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """A temporary outage must not prevent the scheduler from starting."""

    class TransientApi:
        def __init__(self, *, client_id: str, client_secret: str):
            pass

        def validate_credentials(self) -> None:
            raise DingTalkTransientError("temporary outage")

    sentinel = object()

    def fake_service(**kwargs):
        assert isinstance(kwargs["binding_store"], DingTalkBindingStore)
        return sentinel

    monkeypatch.setattr(service_module, "DingTalkApiClient", TransientApi)
    monkeypatch.setattr(service_module, "DingTalkInspectionService", fake_service)

    with caplog.at_level(logging.WARNING):
        result = create_dingtalk_service(
            _config(tmp_path),
            threading.Event(),
            logging.getLogger("test.dingtalk.transient"),
        )

    assert result is sentinel
    assert "Stream 将继续重连" in caplog.text
    assert "secret" not in caplog.text


def test_service_factory_reports_missing_stream_sdk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setattr(service_module, "dingtalk_stream", None)

    with pytest.raises(DingTalkStartupError, match="缺少 dingtalk-stream"):
        create_dingtalk_service(
            _config(tmp_path),
            threading.Event(),
            logging.getLogger("test.dingtalk.missing-sdk"),
        )
