"""从请求参数中识别日期字段并生成可读的查询数据范围。"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
import json
import re
from typing import Any
from urllib.parse import parse_qsl


_DATE_TOKEN = re.compile(
    r"(?<!\d)(?P<year>(?:19|20|21)\d{2})"
    r"(?:-(?P<month>\d{2})(?:-(?P<day>\d{2}))?)?(?!\d)"
)
_IGNORED_FIELDS = {
    "group_time_type",
    "time_field",
    "time_model",
    "time_scope",
    "time_type",
    "time_type1",
}


@dataclass(frozen=True, slots=True)
class QueryDataRange:
    text: str
    invalid: bool = False
    has_time_condition: bool = True


@dataclass(frozen=True, slots=True)
class _DateValue:
    year: int
    month: int | None
    day: int | None
    valid: bool

    @property
    def period(self) -> str:
        if self.month is None:
            return f"{self.year:04d}"
        return f"{self.year:04d}-{self.month:02d}"

    @property
    def order_key(self) -> tuple[int, int, int]:
        return (self.year, self.month or 1, self.day or 1)


def _normalized_field(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_")


def _is_period_field(name: str) -> bool:
    normalized = _normalized_field(name)
    if not normalized or normalized in _IGNORED_FIELDS:
        return False
    if normalized == "daterange":
        return True
    if "month" in normalized or normalized == "year":
        return True
    has_direction = "start" in normalized or "end" in normalized
    has_time_meaning = any(
        marker in normalized
        for marker in ("date", "day", "week", "time", "_at")
    )
    return has_direction and has_time_meaning


def _flatten_json(value: Any, prefix: str = "") -> list[tuple[str, str]]:
    if isinstance(value, dict):
        flattened: list[tuple[str, str]] = []
        for key, child in value.items():
            child_name = f"{prefix}.{key}" if prefix else str(key)
            flattened.extend(_flatten_json(child, child_name))
        return flattened
    if isinstance(value, list):
        if prefix and all(not isinstance(item, (dict, list)) for item in value):
            return [(prefix, json.dumps(value, ensure_ascii=False))]
        flattened = []
        for index, child in enumerate(value):
            flattened.extend(_flatten_json(child, f"{prefix}[{index}]"))
        return flattened
    if not prefix:
        return []
    if value is None:
        return [(prefix, "")]
    if isinstance(value, bool):
        return [(prefix, "true" if value else "false")]
    return [(prefix, str(value))]


def _parameters(raw: str) -> list[tuple[str, str]]:
    stripped = raw.strip()
    if not stripped:
        return []
    if stripped.startswith(("{", "[")):
        try:
            return _flatten_json(json.loads(stripped))
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return parse_qsl(stripped.lstrip("?"), keep_blank_values=True)


def _date_values(value: str) -> list[_DateValue]:
    values: list[_DateValue] = []
    for match in _DATE_TOKEN.finditer(value):
        year = int(match.group("year"))
        month_text = match.group("month")
        day_text = match.group("day")
        month = int(month_text) if month_text is not None else None
        day = int(day_text) if day_text is not None else None
        valid = True
        if month is not None and not 1 <= month <= 12:
            valid = False
        elif day is not None:
            assert month is not None
            valid = 1 <= day <= calendar.monthrange(year, month)[1]
        values.append(
            _DateValue(
                year=year,
                month=month if month is not None and 1 <= month <= 12 else None,
                day=day,
                valid=valid,
            )
        )
    return values


def _with_warning(text: str, invalid: bool) -> QueryDataRange:
    if invalid:
        return QueryDataRange(f"{text}（日期参数异常）", invalid=True)
    return QueryDataRange(text)


def analyze_query_data_range(raw_parameters: str) -> QueryDataRange:
    candidates: list[tuple[str, str]] = []
    for name, value in _parameters(raw_parameters):
        if _is_period_field(name):
            candidates.append((_normalized_field(name), value))

    if not candidates:
        return QueryDataRange(
            "未携带时间条件",
            has_time_condition=False,
        )

    starts: list[_DateValue] = []
    ends: list[_DateValue] = []
    neutral: list[_DateValue] = []
    invalid = False
    unresolved = False

    for name, value in candidates:
        values = _date_values(value)
        if not values:
            unresolved = unresolved or "${" in value or bool(value.strip())
            continue
        invalid = invalid or any(not item.valid for item in values)
        if name == "daterange" and len(values) >= 2:
            starts.append(values[0])
            ends.append(values[-1])
        elif "start" in name:
            starts.append(values[0])
        elif "end" in name:
            ends.append(values[-1])
        else:
            neutral.extend(values)

    if starts and ends:
        start = starts[0]
        end = ends[0]
        invalid = invalid or start.order_key > end.order_key
        if start.month is None and end.month is None and start.year == end.year:
            return _with_warning(f"{start.year:04d} 全年", invalid)
        if start.period == end.period:
            return _with_warning(start.period, invalid)
        return _with_warning(f"{start.period} ～ {end.period}", invalid)

    values = starts or ends or neutral
    if values:
        first = values[0]
        if first.month is None:
            return _with_warning(f"{first.year:04d} 全年", invalid)
        unique_periods = list(dict.fromkeys(item.period for item in values))
        if len(unique_periods) == 1:
            return _with_warning(unique_periods[0], invalid)
        return _with_warning(
            f"{unique_periods[0]} ～ {unique_periods[-1]}",
            invalid,
        )

    if unresolved:
        return QueryDataRange("无法识别")
    return QueryDataRange("无法识别")
