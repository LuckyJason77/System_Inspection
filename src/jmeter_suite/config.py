"""读取并严格校验 TOML 配置、JMX 脚本目录和 JMeter 运行环境。"""

from collections.abc import Callable
import os
from pathlib import Path
import re
import subprocess
import tempfile
from threading import Event
import time
import tomllib
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from apscheduler.triggers.cron import CronTrigger

from .models import (
    AppConfig,
    DingTalkConfig,
    JMeterConfig,
    ReportConfig,
    RunnerConfig,
    ScheduleConfig,
    ScriptConfig,
)
from .report_filter import (
    ReportFilterConfigError,
    parse_excluded_url_keywords,
)
from .request_period import (
    DateParameterConfigError,
    parse_additional_date_parameter_names,
)
from .runner import ProcessTreeTerminationResult, terminate_process_tree


class ConfigError(ValueError):
    """Raised when the runner configuration is invalid."""


class JMeterValidationCancelled(RuntimeError):
    def __init__(self, termination: ProcessTreeTerminationResult):
        self.cleanup_confirmed = termination.confirmed
        self.cleanup_error = termination.error
        super().__init__("JMeter 版本检查已由人工取消")


_TOP_LEVEL_KEYS = frozenset(
    {"jmeter", "scripts", "schedule", "output", "report", "dingtalk"}
)
_JMETER_KEYS = frozenset({"executable", "properties_file"})
_SCRIPTS_KEYS = frozenset({"directory", "timeout_seconds"})
_SCHEDULE_KEYS = frozenset({"timezone", "cron"})
_OUTPUT_KEYS = frozenset({"directory"})
_REPORT_KEYS = frozenset(
    {"excluded_url_keywords", "additional_date_parameter_names"}
)
_DINGTALK_KEYS = frozenset(
    {"enabled", "client_id", "client_secret"}
)
_CMD_UNSAFE_PATH_CHARACTERS = frozenset("&%^!|<>()\r\n")


def _absolute_path(base_directory: Path, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = base_directory / path
    return path.absolute()


def _resolve_path(base_directory: Path, value: str) -> Path:
    return _absolute_path(base_directory, value).resolve()


def _validate_cmd_safe_path(
    path: Path,
    label: str,
    errors: list[str],
) -> bool:
    unsafe_character = next(
        (
            character
            for character in str(path)
            if character in _CMD_UNSAFE_PATH_CHARACTERS
        ),
        None,
    )
    if unsafe_character is None:
        return True
    errors.append(
        f"{label} 包含 CMD 不安全字符 {unsafe_character!r}: {path}"
    )
    return False


def _add_unknown_key_errors(
    table: dict[str, Any],
    allowed_keys: frozenset[str],
    location: str,
    errors: list[str],
) -> None:
    for key in sorted(set(table) - allowed_keys):
        errors.append(f"{location} 包含未知键: {key}")


def _get_table(
    raw: dict[str, Any],
    key: str,
    errors: list[str],
) -> dict[str, Any]:
    if key not in raw:
        errors.append(f"缺少配置块: {key}")
        return {}
    value = raw[key]
    if not isinstance(value, dict):
        errors.append(f"{key} 必须是配置块")
        return {}
    return value


def _get_optional_table(
    raw: dict[str, Any],
    key: str,
    errors: list[str],
) -> dict[str, Any]:
    if key not in raw:
        return {}
    value = raw[key]
    if not isinstance(value, dict):
        errors.append(f"{key} 必须是配置块")
        return {}
    return value


def _get_required_string(
    table: dict[str, Any],
    key: str,
    location: str,
    errors: list[str],
) -> str | None:
    if key not in table:
        errors.append(f"缺少配置项: {location}.{key}")
        return None
    value = table[key]
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{location}.{key} 必须是非空字符串")
        return None
    return value.strip()


def _get_required_positive_integer(
    table: dict[str, Any],
    key: str,
    location: str,
    errors: list[str],
) -> int:
    if key not in table:
        errors.append(f"缺少配置项: {location}.{key}")
        return 3600
    value = table[key]
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        errors.append(f"{location}.{key} 必须是正整数")
        return 3600
    return value


def _get_optional_boolean(
    table: dict[str, Any],
    key: str,
    location: str,
    errors: list[str],
    *,
    default: bool = False,
) -> bool:
    if key not in table:
        return default
    value = table[key]
    if not isinstance(value, bool):
        errors.append(f"{location}.{key} 必须是布尔值")
        return default
    return value


def _get_disabled_credential(
    table: dict[str, Any],
    key: str,
    errors: list[str],
) -> str:
    if key not in table:
        return ""
    value = table[key]
    if not isinstance(value, str):
        errors.append(f"dingtalk.{key} 必须是字符串")
        return ""
    return value.strip()


def _validate_readable_file(
    path: Path | None,
    label: str,
    errors: list[str],
) -> None:
    if path is None:
        return
    if not path.is_file():
        errors.append(f"{label} 文件不存在或不是文件: {path}")
        return
    if not os.access(path, os.R_OK):
        errors.append(f"{label} 文件不可读: {path}")


def _validate_output_root(path: Path | None, errors: list[str]) -> None:
    if path is None:
        return
    if path.exists():
        if not path.is_dir():
            errors.append(f"output.directory 必须是目录: {path}")
            return
        writable_directory = path
    else:
        writable_directory = path.parent
        while (
            not writable_directory.exists()
            and writable_directory != writable_directory.parent
        ):
            writable_directory = writable_directory.parent
        if not writable_directory.exists():
            errors.append(
                f"output.directory 找不到存在的父目录: {path}"
            )
            return
        if not writable_directory.is_dir():
            errors.append(
                "output.directory 最近的现有父路径不是目录: "
                f"{writable_directory}"
            )
            return
    if not os.access(writable_directory, os.W_OK):
        errors.append(
            "output.directory 最近的现有目录不可写: "
            f"{writable_directory}"
        )


def _discover_scripts(
    directory: Path,
    timeout_seconds: int,
    errors: list[str],
) -> tuple[ScriptConfig, ...]:
    if not _validate_cmd_safe_path(directory, "scripts.directory", errors):
        return ()
    if directory.is_symlink():
        errors.append(f"scripts.directory 不允许是符号链接: {directory}")
        return ()
    directory = directory.resolve()
    if not directory.is_dir():
        errors.append(f"scripts.directory 不存在或不是目录: {directory}")
        return ()
    if not os.access(directory, os.R_OK):
        errors.append(f"scripts.directory 不可读: {directory}")
        return ()

    try:
        entries = tuple(directory.iterdir())
    except OSError as error:
        errors.append(f"scripts.directory 无法读取: {directory} ({error})")
        return ()

    valid_jmx_paths: list[Path] = []
    for path in entries:
        if path.is_symlink():
            errors.append(f"scripts.directory 不允许包含符号链接: {path}")
            continue
        if path.is_dir():
            errors.append(f"scripts.directory 不允许包含子目录: {path}")
            continue
        if not path.is_file() or path.suffix.casefold() != ".jmx":
            errors.append(f"scripts.directory 只能包含 JMX 文件: {path}")
            continue
        if not _validate_cmd_safe_path(
            path,
            "scripts.directory 中的 JMX 文件",
            errors,
        ):
            continue
        if not os.access(path, os.R_OK):
            errors.append(
                f"scripts.directory 中的 JMX 文件不可读: {path}"
            )
            continue
        valid_jmx_paths.append(path)

    valid_jmx_paths.sort(key=lambda path: (path.name.casefold(), path.name))
    if not valid_jmx_paths:
        errors.append("scripts.directory 至少需要一个 JMX 文件")
        return ()

    scripts: list[ScriptConfig] = []
    seen_names: set[str] = set()
    for path in valid_jmx_paths:
        name = path.stem.strip()
        if not name:
            errors.append(f"JMX 文件名去除空白后不能为空: {path}")
            continue
        normalized_name = name.casefold()
        if normalized_name in seen_names:
            errors.append(f"JMX 脚本名称重复（忽略大小写）: {name}")
            continue
        seen_names.add(normalized_name)
        scripts.append(
            ScriptConfig(
                name=name,
                jmx=path.resolve(),
                working_directory=directory,
                timeout_seconds=timeout_seconds,
            )
        )
    return tuple(scripts)


def _read_toml(config_path: Path) -> dict[str, Any]:
    try:
        with config_path.open("rb") as config_file:
            return tomllib.load(config_file)
    except UnicodeDecodeError as error:
        raise ConfigError(
            f"配置文件不是有效的 UTF-8: {config_path} ({error})"
        ) from None
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"配置 TOML 格式错误: {error}") from None
    except OSError as error:
        raise ConfigError(f"无法读取配置文件 {config_path}: {error}") from None


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).resolve()
    raw = _read_toml(config_path)
    errors: list[str] = []

    for key in sorted(set(raw) - _TOP_LEVEL_KEYS):
        errors.append(f"未知顶级配置块: {key}")

    jmeter_raw = _get_table(raw, "jmeter", errors)
    scripts_raw = _get_table(raw, "scripts", errors)
    schedule_raw = _get_table(raw, "schedule", errors)
    output_raw = _get_table(raw, "output", errors)
    report_raw = _get_optional_table(raw, "report", errors)
    dingtalk_raw = _get_optional_table(raw, "dingtalk", errors)
    _add_unknown_key_errors(jmeter_raw, _JMETER_KEYS, "jmeter", errors)
    _add_unknown_key_errors(scripts_raw, _SCRIPTS_KEYS, "scripts", errors)
    _add_unknown_key_errors(
        schedule_raw,
        _SCHEDULE_KEYS,
        "schedule",
        errors,
    )
    _add_unknown_key_errors(output_raw, _OUTPUT_KEYS, "output", errors)
    _add_unknown_key_errors(report_raw, _REPORT_KEYS, "report", errors)
    _add_unknown_key_errors(
        dingtalk_raw,
        _DINGTALK_KEYS,
        "dingtalk",
        errors,
    )

    dingtalk_enabled = _get_optional_boolean(
        dingtalk_raw,
        "enabled",
        "dingtalk",
        errors,
    )
    if dingtalk_enabled:
        dingtalk_client_id = _get_required_string(
            dingtalk_raw,
            "client_id",
            "dingtalk",
            errors,
        ) or ""
        dingtalk_client_secret = _get_required_string(
            dingtalk_raw,
            "client_secret",
            "dingtalk",
            errors,
        ) or ""
    else:
        dingtalk_client_id = _get_disabled_credential(
            dingtalk_raw,
            "client_id",
            errors,
        )
        dingtalk_client_secret = _get_disabled_credential(
            dingtalk_raw,
            "client_secret",
            errors,
        )

    try:
        excluded_url_keywords = parse_excluded_url_keywords(
            report_raw.get("excluded_url_keywords")
        )
    except ReportFilterConfigError as error:
        errors.append(str(error))
        excluded_url_keywords = ()
    try:
        additional_date_parameter_names = (
            parse_additional_date_parameter_names(
                report_raw.get("additional_date_parameter_names")
            )
        )
    except DateParameterConfigError as error:
        errors.append(str(error))
        additional_date_parameter_names = ()

    base_directory = config_path.parent
    executable_value = _get_required_string(
        jmeter_raw,
        "executable",
        "jmeter",
        errors,
    )
    properties_file_value = _get_required_string(
        jmeter_raw,
        "properties_file",
        "jmeter",
        errors,
    )
    scripts_directory_value = _get_required_string(
        scripts_raw,
        "directory",
        "scripts",
        errors,
    )
    timeout_seconds = _get_required_positive_integer(
        scripts_raw,
        "timeout_seconds",
        "scripts",
        errors,
    )
    timezone_value = _get_required_string(
        schedule_raw,
        "timezone",
        "schedule",
        errors,
    )
    cron_value = _get_required_string(
        schedule_raw,
        "cron",
        "schedule",
        errors,
    )
    output_directory_value = _get_required_string(
        output_raw,
        "directory",
        "output",
        errors,
    )

    if cron_value is not None:
        if len(cron_value.split()) != 5:
            errors.append("schedule.cron 必须恰好包含 5 段")
        else:
            try:
                CronTrigger.from_crontab(cron_value)
            except ValueError as error:
                errors.append(
                    f"schedule.cron 语法无效: {cron_value} ({error})"
                )
    if timezone_value is not None:
        try:
            ZoneInfo(timezone_value)
        except (ZoneInfoNotFoundError, ValueError):
            errors.append(f"未知时区: {timezone_value}")

    executable = (
        _resolve_path(base_directory, executable_value)
        if executable_value is not None
        else None
    )
    properties_file = (
        _resolve_path(base_directory, properties_file_value)
        if properties_file_value is not None
        else None
    )
    scripts_directory = (
        _absolute_path(base_directory, scripts_directory_value)
        if scripts_directory_value is not None
        else None
    )
    output_root = (
        _resolve_path(base_directory, output_directory_value)
        if output_directory_value is not None
        else None
    )
    if executable is not None:
        _validate_cmd_safe_path(executable, "jmeter.executable", errors)
    if properties_file is not None:
        _validate_cmd_safe_path(
            properties_file,
            "jmeter.properties_file",
            errors,
        )
    if output_root is not None:
        _validate_cmd_safe_path(output_root, "output.directory", errors)
    _validate_readable_file(executable, "jmeter.executable", errors)
    _validate_readable_file(
        properties_file,
        "jmeter.properties_file",
        errors,
    )
    scripts = (
        _discover_scripts(scripts_directory, timeout_seconds, errors)
        if scripts_directory is not None
        else ()
    )
    _validate_output_root(output_root, errors)

    if errors:
        details = "\n".join(f"- {error}" for error in errors)
        raise ConfigError(f"配置校验失败：\n{details}")

    assert executable is not None
    assert properties_file is not None
    assert timezone_value is not None
    assert cron_value is not None
    assert output_root is not None

    return AppConfig(
        jmeter=JMeterConfig(
            executable=executable,
            properties_file=properties_file,
        ),
        schedule=ScheduleConfig(
            timezone=timezone_value,
            cron=cron_value,
        ),
        runner=RunnerConfig(
            output_root=output_root,
            default_timeout_seconds=timeout_seconds,
        ),
        scripts=scripts,
        report=ReportConfig(
            excluded_url_keywords=excluded_url_keywords,
            additional_date_parameter_names=(
                additional_date_parameter_names
            ),
        ),
        dingtalk=DingTalkConfig(
            enabled=dingtalk_enabled,
            client_id=dingtalk_client_id,
            client_secret=dingtalk_client_secret,
        ),
    )


def validate_jmeter_executable(
    config: AppConfig,
    cancel_event: Event | None = None,
    *,
    popen_factory: Callable[..., Any] = subprocess.Popen,
    terminate_tree: Callable[
        [Any], ProcessTreeTerminationResult
    ] = terminate_process_tree,
    monotonic: Callable[[], float] = time.monotonic,
) -> str:
    """Run ``jmeter -v`` without creating logs in the project workspace."""
    with tempfile.TemporaryDirectory(
        prefix="jmeter-suite-validate-"
    ) as temporary_directory:
        log_path = Path(temporary_directory) / "jmeter-version.log"
        log_path_errors: list[str] = []
        _validate_cmd_safe_path(
            log_path,
            "JMeter 临时日志路径",
            log_path_errors,
        )
        if log_path_errors:
            raise ConfigError(log_path_errors[0])
        command = [
            str(config.jmeter.executable),
            "-v",
            "-j",
            str(log_path),
        ]
        try:
            process = popen_factory(
                command,
                cwd=config.jmeter.executable.parent,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                creationflags=(
                    subprocess.CREATE_NEW_PROCESS_GROUP
                    if os.name == "nt"
                    else 0
                ),
            )
        except OSError as error:
            raise ConfigError(f"无法启动 JMeter: {error}") from None

        started = monotonic()
        while True:
            if cancel_event is not None and cancel_event.is_set():
                termination = terminate_tree(process)
                raise JMeterValidationCancelled(termination)
            remaining = 15 - (monotonic() - started)
            if remaining <= 0:
                termination = terminate_tree(process)
                detail = (
                    ""
                    if termination.confirmed
                    else f"；进程树清理未确认: {termination.error}"
                )
                raise ConfigError(
                    f"JMeter 版本检查超时（15 秒）{detail}"
                )
            try:
                stdout, stderr = process.communicate(
                    timeout=min(0.2, remaining)
                )
            except subprocess.TimeoutExpired:
                continue
            break

    stdout = stdout.strip()
    stderr = stderr.strip()
    if process.returncode != 0:
        detail = stderr or stdout or f"退出码 {process.returncode}"
        raise ConfigError(f"JMeter 版本检查失败: {detail}")

    output = "\n".join(part for part in (stdout, stderr) if part)
    for pattern in (
        r"\bVersion\s+(\d+(?:\.\d+){1,3}(?:[-._A-Za-z0-9]*)?)",
        r"\bApache JMeter\s+(\d+(?:\.\d+){1,3}(?:[-._A-Za-z0-9]*)?)",
        (
            r"^/_/\s+\\_\\_\|\s+/_/[^\r\n]*?[ \t]+"
            r"(\d+(?:\.\d+){1,3}(?:[-._A-Za-z0-9]*)?)[ \t]*$"
        ),
    ):
        match = re.search(
            pattern,
            output,
            flags=re.IGNORECASE | re.MULTILINE,
        )
        if match:
            return match.group(1)
    raise ConfigError("无法从 JMeter -v 输出识别版本")
