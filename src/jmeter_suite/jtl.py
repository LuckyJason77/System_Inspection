"""流式解析 XML JTL，保留 HTTP 样本、断言和不完整文件中的可用结果。"""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import xml.etree.ElementTree as ET

from .models import (
    AssertionResult,
    JTLParseResult,
    SampleRecord,
    SampleSummary,
)


_SAMPLE_TAGS = {"httpSample", "sample"}


@dataclass(slots=True)
class _SampleFrame:
    sequence: int
    depth: int
    sample_type: str
    has_child_sample: bool = False


def _local_name(tag: str) -> str:
    if tag.startswith("{"):
        return tag.rsplit("}", 1)[-1]
    return tag


def _integer(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _is_true(value: str | None) -> bool:
    return value is not None and value.lower() == "true"


def _text(element: ET.Element) -> str:
    return "".join(element.itertext())


def _assertion(element: ET.Element) -> AssertionResult:
    values = {
        _local_name(child.tag): _text(child)
        for child in element
    }
    return AssertionResult(
        name=values.get("name", ""),
        failure=_is_true(values.get("failure")),
        error=_is_true(values.get("error")),
        failure_message=values.get("failureMessage", ""),
    )


def _record(element: ET.Element, frame: _SampleFrame) -> SampleRecord:
    child_text: dict[str, str] = {}
    assertions: list[AssertionResult] = []
    for child in element:
        child_name = _local_name(child.tag)
        if child_name == "assertionResult":
            assertions.append(_assertion(child))
        elif child_name not in _SAMPLE_TAGS:
            child_text[child_name] = _text(child)

    return SampleRecord(
        sample_id=f"sample-{frame.sequence:06d}",
        sequence=frame.sequence,
        depth=frame.depth,
        sample_type=frame.sample_type,
        label=element.get("lb", ""),
        success=_is_true(element.get("s")),
        is_leaf=not frame.has_child_sample,
        timestamp_ms=_integer(element.get("ts")),
        elapsed_ms=_integer(element.get("t")),
        response_code=element.get("rc", ""),
        response_message=element.get("rm", ""),
        thread_name=element.get("tn", ""),
        url=child_text.get("java.net.URL", ""),
        query_string=child_text.get("queryString", ""),
        request_headers=child_text.get("requestHeader", ""),
        sampler_data=child_text.get("samplerData", ""),
        response_headers=child_text.get("responseHeader", ""),
        response_data=child_text.get("responseData", ""),
        data_type=element.get("dt", ""),
        encoding=element.get("de", ""),
        assertions=tuple(assertions),
    )


def _summary(record: SampleRecord) -> SampleSummary:
    return SampleSummary(
        sample_id=record.sample_id,
        sequence=record.sequence,
        depth=record.depth,
        sample_type=record.sample_type,
        label=record.label,
        is_leaf=record.is_leaf,
        passed=record.passed,
        response_code=record.response_code,
        assertion_failed=any(
            not assertion.passed for assertion in record.assertions
        ),
    )


def _result(
    source_path: Path,
    summaries: list[SampleSummary],
    assertion_failures: int,
    *,
    complete: bool,
    error: str | None,
) -> JTLParseResult:
    ordered_summaries = tuple(
        sorted(summaries, key=lambda summary: summary.sequence)
    )
    leaf_summaries = tuple(
        summary for summary in ordered_summaries if summary.is_leaf
    )
    passed_leaf_samples = sum(
        summary.passed for summary in leaf_summaries
    )
    return JTLParseResult(
        source_path=source_path,
        summaries=ordered_summaries,
        complete=complete,
        error=error,
        total_samples=len(ordered_summaries),
        leaf_samples=len(leaf_summaries),
        passed_leaf_samples=passed_leaf_samples,
        failed_leaf_samples=len(leaf_summaries) - passed_leaf_samples,
        assertion_failures=assertion_failures,
    )


def parse_jtl(
    path: Path,
    *,
    on_sample: Callable[[SampleRecord], None] | None = None,
) -> JTLParseResult:
    summaries: list[SampleSummary] = []
    assertion_failures = 0
    frames: list[_SampleFrame] = []
    elements: list[ET.Element] = []
    sequence = 0

    try:
        events = iter(
            ET.iterparse(path, events=("start", "end"))
        )
    except OSError as error:
        return _result(
            path,
            summaries,
            assertion_failures,
            complete=False,
            error=f"Unable to read JTL: {error}",
        )

    while True:
        try:
            event, element = next(events)
        except StopIteration:
            return _result(
                path,
                summaries,
                assertion_failures,
                complete=True,
                error=None,
            )
        except ET.ParseError as error:
            return _result(
                path,
                summaries,
                assertion_failures,
                complete=False,
                error=f"XML parse error: {error}",
            )
        except OSError as error:
            return _result(
                path,
                summaries,
                assertion_failures,
                complete=False,
                error=f"Unable to read JTL: {error}",
            )

        element_name = _local_name(element.tag)

        if event == "start":
            elements.append(element)
            if element_name not in _SAMPLE_TAGS:
                continue

            sequence += 1
            if frames:
                frames[-1].has_child_sample = True
            frames.append(
                _SampleFrame(
                    sequence=sequence,
                    depth=len(frames),
                    sample_type=element_name,
                )
            )
            continue

        parent = elements[-2] if len(elements) > 1 else None
        if element_name in _SAMPLE_TAGS:
            frame = frames.pop()
            record = _record(element, frame)
            if on_sample is not None:
                on_sample(record)
            summaries.append(_summary(record))
            assertion_failures += sum(
                not assertion.passed for assertion in record.assertions
            )
            if parent is not None:
                parent.remove(element)
            element.clear()
        elif not frames:
            if parent is not None:
                parent.remove(element)
            element.clear()
        elements.pop()
