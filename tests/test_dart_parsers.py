# -*- coding: utf-8 -*-
"""dart_parsers characterization 테스트 — 공시 파서(kv dict → 텔레그램 라인).

dart_parsers는 최대 파일(2,767줄)이자 분할 예정(고위험 보류) 대상.
파서는 순수 함수(kv dict 입력 → list[str] 출력)라 실 HTML 없이 픽스처로 검증 가능.
분할 리팩토링 시 출력 텍스트가 바뀌면(=사용자에게 가는 메시지 변화) 즉시 포착한다.
기대값은 2026-07-25 실제 실행 출력에서 캡처.
"""
from dart_parsers import (
    parse_dividend, parse_treasury_acquisition, parse_share_cancellation,
)


def test_parse_dividend_full():
    kv = {
        "1. 배당구분": "결산배당",
        "2. 배당종류": "현금배당",
        "1주당 배당금(원)": "보통주식 233",
        "시가배당률(%)": "보통주식 1.5",
        "배당금총액(원)": "15000000000",
        "배당기준일": "2025-12-31",
        "배당금지급 예정일자": "2026-04-15",
        "이사회결의일(결정일)": "2026-02-10",
    }
    assert parse_dividend(kv) == [
        "💰 결산배당 · 현금배당",
        "💵 1주당 233원 (시가배당률 1.5%)",
        "💰 배당총액: 150억원",
        "📅 배당기준일: 2025-12-31",
        "📅 지급예정: 2026-04-15",
        "📋 결의일: 2026-02-10",
    ]


def test_parse_dividend_minimal():
    """배당구분만 있으면 헤더 한 줄만."""
    assert parse_dividend({"1. 배당구분": "결산배당"}) == ["💰 결산배당"]


def test_parse_dividend_empty():
    """빈 kv → 빈 리스트 (graceful)."""
    assert parse_dividend({}) == []


def test_parse_treasury_acquisition():
    """자기주식 취득 — 영문 폼 키. 수탁사 괄호 이후 제거."""
    kv = {
        "1. Contract amount (KRW)": "10000000000",
        "9. Number of shares to be acquired": "500,000",
        "10. Price of shares to be acquired": "20,000",
        "Start date": "2026-02-11", "End date": "2026-05-10",
        "3. Purpose of contract": "주주가치 제고",
        "4. Counterparty (Trust company)": "삼성증권 (Samsung Securities)",
        "7. Board resolution date": "2026-02-10",
    }
    assert parse_treasury_acquisition(kv) == [
        "💰 취득금액: 100억원",
        "🔢 취득예정: 500,000주 (주당 20,000원)",
        "📅 계약기간: 2026-02-11 ~ 2026-05-10",
        "📋 목적: 주주가치 제고",
        "🏦 수탁사: 삼성증권",   # 영문 괄호 제거됨
        "📋 결의일: 2026-02-10",
    ]


def test_parse_share_cancellation():
    """주식소각 — 종류·수 필드에서 수량 추출(폴백 경로)."""
    kv = {
        "소각할 주식의 종류와 수": "보통주 1,000,000",
        "소각예정금액(원)": "20000000000",
        "소각할 주식의 취득방법": "기취득 자기주식",
        "소각 예정일": "2026-03-15",
        "이사회결의일(결정일)": "2026-02-10",
    }
    assert parse_share_cancellation(kv) == [
        "🔥 소각주식: 보통주 1,000,000주",
        "💰 소각예정금액: 200억원",
        "📋 취득방법: 기취득 자기주식",
        "📅 소각예정일: 2026-03-15",
        "📋 결의일: 2026-02-10",
    ]
