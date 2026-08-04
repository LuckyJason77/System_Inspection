"""验证一次性巡检和调度应用的启动、取消、错误映射及控制台输出。"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import signal

import pytest

import jmeter_suite.application as application_module
from jmeter_suite.application import (
    run_once_application,
    run_scheduler_application,
)
from jmeter_suite.config import ConfigError, JMeterValidationCancelled
from jmeter_suite.locking import LockUnavailableError
from jmeter_suite.models import (
    JTLParseResult,
    ReportStatus,
    ScriptReportResult,
    SuiteReportResult,
)
from jmeter_suite.orchestration import SuiteStartupError
from jmeter_suite.runner import ProcessTreeTerminationResult
from jmeter_suite.scheduler import (
    SchedulerAlreadyRunningError,
    SchedulerRuntimeError,
)


def _write_valid_app_config(config_dir: Path) -> Path:
    (config_dir / "tools").mkdir(parents=True)
    (config_dir / "plans").mkdir()
    (config_dir / "tools" / "jmeter.bat").write_text(
        "",
        encoding="utf-8",
    )
    (config_dir / "tools" / "jmeter.properties").write_text(
        "jmeter.save.saveservice.output_format=xml\n",
        encoding="utf-8",
    )
    (config_dir / "plans" / "01-login.jmx").write_text(
        "<jmeterTestPlan />",
        encoding="utf-8",
    )
    (config_dir / "plans" / "02-query.JMX").write_text(
        "<jmeterTestPlan />",
        encoding="utf-8",
    )
    config_path = config_dir / "config.toml"
    config_path.write_text(
        """
[jmeter]
executable = "tools/jmeter.bat"
properties_file = "tools/jmeter.properties"

[scripts]
directory = "plans"
timeout_seconds = 1800

[schedule]
timezone = "Asia/Shanghai"
cron = "0 2 * * *"

[output]
directory = "runs"
""".strip(),
        encoding="utf-8",
    )
    return config_path


def _make_report(
    config,
    status: ReportStatus,
    *,
    scripts: tuple[object, ...] = (),
) -> SuiteReportResult:
    run_directory = config.runner.output_root / "2026-08-03_12-00"
    report_path = (
        run_directory
        / "2026-08-03_12-00-01_Inspection_Report"
        / "report.html"
    )
    manifest_path = run_directory / "manifest.json"
    now = datetime(2026, 8, 3, 4, 0, tzinfo=timezone.utc)
    return SuiteReportResult(
        run_id=run_directory.name,
        status=status,
        started_at=now,
        finished_at=now,
        run_directory=run_directory,
        report_path=report_path,
        manifest_path=manifest_path,
        scripts=scripts,
    )


def _make_script_report(
    config,
    *,
    status: ReportStatus,
    process_tree_termination_confirmed: bool | None,
    error: str | None,
) -> ScriptReportResult:
    run_directory = config.runner.output_root / "20260803-120000-12345678"
    artifact_directory = run_directory / "artifacts" / "01-login"
    jtl_path = artifact_directory / "result.jtl"
    now = datetime(2026, 8, 3, 4, 0, tzinfo=timezone.utc)
    return ScriptReportResult(
        script_name="01-login.jmx",
        status=status,
        started_at=now,
        finished_at=now,
        exit_code=None,
        error=error,
        jtl_path=jtl_path,
        log_path=artifact_directory / "jmeter.log",
        report_path=(
            run_directory
            / "2026-08-03_12-00-01_Inspection_Report"
            / "report.html"
        ),
        parse_result=JTLParseResult(
            source_path=jtl_path,
            summaries=(),
            complete=False,
            error="JTL 文件不完整",
            total_samples=0,
            leaf_samples=0,
            passed_leaf_samples=0,
            failed_leaf_samples=0,
            assertion_failures=0,
        ),
        sample_paths=(),
        process_tree_termination_confirmed=(
            process_tree_termination_confirmed
        ),
    )


def _install_final_mapping_interrupt_event(
    monkeypatch: pytest.MonkeyPatch,
    previous_handler: object,
) -> list[object]:
    events: list[object] = []

    class FinalMappingInterruptEvent:
        def __init__(self):
            self.interruptions = 0
            self.set_calls = 0
            self.was_set = False
            events.append(self)

        def is_set(self):
            if (
                not self.was_set
                and signal.getsignal(signal.SIGINT) is previous_handler
            ):
                self.interruptions += 1
                raise KeyboardInterrupt
            return self.was_set

        def set(self):
            self.set_calls += 1
            self.was_set = True

    monkeypatch.setattr(application_module, "Event", FinalMappingInterruptEvent)
    return events


def test_run_once_application_loads_validates_summarizes_and_executes_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    """Skipping or repeating a startup stage could run an unvalidated suite."""
    config_path = _write_valid_app_config(tmp_path / "run-order")
    calls: list[str] = []
    events: list[object] = []
    original_load = application_module.load_config
    original_summary = application_module._print_validation_summary
    previous_handler = signal.getsignal(signal.SIGINT)

    def counted_load(actual_path):
        calls.append("load")
        assert Path(actual_path) == config_path
        return original_load(actual_path)

    def validator(config, cancel_event):
        calls.append("validate")
        events.append(cancel_event)
        assert config.scripts[0].name == "01-login"
        return "5.4.1"

    def print_summary(config, version):
        calls.append("summary")
        original_summary(config, version)

    def execute(config, cancel_event):
        calls.append("execute")
        events.append(cancel_event)
        return _make_report(config, ReportStatus.PASSED)

    monkeypatch.setattr(application_module, "load_config", counted_load)
    monkeypatch.setattr(
        application_module,
        "_print_validation_summary",
        print_summary,
    )

    exit_code = run_once_application(
        config_path,
        jmeter_validator=validator,
        suite_executor=execute,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert calls == ["load", "validate", "summary", "execute"]
    assert len(events) == 2
    assert events[0] is events[1]
    assert events[0].is_set() is False
    assert captured.err == ""
    assert "配置校验通过" in captured.out
    assert "JMeter 版本: 5.4.1" in captured.out
    assert "时区: Asia/Shanghai" in captured.out
    assert "Cron: 0 2 * * *" in captured.out
    assert "默认超时: 1800 秒" in captured.out
    assert "发现脚本: 2" in captured.out
    assert "1. 01-login（超时 1800 秒）" in captured.out
    assert "2. 02-query（超时 1800 秒）" in captured.out
    assert captured.out.index("1. 01-login") < captured.out.index(
        "2. 02-query"
    )
    assert "启动即运行" not in captured.out
    assert "套件状态: 通过" in captured.out
    assert "运行 ID: 2026-08-03_12-00" in captured.out
    assert "运行目录:" in captured.out
    assert "报告:" in captured.out
    assert signal.getsignal(signal.SIGINT) is previous_handler


@pytest.mark.parametrize(
    ("status", "status_label"),
    [
        (ReportStatus.FAILED, "失败"),
        (ReportStatus.ERROR, "执行错误"),
        (ReportStatus.TIMEOUT, "超时"),
    ],
)
def test_run_once_application_returns_one_for_unsuccessful_suite_status(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    status: ReportStatus,
    status_label: str,
):
    """FAILED, ERROR, and TIMEOUT must all be visible to shell automation."""
    config_path = _write_valid_app_config(tmp_path / status.value)

    exit_code = run_once_application(
        config_path,
        jmeter_validator=lambda config, event: "5.4.1",
        suite_executor=lambda config, event: _make_report(config, status),
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.err == ""
    assert f"套件状态: {status_label}" in captured.out


@pytest.mark.parametrize(
    ("case", "expected_error"),
    [
        ("missing-config", "无法读取配置文件"),
        ("missing-properties", "properties_file 文件不存在或不是文件"),
        ("invalid-utf8", "配置文件不是有效的 UTF-8"),
    ],
)
def test_run_once_application_configuration_errors_stop_before_jmeter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
    expected_error: str,
):
    """Invalid startup input must never reach JMeter or suite execution."""
    if case == "missing-config":
        config_path = tmp_path / "missing.toml"
    else:
        config_path = _write_valid_app_config(tmp_path / case)
        if case == "missing-properties":
            (config_path.parent / "tools" / "jmeter.properties").unlink()
        else:
            config_path.write_bytes(b"\xff\xfe\x00")

    load_calls = 0
    original_load = application_module.load_config

    def counted_load(actual_path):
        nonlocal load_calls
        load_calls += 1
        return original_load(actual_path)

    def forbidden(*args, **kwargs):
        pytest.fail("JMeter and suite must not start")

    monkeypatch.setattr(application_module, "load_config", counted_load)

    exit_code = run_once_application(
        config_path,
        jmeter_validator=forbidden,
        suite_executor=forbidden,
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert load_calls == 1
    assert captured.out == ""
    assert "配置错误：" in captured.err
    assert expected_error in captured.err
    assert "Traceback" not in captured.err


def test_run_once_application_preserves_explicit_preflight_config_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    """A known validation error must not be mislabeled as an unexpected check crash."""
    config_path = _write_valid_app_config(tmp_path / "known-preflight")

    def known_error(config, cancel_event):
        raise ConfigError("版本不受支持")

    exit_code = run_once_application(
        config_path,
        jmeter_validator=known_error,
        suite_executor=lambda *args: pytest.fail("suite must not start"),
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "配置错误：版本不受支持" in captured.err
    assert "启动检查失败" not in captured.err


def test_run_once_application_wraps_unexpected_preflight_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    """An unexpected validator crash is a contained startup/config failure."""
    config_path = _write_valid_app_config(tmp_path / "broken-preflight")

    def broken_validator(config, cancel_event):
        raise RuntimeError("probe exploded")

    exit_code = run_once_application(
        config_path,
        jmeter_validator=broken_validator,
        suite_executor=lambda *args: pytest.fail("suite must not start"),
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "配置错误：启动检查失败: probe exploded" in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize(
    ("failure", "expected_text"),
    [
        (
            LockUnavailableError(Path("suite.lock")),
            "文件锁已被占用",
        ),
        (
            SuiteStartupError("无法初始化运行锁"),
            "无法初始化运行锁",
        ),
    ],
)
def test_run_once_application_maps_suite_startup_failures_to_two(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    failure: Exception,
    expected_text: str,
):
    """Lock contention and lock initialization errors are startup failures."""
    config_path = _write_valid_app_config(tmp_path / expected_text)

    def failing_execute(config, cancel_event):
        raise failure

    exit_code = run_once_application(
        config_path,
        jmeter_validator=lambda config, event: "5.4.1",
        suite_executor=failing_execute,
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert captured.out.startswith("配置校验通过")
    assert "启动失败：" in captured.err
    assert expected_text in captured.err


def test_run_once_application_cancelled_result_returns_130_with_summary(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    """A runner-reported cancellation is an interrupted process, not failure 1."""
    config_path = _write_valid_app_config(tmp_path / "cancelled-result")

    exit_code = run_once_application(
        config_path,
        jmeter_validator=lambda config, event: "5.4.1",
        suite_executor=lambda config, event: _make_report(
            config,
            ReportStatus.CANCELLED,
        ),
    )

    captured = capsys.readouterr()
    assert exit_code == 130
    assert captured.err == ""
    assert "套件状态: 已取消" in captured.out


def test_run_once_application_contains_report_or_runtime_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    """An orchestration/report exception must become exit 1 without a traceback."""
    config_path = _write_valid_app_config(tmp_path / "report-error")

    def broken_report(config, cancel_event):
        raise RuntimeError("report rendering failed")

    exit_code = run_once_application(
        config_path,
        jmeter_validator=lambda config, event: "5.4.1",
        suite_executor=broken_report,
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "运行失败：report rendering failed" in captured.err
    assert "Traceback" not in captured.err


def test_run_once_config_load_sigint_returns_130_before_validator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    """Ctrl+C during config loading must stop before JMeter validation."""
    config_path = _write_valid_app_config(tmp_path / "config-sigint")
    config = application_module.load_config(config_path)
    previous_handler = signal.getsignal(signal.SIGINT)
    validator_calls = 0

    def interrupted_load(path):
        signal.raise_signal(signal.SIGINT)
        return config

    def forbidden_validator(config, cancel_event):
        nonlocal validator_calls
        validator_calls += 1
        pytest.fail("validator must not start")

    monkeypatch.setattr(application_module, "load_config", interrupted_load)

    exit_code = run_once_application(
        tmp_path / "ignored.toml",
        jmeter_validator=forbidden_validator,
        suite_executor=lambda *args: pytest.fail("suite must not start"),
    )

    captured = capsys.readouterr()
    assert exit_code == 130
    assert validator_calls == 0
    assert captured.err.count("已收到人工中断") == 1
    assert signal.getsignal(signal.SIGINT) is previous_handler


def test_run_once_version_check_sigint_returns_130_before_suite(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    """Ctrl+C during version probing must stop before suite execution."""
    config_path = _write_valid_app_config(tmp_path / "version-sigint")
    previous_handler = signal.getsignal(signal.SIGINT)
    suite_calls = 0

    def interrupted_validator(config, cancel_event):
        signal.raise_signal(signal.SIGINT)
        assert cancel_event.is_set() is True
        return "5.4.1"

    def forbidden_suite(config, cancel_event):
        nonlocal suite_calls
        suite_calls += 1
        pytest.fail("suite must not start")

    exit_code = run_once_application(
        config_path,
        jmeter_validator=interrupted_validator,
        suite_executor=forbidden_suite,
    )

    captured = capsys.readouterr()
    assert exit_code == 130
    assert suite_calls == 0
    assert captured.err.count("已收到人工中断") == 1
    assert signal.getsignal(signal.SIGINT) is previous_handler


def test_run_once_version_cancel_warns_when_tree_cleanup_unconfirmed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    """Unconfirmed version-process cleanup must remain visible on interruption."""
    config_path = _write_valid_app_config(tmp_path / "version-cleanup")
    previous_handler = signal.getsignal(signal.SIGINT)

    def cancelled_validator(config, cancel_event):
        raise JMeterValidationCancelled(
            ProcessTreeTerminationResult(
                confirmed=False,
                error="taskkill denied",
            )
        )

    exit_code = run_once_application(
        config_path,
        jmeter_validator=cancelled_validator,
        suite_executor=lambda *args: pytest.fail("suite must not start"),
    )

    captured = capsys.readouterr()
    assert exit_code == 130
    assert "人工中断：进程树清理未确认: taskkill denied" in captured.err
    assert signal.getsignal(signal.SIGINT) is previous_handler


def test_run_once_suite_cancel_warns_when_tree_cleanup_unconfirmed(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    """A failed cleanup after cancelling a real suite must be visible on stderr."""
    config_path = _write_valid_app_config(tmp_path / "suite-cleanup")

    def interrupted_execute(config, cancel_event):
        signal.raise_signal(signal.SIGINT)
        return _make_report(
            config,
            ReportStatus.FAILED,
            scripts=(
                _make_script_report(
                    config,
                    status=ReportStatus.CANCELLED,
                    error=(
                        "JMeter execution was cancelled; "
                        "process-tree cleanup failure: taskkill denied"
                    ),
                    process_tree_termination_confirmed=False,
                ),
            ),
        )

    exit_code = run_once_application(
        config_path,
        jmeter_validator=lambda config, event: "5.4.1",
        suite_executor=interrupted_execute,
    )

    captured = capsys.readouterr()
    assert exit_code == 130
    assert "人工中断：进程树清理未确认" in captured.err
    assert "01-login.jmx" in captured.err
    assert "taskkill denied" in captured.err


def test_run_once_timeout_cleanup_warns_when_cancel_event_is_set(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    """SIGINT during timeout cleanup must keep its warning visible."""
    config_path = _write_valid_app_config(tmp_path / "timeout-cancel-cleanup")

    def interrupted_timeout(config, cancel_event):
        cancel_event.set()
        return _make_report(
            config,
            ReportStatus.FAILED,
            scripts=(
                _make_script_report(
                    config,
                    status=ReportStatus.TIMEOUT,
                    error=(
                        "JMeter execution timed out; "
                        "process-tree cleanup failure: taskkill denied"
                    ),
                    process_tree_termination_confirmed=False,
                ),
            ),
        )

    exit_code = run_once_application(
        config_path,
        jmeter_validator=lambda config, event: "5.4.1",
        suite_executor=interrupted_timeout,
    )

    captured = capsys.readouterr()
    assert exit_code == 130
    assert "人工中断：进程树清理未确认" in captured.err
    assert "01-login.jmx" in captured.err
    assert "taskkill denied" in captured.err


def test_run_once_timeout_cleanup_does_not_warn_without_cancel_event(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    """An ordinary timeout cleanup failure must not be labeled SIGINT."""
    config_path = _write_valid_app_config(tmp_path / "timeout-cleanup")

    def timed_out(config, cancel_event):
        return _make_report(
            config,
            ReportStatus.FAILED,
            scripts=(
                _make_script_report(
                    config,
                    status=ReportStatus.TIMEOUT,
                    error=(
                        "JMeter execution timed out; "
                        "process-tree cleanup failure: taskkill denied"
                    ),
                    process_tree_termination_confirmed=False,
                ),
            ),
        )

    exit_code = run_once_application(
        config_path,
        jmeter_validator=lambda config, event: "5.4.1",
        suite_executor=timed_out,
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "人工中断：进程树清理未确认" not in captured.err


@pytest.mark.parametrize(
    "cleanup_confirmed",
    [True, None],
)
def test_run_once_suite_cancel_does_not_warn_without_cleanup_failure(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    cleanup_confirmed: bool | None,
):
    """Confirmed or unattempted cleanup must not emit the failure warning."""
    config_path = _write_valid_app_config(
        tmp_path / f"suite-cleanup-{cleanup_confirmed}"
    )

    def interrupted_execute(config, cancel_event):
        cancel_event.set()
        return _make_report(
            config,
            ReportStatus.FAILED,
            scripts=(
                _make_script_report(
                    config,
                    status=ReportStatus.CANCELLED,
                    error="JMeter execution was cancelled",
                    process_tree_termination_confirmed=cleanup_confirmed,
                ),
            ),
        )

    exit_code = run_once_application(
        config_path,
        jmeter_validator=lambda config, event: "5.4.1",
        suite_executor=interrupted_execute,
    )

    captured = capsys.readouterr()
    assert exit_code == 130
    assert "进程树清理未确认" not in captured.err


def test_run_once_suite_sigint_sets_event_prints_once_and_returns_130(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    """Suite execution must share the handler event and duplicate SIGINT stays quiet."""
    config_path = _write_valid_app_config(tmp_path / "suite-sigint")
    previous_handler = signal.getsignal(signal.SIGINT)

    def interrupted_execute(config, cancel_event):
        signal.raise_signal(signal.SIGINT)
        signal.raise_signal(signal.SIGINT)
        assert cancel_event.is_set() is True
        return _make_report(config, ReportStatus.CANCELLED)

    exit_code = run_once_application(
        config_path,
        jmeter_validator=lambda config, event: "5.4.1",
        suite_executor=interrupted_execute,
    )

    captured = capsys.readouterr()
    assert exit_code == 130
    assert captured.err.count("已收到人工中断") == 1
    assert signal.getsignal(signal.SIGINT) is previous_handler


@pytest.mark.parametrize(
    "failure",
    [
        LockUnavailableError(Path("suite.lock")),
        SuiteStartupError("无法初始化运行锁"),
        RuntimeError("report cleanup failed"),
    ],
)
def test_run_once_sigint_takes_precedence_over_followup_suite_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    failure: Exception,
):
    """A cleanup error after SIGINT must not replace interrupted exit 130."""
    config_path = _write_valid_app_config(tmp_path / type(failure).__name__)
    previous_handler = signal.getsignal(signal.SIGINT)

    def interrupted_execute(config, cancel_event):
        signal.raise_signal(signal.SIGINT)
        assert cancel_event.is_set() is True
        raise failure

    exit_code = run_once_application(
        config_path,
        jmeter_validator=lambda config, event: "5.4.1",
        suite_executor=interrupted_execute,
    )

    captured = capsys.readouterr()
    assert exit_code == 130
    assert captured.err.count("已收到人工中断") == 1
    assert "启动失败：" not in captured.err
    assert "运行失败：" not in captured.err
    assert signal.getsignal(signal.SIGINT) is previous_handler


def test_run_once_report_summary_sigint_is_still_in_application_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    """Restoring SIGINT before final report output would leave a lifecycle gap."""
    config_path = _write_valid_app_config(tmp_path / "summary-sigint")
    previous_handler = signal.getsignal(signal.SIGINT)
    original_summary = application_module._print_run_summary

    def interrupted_summary(result):
        signal.raise_signal(signal.SIGINT)
        original_summary(result)

    monkeypatch.setattr(
        application_module,
        "_print_run_summary",
        interrupted_summary,
    )

    exit_code = run_once_application(
        config_path,
        jmeter_validator=lambda config, event: "5.4.1",
        suite_executor=lambda config, event: _make_report(
            config,
            ReportStatus.PASSED,
        ),
    )

    captured = capsys.readouterr()
    assert exit_code == 130
    assert "套件状态: 通过" in captured.out
    assert captured.err.count("已收到人工中断") == 1
    assert signal.getsignal(signal.SIGINT) is previous_handler


def test_run_once_sigint_while_signal_context_exits_returns_130(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    """SIGINT just before handler restoration must override a PASSED result."""
    config_path = _write_valid_app_config(tmp_path / "context-exit-sigint")
    previous_handler = signal.getsignal(signal.SIGINT)
    original_context = application_module._signal_cancellation

    @contextmanager
    def interrupt_before_restore(cancel_event):
        with original_context(cancel_event):
            try:
                yield
            finally:
                signal.raise_signal(signal.SIGINT)

    monkeypatch.setattr(
        application_module,
        "_signal_cancellation",
        interrupt_before_restore,
    )

    exit_code = run_once_application(
        config_path,
        jmeter_validator=lambda config, event: "5.4.1",
        suite_executor=lambda config, event: _make_report(
            config,
            ReportStatus.PASSED,
        ),
    )

    captured = capsys.readouterr()
    assert exit_code == 130
    assert "套件状态: 通过" in captured.out
    assert captured.err.count("已收到人工中断") == 1
    assert signal.getsignal(signal.SIGINT) is previous_handler


def test_run_once_keyboard_interrupt_after_handler_restore_returns_130(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    """The final manual status read remains protected after handler restore."""
    config_path = _write_valid_app_config(tmp_path / "manual-final-interrupt")
    previous_handler = signal.getsignal(signal.SIGINT)
    events = _install_final_mapping_interrupt_event(
        monkeypatch,
        previous_handler,
    )

    try:
        exit_code = run_once_application(
            config_path,
            jmeter_validator=lambda config, event: "5.4.1",
            suite_executor=lambda config, event: _make_report(
                config,
                ReportStatus.PASSED,
            ),
        )
    except KeyboardInterrupt:
        pytest.fail(
            "KeyboardInterrupt escaped from final manual status mapping"
        )

    captured = capsys.readouterr()
    assert exit_code == 130
    assert len(events) == 1
    assert events[0].interruptions == 1
    assert events[0].set_calls == 1
    assert events[0].was_set is True
    assert "已收到人工中断" in captured.err
    assert signal.getsignal(signal.SIGINT) is previous_handler


def test_run_scheduler_application_loads_validates_and_starts_once_after_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    """The scheduler starts once, after preflight and after handler restoration."""
    config_path = _write_valid_app_config(tmp_path / "scheduler-order")
    calls: list[str] = []
    events: list[object] = []
    previous_handler = signal.getsignal(signal.SIGINT)
    original_load = application_module.load_config
    original_summary = application_module._print_validation_summary

    def counted_load(actual_path):
        calls.append("load")
        return original_load(actual_path)

    def validator(config, cancel_event):
        calls.append("validate")
        events.append(cancel_event)
        return "5.4.1"

    def print_summary(config, version):
        calls.append("summary")
        original_summary(config, version)

    def scheduler(config, cancel_event):
        calls.append("scheduler")
        events.append(cancel_event)
        assert signal.getsignal(signal.SIGINT) is previous_handler
        return False

    monkeypatch.setattr(application_module, "load_config", counted_load)
    monkeypatch.setattr(
        application_module,
        "_print_validation_summary",
        print_summary,
    )

    exit_code = run_scheduler_application(
        config_path,
        jmeter_validator=validator,
        scheduler_runner=scheduler,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert calls == ["load", "validate", "summary", "scheduler"]
    assert len(events) == 2
    assert events[0] is events[1]
    assert events[0].is_set() is False
    assert "配置校验通过" in captured.out
    assert captured.err == ""
    assert signal.getsignal(signal.SIGINT) is previous_handler


def test_run_scheduler_application_invalid_config_stops_before_dependencies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    """Scheduler config errors must stop before validation and construction."""
    config_path = tmp_path / "missing.toml"
    load_calls = 0
    original_load = application_module.load_config

    def counted_load(actual_path):
        nonlocal load_calls
        load_calls += 1
        return original_load(actual_path)

    def forbidden(*args, **kwargs):
        pytest.fail("scheduler dependencies must not start")

    monkeypatch.setattr(application_module, "load_config", counted_load)

    exit_code = run_scheduler_application(
        config_path,
        jmeter_validator=forbidden,
        scheduler_runner=forbidden,
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert load_calls == 1
    assert captured.out == ""
    assert "配置错误：" in captured.err


def test_scheduler_preflight_sigint_returns_130_before_scheduler(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    """A preflight Ctrl+C must not enter the resident scheduler."""
    config_path = _write_valid_app_config(tmp_path / "scheduler-sigint")
    previous_handler = signal.getsignal(signal.SIGINT)
    scheduler_calls = 0

    def interrupted_validator(config, cancel_event):
        signal.raise_signal(signal.SIGINT)
        assert cancel_event.is_set() is True
        return "5.4.1"

    def forbidden_scheduler(config, cancel_event):
        nonlocal scheduler_calls
        scheduler_calls += 1
        pytest.fail("scheduler must not start")

    exit_code = run_scheduler_application(
        config_path,
        jmeter_validator=interrupted_validator,
        scheduler_runner=forbidden_scheduler,
    )

    captured = capsys.readouterr()
    assert exit_code == 130
    assert scheduler_calls == 0
    assert captured.err.count("已收到人工中断") == 1
    assert signal.getsignal(signal.SIGINT) is previous_handler


def test_scheduler_preflight_sigint_takes_precedence_over_summary_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    """A summary failure after preflight SIGINT must remain exit 130."""
    config_path = _write_valid_app_config(tmp_path / "scheduler-summary-error")
    previous_handler = signal.getsignal(signal.SIGINT)

    def interrupted_summary(config, version):
        signal.raise_signal(signal.SIGINT)
        raise RuntimeError("summary output failed")

    monkeypatch.setattr(
        application_module,
        "_print_validation_summary",
        interrupted_summary,
    )

    exit_code = run_scheduler_application(
        config_path,
        jmeter_validator=lambda config, event: "5.4.1",
        scheduler_runner=lambda *args: pytest.fail("scheduler must not start"),
    )

    captured = capsys.readouterr()
    assert exit_code == 130
    assert captured.err.count("已收到人工中断") == 1
    assert "调度器启动失败：" not in captured.err
    assert signal.getsignal(signal.SIGINT) is previous_handler


def test_scheduler_idle_keyboard_interrupt_returns_130(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    """BlockingScheduler's idle Ctrl+C path maps to process interruption."""
    config_path = _write_valid_app_config(tmp_path / "scheduler-idle")
    previous_handler = signal.getsignal(signal.SIGINT)
    observed_event = None

    def interrupted_scheduler(config, cancel_event):
        nonlocal observed_event
        observed_event = cancel_event
        assert signal.getsignal(signal.SIGINT) is previous_handler
        raise KeyboardInterrupt

    exit_code = run_scheduler_application(
        config_path,
        jmeter_validator=lambda config, event: "5.4.1",
        scheduler_runner=interrupted_scheduler,
    )

    captured = capsys.readouterr()
    assert exit_code == 130
    assert observed_event.is_set() is True
    assert "已收到人工中断" in captured.err
    assert signal.getsignal(signal.SIGINT) is previous_handler


def test_scheduler_running_job_cancellation_returns_130(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    """Stopping after active-job/report cleanup remains an interrupted exit."""
    config_path = _write_valid_app_config(tmp_path / "scheduler-running")
    previous_handler = signal.getsignal(signal.SIGINT)

    def stopped_after_cleanup(config, cancel_event):
        assert signal.getsignal(signal.SIGINT) is previous_handler
        cancel_event.set()
        return True

    exit_code = run_scheduler_application(
        config_path,
        jmeter_validator=lambda config, event: "5.4.1",
        scheduler_runner=stopped_after_cleanup,
    )

    captured = capsys.readouterr()
    assert exit_code == 130
    assert captured.err == ""
    assert signal.getsignal(signal.SIGINT) is previous_handler


def test_scheduler_keyboard_interrupt_after_handler_restore_returns_130(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    """The final scheduler status read remains protected after runner return."""
    config_path = _write_valid_app_config(tmp_path / "scheduler-final-interrupt")
    previous_handler = signal.getsignal(signal.SIGINT)
    events = _install_final_mapping_interrupt_event(
        monkeypatch,
        previous_handler,
    )

    def scheduler(config, cancel_event):
        assert signal.getsignal(signal.SIGINT) is previous_handler
        return False

    try:
        exit_code = run_scheduler_application(
            config_path,
            jmeter_validator=lambda config, event: "5.4.1",
            scheduler_runner=scheduler,
        )
    except KeyboardInterrupt:
        pytest.fail(
            "KeyboardInterrupt escaped from final scheduler status mapping"
        )

    captured = capsys.readouterr()
    assert exit_code == 130
    assert len(events) == 1
    assert events[0].interruptions == 1
    assert events[0].set_calls == 1
    assert events[0].was_set is True
    assert "已收到人工中断" in captured.err
    assert signal.getsignal(signal.SIGINT) is previous_handler


@pytest.mark.parametrize(
    "failure",
    [
        SchedulerRuntimeError("executor infrastructure failed"),
        SchedulerAlreadyRunningError(Path("scheduler.lock")),
        OSError("shutdown cleanup failed"),
    ],
)
def test_scheduler_manual_stop_takes_precedence_over_followup_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    failure: Exception,
):
    """A scheduler cleanup error after cancellation must remain exit 130."""
    config_path = _write_valid_app_config(
        tmp_path / f"scheduler-{type(failure).__name__}"
    )
    previous_handler = signal.getsignal(signal.SIGINT)

    def interrupted_scheduler(config, cancel_event):
        assert signal.getsignal(signal.SIGINT) is previous_handler
        cancel_event.set()
        raise failure

    exit_code = run_scheduler_application(
        config_path,
        jmeter_validator=lambda config, event: "5.4.1",
        scheduler_runner=interrupted_scheduler,
    )

    captured = capsys.readouterr()
    assert exit_code == 130
    assert "调度器运行失败：" not in captured.err
    assert "调度器启动失败：" not in captured.err
    assert signal.getsignal(signal.SIGINT) is previous_handler


def test_run_scheduler_application_second_instance_returns_two(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    """A resident scheduler lock conflict is a startup error."""
    config_path = _write_valid_app_config(tmp_path / "scheduler-conflict")

    def conflicting_scheduler(config, cancel_event):
        raise SchedulerAlreadyRunningError(
            config.runner.output_root / ".runtime" / "scheduler.lock"
        )

    exit_code = run_scheduler_application(
        config_path,
        jmeter_validator=lambda config, event: "5.4.1",
        scheduler_runner=conflicting_scheduler,
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "调度器启动失败：已有调度器进程正在运行" in captured.err


def test_run_scheduler_application_runtime_failure_returns_one(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    """Only a failure after blocking scheduler start is process failure 1."""
    config_path = _write_valid_app_config(tmp_path / "scheduler-runtime")

    def broken_runtime(config, cancel_event):
        raise SchedulerRuntimeError("executor infrastructure failed")

    exit_code = run_scheduler_application(
        config_path,
        jmeter_validator=lambda config, event: "5.4.1",
        scheduler_runner=broken_runtime,
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "调度器运行失败：executor infrastructure failed" in captured.err


def test_run_scheduler_application_startup_failure_returns_two(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    """Construction, logging, lock, and other pre-start failures stay exit 2."""
    config_path = _write_valid_app_config(tmp_path / "scheduler-startup")

    def broken_startup(config, cancel_event):
        raise OSError("cannot create scheduler log")

    exit_code = run_scheduler_application(
        config_path,
        jmeter_validator=lambda config, event: "5.4.1",
        scheduler_runner=broken_startup,
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "调度器启动失败：cannot create scheduler log" in captured.err
