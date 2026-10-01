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
import json
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
    mgr.note_send_failure = lambda chat_id, reason: None

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
import naver_report as N        # noqa: E402
import kind_ir as K             # noqa: E402
_restore_stubs(_saved)


BACKSLASH = chr(92)


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


# ── naver_report: 리서치 API 항목 → 파일명 ───────────────────────
def _corp_item(nid="96383", item_name="삼성전자", broker="신한투자증권", date="2026-09-30"):
    # 실제 목록 API(company) 항목 꼴 — PDF 주소(attachUrl)는 상세 API에만 있다
    return {"nid": nid, "title": "HBM 시장 침투 준비 완료", "brokerName": broker,
            "brokerCode": "1", "writeDate": date, "itemCode": "005930",
            "itemName": item_name, "goalPrice": "100000", "opinionText": "매수"}


def _ind_item(nid="46271", industry="반도체", title="지금 알아야 할 모든 소부장"):
    return {"nid": nid, "title": title, "brokerName": "한화투자증권", "brokerCode": "2",
            "writeDate": "2026-09-30", "analystName": "x", "industry": "semiconductor",
            "industryKoreanName": industry}


def test_report_file_name_corp():
    # 구 목록 표와 같은 꼴: {종목명}_{YYMMDD}_{증권사}.pdf, tag=종목명
    assert N._report_file_name(_corp_item(), '기업분석') == \
        ('삼성전자_260930_신한투자증권.pdf', '삼성전자')


def test_report_file_name_industry():
    assert N._report_file_name(_ind_item(), '산업분석') == \
        ('[반도체] 지금 알아야 할 모든 소부장_260930_한화투자증권.pdf', '반도체')


def test_report_file_name_industry_robot():
    # '기타' + 로봇 키워드 → '로봇' 승격
    fn, tag = N._report_file_name(_ind_item(industry='기타', title='로봇 감속기 전망'), '산업분석')
    assert tag == '로봇'
    assert fn.startswith('[로봇] 로봇 감속기 전망_')


def test_report_file_name_missing_fields():
    assert N._report_file_name({}, '기업분석') is None
    assert N._report_file_name({**_corp_item(), "itemName": ""}, '기업분석') is None
    assert N._report_file_name({**_corp_item(), "nid": None}, '기업분석') is None


def test_industry_map_uses_new_site_names():
    # 새 사이트 분류명은 '인터넷포탈'(붙여 씀)
    assert N.REPORT_INDUSTRY_MAP['인터넷포탈'] == '테크'


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


# ── naver_report: 크롤 — '리포트 0건'과 'API 깨짐' 구분 ──────────
class _Res:
    """requests.Response 흉내. body(dict)는 JSON으로, text를 주면 그대로(HTML 등)."""
    def __init__(self, body=None, status=200, text=None, url=None):
        self.text = text if text is not None else json.dumps(body, ensure_ascii=False)
        self.status_code, self.url = status, url

    def json(self):
        return json.loads(self.text)


class _FakeSession:
    """요청 순서대로 응답을 돌려주고, 받은 (url, kwargs)를 기록한다."""
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        return self.responses.pop(0)


def _page(items, has_next=False):
    return _Res({"hasNext": has_next, "totalCount": str(len(items)),
                 "items": items, "size": "50", "index": "0"})


def _detail(pdf="https://stock.pstatic.net/stock-research/company/1/20260930_company_1.pdf"):
    return _Res({"nid": "96383", "attachUrl": pdf, "attachName": "x.pdf"})


def _crawl_with(session, page_type="기업분석", history=None):
    saved = (N._session, N._PAGE_DELAY_SEC, N._DETAIL_DELAY_SEC)
    N._session, N._PAGE_DELAY_SEC, N._DETAIL_DELAY_SEC = session, 0, 0
    try:
        return N.crawl_report_pages(page_type, "2026-09-30", history or N.HistoryManager("x"))
    finally:
        N._session, N._PAGE_DELAY_SEC, N._DETAIL_DELAY_SEC = saved


def _expect_crawl_error(session, needle):
    try:
        _crawl_with(session)
    except N.ReportCrawlError as e:
        assert needle in str(e), str(e)
        return
    raise AssertionError("ReportCrawlError 미발생")


def test_crawl_list_then_detail_with_browser_headers():
    s = _FakeSession(_page([_corp_item()]), _detail())
    reports = _crawl_with(s)
    assert reports == [("https://stock.pstatic.net/stock-research/company/1/20260930_company_1.pdf",
                        "삼성전자_260930_신한투자증권.pdf", "삼성전자")]
    (list_url, list_kw), (detail_url, _) = s.calls
    assert list_url == N.NAVER_RESEARCH_API + "/company"
    assert list_kw["params"] == {"index": 0, "size": 50,
                                 "startDate": "2026-09-30", "endDate": "2026-09-30"}
    assert detail_url == N.NAVER_RESEARCH_API + "/company/96383"
    headers = list_kw["headers"]
    assert headers["User-Agent"].startswith("Mozilla/5.0")
    assert headers["Content-Type"] is None          # 세션 기본 application/json 제거
    assert list_kw["timeout"] == N._LIST_TIMEOUT_SEC


def test_crawl_industry_uses_industry_api():
    s = _FakeSession(_page([_ind_item()]), _detail("https://x/i.pdf"))
    assert _crawl_with(s, "산업분석")[0][2] == "반도체"
    assert s.calls[0][0] == N.NAVER_RESEARCH_API + "/industry"
    assert s.calls[1][0] == N.NAVER_RESEARCH_API + "/industry/46271"


def test_crawl_empty_day_is_not_error():
    # 목록은 정상인데 항목 0개 = 그날 0건 → 정상 빈 리스트, 상세 조회 없음
    s = _FakeSession(_page([]))
    assert _crawl_with(s) == []
    assert len(s.calls) == 1


def test_crawl_history_skips_detail_lookup():
    # 이미 보낸 리포트는 상세(PDF 주소)를 부르지 않는다
    h = N.HistoryManager("x")
    h.add("삼성전자_260930_신한투자증권.pdf")
    s = _FakeSession(_page([_corp_item()]))
    assert _crawl_with(s, history=h) == []
    assert len(s.calls) == 1


def test_crawl_paginates_until_has_next_false():
    s = _FakeSession(_page([_corp_item("1", "가")], has_next=True), _page([_corp_item("2", "나")]),
                     _detail("https://x/1.pdf"), _detail("https://x/2.pdf"))
    assert [r[2] for r in _crawl_with(s)] == ["가", "나"]
    assert s.calls[1][1]["params"]["index"] == 1


def test_crawl_non_json_reports_new_url():
    moved = _Res(text="<html><head><title>Npay 증권</title></head></html>",
                 url="https://stock.naver.com/research/company")
    _expect_crawl_error(_FakeSession(moved), "이동→https://stock.naver.com/research/company")
    _expect_crawl_error(_FakeSession(_Res(text="<html></html>")), "JSON 아님")


def test_crawl_http_error_raises():
    _expect_crawl_error(_FakeSession(_Res(text="Forbidden", status=403)), "HTTP 403")


def test_crawl_missing_items_raises():
    _expect_crawl_error(_FakeSession(_Res({"detailCode": "x"})), "items 없음")


def test_crawl_field_change_raises():
    # 항목은 있는데 종목명 필드가 바뀌어 한 건도 이름을 못 붙임
    item = {k: v for k, v in _corp_item().items() if k != "itemName"}
    _expect_crawl_error(_FakeSession(_page([item])), "필드 변경")


def test_crawl_later_page_failure_keeps_collected():
    s = _FakeSession(_page([_corp_item()], has_next=True), _Res(text="", status=500), _detail())
    assert len(_crawl_with(s)) == 1


def test_crawl_all_details_failing_raises():
    _expect_crawl_error(_FakeSession(_page([_corp_item()]), _Res(text="", status=404)),
                        "상세(PDF 주소) 조회 1건 모두 실패")


def test_crawl_partial_detail_failure_keeps_rest():
    # 둘 중 하나만 실패 → 나머지는 보낸다 (실패분은 history에 안 남아 다음 실행에 재시도)
    s = _FakeSession(_page([_corp_item("1", "가"), _corp_item("2", "나")]),
                     _Res(text="", status=500), _detail("https://x/2.pdf"))
    assert [r[2] for r in _crawl_with(s)] == ["나"]


def test_crawl_item_without_attachment_is_skipped():
    s = _FakeSession(_page([_corp_item()]), _Res({"nid": "96383", "attachUrl": None}))
    assert _crawl_with(s) == []


# ── naver_report: 잡 — 실패를 삼키지 않는다 ──────────────────────
def _run_job_with(*, crawl, send_doc=lambda *a, **k: True):
    bridge_mod = types.ModuleType("supabase_bridge")
    class _Bridge:
        def get_config(self, key, default=None): return default
    bridge_mod.bridge = _Bridge()
    saved_mod = sys.modules.get("supabase_bridge")
    sys.modules["supabase_bridge"] = bridge_mod
    saved = (N.crawl_report_pages, N._send_telegram_doc, N._fetch_pdf_file)
    N.crawl_report_pages, N._send_telegram_doc = crawl, send_doc
    N._fetch_pdf_file = lambda url: None
    try:
        N.run_naver_report_job()
    finally:
        N.crawl_report_pages, N._send_telegram_doc, N._fetch_pdf_file = saved
        if saved_mod is None:
            sys.modules.pop("supabase_bridge", None)
        else:
            sys.modules["supabase_bridge"] = saved_mod


def _expect_job_error(needle, **kw):
    try:
        _run_job_with(**kw)
    except RuntimeError as e:
        assert needle in str(e), str(e)
        return
    raise AssertionError("RuntimeError 미발생")


def test_job_raises_on_crawl_error():
    def crawl(page_type, *a, **k):
        raise N.ReportCrawlError(f"{page_type}: 목록 표 없음")
    _expect_job_error("목록 표 없음", crawl=crawl)


def test_job_raises_on_doc_send_failure():
    crawl = lambda page_type, *a, **k: [("https://x/1.pdf", "a_260930_b.pdf", "a")]
    _expect_job_error("PDF 발송 실패 1/1건", crawl=crawl,
                      send_doc=lambda *a, **k: False)


def test_job_ok_when_no_reports():
    _run_job_with(crawl=lambda *a, **k: [])        # 예외 없음


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
