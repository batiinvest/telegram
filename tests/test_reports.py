# -*- coding: utf-8 -*-
"""tests/test_reports.py — 리포트·IR 발송 순수 함수 characterization 테스트.

naver_report.py / kind_ir.py 의 파싱·정규화·파일명·해시태그·타겟해석·
신규선별·요약메시지 등 부작용 없는 함수의 현재 출력을 그대로 고정한다.
리팩토링(2026-07 Stage 1~3)으로 출력이 1글자라도 바뀌면 회귀로 잡는다.

외부 의존 없음: import 전 config/managers/stock_api 를 스텁해 Supabase·
네트워크 로드를 차단하고, import 직후 스텁을 제거해 다른 테스트 오염을 막는다.

실행:
  python -m pytest tests/test_reports.py       # dev (requirements-dev.txt)
  python3 tests/test_reports.py                # 서버 등 pytest 미설치 환경
"""
import os
import sys
import types

# 루트를 import 경로에 추가 (standalone 실행 대비; pytest 는 conftest 가 처리)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _install_stubs():
    """config/managers/stock_api 스텁 설치. 교체 전 원본을 저장해 반환."""
    cfg = types.ModuleType("config")
    cfg.TELEGRAM_BOT_TOKEN  = "TEST_TOKEN"
    cfg.COMPANY_CHAT_IDS    = {}
    cfg.INDUSTRY_CHAT_IDS   = {}
    cfg.COMPANY_TO_INDUSTRY = {}
    cfg.COMPANY_CODES       = {}

    mgr = types.ModuleType("managers")
    class _Session:
        def get(self, *a, **k):  raise RuntimeError("net disabled in tests")
        def post(self, *a, **k): raise RuntimeError("net disabled in tests")
    class _History:
        def __init__(self, *a, **k): self._s = set()
        def contains(self, k): return k in self._s
        def add(self, k): self._s.add(k)
    class _Bot:
        def send_message(self, *a, **k): return True
    mgr.global_session = _Session()
    mgr.HistoryManager = _History
    mgr.telegram_bot   = _Bot()

    sa = types.ModuleType("stock_api")
    sa.get_company_chat_id = lambda corp, code="": None

    saved = {}
    for name, mod in (("config", cfg), ("managers", mgr), ("stock_api", sa)):
        saved[name] = sys.modules.get(name)
        sys.modules[name] = mod
    return saved


def _restore_stubs(saved):
    """스텁 제거 — 원본이 없었으면 삭제, 있었으면 복원 (세션 오염 방지)."""
    for name, orig in saved.items():
        if orig is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = orig


_saved = _install_stubs()
from bs4 import BeautifulSoup   # noqa: E402
import naver_report as N        # noqa: E402
import kind_ir as K             # noqa: E402
_restore_stubs(_saved)


BACKSLASH = chr(92)


def _row(cells):
    """HTML <td> 셀 리스트 → 파싱용 <tr> 노드."""
    tds = "".join(f"<td>{c}</td>" for c in cells)
    return BeautifulSoup(f"<table><tr>{tds}</tr></table>", "html.parser").find("tr")


# ── naver_report: 파일명/캡션 유틸 ─────────────────────────────
def test_sanitize_filename():
    assert N._sanitize_filename('삼성전자_260731_하나증권.pdf') == '삼성전자_260731_하나증권.pdf'
    assert N._sanitize_filename('삼성/전자?.pdf') == '삼성_전자_.pdf'
    assert N._sanitize_filename('a<b>c:d"e' + BACKSLASH + 'f|g*h.pdf') == 'a_b_c_d_e_f_g_h.pdf'


def test_safe_caption():
    assert N._safe_caption('삼성전자_260731_하나증권.pdf') == '삼성전자 260731 하나증권'
    assert N._safe_caption('a_b.PDF') == 'a b'
    assert N._safe_caption('noext') == 'noext'
    assert len(N._safe_caption('x' * 3000)) == 1024


def test_is_robot_topic():
    assert bool(N._is_robot_topic('감속기 시장 전망')) is True
    assert bool(N._is_robot_topic('AMR 도입 확대')) is True
    assert bool(N._is_robot_topic('반도체 업황 점검')) is False


def test_make_hashtag():
    assert N._make_hashtag('삼성전자') == '#삼성전자'
    assert N._make_hashtag('반도체 장비') == '#반도체장비'
    assert N._make_hashtag('IT/게임') == '#IT게임'
    assert N._make_hashtag('!!!') == ''
    assert N._make_hashtag('') == ''


def test_extract_firm():
    assert N._extract_firm('삼성전자_260519_하나증권.pdf') == '하나증권'
    assert N._extract_firm('nounderscore.pdf') == 'nounderscore'


def test_report_hashtags():
    assert N._report_hashtags('기업분석', '삼성전자', '삼성전자_260519_하나증권.pdf') == \
        '#기업분석 #삼성전자 #하나증권'
    assert N._report_hashtags('산업분석', '자동차', '[자동차] 현대차_260519_KB증권.pdf') == \
        '#산업분석 #자동차 #KB증권'
    # 중복 태그 제거
    assert N._report_hashtags('기업분석', '기업분석', 'x_y_기업분석.pdf') == '#기업분석'


def test_norm_target_price():
    assert N._norm_target_price('100,000원') == '100,000원'
    assert N._norm_target_price('95000원') == '95,000원'
    assert N._norm_target_price('미제시') == '미제시'
    assert N._norm_target_price('') == '미제시'
    assert N._norm_target_price(None) == '미제시'
    assert N._norm_target_price('상향 조정') == '상향 조정'


def test_norm_upside():
    assert N._norm_upside('현재가 대비 +25.3%') == '+25.3%'
    assert N._norm_upside('30%') == '+30.0%'
    assert N._norm_upside('-12.5%') == '-12.5%'
    assert N._norm_upside('N/A') == ''
    assert N._norm_upside('') == ''


def test_build_report_caption_plain():
    # fields 없음 → 평문 폴백
    assert N._build_report_caption('삼성전자_260519_하나증권.pdf', '삼성전자', '#a #b', None) == (
        "📌 <a href='https://t.me/batiarchive'>바티아카이브</a> — 리포트·IR자료"
        "\n\n삼성전자 260519 하나증권\n\n#a #b"
    )


def test_build_report_caption_ai():
    fields = {"포인트": ["수요 회복", "가격 반등", "점유율 확대"], "투자의견": "매수",
              "목표주가": "120000원", "상승여력": "현재가 대비 +25.3%",
              "실적밸류": "PER 12x", "리스크": "환율"}
    cap = N._build_report_caption('삼성전자_260519_하나증권.pdf', '삼성전자', '#a #b', fields)
    assert '📑 <b>삼성전자</b> · 하나증권' in cap
    assert '📈 투자의견  매수' in cap
    assert '🎯 목표주가  120,000원  (상승여력 +25.3%)' in cap
    assert '📊 실적·밸류  PER 12x' in cap
    assert '① 수요 회복' in cap
    assert '③ 점유율 확대' in cap
    assert '⚠️ 리스크  환율' in cap
    assert cap.endswith('#a #b')


# ── naver_report: HTML 파싱 ────────────────────────────────────
def test_parse_report_row_corp():
    row = _row(['<a href="/read">삼성전자 실적</a>', '-', '하나증권',
                '<a href="/download/1.pdf">PDF</a>', '26.07.31'])
    r = N._parse_report_row(row, N.NAVER_REPORT_URLS['기업분석'], '기업분석')
    assert r[1] == '삼성전자 실적_260731_하나증권.pdf'
    assert r[2] == '삼성전자 실적'
    assert r[0] == 'https://finance.naver.com/download/1.pdf'


def test_parse_report_row_industry_robot():
    # '기타' + 로봇 키워드 → '로봇' 승격
    row = _row(['기타', '<a href="/read">로봇 감속기 전망</a>', 'KB증권',
                '<a href="/download/2.pdf">PDF</a>', '26.07.31'])
    r = N._parse_report_row(row, N.NAVER_REPORT_URLS['산업분석'], '산업분석')
    assert r[2] == '로봇'
    assert '[로봇]' in r[1]


def test_parse_report_row_insufficient_cols():
    assert N._parse_report_row(_row(['a', 'b']),
                               N.NAVER_REPORT_URLS['기업분석'], '기업분석') is None


def test_get_total_pages():
    soup = BeautifulSoup(
        '<table><tr><td class="pgRR"><a href="/x?page=7">맨끝</a></td></tr></table>',
        "html.parser")
    assert N._get_total_pages(soup) == 7
    assert N._get_total_pages(BeautifulSoup('<div>없음</div>', "html.parser")) == 1


# ── naver_report: 팬아웃 타겟 해석 ─────────────────────────────
def test_resolve_report_targets():
    # 모듈 전역 채널 딕셔너리를 테스트값으로 임시 세팅 후 복원
    saved = (N.INDUSTRY_CHAT_IDS, N.COMPANY_CHAT_IDS, N.COMPANY_TO_INDUSTRY)
    N.INDUSTRY_CHAT_IDS   = {'2차전지': '@ind2', '테크': '@indtech'}
    N.COMPANY_CHAT_IDS    = {'삼성전자': '@co_samsung'}
    N.COMPANY_TO_INDUSTRY = {'삼성전자': '테크'}
    try:
        # 산업분석: REPORT_INDUSTRY_MAP['자동차']='2차전지'
        assert N._resolve_report_targets('산업분석', '자동차') == {'@ind2'}
        assert N._resolve_report_targets('산업분석', '알수없는분류') == set()
        # 기업분석: 기업방 + 소속 산업방
        assert N._resolve_report_targets('기업분석', '삼성전자') == {'@co_samsung', '@indtech'}
        assert N._resolve_report_targets('기업분석', '무명종목') == set()
    finally:
        N.INDUSTRY_CHAT_IDS, N.COMPANY_CHAT_IDS, N.COMPANY_TO_INDUSTRY = saved


# ── kind_ir: 영문판정 / 발송파일명 ─────────────────────────────
def test_is_english_file():
    assert K._is_english_file('doosan_eng.pdf') is True
    assert K._is_english_file('report_en.pdf') is True
    assert K._is_english_file('company_english_v.pdf') is True
    assert K._is_english_file('한글리포트.pdf') is False


def test_make_send_filename():
    assert K._make_send_filename('에코프로비엠', '260731', 'x.pdf', 0, 1, False) == \
        '에코프로비엠_IR_260731.pdf'
    assert K._make_send_filename('두산', '260805', 'doosan_eng.pdf', 1, 2, True) == \
        '두산_IR_260805_Eng.pdf'
    assert K._make_send_filename('두산', '260805', 'doosan_ko.pdf', 0, 2, True) == \
        '두산_IR_260805.pdf'
    assert K._make_send_filename('에이', '260731', 'a.pdf', 0, 2, False) == \
        '에이_IR_260731 (1).pdf'
    assert K._make_send_filename('에이', '260731', 'b.pdf', 1, 2, False) == \
        '에이_IR_260731 (2).pdf'


# ── kind_ir: 신규선별 / 요약메시지 ─────────────────────────────
def _it(seq, corp, dt):
    return {"ir_seq": seq, "corp": corp, "date": dt}


_KIND_ITEMS = [_it("100", "에코프로비엠", "2026-07-31"),
               _it("101", "두산로보틱스", "2026-08-05"),
               _it("50",  "과거기업",     "2026-07-01"),   # <= last_seq → 제외
               _it("90",  "이미전송",     "2026-07-20"),   # sent_set → 제외
               _it("abc", "비정상",       "2026-07-31")]   # 비정수 → 제외


def test_select_new_items():
    new = K._select_new_items(list(_KIND_ITEMS), last_seq=80, sent_set={"90"})
    assert [x["ir_seq"] for x in new] == ["100", "101"]
    # 전부 신규: 비정수만 제외, 오름차순
    new2 = K._select_new_items(list(_KIND_ITEMS), last_seq=0, sent_set=set())
    assert [x["ir_seq"] for x in new2] == ["50", "90", "100", "101"]


def test_build_summary_message():
    new = K._select_new_items(list(_KIND_ITEMS), last_seq=80, sent_set={"90"})
    msg = K._build_summary_message(new)
    assert 'IR자료 (총 2건)' in msg
    assert '1. 에코프로비엠 - IR일자: 2026-07-31' in msg
    assert '2. 두산로보틱스 - IR일자: 2026-08-05' in msg


# ── standalone 러너 (pytest 미설치 환경) ───────────────────────
if __name__ == "__main__":
    fails = []
    tests = sorted((n, o) for n, o in globals().items()
                   if n.startswith("test_") and callable(o))
    for name, fn in tests:
        try:
            fn()
        except AssertionError as e:
            fails.append(f"{name}: {e or 'assert 실패'}")
        except Exception as e:
            fails.append(f"{name}: {type(e).__name__}: {e}")
    if fails:
        print(f"❌ test_reports FAIL {len(fails)}/{len(tests)}")
        for f in fails:
            print(" -", f)
        raise SystemExit(1)
    print(f"✅ test_reports OK — {len(tests)}개 함수 통과 "
          f"(naver_report 파싱·캡션·타겟 + kind_ir 파일명·선별·요약)")
