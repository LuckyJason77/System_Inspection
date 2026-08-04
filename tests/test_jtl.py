"""验证 XML JTL 流式解析、断言统计、容错和大响应保真。"""

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import jmeter_suite.jtl as jtl_module
from jmeter_suite.jtl import parse_jtl
from jmeter_suite.models import AssertionResult, SampleRecord


FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_http_sample_preserves_fields_and_assertion_semantics():
    records: list[SampleRecord] = []

    result = parse_jtl(
        FIXTURES / "single-http.jtl",
        on_sample=records.append,
    )

    assert len(records) == 1
    record = records[0]
    assert record == SampleRecord(
        sample_id="sample-000001",
        sequence=1,
        depth=0,
        sample_type="httpSample",
        label="登录 & 查询",
        success=True,
        is_leaf=True,
        timestamp_ms=1722398765432,
        elapsed_ms=345,
        response_code="200",
        response_message="OK & ready",
        thread_name="线程组 1-1",
        url="https://example.test/登录?x=1&y=二",
        query_string='{"用户名":"张三","html":"</pre><script>"}',
        request_headers="Content-Type: application/json\nX-原样: 值",
        sampler_data=(
            "POST https://example.test/登录\n"
            '{"用户名":"张三"}'
        ),
        response_headers="HTTP/1.1 200 OK\nX-Note: 中文",
        response_data=(
            "第一行\n"
            '</pre><script>alert("x")</script>\n'
            "最后一行"
        ),
        data_type="text",
        encoding="UTF-8",
        assertions=(
            AssertionResult(
                name="状态断言",
                failure=False,
                error=False,
                failure_message="",
            ),
            AssertionResult(
                name="正文断言",
                failure=True,
                error=False,
                failure_message="应包含欢迎语\n第二行",
            ),
        ),
    )
    assert record.assertions[0].passed is True
    assert record.assertions[1].passed is False
    assert record.passed is False
    with pytest.raises(FrozenInstanceError):
        record.label = "changed"

    assert result.source_path == FIXTURES / "single-http.jtl"
    assert result.complete is True
    assert result.error is None
    assert result.total_samples == 1
    assert result.leaf_samples == 1
    assert result.passed_leaf_samples == 0
    assert result.failed_leaf_samples == 1
    assert result.assertion_failures == 1
    assert result.passed is False
    assert len(result.summaries) == 1
    summary = result.summaries[0]
    assert summary.sample_id == "sample-000001"
    assert summary.sequence == 1
    assert summary.depth == 0
    assert summary.sample_type == "httpSample"
    assert summary.label == "登录 & 查询"
    assert summary.is_leaf is True
    assert summary.passed is False
    assert summary.response_code == "200"
    assert summary.assertion_failed is True


def test_nested_namespaced_samples_stream_in_end_order_and_summarize_in_start_order():
    records: list[SampleRecord] = []

    result = parse_jtl(
        FIXTURES / "nested-namespaced.jtl",
        on_sample=records.append,
    )

    assert [record.sequence for record in records] == [2, 1, 3]
    assert [record.sample_id for record in records] == [
        "sample-000002",
        "sample-000001",
        "sample-000003",
    ]
    assert [record.depth for record in records] == [1, 0, 0]
    assert [record.sample_type for record in records] == [
        "sample",
        "httpSample",
        "sample",
    ]
    assert [record.is_leaf for record in records] == [
        True,
        False,
        True,
    ]
    assert records[0].label == "非 HTTP 子样本"
    assert records[0].response_data == "child 中文"
    assert records[1].success is False

    assert [
        summary.sequence for summary in result.summaries
    ] == [1, 2, 3]
    assert [
        summary.sample_id for summary in result.summaries
    ] == [
        "sample-000001",
        "sample-000002",
        "sample-000003",
    ]
    assert [
        summary.depth for summary in result.summaries
    ] == [0, 1, 0]
    assert [
        summary.is_leaf for summary in result.summaries
    ] == [False, True, True]
    assert result.total_samples == 3
    assert result.leaf_samples == 2
    assert result.passed_leaf_samples == 2
    assert result.failed_leaf_samples == 0
    assert result.assertion_failures == 0
    assert result.complete is True
    assert result.passed is True


def test_failed_parent_assertion_is_counted_without_failing_leaf_counter(
    tmp_path: Path,
):
    path = tmp_path / "parent-assertion.jtl"
    path.write_text(
        """\
<testResults>
  <sample lb="transaction" s="true">
    <assertionResult>
      <name>父级断言</name>
      <failure>false</failure>
      <error>TrUe</error>
      <failureMessage>父事务校验异常</failureMessage>
    </assertionResult>
    <httpSample lb="leaf" s="true" rc="204" />
  </sample>
</testResults>
""",
        encoding="utf-8",
    )
    records: list[SampleRecord] = []

    result = parse_jtl(path, on_sample=records.append)

    assert [record.sequence for record in records] == [2, 1]
    assert records[0].passed is True
    assert records[1].is_leaf is False
    assert records[1].passed is False
    assert result.summaries[0].assertion_failed is True
    assert result.summaries[1].assertion_failed is False
    assert result.leaf_samples == 1
    assert result.passed_leaf_samples == 1
    assert result.failed_leaf_samples == 0
    assert result.assertion_failures == 1
    assert result.passed is False


def test_missing_and_invalid_attributes_use_safe_failed_defaults():
    records: list[SampleRecord] = []

    result = parse_jtl(
        FIXTURES / "invalid-values.jtl",
        on_sample=records.append,
    )

    assert [record.success for record in records] == [False, False]
    assert [record.timestamp_ms for record in records] == [None, None]
    assert [record.elapsed_ms for record in records] == [None, None]
    assert records[0].response_code == ""
    assert records[0].response_message == ""
    assert records[0].thread_name == ""
    assert records[0].url == ""
    assert records[0].query_string == ""
    assert records[0].request_headers == ""
    assert records[0].sampler_data == ""
    assert records[0].response_headers == ""
    assert records[0].response_data == ""
    assert records[0].data_type == ""
    assert records[0].encoding == ""
    assert records[0].assertions == ()
    assert result.complete is True
    assert result.total_samples == 2
    assert result.leaf_samples == 2
    assert result.passed_leaf_samples == 0
    assert result.failed_leaf_samples == 2
    assert result.passed is False


def test_malformed_xml_returns_completed_samples_as_partial_result():
    records: list[SampleRecord] = []

    result = parse_jtl(
        FIXTURES / "malformed-partial.jtl",
        on_sample=records.append,
    )

    assert [record.label for record in records] == ["已完成"]
    assert records[0].response_data == "完整样本"
    assert result.source_path == FIXTURES / "malformed-partial.jtl"
    assert result.complete is False
    assert result.error is not None
    assert result.error.startswith("XML parse error:")
    assert result.total_samples == 1
    assert result.leaf_samples == 1
    assert result.passed_leaf_samples == 1
    assert result.failed_leaf_samples == 0
    assert result.assertion_failures == 0
    assert [summary.label for summary in result.summaries] == ["已完成"]
    assert result.passed is False


def test_missing_jtl_returns_incomplete_result_instead_of_raising(
    tmp_path: Path,
):
    missing_path = tmp_path / "missing-result.jtl"

    result = parse_jtl(missing_path)

    assert result.source_path == missing_path
    assert result.summaries == ()
    assert result.complete is False
    assert result.error is not None
    assert result.error.startswith("Unable to read JTL:")
    assert "missing-result.jtl" in result.error
    assert result.total_samples == 0
    assert result.leaf_samples == 0
    assert result.passed_leaf_samples == 0
    assert result.failed_leaf_samples == 0
    assert result.assertion_failures == 0
    assert result.passed is False


def test_well_formed_jtl_without_samples_is_complete_but_not_passed():
    result = parse_jtl(FIXTURES / "no-samples.jtl")

    assert result.complete is True
    assert result.error is None
    assert result.summaries == ()
    assert result.total_samples == 0
    assert result.leaf_samples == 0
    assert result.passed is False


def test_consumer_oserror_propagates_without_becoming_parse_result():
    consumer_error = OSError("report destination is full")

    def fail_consumer(record: SampleRecord) -> None:
        raise consumer_error

    with pytest.raises(OSError) as raised:
        parse_jtl(
            FIXTURES / "single-http.jtl",
            on_sample=fail_consumer,
        )

    assert raised.value is consumer_error


def test_large_response_round_trips_without_tree_parse_or_truncation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    response_body = (
        "开头中文\n</pre><script>alert('x')</script>\n"
        + ("中文0123456789\n" * 90_000)
        + "结尾中文"
    )
    path = tmp_path / "large-response.jtl"
    path.write_text(
        (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<testResults version="1.2">\n'
            '  <httpSample lb="大正文" s="true" rc="200">\n'
            "    <responseData><![CDATA["
            + response_body
            + "]]></responseData>\n"
            "  </httpSample>\n"
            "</testResults>\n"
        ),
        encoding="utf-8",
    )

    def reject_tree_parse(*args: object, **kwargs: object) -> None:
        pytest.fail("parse_jtl must not call ElementTree.parse")

    monkeypatch.setattr(jtl_module.ET, "parse", reject_tree_parse)
    records: list[SampleRecord] = []

    result = jtl_module.parse_jtl(path, on_sample=records.append)

    assert result.complete is True
    assert result.passed is True
    assert len(records) == 1
    assert len(records[0].response_data) == 1_170_043
    assert records[0].response_data.startswith(
        "开头中文\n</pre><script>alert('x')</script>\n"
    )
    assert records[0].response_data.endswith(
        "中文0123456789\n结尾中文"
    )
    assert records[0].response_data == response_body


def test_completed_samples_and_wrappers_are_detached_from_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    path = tmp_path / "many-samples.jtl"
    top_level_samples = "".join(
        (
            f'<httpSample lb="top-{index:04d}" '
            's="true" rc="200" />\n'
        )
        for index in range(500)
    )
    wrapped_samples = "".join(
        (
            f'<sample lb="wrapped-{index:04d}" '
            's="true" rc="200" />\n'
        )
        for index in range(500)
    )
    path.write_text(
        (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            "<testResults>\n"
            + top_level_samples
            + "<resultWrapper>\n"
            + wrapped_samples
            + "</resultWrapper>\n"
            "</testResults>\n"
        ),
        encoding="utf-8",
    )

    real_iterparse = jtl_module.ET.iterparse
    captured_roots: list[jtl_module.ET.Element] = []

    def observing_iterparse(
        source: Path,
        events: tuple[str, str],
    ):
        for event, element in real_iterparse(source, events=events):
            if event == "start" and not captured_roots:
                captured_roots.append(element)
            yield event, element

    monkeypatch.setattr(
        jtl_module.ET,
        "iterparse",
        observing_iterparse,
    )

    result = jtl_module.parse_jtl(path)

    assert result.complete is True
    assert result.total_samples == 1000
    assert len(result.summaries) == 1000
    assert result.summaries[0].label == "top-0000"
    assert result.summaries[499].label == "top-0499"
    assert result.summaries[500].label == "wrapped-0000"
    assert result.summaries[-1].label == "wrapped-0499"
    assert len(captured_roots) == 1
    assert captured_roots[0].tag == "testResults"
    assert len(captured_roots[0]) == 0
