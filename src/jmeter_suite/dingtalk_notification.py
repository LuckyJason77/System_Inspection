"""将单页巡检报告压缩并向已绑定钉钉群发送结论和附件。"""

from __future__ import annotations

from dataclasses import dataclass
import logging
from pathlib import Path
import tempfile
from typing import Protocol
from zipfile import ZIP_DEFLATED, ZipFile
from zoneinfo import ZoneInfo

from .dingtalk_binding import DingTalkBinding
from .models import SuiteReportResult
from .report import status_label


MAX_DINGTALK_FILE_BYTES = 20 * 1024 * 1024


class DingTalkNotificationApi(Protocol):
    def send_markdown(
        self,
        conversation_id: str,
        title: str,
        text: str,
    ) -> None: ...

    def upload_file(self, path: Path) -> str: ...

    def send_file(
        self,
        conversation_id: str,
        media_id: str,
        file_name: str,
        file_type: str,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class DingTalkDeliveryResult:
    summary_sent: bool
    attachment_sent: bool
    archive_too_large: bool = False
    error: str | None = None


class DingTalkNotifier:
    def __init__(
        self,
        api: DingTalkNotificationApi,
        *,
        logger: logging.Logger | None = None,
        max_upload_bytes: int = MAX_DINGTALK_FILE_BYTES,
    ):
        self._api = api
        self._logger = logger or logging.getLogger(__name__)
        self._max_upload_bytes = max_upload_bytes

    def notify(
        self,
        report: SuiteReportResult,
        binding: DingTalkBinding,
    ) -> DingTalkDeliveryResult:
        errors: list[str] = []
        summary_sent = False
        attachment_sent = False
        archive_too_large = False

        try:
            with tempfile.TemporaryDirectory(
                prefix="jmeter-dingtalk-report-"
            ) as temporary_directory:
                archive_path = (
                    Path(temporary_directory)
                    / f"自动化巡检报告_{report.run_id}.zip"
                )
                _create_report_archive(report.report_path, archive_path)
                archive_too_large = (
                    archive_path.stat().st_size > self._max_upload_bytes
                )
                summary = _build_summary(
                    report,
                    archive_too_large=archive_too_large,
                )
                try:
                    self._api.send_markdown(
                        binding.open_conversation_id,
                        "自动化巡检",
                        summary,
                    )
                    summary_sent = True
                except Exception as error:
                    errors.append(f"结论发送失败: {error}")
                    self._logger.exception("钉钉巡检结论发送失败")

                if archive_too_large:
                    self._logger.warning(
                        "钉钉报告附件超过限制：size=%s limit=%s report=%s",
                        archive_path.stat().st_size,
                        self._max_upload_bytes,
                        report.report_path,
                    )
                else:
                    try:
                        media_id = self._api.upload_file(archive_path)
                        self._api.send_file(
                            binding.open_conversation_id,
                            media_id,
                            archive_path.name,
                            "zip",
                        )
                        attachment_sent = True
                    except Exception as error:
                        errors.append(f"报告附件发送失败: {error}")
                        self._logger.exception("钉钉报告附件发送失败")
        except Exception as error:
            errors.append(f"报告打包失败: {error}")
            self._logger.exception("钉钉报告打包失败")

        return DingTalkDeliveryResult(
            summary_sent=summary_sent,
            attachment_sent=attachment_sent,
            archive_too_large=archive_too_large,
            error="；".join(errors) or None,
        )


def _create_report_archive(report_path: Path, archive_path: Path) -> None:
    if not report_path.is_file():
        raise FileNotFoundError(f"巡检报告不存在: {report_path}")
    with ZipFile(archive_path, mode="w", compression=ZIP_DEFLATED) as archive:
        archive.write(report_path, arcname="report.html")


def _build_summary(
    report: SuiteReportResult,
    *,
    archive_too_large: bool,
) -> str:
    total_requests = sum(script.leaf_samples for script in report.scripts)
    passed_requests = sum(script.passed_samples for script in report.scripts)
    failed_requests = sum(script.failed_samples for script in report.scripts)
    failed_assertions = sum(
        script.assertion_failures for script in report.scripts
    )
    started_at = report.started_at.astimezone(
        ZoneInfo("Asia/Shanghai")
    ).strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"### 自动化巡检 · {status_label(report.status)}",
        "",
        f"**巡检结论：{status_label(report.status)}**",
        f"- 运行 ID：{report.run_id}",
        f"- 请求时间：{started_at}",
        f"- 耗时：{_format_duration(report.duration_seconds)}",
        f"- 请求总数：{total_requests}",
        f"- 通过请求：{passed_requests}",
        f"- 失败请求：{failed_requests}",
        f"- 失败断言：{failed_assertions}",
    ]
    if archive_too_large:
        lines.extend(
            [
                "",
                "报告 ZIP 超过钉钉 20MB 限制，未上传附件。",
                f"本地报告：{report.report_path}",
            ]
        )
    return "\n".join(lines)


def _format_duration(seconds: float) -> str:
    total_seconds = max(0, int(round(seconds)))
    minutes, remaining_seconds = divmod(total_seconds, 60)
    if minutes:
        return f"{minutes} 分 {remaining_seconds} 秒"
    return f"{remaining_seconds} 秒"
