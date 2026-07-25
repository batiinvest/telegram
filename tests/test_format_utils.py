# -*- coding: utf-8 -*-
"""format_utils characterization 테스트 — 현재 동작을 그대로 고정.

프론트 config.js(chgStr/fmtCap/fmtNet)와 정합을 이루는 포맷 로직이므로,
리팩토링 시 출력이 1글자라도 바뀌면 회귀로 잡아낸다.
값은 2026-07-25 실제 실행 출력에서 캡처.
"""
import pytest

from format_utils import (
    fmt_change_pct, chg_icon, fmt_cap, fmt_net, fmt_money, fmt_volume,
    parse_date, get_prev_quarter,
)


@pytest.mark.parametrize("args, expected", [
    ((None,),        "—"),
    ((0,),           "0.00%"),
    ((2.345,),       "+2.35%"),
    ((-1.5,),        "-1.50%"),
    ((2.345, 1),     "+2.3%"),
    ((100,),         "+100.00%"),
    ((-0.001,),      "-0.00%"),   # 반올림 0이어도 음수 부호 유지(현행 동작)
])
def test_fmt_change_pct(args, expected):
    assert fmt_change_pct(*args) == expected


@pytest.mark.parametrize("v, expected", [
    (None, "━"), (0, "━"), (3.1, "▲"), (-2.0, "▼"),
])
def test_chg_icon(v, expected):
    assert chg_icon(v) == expected


@pytest.mark.parametrize("v, expected", [
    (None,                 "—"),
    (0,                    "—"),
    (1_500_000_000_000,    "1조 5,000억"),
    (1_000_000_000_000,    "1조"),
    (250_000_000,          "2억"),
    (50_000_000,           "0.5억"),
    (-1_500_000_000_000,   "-1조 5,000억"),
    (99_000_000,           "1.0억"),   # 0.99억 → 반올림 1.0억(현행)
])
def test_fmt_cap(v, expected):
    assert fmt_cap(v) == expected


@pytest.mark.parametrize("v, expected", [
    (None,        "—"),
    (0,           "+0백만"),
    (1_500_000,   "+1.5조"),
    (123_400,     "+1,234억"),
    (50,          "+0.5억"),
    (-50,         "-0.5억"),
    (-1_500_000,  "-1.5조"),
    (99,          "+1.0억"),
])
def test_fmt_net(v, expected):
    assert fmt_net(v) == expected


@pytest.mark.parametrize("args, expected", [
    ((None,),        "—"),
    ((15000,),       "1조 5,000억"),
    ((15000, True),  "1조"),          # short=True
    ((9999,),        "9,999억"),
    ((-15000,),      "-1조 5,000억"),
    ((10000,),       "1조"),
    ((0,),           "0억"),
])
def test_fmt_money(args, expected):
    assert fmt_money(*args) == expected


@pytest.mark.parametrize("v, expected", [
    (None, "—"), (12345, "1.2만"), (9999, "9999"), (-20000, "-2.0만"), (0, "0"),
])
def test_fmt_volume(v, expected):
    assert fmt_volume(v) == expected


@pytest.mark.parametrize("v, expected", [
    ("20250131", "2025-01-31"),
    ("",         None),
    ("2025",     None),
    ("abcdefgh", None),
    (None,       None),
    ("20251301", "2025-13-01"),   # 월/일 유효성 검증 안 함(현행)
])
def test_parse_date(v, expected):
    assert parse_date(v) == expected


@pytest.mark.parametrize("year, quarter, expected", [
    ("2025", "Q4", ("2025", "Q3")),
    ("2025", "Q1", ("2024", "Q4")),   # 연도 롤백
    ("2025", "Q2", ("2025", "Q1")),
    ("2025", "Q3", ("2025", "Q2")),
])
def test_get_prev_quarter(year, quarter, expected):
    assert get_prev_quarter(year, quarter) == expected
