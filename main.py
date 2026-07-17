# main.py — DART 공시 봇 (개선 버전)
#
# 개선 사항:
#   1. 공시 중요도 3단계 분류 → 채널별 라우팅
#   2. 노이즈성 공시 스킵 (산업/메인 채널에서만)
#   3. 기업 블랙리스트 (app_config에서 관리)
#   4. 스킵/블랙리스트 DB 로드 지원

import re
import html
import requests
import datetime
import time
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from managers import market_timer, HistoryManager, get_session
from db_utils import fetch_all_pages as _fetch_all_pages

import stock_api
from ai_analyst import analyze_disclosure_gemini
from dart_parser import get_disclosure_detail, get_audit_opinion

from config import (
    DART_API_KEY,
    TELEGRAM_BOT_TOKEN,
    INDUSTRY_CHAT_IDS,
    INDUSTRY_HIERARCHY,
    COMPANY_CHAT_IDS,
    CHAT_IDS_BY_CODE,
    COMPANY_CODES,
    AI_TRIGGER_KEYWORDS,
    GLOBAL_IMPORTANT_KEYWORDS,
    COMPANY_TO_INDUSTRY
)

try:
    from supabase_bridge import bridge as _bridge
    _BRIDGE_OK = True
except Exception:
    _BRIDGE_OK = False

DEFAULT_CHAT_ID = "@BatiInvestChat"

# ══════════════════════════════════════════════════════
#  공시 중요도 분류
#
#  URGENT  → 메인 + 산업 + 기업채널
#  MAJOR   → 산업 + 기업채널
#  NORMAL  → 기업채널만
#  SKIP    → 산업/메인에서 제외 (기업채널엔 발송)
# ══════════════════════════════════════════════════════

URGENT_KEYWORDS = [
    "거래정지", "매매거래정지",
    "상장폐지", "관리종목",
    "횡령", "배임",
    "공개매수",
    "불성실공시",
    "영업정지",
    "회생절차", "파산", "감자",
    "상장적격성",   # 상장적격성 실질심사 사유 발생/결과 — 상폐 심사 단계
]

MAJOR_KEYWORDS = [
    "공급계약", "수주",
    "잠정실적", "매출액",
    "무상증자",
    "유상증자",
    "최대주주변경",
    "합병", "분할", "인수",
    "전환사채", "신주인수권부사채",
    "소송", "분쟁",
    "특허", "임상",
    "사업보고서", "분기보고서", "반기보고서",
    "영업양수", "영업양도", "주식소각", "액면", "배당",
]

# 산업/메인 채널에서 스킵 (기업채널은 정상 발송)
SKIP_FOR_BROADCAST = [
    "주식등의대량보유상황보고",
    "임원ㆍ주요주주특정증권등소유상황보고서",
    "임원·주요주주특정증권등소유상황보고서",
    "소유상황보고",
    "기업설명회",
    "IR개최",
    "감사보고서",
    "주주총회소집공고",
    "주주총회결과",
    "의결권대리행사권유",
    "증권발행실적보고서",  # 증자 완료 후 결과 보고
    "투자설명서",          # 공모 관련 중간 서류
    "자기주식취득결과보고서",
    "자기주식처분결과보고서",
]

# 기업 블랙리스트 (기본값 — DB에서 덮어씀)
DART_BLACKLIST: set = set()
DART_TITLE_FILTER: list = []   # 공시 제목 부분일치 차단
DART_CORP_FILTER: list = []    # 기업명 부분일치 차단


def _load_dart_filters():
    """app_config에서 공시 등급 키워드 + 각종 필터 로드. DB에 없는 기본값은 최초 1회 시드."""
    global DART_BLACKLIST, URGENT_KEYWORDS, MAJOR_KEYWORDS, SKIP_FOR_BROADCAST
    global DART_TITLE_FILTER, DART_CORP_FILTER
    if not _BRIDGE_OK:
        return
    _bridge.seed_defaults({
        "dart_urgent": ",".join(URGENT_KEYWORDS),
        "dart_major":  ",".join(MAJOR_KEYWORDS),
        "dart_skip":   ",".join(SKIP_FOR_BROADCAST),
    })
    try:
        client = _bridge._get_client()
        if not client:
            return
        keys = ['dart_blacklist', 'dart_urgent', 'dart_major', 'dart_skip',
                'dart_title_filter', 'dart_corp_filter']
        res = client.table('app_config').select('key,value').in_('key', keys).execute()
        cfg = {r['key']: r['value'] for r in (res.data or [])}

        if cfg.get('dart_blacklist'):
            DART_BLACKLIST = {c.strip() for c in cfg['dart_blacklist'].split(',') if c.strip()}
            logging.info(f"✅ [공시봇] 블랙리스트 {len(DART_BLACKLIST)}개 로드")

        if cfg.get('dart_urgent'):
            kws = [k.strip() for k in cfg['dart_urgent'].split(',') if k.strip()]
            if kws: URGENT_KEYWORDS = kws; logging.info(f"✅ [공시봇] 긴급 키워드 {len(kws)}개 로드")

        if cfg.get('dart_major'):
            kws = [k.strip() for k in cfg['dart_major'].split(',') if k.strip()]
            if kws: MAJOR_KEYWORDS = kws; logging.info(f"✅ [공시봇] 중요 키워드 {len(kws)}개 로드")

        if cfg.get('dart_skip'):
            kws = [k.strip() for k in cfg['dart_skip'].split(',') if k.strip()]
            if kws: SKIP_FOR_BROADCAST = kws; logging.info(f"✅ [공시봇] 잡공시 키워드 {len(kws)}개 로드")

        if cfg.get('dart_title_filter'):
            kws = [k.strip() for k in cfg['dart_title_filter'].split(',') if k.strip()]
            if kws: DART_TITLE_FILTER = kws; logging.info(f"✅ [공시봇] 제목 필터 {len(kws)}개 로드")

        if cfg.get('dart_corp_filter'):
            kws = [k.strip() for k in cfg['dart_corp_filter'].split(',') if k.strip()]
            if kws: DART_CORP_FILTER = kws; logging.info(f"✅ [공시봇] 기업명 필터 {len(kws)}개 로드")

    except Exception as e:
        logging.warning(f"⚠️ [공시봇] 필터 로드 실패 (기본값 사용): {e}")


# reload_flag 소비는 watchdog 단일 창구 — 재로드 시 공시 필터도 함께 갱신되도록 콜백 등록.
# (구: 봇 자체 check_reload_flag 폴링 — bridge 싱글톤 플래그를 워치독과 경쟁 소비해
#  먼저 본 쪽만 반영되던 문제. 2026-07-11 등록제로 통일)
try:
    from config import on_reload as _on_reload
    _on_reload(_load_dart_filters)
except Exception:
    pass


def classify_disclosure(report_nm: str) -> str:
    """
    공시 중요도 분류.
    반환: 'urgent' | 'major' | 'skip' | 'normal'
    """
    # 긴급 최우선 — skip 키워드와 한 제목에서 겹칠 때 긴급이 이겨야 함
    # (키워드는 app_config에서 운영 변경되므로 겹침을 전제로 방어)
    if any(k in report_nm for k in URGENT_KEYWORDS):
        return 'urgent'
    if any(k in report_nm for k in SKIP_FOR_BROADCAST):
        return 'skip'
    if any(k in report_nm for k in MAJOR_KEYWORDS):
        return 'major'
    return 'normal'


CAP_THRESHOLD_MAIN = 100_000_000_000  # 메인 채널 시총 기준: 1000억

class DartRoutingBot:
    def __init__(self):
        self.base_url = "https://opendart.fss.or.kr/api/list.json"
        self.history  = HistoryManager("sent_list.txt", max_len=2000)
        self.ai_executor = ThreadPoolExecutor(max_workers=2)
        self.session = get_session()
        self._api_fail_streak = 0       # list.json 연속 실패 카운트 (관리자 알림용)
        self._swept = False             # 기동 첫 사이클 복구 스윕 완료 여부
        # 발송 실패 채널 재시도 큐: rcept_no → {targets, msg, n, desc}
        # (구 _retry_counts: 전 채널 실패만 재시도 → 부분 실패 채널 유실 + 재파싱 낭비)
        self._pending: dict = {}
        # 일일 운영 통계 (19시 이후 요약 1회 보고)
        self._day_stats: dict = {"date": "", "reported": False, "sent": 0,
                                 "filtered": 0, "fail": 0, "err": 0, "by_level": {}}

        # 시총 캐시 (메인 채널 필터링용)
        self._cap_cache: dict = {}   # stock_code(숫자) → market_cap
        self._cap_loaded: datetime.datetime | None = None
        self._load_cap_cache()

        # DB에서 블랙리스트 로드
        _load_dart_filters()

    def _load_cap_cache(self):
        """Supabase market_data에서 최신 시총 캐시 로드 (24시간마다 갱신).
        ※ 전체 상장사(2000개+) 처리를 위해 페이지네이션 사용."""
        if not _BRIDGE_OK:
            return
        try:
            sb = _bridge._get_client()
            date_res = sb.table('market_data').select('base_date') \
                         .order('base_date', desc=True).limit(1).execute()
            max_date = (date_res.data or [{}])[0].get('base_date')
            if max_date:
                all_rows = _fetch_all_pages(
                    sb.table('market_data')
                      .select('stock_code,market_cap')
                      .eq('base_date', max_date)
                )

                self._cap_cache = {
                    m['stock_code']: m['market_cap']
                    for m in all_rows
                    if m.get('market_cap') is not None
                }
                self._cap_loaded = datetime.datetime.now()
                logging.info(f"[공시봇] 시총 캐시 로드 완료: {len(self._cap_cache)}개")
        except Exception as e:
            logging.warning(f"[공시봇] 시총 캐시 로드 실패: {e}")

    def _is_main_worthy(self, stock_code: str) -> bool:
        """시총 1000억 이상인지 확인. 정보 없으면 True(허용)."""
        # 24시간마다 캐시 갱신
        if self._cap_loaded is None or \
           (datetime.datetime.now() - self._cap_loaded).total_seconds() > 86400:
            self._load_cap_cache()

        if not stock_code:
            return True  # stock_code 없으면 허용 (시장 전체 중요 공시 등)
        code = stock_code.replace('.KS', '').replace('.KQ', '').strip()
        cap  = self._cap_cache.get(code)
        if cap is None:
            return True  # 시총 정보 없으면 허용
        return cap >= CAP_THRESHOLD_MAIN

    def ai_worker(self, chat_id, corp_name, report_nm, rcept_no):
        logging.info(f"🤖 AI Analyzing: {corp_name}")
        result = analyze_disclosure_gemini(corp_name, report_nm, rcept_no)
        if result:
            # AI 출력·기업명·공시명은 <,>,& 포함 가능 → HTML parse_mode 보호 이스케이프
            corp_esc   = html.escape(corp_name or "", quote=False)
            report_esc = html.escape(re.sub(r'\s+', ' ', report_nm).strip(), quote=False)
            result_esc = html.escape(result.strip(), quote=False)
            msg = (
                f"🤖 <b>[AI 심층 분석] {corp_esc}</b>\n"
                f"└ {report_esc}\n\n{result_esc}\n\n"
                f"⚠️ <i>AI 분석은 참고용이며 투자 판단의 근거가 아닙니다.</i>"
            )
            stock_api.send_telegram(chat_id, msg)

    def get_emoji(self, title: str) -> str:
        t = title.replace(" ", "")
        if any(x in t for x in ["거래정지", "횡령", "배임", "소송", "불성실", "상장폐지"]): return "🚨"
        if any(x in t for x in ["공급계약", "수주", "무상증자", "최대주주변경", "공개매수"]): return "📈"
        if any(x in t for x in ["유상증자", "전환사채", "CB", "BW", "신주인수권"]): return "💰"
        if any(x in t for x in ["주식등의대량보유", "임원", "주요주주", "소유상황"]): return "📊"
        if any(x in t for x in ["사업보고서", "분기보고서", "잠정실적"]): return "📘"
        return "📄"

    def _get_company_chat_id(self, corp_name: str, stock_code: str = "") -> str | None:
        """stock_api.get_company_chat_id()로 위임 (모든 파일 공통 사용)"""
        return stock_api.get_company_chat_id(corp_name, stock_code)

    def _build_msg(self, corp_name, report_nm, rcept_no, stock_code, prefix="", detail=""):
        emoji       = self.get_emoji(report_nm)
        link        = f"http://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"
        target_code = COMPANY_CODES.get(corp_name, stock_code)
        price_info  = stock_api.get_stock_price(target_code)
        stock_msg   = f"<b>{price_info}</b>\n" if price_info else ""
        # 외부 데이터(DART 파싱 detail·기업명·공시명)는 원문 XML/특수문자(<,>,&)를
        # 포함할 수 있어 HTML parse_mode를 깨뜨림(예: <?xml → 400 발송실패) → 이스케이프.
        corp_esc    = html.escape(corp_name or "", quote=False)
        detail_esc  = html.escape(detail, quote=False) if detail else ""
        detail_block = f"\n\n{detail_esc}" if detail_esc else ""
        # DART report_nm에 과도한 공백이 포함되는 경우 정규화
        report_nm_clean = html.escape(re.sub(r'\s+', ' ', report_nm).strip(), quote=False)
        return (
            f"{prefix}{emoji} <b>[{corp_esc}]</b>\n"
            f"{stock_msg}{report_nm_clean}{detail_block}\n"
            f"🔗 <a href='{link}'>공시 원문</a> | "
            f"📈 <a href='https://finance.naver.com/item/main.nhn?code={target_code}'>네이버</a>"
        )

    _MAX_PAGES         = 3    # 평시 캡 — 100건×3페이지, 분당 300건 폭주까지 커버
    _MAX_PAGES_STARTUP = 30   # 기동 첫 사이클 캡 — 중단·장애 시간대 복구 스윕용

    def _note_api_failure(self, detail: str):
        """list.json 오류 로그 + 연속 실패 누적 시 관리자 1회 알림.
        (구: 오류 status 무로그 break — 쿼터초과·키만료로 수집이 전면
         중단돼도 heartbeat는 정상이라 탐지 불가했음)"""
        self._api_fail_streak += 1
        logging.error(f"❌ [공시] list.json 오류 ({self._api_fail_streak}연속): {detail}")
        if self._api_fail_streak == 10:
            try:
                from telegram_utils import get_admin_chat_id
                admin = get_admin_chat_id()
                if admin:
                    stock_api.send_telegram(
                        admin, f"🚨 <b>[공시봇]</b> DART 수집 10분 연속 실패\n└ {detail}")
            except Exception:
                logging.exception("⚠️ [공시] 관리자 알림 발송 실패")

    def _fetch_items(self, now) -> list:
        """DART list.json 조회 (어제~오늘, 최신순).

        - bgn_de=어제: 19시 폴링 종료 이후 접수분(저녁 정정·5%보고 등)을
          다음날 아침 첫 사이클이 자연 수거 (구: '오늘' 고정 → 영구 누락).
        - 페이지는 최신순 → 이미 처리한 공시가 포함된 페이지까지만 조회
          (그보다 오래된 페이지는 기처리 영역).
        - 기동 첫 사이클은 캡 30페이지: 재기동·장애 시간대 복구 스윕.
        """
        bgn_de = (now - datetime.timedelta(days=1)).strftime("%Y%m%d")
        end_de = now.strftime("%Y%m%d")
        max_pages = self._MAX_PAGES if self._swept else self._MAX_PAGES_STARTUP
        self._swept = True

        items = []
        total_page = 1
        for _page in range(1, max_pages + 1):
            params = {
                "crtfc_key": DART_API_KEY,
                "bgn_de": bgn_de, "end_de": end_de,
                "page_count": 100, "page_no": _page,
            }
            res = self.session.get(self.base_url, params=params, timeout=10)
            try:
                data = res.json()
            except ValueError:
                self._note_api_failure(f"비JSON 응답 (HTTP {res.status_code})")
                break
            status = data.get("status")
            if status not in ("000", "013"):   # 013 = 조회 데이터 없음 (정상)
                self._note_api_failure(f"status={status} {data.get('message', '')}")
                break
            self._api_fail_streak = 0
            if status == "013":
                break
            page_items = data.get("list", [])
            items.extend(page_items)
            total_page = int(data.get("total_page", 1) or 1)
            if _page >= total_page:
                break
            if any(self.history.contains(it.get("rcept_no")) for it in page_items):
                break
        else:
            logging.warning(f"⚠️ [공시] 페이지 캡({max_pages}) 도달 — "
                            f"미조회분 존재 가능 (total_page={total_page})")
        return items

    def _process_item(self, item: dict):
        """공시 1건 처리: 필터 → 분류 → 메시지 → 라우팅 발송 → 이력 기록.
        run() 사이클에서 항목별 try로 감싸 호출 — 한 항목의 예외가
        같은 사이클의 나머지(더 최신) 공시 처리를 막지 않도록 격리.
        반환: 'sent' | 'fail'(전채널 실패) | 'filtered' | 'ignored' | None(기처리)."""
        rcept_no   = item.get("rcept_no") or ""
        corp_name  = item.get("corp_name") or ""
        report_nm  = item.get("report_nm") or ""
        stock_code = (item.get("stock_code") or "").strip()

        # ① 처리 이력
        if not rcept_no or self.history.contains(rcept_no):
            return None

        # ② 비상장 제외
        if not stock_code:
            return None

        # ③ 블랙리스트 제외 (정확한 기업명)
        if corp_name in DART_BLACKLIST:
            self.history.add(rcept_no)
            return 'filtered'

        # ③-a 기업명 부분일치 필터
        if DART_CORP_FILTER and any(k in corp_name for k in DART_CORP_FILTER):
            self.history.add(rcept_no)
            return 'filtered'

        # ③-b 공시 제목 필터
        if DART_TITLE_FILTER and any(k in report_nm for k in DART_TITLE_FILTER):
            self.history.add(rcept_no)
            return 'filtered'

        is_my_stock         = (corp_name in COMPANY_CODES) or (bool(stock_code) and stock_code.split(".")[0] in CHAT_IDS_BY_CODE)
        is_global_important = any(k in report_nm for k in GLOBAL_IMPORTANT_KEYWORDS)

        # ④ 내 종목도 아니고 전체 중요 공시도 아니면 스킵
        if not is_my_stock and not is_global_important:
            return 'ignored'

        # ⑤ 공시 중요도 분류
        level = classify_disclosure(report_nm)

        # ⑤-a 감사·검토보고서 승격: 제목만으론 비적정 의견 감지 불가(잡공시 등급) →
        # 원문에서 감사의견 추출, 한정/부적정/의견거절이면 긴급 승격
        audit_note = ""
        if '감사보고서' in report_nm or '검토보고서' in report_nm:
            try:
                opinion = get_audit_opinion(rcept_no)
            except Exception:
                logging.exception(f"⚠️ [공시] 감사의견 확인 실패: {corp_name}")
                opinion = None
            if opinion and opinion != '적정':
                level = 'urgent'
                audit_note = f"🚨 감사의견: {opinion}"
                logging.warning(f"🚨 [공시] 감사의견 비적정({opinion}) → 긴급 승격: {corp_name}")

        # ── 메시지 생성 ──
        is_market_wide = not is_my_stock and is_global_important
        prefix = "🔥 <b>[시장속보]</b> " if is_market_wide else ""
        # 등급 배지 — 긴급/주요 공시가 일반 공시와 한눈에 구분되도록
        if level == 'urgent':
            prefix += "🚨 <b>[긴급]</b> "
        elif level == 'major':
            prefix += "📌 <b>[주요]</b> "
        detail = get_disclosure_detail(rcept_no, report_nm)
        if audit_note:
            detail = f"{audit_note}\n{detail}".strip()
        msg = self._build_msg(corp_name, report_nm, rcept_no, stock_code, prefix, detail)

        # ── 채널 라우팅 — 대상 확정 후 일괄 발송 (성공 여부 추적) ──
        industry = COMPANY_TO_INDUSTRY.get(corp_name)
        _ind_cid = INDUSTRY_CHAT_IDS.get(industry) if industry else None
        _cid     = self._get_company_chat_id(corp_name, stock_code)

        targets = []
        if level == 'urgent':
            # 긴급: 메인(시총 1000억↑) + 산업 + 기업
            if self._is_main_worthy(stock_code):
                targets.append(DEFAULT_CHAT_ID)
            if _ind_cid:
                targets.append(_ind_cid)
            if _cid:
                targets.append(_cid)
        elif level == 'major':
            # 중요: 산업 + 기업 (+ 시장속보/공급계약·수주는 메인, 시총 1000억↑만)
            if _ind_cid:
                targets.append(_ind_cid)
            if _cid:
                targets.append(_cid)
            _to_main = is_market_wide or ("기재정정" not in report_nm and any(k in report_nm for k in ("공급계약", "수주")))
            if _to_main and self._is_main_worthy(stock_code):
                targets.append(DEFAULT_CHAT_ID)
        elif level == 'skip':
            # 잡공시: 기업채널만 (산업/메인 제외)
            if _cid:
                targets.append(_cid)
        else:  # normal
            # 일반: 산업 + 기업 (메인 제외)
            if _ind_cid:
                targets.append(_ind_cid)
            if _cid:
                targets.append(_cid)

        # 같은 방 중복 발송 방지 (산업방=기업방 동일 설정 등) — 순서 보존 dedup
        targets = list(dict.fromkeys(targets))

        results = [stock_api.send_telegram(t, msg) for t in targets]
        failed  = [t for t, ok in zip(targets, results) if not ok]

        # 실패 채널만 재시도 큐 등록 — 사이클마다 재발송, 최대 3회 (H-2)
        # 이력은 아래서 즉시 기록: 재파싱·성공 채널 중복 발송 없이 실패분만 복구
        if failed:
            if len(self._pending) < 50:   # 장애 폭주 시 무한 성장 방지
                self._pending[rcept_no] = {
                    "targets": failed, "msg": msg, "n": 0,
                    "desc": f"{corp_name} {report_nm[:30]}",
                }
            logging.warning(f"⚠️ [공시] {len(failed)}/{len(targets)}채널 발송 실패 "
                            f"— 재시도 큐 등록: {corp_name} {report_nm}")

        # ── AI 분석 (긴급/중요만) ── [임시 중지: 업데이트 후 재적용]
        # Gemini 모델 폐기로 인해 분석 실패 → 임시 비활성화 (2026-06-25)
        # 복구 시 아래 블록 주석 해제 (+ ai_worker 메시지 escape·호재/악재 표현 완화 필요)
        # if level in ('urgent', 'major') and \
        #    any(k in report_nm for k in AI_TRIGGER_KEYWORDS):
        #     logging.info(f"🤖 AI 분석 큐: {corp_name}")
        #     ai_target = self._get_company_chat_id(corp_name, stock_code) or DEFAULT_CHAT_ID
        #     self.ai_executor.submit(
        #         self.ai_worker, ai_target, corp_name, report_nm, rcept_no
        #     )

        # ── 발송 기록 ──
        if _BRIDGE_OK:
            try:
                _bridge.log_notice(
                    target=corp_name,
                    content=f"[공시/{level}] {report_nm}",
                    sent_count=len(targets),
                    ok_count=sum(1 for r in results if r),
                )
            except Exception:
                pass

        self.history.add(rcept_no)
        logging.info(f"✅ [공시/{level}] {corp_name}: {report_nm}")
        bl = self._day_stats["by_level"]
        bl[level] = bl.get(level, 0) + 1
        time.sleep(1)
        return 'fail' if (targets and not any(results)) else 'sent'

    def _flush_retries(self):
        """발송 실패 채널 재시도 — 사이클당 1회, 항목당 최대 3회 후 포기."""
        for rcept_no in list(self._pending):
            ent = self._pending.get(rcept_no)
            if not ent:
                continue
            still = [t for t in ent["targets"] if not stock_api.send_telegram(t, ent["msg"])]
            ent["n"] += 1
            if not still:
                logging.info(f"✅ [공시] 재시도 성공 ({ent['n']}회차): {ent['desc']}")
                self._pending.pop(rcept_no, None)
            elif ent["n"] >= 3:
                logging.error(f"❌ [공시] 재시도 3회 실패 — 포기 ({len(still)}채널): {ent['desc']}")
                self._pending.pop(rcept_no, None)
            else:
                ent["targets"] = still
                logging.warning(f"⚠️ [공시] 재시도 {ent['n']}/3 — 잔여 {len(still)}채널: {ent['desc']}")
            time.sleep(1)

    def _report_daily(self):
        """일일 운영 요약 — 19시 이후 1회 로그 + (발송 있던 날만) 관리자 방 보고."""
        d = self._day_stats
        d["reported"] = True
        bl = d.get("by_level", {})
        parser_line = ""
        try:
            from dart_parser import PARSER_STATS
            cat = sum(v for k, v in PARSER_STATS.items()
                      if k not in ('fallback', 'empty', 'no_html', 'skip_type', 'error'))
            parser_line = (f"파서: 전용 {cat} · 범용 {PARSER_STATS.get('fallback', 0)}"
                           f" · 빈결과 {PARSER_STATS.get('empty', 0)}"
                           f" · 원문없음 {PARSER_STATS.get('no_html', 0)}"
                           f" · 오류 {PARSER_STATS.get('error', 0)}")
            PARSER_STATS.clear()
        except Exception:
            pass
        msg = (f"📊 <b>[공시봇 일일요약]</b> {d['date']}\n"
               f"발송 {d['sent']}건 (긴급 {bl.get('urgent', 0)} · 주요 {bl.get('major', 0)}"
               f" · 일반 {bl.get('normal', 0)} · 잡 {bl.get('skip', 0)})\n"
               f"필터제외 {d['filtered']} · 전채널실패 {d['fail']} · 처리오류 {d['err']}"
               f" · 재시도잔여 {len(self._pending)}"
               + (f"\n{parser_line}" if parser_line else ""))
        logging.info("[공시] 일일요약 | " + msg.replace("\n", " | "))
        if d["sent"] or d["fail"] or d["err"]:
            try:
                from telegram_utils import get_admin_chat_id
                admin = get_admin_chat_id()
                if admin:
                    stock_api.send_telegram(admin, msg)
            except Exception:
                logging.exception("⚠️ [공시] 일일요약 발송 실패")

    def run(self):
        logging.info("🚀 DART Bot Started")
        loop_count = 0

        while True:
            loop_count += 1
            if loop_count % 60 == 1 and _BRIDGE_OK:
                try:
                    _bridge.heartbeat("dart_bot")
                except Exception:
                    pass
                # reload_flag 소비는 watchdog 단일 창구 —
                # 필터 갱신은 config.on_reload(_load_dart_filters) 콜백으로 수신

            now = market_timer.get_now()

            # ── 일일 통계 리셋 / 19시 이후 요약 보고 ──
            today = now.strftime("%Y%m%d")
            if self._day_stats["date"] != today:
                self._day_stats = {"date": today, "reported": False, "sent": 0,
                                   "filtered": 0, "fail": 0, "err": 0, "by_level": {}}
            elif now.hour >= 19 and not self._day_stats["reported"]:
                try:
                    self._report_daily()
                except Exception:
                    logging.exception("⚠️ [공시] 일일요약 생성 실패")
                    self._day_stats["reported"] = True

            if not market_timer.is_weekday():
                for _ in range(60): time.sleep(60)
                continue

            if 7 <= now.hour < 19:
                try:
                    if self._pending:
                        self._flush_retries()
                    st = {"sent": 0, "fail": 0, "filtered": 0, "ignored": 0, "err": 0}
                    items = self._fetch_items(now)
                    for item in reversed(items):
                        try:
                            outcome = self._process_item(item)
                            if outcome:
                                st[outcome] = st.get(outcome, 0) + 1
                        except Exception:
                            st["err"] += 1
                            rn = item.get("rcept_no") or ""
                            logging.exception(
                                f"❌ [공시] 항목 처리 실패 — 스킵({rn}): "
                                f"{item.get('corp_name')} {str(item.get('report_nm', ''))[:40]}")
                            if rn:
                                self.history.add(rn)   # 무한 재시도 차단

                    # 사이클 요약 — 유효 이벤트(발송·제외·실패) 있을 때만 기록
                    if st["sent"] or st["fail"] or st["err"] or st["filtered"]:
                        logging.info(f"[공시] 사이클: 발송 {st['sent']} · 필터제외 {st['filtered']}"
                                     f" · 관심밖 {st['ignored']} · 전채널실패 {st['fail']}"
                                     f" · 오류 {st['err']} · 재시도대기 {len(self._pending)}")
                    d = self._day_stats
                    d["sent"] += st["sent"]; d["filtered"] += st["filtered"]
                    d["fail"] += st["fail"]; d["err"] += st["err"]
                    time.sleep(60)
                except Exception:
                    logging.exception("❌ [공시] 사이클 실패")
                    time.sleep(60)
            else:
                time.sleep(600)


if __name__ == "__main__":
    bot = DartRoutingBot()
    bot.run()
