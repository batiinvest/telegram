# -*- coding: utf-8 -*-
"""2026-10-28 채팅방 발송 메시지 점검에서 나온 수정의 회귀 테스트.

- dart_rules: 같은 사건 지문(지주·자회사 공시)·연결 회사의 별도 잠정실적·긴급 후속·증권사 일상 서류
- 잠정실적 파서: 적자가 낀 증감률은 흑자/적자 전환·지속으로, 소수 한 자리
- 공시 다이제스트: 같은 회사 다른 계약 누락·지주가 낸 자회사 계약/실적 중복·서식 우선순위
외부 의존 없음(DB·DART는 가짜).
"""
import datetime
import sys
import types

import dart_rules as dr
from dart_parsers import parse_preliminary_earnings


# ── dart_rules ─────────────────────────────────────────────────────────────

def test_event_fingerprint_contract_and_earnings():
    d = "💰 계약금액: 2,539억원 (매출대비 14.58%)\n📅 계약기간: 2026-10-28 ~ 2028-12-31"
    own = dr.event_fingerprint("단일판매ㆍ공급계약체결", d)
    parent = dr.event_fingerprint("단일판매ㆍ공급계약체결(자회사의 주요경영사항)", d)
    assert own == parent == ("계약", "2,539억원")
    assert dr.event_fingerprint("단일판매ㆍ공급계약체결", "계약금액: 공시유보") is None
    e = "📊 연결 잠정실적\n  매출: 5조 7,325억 / YoY +12.0%\n  영업이익: 8,917억 / YoY +59.2%"
    assert dr.event_fingerprint("연결재무제표기준영업(잠정)실적(공정공시)", e) == ("실적", "5조 7,325억", "8,917억")
    assert dr.event_fingerprint("주식소각결정", d) is None


def test_secondary_earnings_and_followup():
    assert dr.is_secondary_earnings("영업(잠정)실적(공정공시)", True)
    assert dr.is_secondary_earnings("영업(잠정)실적(공정공시)(자회사의 주요경영사항)", True)
    assert not dr.is_secondary_earnings("연결재무제표기준영업(잠정)실적(공정공시)", True)
    assert not dr.is_secondary_earnings("영업(잠정)실적(공정공시)", False)   # 연결 없는 회사는 별도가 유일한 실적
    assert dr.is_urgent_followup("주권매매거래정지기간변경")
    assert dr.is_urgent_followup("기타시장안내")
    assert not dr.is_urgent_followup("주권매매거래정지")


def test_routine_filings_skip_broadcast():
    assert dr.classify_disclosure("일괄신고추가서류(파생결합증권-주가연계증권)") == "skip"
    assert dr.classify_disclosure("주권매매거래정지") == "urgent"   # 긴급은 그대로


# ── 잠정실적 파서 ───────────────────────────────────────────────────────────

def _earn_kv(metric, cur, prev_q, qoq, prev_y, yoy):
    # 서식 순서: (지표, '당해실적'), (당기, 전기), (QoQ%, '-'), (전년동기, YoY%)
    return {"_report_nm": "영업(잠정)실적(공정공시)", metric: "당해실적",
            cur: prev_q, qoq: "-", prev_y: yoy}


def _line(lines, label):
    return next(l for l in lines if l.strip().startswith(label + ":"))


def test_loss_yoy_reads_as_turnaround_or_continuing():
    # SKC형: 손실끼리의 회사 기재 % — '+9.7%' 대신 손실 축소/확대
    lines = parse_preliminary_earnings(_earn_kv("영업이익", "-49,600", "-30,000", "-65.3", "-54,900", "9.7"))
    ln = _line(lines, "영업이익")
    assert "YoY 적자지속 (손실 축소)" in ln and "QoQ 적자지속 (손실 확대)" in ln and "%" not in ln
    lines = parse_preliminary_earnings(_earn_kv("영업이익", "1,200", "-300", "-", "-800", "-"))
    assert "YoY 흑자전환" in _line(lines, "영업이익") and "QoQ 흑자전환" in _line(lines, "영업이익")
    lines = parse_preliminary_earnings(_earn_kv("영업이익", "-500", "300", "-", "900", "-"))
    assert "YoY 적자전환" in _line(lines, "영업이익")


def test_pct_one_decimal():
    lines = parse_preliminary_earnings(_earn_kv("매출액", "24,700", "20,000", "23.5", "9,570", "157.59"))
    ln = _line(lines, "매출")
    assert "YoY +158.1%" not in ln            # 회사 기재 %를 그대로 쓰되
    assert "YoY +157.6%" in ln and "QoQ +23.5%" in ln   # 소수 한 자리로


# ── 공시 다이제스트 ─────────────────────────────────────────────────────────

class _Q:
    def __init__(self, rows): self.rows = rows
    def __getattr__(self, _):
        return lambda *a, **k: self
    def execute(self):
        return types.SimpleNamespace(data=self.rows)


class _SB:
    def __init__(self, rows): self.rows = rows
    def table(self, _): return _Q(self.rows)


def _gen(rows, details):
    stub = types.ModuleType("dart_parser")
    stub.get_disclosure_detail = lambda rc, nm: details.get(rc, "")
    saved = sys.modules.get("dart_parser")
    sys.modules["dart_parser"] = stub
    try:
        import disclosure_digest as dd
        return dd.generate(_SB(rows), day=datetime.date(2026, 10, 28))
    finally:
        if saved is None:
            sys.modules.pop("dart_parser", None)
        else:
            sys.modules["dart_parser"] = saved


def test_digest_keeps_every_contract_and_drops_parent_duplicate():
    rows = [
        {"target": "코오롱글로벌", "content": "[공시/major] 단일판매ㆍ공급계약체결 #1"},
        {"target": "코오롱", "content": "[공시/major] 단일판매ㆍ공급계약체결(자회사의 주요경영사항) #2"},
        {"target": "코오롱글로벌", "content": "[공시/major] 단일판매ㆍ공급계약체결 #3"},
        {"target": "SK", "content": "[공시/major] 단일판매ㆍ공급계약체결(자회사의 주요경영사항) #4"},
    ]
    det = {"1": "💰 계약금액: 1,384억원 (매출대비 5.16%)", "2": "💰 계약금액: 973억원 (매출대비 3.63%)",
           "3": "💰 계약금액: 973억원 (매출대비 3.63%)", "4": "💰 계약금액: 500억원 (매출대비 2.00%)"}
    txt = _gen(rows, det)
    assert "코오롱글로벌: 1,384억원" in txt and "코오롱글로벌: 973억원" in txt   # 같은 회사 두 계약 다
    assert "• 코오롱" not in txt.replace("• 코오롱글로벌", "")                  # 자회사 공시와 겹친 지주 줄은 빠짐
    assert "SK(자회사): 500억원 (자회사 매출대비 2.00%)" in txt                 # 겹칠 자회사 공시가 없으면 표시만 바꿔 남김


def test_digest_earnings_prefers_consolidated_and_drops_parent_copy():
    rows = [
        {"target": "HD한국조선해양", "content": "[공시/major] 영업(잠정)실적(공정공시)(자회사의 주요경영사항) #10"},
        {"target": "HD한국조선해양", "content": "[공시/major] 영업(잠정)실적(공정공시) #11"},
        {"target": "HD한국조선해양", "content": "[공시/major] 연결재무제표기준영업(잠정)실적(공정공시) #12"},
        {"target": "HD현대중공업", "content": "[공시/major] 연결재무제표기준영업(잠정)실적(공정공시) #13"},
        {"target": "SKC", "content": "[공시/major] 연결재무제표기준영업(잠정)실적(공정공시) #14"},
    ]
    det = {"10": "  매출: 5조 7,325억 / YoY +12.0%\n  영업이익: 8,917억 / YoY +59.2%",
           "11": "  매출: 1,000억 / YoY +1.0%\n  영업이익: 10억 / YoY +1.0%",
           "12": "  매출: 8조 2,862억 / YoY +20.0%\n  영업이익: 1조 4,472억 / YoY +37.3%",
           "13": "  매출: 5조 7,325억 / YoY +12.0%\n  영업이익: 8,917억 / YoY +59.2%",
           "14": "  매출: 6,389억 / YoY +3.0%\n  영업이익: -496억 / YoY 적자지속 (손실 축소)"}
    txt = _gen(rows, det)
    assert "HD한국조선해양: 8조 2,862억 / 1조 4,472억" in txt     # 연결을 고름(별도·자회사 아님)
    assert "1,000억" not in txt and "(자회사)" not in txt
    assert txt.count("5조 7,325억") == 1                         # 자회사 실적은 HD현대중공업 자체 공시로 한 번만
    assert "SKC: 6,389억 / -496억 · YoY 적자지속 (손실 축소)" in txt
