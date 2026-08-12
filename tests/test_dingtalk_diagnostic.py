"""验证独立钉钉诊断脚本的消息、文件和错误定位能力。"""

from __future__ import annotations

from datetime import datetime
import importlib
from pathlib import Path
from zipfile import ZipFile

import pytest

from jmeter_suite.dingtalk_binding import (
    DingTalkBinding,
    DingTalkBindingStore,
)
from jmeter_suite.models import (
    AppConfig,
    DingTalkConfig,
    JMeterConfig,
    RunnerConfig,
    ScheduleConfig,
)


def _diagnostic_module():
    return importlib.import_module("jmeter_suite.dingtalk_diagnostic")


def _config(tmp_path: Path, *, enabled: bool = True) -> AppConfig:
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
            enabled=enabled,
            client_id="ding-client" if enabled else "",
            client_secret="super-secret" if enabled else "",
        ),
    )


def _bind(config: AppConfig) -> None:
    DingTalkBindingStore(
        config.runner.output_root / ".runtime" / "dingtalk-binding.json"
    ).save(
        DingTalkBinding(
            open_conversation_id="cid-main",
            conversation_title="自动化巡检群",
            bound_by_staff_id="staff-admin",
            bound_at=datetime.fromisoformat("2026-08-12T08:00:00+08:00"),
        )
    )


class RecordingApi:
    def __init__(self, *, fail_stage: str | None = None):
        self.fail_stage = fail_stage
        self.markdown_calls: list[tuple[str, str, str]] = []
        self.file_calls: list[tuple[str, str, str, str]] = []
        self.archive_names: list[str] = []
        self.archive_text = ""
        self.upload_path: Path | None = None

    def _fail(self, stage: str) -> None:
        if self.fail_stage == stage:
            raise RuntimeError(
                f"{stage} rejected, leaked=super-secret"
            )

    def validate_credentials(self) -> None:
        self._fail("credential")

    def send_markdown(
        self,
        conversation_id: str,
        title: str,
        text: str,
    ) -> None:
        self._fail("message")
        self.markdown_calls.append((conversation_id, title, text))

    def upload_file(self, path: Path) -> str:
        self._fail("upload")
        self.upload_path = path
        with ZipFile(path) as archive:
            self.archive_names = archive.namelist()
            self.archive_text = archive.read(
                "钉钉连通性测试.txt"
            ).decode("utf-8")
        return "@media-diagnostic"

    def send_file(
        self,
        conversation_id: str,
        media_id: str,
        file_name: str,
        file_type: str,
    ) -> None:
        self._fail("file")
        self.file_calls.append(
            (conversation_id, media_id, file_name, file_type)
        )


def test_diagnostic_sends_visible_message_and_temporary_zip(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    """Skipping either outbound path would leave report delivery unverified."""
    module = _diagnostic_module()
    config = _config(tmp_path)
    _bind(config)
    api = RecordingApi()

    exit_code = module.run_dingtalk_diagnostic(
        tmp_path / "config" / "app.toml",
        config_loader=lambda path: config,
        api_factory=lambda **kwargs: api,
        now=lambda: datetime.fromisoformat("2026-08-12T09:30:45+08:00"),
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == ""
    assert "目标群: 自动化巡检群" in captured.out
    assert "1/4 凭据校验: 通过" in captured.out
    assert "2/4 文本消息发送: 通过" in captured.out
    assert "3/4 测试文件上传: 通过" in captured.out
    assert "4/4 文件消息发送: 通过" in captured.out
    assert "钉钉连通性测试通过" in captured.out
    assert "super-secret" not in captured.out
    assert api.markdown_calls[0][0] == "cid-main"
    assert "2026-08-12 09:30:45" in api.markdown_calls[0][2]
    assert api.archive_names == ["钉钉连通性测试.txt"]
    assert "2026-08-12 09:30:45" in api.archive_text
    assert api.file_calls == [
        (
            "cid-main",
            "@media-diagnostic",
            "钉钉连通性测试.zip",
            "zip",
        )
    ]
    assert api.upload_path is not None
    assert api.upload_path.exists() is False


def test_diagnostic_rejects_disabled_dingtalk_without_network(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    module = _diagnostic_module()

    exit_code = module.run_dingtalk_diagnostic(
        tmp_path / "config" / "app.toml",
        config_loader=lambda path: _config(tmp_path, enabled=False),
        api_factory=lambda **kwargs: pytest.fail(
            "disabled config must not access DingTalk"
        ),
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out == ""
    assert "dingtalk.enabled 必须设置为 true" in captured.err


def test_diagnostic_explains_how_to_create_missing_binding(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    module = _diagnostic_module()
    config = _config(tmp_path)

    exit_code = module.run_dingtalk_diagnostic(
        tmp_path / "config" / "app.toml",
        config_loader=lambda path: config,
        api_factory=lambda **kwargs: pytest.fail(
            "missing binding must not access DingTalk"
        ),
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "尚未绑定巡检群" in captured.err
    assert "@机器人 绑定巡检群" in captured.err


@pytest.mark.parametrize(
    ("fail_stage", "expected_stage"),
    [
        ("credential", "凭据校验失败"),
        ("message", "文本消息发送失败"),
        ("upload", "测试文件上传失败"),
        ("file", "文件消息发送失败"),
    ],
)
def test_diagnostic_identifies_failed_stage_and_redacts_secret(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    fail_stage: str,
    expected_stage: str,
):
    module = _diagnostic_module()
    config = _config(tmp_path)
    _bind(config)
    api = RecordingApi(fail_stage=fail_stage)

    exit_code = module.run_dingtalk_diagnostic(
        tmp_path / "config" / "app.toml",
        config_loader=lambda path: config,
        api_factory=lambda **kwargs: api,
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert expected_stage in captured.err
    assert "super-secret" not in captured.err
    assert "***" in captured.err


def test_diagnostic_explains_api_initialization_failure_without_secret(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    """An SDK/client construction failure must not escape as a traceback."""
    module = _diagnostic_module()
    config = _config(tmp_path)
    _bind(config)

    def failing_api(**kwargs):
        raise RuntimeError("SDK initialization failed: super-secret")

    exit_code = module.run_dingtalk_diagnostic(
        tmp_path / "config" / "app.toml",
        config_loader=lambda path: config,
        api_factory=failing_api,
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "凭据校验失败" in captured.err
    assert "SDK initialization failed" in captured.err
    assert "super-secret" not in captured.err
    assert "***" in captured.err


def test_diagnostic_explains_local_test_file_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    """A disk/temp error must be reported instead of producing a traceback."""
    module = _diagnostic_module()
    config = _config(tmp_path)
    _bind(config)
    api = RecordingApi()

    def failing_temporary_directory(*args, **kwargs):
        raise OSError("temporary disk full: super-secret")

    monkeypatch.setattr(
        module.tempfile,
        "TemporaryDirectory",
        failing_temporary_directory,
    )

    exit_code = module.run_dingtalk_diagnostic(
        tmp_path / "config" / "app.toml",
        config_loader=lambda path: config,
        api_factory=lambda **kwargs: api,
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "测试文件准备失败" in captured.err
    assert "temporary disk full" in captured.err
    assert "super-secret" not in captured.err


def test_diagnostic_maps_keyboard_interrupt_to_130(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    module = _diagnostic_module()
    config = _config(tmp_path)
    _bind(config)

    class InterruptedApi(RecordingApi):
        def validate_credentials(self) -> None:
            raise KeyboardInterrupt

    exit_code = module.run_dingtalk_diagnostic(
        tmp_path / "config" / "app.toml",
        config_loader=lambda path: config,
        api_factory=lambda **kwargs: InterruptedApi(),
    )

    captured = capsys.readouterr()
    assert exit_code == 130
    assert "钉钉连通性测试已中断" in captured.err
