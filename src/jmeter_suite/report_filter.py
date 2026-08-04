"""解析 URL 关键字过滤配置，并判断接口是否应从报告中排除。"""

from __future__ import annotations

from urllib.parse import urlsplit


class ReportFilterConfigError(ValueError):
    """Raised when report URL exclusion configuration is invalid."""


def parse_excluded_url_keywords(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ReportFilterConfigError(
            "report.excluded_url_keywords 必须是字符串数组"
        )

    keywords: list[str] = []
    for index, keyword in enumerate(value, start=1):
        if not isinstance(keyword, str) or not keyword.strip():
            raise ReportFilterConfigError(
                "report.excluded_url_keywords"
                f"[{index}] 必须是非空字符串"
            )
        keywords.append(keyword.strip())
    return tuple(keywords)


def url_without_query_or_fragment(url: str) -> str:
    value = url.strip()
    if not value:
        return ""
    try:
        parsed = urlsplit(value)
        return parsed._replace(query="", fragment="").geturl()
    except ValueError:
        return value.partition("#")[0].partition("?")[0]


def is_url_excluded(url: str, keywords: tuple[str, ...]) -> bool:
    target = url_without_query_or_fragment(url).casefold()
    if not target:
        return False
    return any(keyword.casefold() in target for keyword in keywords)
