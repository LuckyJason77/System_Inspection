"""定义配置、执行、JTL 解析和报告生成流程共享的不可变数据模型。"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path


class ExecutionStatus(str, Enum):
    COMPLETED = "completed"
    ERROR = "error"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class ReportStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class AssertionResult:
    name: str
    failure: bool
    error: bool
    failure_message: str

    @property
    def passed(self) -> bool:
        return not self.failure and not self.error


@dataclass(frozen=True, slots=True)
class SampleRecord:
    sample_id: str
    sequence: int
    depth: int
    sample_type: str
    label: str
    success: bool
    is_leaf: bool
    timestamp_ms: int | None
    elapsed_ms: int | None
    response_code: str
    response_message: str
    thread_name: str
    url: str
    query_string: str
    request_headers: str
    sampler_data: str
    response_headers: str
    response_data: str
    data_type: str
    encoding: str
    assertions: tuple[AssertionResult, ...]

    @property
    def passed(self) -> bool:
        return self.success and all(
            assertion.passed for assertion in self.assertions
        )


@dataclass(frozen=True, slots=True)
class SampleSummary:
    sample_id: str
    sequence: int
    depth: int
    sample_type: str
    label: str
    is_leaf: bool
    passed: bool
    response_code: str
    assertion_failed: bool


@dataclass(frozen=True, slots=True)
class JTLParseResult:
    source_path: Path
    summaries: tuple[SampleSummary, ...]
    complete: bool
    error: str | None
    total_samples: int
    leaf_samples: int
    passed_leaf_samples: int
    failed_leaf_samples: int
    assertion_failures: int

    @property
    def passed(self) -> bool:
        return (
            self.complete
            and self.leaf_samples > 0
            and self.failed_leaf_samples == 0
            and self.assertion_failures == 0
        )


@dataclass(frozen=True, slots=True)
class JMeterExecutionResult:
    script_name: str
    status: ExecutionStatus
    started_at: datetime
    finished_at: datetime
    exit_code: int | None
    error: str | None
    jtl_path: Path
    log_path: Path
    stdout: str
    stderr: str
    process_tree_termination_confirmed: bool | None = None

    @property
    def duration_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()


@dataclass(frozen=True, slots=True)
class SuiteProcessResult:
    run_id: str
    run_directory: Path
    started_at: datetime
    finished_at: datetime
    executions: tuple[JMeterExecutionResult, ...]


@dataclass(frozen=True, slots=True)
class ScriptReportResult:
    script_name: str
    status: ReportStatus
    started_at: datetime
    finished_at: datetime
    exit_code: int | None
    error: str | None
    jtl_path: Path
    log_path: Path
    report_path: Path
    parse_result: JTLParseResult
    sample_paths: tuple[Path, ...]
    process_tree_termination_confirmed: bool | None = None

    @property
    def name(self) -> str:
        return self.script_name

    @property
    def duration_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()

    @property
    def parse_complete(self) -> bool:
        return self.parse_result.complete

    @property
    def parse_error(self) -> str | None:
        return self.parse_result.error

    @property
    def total_samples(self) -> int:
        return self.parse_result.total_samples

    @property
    def leaf_samples(self) -> int:
        return self.parse_result.leaf_samples

    @property
    def passed_samples(self) -> int:
        return self.parse_result.passed_leaf_samples

    @property
    def failed_samples(self) -> int:
        return self.parse_result.failed_leaf_samples

    @property
    def assertion_failures(self) -> int:
        return self.parse_result.assertion_failures


@dataclass(frozen=True, slots=True)
class SuiteReportResult:
    run_id: str
    status: ReportStatus
    started_at: datetime
    finished_at: datetime
    run_directory: Path
    report_path: Path
    manifest_path: Path
    scripts: tuple[ScriptReportResult, ...]

    @property
    def script_results(self) -> tuple[ScriptReportResult, ...]:
        return self.scripts

    @property
    def duration_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()


@dataclass(frozen=True, slots=True)
class SuiteHtmlReportResult:
    run_id: str
    status: ReportStatus
    started_at: datetime
    finished_at: datetime
    run_directory: Path
    report_path: Path
    scripts: tuple[ScriptReportResult, ...]

    @property
    def duration_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()


@dataclass(frozen=True, slots=True)
class JMeterConfig:
    executable: Path
    properties_file: Path


@dataclass(frozen=True, slots=True)
class ScheduleConfig:
    timezone: str
    cron: str


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    output_root: Path
    default_timeout_seconds: int


@dataclass(frozen=True, slots=True)
class ScriptConfig:
    name: str
    jmx: Path
    working_directory: Path
    timeout_seconds: int


@dataclass(frozen=True, slots=True)
class ReportConfig:
    excluded_url_keywords: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AppConfig:
    jmeter: JMeterConfig
    schedule: ScheduleConfig
    runner: RunnerConfig
    scripts: tuple[ScriptConfig, ...]
    report: ReportConfig = ReportConfig()
