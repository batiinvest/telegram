# -*- coding: utf-8 -*-
"""spam_guard characterization 테스트 — 채팅방 스팸 탐지 순수 로직.

_detect 오탐은 정상 사용자 메시지 삭제로 직결되므로 회귀 방지가 중요.
_is_flood/_detect의 도배 판정은 모듈 전역 상태(_recent)를 쓰므로,
각 테스트는 고유 텍스트를 사용해 상호 간섭을 피한다.
기대값은 2026-07-25 실제 실행 출력에서 캡처.
"""
import pytest

import spam_guard as SG


@pytest.mark.parametrize("text, expected", [
    ("https://n.news.naver.com/article/123", True),
    ("그냥 텍스트 메시지입니다",                False),
    ("영상 youtu.be/abc",                     True),   # youtube도 정상 도메인
])
def test_has_news_link(text, expected):
    assert SG._has_news_link(text) is expected


def test_detect_invite_link():
    """텔레그램 외부 초대링크 → 스팸 + 자동삭제."""
    assert SG._detect("가입하세요 https://t.me/+abcdef", 111) == (
        True, "텔레그램 외부 초대링크", True)


def test_detect_kakao_openchat():
    """카카오 오픈채팅 → 스팸 + 자동삭제."""
    assert SG._detect("오픈채팅 open.kakao.com/o/xyz", 111) == (
        True, "카카오 오픈채팅", True)


def test_detect_normal_message():
    """평범한 메시지 → 스팸 아님."""
    assert SG._detect("안녕하세요 오늘 시장 어떤가요", 111) == (False, "", False)


def test_detect_flood_progression():
    """동일 텍스트가 3개 방 이상 → 도배 의심(알림만, 자동삭제 X)."""
    text = "테스트용 고유 도배문구 사세요 대박 종목"  # 이 테스트 전용 고유 텍스트
    assert SG._detect(text, 2001) == (False, "", False)   # 1개 방
    assert SG._detect(text, 2002) == (False, "", False)   # 2개 방
    assert SG._detect(text, 2003) == (True, "다중방 도배(의심)", False)  # 3개 방


def test_detect_flood_exempt_when_news_link():
    """뉴스 링크 포함 시 다중방이어도 도배 면제(정상 뉴스 공유 오탐 방지)."""
    text = "이 기사 공유 naver.com/news/unique-flood-exempt 필독"
    results = [SG._detect(text, cid) for cid in (3001, 3002, 3003)]
    assert all(r == (False, "", False) for r in results)


def test_is_flood_short_text_ignored():
    """최소 길이(12자) 미만은 도배 추적에서 제외."""
    assert SG._is_flood("짧음", 4001) is False
