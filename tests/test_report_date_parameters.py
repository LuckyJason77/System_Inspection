"""验证补充日期参数名会作用于最终 HTML 报告。"""

from datetime import datetime, timezone
from pathlib import Path

from jmeter_suite.models import (
    ExecutionStatus,
    JMeterExecutionResult,
    SuiteProcessResult,
)
from jmeter_suite.report import generate_suite_html_report


def test_report_recognizes_configured_account_period(tmp_path: Path):
    run_directory = tmp_path / "run"
    artifact_directory = run_directory / "artifacts" / "01-inspection"
    artifact_directory.mkdir(parents=True)
    jtl_path = artifact_directory / "result.jtl"
    jtl_path.write_text(
        """<?xml version="1.0" encoding="UTF-8"?>
<testResults version="1.2">
  <httpSample lb="账期查询" s="true" ts="1785801600000" t="25"
              rc="200" rm="OK" dt="text" de="UTF-8">
    <java.net.URL>https://example.test/accounts</java.net.URL>
    <queryString>account_period=2026-08&amp;page=1</queryString>
    <responseData>{"ok":true}</responseData>
  </httpSample>
</testResults>
""",
        encoding="utf-8",
    )
    log_path = artifact_directory / "jmeter.log"
    log_path.write_text("done\n", encoding="utf-8")
    started_at = datetime(2026, 8, 4, tzinfo=timezone.utc)
    execution = JMeterExecutionResult(
        script_name="巡检",
        status=ExecutionStatus.COMPLETED,
        started_at=started_at,
        finished_at=started_at,
        exit_code=0,
        error=None,
        jtl_path=jtl_path,
        log_path=log_path,
        stdout="",
        stderr="",
    )
    suite = SuiteProcessResult(
        run_id="2026-08-04_08-00",
        run_directory=run_directory,
        started_at=started_at,
        finished_at=started_at,
        executions=(execution,),
    )

    result = generate_suite_html_report(
        suite,
        additional_date_parameter_names=("account_period",),
    )

    report_html = result.report_path.read_text(encoding="utf-8")
    assert "查询数据范围：2026-08" in report_html
    assert "未携带时间条件" not in report_html
