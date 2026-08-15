# -*- coding: utf-8 -*-
"""dart_parsers 전 파서 스모크/특성 테스트 — 빈 kv 거동 고정.

44개 파서 전부가 빈 kv({})에서 크래시 없이 list를 반환함을 고정한다.
대부분 []를 반환하고, 4개만 헤더 한 줄을 반환(현행 동작). 리팩토링·분할
시 어떤 파서든 빈 입력 처리가 깨지면 즉시 포착된다.
기대값은 2026-07-25 실제 실행 출력에서 캡처.
"""
import dart_parsers as D

# 빈 kv에서 헤더를 반환하는 6개 (나머지 41개는 [])
EMPTY_HEADERS = {
    "parse_agm_notice":          ["🏛 주주총회 소집 — 안건은 공시 원문 참조"],
    "parse_hq_relocation":       ["📍 본점 이전"],
    "parse_preliminary_earnings": ["📊 별도 잠정실적"],
    "parse_tender_offer":        ["📢 공개매수"],
    "parse_subscription_result": ["📢 유상증자 청약결과"],
    "parse_tender_opinion":      ["📢 공개매수 의견표명"],
}


def _all_parsers():
    return sorted(n for n in dir(D) if n.startswith("parse_"))


def test_parser_count_stable():
    """파서 수 53 — 예상치 못한 추가/삭제 감지."""
    assert len(_all_parsers()) == 53


def test_all_parsers_empty_kv_return_list():
    """빈 kv → 크래시 없이 list 반환 + 정확한 빈-kv 출력 고정."""
    for name in _all_parsers():
        out = getattr(D, name)({})
        assert isinstance(out, list), f"{name} returned {type(out).__name__}, not list"
        assert out == EMPTY_HEADERS.get(name, []), f"{name} empty-kv output changed: {out!r}"


def test_all_parsers_ignore_unknown_keys():
    """무관한 키만 있는 kv → 빈 kv와 동일하게 크래시 없이 list 반환."""
    junk = {"관계없는키": "값", "_html": "", "_rcept_no": "20260101000001"}
    for name in _all_parsers():
        out = getattr(D, name)(dict(junk))
        assert isinstance(out, list), f"{name} crashed/returned non-list on junk kv"
