"""从已有运行清单和 JTL 安全重建单文件 HTML 巡检报告。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import tomllib

from .models import (
    ExecutionStatus,
    JMeterExecutionResult,
    ReportStatus,
    SuiteProcessResult,
)
from .report import generate_suite_html_report, status_label
from .report_filter import (
    ReportFilterConfigError,
    parse_excluded_url_keywords,
)
from .request_period import (
    DateParameterConfigError,
    parse_additional_date_parameter_names,
)


class ManualReportError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ManualSourceRun:
    run_id: str
    directory: Path
    status: ReportStatus
    started_at: datetime
    finished_at: datetime
    executions: tuple[JMeterExecutionResult, ...]

    def suite_for(self, output_directory: Path) -> SuiteProcessResult:
        return SuiteProcessResult(
            run_id=self.run_id,
            run_directory=output_directory,
            started_at=self.started_at,
            finished_at=self.finished_at,
            executions=self.executions,
        )


@dataclass(frozen=True, slots=True)
class ManualReportSettings:
    runs_root: Path
    excluded_url_keywords: tuple[str, ...]
    additional_date_parameter_names: tuple[str, ...]


def _datetime(value: object, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ManualReportError(f"{field} 缺失或格式错误")
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ManualReportError(f"{field} 格式错误") from error
    if parsed.tzinfo is None:
        raise ManualReportError(f"{field} 必须包含时区")
    return parsed.astimezone(timezone.utc)


def _report_status(value: object, field: str) -> ReportStatus:
    try:
        return ReportStatus(value)
    except (TypeError, ValueError) as error:
        raise ManualReportError(f"{field} 状态无效") from error


def _execution_status(status: ReportStatus) -> ExecutionStatus:
    if status in (ReportStatus.PASSED, ReportStatus.FAILED):
        return ExecutionStatus.COMPLETED
    return ExecutionStatus(status.value)


def _safe_source_path(
    run_directory: Path,
    value: object,
    field: str,
    *,
    required: bool,
) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ManualReportError(f"{field} 缺失或格式错误")
    relative = Path(value)
    if relative.is_absolute():
        raise ManualReportError(f"{field} 必须是运行目录内的相对路径")
    root = run_directory.resolve()
    path = (run_directory / relative).resolve(strict=False)
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ManualReportError(f"{field} 超出原运行目录") from error
    if required and (not path.is_file() or path.is_symlink()):
        raise ManualReportError(f"{field} 文件不存在或不是普通文件")
    return path


def _source_from_manifest(run_directory: Path) -> ManualSourceRun:
    manifest_path = run_directory / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ManualReportError("manifest.json 不存在或不是普通文件")
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ManualReportError("manifest.json 无法读取或内容损坏") from error
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ManualReportError("manifest.json schema_version 不是 1")
    run_id = payload.get("run_id")
    if run_id != run_directory.name:
        raise ManualReportError("manifest.json 的 run_id 与目录名不一致")

    raw_scripts = payload.get("scripts")
    if not isinstance(raw_scripts, list) or not raw_scripts:
        raise ManualReportError("manifest.json 没有脚本记录")
    executions: list[JMeterExecutionResult] = []
    for index, raw_script in enumerate(raw_scripts, start=1):
        if not isinstance(raw_script, dict):
            raise ManualReportError(f"scripts[{index}] 格式错误")
        name = raw_script.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ManualReportError(f"scripts[{index}].name 缺失")
        script_status = _report_status(
            raw_script.get("status"),
            f"scripts[{index}].status",
        )
        paths = raw_script.get("paths")
        if not isinstance(paths, dict):
            raise ManualReportError(f"scripts[{index}].paths 格式错误")
        jtl_path = _safe_source_path(
            run_directory,
            paths.get("jtl"),
            f"scripts[{index}].paths.jtl",
            required=True,
        )
        log_path = _safe_source_path(
            run_directory,
            paths.get("log"),
            f"scripts[{index}].paths.log",
            required=False,
        )
        exit_code = raw_script.get("exit_code")
        if exit_code is not None and not isinstance(exit_code, int):
            raise ManualReportError(f"scripts[{index}].exit_code 格式错误")
        error = raw_script.get("error")
        if error is not None and not isinstance(error, str):
            raise ManualReportError(f"scripts[{index}].error 格式错误")
        executions.append(
            JMeterExecutionResult(
                script_name=name,
                status=_execution_status(script_status),
                started_at=_datetime(
                    raw_script.get("started_at"),
                    f"scripts[{index}].started_at",
                ),
                finished_at=_datetime(
                    raw_script.get("finished_at"),
                    f"scripts[{index}].finished_at",
                ),
                exit_code=exit_code,
                error=error,
                jtl_path=jtl_path,
                log_path=log_path,
                stdout="",
                stderr="",
            )
        )

    return ManualSourceRun(
        run_id=run_id,
        directory=run_directory,
        status=_report_status(payload.get("status"), "status"),
        started_at=_datetime(payload.get("started_at"), "started_at"),
        finished_at=_datetime(payload.get("finished_at"), "finished_at"),
        executions=tuple(executions),
    )


def _configured_settings(project_root: Path) -> ManualReportSettings:
    config_path = project_root / "config" / "app.toml"
    try:
        with config_path.open("rb") as source:
            payload = tomllib.load(source)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ManualReportError(f"无法读取配置 {config_path}: {error}") from error
    output = payload.get("output")
    directory = output.get("directory") if isinstance(output, dict) else None
    if not isinstance(directory, str) or not directory.strip():
        raise ManualReportError("配置缺少 output.directory")
    configured = Path(directory)
    if not configured.is_absolute():
        configured = config_path.parent / configured

    report = payload.get("report")
    if report is None:
        report = {}
    if not isinstance(report, dict):
        raise ManualReportError("report 必须是配置块")
    unknown_keys = sorted(
        set(report)
        - {"excluded_url_keywords", "additional_date_parameter_names"}
    )
    if unknown_keys:
        raise ManualReportError(f"report 包含未知键: {unknown_keys[0]}")
    try:
        excluded_url_keywords = parse_excluded_url_keywords(
            report.get("excluded_url_keywords")
        )
    except ReportFilterConfigError as error:
        raise ManualReportError(str(error)) from error
    try:
        additional_date_parameter_names = (
            parse_additional_date_parameter_names(
                report.get("additional_date_parameter_names")
            )
        )
    except DateParameterConfigError as error:
        raise ManualReportError(str(error)) from error

    return ManualReportSettings(
        runs_root=configured.resolve(strict=False),
        excluded_url_keywords=excluded_url_keywords,
        additional_date_parameter_names=additional_date_parameter_names,
    )


def _discover_manual_sources(
    runs_root: Path,
) -> tuple[ManualSourceRun, ...]:
    if not runs_root.is_dir() or runs_root.is_symlink():
        return ()
    sources: list[ManualSourceRun] = []
    for candidate in runs_root.iterdir():
        if (
            candidate.name.startswith(".")
            or not candidate.is_dir()
            or candidate.is_symlink()
        ):
            continue
        try:
            sources.append(_source_from_manifest(candidate))
        except ManualReportError:
            continue
    return tuple(
        sorted(
            sources,
            key=lambda source: (source.started_at, source.run_id),
            reverse=True,
        )
    )


def discover_manual_sources(project_root: Path) -> tuple[ManualSourceRun, ...]:
    return _discover_manual_sources(
        _configured_settings(project_root).runs_root
    )


def _select_source(
    sources: tuple[ManualSourceRun, ...],
    input_reader: Callable[[str], str],
) -> ManualSourceRun:
    print("请选择已有运行记录：")
    for index, source in enumerate(sources, start=1):
        default = " [默认]" if index == 1 else ""
        print(
            f"{index}. {source.run_id}"
            f"（{status_label(source.status)}，{len(source.executions)} 个 JTL）"
            f"{default}"
        )
    while True:
        try:
            answer = input_reader("请输入序号，直接回车选择 1：").strip()
        except EOFError:
            answer = ""
        if not answer:
            return sources[0]
        try:
            selection = int(answer)
        except ValueError:
            selection = 0
        if 1 <= selection <= len(sources):
            return sources[selection - 1]
        print(f"请输入有效序号（1-{len(sources)}）")


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            with source.open("rb") as input_file:
                shutil.copyfileobj(input_file, temporary)
        os.replace(temporary_path, destination)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _publish_report(source_report: Path, destination_root: Path) -> Path:
    destination_root.mkdir(parents=True, exist_ok=True)
    base_name = source_report.parent.name
    destination: Path | None = None
    for attempt in range(1, 101):
        directory_name = (
            base_name if attempt == 1 else f"{base_name}-{attempt:02d}"
        )
        candidate = destination_root / directory_name
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        destination = candidate
        break
    if destination is None:
        raise RuntimeError(
            f"无法为手动报告分配唯一目录：{base_name}"
        )

    report_path = destination / "report.html"
    try:
        _atomic_copy(source_report, report_path)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return report_path


def generate_manual_report(
    project_root: Path,
    source: ManualSourceRun,
    *,
    excluded_url_keywords: tuple[str, ...] = (),
    additional_date_parameter_names: tuple[str, ...] = (),
) -> Path:
    manual_root = project_root / "manual_reports"
    manual_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=f".{source.run_id}-",
        dir=manual_root,
    ) as temporary_directory:
        staging = Path(temporary_directory)
        html_result = generate_suite_html_report(
            source.suite_for(staging),
            excluded_url_keywords=excluded_url_keywords,
            additional_date_parameter_names=(
                additional_date_parameter_names
            ),
        )
        return _publish_report(
            html_result.report_path,
            manual_root,
        )


def run_manual_report_application(
    project_root: Path,
    *,
    input_reader: Callable[[str], str] | None = None,
) -> int:
    try:
        settings = _configured_settings(project_root)
        sources = _discover_manual_sources(settings.runs_root)
        if not sources:
            print("生成失败：没有可用的已完成运行记录", file=sys.stderr)
            return 2
        source = _select_source(sources, input_reader or input)
        report_path = generate_manual_report(
            project_root,
            source,
            excluded_url_keywords=settings.excluded_url_keywords,
            additional_date_parameter_names=(
                settings.additional_date_parameter_names
            ),
        )
        print(f"来源: {source.directory}")
        print(f"原巡检状态: {status_label(source.status)}")
        print(f"手动报告: {report_path}")
        return 0
    except KeyboardInterrupt:
        print("已取消手动报告生成", file=sys.stderr)
        return 130
    except ManualReportError as error:
        print(f"配置错误：{error}", file=sys.stderr)
        return 2
    except Exception as error:
        print(f"生成失败：{error}", file=sys.stderr)
        return 1
