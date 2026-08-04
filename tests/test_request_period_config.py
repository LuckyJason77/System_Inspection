"""验证报告可通过配置补充特殊日期参数名。"""

from jmeter_suite.request_period import (
    QueryDataRange,
    analyze_query_data_range,
)


def test_configured_account_period_is_recognized_as_single_month():
    raw_parameters = "account_period=2026-08&page=1"

    assert analyze_query_data_range(raw_parameters).has_time_condition is False
    assert analyze_query_data_range(
        raw_parameters,
        additional_date_parameter_names=("ACCOUNT_PERIOD",),
    ) == QueryDataRange("2026-08")
