"""验证从历史运行记录手动重建单文件报告的选择、过滤和发布行为。"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import gzip
import json
from pathlib import Path
import re

import pytest


STARTED_AT = datetime(2026, 8, 4, 0, 0, tzinfo=timezone.utc)
FINISHED_AT = datetime(2026, 8, 4, 0, 1, tzinfo=timezone.utc)
REPORT_GENERATED_AT = datetime(
    2026,
    8,
    4,
    7,
    0,
    1,
    tzinfo=timezone(timedelta(hours=8)),
)
REPORT_DIRECTORY_NAME = "2026-08-04_07-00-01_Inspection_Report"


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


@pytest.fixture(autouse=True)
def _fixed_report_generation_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import jmeter_suite.report as report_module

    monkeypatch.setattr(
        report_module,
        "_report_generated_at",
        lambda: REPORT_GENERATED_AT,
    )


def _project(
    tmp_path: Path,
    *,
    report_config: str = "",
) -> Path:
    project_root = tmp_path / "project"
    config_directory = project_root / "config"
    config_directory.mkdir(parents=True)
    (config_directory / "app.toml").write_text(
        '[output]\ndirectory = "../runs"\n' + report_config,
        encoding="utf-8",
    )
    return project_root


def _jtl(label: str, *, success: bool = False) -> str:
    return f"""\
<testResults>
  <httpSample lb="{label}" s="{str(success).lower()}" rc="{'200' if success else '500'}" ts="1785801600000" t="25">
    <java.net.URL>https://example.test/data</java.net.URL>
    <queryString>date_start=2026-06-01&amp;date_end=2026-06-30</queryString>
    <responseData>{{"code":500}}</responseData>
  </httpSample>
</testResults>
"""


def _write_run(
    project_root: Path,
    run_id: str,
    *,
    started_at: datetime = STARTED_AT,
    finished_at: datetime = FINISHED_AT,
    status: str = "failed",
    labels: tuple[str, ...] = ("月份接口",),
) -> Path:
    run_directory = project_root / "runs" / run_id
    scripts: list[dict[str, object]] = []
    for index, label in enumerate(labels, start=1):
        artifact_name = f"{index:02d}-script-{index}"
        artifact_directory = run_directory / "artifacts" / artifact_name
        artifact_directory.mkdir(parents=True, exist_ok=True)
        jtl_path = artifact_directory / "result.jtl"
        jtl_path.write_text(
            _jtl(label, success=status == "passed"),
            encoding="utf-8",
        )
        (artifact_directory / "jmeter.log").write_text(
            "source log",
            encoding="utf-8",
        )
        scripts.append(
            {
                "name": f"script-{index}",
                "status": status,
                "started_at": started_at.isoformat().replace("+00:00", "Z"),
                "finished_at": finished_at.isoformat().replace("+00:00", "Z"),
                "exit_code": 0,
                "error": "存在失败样本或断言" if status == "failed" else None,
                "paths": {
                    "jtl": f"artifacts/{artifact_name}/result.jtl",
                    "log": f"artifacts/{artifact_name}/jmeter.log",
                    "report": f"{REPORT_DIRECTORY_NAME}/report.html",
                },
            }
        )
    manifest = {
        "schema_version": 1,
        "run_id": run_id,
        "status": status,
        "started_at": started_at.isoformat().replace("+00:00", "Z"),
        "finished_at": finished_at.isoformat().replace("+00:00", "Z"),
        "report_path": f"{REPORT_DIRECTORY_NAME}/report.html",
        "scripts": scripts,
    }
    (run_directory / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )
    return run_directory


def _files(directory: Path) -> set[str]:
    return {
        path.relative_to(directory).as_posix()
        for path in directory.rglob("*")
        if path.is_file()
    }


def test_manual_sources_accept_beijing_offset_manifest_timestamps(
    tmp_path: Path,
):
    from jmeter_suite.manual_report import discover_manual_sources

    project_root = _project(tmp_path)
    beijing = timezone(timedelta(hours=8))
    _write_run(
        project_root,
        "2026-08-04_08-00",
        started_at=datetime(2026, 8, 4, 8, 0, tzinfo=beijing),
        finished_at=datetime(2026, 8, 4, 8, 1, tzinfo=beijing),
    )

    sources = discover_manual_sources(project_root)

    assert len(sources) == 1
    source = sources[0]
    assert source.started_at == STARTED_AT
    assert source.finished_at == FINISHED_AT
    assert source.executions[0].started_at == STARTED_AT
    assert source.executions[0].finished_at == FINISHED_AT


def test_manual_application_defaults_to_latest_and_outputs_single_html(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    from jmeter_suite.manual_report import run_manual_report_application

    project_root = _project(tmp_path)
    _write_run(
        project_root,
        "2026-08-03_08-00",
        started_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
        labels=("旧接口",),
    )
    source = _write_run(
        project_root,
        "2026-08-04_08-00",
        labels=("第一个接口", "第二个接口"),
    )

    code = run_manual_report_application(
        project_root,
        input_reader=lambda prompt: "",
    )

    assert code == 0
    output_directory = (
        project_root / "manual_reports" / REPORT_DIRECTORY_NAME
    )
    assert _files(output_directory) == {"report.html"}
    report = (output_directory / "report.html").read_text(encoding="utf-8")
    assert report.index("第一个接口") < report.index("第二个接口")
    assert "查询数据范围：2026-06" in report
    assert "<style>" in report
    assert "<script>" in report
    assert 'href="assets/report.css"' not in report
    assert 'src="assets/report.js"' not in report
    assert not (output_directory / "manifest.json").exists()
    assert not (output_directory / "artifacts").exists()
    assert (source / "artifacts" / "01-script-1" / "result.jtl").is_file()
    captured = capsys.readouterr()
    assert "2026-08-04_08-00" in captured.out
    assert "手动报告:" in captured.out
    assert str(output_directory / "report.html") in captured.out
    assert captured.err == ""


def test_manual_application_retries_invalid_choice_and_selects_older_run(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    from jmeter_suite.manual_report import run_manual_report_application

    project_root = _project(tmp_path)
    older = _write_run(
        project_root,
        "2026-08-03_08-00",
        started_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
        labels=("用户选择的旧接口",),
    )
    _write_run(project_root, "2026-08-04_08-00", labels=("默认新接口",))
    answers = iter(("abc", "9", "2"))

    code = run_manual_report_application(
        project_root,
        input_reader=lambda prompt: next(answers),
    )

    assert code == 0
    report_path = (
        project_root
        / "manual_reports"
        / REPORT_DIRECTORY_NAME
        / "report.html"
    )
    report = report_path.read_text(encoding="utf-8")
    assert "用户选择的旧接口" in report
    assert "默认新接口" not in report
    captured = capsys.readouterr()
    assert captured.out.index("2026-08-04_08-00") < captured.out.index(
        "2026-08-03_08-00"
    )
    assert captured.out.count("请输入有效序号") == 2


def test_manual_application_preserves_existing_report_and_uses_collision_suffix(
    tmp_path: Path,
):
    from jmeter_suite.manual_report import run_manual_report_application

    project_root = _project(tmp_path)
    _write_run(project_root, "2026-08-04_08-00")
    output_directory = (
        project_root / "manual_reports" / REPORT_DIRECTORY_NAME
    )
    output_directory.mkdir(parents=True)
    existing = output_directory / "report.html"
    existing.write_text("old report", encoding="utf-8")

    code = run_manual_report_application(
        project_root,
        input_reader=lambda prompt: "",
    )

    assert code == 0
    assert existing.read_text(encoding="utf-8") == "old report"
    new_directory = (
        project_root
        / "manual_reports"
        / f"{REPORT_DIRECTORY_NAME}-02"
    )
    assert _files(new_directory) == {"report.html"}
    assert "月份接口" in (new_directory / "report.html").read_text(
        encoding="utf-8"
    )


def test_manual_application_preserves_existing_report_when_rendering_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    import jmeter_suite.manual_report as manual_report

    project_root = _project(tmp_path)
    _write_run(project_root, "2026-08-04_08-00")
    output_directory = (
        project_root / "manual_reports" / REPORT_DIRECTORY_NAME
    )
    output_directory.mkdir(parents=True)
    existing = output_directory / "report.html"
    existing.write_text("keep this report", encoding="utf-8")

    def fail_rendering(*args: object, **kwargs: object) -> object:
        raise RuntimeError("render failed")

    monkeypatch.setattr(
        manual_report,
        "generate_suite_html_report",
        fail_rendering,
    )

    code = manual_report.run_manual_report_application(
        project_root,
        input_reader=lambda prompt: "",
    )

    assert code == 1
    assert existing.read_text(encoding="utf-8") == "keep this report"
    assert "render failed" in capsys.readouterr().err


def test_manual_application_returns_2_without_completed_runs(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    from jmeter_suite.manual_report import run_manual_report_application

    project_root = _project(tmp_path)
    incomplete = project_root / "runs" / "2026-08-04_08-00"
    incomplete.mkdir(parents=True)
    (incomplete / "result.jtl").write_text(_jtl("未完成"), encoding="utf-8")

    code = run_manual_report_application(project_root)

    assert code == 2
    assert "没有可用的已完成运行记录" in capsys.readouterr().err


def test_manual_application_returns_130_when_selection_is_interrupted(
    tmp_path: Path,
):
    from jmeter_suite.manual_report import run_manual_report_application

    project_root = _project(tmp_path)
    _write_run(project_root, "2026-08-04_08-00")

    def interrupt(prompt: str) -> str:
        raise KeyboardInterrupt

    assert run_manual_report_application(
        project_root,
        input_reader=interrupt,
    ) == 130


def test_manual_report_uses_url_filter_without_validating_or_starting_jmeter(
    tmp_path: Path,
):
    from jmeter_suite.manual_report import run_manual_report_application

    project_root = _project(
        tmp_path,
        report_config=(
            '\n[report]\n'
            'excluded_url_keywords = ["/internal/login"]\n'
        ),
    )
    source = _write_run(project_root, "2026-08-04_08-00")
    jtl_path = source / "artifacts" / "01-script-1" / "result.jtl"
    jtl_path.write_text(
        """\
<testResults>
  <httpSample lb="保留接口" s="true" rc="200" ts="1785801600000" t="25">
    <java.net.URL>https://example.test/orders?month=2026-06</java.net.URL>
    <responseData>kept-manual-response</responseData>
  </httpSample>
  <httpSample lb="过滤接口" s="false" rc="500" ts="1785801601000" t="25">
    <java.net.URL>https://example.test/INTERNAL/Login?token=secret</java.net.URL>
    <responseData>filtered-manual-response</responseData>
  </httpSample>
</testResults>
""",
        encoding="utf-8",
    )

    code = run_manual_report_application(
        project_root,
        input_reader=lambda prompt: "",
    )

    assert code == 0
    report = (
        project_root
        / "manual_reports"
        / REPORT_DIRECTORY_NAME
        / "report.html"
    ).read_text(encoding="utf-8")
    assert "保留接口" in report
    assert _decode_compressed_payloads(report) == []
    assert "month=2026-06" in report
    assert "kept-manual-response" in report
    assert "过滤接口" not in report
    assert "filtered-manual-response" not in report
    assert "本次巡检通过" in report


def test_manual_report_uses_configured_account_period_parameter(
    tmp_path: Path,
):
    from jmeter_suite.manual_report import run_manual_report_application

    project_root = _project(
        tmp_path,
        report_config=(
            "\n[report]\n"
            'additional_date_parameter_names = ["account_period"]\n'
        ),
    )
    source = _write_run(project_root, "2026-08-04_08-00")
    jtl_path = source / "artifacts" / "01-script-1" / "result.jtl"
    jtl_path.write_text(
        """\
<testResults>
  <httpSample lb="账期查询" s="true" rc="200" ts="1785801600000" t="25">
    <java.net.URL>https://example.test/accounts</java.net.URL>
    <queryString>account_period=2026-08&amp;page=1</queryString>
    <responseData>{"ok":true}</responseData>
  </httpSample>
</testResults>
""",
        encoding="utf-8",
    )

    code = run_manual_report_application(
        project_root,
        input_reader=lambda prompt: "",
    )

    assert code == 0
    report = (
        project_root
        / "manual_reports"
        / REPORT_DIRECTORY_NAME
        / "report.html"
    ).read_text(encoding="utf-8")
    assert "查询数据范围：2026-08" in report
    assert "未携带时间条件" not in report


def test_manual_report_rejects_invalid_url_filter_config(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    from jmeter_suite.manual_report import run_manual_report_application

    project_root = _project(
        tmp_path,
        report_config=(
            "\n[report]\n"
            'excluded_url_keywords = "/health"\n'
        ),
    )
    _write_run(project_root, "2026-08-04_08-00")

    code = run_manual_report_application(project_root)

    assert code == 2
    assert "report.excluded_url_keywords 必须是字符串数组" in (
        capsys.readouterr().err
    )


def test_manual_report_rejects_invalid_date_parameter_config(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    from jmeter_suite.manual_report import run_manual_report_application

    project_root = _project(
        tmp_path,
        report_config=(
            "\n[report]\n"
            'additional_date_parameter_names = "account_period"\n'
        ),
    )
    _write_run(project_root, "2026-08-04_08-00")

    code = run_manual_report_application(project_root)

    assert code == 2
    assert "report.additional_date_parameter_names 必须是字符串数组" in (
        capsys.readouterr().err
    )
