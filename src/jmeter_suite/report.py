"""将 JTL 执行结果生成内嵌资源的单页 HTML 报告和精简清单。"""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import gzip
import hashlib
from importlib import resources
import json
import os
from pathlib import Path
import shutil
import tempfile
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from jinja2 import DictLoader, Environment, StrictUndefined, select_autoescape
from markupsafe import Markup

from .jtl import parse_jtl
from .models import (
    ExecutionStatus,
    JTLParseResult,
    JMeterExecutionResult,
    ReportStatus,
    SampleRecord,
    ScriptReportResult,
    SuiteHtmlReportResult,
    SuiteProcessResult,
    SuiteReportResult,
)
from .request_period import QueryDataRange, analyze_query_data_range
from .report_filter import is_url_excluded


_TEMPLATE_NAMES = (
    "base.html",
    "suite.html",
    "script.html",
    "endpoint_group.html",
    "sample.html",
)
_SUITE_DETAILS_MARKER = "<!-- REPORT_SCRIPT_DETAILS -->"
_SCRIPT_SAMPLES_MARKER = "<!-- REPORT_SAMPLE_CARDS -->"
_ENDPOINT_SAMPLES_MARKER = "<!-- REPORT_ENDPOINT_SAMPLE_CARDS -->"
_REPORT_SAMPLE_TYPE = "httpSample"
_BEIJING_TIMEZONE = ZoneInfo("Asia/Shanghai")
_REPORT_FILENAME = "report.html"
_REPORT_DIRECTORY_SUFFIX = "_Inspection_Report"
_MAX_REPORT_DIRECTORIES_PER_SECOND = 100
_STATUS_LABELS = {
    ReportStatus.PASSED.value: "通过",
    ReportStatus.FAILED.value: "失败",
    ReportStatus.ERROR.value: "执行错误",
    ReportStatus.TIMEOUT.value: "超时",
    ReportStatus.CANCELLED.value: "已取消",
    ExecutionStatus.COMPLETED.value: "已完成",
}


@dataclass(frozen=True, slots=True)
class _SampleFragment:
    sample_id: str
    target_id: str
    sequence: int
    passed: bool
    label: str
    url: str
    endpoint_key: str
    endpoint_order: int
    query_data_range: QueryDataRange
    path: Path


@dataclass(frozen=True, slots=True)
class _FailedSampleOverview:
    target_id: str
    sequence: int
    label: str
    url: str
    endpoint_key: str
    endpoint_order: int
    timestamp_ms: int | None
    response_code: str
    elapsed_ms: int | None
    query_data_range: QueryDataRange


@dataclass(frozen=True, slots=True)
class _EndpointRangeSummary:
    text: str
    total: int
    passed: int
    failed: int


@dataclass(frozen=True, slots=True)
class _EndpointGroup:
    group_id: str
    endpoint_key: str
    label: str
    url: str
    fragments: tuple[_SampleFragment, ...]
    ranges: tuple[_EndpointRangeSummary, ...]
    passed: int
    failed: int
    expanded: bool


@dataclass(frozen=True, slots=True)
class _FailureRangeLink:
    text: str
    count: int
    target_id: str
    always_visible: bool


@dataclass(frozen=True, slots=True)
class _FailedEndpointOverview:
    endpoint_key: str
    label: str
    url: str
    failure_count: int
    ranges: tuple[_FailureRangeLink, ...]
    timestamp_ms: int | None


@dataclass(frozen=True, slots=True)
class _RenderedScript:
    execution: JMeterExecutionResult
    artifact_name: str
    result: ScriptReportResult
    fragments: tuple[_SampleFragment, ...]
    endpoint_groups: tuple[_EndpointGroup, ...]
    failed_samples: tuple[_FailedSampleOverview, ...]


@dataclass(frozen=True, slots=True)
class _ReportParseOutcome:
    result: JTLParseResult
    filtered_samples: int


@dataclass(frozen=True, slots=True)
class _InlineAssets:
    css: Markup
    javascript: Markup
    style_csp_hash: str
    script_csp_hash: str


def _template_environment() -> Environment:
    template_root = resources.files("jmeter_suite").joinpath("templates")
    source = {
        name: template_root.joinpath(name).read_text(encoding="utf-8")
        for name in _TEMPLATE_NAMES
    }
    environment = Environment(
        loader=DictLoader(source),
        autoescape=select_autoescape(enabled_extensions=("html",)),
        undefined=StrictUndefined,
    )
    environment.filters.update(
        status_label=status_label,
        beijing_time=_format_beijing,
        duration=_format_duration,
        sample_time=_format_sample_time,
    )
    return environment


def _write_page(
    environment: Environment,
    template_name: str,
    destination: Path,
    **context: object,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        environment.get_template(template_name).render(**context),
        encoding="utf-8",
    )


def _csp_hash(source: str) -> str:
    digest = hashlib.sha256(source.encode("utf-8")).digest()
    encoded = base64.b64encode(digest).decode("ascii")
    return f"sha256-{encoded}"


def _load_inline_assets() -> _InlineAssets:
    source_root = resources.files("jmeter_suite").joinpath("static")
    css = source_root.joinpath("report.css").read_text(encoding="utf-8")
    javascript = source_root.joinpath("report.js").read_text(
        encoding="utf-8"
    )
    if "</style" in css.casefold():
        raise RuntimeError("report.css contains an unsafe closing style tag")
    if "</script" in javascript.casefold():
        raise RuntimeError("report.js contains an unsafe closing script tag")
    return _InlineAssets(
        css=Markup(css),
        javascript=Markup(javascript),
        style_csp_hash=_csp_hash(css),
        script_csp_hash=_csp_hash(javascript),
    )


def _report_generated_at() -> datetime:
    return datetime.now(_BEIJING_TIMEZONE)


def _create_report_directory(
    parent_directory: Path,
    generated_at: datetime,
) -> Path:
    if generated_at.tzinfo is None or generated_at.utcoffset() is None:
        raise ValueError("report generation time must be timezone-aware")
    parent_directory.mkdir(parents=True, exist_ok=True)
    timestamp = generated_at.astimezone(_BEIJING_TIMEZONE).strftime(
        "%Y-%m-%d_%H-%M-%S"
    )
    base_name = f"{timestamp}{_REPORT_DIRECTORY_SUFFIX}"
    for attempt in range(1, _MAX_REPORT_DIRECTORIES_PER_SECOND + 1):
        directory_name = (
            base_name if attempt == 1 else f"{base_name}-{attempt:02d}"
        )
        candidate = parent_directory / directory_name
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise RuntimeError(
        "unable to allocate a unique report directory for generation time "
        f"{timestamp}"
    )


def _iso_beijing(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("report timestamps must be timezone-aware")
    return value.astimezone(_BEIJING_TIMEZONE).isoformat()


def status_label(status: object) -> str:
    value = getattr(status, "value", status)
    return _STATUS_LABELS.get(str(value), str(value))


def _format_beijing(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("report timestamps must be timezone-aware")
    return value.astimezone(_BEIJING_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")


def _format_sample_time(timestamp_ms: int | None) -> str:
    if timestamp_ms is None:
        return "未记录"
    try:
        sampled_at = datetime.fromtimestamp(
            timestamp_ms / 1000,
            tz=timezone.utc,
        ).astimezone(_BEIJING_TIMEZONE)
    except (OverflowError, OSError, ValueError):
        return "无法解析"
    return sampled_at.strftime("%Y-%m-%d %H:%M:%S")


def _format_failure_rate(failed: int, total: int) -> str:
    if total <= 0:
        return "0%"
    rendered = f"{failed * 100 / total:.1f}".rstrip("0").rstrip(".")
    return f"{rendered}%"


def _format_duration(seconds: float) -> str:
    value = max(0.0, seconds)
    if value < 60:
        rendered = f"{value:.3f}".rstrip("0").rstrip(".")
        return f"{rendered} 秒"
    whole_seconds = int(round(value))
    hours, remainder = divmod(whole_seconds, 3600)
    minutes, seconds_part = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours} 小时")
    if minutes:
        parts.append(f"{minutes} 分")
    if seconds_part or not parts:
        parts.append(f"{seconds_part} 秒")
    return " ".join(parts)


def _relative(path: Path, run_directory: Path) -> str:
    return path.relative_to(run_directory).as_posix()


def _script_status(
    execution: JMeterExecutionResult,
    *,
    parse_passed: bool,
    parse_complete: bool,
    leaf_samples: int,
) -> ReportStatus:
    if execution.status is ExecutionStatus.TIMEOUT:
        return ReportStatus.TIMEOUT
    if execution.status is ExecutionStatus.CANCELLED:
        return ReportStatus.CANCELLED
    if execution.status is ExecutionStatus.ERROR:
        return ReportStatus.ERROR
    if not parse_complete:
        return ReportStatus.ERROR
    if leaf_samples == 0:
        return ReportStatus.ERROR
    return ReportStatus.PASSED if parse_passed else ReportStatus.FAILED


def _script_error(
    execution: JMeterExecutionResult,
    parse_result: JTLParseResult,
    status: ReportStatus,
    *,
    filtered_samples: int,
) -> str | None:
    if parse_result.error is not None:
        return parse_result.error
    if (
        execution.status is ExecutionStatus.COMPLETED
        and parse_result.leaf_samples == 0
    ):
        if parse_result.total_samples == 0 and filtered_samples > 0:
            return "URL 关键字过滤后没有可报告的 HTTP 接口"
        return "JTL 中没有可报告的 HTTP 接口样本"
    if status is ReportStatus.PASSED:
        return None
    if execution.error is not None:
        return execution.error
    if status is ReportStatus.FAILED:
        return "存在失败样本或断言"
    return None


def _unavailable_parse_result(path: Path, error: str) -> JTLParseResult:
    return JTLParseResult(
        source_path=path,
        summaries=(),
        complete=False,
        error=error,
        total_samples=0,
        leaf_samples=0,
        passed_leaf_samples=0,
        failed_leaf_samples=0,
        assertion_failures=0,
    )


def _parse_report_jtl(
    path: Path,
    on_sample: Callable[[SampleRecord], None],
    excluded_url_keywords: tuple[str, ...],
) -> _ReportParseOutcome:
    try:
        if not path.is_file():
            return _ReportParseOutcome(
                _unavailable_parse_result(path, "JTL 文件不存在"),
                0,
            )
        if path.stat().st_size == 0:
            return _ReportParseOutcome(
                _unavailable_parse_result(path, "JTL 文件为空"),
                0,
            )
    except OSError as error:
        return _ReportParseOutcome(
            _unavailable_parse_result(
                path,
                f"无法检查 JTL 文件：{error}",
            ),
            0,
        )

    assertion_failures = 0
    filtered_samples = 0
    included_sample_ids: set[str] = set()

    def handle_sample(sample: SampleRecord) -> None:
        nonlocal assertion_failures, filtered_samples
        if sample.sample_type != _REPORT_SAMPLE_TYPE:
            return
        if is_url_excluded(sample.url, excluded_url_keywords):
            filtered_samples += 1
            return
        included_sample_ids.add(sample.sample_id)
        assertion_failures += sum(
            not assertion.passed for assertion in sample.assertions
        )
        on_sample(sample)

    parsed = parse_jtl(path, on_sample=handle_sample)
    summaries = tuple(
        summary
        for summary in parsed.summaries
        if summary.sample_id in included_sample_ids
    )
    leaf_summaries = tuple(
        summary for summary in summaries if summary.is_leaf
    )
    passed_leaf_samples = sum(
        summary.passed for summary in leaf_summaries
    )
    return _ReportParseOutcome(
        JTLParseResult(
            source_path=parsed.source_path,
            summaries=summaries,
            complete=parsed.complete,
            error=parsed.error,
            total_samples=len(summaries),
            leaf_samples=len(leaf_summaries),
            passed_leaf_samples=passed_leaf_samples,
            failed_leaf_samples=len(leaf_summaries) - passed_leaf_samples,
            assertion_failures=assertion_failures,
        ),
        filtered_samples,
    )


def _request_parameters(sample: SampleRecord) -> str:
    if sample.query_string.strip():
        return sample.query_string

    if sample.url:
        try:
            url_query = urlsplit(sample.url).query
        except ValueError:
            url_query = sample.url.partition("?")[2].partition("#")[0]
        if url_query:
            return url_query
        if sample.sampler_data.strip():
            return sample.sampler_data

    return ""


def _endpoint_key(url: str, *, missing_key: str) -> str:
    value = url.strip()
    if not value:
        return missing_key
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value.partition("?")[0].partition("#")[0] or missing_key
    if parsed.scheme or parsed.netloc:
        return parsed._replace(
            scheme=parsed.scheme.lower(),
            netloc=parsed.netloc.lower(),
            query="",
            fragment="",
        ).geturl()
    return parsed.path or missing_key


def _compressed_sample_payload(
    request_parameters: str,
    response_data: str,
) -> str:
    payload = json.dumps(
        {
            "request": request_parameters,
            "response": response_data,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    compressed = gzip.compress(payload, compresslevel=9, mtime=0)
    return base64.b64encode(compressed).decode("ascii")


def _endpoint_display_url(
    endpoint_key: str,
    samples: tuple[_SampleFragment, ...],
) -> str:
    if not endpoint_key.startswith("\0"):
        return endpoint_key
    return next(
        (sample.url.strip() for sample in samples if sample.url.strip()),
        "未记录 URL",
    )


def _build_endpoint_groups(
    fragments: tuple[_SampleFragment, ...],
    script_index: int,
) -> tuple[_EndpointGroup, ...]:
    grouped: dict[str, list[_SampleFragment]] = {}
    for fragment in fragments:
        grouped.setdefault(fragment.endpoint_key, []).append(fragment)

    groups: list[_EndpointGroup] = []
    for group_index, (endpoint_key, group_fragments) in enumerate(
        grouped.items(),
        start=1,
    ):
        samples = tuple(group_fragments)
        source_order_samples = tuple(
            sorted(samples, key=lambda sample: sample.sequence)
        )
        range_samples: dict[str, list[_SampleFragment]] = {}
        for sample in source_order_samples:
            range_samples.setdefault(
                sample.query_data_range.text,
                [],
            ).append(sample)
        ranges = tuple(
            _EndpointRangeSummary(
                text=text,
                total=len(items),
                passed=sum(item.passed for item in items),
                failed=sum(not item.passed for item in items),
            )
            for text, items in range_samples.items()
        )
        passed = sum(sample.passed for sample in samples)
        failed = len(samples) - passed
        first_sample = source_order_samples[0]
        groups.append(
            _EndpointGroup(
                group_id=(
                    f"script-{script_index:02d}-endpoint-{group_index:04d}"
                ),
                endpoint_key=endpoint_key,
                label=first_sample.label or "未命名请求",
                url=_endpoint_display_url(endpoint_key, samples),
                fragments=samples,
                ranges=ranges,
                passed=passed,
                failed=failed,
                expanded=failed > 0,
            )
        )
    return tuple(groups)


def _build_failed_endpoint_overviews(
    rendered_scripts: tuple[_RenderedScript, ...],
) -> tuple[_FailedEndpointOverview, ...]:
    grouped: dict[str, list[_FailedSampleOverview]] = {}
    for rendered in rendered_scripts:
        for sample in rendered.failed_samples:
            grouped.setdefault(sample.endpoint_key, []).append(sample)

    overviews: list[_FailedEndpointOverview] = []
    for endpoint_key, samples in grouped.items():
        range_samples: dict[str, list[_FailedSampleOverview]] = {}
        for sample in samples:
            range_samples.setdefault(
                sample.query_data_range.text,
                [],
            ).append(sample)
        ranges = tuple(
            _FailureRangeLink(
                text=text,
                count=len(items),
                target_id=items[0].target_id,
                always_visible=any(
                    not item.query_data_range.has_time_condition
                    for item in items
                ),
            )
            for text, items in range_samples.items()
        )
        first_sample = samples[0]
        display_url = (
            endpoint_key
            if not endpoint_key.startswith("\0")
            else first_sample.url or "未记录 URL"
        )
        overviews.append(
            _FailedEndpointOverview(
                endpoint_key=endpoint_key,
                label=first_sample.label or "未命名请求",
                url=display_url,
                failure_count=len(samples),
                ranges=ranges,
                timestamp_ms=first_sample.timestamp_ms,
            )
        )
    return tuple(overviews)


def _collect_script_report(
    environment: Environment,
    run_directory: Path,
    report_path: Path,
    fragment_root: Path,
    execution: JMeterExecutionResult,
    script_index: int,
    excluded_url_keywords: tuple[str, ...],
    additional_date_parameter_names: tuple[str, ...],
) -> _RenderedScript:
    artifact_name = execution.jtl_path.parent.name
    fragment_directory = fragment_root / f"script-{script_index:02d}"
    fragments: list[_SampleFragment] = []
    failed_samples: list[_FailedSampleOverview] = []
    endpoint_orders: dict[str, int] = {}

    def render_sample(sample: SampleRecord) -> None:
        fragment_path = fragment_directory / f"{sample.sample_id}.html"
        dom_prefix = f"script-{script_index:02d}-{sample.sample_id}"
        request_parameters = _request_parameters(sample)
        query_data_range = analyze_query_data_range(
            request_parameters,
            additional_date_parameter_names=(
                additional_date_parameter_names
            ),
        )
        endpoint_key = _endpoint_key(
            sample.url,
            missing_key=f"\0{script_index}:{sample.sample_id}",
        )
        endpoint_order = endpoint_orders.setdefault(
            endpoint_key,
            len(endpoint_orders),
        )
        _write_page(
            environment,
            "sample.html",
            fragment_path,
            sample=sample,
            sample_status=(
                ReportStatus.PASSED if sample.passed else ReportStatus.FAILED
            ),
            request_parameters=request_parameters,
            query_data_range=query_data_range,
            expanded=not sample.passed,
            dom_prefix=dom_prefix,
            endpoint_group_id=(
                f"script-{script_index:02d}-endpoint-"
                f"{endpoint_order + 1:04d}"
            ),
            compressed_payload=(
                _compressed_sample_payload(
                    request_parameters,
                    sample.response_data,
                )
                if sample.passed
                else None
            ),
        )
        fragments.append(
            _SampleFragment(
                sample_id=sample.sample_id,
                target_id=f"{dom_prefix}-card",
                sequence=sample.sequence,
                passed=sample.passed,
                label=sample.label,
                url=sample.url,
                endpoint_key=endpoint_key,
                endpoint_order=endpoint_order,
                query_data_range=query_data_range,
                path=fragment_path,
            )
        )
        if not sample.passed:
            failed_samples.append(
                _FailedSampleOverview(
                    target_id=f"{dom_prefix}-card",
                    sequence=sample.sequence,
                    label=sample.label,
                    url=sample.url,
                    endpoint_key=endpoint_key,
                    endpoint_order=endpoint_order,
                    timestamp_ms=sample.timestamp_ms,
                    response_code=sample.response_code,
                    elapsed_ms=sample.elapsed_ms,
                    query_data_range=query_data_range,
                )
            )

    parse_outcome = _parse_report_jtl(
        execution.jtl_path,
        render_sample,
        excluded_url_keywords,
    )
    parse_result = parse_outcome.result
    status = _script_status(
        execution,
        parse_passed=parse_result.passed,
        parse_complete=parse_result.complete,
        leaf_samples=parse_result.leaf_samples,
    )
    error = _script_error(
        execution,
        parse_result,
        status,
        filtered_samples=parse_outcome.filtered_samples,
    )
    result = ScriptReportResult(
        script_name=execution.script_name,
        status=status,
        started_at=execution.started_at,
        finished_at=execution.finished_at,
        exit_code=execution.exit_code,
        error=error,
        jtl_path=execution.jtl_path,
        log_path=execution.log_path,
        report_path=report_path,
        parse_result=parse_result,
        sample_paths=(),
        process_tree_termination_confirmed=(
            execution.process_tree_termination_confirmed
        ),
    )
    sorted_fragments = tuple(
        sorted(
            fragments,
            key=lambda fragment: (
                fragment.endpoint_order,
                fragment.passed,
                fragment.sequence,
            ),
        )
    )
    return _RenderedScript(
        execution=execution,
        artifact_name=artifact_name,
        result=result,
        fragments=sorted_fragments,
        endpoint_groups=_build_endpoint_groups(
            sorted_fragments,
            script_index,
        ),
        failed_samples=tuple(
            sorted(
                failed_samples,
                key=lambda sample: (sample.endpoint_order, sample.sequence),
            )
        ),
    )


def _partition_template(rendered: str, marker: str, template_name: str) -> tuple[str, str]:
    before, found, after = rendered.partition(marker)
    if not found:
        raise RuntimeError(f"{template_name} is missing streaming marker {marker}")
    return before, after


def _write_streamed_suite_page(
    environment: Environment,
    destination: Path,
    suite_result: SuiteProcessResult,
    status: ReportStatus,
    rendered_scripts: tuple[_RenderedScript, ...],
    inline_assets: _InlineAssets,
) -> None:
    scripts = tuple(rendered.result for rendered in rendered_scripts)
    totals = _report_totals(scripts)
    failed_endpoints = _build_failed_endpoint_overviews(rendered_scripts)
    period_options = tuple(
        sorted(
            {
                fragment.query_data_range.text
                for rendered in rendered_scripts
                for fragment in rendered.fragments
                if fragment.query_data_range.has_time_condition
            },
            key=lambda value: (value.casefold(), value),
        )
    )
    suite_shell = environment.get_template("suite.html").render(
        title=f"{suite_result.run_id} - 自动化巡检",
        suite=suite_result,
        status=status,
        scripts=scripts,
        totals=totals,
        failed_endpoints=failed_endpoints,
        period_options=period_options,
        failure_rate=_format_failure_rate(
            totals["failed_samples"],
            totals["leaf_samples"],
        ),
        detail_count=sum(
            script.parse_result.total_samples for script in scripts
        ),
        assertion_failures=sum(
            script.parse_result.assertion_failures for script in scripts
        ),
        report_css=inline_assets.css,
        report_js=inline_assets.javascript,
        style_csp_hash=inline_assets.style_csp_hash,
        script_csp_hash=inline_assets.script_csp_hash,
    )
    suite_before, suite_after = _partition_template(
        suite_shell,
        _SUITE_DETAILS_MARKER,
        "suite.html",
    )

    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as output:
        output.write(suite_before.encode("utf-8"))
        for script_index, rendered in enumerate(rendered_scripts, start=1):
            script_shell = environment.get_template("script.html").render(
                script_index=script_index,
                execution=rendered.execution,
                artifact_name=rendered.artifact_name,
                script=rendered.result,
            )
            script_before, script_after = _partition_template(
                script_shell,
                _SCRIPT_SAMPLES_MARKER,
                "script.html",
            )
            output.write(script_before.encode("utf-8"))
            for endpoint_group in rendered.endpoint_groups:
                endpoint_shell = environment.get_template(
                    "endpoint_group.html"
                ).render(group=endpoint_group)
                endpoint_before, endpoint_after = _partition_template(
                    endpoint_shell,
                    _ENDPOINT_SAMPLES_MARKER,
                    "endpoint_group.html",
                )
                output.write(endpoint_before.encode("utf-8"))
                for fragment in endpoint_group.fragments:
                    with fragment.path.open("rb") as source:
                        shutil.copyfileobj(source, output)
                    output.write(b"\n")
                output.write(endpoint_after.encode("utf-8"))
            output.write(script_after.encode("utf-8"))
        output.write(suite_after.encode("utf-8"))


def _manifest(
    suite_result: SuiteProcessResult,
    status: ReportStatus,
    script_results: tuple[ScriptReportResult, ...],
    report_path: Path,
) -> dict[str, object]:
    run_directory = suite_result.run_directory
    totals = _report_totals(script_results)
    scripts = [
        {
            "name": result.script_name,
            "status": result.status.value,
            "started_at": _iso_beijing(result.started_at),
            "finished_at": _iso_beijing(result.finished_at),
            "duration_seconds": result.duration_seconds,
            "exit_code": result.exit_code,
            "error": result.error,
            "total_samples": result.parse_result.total_samples,
            "leaf_samples": result.parse_result.leaf_samples,
            "passed_samples": result.parse_result.passed_leaf_samples,
            "failed_samples": result.parse_result.failed_leaf_samples,
            "assertion_failures": result.parse_result.assertion_failures,
            "parse_complete": result.parse_result.complete,
            "parse_error": result.parse_result.error,
            "paths": {
                "jtl": _relative(result.jtl_path, run_directory),
                "log": _relative(result.log_path, run_directory),
                "report": _relative(result.report_path, run_directory),
            },
        }
        for result in script_results
    ]
    return {
        "schema_version": 1,
        "run_id": suite_result.run_id,
        "status": status.value,
        "started_at": _iso_beijing(suite_result.started_at),
        "finished_at": _iso_beijing(suite_result.finished_at),
        "report_path": _relative(report_path, run_directory),
        "totals": totals,
        "scripts": scripts,
    }


def _report_totals(
    script_results: tuple[ScriptReportResult, ...],
) -> dict[str, int]:
    return {
        "scripts": len(script_results),
        "passed": sum(
            result.status is ReportStatus.PASSED for result in script_results
        ),
        "failed": sum(
            result.status is ReportStatus.FAILED for result in script_results
        ),
        "error": sum(
            result.status is ReportStatus.ERROR for result in script_results
        ),
        "timeout": sum(
            result.status is ReportStatus.TIMEOUT for result in script_results
        ),
        "cancelled": sum(
            result.status is ReportStatus.CANCELLED for result in script_results
        ),
        "leaf_samples": sum(
            result.parse_result.leaf_samples for result in script_results
        ),
        "passed_samples": sum(
            result.parse_result.passed_leaf_samples for result in script_results
        ),
        "failed_samples": sum(
            result.parse_result.failed_leaf_samples for result in script_results
        ),
    }


def _write_manifest(path: Path, payload: dict[str, object]) -> None:
    temporary_path = path.with_name(f"{path.name}.tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary_path, path)


def generate_suite_html_report(
    suite_result: SuiteProcessResult,
    *,
    excluded_url_keywords: tuple[str, ...] = (),
    additional_date_parameter_names: tuple[str, ...] = (),
) -> SuiteHtmlReportResult:
    run_directory = suite_result.run_directory
    run_directory.mkdir(parents=True, exist_ok=True)
    environment = _template_environment()
    inline_assets = _load_inline_assets()
    report_directory = _create_report_directory(
        run_directory,
        _report_generated_at(),
    )
    report_path = report_directory / _REPORT_FILENAME

    try:
        with tempfile.TemporaryDirectory(
            prefix=".report-fragments-",
            dir=run_directory,
        ) as temporary_directory:
            fragment_root = Path(temporary_directory)
            rendered_scripts = tuple(
                _collect_script_report(
                    environment,
                    run_directory,
                    report_path,
                    fragment_root,
                    execution,
                    script_index,
                    excluded_url_keywords,
                    additional_date_parameter_names,
                )
                for script_index, execution in enumerate(
                    suite_result.executions,
                    start=1,
                )
            )
            scripts = tuple(
                rendered.result for rendered in rendered_scripts
            )
            status = (
                ReportStatus.PASSED
                if scripts
                and all(
                    script.status is ReportStatus.PASSED
                    for script in scripts
                )
                else ReportStatus.FAILED
            )
            _write_streamed_suite_page(
                environment,
                report_path,
                suite_result,
                status,
                rendered_scripts,
                inline_assets,
            )
    except Exception:
        shutil.rmtree(report_directory, ignore_errors=True)
        raise

    return SuiteHtmlReportResult(
        run_id=suite_result.run_id,
        status=status,
        started_at=suite_result.started_at,
        finished_at=suite_result.finished_at,
        run_directory=run_directory,
        report_path=report_path,
        scripts=scripts,
    )


def generate_suite_report(
    suite_result: SuiteProcessResult,
    *,
    excluded_url_keywords: tuple[str, ...] = (),
    additional_date_parameter_names: tuple[str, ...] = (),
) -> SuiteReportResult:
    html_result = generate_suite_html_report(
        suite_result,
        excluded_url_keywords=excluded_url_keywords,
        additional_date_parameter_names=additional_date_parameter_names,
    )
    manifest_path = suite_result.run_directory / "manifest.json"
    _write_manifest(
        manifest_path,
        _manifest(
            suite_result,
            html_result.status,
            html_result.scripts,
            html_result.report_path,
        ),
    )
    return SuiteReportResult(
        run_id=html_result.run_id,
        status=html_result.status,
        started_at=html_result.started_at,
        finished_at=html_result.finished_at,
        run_directory=html_result.run_directory,
        report_path=html_result.report_path,
        manifest_path=manifest_path,
        scripts=html_result.scripts,
    )
