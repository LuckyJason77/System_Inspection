"""使用真实 JMeter 与本地 HTTP 服务验证串行执行和离线报告全链路。"""

import base64
from collections.abc import Iterator
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import threading

import pytest

from jmeter_suite.application import run_once_application
from jmeter_suite.config import ConfigError, load_config


FIXTURES = Path(__file__).parent / "fixtures" / "integration"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
PASS_REQUEST = "通过链路完整中文请求正文-永久保留"
FAIL_REQUEST = "失败链路完整中文请求正文-永久保留"
RESPONSE_PREFIX = "服务端完整中文响应"


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


@pytest.fixture(scope="session")
def jmeter_executable() -> Path:
    candidates: list[Path] = []
    jmeter_home = os.environ.get("JMETER_HOME")
    if jmeter_home:
        candidates.append(Path(jmeter_home) / "bin" / "jmeter.bat")
    app_config = PROJECT_ROOT / "config" / "app.toml"
    if app_config.is_file():
        try:
            candidates.append(load_config(app_config).jmeter.executable)
        except ConfigError:
            pass
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    pytest.skip(
        "未找到 JMeter：请设置 JMETER_HOME，或在 config/app.toml "
        "中配置有效的 jmeter.executable"
    )


@pytest.fixture
def local_http_server() -> Iterator[
    tuple[str, list[tuple[str, str, str]]]
]:
    received: list[tuple[str, str, str]] = []
    received_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:
            content_length = int(self.headers.get("Content-Length", "0"))
            request_body = self.rfile.read(content_length).decode("utf-8")
            case_header = self.headers.get("X-Integration-Case", "")
            with received_lock:
                received.append((self.path, request_body, case_header))

            response_text = (
                f"{RESPONSE_PREFIX}-{self.path.removeprefix('/')}-"
                f"{request_body}"
            )
            response_body = response_text.encode("utf-8")
            self.send_response(200)
            self.send_header(
                "Content-Type",
                "text/plain; charset=UTF-8",
            )
            self.send_header("Content-Length", str(len(response_body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(response_body)
            self.close_connection = True

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}", received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


def _toml_string(value: str | Path) -> str:
    return json.dumps(str(value).replace("\\", "/"), ensure_ascii=False)


def _write_app_config(
    directory: Path,
    *,
    jmeter_executable: Path,
    base_url: str,
    output_root: Path,
    scripts: tuple[tuple[str, str], ...],
) -> Path:
    directory.mkdir(parents=True)
    scripts_directory = directory / "jmx"
    scripts_directory.mkdir()
    expected_script_names = []
    base_url_placeholder = "${__P(base_url)}"
    for target_name, fixture_name in scripts:
        source_path = FIXTURES / fixture_name
        source_bytes = source_path.read_bytes()
        source_text = source_bytes.decode("utf-8")
        assert base_url_placeholder in source_text
        target_path = scripts_directory / target_name
        target_path.write_text(
            source_text.replace(base_url_placeholder, base_url),
            encoding="utf-8",
        )
        assert source_path.read_bytes() == source_bytes
        assert base_url_placeholder not in target_path.read_text(
            encoding="utf-8"
        )
        expected_script_names.append(target_name)

    script_entries = sorted(
        scripts_directory.iterdir(),
        key=lambda path: (path.name.casefold(), path.name),
    )
    assert [path.name for path in script_entries] == expected_script_names
    assert all(
        path.is_file()
        and not path.is_symlink()
        and path.suffix.casefold() == ".jmx"
        for path in script_entries
    )

    config_path = directory / "config.toml"
    config_path.write_text(
        f"""\
[jmeter]
executable = {_toml_string(jmeter_executable)}
properties_file = {_toml_string(jmeter_executable.parent / "jmeter.properties")}

[scripts]
directory = "jmx"
timeout_seconds = 60

[schedule]
timezone = "Asia/Shanghai"
cron = "0 2 * * *"

[output]
directory = {_toml_string(output_root)}
""",
        encoding="utf-8",
    )
    return config_path


def _only_run_directory(output_root: Path) -> Path:
    run_directories = [
        path
        for path in output_root.iterdir()
        if path.is_dir() and path.name != ".runtime"
    ]
    assert len(run_directories) == 1
    return run_directories[0]


def _report_html(run_directory: Path, script: dict[str, object]) -> str:
    paths = script["paths"]
    assert isinstance(paths, dict)
    report_path = paths["report"]
    assert isinstance(report_path, str)
    assert re.fullmatch(
        r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_Inspection_Report"
        r"(?:-\d{2})?/report\.html",
        report_path,
    )
    return (run_directory / report_path).read_text(encoding="utf-8")


@pytest.mark.integration
def test_real_jmeter_runs_serially_and_generates_raw_offline_reports(
    tmp_path: Path,
    jmeter_executable: Path,
    local_http_server: tuple[str, list[tuple[str, str, str]]],
    capsys: pytest.CaptureFixture[str],
):
    base_url, received = local_http_server
    passed_output_root = tmp_path / "passed-runs"
    passed_config = _write_app_config(
        tmp_path / "passed-config",
        jmeter_executable=jmeter_executable,
        base_url=base_url,
        output_root=passed_output_root,
        scripts=(("01-仅通过 JMX 的套件.jmx", "passing-http.jmx"),),
    )
    assert run_once_application(passed_config) == 0
    passed_console = capsys.readouterr()

    failed_output_root = tmp_path / "failed-runs"
    failed_config = _write_app_config(
        tmp_path / "failed-config",
        jmeter_executable=jmeter_executable,
        base_url=base_url,
        output_root=failed_output_root,
        scripts=(
            (
                "01-先执行真实断言失败链路.jmx",
                "failing-assertion-http.jmx",
            ),
            ("02-失败后继续真实通过链路.jmx", "passing-http.jmx"),
        ),
    )
    assert run_once_application(failed_config) == 1
    failed_console = capsys.readouterr()

    assert passed_console.err == ""
    assert "套件状态: 通过" in passed_console.out
    assert failed_console.err == ""
    assert "套件状态: 失败" in failed_console.out

    passed_run = _only_run_directory(passed_output_root)
    failed_run = _only_run_directory(failed_output_root)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}", passed_run.name)
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}", failed_run.name)
    assert f"运行 ID: {passed_run.name}" in passed_console.out
    assert f"运行 ID: {failed_run.name}" in failed_console.out
    assert (passed_output_root / ".runtime" / "suite.lock").is_file()
    assert (failed_output_root / ".runtime" / "suite.lock").is_file()
    passed_manifest_text = (passed_run / "manifest.json").read_text(
        encoding="utf-8"
    )
    failed_manifest_text = (failed_run / "manifest.json").read_text(
        encoding="utf-8"
    )
    passed_manifest = json.loads(passed_manifest_text)
    failed_manifest = json.loads(failed_manifest_text)

    assert passed_manifest["run_id"] == passed_run.name
    assert failed_manifest["run_id"] == failed_run.name
    passed_report_path = passed_run / passed_manifest["report_path"]
    failed_report_path = failed_run / failed_manifest["report_path"]
    assert f"<title>{passed_run.name} - 自动化巡检</title>" in (
        passed_report_path.read_text(encoding="utf-8")
    )
    assert f"<title>{failed_run.name} - 自动化巡检</title>" in (
        failed_report_path.read_text(encoding="utf-8")
    )
    assert passed_manifest["status"] == "passed"
    assert [
        script["status"] for script in passed_manifest["scripts"]
    ] == ["passed"]
    assert [
        script["exit_code"] for script in passed_manifest["scripts"]
    ] == [0]

    assert [
        script["name"] for script in failed_manifest["scripts"]
    ] == [
        "01-先执行真实断言失败链路",
        "02-失败后继续真实通过链路",
    ]
    assert [
        script["status"] for script in failed_manifest["scripts"]
    ] == ["failed", "passed"]
    assert [
        script["exit_code"] for script in failed_manifest["scripts"]
    ] == [0, 0]
    assert (
        failed_manifest["scripts"][0]["finished_at"]
        <= failed_manifest["scripts"][1]["started_at"]
    )
    assert received == [
        ("/pass", PASS_REQUEST, "pass-header-secret"),
        ("/fail", FAIL_REQUEST, "fail-header-secret"),
        ("/pass", PASS_REQUEST, "pass-header-secret"),
    ]
    assert failed_manifest["status"] == "failed"
    assert failed_manifest["scripts"][0]["assertion_failures"] == 1
    assert failed_manifest["scripts"][0]["failed_samples"] == 1
    assert failed_manifest["scripts"][1]["assertion_failures"] == 0
    for run_directory, manifest in (
        (passed_run, passed_manifest),
        (failed_run, failed_manifest),
    ):
        report_path = run_directory / manifest["report_path"]
        assert report_path.is_file()
        assert list(run_directory.rglob("*.html")) == [report_path]
        assert {
            path.name
            for path in report_path.parent.iterdir()
        } == {"report.html"}
        for script in manifest["scripts"]:
            assert script["paths"]["report"] == manifest["report_path"]
            jtl_path = run_directory / script["paths"]["jtl"]
            assert jtl_path.is_file()
            assert jtl_path.stat().st_size > 0

    passing_html = _report_html(
        passed_run,
        passed_manifest["scripts"][0],
    )
    failing_html = _report_html(
        failed_run,
        failed_manifest["scripts"][0],
    )
    assert _decode_compressed_payloads(passing_html) == [
        {
            "request": PASS_REQUEST,
            "response": f"{RESPONSE_PREFIX}-pass-{PASS_REQUEST}",
        }
    ]
    assert PASS_REQUEST not in passing_html
    assert f"{RESPONSE_PREFIX}-pass-{PASS_REQUEST}" not in passing_html
    assert "pass-header-secret" not in passing_html
    assert f'data-raw-request>{FAIL_REQUEST}</pre>' in failing_html
    assert (
        f'data-raw-response>{RESPONSE_PREFIX}-fail-{FAIL_REQUEST}</pre>'
        in failing_html
    )
    assert "fail-header-secret" not in failing_html
    assert "故意失败的中文断言" in failing_html
    assert "永远不会出现的断言内容" in failing_html
    assert 'data-detail-toggle aria-expanded="true"' in failing_html
    assert ">收起<" in failing_html

    for manifest_text in (passed_manifest_text, failed_manifest_text):
        for raw_content in (
            PASS_REQUEST,
            FAIL_REQUEST,
            RESPONSE_PREFIX,
            "pass-header-secret",
            "fail-header-secret",
            "永远不会出现的断言内容",
        ):
            assert raw_content not in manifest_text
