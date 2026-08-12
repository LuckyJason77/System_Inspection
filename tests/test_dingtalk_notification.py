"""验证巡检结论、报告压缩及钉钉群文件推送。"""

from __future__ import annotations

from datetime import datetime, timezone
import io
import logging
from pathlib import Path
from zipfile import ZipFile

from jmeter_suite.dingtalk_binding import DingTalkBinding
from jmeter_suite.dingtalk_notification import DingTalkNotifier
from jmeter_suite.models import (
    JTLParseResult,
    ReportStatus,
    ScriptReportResult,
    SuiteReportResult,
)


class RecordingApi:
    def __init__(self):
        self.markdown_calls: list[tuple[str, str, str]] = []
        self.uploaded_archives: list[bytes] = []
        self.uploaded_names: list[str] = []
        self.file_calls: list[tuple[str, str, str, str]] = []

    def send_markdown(
        self,
        conversation_id: str,
        title: str,
        text: str,
    ) -> None:
        self.markdown_calls.append((conversation_id, title, text))

    def upload_file(self, path: Path) -> str:
        self.uploaded_names.append(path.name)
        self.uploaded_archives.append(path.read_bytes())
        return "@media-report"

    def send_file(
        self,
        conversation_id: str,
        media_id: str,
        file_name: str,
        file_type: str,
    ) -> None:
        self.file_calls.append(
            (conversation_id, media_id, file_name, file_type)
        )


def _report(tmp_path: Path) -> SuiteReportResult:
    run_directory = tmp_path / "runs" / "2026-08-12_08-00"
    report_directory = (
        run_directory / "2026-08-12_08-05-00_Inspection_Report"
    )
    report_directory.mkdir(parents=True)
    report_path = report_directory / "report.html"
    report_path.write_text("<html>完整巡检报告</html>", encoding="utf-8")
    artifact_directory = run_directory / "artifacts" / "01-script"
    artifact_directory.mkdir(parents=True)
    jtl_path = artifact_directory / "result.jtl"
    log_path = artifact_directory / "jmeter.log"
    jtl_path.write_text("<testResults />", encoding="utf-8")
    log_path.write_text("done", encoding="utf-8")
    started = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)
    finished = datetime(2026, 8, 12, 0, 4, 9, tzinfo=timezone.utc)
    parse_result = JTLParseResult(
        source_path=jtl_path,
        summaries=(),
        complete=True,
        error=None,
        total_samples=90,
        leaf_samples=81,
        passed_leaf_samples=66,
        failed_leaf_samples=15,
        assertion_failures=12,
    )
    script = ScriptReportResult(
        script_name="Inspection_Test",
        status=ReportStatus.FAILED,
        started_at=started,
        finished_at=finished,
        exit_code=0,
        error=None,
        jtl_path=jtl_path,
        log_path=log_path,
        report_path=report_path,
        parse_result=parse_result,
        sample_paths=(),
    )
    return SuiteReportResult(
        run_id=run_directory.name,
        status=ReportStatus.FAILED,
        started_at=started,
        finished_at=finished,
        run_directory=run_directory,
        report_path=report_path,
        manifest_path=run_directory / "manifest.json",
        scripts=(script,),
    )


def _binding() -> DingTalkBinding:
    return DingTalkBinding(
        open_conversation_id="cid-main",
        conversation_title="巡检群",
        bound_by_staff_id="staff-1",
        bound_at=datetime.fromisoformat("2026-08-12T07:00:00+08:00"),
    )


def test_notifier_sends_summary_and_zip_with_only_final_report(
    tmp_path: Path,
):
    """JTL, logs and manifest must never enter the DingTalk attachment."""
    api = RecordingApi()
    notifier = DingTalkNotifier(api)
    report = _report(tmp_path)
    unexpected_asset = report.report_path.parent / "assets" / "report.js"
    unexpected_asset.parent.mkdir()
    unexpected_asset.write_text("should not be uploaded", encoding="utf-8")

    result = notifier.notify(report, _binding())

    assert result.summary_sent is True
    assert result.attachment_sent is True
    assert result.error is None
    assert len(api.markdown_calls) == 1
    conversation_id, title, summary = api.markdown_calls[0]
    assert conversation_id == "cid-main"
    assert title == "自动化巡检"
    assert "**巡检结论：失败**" in summary
    assert "运行 ID：2026-08-12_08-00" in summary
    assert "请求总数：81" in summary
    assert "通过请求：66" in summary
    assert "失败请求：15" in summary
    assert "失败断言：12" in summary
    assert "耗时：4 分 9 秒" in summary
    assert api.uploaded_names == [
        "自动化巡检报告_2026-08-12_08-00.zip"
    ]
    with ZipFile(io.BytesIO(api.uploaded_archives[0])) as archive:
        assert archive.namelist() == ["report.html"]
        assert archive.read("report.html").decode("utf-8") == (
            "<html>完整巡检报告</html>"
        )
    assert api.file_calls == [
        (
            "cid-main",
            "@media-report",
            "自动化巡检报告_2026-08-12_08-00.zip",
            "zip",
        )
    ]


def test_notifier_reports_oversize_without_uploading(
    tmp_path: Path,
):
    """An oversized attachment must still leave a visible group conclusion."""
    api = RecordingApi()
    logger = logging.getLogger("test.dingtalk.oversize")
    stream = io.StringIO()
    logger.handlers.clear()
    logger.addHandler(logging.StreamHandler(stream))
    logger.setLevel(logging.INFO)
    notifier = DingTalkNotifier(api, logger=logger, max_upload_bytes=1)
    report = _report(tmp_path)

    result = notifier.notify(report, _binding())

    assert result.summary_sent is True
    assert result.attachment_sent is False
    assert result.archive_too_large is True
    assert api.uploaded_archives == []
    assert api.file_calls == []
    assert "报告 ZIP 超过钉钉 20MB 限制" in api.markdown_calls[0][2]
    assert str(report.report_path) in api.markdown_calls[0][2]
    assert "附件超过限制" in stream.getvalue()


def test_notifier_contains_delivery_failure_without_changing_report(
    tmp_path: Path,
):
    """A DingTalk outage must not turn a passed or failed suite into another status."""
    class FailingApi(RecordingApi):
        def upload_file(self, path: Path) -> str:
            raise RuntimeError("network down")

    api = FailingApi()
    logger = logging.getLogger("test.dingtalk.failure")
    stream = io.StringIO()
    logger.handlers.clear()
    logger.addHandler(logging.StreamHandler(stream))
    logger.setLevel(logging.INFO)
    notifier = DingTalkNotifier(api, logger=logger)
    report = _report(tmp_path)

    result = notifier.notify(report, _binding())

    assert report.status is ReportStatus.FAILED
    assert result.summary_sent is True
    assert result.attachment_sent is False
    assert "network down" in (result.error or "")
    assert "报告附件发送失败" in stream.getvalue()
