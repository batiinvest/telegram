# main.py — DART 공시 봇 (개선 버전)
#
# 개선 사항:
#   1. 공시 중요도 3단계 분류 → 채널별 라우팅
#   2. 노이즈성 공시 스킵 (산업/메인 채널에서만)
#   3. 기업 블랙리스트 (app_config에서 관리)
#   4. 스킵/블랙리스트 DB 로드 지원

import requests
import datetime
import time
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from managers import market_timer, HistoryManager, get_session

import stock_api
from ai_analyst import analyze_disclosure_gemini

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
    """app_config에서 공시 등급 키워드 + 각종 필터 로드"""
    global DART_BLACKLIST, URGENT_KEYWORDS, MAJOR_KEYWORDS, SKIP_FOR_BROADCAST
    global DART_TITLE_FILTER, DART_CORP_FILTER
    if not _BRIDGE_OK:
        return
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


def classify_disclosure(report_nm: str) -> str:
    """
    공시 중요도 분류.
    반환: 'urgent' | 'major' | 'skip' | 'normal'
    """
    # 스킵 여부 먼저 (기업채널은 별도 처리)
    if any(k in report_nm for k in SKIP_FOR_BROADCAST):
        return 'skip'
    if any(k in report_nm for k in URGENT_KEYWORDS):
        return 'urgent'
    if any(k in report_nm for k in MAJOR_KEYWORDS):
        return 'major'
    return 'normal'


class DartRoutingBot:
    def __init__(self):
        self.base_url = "https://opendart.fss.or.kr/api/list.json"
        self.history  = HistoryManager("sent_list.txt", max_len=2000)
        self.ai_executor = ThreadPoolExecutor(max_workers=2)
        self.session = get_session()

        # DB에서 블랙리스트 로드
        _load_dart_filters()

    def ai_worker(self, chat_id, corp_name, report_nm, rcept_no):
        logging.info(f"🤖 AI Analyzing: {corp_name}")
        result = analyze_disclosure_gemini(corp_name, report_nm, rcept_no)
        if result:
            msg = (
                f"🤖 <b>[AI 심층 분석] {corp_name}</b>\n"
                f"└ {report_nm}\n\n{result}\n\n⚠️ <i>AI 분석은 참고용입니다.</i>"
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

    def _build_msg(self, corp_name, report_nm, rcept_no, stock_code, prefix=""):
        emoji      = self.get_emoji(report_nm)
        link       = f"http://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}"
        target_code = COMPANY_CODES.get(corp_name, stock_code)
        price_info = stock_api.get_stock_price(target_code)
        stock_msg  = f"<b>{price_info}</b>\n" if price_info else ""
        return (
            f"{prefix}{emoji} <b>[{corp_name}]</b>\n"
            f"{stock_msg}{report_nm}\n"
            f"🔗 <a href='{link}'>공시 원문</a> | "
            f"📈 <a href='https://finance.naver.com/item/main.nhn?code={target_code}'>네이버</a>"
        )

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
                try:
                    if _bridge.check_reload_flag():
                        from config import reload_company_data
                        reload_company_data()
                        _load_dart_filters()
                        logging.info("🔄 [main] 종목 데이터 재로드 완료")
                except Exception as _re:
                    logging.debug(f"reload_flag 체크 오류: {_re}")

            now = market_timer.get_now()
            if not market_timer.is_weekday():
                for _ in range(60): time.sleep(60)
                continue

            if 7 <= now.hour < 19:
                try:
                    today_str = now.strftime("%Y%m%d")
                    params = {
                        "crtfc_key": DART_API_KEY,
                        "bgn_de": today_str, "end_de": today_str,
                        "page_count": 50, "page_no": 1
                    }
                    res  = self.session.get(self.base_url, params=params, timeout=10)
                    data = res.json()

                    if data.get("status") == "000":
                        for item in reversed(data.get("list", [])):
                            rcept_no   = item.get("rcept_no")
                            corp_name  = item.get("corp_name")
                            report_nm  = item.get("report_nm", "")
                            stock_code = item.get("stock_code", "").strip()

                            # ① 처리 이력
                            if self.history.contains(rcept_no):
                                continue

                            # ② 비상장 제외
                            if not stock_code:
                                continue

                            # ③ 블랙리스트 제외 (정확한 기업명)
                            if corp_name in DART_BLACKLIST:
                                self.history.add(rcept_no)
                                continue

                            # ③-a 기업명 부분일치 필터
                            if DART_CORP_FILTER and any(k in corp_name for k in DART_CORP_FILTER):
                                self.history.add(rcept_no)
                                continue

                            # ③-b 공시 제목 필터
                            if DART_TITLE_FILTER and any(k in report_nm for k in DART_TITLE_FILTER):
                                self.history.add(rcept_no)
                                continue

                            is_my_stock        = (corp_name in COMPANY_CODES) or (bool(stock_code) and stock_code.split(".")[0] in CHAT_IDS_BY_CODE)
                            is_global_important = any(k in report_nm for k in GLOBAL_IMPORTANT_KEYWORDS)

                            # ④ 내 종목도 아니고 전체 중요 공시도 아니면 스킵
                            if not is_my_stock and not is_global_important:
                                continue

                            # ⑤ 공시 중요도 분류
                            level = classify_disclosure(report_nm)

                            # ── 메시지 생성 ──
                            is_market_wide = not is_my_stock and is_global_important
                            prefix = "🔥 <b>[시장속보]</b> " if is_market_wide else ""
                            msg = self._build_msg(corp_name, report_nm, rcept_no, stock_code, prefix)

                            # ── 채널 라우팅 ──
                            industry = COMPANY_TO_INDUSTRY.get(corp_name)

                            if level == 'urgent':
                                # 긴급: 메인 + 산업 + 기업
                                stock_api.send_telegram(DEFAULT_CHAT_ID, msg)
                                if industry and industry in INDUSTRY_CHAT_IDS:
                                    stock_api.send_telegram(INDUSTRY_CHAT_IDS[industry], msg)
                                _cid = self._get_company_chat_id(corp_name, stock_code)
                                if _cid:
                                    stock_api.send_telegram(_cid, msg)

                            elif level == 'major':
                                # 중요: 산업 + 기업 (메인 제외)
                                if industry and industry in INDUSTRY_CHAT_IDS:
                                    stock_api.send_telegram(INDUSTRY_CHAT_IDS[industry], msg)
                                _cid = self._get_company_chat_id(corp_name, stock_code)
                                if _cid:
                                    stock_api.send_telegram(_cid, msg)
                                # 시장 전체 중요 공시는 메인에도
                                if is_market_wide:
                                    stock_api.send_telegram(DEFAULT_CHAT_ID, msg)

                            elif level == 'skip':
                                # 잡공시: 기업채널만 (산업/메인 제외)
                                _cid = self._get_company_chat_id(corp_name, stock_code)
                                if _cid:
                                    stock_api.send_telegram(_cid, msg)

                            else:  # normal
                                # 일반: 산업 + 기업 (메인 제외)
                                if industry and industry in INDUSTRY_CHAT_IDS:
                                    stock_api.send_telegram(INDUSTRY_CHAT_IDS[industry], msg)
                                _cid = self._get_company_chat_id(corp_name, stock_code)
                                if _cid:
                                    stock_api.send_telegram(_cid, msg)

                            # ── AI 분석 (긴급/중요만) ──
                            if level in ('urgent', 'major') and \
                               any(k in report_nm for k in AI_TRIGGER_KEYWORDS):
                                logging.info(f"🤖 AI 분석 큐: {corp_name}")
                                ai_target = self._get_company_chat_id(corp_name, stock_code) or DEFAULT_CHAT_ID
                                self.ai_executor.submit(
                                    self.ai_worker, ai_target, corp_name, report_nm, rcept_no
                                )

                            # ── 발송 기록 ──
                            if _BRIDGE_OK:
                                try:
                                    _bridge.log_notice(
                                        target=corp_name,
                                        content=f"[공시/{level}] {report_nm}",
                                        sent_count=1, ok_count=1
                                    )
                                except Exception:
                                    pass

                            self.history.add(rcept_no)
                            logging.info(f"✅ [공시/{level}] {corp_name}: {report_nm}")
                            time.sleep(1)

                    time.sleep(60)
                except Exception as e:
                    logging.error(f"Error: {e}")
                    time.sleep(60)
            else:
                time.sleep(600)


if __name__ == "__main__":
    bot = DartRoutingBot()
    bot.run()
