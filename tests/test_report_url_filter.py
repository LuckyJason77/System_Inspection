"""验证 URL 关键字过滤对报告内容、统计和最终状态的影响。"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import gzip
import json
from pathlib import Path
import re


STARTED_AT = datetime(2026, 8, 4, 0, 0, tzinfo=timezone.utc)
FINISHED_AT = datetime(2026, 8, 4, 0, 1, tzinfo=timezone.utc)


def _decode_compressed_payloads(report_html: str) -> list[dict[str, str]]:
    payloads = re.findall(
        r"<template[^>]+data-compressed-payload[^>]*>([^<]+)</template>",
        report_html,
    )
    return [
        json.loads(
            gzip.decompress(base64.b64decode(payload.strip())).decode("utf-8")
        )
        for payload in payloads
    ]


def _sample(
    label: str,
    url: str,
    *,
    success: bool,
    assertion: str = "",
    response: str = "response",
) -> str:
    return f"""\
  <httpSample lb="{label}" s="{str(success).lower()}" rc="{'200' if success else '500'}" ts="1785801600000" t="25">
    <java.net.URL>{url.replace('&', '&amp;')}</java.net.URL>
    <queryString>date_start=2026-06-01&amp;date_end=2026-06-30</queryString>
    <responseData>{response}</responseData>
    {assertion}
  </httpSample>
"""


def _suite(tmp_path: Path, jtl: str):
    from jmeter_suite.models import (
        ExecutionStatus,
        JMeterExecutionResult,
        SuiteProcessResult,
    )

    run_directory = tmp_path / "filtered-report"
    artifact_directory = run_directory / "artifacts" / "01-inspection"
    artifact_directory.mkdir(parents=True)
    jtl_path = artifact_directory / "result.jtl"
    jtl_path.write_text(jtl, encoding="utf-8")
    log_path = artifact_directory / "jmeter.log"
    log_path.write_text("done", encoding="utf-8")
    execution = JMeterExecutionResult(
        script_name="巡检",
        status=ExecutionStatus.COMPLETED,
        started_at=STARTED_AT,
        finished_at=FINISHED_AT,
        exit_code=0,
        error=None,
        jtl_path=jtl_path,
        log_path=log_path,
        stdout="",
        stderr="",
    )
    return SuiteProcessResult(
        run_id="2026-08-04_08-00",
        run_directory=run_directory,
        started_at=STARTED_AT,
        finished_at=FINISHED_AT,
        executions=(execution,),
    )


def test_url_filter_is_case_insensitive_and_ignores_query_and_fragment():
    from jmeter_suite.report_filter import is_url_excluded

    keywords = ("/login/login/singlelogin",)

    assert is_url_excluded(
        "HTTPS://EXAMPLE.TEST/Login/Login/SingleLogin?token=secret#result",
        keywords,
    )
    assert not is_url_excluded(
        "https://example.test/orders?next=/login/login/singlelogin",
        keywords,
    )
    assert not is_url_excluded("", keywords)


def test_filtered_failure_is_removed_from_html_stats_assertions_and_manifest(
    tmp_path: Path,
):
    from jmeter_suite.models import ReportStatus
    from jmeter_suite.report import generate_suite_report

    failed_assertion = """\
<assertionResult>
  <name>不应计入的断言</name>
  <failure>true</failure>
  <error>false</error>
  <failureMessage>filtered assertion failure</failureMessage>
</assertionResult>
"""
    jtl = (
        "<testResults>\n"
        + _sample(
            "应保留接口",
            "https://example.test/orders?month=2026-06",
            success=True,
            response="kept-response",
        )
        + _sample(
            "应过滤登录接口",
            "HTTPS://EXAMPLE.TEST/Login/Login/SingleLogin?token=secret#done",
            success=False,
            assertion=failed_assertion,
            response="filtered-secret-response",
        )
        + "</testResults>\n"
    )

    result = generate_suite_report(
        _suite(tmp_path, jtl),
        excluded_url_keywords=("/login/login/singlelogin",),
    )

    assert result.status is ReportStatus.PASSED
    assert result.scripts[0].status is ReportStatus.PASSED
    assert result.scripts[0].error is None
    parsed = result.scripts[0].parse_result
    assert parsed.total_samples == 1
    assert parsed.leaf_samples == 1
    assert parsed.passed_leaf_samples == 1
    assert parsed.failed_leaf_samples == 0
    assert parsed.assertion_failures == 0
    report = result.report_path.read_text(encoding="utf-8")
    assert "应保留接口" in report
    assert _decode_compressed_payloads(report) == [
        {
            "request": "date_start=2026-06-01&date_end=2026-06-30",
            "response": "kept-response",
        }
    ]
    assert "kept-response" not in report
    assert "应过滤登录接口" not in report
    assert "filtered-secret-response" not in report
    assert "不应计入的断言" not in report
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "passed"
    assert manifest["totals"]["leaf_samples"] == 1
    assert manifest["totals"]["failed_samples"] == 0
    assert manifest["scripts"][0]["total_samples"] == 1
    assert manifest["scripts"][0]["assertion_failures"] == 0


def test_all_http_interfaces_filtered_marks_script_error_with_clear_reason(
    tmp_path: Path,
):
    from jmeter_suite.models import ReportStatus
    from jmeter_suite.report import generate_suite_report

    jtl = (
        "<testResults>\n"
        + _sample(
            "只存在健康检查",
            "https://example.test/health?verbose=true",
            success=True,
        )
        + "</testResults>\n"
    )

    result = generate_suite_report(
        _suite(tmp_path, jtl),
        excluded_url_keywords=("/health",),
    )

    script = result.scripts[0]
    assert script.status is ReportStatus.ERROR
    assert script.error == "URL 关键字过滤后没有可报告的 HTTP 接口"
    assert script.parse_result.complete is True
    assert script.parse_result.total_samples == 0
    assert script.parse_result.leaf_samples == 0
    report = result.report_path.read_text(encoding="utf-8")
    assert "只存在健康检查" not in report
    assert "URL 关键字过滤后没有可报告的 HTTP 接口" in report
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["scripts"][0]["status"] == "error"
    assert manifest["scripts"][0]["error"] == script.error
    assert manifest["totals"]["leaf_samples"] == 0


def test_empty_filter_keeps_existing_report_behavior(tmp_path: Path):
    from jmeter_suite.models import ReportStatus
    from jmeter_suite.report import generate_suite_report

    jtl = (
        "<testResults>\n"
        + _sample(
            "健康检查仍保留",
            "https://example.test/health",
            success=False,
            response="visible-response",
        )
        + "</testResults>\n"
    )

    result = generate_suite_report(_suite(tmp_path, jtl))

    assert result.status is ReportStatus.FAILED
    assert result.scripts[0].parse_result.leaf_samples == 1
    report = result.report_path.read_text(encoding="utf-8")
    assert "健康检查仍保留" in report
    assert "visible-response" in report
