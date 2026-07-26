# -*- coding: utf-8 -*-
"""sms_webhook.process_deposit 테스트 — 입금 자동처리 라우팅(결제 경로).

process_deposit은 파싱→멤버검색→(매칭 연장 / 미매칭 알림)로 분기한다.
DB·텔레그램은 monkeypatch로 대체하고 분기 판정과 반환 status를 고정한다.
not_deposit 경로는 DB 접근 전에 조기반환하므로 목 없이 검증.
"""
import pytest

import sms_webhook as W

DEPOSIT_SMS = "[농협] 홍길동 100,000원 입금 잔액 500,000원"


@pytest.mark.parametrize("text", [
    "출금 5,000원 잔액 100,000원",   # 입금 아님
    "",                              # 빈 문자열
    "안녕하세요 오늘 날씨 좋네요",     # 무관 텍스트
])
def test_not_deposit_early_return(text):
    """입금 SMS 아니면 DB 접근 없이 not_deposit."""
    res = W.process_deposit(text)
    assert res["status"] == "not_deposit"
    assert res["parsed"] is None


def test_unmatched_member(monkeypatch):
    """파싱 성공 + 멤버 미매칭 → unmatched (어드민 알림은 admin_chat 없어 스킵)."""
    monkeypatch.setattr(W, "_find_member_by_name", lambda name: None)
    monkeypatch.setattr(W, "_get_admin_chat", lambda: "")   # falsy → 텔레그램 스킵
    recorded = []
    monkeypatch.setattr(W, "_record_deposit", lambda parsed, member, action: recorded.append((member, action)))

    res = W.process_deposit(DEPOSIT_SMS)
    assert res["status"] == "unmatched"
    assert res["parsed"]["name"] == "홍길동"
    assert res["parsed"]["amount"] == 100000
    assert recorded == [(None, "unmatched")]   # 미매칭 이력 기록


def test_matched_member_auto_extend(monkeypatch):
    """파싱 성공 + 멤버 매칭 → matched + 구독 연장 호출."""
    calls = {}

    class FakePro:
        def extend_member(self, tid, months):
            calls["extend"] = (tid, months)
        def send_invite(self, tid, months=1):
            calls["invite"] = (tid, months)
            return True

    monkeypatch.setattr(W, "_find_member_by_name",
                        lambda name: {"telegram_id": 42, "real_name": "홍길동"})
    monkeypatch.setattr(W, "_get_admin_chat", lambda: "")
    monkeypatch.setattr(W, "_pro", FakePro())
    monkeypatch.setattr(W, "_record_deposit", lambda parsed, member, action: None)

    res = W.process_deposit(DEPOSIT_SMS)
    assert res["status"] == "matched"
    assert calls["extend"] == (42, W.DEFAULT_MONTHS)   # 기본 개월수로 연장
    assert calls["invite"][0] == 42
