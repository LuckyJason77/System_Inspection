"""验证单页报告的接口概览、请求明细、筛选复制和批量展开交互。"""

from __future__ import annotations

from datetime import datetime, timezone
import html
import json
from pathlib import Path
import re

import pytest

from jmeter_suite.models import (
    ExecutionStatus,
    JMeterExecutionResult,
    SuiteProcessResult,
)


STARTED_AT = datetime(2026, 8, 3, 7, 23, 5, tzinfo=timezone.utc)
FINISHED_AT = datetime(2026, 8, 3, 7, 24, 43, tzinfo=timezone.utc)


def _execution(
    run_directory: Path,
    jtl: str | None,
    *,
    name: str = "业务巡检",
    artifact_name: str = "01-业务巡检",
    status: ExecutionStatus = ExecutionStatus.COMPLETED,
    exit_code: int | None = 0,
    error: str | None = None,
) -> JMeterExecutionResult:
    artifact_directory = run_directory / "artifacts" / artifact_name
    artifact_directory.mkdir(parents=True, exist_ok=True)
    jtl_path = artifact_directory / "result.jtl"
    if jtl is not None:
        jtl_path.write_text(jtl, encoding="utf-8")
    log_path = artifact_directory / "jmeter.log"
    log_path.write_text("JMeter log", encoding="utf-8")
    return JMeterExecutionResult(
        script_name=name,
        status=status,
        started_at=STARTED_AT,
        finished_at=FINISHED_AT,
        exit_code=exit_code,
        error=error,
        jtl_path=jtl_path,
        log_path=log_path,
        stdout="",
        stderr="",
    )


def _suite(
    run_directory: Path,
    *executions: JMeterExecutionResult,
) -> SuiteProcessResult:
    return SuiteProcessResult(
        run_id="2026-08-03_15-23",
        run_directory=run_directory,
        started_at=STARTED_AT,
        finished_at=FINISHED_AT,
        executions=tuple(executions),
    )


def _sample(
    *,
    label: str,
    success: bool = True,
    response_code: str = "200",
    timestamp_ms: int | None = None,
    url: str = "https://example.test/api",
    query: str = "page=1&amp;limit=20",
    sampler_data: str = "",
    response: str = '{"code":200,"message":"success"}',
    request_header: str = "Authorization: Bearer request-header-secret",
    response_header: str = "X-Response-Secret: response-header-secret",
    assertion: str = "",
) -> str:
    timestamp_attribute = (
        f' ts="{timestamp_ms}"' if timestamp_ms is not None else ""
    )
    return f"""\
  <httpSample lb="{label}" s="{str(success).lower()}" rc="{response_code}" t="438"{timestamp_attribute}>
    <java.net.URL>{url}</java.net.URL>
    <queryString>{query}</queryString>
    <requestHeader>{request_header}</requestHeader>
    <samplerData>{sampler_data}</samplerData>
    <responseHeader>{response_header}</responseHeader>
    <responseData>{response}</responseData>
    {assertion}
  </httpSample>
"""


def _jtl(*samples: str) -> str:
    return "<testResults>\n" + "".join(samples) + "</testResults>\n"


def test_report_is_one_page_with_parameters_and_without_removed_fields(
    tmp_path: Path,
):
    from jmeter_suite.models import ReportStatus
    from jmeter_suite.report import generate_suite_report

    run_directory = tmp_path / "single-page"
    execution = _execution(
        run_directory,
        _jtl(
            _sample(
                label="销售排行",
                query="date_start=2026-08-01&amp;page=1",
                sampler_data="request-body-must-not-replace-query",
                response="{&quot;code&quot;:200,&quot;total&quot;:46}",
            )
        ),
    )

    result = generate_suite_report(_suite(run_directory, execution))

    assert result.status is ReportStatus.PASSED
    assert list(run_directory.rglob("*.html")) == [result.report_path]
    assert result.report_path.name == "report.html"
    assert result.report_path.parent.name.endswith("_Inspection_Report")
    assert result.scripts[0].report_path == result.report_path
    assert result.scripts[0].sample_paths == ()
    assert not (run_directory / "scripts").exists()

    report = result.report_path.read_text(encoding="utf-8")
    assert "自动化巡检" in report
    assert "销售排行" in report
    assert "请求参数" in report
    assert "date_start=2026-08-01&amp;page=1" in report
    assert "响应参数" in report
    assert '{"code":200,"total":46}' in html.unescape(report)
    assert "request-header-secret" not in report
    assert "response-header-secret" not in report
    assert "request-body-must-not-replace-query" not in report
    assert "请求头" not in report
    assert "响应头" not in report
    for removed_label in (
        "标签",
        "类型",
        "序号",
        "层级深度",
        "原始时间戳（毫秒）",
        "线程名称",
        "叶子样本",
        "数据类型",
        "编码",
    ):
        assert removed_label not in report
    assert 'href="scripts/' not in report
    assert 'href="samples/' not in report
    assert 'data-all-details-toggle' in report
    assert '>展开全部</span>' in report

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    relative_report_path = result.report_path.relative_to(
        run_directory
    ).as_posix()
    assert manifest["report_path"] == relative_report_path
    assert manifest["scripts"][0]["paths"]["report"] == (
        relative_report_path
    )
    assert "date_start" not in json.dumps(manifest, ensure_ascii=False)
    assert "total&quot;" not in json.dumps(manifest, ensure_ascii=False)


def test_failed_interface_overview_replaces_script_dimension(
    tmp_path: Path,
):
    from jmeter_suite.report import generate_suite_report

    run_directory = tmp_path / "failed-interface-overview"
    execution = _execution(
        run_directory,
        _jtl(
            _sample(
                label="正常接口",
                timestamp_ms=0,
                url="https://example.test/passed",
            ),
            _sample(
                label="库存查询失败接口",
                success=False,
                response_code="503",
                timestamp_ms=1000,
                url="https://example.test/failed",
            ),
        ),
        name="不应出现在正常报告中的脚本名",
    )

    result = generate_suite_report(_suite(run_directory, execution))

    report = result.report_path.read_text(encoding="utf-8")
    assert report.count("data-summary-card=") == 4
    assert 'data-summary-card="scripts"' not in report
    assert "脚本总数" not in report
    assert "脚本结果" not in report
    assert "不应出现在正常报告中的脚本名" not in report
    assert "script-caption" not in report

    overview = re.search(
        r'<section class="panel overview-panel".*?</section>',
        report,
        flags=re.DOTALL,
    )
    assert overview is not None
    overview_html = overview.group(0)
    assert "失败接口概览" in overview_html
    assert "失败 1 / 2" in overview_html
    assert "失败率 50%" in overview_html
    assert overview_html.count("data-failed-overview-item") == 1
    assert "库存查询失败接口" in overview_html
    assert "https://example.test/failed" in overview_html
    assert "1970-01-01 08:00:01" in overview_html
    assert ">响应码<" not in overview_html
    assert ">耗时<" not in overview_html
    assert "503" not in overview_html
    assert "438 ms" not in overview_html
    assert "查询数据范围" in overview_html

    details_html = report[report.index('<section class="details-area"') :]
    assert "<small>响应码</small>" in details_html
    assert "<small>耗时</small>" in details_html
    assert "503" in details_html
    assert "438 ms" in details_html
    assert "未携带时间条件" in overview_html
    assert "HTTP 503" not in overview_html
    assert "正常接口" not in overview_html
    assert "首次失败请求时间" not in report
    assert "最慢失败接口" not in report


@pytest.mark.parametrize(
    ("query", "sampler_data", "expected"),
    [
        (
            "date_start=2026-06-01&amp;date_end=2026-06-30",
            "",
            "2026-06",
        ),
        (
            "date_start=2026%2D06%2D01&amp;date_end=2026%2D08%2D31",
            "",
            "2026-06 ～ 2026-08",
        ),
        (
            "data_month_start=2026&amp;data_month_end=2026",
            "",
            "2026 全年",
        ),
        (
            "dateRange=[&quot;2026-06-01 00:00:01&quot;,&quot;2026-08-31 23:59:59&quot;]",
            "",
            "2026-06 ～ 2026-08",
        ),
        (
            "",
            "{&quot;created_at_start&quot;:&quot;2026-07-01&quot;,&quot;created_at_end&quot;:&quot;2026-07-31&quot;}",
            "2026-07",
        ),
        ("time_type=platform&amp;page=1", "", "未携带时间条件"),
        ("date_start=${start_1}&amp;date_end=${end_1}", "", "无法识别"),
        (
            "date_start=2026-04-01&amp;date_end=2026-04-31",
            "",
            "2026-04（日期参数异常）",
        ),
        (
            "date_start=2026-08-01&amp;date_end=2026-06-30",
            "",
            "2026-08 ～ 2026-06（日期参数异常）",
        ),
    ],
)
def test_interface_header_normalizes_query_data_range(
    tmp_path: Path,
    query: str,
    sampler_data: str,
    expected: str,
):
    from jmeter_suite.report import generate_suite_report

    run_directory = tmp_path / "query-data-range"
    execution = _execution(
        run_directory,
        _jtl(
            _sample(
                label="数据查询接口",
                query=query,
                sampler_data=sampler_data,
            )
        ),
    )

    result = generate_suite_report(_suite(run_directory, execution))

    report = html.unescape(result.report_path.read_text(encoding="utf-8"))
    assert f"查询数据范围：{expected}" in report
    sample_card = re.search(
        r'<article[^>]+data-sample-item[^>]*>',
        report,
    )
    assert sample_card is not None
    card_attributes = sample_card.group(0)
    assert f'data-query-data-range="{expected}"' in card_attributes
    assert (
        'data-period-always-visible="true"' in card_attributes
    ) is (expected == "未携带时间条件")
    search_text = re.search(
        r'data-search-text="([^"]*)"',
        card_attributes,
    )
    assert search_text is not None
    assert search_text.group(1) == "数据查询接口 https://example.test/api"


def test_query_data_range_filter_metadata_covers_overview_and_details(
    tmp_path: Path,
):
    from jmeter_suite.report import generate_suite_report

    run_directory = tmp_path / "query-data-range-filter"
    execution = _execution(
        run_directory,
        _jtl(
            _sample(
                label="六月失败接口",
                success=False,
                response_code="598",
                url="https://example.test/orders/june",
                query="date_start=2026-06-01&amp;date_end=2026-06-30",
            ),
            _sample(
                label="无时间失败接口",
                success=False,
                response_code="599",
                url="https://example.test/orders/no-time",
                query="page=1",
            ),
            _sample(
                label="七月通过接口",
                url="https://example.test/orders/july",
                query="date_start=2026-07-01&amp;date_end=2026-07-31",
            ),
            _sample(
                label="六月通过接口",
                url="https://example.test/orders/june-pass",
                query="month=2026-06",
            ),
        ),
    )

    result = generate_suite_report(_suite(run_directory, execution))

    report = html.unescape(result.report_path.read_text(encoding="utf-8"))
    period_selects = re.findall(
        r'<select[^>]+data-period-filter[^>]*>(.*?)</select>',
        report,
        flags=re.DOTALL,
    )
    assert len(period_selects) == 2
    for select_html in period_selects:
        assert re.findall(r'<option value="([^"]*)"', select_html) == [
            "all",
            "2026-06",
            "2026-07",
        ]
        assert "未携带时间条件" not in select_html

    june_card = re.search(
        r'<article[^>]+data-sample-id="sample-000001"[^>]*>',
        report,
    )
    no_time_card = re.search(
        r'<article[^>]+data-sample-id="sample-000002"[^>]*>',
        report,
    )
    assert june_card is not None
    assert no_time_card is not None
    assert 'data-query-data-range="2026-06"' in june_card.group(0)
    assert 'data-period-always-visible="false"' in june_card.group(0)
    assert 'data-query-data-range="未携带时间条件"' in no_time_card.group(0)
    assert 'data-period-always-visible="true"' in no_time_card.group(0)

    overview_rows = re.findall(
        r'<tr[^>]+data-failed-overview-item[^>]*>',
        report,
    )
    assert len(overview_rows) == 2
    assert 'data-query-data-range="2026-06"' in overview_rows[0]
    assert 'data-period-always-visible="false"' in overview_rows[0]
    assert 'data-query-data-range="未携带时间条件"' in overview_rows[1]
    assert 'data-period-always-visible="true"' in overview_rows[1]
    assert "data-overview-visible-count" in report

    june_search_text = re.search(
        r'data-search-text="([^"]*)"',
        june_card.group(0),
    )
    assert june_search_text is not None
    assert june_search_text.group(1) == (
        "六月失败接口 https://example.test/orders/june"
    )
    assert "598" not in june_search_text.group(1)
    assert "2026-06" not in june_search_text.group(1)
    assert "搜索请求名称或 URL" in report
    assert "搜索请求名称、响应码或 URL" not in report


def test_failed_overview_replaces_failure_reason_with_query_data_range(
    tmp_path: Path,
):
    from jmeter_suite.report import generate_suite_report

    run_directory = tmp_path / "failed-query-data-range"
    execution = _execution(
        run_directory,
        _jtl(
            _sample(
                label="失败月份接口",
                success=False,
                response_code="503",
                query="date_start=2026-06-01&amp;date_end=2026-08-31",
            )
        ),
    )

    result = generate_suite_report(_suite(run_directory, execution))

    report = html.unescape(result.report_path.read_text(encoding="utf-8"))
    overview = re.search(
        r'<section class="panel overview-panel".*?</section>',
        report,
        flags=re.DOTALL,
    )
    assert overview is not None
    overview_html = overview.group(0)
    assert "查询数据范围" in overview_html
    assert "2026-06 ～ 2026-08" in overview_html
    assert "失败原因" not in overview_html
    assert "HTTP 503" not in overview_html


def test_every_interface_header_boldly_displays_request_time_to_second(
    tmp_path: Path,
):
    from jmeter_suite.report import generate_suite_report

    run_directory = tmp_path / "request-time"
    execution = _execution(
        run_directory,
        _jtl(
            _sample(label="通过接口", timestamp_ms=0),
            _sample(
                label="失败接口",
                success=False,
                response_code="500",
                timestamp_ms=1000,
            ),
            _sample(label="未记录时间接口"),
        ),
    )

    result = generate_suite_report(_suite(run_directory, execution))

    report = result.report_path.read_text(encoding="utf-8")
    assert report.count('class="request-time"') == 3
    assert re.search(
        r'<h4>通过接口</h4>\s*<strong class="request-time">'
        r'请求时间：1970-01-01 08:00:00</strong>',
        report,
    )
    assert re.search(
        r'<h4>失败接口</h4>\s*<strong class="request-time">'
        r'请求时间：1970-01-01 08:00:01</strong>',
        report,
    )
    assert re.search(
        r'<h4>未记录时间接口</h4>\s*<strong class="request-time">'
        r'请求时间：未记录</strong>',
        report,
    )
    assert "08:00:00.000" not in report
    assert "08:00:01.000" not in report


def test_get_query_uses_url_fallback_and_post_body_uses_sampler_fallback(
    tmp_path: Path,
):
    from jmeter_suite.report import generate_suite_report

    run_directory = tmp_path / "parameter-fallbacks"
    execution = _execution(
        run_directory,
        _jtl(
            _sample(
                label="GET 参数回退",
                url=(
                    "https://example.test/list?"
                    "page=2&amp;keyword=%E5%BC%A0%E4%B8%89"
                ),
                query="",
                response="get-response",
            ),
            _sample(
                label="POST 正文回退",
                url="https://example.test/create",
                query="",
                sampler_data="{&quot;name&quot;:&quot;张三&quot;}",
                response="post-response",
            ),
        ),
    )

    result = generate_suite_report(_suite(run_directory, execution))
    report = html.unescape(result.report_path.read_text(encoding="utf-8"))

    assert "page=2&keyword=%E5%BC%A0%E4%B8%89" in report
    assert '{"name":"张三"}' in report
    assert "get-response" in report
    assert "post-response" in report


def test_failed_requests_are_first_and_toggle_state_is_explicit(
    tmp_path: Path,
):
    from jmeter_suite.report import generate_suite_report

    run_directory = tmp_path / "failure-first"
    failed_assertion = """\
<assertionResult>
  <name>业务状态断言</name>
  <failure>true</failure>
  <error>false</error>
  <failureMessage>业务状态不是成功</failureMessage>
</assertionResult>
"""
    execution = _execution(
        run_directory,
        _jtl(
            _sample(label="先执行但通过", response="pass-response"),
            _sample(
                label="后执行但失败",
                response="fail-response",
                assertion=failed_assertion,
            ),
        ),
    )

    result = generate_suite_report(_suite(run_directory, execution))
    report = result.report_path.read_text(encoding="utf-8")

    assert report.index("后执行但失败") < report.index("先执行但通过")
    assert len(
        re.findall(r"<button\b[^>]*\bdata-detail-toggle\b", report)
    ) == 2
    assert len(
        re.findall(r"<button\b[^>]*\bdata-all-details-toggle\b", report)
    ) == 1
    assert ">收起全部</span>" in report
    failed_card = re.search(
        r'<article[^>]+data-sample-id="sample-000002".*?</article>',
        report,
        flags=re.DOTALL,
    )
    passed_card = re.search(
        r'<article[^>]+data-sample-id="sample-000001".*?</article>',
        report,
        flags=re.DOTALL,
    )
    assert failed_card is not None
    assert passed_card is not None
    assert 'aria-expanded="true"' in failed_card.group(0)
    assert ">收起<" in failed_card.group(0)
    assert "data-detail-panel hidden" not in failed_card.group(0)
    assert 'aria-expanded="false"' in passed_card.group(0)
    assert ">展开<" in passed_card.group(0)
    assert "data-detail-panel hidden" in passed_card.group(0)
    assert "业务状态断言" in failed_card.group(0)
    assert "业务状态不是成功" in failed_card.group(0)


def test_same_endpoint_requests_are_grouped_without_query_before_status(
    tmp_path: Path,
):
    from jmeter_suite.report import generate_suite_report

    run_directory = tmp_path / "endpoint-groups"
    execution = _execution(
        run_directory,
        _jtl(
            _sample(
                label="订单六月通过",
                url="https://example.test/orders?month=2026-06",
            ),
            _sample(
                label="用户六月失败",
                success=False,
                url="https://example.test/users?month=2026-06",
            ),
            _sample(
                label="订单七月失败",
                success=False,
                url="https://example.test/orders?month=2026-07",
            ),
            _sample(
                label="用户八月失败",
                success=False,
                url="https://example.test/users?month=2026-08",
            ),
            _sample(
                label="订单八月通过",
                url="https://example.test/orders?month=2026-08",
            ),
        ),
    )

    result = generate_suite_report(_suite(run_directory, execution))
    report = result.report_path.read_text(encoding="utf-8")

    details = report[report.index('<section class="details-area"') :]
    assert details.index("订单七月失败") < details.index("订单六月通过")
    assert details.index("订单六月通过") < details.index("订单八月通过")
    assert details.index("订单八月通过") < details.index("用户六月失败")
    assert details.index("用户六月失败") < details.index("用户八月失败")

    overview = re.search(
        r'<section class="panel overview-panel".*?</section>',
        report,
        flags=re.DOTALL,
    )
    assert overview is not None
    overview_html = overview.group(0)
    assert overview_html.index("订单七月失败") < overview_html.index("用户六月失败")
    assert overview_html.index("用户六月失败") < overview_html.index("用户八月失败")


def test_partial_jtl_and_process_errors_stay_visible_on_the_single_page(
    tmp_path: Path,
):
    from jmeter_suite.report import generate_suite_report

    run_directory = tmp_path / "partial"
    partial_jtl = """\
<testResults>
  <httpSample lb="完整样本" s="true" rc="200" t="25">
    <responseData>partial response remains visible</responseData>
  </httpSample>
  <httpSample lb="损坏样本" s="true" rc="200">
    <responseData>truncated
</testResults>
"""
    partial = _execution(run_directory, partial_jtl)
    missing = _execution(
        run_directory,
        None,
        name="启动失败脚本",
        artifact_name="02-启动失败脚本",
        status=ExecutionStatus.ERROR,
        exit_code=7,
        error="JMeter exited with exit code 7",
    )

    result = generate_suite_report(_suite(run_directory, partial, missing))
    report = result.report_path.read_text(encoding="utf-8")

    assert list(run_directory.rglob("*.html")) == [result.report_path]
    assert "JTL 解析不完整" in report
    assert "以下仅展示已经完整结束的请求" in report
    assert "完整样本" in report
    assert "partial response remains visible" in report
    assert "进程未正常完成" in report
    assert "JMeter exited with exit code 7" in report


def test_large_response_is_complete_escaped_and_report_is_self_contained(
    tmp_path: Path,
):
    from jmeter_suite.report import generate_suite_report

    run_directory = tmp_path / "large-response"
    response_data = (
        "BEGIN-LARGE-RESPONSE\n"
        + ("0123456789汉字<&>\"'\n" * 70_000)
        + '</pre><script>alert("response")</script>\n'
        + "END-LARGE-RESPONSE"
    )
    jtl = _jtl(
        _sample(
            label="大响应接口",
            query="q=&lt;script&gt;alert(1)&lt;/script&gt;",
            response=f"<![CDATA[{response_data}]]>",
        )
    )
    execution = _execution(run_directory, jtl)

    result = generate_suite_report(_suite(run_directory, execution))
    report = result.report_path.read_text(encoding="utf-8")

    response_match = re.search(
        r'<pre[^>]+data-raw-response[^>]*>(.*?)</pre>',
        report,
        flags=re.DOTALL,
    )
    assert response_match is not None
    assert html.unescape(response_match.group(1)) == response_data
    assert "BEGIN-LARGE-RESPONSE" in report
    assert "END-LARGE-RESPONSE" in report
    assert "<script>alert" not in report
    assert "&lt;/pre&gt;&lt;script&gt;alert" in report
    packaged_javascript = (
        Path(__file__).parents[1]
        / "src"
        / "jmeter_suite"
        / "static"
        / "report.js"
    ).read_text(encoding="utf-8")
    assert "fetch(" not in packaged_javascript
    assert f"<script>{packaged_javascript}</script>" in report
    assert re.search(r'\son[a-z]+\s*=', report, flags=re.IGNORECASE) is None
    links = re.findall(r'(?:href|src)="([^"]+)"', report)
    assert links == []
    assert {
        path.relative_to(result.report_path.parent).as_posix()
        for path in result.report_path.parent.rglob("*")
        if path.is_file()
    } == {"report.html"}


def test_report_javascript_supports_filter_copy_and_all_details_toggle():
    package_root = Path(__file__).parents[1] / "src" / "jmeter_suite"
    report_js = (package_root / "static" / "report.js").read_text(
        encoding="utf-8"
    )
    report_css = (package_root / "static" / "report.css").read_text(
        encoding="utf-8"
    )

    assert "data-detail-toggle" in report_js
    assert "data-all-details-toggle" in report_js
    assert "data-all-details-label" in report_js
    assert "setAllDetailsExpanded" in report_js
    assert "aria-expanded" in report_js
    assert '"展开全部"' in report_js
    assert '"收起全部"' in report_js
    assert '"展开"' in report_js
    assert '"收起"' in report_js
    assert "data-sample-item" in report_js
    assert "data-visible-count" in report_js
    assert "activePeriodFilter" in report_js
    assert "data-period-filter" in report_js
    assert "queryDataRange" in report_js
    assert "periodAlwaysVisible" in report_js
    assert "applyOverviewFilters" in report_js
    assert "syncPeriodFilters" in report_js
    assert 'event.target.matches("[data-period-filter]")' in report_js
    assert 'item.dataset.periodAlwaysVisible === "true"' in report_js
    assert "navigator.clipboard" in report_js
    assert 'addEventListener("beforeprint"' in report_js
    assert 'addEventListener("afterprint"' in report_js
    assert "fetch(" not in report_js
    assert "innerHTML" not in report_js
    assert ".sample-card" in report_css
    assert ".detail-toggle" in report_css
    assert ".all-details-toggle" in report_css
    assert ".period-filter" in report_css
    assert "position: sticky" in report_css
    assert "@media print" in report_css


def test_report_assets_batch_bulk_details_and_defer_offscreen_rendering():
    package_root = Path(__file__).parents[1] / "src" / "jmeter_suite"
    report_js = (package_root / "static" / "report.js").read_text(
        encoding="utf-8"
    )
    report_css = (package_root / "static" / "report.css").read_text(
        encoding="utf-8"
    )

    assert "BULK_DETAIL_BATCH_SIZE = 20" in report_js
    assert "requestAnimationFrame" in report_js
    assert 'setAttribute("aria-busy", "true")' in report_js
    assert 'removeAttribute("aria-busy")' in report_js
    assert "content-visibility: auto" in report_css
    assert "contain-intrinsic-size: auto 720px" in report_css

    print_styles = report_css.split("@media print", maxsplit=1)[1]
    assert "content-visibility: visible" in print_styles
    assert "contain-intrinsic-size: none" in print_styles
