"""使用现有钉钉配置和绑定群验证消息及文件发送链路。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
import sys
import tempfile
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile
from zoneinfo import ZoneInfo

from .config import ConfigError, load_config
from .dingtalk_api import DingTalkApiClient
from .dingtalk_binding import (
    DingTalkBindingError,
    DingTalkBindingStore,
)
from .models import AppConfig


ConfigLoader = Callable[[Path], AppConfig]
ApiFactory = Callable[..., Any]
Clock = Callable[[], datetime]


class _DiagnosticStageError(RuntimeError):
    def __init__(self, stage: str, detail: str):
        self.stage = stage
        self.detail = detail
        super().__init__(f"{stage}失败：{detail}")


def _beijing_now() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))


def _safe_error(error: Exception, client_secret: str) -> str:
    detail = str(error).strip() or type(error).__name__
    if client_secret:
        detail = detail.replace(client_secret, "***")
    return detail


def _run_stage(
    number: int,
    label: str,
    operation: Callable[[], Any],
    client_secret: str,
) -> Any:
    try:
        result = operation()
    except Exception as error:
        raise _DiagnosticStageError(
            label,
            _safe_error(error, client_secret),
        ) from error
    print(f"{number}/4 {label}: 通过")
    return result


def run_dingtalk_diagnostic(
    config_path: Path,
    *,
    config_loader: ConfigLoader = load_config,
    api_factory: ApiFactory = DingTalkApiClient,
    now: Clock = _beijing_now,
) -> int:
    """Send one visible test message and ZIP to the currently bound group."""
    try:
        try:
            config = config_loader(Path(config_path))
        except ConfigError as error:
            print(f"配置错误：{error}", file=sys.stderr)
            return 2
        except Exception as error:
            print(f"配置读取失败：{error}", file=sys.stderr)
            return 2

        if not config.dingtalk.enabled:
            print(
                "配置错误：dingtalk.enabled 必须设置为 true",
                file=sys.stderr,
            )
            return 2
        if (
            not config.dingtalk.client_id
            or not config.dingtalk.client_secret
        ):
            print(
                "配置错误：dingtalk.client_id 和 "
                "dingtalk.client_secret 必须填写",
                file=sys.stderr,
            )
            return 2

        binding_store = DingTalkBindingStore(
            config.runner.output_root
            / ".runtime"
            / "dingtalk-binding.json"
        )
        try:
            binding = binding_store.load()
        except DingTalkBindingError as error:
            print(f"绑定信息错误：{error}", file=sys.stderr)
            return 2
        if binding is None:
            print(
                "钉钉测试无法开始：尚未绑定巡检群。请先启动 "
                "scheduler_service.py，并在目标群发送“@机器人 绑定巡检群”。",
                file=sys.stderr,
            )
            return 2

        target_name = (
            binding.conversation_title.strip()
            or binding.open_conversation_id
        )
        print("钉钉配置: 已启用")
        print(f"目标群: {target_name}")
        print("本次测试将发送一条 Markdown 消息和一个 ZIP 文件。")

        secret = config.dingtalk.client_secret

        def create_and_validate_api() -> Any:
            api_client = api_factory(
                client_id=config.dingtalk.client_id,
                client_secret=secret,
            )
            api_client.validate_credentials()
            return api_client

        api = _run_stage(
            1,
            "凭据校验",
            create_and_validate_api,
            secret,
        )

        test_time = now().astimezone(
            ZoneInfo("Asia/Shanghai")
        ).strftime("%Y-%m-%d %H:%M:%S")
        markdown = "\n".join(
            [
                "### 钉钉连通性测试",
                "",
                "**文本消息发送测试**",
                f"- 测试时间：{test_time}",
                "- 下一步：发送一个临时 ZIP 测试文件",
            ]
        )
        _run_stage(
            2,
            "文本消息发送",
            lambda: api.send_markdown(
                binding.open_conversation_id,
                "钉钉连通性测试",
                markdown,
            ),
            secret,
        )

        try:
            with tempfile.TemporaryDirectory(
                prefix="jmeter-dingtalk-diagnostic-"
            ) as temporary_directory:
                archive_path = (
                    Path(temporary_directory) / "钉钉连通性测试.zip"
                )
                with ZipFile(
                    archive_path,
                    mode="w",
                    compression=ZIP_DEFLATED,
                ) as archive:
                    archive.writestr(
                        "钉钉连通性测试.txt",
                        "\n".join(
                            [
                                "自动化巡检钉钉文件发送测试",
                                f"测试时间：{test_time}",
                                "如果能够打开此文件，说明上传和文件消息链路正常。",
                                "",
                            ]
                        ),
                    )
                media_id = _run_stage(
                    3,
                    "测试文件上传",
                    lambda: api.upload_file(archive_path),
                    secret,
                )
                _run_stage(
                    4,
                    "文件消息发送",
                    lambda: api.send_file(
                        binding.open_conversation_id,
                        media_id,
                        archive_path.name,
                        "zip",
                    ),
                    secret,
                )
        except _DiagnosticStageError:
            raise
        except Exception as error:
            raise _DiagnosticStageError(
                "测试文件准备",
                _safe_error(error, secret),
            ) from error

        print("钉钉连通性测试通过：消息和文件均已发送到目标群。")
        return 0
    except _DiagnosticStageError as error:
        print(f"钉钉测试失败：{error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("钉钉连通性测试已中断", file=sys.stderr)
        return 130
