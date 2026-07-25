# -*- coding: utf-8 -*-
"""sms_parser characterization 테스트 — 은행 입금 SMS 파싱(결제 자동승인 경로).

parse_deposit 오탐/미탐은 프로 멤버 자동 갱신에 직결되므로 회귀 방지가 중요.
정상 6종은 모듈 내장 _TEST_SAMPLES를 그대로 사용하고, 엣지 케이스를 추가.
"""
import pytest

from sms_parser import parse_deposit, is_bank_sms, _TEST_SAMPLES


@pytest.mark.parametrize("text, expected", _TEST_SAMPLES)
def test_parse_deposit_samples(text, expected):
    """내장 정상 샘플 6종 — bank/name/amount 일치."""
    result = parse_deposit(text)
    assert result is not None
    assert result["bank"] == expected["bank"]
    assert result["name"] == expected["name"]
    assert result["amount"] == expected["amount"]
    assert result["raw"] == text.strip()


@pytest.mark.parametrize("text", [
    None,
    "",
    "[농협] 홍길동 100,000원 출금 잔액",   # '입금' 키워드 없음(출금)
    "[농협] 홍길동 0원 입금",              # 금액 0 → 무시
])
def test_parse_deposit_rejects(text):
    """입금 아님/금액0/빈값 → None."""
    assert parse_deposit(text) is None


@pytest.mark.parametrize("sender, expected", [
    ("15881006",       True),
    ("028-1588-1006",  True),   # 하이픈/앞자리 포함 endswith
    ("1588-1006",      True),
    ("01012345678",    False),
    ("",               False),
])
def test_is_bank_sms(sender, expected):
    assert is_bank_sms(sender) is expected
