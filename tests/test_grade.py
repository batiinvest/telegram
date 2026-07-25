# -*- coding: utf-8 -*-
"""grade characterization 테스트 — 실적 등급 분류(S/A/B/관찰)와 추세신호.

grade_row는 순수 함수(캐시 dict만 받음)라 DB 없이 검증 가능.
복잡한 비즈니스 규칙이므로 각 등급 진입/제외 경로를 고정한다.
기대값은 2026-07-25 실제 실행 출력에서 캡처.
"""
import grade as G


def gr(r, prev=None, prevy=None, prev2=None):
    return G.grade_row(r, prev or {}, prevy or {}, prev2 or {})


def test_grade_S():
    """매출YoY 40 + 영업익YoY 30 + 마진+3p + 전분기YoY 25 → S."""
    out = gr(
        {"stock_code": "A", "revenue": 10e9, "operating_profit": 1e9,
         "revenue_yoy": 40, "op_profit_yoy": 30, "operating_margin": 12},
        prev={"A": {"revenue_yoy": 25}},
        prevy={"A": {"operating_margin": 9, "revenue": 7e9}})
    assert out == {"grade": "S", "score": 182}


def test_grade_A_via_margin_only():
    """S 조건 중 마진만 충족(연속성 미충족) → A."""
    out = gr(
        {"stock_code": "A", "revenue": 10e9, "operating_profit": 1e9,
         "revenue_yoy": 40, "op_profit_yoy": 30, "operating_margin": 12},
        prev={"A": {"revenue_yoy": 5}},
        prevy={"A": {"operating_margin": 9, "revenue": 7e9}})
    assert out == {"grade": "A", "score": 155}


def test_grade_A_branch():
    """매출YoY 35 + 영업익 개선(op_yoy>=0) → A(A분기 로직)."""
    out = gr(
        {"stock_code": "A", "revenue": 10e9, "operating_profit": 1e9,
         "revenue_yoy": 35, "op_profit_yoy": 5},
        prevy={"A": {"revenue": 7e9}})
    assert out == {"grade": "A", "score": 115}


def test_grade_B():
    """매출YoY 22 + 영업익 흑자 → B."""
    out = gr(
        {"stock_code": "A", "revenue": 10e9, "operating_profit": 1e9, "revenue_yoy": 22},
        prevy={"A": {"revenue": 8e9}})
    assert out == {"grade": "B", "score": 72}


def test_grade_watch_black_turn():
    """매출QoQ 25 + 전분기 영업적자→당분기 흑자 → 관찰."""
    out = gr(
        {"stock_code": "A", "revenue": 10e9, "operating_profit": 1e9, "revenue_qoq": 25},
        prev={"A": {"operating_profit": -5e8, "revenue": 8e9}})
    assert out == {"grade": "관찰", "score": 55}


def test_grade_none_small_revenue():
    """매출 50억 미만 → 제외(None)."""
    assert gr({"stock_code": "A", "revenue": 1e9, "revenue_yoy": 40, "op_profit_yoy": 30}) is None


def test_grade_none_base_effect():
    """전년동기 10% 미만 + YoY 200% 초과 → 베이스효과 제외."""
    out = gr(
        {"stock_code": "A", "revenue": 10e9, "operating_profit": 1e9,
         "revenue_yoy": 300, "op_profit_yoy": 30},
        prevy={"A": {"revenue": 5e8}})
    assert out is None


def test_grade_none_op_loss_deepening():
    """영업손실 + op_yoy -50 이하 → 제외."""
    out = gr({"stock_code": "A", "revenue": 10e9, "operating_profit": -1e9,
              "revenue_yoy": 40, "op_profit_yoy": -60})
    assert out is None


def test_grade_none_one_off_gain():
    """기타영업수익 > 영업이익 50% → 일회성 이익 제외."""
    out = gr({"stock_code": "A", "revenue": 10e9, "operating_profit": 1e9,
              "revenue_yoy": 40, "op_profit_yoy": 30, "other_operating_income": 6e8})
    assert out is None


# ── detect_trend_flags ──

def test_trend_too_short():
    assert G.detect_trend_flags([{"revenue_qoq": 5}]) == {
        "rev_slowdown": False, "op_leverage_fail": False, "debt_surge": False}


def test_trend_rev_slowdown():
    flags = G.detect_trend_flags([
        {"revenue_qoq": 30}, {"revenue_qoq": 20}, {"revenue_qoq": 15},
        {"revenue_qoq": 5, "revenue_yoy": 1, "op_profit_yoy": 1}])
    assert flags["rev_slowdown"] is True


def test_trend_op_leverage_fail():
    flags = G.detect_trend_flags([
        {"revenue_qoq": 10},
        {"revenue_qoq": 10, "revenue_yoy": 10, "op_profit_yoy": -20}])
    assert flags["op_leverage_fail"] is True


def test_trend_debt_surge():
    flags = G.detect_trend_flags([
        {"total_debt": 50, "total_equity": 100},
        {"total_debt": 90, "total_equity": 100}])
    assert flags["debt_surge"] is True


def test_trend_clean():
    flags = G.detect_trend_flags([
        {"revenue_qoq": 10},
        {"revenue_qoq": 12, "revenue_yoy": 10, "op_profit_yoy": 15,
         "total_debt": 50, "total_equity": 100}])
    assert flags == {"rev_slowdown": False, "op_leverage_fail": False, "debt_surge": False}
