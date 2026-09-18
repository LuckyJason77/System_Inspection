"""验证单页报告生成、目录命名、CSP、清单和异常样本展示。"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import gzip
import html
from importlib import resources
import json
from pathlib import Path
import re
import tomllib

import pytest

from jmeter_suite.models import (
    ExecutionStatus,
    JMeterExecutionResult,
    SuiteProcessResult,
)


STARTED_AT = datetime(2026, 7, 31, 1, 2, 3, tzinfo=timezone.utc)
FINISHED_AT = datetime(2026, 7, 31, 1, 2, 8, tzinfo=timezone.utc)
REPORT_GENERATED_AT = datetime(
    2026,
    8,
    4,
    7,
    0,
    1,
    tzinfo=timezone(timedelta(hours=8)),
)


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


def _execution(
    run_directory: Path,
    *,
    artifact_name: str = "01-登录脚本",
    script_name: str = "登录脚本",
    status: ExecutionStatus = ExecutionStatus.COMPLETED,
    exit_code: int | None = 0,
    error: str | None = None,
    jtl: str | None = None,
    process_tree_termination_confirmed: bool | None = None,
) -> JMeterExecutionResult:
    artifact_directory = run_directory / "artifacts" / artifact_name
    artifact_directory.mkdir(parents=True, exist_ok=True)
    jtl_path = artifact_directory / "result.jtl"
    if jtl is not None:
        jtl_path.write_text(jtl, encoding="utf-8")
    log_path = artifact_directory / "jmeter.log"
    log_path.write_text("JMeter 日志", encoding="utf-8")
    return JMeterExecutionResult(
        script_name=script_name,
        status=status,
        started_at=STARTED_AT,
        finished_at=FINISHED_AT,
        exit_code=exit_code,
        error=error,
        jtl_path=jtl_path,
        log_path=log_path,
        stdout="不应进入报告的标准输出",
        stderr="不应进入报告的标准错误",
        process_tree_termination_confirmed=(
            process_tree_termination_confirmed
        ),
    )


def _suite(
    run_directory: Path,
    *executions: JMeterExecutionResult,
) -> SuiteProcessResult:
    return SuiteProcessResult(
        run_id="20260731-090203-deadbeef",
        run_directory=run_directory,
        started_at=STARTED_AT,
        finished_at=FINISHED_AT,
        executions=tuple(executions),
    )


def _passing_jtl(
    *,
    label: str = "登录接口",
    response_data: str = "第一行\n完整响应\n最后一行",
) -> str:
    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<testResults version="1.2">
  <httpSample lb="{label}" s="true" ts="1722398765432" t="345"
              rc="200" rm="OK" tn="线程组 1-1" dt="text" de="UTF-8">
    <java.net.URL>https://example.test/login?x=1&amp;y=2</java.net.URL>
    <queryString>username=张三&amp;token=secret-query</queryString>
    <requestHeader>Authorization: Bearer secret-header</requestHeader>
    <samplerData>POST /login secret-request-body</samplerData>
    <responseHeader>HTTP/1.1 200 OK</responseHeader>
    <responseData>{response_data}</responseData>
    <assertionResult>
      <name>状态码断言</name>
      <failure>false</failure>
      <error>false</error>
      <failureMessage></failureMessage>
    </assertionResult>
  </httpSample>
</testResults>
"""


def test_generate_passed_report_writes_single_offline_page_and_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from jmeter_suite.models import ReportStatus, SuiteReportResult
    import jmeter_suite.report as report_module

    monkeypatch.setattr(
        report_module,
        "_report_generated_at",
        lambda: REPORT_GENERATED_AT,
        raising=False,
    )

    run_directory = tmp_path / "run"
    execution = _execution(run_directory, jtl=_passing_jtl())

    result = report_module.generate_suite_report(
        _suite(run_directory, execution)
    )
    report_directory = (
        run_directory / "2026-08-04_07-00-01_Inspection_Report"
    )
    expected_report_path = report_directory / "report.html"

    assert isinstance(result, SuiteReportResult)
    assert result.run_id == "20260731-090203-deadbeef"
    assert result.status is ReportStatus.PASSED
    assert result.run_directory == run_directory
    assert result.report_path == expected_report_path
    assert result.manifest_path == run_directory / "manifest.json"
    assert len(result.scripts) == 1

    script_result = result.scripts[0]
    assert script_result.script_name == "登录脚本"
    assert script_result.status is ReportStatus.PASSED
    assert script_result.duration_seconds == 5.0
    assert script_result.parse_result.complete is True
    assert script_result.parse_result.leaf_samples == 1
    assert script_result.report_path == expected_report_path
    assert script_result.sample_paths == ()

    expected_files = {
        "manifest.json",
        "2026-08-04_07-00-01_Inspection_Report/report.html",
        "artifacts/01-登录脚本/jmeter.log",
        "artifacts/01-登录脚本/result.jtl",
    }
    actual_files = {
        path.relative_to(run_directory).as_posix()
        for path in run_directory.rglob("*")
        if path.is_file()
    }
    assert actual_files == expected_files

    assert {path.name for path in report_directory.iterdir()} == {
        "report.html"
    }
    suite_html = expected_report_path.read_text(encoding="utf-8")
    inline_assets = report_module._load_inline_assets()
    report_css = str(inline_assets.css)
    report_js = str(inline_assets.javascript)
    style_hash = inline_assets.style_csp_hash.removeprefix("sha256-")
    script_hash = inline_assets.script_csp_hash.removeprefix("sha256-")
    assert '<html lang="zh-CN">' in suite_html
    assert "自动化巡检" in suite_html
    assert "JMeter 自动化巡检" not in suite_html
    assert "本次巡检通过" in suite_html
    assert "status-badge status-passed" in suite_html
    assert "2026-07-31 09:02:03" in suite_html
    assert "2026-07-31 09:02:08" in suite_html
    assert "5 秒" in suite_html
    for card in (
        "requests",
        "passed-requests",
        "failed-requests",
        "failed-assertions",
    ):
        assert f'data-summary-card="{card}"' in suite_html
    assert 'data-summary-card="scripts"' not in suite_html
    assert "脚本总数" not in suite_html
    assert "脚本结果" not in suite_html
    assert "登录脚本" not in suite_html
    assert "本次无失败接口" in suite_html
    assert "报告包含完整敏感数据，并将永久保留" not in suite_html
    assert '<details class="data-notice">' not in suite_html
    assert 'href="scripts/' not in suite_html
    assert 'href="samples/' not in suite_html
    assert 'data-sample-item' in suite_html
    assert 'data-sample-search' in suite_html
    assert 'data-visible-count' in suite_html
    assert 'data-filter="all"' in suite_html
    assert 'data-filter="passed"' in suite_html
    assert 'data-filter="failed"' in suite_html
    assert 'data-detail-toggle' in suite_html
    assert 'aria-expanded="false"' in suite_html
    assert ">展开<" in suite_html
    assert "完整响应" in suite_html
    assert 'data-raw-response' in suite_html
    # 小正文择优后按明文内联渲染，不再压缩懒加载
    assert 'data-lazy-body="response"' not in suite_html
    assert _decode_compressed_payloads(suite_html) == []
    assert "username=张三&amp;token=secret-query" in suite_html
    assert "第一行\n完整响应\n最后一行" in suite_html
    assert "navigator.clipboard" in report_js
    assert "execCommand" in report_js
    assert "fetch(" not in report_js
    assert f"<style>{report_css}</style>" in suite_html
    assert f"<script>{report_js}</script>" in suite_html
    assert f"style-src 'sha256-{style_hash}'" in suite_html
    assert f"script-src 'sha256-{script_hash}'" in suite_html
    assert "unsafe-inline" not in suite_html
    assert 'href="assets/report.css"' not in suite_html
    assert 'src="assets/report.js"' not in suite_html

    manifest_text = result.manifest_path.read_text(encoding="utf-8")
    manifest = json.loads(manifest_text)
    assert manifest == {
        "schema_version": 1,
        "run_id": "20260731-090203-deadbeef",
        "status": "passed",
        "started_at": "2026-07-31T09:02:03+08:00",
        "finished_at": "2026-07-31T09:02:08+08:00",
        "report_path": (
            "2026-08-04_07-00-01_Inspection_Report/report.html"
        ),
        "totals": {
            "scripts": 1,
            "passed": 1,
            "failed": 0,
            "error": 0,
            "timeout": 0,
            "cancelled": 0,
            "leaf_samples": 1,
            "passed_samples": 1,
            "failed_samples": 0,
        },
        "scripts": [
            {
                "name": "登录脚本",
                "status": "passed",
                "started_at": "2026-07-31T09:02:03+08:00",
                "finished_at": "2026-07-31T09:02:08+08:00",
                "duration_seconds": 5.0,
                "exit_code": 0,
                "error": None,
                "total_samples": 1,
                "leaf_samples": 1,
                "passed_samples": 1,
                "failed_samples": 0,
                "assertion_failures": 0,
                "parse_complete": True,
                "parse_error": None,
                "paths": {
                    "jtl": "artifacts/01-登录脚本/result.jtl",
                    "log": "artifacts/01-登录脚本/jmeter.log",
                    "report": (
                        "2026-08-04_07-00-01_Inspection_Report/report.html"
                    ),
                },
            }
        ],
    }
    assert "secret-query" not in manifest_text
    assert "secret-header" not in manifest_text
    assert "secret-request-body" not in manifest_text
    assert "完整响应" not in manifest_text
    assert "不应进入报告的标准输出" not in manifest_text
    assert "不应进入报告的标准错误" not in manifest_text
    # 请求头与 samplerData 正文仍在解析层剥离，不会进入报告
    assert "secret-header" not in suite_html
    assert "secret-request-body" not in suite_html


def test_report_directory_collision_uses_numbered_suffix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import jmeter_suite.report as report_module

    monkeypatch.setattr(
        report_module,
        "_report_generated_at",
        lambda: REPORT_GENERATED_AT,
        raising=False,
    )
    run_directory = tmp_path / "collision-run"
    existing = run_directory / "2026-08-04_07-00-01_Inspection_Report"
    existing.mkdir(parents=True)
    marker = existing / "keep.txt"
    marker.write_text("existing", encoding="utf-8")
    execution = _execution(run_directory, jtl=_passing_jtl())

    result = report_module.generate_suite_report(
        _suite(run_directory, execution)
    )

    assert result.report_path == (
        run_directory
        / "2026-08-04_07-00-01_Inspection_Report-02"
        / "report.html"
    )
    assert marker.read_text(encoding="utf-8") == "existing"


def test_generate_suite_html_report_reads_external_jtl_without_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from jmeter_suite.models import ReportStatus, SuiteHtmlReportResult
    import jmeter_suite.report as report_module

    monkeypatch.setattr(
        report_module,
        "_report_generated_at",
        lambda: REPORT_GENERATED_AT,
    )

    source_directory = tmp_path / "source-run"
    output_directory = tmp_path / "manual-report"
    execution = _execution(source_directory, jtl=_passing_jtl())
    suite = _suite(output_directory, execution)

    result = report_module.generate_suite_html_report(suite)

    assert isinstance(result, SuiteHtmlReportResult)
    assert result.status is ReportStatus.PASSED
    assert result.report_path == (
        output_directory
        / "2026-08-04_07-00-01_Inspection_Report"
        / "report.html"
    )
    assert result.report_path.is_file()
    assert {
        path.relative_to(result.report_path.parent).as_posix()
        for path in result.report_path.parent.rglob("*")
        if path.is_file()
    } == {"report.html"}
    assert not (output_directory / "manifest.json").exists()
    assert not (output_directory / "artifacts").exists()
    assert execution.jtl_path.is_file()


def test_inline_request_details_remove_metadata_and_show_readable_empty_values(
    tmp_path: Path,
):
    from jmeter_suite.report import generate_suite_report

    populated_run = tmp_path / "populated-metadata-run"
    populated_execution = _execution(
        populated_run,
        jtl=_passing_jtl(),
    )

    populated_result = generate_suite_report(
        _suite(populated_run, populated_execution)
    )

    populated_html = populated_result.report_path.read_text(encoding="utf-8")
    assert "345 ms" in populated_html
    assert "请求参数" in populated_html
    assert "响应参数" in populated_html
    for removed_label in (
        "标签",
        "类型",
        "序号",
        "层级深度",
        "采样时间（北京时间）",
        "原始时间戳（毫秒）",
        "线程名称",
        "叶子样本",
        "数据类型",
        "编码",
        "请求头",
        "响应头",
    ):
        assert removed_label not in populated_html

    missing_run = tmp_path / "missing-metadata-run"
    missing_execution = _execution(
        missing_run,
        jtl=(
            '<testResults><httpSample lb="缺失元数据" '
            's="true" rc="204" /></testResults>'
        ),
    )

    missing_result = generate_suite_report(
        _suite(missing_run, missing_execution)
    )

    missing_html = missing_result.report_path.read_text(encoding="utf-8")
    assert "无请求参数" in missing_html
    assert "无响应参数" in missing_html
    assert "未记录 URL" in missing_html
    assert ">None<" not in missing_html


def test_report_ignores_logical_controller_samples_and_counts_only_http_interfaces(
    tmp_path: Path,
):
    from jmeter_suite.models import ReportStatus
    from jmeter_suite.report import generate_suite_report

    run_directory = tmp_path / "logical-controller-filter"
    jtl = """\
<testResults>
  <sample lb="AMAZON 逻辑控制器" s="false" rc="" t="50265">
    <responseData>controller-only-response</responseData>
    <assertionResult>
      <name>控制器断言</name>
      <failure>true</failure>
      <error>false</error>
      <failureMessage>controller-only-failure</failureMessage>
    </assertionResult>
  </sample>
  <httpSample lb="真实 HTTP 接口" s="true" rc="200" t="25">
    <responseData>api-response</responseData>
  </httpSample>
</testResults>
"""
    execution = _execution(run_directory, jtl=jtl)

    result = generate_suite_report(_suite(run_directory, execution))

    assert result.status is ReportStatus.PASSED
    assert result.scripts[0].status is ReportStatus.PASSED
    parse_result = result.scripts[0].parse_result
    assert parse_result.total_samples == 1
    assert parse_result.leaf_samples == 1
    assert parse_result.passed_leaf_samples == 1
    assert parse_result.failed_leaf_samples == 0
    assert parse_result.assertion_failures == 0
    assert [summary.sample_type for summary in parse_result.summaries] == [
        "httpSample"
    ]

    report = result.report_path.read_text(encoding="utf-8")
    assert len(re.findall(r"<article\b[^>]*\bdata-sample-item\b", report)) == 1
    assert "真实 HTTP 接口" in report
    assert "未记录 URL" in report
    assert _decode_compressed_payloads(report) == []
    assert "api-response" in report
    assert "无请求参数" in report
    assert "AMAZON 逻辑控制器" not in report
    assert "controller-only-response" not in report
    assert "controller-only-failure" not in report

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "passed"
    assert manifest["totals"]["leaf_samples"] == 1
    assert manifest["totals"]["passed_samples"] == 1
    assert manifest["totals"]["failed_samples"] == 0
    assert manifest["scripts"][0]["total_samples"] == 1
    assert manifest["scripts"][0]["assertion_failures"] == 0


def test_failed_assertion_fails_script_and_failed_request_is_rendered_first(
    tmp_path: Path,
):
    from jmeter_suite.models import ReportStatus
    from jmeter_suite.report import generate_suite_report

    run_directory = tmp_path / "failed-run"
    nested_jtl = """\
<testResults>
  <sample lb="父事务" s="true" rc="200">
    <httpSample lb="通过子样本" s="true" rc="204">
      <responseData>child passed</responseData>
    </httpSample>
    <httpSample lb="失败子样本" s="false" rc="200" rm="OK">
      <responseData>child assertion failed</responseData>
      <assertionResult>
        <name>正文断言</name>
        <failure>true</failure>
        <error>false</error>
        <failureMessage>缺少预期字段</failureMessage>
      </assertionResult>
    </httpSample>
  </sample>
</testResults>
"""
    execution = _execution(run_directory, jtl=nested_jtl)

    result = generate_suite_report(_suite(run_directory, execution))

    assert result.status is ReportStatus.FAILED
    assert result.scripts[0].status is ReportStatus.FAILED
    parse_result = result.scripts[0].parse_result
    assert parse_result.total_samples == 2
    assert parse_result.leaf_samples == 2
    assert parse_result.passed_leaf_samples == 1
    assert parse_result.failed_leaf_samples == 1
    assert parse_result.assertion_failures == 1
    script_html = result.scripts[0].report_path.read_text(encoding="utf-8")
    assert script_html.index("失败子样本") < script_html.index("通过子样本")
    assert "父事务" not in script_html
    overview = re.search(
        r'<section class="panel overview-panel".*?</section>',
        script_html,
        flags=re.DOTALL,
    )
    assert overview is not None
    assert "查询数据范围" in overview.group(0)
    assert "未携带时间条件" in overview.group(0)
    assert "正文断言：缺少预期字段" not in overview.group(0)
    assert "正文断言" in script_html
    assert "缺少预期字段" in script_html
    assert result.scripts[0].sample_paths == ()
    assert 'href="samples/' not in script_html
    assert len(
        re.findall(r"<article\b[^>]*\bdata-sample-item\b", script_html)
    ) == 2
    failed_sample = re.search(
        r'<article[^>]+data-sample-id="sample-000003".*?</article>',
        script_html,
        flags=re.DOTALL,
    )
    assert failed_sample is not None
    assert 'data-status="failed"' in failed_sample.group(0)
    assert 'aria-expanded="true"' in failed_sample.group(0)
    assert ">收起<" in failed_sample.group(0)
    assert "正文断言" in failed_sample.group(0)
    assert "缺少预期字段" in failed_sample.group(0)

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["status"] == "failed"
    assert manifest["totals"] == {
        "scripts": 1,
        "passed": 0,
        "failed": 1,
        "error": 0,
        "timeout": 0,
        "cancelled": 0,
        "leaf_samples": 2,
        "passed_samples": 1,
        "failed_samples": 1,
    }
    assert manifest["scripts"][0]["assertion_failures"] == 1


def test_process_error_with_missing_jtl_stays_visible_on_single_report(
    tmp_path: Path,
):
    from jmeter_suite.models import ReportStatus
    from jmeter_suite.report import generate_suite_report

    run_directory = tmp_path / "process-error-run"
    execution = _execution(
        run_directory,
        status=ExecutionStatus.ERROR,
        exit_code=7,
        error="JMeter exited with exit code 7",
        jtl=None,
    )

    result = generate_suite_report(_suite(run_directory, execution))

    assert result.status is ReportStatus.FAILED
    assert result.scripts[0].status is ReportStatus.ERROR
    assert result.scripts[0].sample_paths == ()
    assert result.scripts[0].parse_result.complete is False
    script_html = result.scripts[0].report_path.read_text(encoding="utf-8")
    suite_html = result.report_path.read_text(encoding="utf-8")
    assert "进程未正常完成" in script_html
    assert "JMeter exited with exit code 7" in script_html
    assert "JTL 解析不完整" in script_html
    assert "打开脚本报告" not in suite_html
    assert "执行错误" in suite_html
    assert "执行错误" in script_html
    assert result.report_path.is_file()
    assert result.scripts[0].report_path.is_file()
    assert result.scripts[0].report_path == result.report_path
    assert list(run_directory.rglob("*.html")) == [result.report_path]
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["totals"]["error"] == 1
    assert manifest["scripts"][0]["status"] == "error"
    assert manifest["scripts"][0]["parse_complete"] is False


def test_timeout_and_cancelled_status_override_partial_jtl_and_keep_details(
    tmp_path: Path,
):
    from jmeter_suite.models import ReportStatus
    from jmeter_suite.report import generate_suite_report

    partial_jtl = """\
<testResults>
  <httpSample lb="已完成部分" s="true" rc="200">
    <responseData>partial response remains visible</responseData>
  </httpSample>
  <httpSample lb="未完成部分" s="true" rc="200">
    <responseData>truncated
</testResults>
"""
    run_directory = tmp_path / "interrupted-run"
    timeout_execution = _execution(
        run_directory,
        artifact_name="01-超时脚本",
        script_name="超时脚本",
        status=ExecutionStatus.TIMEOUT,
        exit_code=-9,
        error="JMeter execution timed out",
        jtl=partial_jtl,
    )
    cancelled_execution = _execution(
        run_directory,
        artifact_name="02-取消脚本",
        script_name="取消脚本",
        status=ExecutionStatus.CANCELLED,
        exit_code=-15,
        error="JMeter execution was cancelled",
        jtl=partial_jtl,
        process_tree_termination_confirmed=False,
    )

    result = generate_suite_report(
        _suite(run_directory, timeout_execution, cancelled_execution)
    )

    assert result.status is ReportStatus.FAILED
    assert [script.status for script in result.scripts] == [
        ReportStatus.TIMEOUT,
        ReportStatus.CANCELLED,
    ]
    assert result.scripts[0].process_tree_termination_confirmed is None
    assert result.scripts[1].process_tree_termination_confirmed is False
    for script in result.scripts:
        assert script.parse_result.complete is False
        assert script.parse_result.total_samples == 1
        assert script.sample_paths == ()
        assert script.report_path == result.report_path
        script_html = script.report_path.read_text(encoding="utf-8")
        assert "JTL 解析不完整" in script_html
        assert "已完成部分" in script_html
        assert "partial response remains visible" in script_html
    assert _decode_compressed_payloads(script_html) == []
    assert script_html.count("partial response remains visible") == 2
    suite_html = result.report_path.read_text(encoding="utf-8")
    assert "超时" in suite_html
    assert "已取消" in suite_html
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert "process_tree_termination_confirmed" not in manifest["scripts"][1]
    assert manifest["totals"] == {
        "scripts": 2,
        "passed": 0,
        "failed": 0,
        "error": 0,
        "timeout": 1,
        "cancelled": 1,
        "leaf_samples": 2,
        "passed_samples": 2,
        "failed_samples": 0,
    }


def test_completed_malformed_jtl_is_error_with_partial_warning_and_detail(
    tmp_path: Path,
):
    from jmeter_suite.models import ReportStatus
    from jmeter_suite.report import generate_suite_report

    fixture = (
        Path(__file__).parent / "fixtures" / "malformed-partial.jtl"
    ).read_text(encoding="utf-8")
    run_directory = tmp_path / "malformed-run"
    execution = _execution(run_directory, jtl=fixture)

    result = generate_suite_report(_suite(run_directory, execution))

    script = result.scripts[0]
    assert result.status is ReportStatus.FAILED
    assert script.status is ReportStatus.ERROR
    assert script.parse_result.complete is False
    assert script.parse_result.total_samples == 1
    assert script.parse_result.error is not None
    assert script.parse_result.error.startswith("XML parse error:")
    assert script.sample_paths == ()
    assert script.report_path == result.report_path
    script_html = script.report_path.read_text(encoding="utf-8")
    assert "JTL 解析不完整" in script_html
    assert "以下仅展示已经完整结束的请求" in script_html
    assert "已完成" in script_html
    assert _decode_compressed_payloads(script_html) == []
    assert "完整样本" in script_html
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["totals"]["error"] == 1
    assert manifest["totals"]["failed"] == 0
    assert manifest["scripts"][0]["status"] == "error"


def test_completed_zero_leaf_and_empty_jtl_are_errors_with_clear_reasons(
    tmp_path: Path,
):
    from jmeter_suite.models import ReportStatus
    from jmeter_suite.report import generate_suite_report

    run_directory = tmp_path / "empty-results-run"
    no_samples = _execution(
        run_directory,
        artifact_name="01-无样本",
        script_name="无样本",
        jtl="<testResults />",
    )
    empty_file = _execution(
        run_directory,
        artifact_name="02-空文件",
        script_name="空文件",
        jtl="",
    )

    result = generate_suite_report(
        _suite(run_directory, no_samples, empty_file)
    )

    assert result.status is ReportStatus.FAILED
    assert [script.status for script in result.scripts] == [
        ReportStatus.ERROR,
        ReportStatus.ERROR,
    ]
    assert result.scripts[0].parse_result.complete is True
    assert result.scripts[0].parse_result.leaf_samples == 0
    assert result.scripts[0].error == "JTL 中没有可报告的 HTTP 接口样本"
    assert result.scripts[1].parse_result.complete is False
    assert result.scripts[1].parse_result.leaf_samples == 0
    assert result.scripts[1].parse_result.error == "JTL 文件为空"
    assert "JTL 中没有可报告的 HTTP 接口样本" in result.scripts[
        0
    ].report_path.read_text(encoding="utf-8")
    assert "JTL 文件为空" in result.scripts[1].report_path.read_text(
        encoding="utf-8"
    )
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["totals"] == {
        "scripts": 2,
        "passed": 0,
        "failed": 0,
        "error": 2,
        "timeout": 0,
        "cancelled": 0,
        "leaf_samples": 0,
        "passed_samples": 0,
        "failed_samples": 0,
    }


def test_html_is_csp_safe_escapes_injection_and_preserves_large_response(
    tmp_path: Path,
):
    from jmeter_suite.models import ReportStatus
    from jmeter_suite.report import _load_inline_assets, generate_suite_report

    run_directory = tmp_path / "security-run"
    injected_label = '危险"><script>alert("label")</script>'
    response_data = (
        "BEGIN-LARGE-RESPONSE\n"
        + ("0123456789汉字<&>\"'\n" * 70_000)
        + '</pre><script>alert("response")</script>\n'
        + "END-LARGE-RESPONSE"
    )
    assert len(response_data.encode("utf-8")) > 1_048_576
    escaped_label = html.escape(injected_label, quote=True)
    jtl = f"""\
<testResults>
  <httpSample lb="{escaped_label}" s="true" rc="200" rm="OK">
    <java.net.URL>https://example.test/raw</java.net.URL>
    <queryString><![CDATA[</pre><script>alert("query")</script>]]></queryString>
    <requestHeader><![CDATA[X-Raw: </pre><script>alert("header")</script>]]></requestHeader>
    <samplerData><![CDATA[request body <script>alert("body")</script>]]></samplerData>
    <responseHeader><![CDATA[HTTP/1.1 200 OK]]></responseHeader>
    <responseData><![CDATA[{response_data}]]></responseData>
  </httpSample>
</testResults>
"""
    execution = _execution(run_directory, jtl=jtl)

    result = generate_suite_report(_suite(run_directory, execution))

    assert result.status is ReportStatus.PASSED
    sample_html = result.report_path.read_text(encoding="utf-8")
    assert _decode_compressed_payloads(sample_html) == [
        {
            "request": '</pre><script>alert("query")</script>',
            "response": response_data,
        }
    ]
    assert "BEGIN-LARGE-RESPONSE" not in sample_html
    assert "END-LARGE-RESPONSE" not in sample_html
    assert "<script>alert" not in sample_html
    assert "&lt;/pre&gt;&lt;script&gt;alert" not in sample_html
    heading_match = re.search(r"<h4>(.*?)</h4>", sample_html)
    assert heading_match is not None
    assert html.unescape(heading_match.group(1)) == injected_label
    manifest_text = result.manifest_path.read_text(encoding="utf-8")
    assert "BEGIN-LARGE-RESPONSE" not in manifest_text
    assert "alert" not in manifest_text

    package_root = resources.files("jmeter_suite")
    inline_assets = _load_inline_assets()
    report_css = str(inline_assets.css)
    report_js = str(inline_assets.javascript)
    style_hash = inline_assets.style_csp_hash.removeprefix("sha256-")
    script_hash = inline_assets.script_csp_hash.removeprefix("sha256-")
    expected_csp = (
        "default-src 'none'; "
        f"style-src 'sha256-{style_hash}'; "
        f"script-src 'sha256-{script_hash}'; "
        "img-src data:; base-uri 'none'; form-action 'none'"
    )
    rendered_pages = list(run_directory.rglob("*.html"))
    assert rendered_pages == [result.report_path]
    for page_path in rendered_pages:
        page = page_path.read_text(encoding="utf-8")
        assert '<html lang="zh-CN">' in page
        assert (
            '<meta http-equiv="Content-Security-Policy" '
            f'content="{expected_csp}">'
        ) in page
        assert f"<style>{report_css}</style>" in page
        assert re.search(r"\son[a-z]+\s*=", page, flags=re.IGNORECASE) is None
        script_tags = re.findall(
            r"<script\b([^>]*)>",
            page,
            flags=re.IGNORECASE,
        )
        assert script_tags == [""]
        assert f"<script>{report_js}</script>" in page
        resource_links = re.findall(
            r'(?:href|src)="([^"]+)"',
            page,
            flags=re.IGNORECASE,
        )
        assert all(
            not link.lower().startswith(("http://", "https://"))
            for link in resource_links
        )

    for template_name in (
        "base.html",
        "suite.html",
        "script.html",
        "endpoint_group.html",
        "sample.html",
    ):
        source = package_root.joinpath(
            "templates",
            template_name,
        ).read_text(encoding="utf-8")
        assert "|safe" not in source.replace(" ", "")
        assert "http://" not in source
        assert "https://" not in source
        assert re.search(
            r"\son[a-z]+\s*=",
            source,
            flags=re.IGNORECASE,
        ) is None
    report_js = package_root.joinpath(
        "static",
        "report.js",
    ).read_text(encoding="utf-8")
    assert "fetch(" not in report_js
    assert ".style." not in report_js


def test_zero_execution_suite_is_failed_and_manifest_is_atomically_replaced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    from jmeter_suite.models import ReportStatus
    import jmeter_suite.report as report_module

    monkeypatch.setattr(
        report_module,
        "_report_generated_at",
        lambda: REPORT_GENERATED_AT,
    )

    run_directory = tmp_path / "zero-execution-run"
    run_directory.mkdir()
    manifest_path = run_directory / "manifest.json"
    manifest_path.write_text("old manifest", encoding="utf-8")
    offset = timezone(timedelta(hours=8))
    suite = SuiteProcessResult(
        run_id="zero-execution",
        run_directory=run_directory,
        started_at=datetime(
            2026,
            7,
            31,
            9,
            2,
            3,
            123456,
            tzinfo=offset,
        ),
        finished_at=datetime(
            2026,
            7,
            31,
            9,
            2,
            8,
            654321,
            tzinfo=offset,
        ),
        executions=(),
    )
    real_replace = report_module.os.replace
    replacements: list[tuple[Path, Path]] = []

    def observed_replace(source: str | Path, target: str | Path) -> None:
        source_path = Path(source)
        target_path = Path(target)
        assert source_path.is_file()
        assert source_path.name == "manifest.json.tmp"
        assert target_path == manifest_path
        assert target_path.read_text(encoding="utf-8") == "old manifest"
        replacements.append((source_path, target_path))
        real_replace(source_path, target_path)

    monkeypatch.setattr(report_module.os, "replace", observed_replace)

    result = report_module.generate_suite_report(suite)

    assert result.status is ReportStatus.FAILED
    assert result.scripts == ()
    assert replacements == [
        (run_directory / "manifest.json.tmp", manifest_path)
    ]
    assert not list(run_directory.rglob("*.tmp"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest == {
        "schema_version": 1,
        "run_id": "zero-execution",
        "status": "failed",
        "started_at": "2026-07-31T09:02:03.123456+08:00",
        "finished_at": "2026-07-31T09:02:08.654321+08:00",
        "report_path": (
            "2026-08-04_07-00-01_Inspection_Report/report.html"
        ),
        "totals": {
            "scripts": 0,
            "passed": 0,
            "failed": 0,
            "error": 0,
            "timeout": 0,
            "cancelled": 0,
            "leaf_samples": 0,
            "passed_samples": 0,
            "failed_samples": 0,
        },
        "scripts": [],
    }
    report_html = result.report_path.read_text(encoding="utf-8")
    assert "没有可汇总的失败接口" in report_html
    assert "没有可展示的请求明细" in report_html


def test_sample_render_callback_failure_propagates_without_fake_parse_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import jmeter_suite.report as report_module

    run_directory = tmp_path / "render-failure-run"
    execution = _execution(run_directory, jtl=_passing_jtl())
    real_write_page = report_module._write_page

    def fail_sample_page(
        environment: object,
        template_name: str,
        destination: Path,
        **context: object,
    ) -> None:
        if template_name == "sample.html":
            raise RuntimeError("sample template render failed")
        real_write_page(
            environment,
            template_name,
            destination,
            **context,
        )

    monkeypatch.setattr(report_module, "_write_page", fail_sample_page)

    with pytest.raises(
        RuntimeError,
        match="sample template render failed",
    ):
        report_module.generate_suite_report(
            _suite(run_directory, execution)
        )

    assert not (run_directory / "manifest.json").exists()


def test_packaged_templates_and_static_assets_are_resource_accessible():
    package_root = resources.files("jmeter_suite")
    expected_resources = {
        "templates/base.html",
        "templates/suite.html",
        "templates/script.html",
        "templates/endpoint_group.html",
        "templates/sample.html",
        "static/report.css",
        "static/report.js",
    }
    for relative_path in expected_resources:
        resource = package_root.joinpath(*relative_path.split("/"))
        assert resource.is_file()
        assert resource.read_text(encoding="utf-8")

    report_js = package_root.joinpath("static", "report.js").read_text(
        encoding="utf-8"
    )
    report_css = package_root.joinpath("static", "report.css").read_text(
        encoding="utf-8"
    )
    assert "data-sample-search" in report_js
    assert "data-visible-count" in report_js
    assert "position: sticky" in report_css
    assert ":focus-visible" in report_css

    project = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text(
            encoding="utf-8"
        )
    )
    assert set(
        project["tool"]["setuptools"]["package-data"]["jmeter_suite"]
    ) == {
        "templates/*.html",
        "static/*.css",
        "static/*.js",
    }
