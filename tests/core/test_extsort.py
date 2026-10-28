"""Тест 1 (частично) спеки `docs/architecture.md`: `ext_sort`/`slack_ts_to_ms`."""

from hub.core.extsort import ext_sort, is_slack_ts, slack_ts_to_ms


def test_telegram_numeric_ext_id_unchanged():
    assert ext_sort("1") == 1
    assert ext_sort("100") == 100
    assert ext_sort("99") == 99
    assert ext_sort("100") > ext_sort("99")


def test_garbage_and_none_give_zero():
    assert ext_sort(None) == 0
    assert ext_sort("") == 0
    assert ext_sort("abc") == 0
    assert ext_sort("1" * 19) == 0  # длиннее 18 цифр


def test_slack_ts_order():
    # "…9.999999" < "…10.000000" — порядок по (секунды, микросекунды), не по строке.
    a = ext_sort("1700000009.999999")
    b = ext_sort("1700000010.000000")
    assert a < b


def test_slack_ts_to_ms():
    assert slack_ts_to_ms("1700000000.123456") == 1700000000_000 + 123
    assert slack_ts_to_ms("1.000000") == 1000


def test_is_slack_ts():
    assert is_slack_ts("1700000000.123456")
    assert not is_slack_ts("12345")
    assert not is_slack_ts("abc")
    assert not is_slack_ts("1700000000.12345")  # 5 знаков дроби — не формат Slack
