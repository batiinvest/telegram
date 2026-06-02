import threading
import time
import logging
import sys
import os
import datetime
import json
import re
from managers import market_timer, HistoryManager

# ✅ 스케줄링 라이브러리
import schedule

# ✅ 기존 봇 모듈 임포트
try:
    from realtime_alert import KisMyStockScanner
    from news_main import NaverNewsBot
    from main import DartRoutingBot

    import stock_api
    from config import (
        DEFAULT_CHAT_ID,
        INDUSTRY_CHAT_IDS,
        COMPANY_CHAT_IDS,
        CHAT_IDS_BY_CODE,
        COMPANY_CODES,
        COMMON_BUTTON
    )
    from telegram_utils import get_admin_chat_id as _get_admin_chat_id
except ImportError as e:
    print(f"❌ 필수 파일이 누락되었습니다: {e}")
    sys.exit(1)

# ✅ [추가] 재무/시장 데이터 수집 + 상장사 동기화
try:
    from collect_financials        import run as run_financials, auto_detect_quarter
    # run_financials(year, quarter, monitored_only=True) 형태로 호출 가능
    from collect_market            import run as run_market
    from collect_listed_companies  import run as run_sync_companies
    from grade                     import save_grade_history as _save_grade_history
    from collect_insider           import collect_insider_trades as _collect_insider
    from collect_company_info      import collect_one as _collect_company_one
    _COLLECTOR_OK = True
except ImportError as _ie:
    _COLLECTOR_OK = False
    logging.warning(f"수집 모듈 없음 (무시): {_ie}")

# ✅ [추가] 프로 채널 관리 모듈
try:
    import pro_channel as _pro
    _PRO_OK = True
    logging.info("✅ [Pro] 프로 채널 관리 모듈 로드 완료")
except ImportError as _pe:
    _PRO_OK = False
    logging.warning(f"⚠️ [Pro] pro_channel 모듈 없음 (무시): {_pe}")

# ✅ [추가] KIND IR자료 수집 모듈
try:
    import kind_ir as _kind_ir
    _KIND_IR_OK = True
    logging.info("✅ [KIND IR] 모듈 로드 완료")
except ImportError as _kie:
    _KIND_IR_OK = False
    logging.warning(f"⚠️ [KIND IR] kind_ir 모듈 없음 (무시): {_kie}")

# ✅ [추가] SMS 웹훅 서버 (입금 자동 처리)
try:
    import sms_webhook as _sms_wh
    _SMS_WH_OK = True
    logging.info("✅ [SMS] 웹훅 모듈 로드 완료")
except ImportError as _swe:
    _SMS_WH_OK = False
    logging.warning(f"⚠️ [SMS] sms_webhook 모듈 없음 (pip install flask): {_swe}")

# ✅ [추가] 봇 명령어 수신 (/myid, /status, /start)
try:
    import bot_commands as _bot_cmd
    _BOT_CMD_OK = True
    logging.info("✅ [BotCmd] 명령어 모듈 로드 완료")
except ImportError as _bce:
    _BOT_CMD_OK = False
    logging.warning(f"⚠️ [BotCmd] bot_commands 모듈 없음: {_bce}")

# ✅ [추가] Supabase 브릿지 (실패해도 스케줄러 동작에 영향 없음)
try:
    from supabase_bridge import bridge as _bridge
    _BRIDGE_OK = True
    logging.info("✅ [Bridge] Supabase 브릿지 연결 완료")
    # 봇 시작 시 현재 모니터링 목록 스냅샷 (reload 비교용)
    try:
        _sb_init = _bridge._get_client()
        _init_mon = _sb_init.table('companies').select('code').eq('is_monitored', True).execute()
        _bridge._prev_mon_codes = {r['code'].split('.')[0] for r in (_init_mon.data or [])}
        logging.info(f"📋 [Bridge] 모니터링 종목 {len(_bridge._prev_mon_codes)}개 초기화")
    except Exception as _ie:
        _bridge._prev_mon_codes = set()
except Exception as _be:
    _BRIDGE_OK = False
    logging.warning(f"⚠️ [Bridge] Supabase 브릿지 로드 실패 (스케줄 DB 제어 비활성화): {_be}")

# 공시수집 중복 실행 방지 플래그
_disclosure_running = False

# ==========================================
# ⚙️ 통합 로깅 설정 — logger_config.py 위임
# ==========================================
from logger_config import setup_root_logger
setup_root_logger()   # logs/bati.log (RotatingFileHandler 10MB×5) + 콘솔

# ==========================================
# 🔧 스케줄 ON/OFF 헬퍼
# ==========================================
# ==========================================
# 📡 [공통] 산업별/종목별 일괄 발송 헬퍼
# ==========================================

def _broadcast_to_industries(
    api_func,
    *args,
    sleep: float = 0.5,
    keyboard=None,
    skip: tuple = ("바티인베스트",),
    label: str = "산업",
):
    """
    INDUSTRY_CHAT_IDS 전체를 순회해서 api_func(ind_name, *args) 결과를 발송.

    Args:
        api_func : stock_api의 산업별 메시지 생성 함수
        *args    : api_func에 industry_name 뒤로 전달할 추가 인자
        sleep    : 채널 간 대기 시간 (초)
        keyboard : 텔레그램 키보드 버튼
        skip     : 제외할 채널명 튜플
        label    : 로그용 레이블

    Example:
        _broadcast_to_industries(stock_api.get_industry_theme_ranking,
                                  keyboard=COMMON_BUTTON, label="점심 브리핑")
    """
    for ind_name, chat_id in INDUSTRY_CHAT_IDS.items():
        if ind_name in skip:
            continue
        try:
            msg = api_func(ind_name, *args)
            if msg:
                stock_api.send_telegram(chat_id, msg, keyboard=keyboard)
            time.sleep(sleep)
        except Exception as e:
            logging.error(f"❌ [{label}] 산업 발송 오류 ({ind_name}): {e}")


def _broadcast_to_companies(
    api_func,
    *args,
    sleep: float = 0.5,
    keyboard=None,
    label: str = "종목",
):
    """
    COMPANY_CHAT_IDS 전체를 순회해서 api_func(stock_code, comp_name, *args) 결과를 발송.
    COMPANY_CODES에 없는 종목(코드 미등록)은 자동 스킵.

    Args:
        api_func  : stock_api의 종목별 메시지 생성 함수
                    시그니처: f(code, name, *args) → str | None
        sleep     : 채널 간 대기 시간 (초)
        keyboard  : 텔레그램 키보드 버튼
        label     : 로그용 레이블

    Example:
        _broadcast_to_companies(stock_api.get_stock_detail,
                                 keyboard=COMMON_BUTTON, label="마감 브리핑")
    """
    for comp_name, chat_id in COMPANY_CHAT_IDS.items():
        code = COMPANY_CODES.get(comp_name)
        if not code:
            continue
        try:
            msg = api_func(code, comp_name, *args)
            if msg:
                stock_api.send_telegram(chat_id, msg, keyboard=keyboard)
            time.sleep(sleep)
        except Exception as e:
            logging.error(f"❌ [{label}] 종목 발송 오류 ({comp_name}): {e}")


def _is_enabled(job_key: str) -> bool:
    """DB에서 스케줄 활성화 여부 확인. 브릿지 없으면 항상 True."""
    if not _BRIDGE_OK:
        return True
    try:
        return _bridge.is_schedule_enabled(job_key)
    except Exception:
        return True

def _log_notice(target: str, content: str):
    """발송 기록을 Supabase에 저장. 실패해도 무시."""
    if not _BRIDGE_OK:
        return
    try:
        _bridge.log_notice(target=target, content=content, sent_count=1, ok_count=1)
    except Exception:
        pass

# ==========================================
# 🧵 봇 실행 래퍼 함수
# ==========================================
def run_scanner_bot():
    """실시간 시세 감시 및 챗봇 리스너"""
    try:
        logging.info("🚀 [시세감시 봇] 시작")
        scanner = KisMyStockScanner()
        scanner.run()
    except Exception as e:
        logging.error(f"❌ [시세감시 봇] 종료됨 (에러): {e}")

def run_news_bot():
    """네이버 뉴스 봇"""
    try:
        logging.info("🚀 [뉴스 봇] 시작")
        bot = NaverNewsBot()
        bot.run()
    except Exception as e:
        logging.error(f"❌ [뉴스 봇] 종료됨 (에러): {e}")

def run_dart_bot():
    """DART 공시 봇"""
    try:
        logging.info("🚀 [공시 봇] 시작")
        bot = DartRoutingBot()
        bot.run()
    except Exception as e:
        logging.error(f"❌ [공시 봇] 종료됨 (에러): {e}")


# ==========================================
# ⏰ 작업별 함수
# ==========================================

def job_lunch_briefing():
    if market_timer.is_kr_holiday(): return
    # ✅ [추가] DB에서 스케줄 ON/OFF 확인
    if not _is_enabled("lunch"):
        logging.info("⏸ 점심 브리핑 비활성화 (DB 설정)")
        return
    logging.info("🍱 [점심 브리핑] 시작")

    msg_open = "🍱 <b>[점심 시황]</b> 맛점하세요! 오전 장 요약입니다."
    stock_api.send_telegram(DEFAULT_CHAT_ID, msg_open)
    _log_notice(DEFAULT_CHAT_ID, "[점심 브리핑] 시작")

    stock_api.send_telegram(DEFAULT_CHAT_ID, stock_api.get_market_scoreboard())
    time.sleep(1)
    stock_api.send_telegram(DEFAULT_CHAT_ID, stock_api.get_universe_ranking())

    _broadcast_to_industries(
        stock_api.get_industry_theme_ranking,
        keyboard=COMMON_BUTTON, label="점심 브리핑"
    )


def job_naver_report():
    if market_timer.is_kr_holiday(): return
    # ✅ [추가] DB에서 스케줄 ON/OFF 확인
    if not _is_enabled("report"):
        logging.info("⏸ 네이버 리포트 비활성화 (DB 설정)")
        return
    logging.info("📑 [네이버 리포트] 발송 시작")
    try:
        stock_api.run_naver_report_job()
        _log_notice(DEFAULT_CHAT_ID, "[네이버 리포트] 발송")
    except Exception as e:
        logging.error(f"네이버 리포트 발송 에러: {e}")


def job_daily_closing():
    if market_timer.is_kr_holiday(): return
    # ✅ [추가] DB에서 스케줄 ON/OFF 확인
    if not _is_enabled("closing"):
        logging.info("⏸ 마감 브리핑 비활성화 (DB 설정)")
        return
    logging.info("🏁 [마감 브리핑] 시작")

    msg_close = "🏁 <b>[마감 시황]</b> 오늘 하루 고생 많으셨습니다."
    stock_api.send_telegram(DEFAULT_CHAT_ID, msg_close)
    _log_notice(DEFAULT_CHAT_ID, "[마감 브리핑] 시작")

    stock_api.send_telegram(DEFAULT_CHAT_ID, stock_api.get_market_scoreboard(), keyboard=COMMON_BUTTON)
    time.sleep(1)
    stock_api.send_telegram(DEFAULT_CHAT_ID, stock_api.get_universe_ranking(), keyboard=COMMON_BUTTON)

    _broadcast_to_industries(
        stock_api.get_industry_theme_ranking,
        keyboard=COMMON_BUTTON, label="마감 브리핑"
    )
    _broadcast_to_companies(
        stock_api.get_stock_detail,
        keyboard=COMMON_BUTTON, label="마감 브리핑"
    )


def job_sync_listed_companies():
    """토요일 새벽 1시 — 코스피+코스닥 전체 상장사 동기화 (신규/사명변경/상폐)"""
    if not _COLLECTOR_OK:
        logging.warning("collect_listed_companies.py 없음 — 스킵")
        return
    try:
        logging.info("🏢 [상장사동기화] 시작")
        run_sync_companies()
        logging.info("🏢 [상장사동기화] 완료")
        _log_notice("system", "[상장사동기화] 완료")
    except Exception as e:
        logging.error(f"❌ [상장사동기화] 오류: {e}")


def job_cleanup_market_data():
    """토요일 새벽 — market_data 정리
    - 모니터링 종목: 90일 보존
    - 전체 상장사(비모니터링): 28일 보존
    """
    KEEP_MON  = 90   # 모니터링 종목 보존일
    KEEP_ALL  = 28   # 전체 종목 보존일
    try:
        sb = _bridge._get_client() if _BRIDGE_OK else None
        if not sb:
            logging.error("❌ [정리] Supabase 연결 없음 — 스킵")
            return

        cutoff_all = (datetime.date.today() - datetime.timedelta(days=KEEP_ALL)).isoformat()
        cutoff_mon = (datetime.date.today() - datetime.timedelta(days=KEEP_MON)).isoformat()

        # 모니터링 종목 코드 목록
        _mon = sb.table('companies').select('code').eq('is_monitored', True).execute()
        mon_codes = [r['code'].split('.')[0] for r in (_mon.data or [])]

        # 1) 비모니터링 종목 — 28일 초과 삭제
        if mon_codes:
            sb.table('market_data').delete() \
              .lt('base_date', cutoff_all) \
              .not_.in_('stock_code', mon_codes).execute()
        else:
            sb.table('market_data').delete().lt('base_date', cutoff_all).execute()

        # 2) 모니터링 종목 — 90일 초과 삭제
        if mon_codes:
            # 500개 청크로 분할
            chunk = 200
            for i in range(0, len(mon_codes), chunk):
                sb.table('market_data').delete() \
                  .lt('base_date', cutoff_mon) \
                  .in_('stock_code', mon_codes[i:i+chunk]).execute()

        logging.info(f"🗑️ [정리] market_data 정리 완료 — 모니터링 {KEEP_MON}일 / 전체 {KEEP_ALL}일 보존")
    except Exception as e:
        logging.error(f"❌ [정리] market_data 정리 오류: {e}")



def _preprocess_disclosures(all_disc_records: list, sb) -> list:
    """
    app_config 저장 전 전처리:
      1. 노이즈 공시 제거 (NOISE_KEYWORDS)
      2. 비상장 제거 (stock_code 없는 것) — companies 테이블 조회
      3. 시총 1000억 미만 제거 — market_data 조회 (market_cap 필드도 첨부)
      4. 카테고리 분류 — category 필드 추가
      5. insider 요약 — insider_summary 필드 추가 (rcept_no 기준)
    반환: 전처리된 레코드 리스트 (각 항목에 category, market_cap, insider_summary 포함)
    """

    # ── 1. 노이즈 제거 ──────────────────────────────────────────────
    NOISE_KEYWORDS = [
        '효력발생안내', '투자설명서(일괄신고)', '일괄신고추가서류',
        '일괄신고서(', '증권발행실적보고서', '[발행조건확정]',
        '해산사유발생', '의결권대리행사권유',
        '동일인등출자계열회사와의상품ㆍ용역거래',
        '정정신고서제출요구', '소속부변경', '소액공모공시서류', '집합투자증권',
    ]
    records = [
        d for d in all_disc_records
        if not any(kw in (d.get('report_nm') or '') for kw in NOISE_KEYWORDS)
    ]
    logging.info(f"📋 [전처리] 노이즈 제거: {len(all_disc_records)}→{len(records)}건")

    # ── 2 & 3. companies + market_data 조회 (상장 여부 + 시총) ─────
    corp_codes = list({d['corp_code'] for d in records if d.get('corp_code')})
    listed_set = set()    # 상장 corp_code
    cap_map    = {}       # corp_code → market_cap

    try:
        # 2. 상장 여부
        chunk = 500
        for i in range(0, len(corp_codes), chunk):
            res = sb.table('companies').select('corp_code,code') \
                    .in_('corp_code', corp_codes[i:i+chunk]).execute()
            for c in (res.data or []):
                listed_set.add(c['corp_code'])

        # 3. 시총 — 전체 corp_codes → stock_code 매핑 (500개 제한 버그 수정)
        stock_map = {}   # corp_code → stock_code
        for i in range(0, len(corp_codes), chunk):
            res2 = sb.table('companies').select('corp_code,code') \
                      .in_('corp_code', corp_codes[i:i+chunk]).execute()
            for c in (res2.data or []):
                sc = (c.get('code') or '').replace('.KS', '').replace('.KQ', '')
                if sc:
                    stock_map[c['corp_code']] = sc

        stock_codes = list(set(stock_map.values()))
        if stock_codes:
            date_res = sb.table('market_data').select('base_date') \
                         .order('base_date', desc=True).limit(1).execute()
            max_date = (date_res.data or [{}])[0].get('base_date')
            if max_date:
                mkt_map = {}
                for i in range(0, len(stock_codes), chunk):
                    mkt_res = sb.table('market_data').select('stock_code,market_cap') \
                                .eq('base_date', max_date) \
                                .in_('stock_code', stock_codes[i:i+chunk]).execute()
                    mkt_map.update({m['stock_code']: m['market_cap'] for m in (mkt_res.data or [])})
                for cc, sc in stock_map.items():
                    if sc in mkt_map:
                        cap_map[cc] = mkt_map[sc]

        logging.info(f"📋 [전처리] 상장사 {len(listed_set)}개 / 시총 {len(cap_map)}개 조회 완료")
    except Exception as e:
        logging.warning(f"⚠️ [전처리] companies/market_data 조회 실패 (필터 스킵): {e}")

    CAP_THRESHOLD = 100_000_000_000  # 1000억

    filtered = []
    for d in records:
        cc = d.get('corp_code')
        # 비상장 제거 (listed_set에 없으면 제외 — 단, listed_set이 비어있으면 전부 통과)
        if listed_set and cc and cc not in listed_set:
            continue
        # 시총 1000억 미만 제거 (market_cap 정보가 있는 경우만)
        cap = cap_map.get(cc)
        if cap is not None and cap < CAP_THRESHOLD:
            continue
        filtered.append(d)
    logging.info(f"📋 [전처리] 상장/시총 필터: {len(records)}→{len(filtered)}건")

    # ── 4. 카테고리 분류 ───────────────────────────────────────────
    CATEGORIES = [
        ('사업보고서',    ['사업보고서']),
        ('반기보고서',    ['반기보고서']),
        ('분기보고서',    ['분기보고서']),
        ('기업설명회(IR)',['기업설명회', 'IR개최', 'NDR']),
        ('잠정실적',      ['잠정', '결산실적', '실적(공정공시)']),
        ('주요사항',      ['주요사항보고']),
        ('증자/감자',     ['유상증자', '무상증자', '감자']),
        ('합병/분할',     ['합병', '분할', '영업양수', '영업양도']),
        ('사채/전환',     ['전환사채', '신주인수권', '교환사채', '사채권']),
        ('자사주',        ['자기주식']),
        ('배당',          ['배당']),
        ('최대주주변동',  ['최대주주등소유주식변동', '최대주주변동']),
        ('대량보유',      ['대량보유상황보고서', '대량보유']),
        ('거래계획(예고)',['거래계획보고서']),
        ('거래계획(철회)',['거래계획철회보고서']),
        ('지분공시',      ['소유상황보고서', '임원ㆍ주요주주', '임원·주요주주']),
        ('임원/주식',     ['임원', '주요주주']),
        ('감사보고서',    ['감사보고서', '내부회계']),
        ('공정공시',      ['공정공시']),
        ('주식매수선택권',['주식매수선택권']),
        ('주요경영사항',  [
            '투자판단관련주요경영사항', '단일판매ㆍ공급계약', '단일판매·공급계약',
            '소송등의판결', '타인에대한채무보증', '특수관계인으로부터자금차입',
            '특수관계인에대한자금대여', '특수관계인에대한출자', '특수관계인으로부터자산양수',
            '특수관계인의유상증자참여', '금전대여결정', '단기차입금증가',
            '기업가치제고계획', '대표이사변경', '사외이사의선임',
            '증권발행결과', '주식병합결정', '감자완료',
        ]),
        ('증권신고',      ['증권신고서', '투자설명서']),
    ]

    def _classify(report_nm: str) -> str:
        for label, keywords in CATEGORIES:
            if any(kw in report_nm for kw in keywords):
                return label
        return '기타'

    for d in filtered:
        d['category'] = _classify(d.get('report_nm') or '')

    # ── 5. insider 요약 첨부 (insider_trades DB에서 오늘 데이터 조회) ──
    try:
        today_str = datetime.date.today().isoformat()
        ins_res = sb.table('insider_trades') \
                    .select('rcept_no,corp_code,reporter,shares_change,hold_ratio_before,hold_ratio_after,plan_period,report_type') \
                    .eq('base_date', today_str).execute()
        ins_rows = ins_res.data or []

        ins_map      = {}  # rcept_no → [rows]
        corp_ins_map = {}  # corp_code → [rows]
        for r in ins_rows:
            if r.get('rcept_no'):
                ins_map.setdefault(r['rcept_no'], []).append(r)
            if r.get('corp_code'):
                corp_ins_map.setdefault(r['corp_code'], []).append(r)

        for d in filtered:
            rno = d.get('rcept_no')
            cc  = d.get('corp_code')
            cat = d.get('category', '')
            summary = None

            if cat == '지분공시' and cc and cc in corp_ins_map:
                rows = corp_ins_map[cc]
                buy  = sum(r['shares_change'] for r in rows if (r.get('shares_change') or 0) > 0)
                sell = sum(abs(r['shares_change']) for r in rows if (r.get('shares_change') or 0) < 0)
                summary = {'type': 'insider', 'buy': buy, 'sell': sell, 'count': len(rows)}
            elif cat == '대량보유' and rno and rno in ins_map:
                row = ins_map[rno][0]
                chg = row.get('shares_change') or 0
                summary = {
                    'type': 'bulk', 'change': chg,
                    'ratio_before': row.get('hold_ratio_before'),
                    'ratio_after':  row.get('hold_ratio_after'),
                }
            elif cat == '최대주주변동' and rno and rno in ins_map:
                summary = {'type': 'major'}
            elif cat == '거래계획(예고)' and rno and rno in ins_map:
                rows = ins_map[rno]
                buy  = sum(r['shares_change'] for r in rows if (r.get('shares_change') or 0) > 0)
                sell = sum(abs(r['shares_change']) for r in rows if (r.get('shares_change') or 0) < 0)
                periods = list({r['plan_period'] for r in rows if r.get('plan_period')})
                summary = {'type': 'plan', 'buy': buy, 'sell': sell,
                           'period': periods[0] if periods else None}
            if summary:
                d['insider_summary'] = summary

        logging.info(f"📋 [전처리] insider 요약 첨부 완료 ({len(ins_rows)}건 참조)")
    except Exception as e:
        logging.warning(f"⚠️ [전처리] insider 요약 첨부 실패 (스킵): {e}")

    return filtered


def job_collect_financials():
    """평일 18:30 — 오늘 DART 공시된 종목만 재무 수집"""
    if not _COLLECTOR_OK:
        logging.warning("collect_financials.py 없음 — 수집 스킵")
        return
    if not _is_enabled('collect_financials'):
        logging.info("⏸ 재무수집 비활성화 (DB 설정)")
        return
    try:
        import OpenDartReader
        dart   = OpenDartReader(os.environ.get('DART_API_KEY',''))
        today  = datetime.date.today().strftime('%Y%m%d')
        today_display = datetime.date.today().strftime('%Y-%m-%d')  # 가독성용
        year, quarter = auto_detect_quarter()

        # ── 오늘 전체 공시 조회 (final=False → 임원변동, 주요사항 등 모두 포함) ──
        disc_all  = dart.list(start=today, end=today, final=False)

        # ── 재무수집용: 정기보고서 최종본만 (final=True → 정정 전 원본 중복 방지) ──
        disc_fin  = dart.list(start=today, end=today, final=True)

        # 전체 공시 목록 구성 (disc_all 기준)
        all_disc_records = []
        seen_rcept = set()
        if disc_all is not None and not disc_all.empty:
            for _, row in disc_all.iterrows():
                rno = row.get('rcept_no', '')
                seen_rcept.add(rno)
                all_disc_records.append({
                    'corp_name': row.get('corp_name', ''),
                    'report_nm': row.get('report_nm', ''),
                    'rcept_dt':  row.get('rcept_dt',  ''),
                    'corp_code': row.get('corp_code', ''),
                    'rcept_no':  row.get('rcept_no',  ''),
                })

        # disc_fin의 정기보고서 중 disc_all에 없는 것 병합
        # (final=False에서 누락되는 경우 보완)
        target_types = ['분기보고서', '반기보고서', '사업보고서']
        if disc_fin is not None and not disc_fin.empty:
            fin_only = disc_fin[disc_fin['report_nm'].str.contains('|'.join(target_types), na=False)]
            for _, row in fin_only.iterrows():
                rno = row.get('rcept_no', '')
                if rno and rno not in seen_rcept:
                    all_disc_records.append({
                        'corp_name': row.get('corp_name', ''),
                        'report_nm': row.get('report_nm', ''),
                        'rcept_dt':  row.get('rcept_dt',  ''),
                        'corp_code': row.get('corp_code', ''),
                        'rcept_no':  rno,
                    })
                    seen_rcept.add(rno)

        # ── 분기/반기/사업보고서만 필터 (재무수집 + 실적공시 카드용) ──
        def _parse_quarter(report_nm: str):
            """report_nm에서 실제 분기 파싱. 예: '사업보고서 (2025.12)' → ('2025', 'Q4')"""
            m = re.search(r'\((\d{4})\.(\d{2})\)', report_nm)
            if not m:
                return None, None
            y, mo = m.group(1), int(m.group(2))
            return y, {3:'Q1', 6:'Q2', 9:'Q3', 12:'Q4'}.get(mo)

        today_corps  = []
        if disc_fin is not None and not disc_fin.empty:
            filtered = disc_fin[
                disc_fin['report_nm'].str.contains('|'.join(target_types), na=False) &
                ~disc_fin['report_nm'].str.contains('정정|기재정정', na=False)  # ✅ 정정 공시 제외
            ]
            if not filtered.empty:
                # 종목별로 report_nm에서 실제 분기 파싱 → 분기별 그룹핑
                from collect_financials import run_by_corp_codes
                quarter_groups = {}  # {(year, quarter): [corp_code, ...]}
                seen = {}

                for _, row in filtered.iterrows():
                    code  = row['corp_code']
                    nm    = row.get('report_nm', '')
                    is_amended = '정정' in nm or '기재정정' in nm
                    y, q  = _parse_quarter(nm)

                    # 동일 corp_code 중복 제거 (정정 우선)
                    if code not in seen or (is_amended and not seen[code]['is_amended']):
                        seen[code] = {
                            'corp_code':  code,
                            'corp_name':  row.get('corp_name', ''),
                            'report_nm':  nm,
                            'is_amended': is_amended,
                        }
                        if y and q:
                            key = (y, q)
                            quarter_groups.setdefault(key, [])
                            if code not in quarter_groups[key]:
                                quarter_groups[key].append(code)

                today_corps = list(seen.values())

                # ── KOSPI/KOSDAQ 상장사만 필터 ──────────────────────────
                # DART corp_cls: Y=유가증권(KOSPI), K=코스닥, E=코넥스, N=기타(외감법인/펀드 등)
                # disc_fin에 corp_cls 컬럼이 있으면 직접 필터
                if 'corp_cls' in filtered.columns:
                    listed_codes = set(
                        filtered[filtered['corp_cls'].isin(['Y', 'K'])]['corp_code'].tolist()
                    )
                    today_corps = [c for c in today_corps if c['corp_code'] in listed_codes]
                    logging.info(f"📊 [재무수집] 상장사 필터 적용: {len(seen)}개 → {len(today_corps)}개")
                else:
                    # corp_cls 없으면 companies DB에서 확인
                    if _BRIDGE_OK:
                        try:
                            _sb_tmp = _bridge._get_client()
                            all_corp_codes = [c['corp_code'] for c in today_corps if c.get('corp_code')]
                            if all_corp_codes:
                                chunk = 500
                                listed_set = set()
                                for i in range(0, len(all_corp_codes), chunk):
                                    _res = _sb_tmp.table('companies').select('corp_code') \
                                                  .in_('corp_code', all_corp_codes[i:i+chunk]).execute()
                                    for r in (_res.data or []):
                                        listed_set.add(r['corp_code'])
                                today_corps = [c for c in today_corps if c['corp_code'] in listed_set]
                                logging.info(f"📊 [재무수집] 상장사 필터(DB): {len(seen)}개 → {len(today_corps)}개")
                        except Exception as _fe:
                            logging.warning(f"⚠️ [재무수집] 상장사 필터 실패 (전체 포함): {_fe}")

                # 분기별로 나눠서 수집
                total_ok, total_fail = 0, 0
                for (y, q), codes in quarter_groups.items():
                    logging.info(f"📊 [재무수집] {y} {q} — {len(codes)}개 종목 수집 시작")
                    ok, fail = run_by_corp_codes(codes, y, q)
                    total_ok += ok; total_fail += fail
                    logging.info(f"📊 [재무수집] {y} {q} 완료: 성공 {ok}개, 실패 {fail}개")

                logging.info(f"📊 [재무수집] 전체 완료: 성공 {total_ok}개, 실패 {total_fail}개")
                _log_notice("system", f"[재무수집] 오늘 공시 {len(today_corps)}개 → {total_ok}개 완료")
            else:
                logging.info(f"📊 [재무수집] 오늘 재무 공시 없음 — 재무수집 스킵, 공시목록만 저장")
        else:
            logging.info(f"📊 [재무수집] 오늘({today}) 정기보고서 공시 없음 — 공시목록만 저장")

        # ── 지분공시 파싱 (임원 매수/매도) — app_config 저장 전에 실행 ──
        if all_disc_records and _COLLECTOR_OK and _BRIDGE_OK:
            try:
                dart_key = os.environ.get('DART_API_KEY', '')
                sb2 = _bridge._get_client()
                _collect_insider(all_disc_records, dart_key, sb2)
            except Exception as _ie:
                logging.error(f"❌ [지분공시] 수집 오류: {_ie}")

        # ── 공시 저장 (항상 실행 — 날짜 기준으로 갱신) ──────────────
        if _BRIDGE_OK:
            sb = _bridge._get_client()

            # 전체 공시: 저장 전 전처리 (노이즈 제거 + 상장 필터 + 시총 + 카테고리 + insider 요약)
            # insider_trades는 이미 위에서 _collect_insider로 저장 완료된 상태이므로
            # _preprocess_disclosures가 insider_trades DB를 바로 읽어 요약 첨부 가능
            processed_disclosures = _preprocess_disclosures(all_disc_records, sb)

            # ── 1. 실적 공시 종목 → app_config (소량 JSON, 유지) ──
            sb.table('app_config').upsert({
                'key':         'today_earnings_corps',
                'value':       json.dumps(today_corps, ensure_ascii=False),
                'description': f'{today_display} 실적 공시 종목 목록'
            }, on_conflict='key').execute()

            # ── 2. 전체 공시 → daily_disclosures 테이블 (app_config JSON 탈출) ──
            _today_str = datetime.date.today().isoformat()

            # 오늘 기존 데이터 삭제 후 재삽입 (전체 갱신)
            sb.table('daily_disclosures').delete().eq('base_date', _today_str).execute()

            if processed_disclosures:
                rows_to_insert = []
                for d in processed_disclosures:
                    rows_to_insert.append({
                        'base_date':       _today_str,
                        'corp_code':       d.get('corp_code') or '',
                        'corp_name':       d.get('corp_name') or '',
                        'report_nm':       d.get('report_nm') or '',
                        'rcept_no':        d.get('rcept_no') or None,
                        'rcept_dt':        d.get('rcept_dt') or None,
                        'category':        d.get('category') or '기타',
                        'market_cap':      d.get('market_cap') or None,
                        'insider_summary': d.get('insider_summary') or None,
                    })
                # 100개 배치 삽입
                for i in range(0, len(rows_to_insert), 100):
                    sb.table('daily_disclosures') \
                      .insert(rows_to_insert[i:i + 100]) \
                      .execute()

            logging.info(
                f"📊 [공시저장] 실적공시 {len(today_corps)}건 / "
                f"전체공시 {len(all_disc_records)}→{len(processed_disclosures)}건 "
                f"daily_disclosures 저장 완료"
            )

        # 재무수집 완료 후 등급 이력 저장
        if today_corps and quarter_groups:
            for (y, q) in quarter_groups.keys():
                job_save_grade_history(y, q)

    except Exception as e:
        logging.error(f"❌ [재무수집] 오류: {e}")


def job_save_grade_history(year: str = None, quarter: str = None):
    """재무수집 완료 후 — 등급 계산 & earnings_grade_history 저장 + 등급 변경 알림"""
    if not _BRIDGE_OK:
        return
    try:
        if not year or not quarter:
            year, quarter = auto_detect_quarter()
        sb = _bridge._get_client()
        result = _save_grade_history(sb, year, quarter)

        # ── 등급 변경 알림 발송 ──────────────────────────────────────
        new_list  = result.get('new',  [])
        up_list   = result.get('up',   [])
        down_list = result.get('down', [])

        if not (new_list or up_list or down_list):
            return

        GRADE_EMOJI = {'S': '🏆', 'A': '🥇', 'B': '🥈', '관찰': '⚡'}
        GRADE_COLOR = {'S': '금', 'A': '주황', 'B': '파랑', '관찰': '초록'}

        def fmt_row(r, change_label):
            g  = r['grade']
            em = GRADE_EMOJI.get(g, '')
            rev_str = ''
            if r.get('rev_yoy') is not None:
                rev_str = f" | YoY {'+' if r['rev_yoy'] > 0 else ''}{r['rev_yoy']:.1f}%"
            op_str = ''
            if r.get('op_yoy') is not None:
                op_str = f" / 영업익 {'+' if r['op_yoy'] > 0 else ''}{r['op_yoy']:.1f}%"
            return f"{em} <b>{r['corp_name']}</b> {g}급 {change_label}{rev_str}{op_str}"

        sections = []

        if new_list:
            lines = [fmt_row(r, '신규진입') for r in new_list[:15]]
            if len(new_list) > 15:
                lines.append(f"... 외 {len(new_list) - 15}개")
            sections.append("🆕 <b>신규 진입</b>\n" + "\n".join(lines))

        if up_list:
            lines = [fmt_row(r, '↑') for r in up_list[:10]]
            if len(up_list) > 10:
                lines.append(f"... 외 {len(up_list) - 10}개")
            sections.append("📈 <b>등급 향상</b>\n" + "\n".join(lines))

        if down_list:
            lines = [fmt_row(r, '↓') for r in down_list[:10]]
            if len(down_list) > 10:
                lines.append(f"... 외 {len(down_list) - 10}개")
            sections.append("📉 <b>등급 하락</b>\n" + "\n".join(lines))

        msg = (
            f"📊 <b>[실적 등급 변동] {year} {quarter}</b>\n"
            f"{'─' * 20}\n"
            + "\n\n".join(sections)
        )

        # 개인 채팅방(admin_chat_id)으로만 발송
        target = _get_admin_chat_id(fallback=DEFAULT_CHAT_ID)
        stock_api.send_telegram(target, msg)
        logging.info(
            f"📢 [등급알림] {year} {quarter} → {target} — "
            f"신규 {len(new_list)}개 / 향상 {len(up_list)}개 / 하락 {len(down_list)}개"
        )

    except Exception as e:
        logging.error(f"❌ [등급이력] 오류: {e}")


def job_collect_macro():
    """글로벌 매크로 데이터 수집 (지수/환율/원자재) — 06:30 아침 수집 시 메인 채널 발송 포함"""
    if not _is_enabled('collect_macro'):
        return
    logging.info("=== 매크로 데이터 수집 시작 ===")
    try:
        import collect_macro
        data = collect_macro.collect_all()
        collect_macro.save_to_db(data)
        logging.info("=== 매크로 데이터 수집 완료 ===")

        # 06:xx 아침 수집에만 메인 채널 브리핑 발송 (16:10 저녁 수집 제외)
        if datetime.datetime.now().hour == 6 and _is_enabled('macro_briefing'):
            msg = stock_api.get_macro_briefing(data)
            if msg:
                stock_api.send_telegram(DEFAULT_CHAT_ID, msg)
                _log_notice(DEFAULT_CHAT_ID, "[매크로 브리핑] 발송")
                logging.info("📊 [매크로 브리핑] 메인 채널 발송 완료")
    except Exception as e:
        logging.error(f"매크로 데이터 수집 실패: {e}")


def job_collect_analyst_opinions():
    """증권사 투자의견 수집 (하루 2회: 장전 + 장후)"""
    if datetime.datetime.now().weekday() >= 5:
        return
    logging.info("📋 [투자의견] 증권사 투자의견 수집 시작")
    try:
        import collect_market
        saved = collect_market.collect_analyst_opinions()
        logging.info(f"📋 [투자의견] 완료: {saved}건 저장")
    except Exception as e:
        logging.error(f"❌ [투자의견] 수집 실패: {e}")


def job_collect_foreign_institution():
    """기관/외국인 매매가집계 수집 (장중 4회 + 장마감 후)"""
    if datetime.datetime.now().weekday() >= 5:
        return
    logging.info("=== 기관/외국인 수급 수집 시작 ===")
    try:
        import collect_market
        result = collect_market.collect_foreign_institution()
        logging.info(f"=== 수급 수집 완료: 외국인 {len(result['frgn_buy'])}개 / 기관 {len(result['orgn_buy'])}개 / 동시매수 {len(result['both_buy'])}개 ===")
    except Exception as e:
        logging.error(f"❌ 수급 수집 실패: {e}")


def job_collect_new_high():
    """장 마감 후 신고가 종목 수집"""
    if datetime.datetime.now().weekday() >= 5:
        return
    logging.info("=== 신고가 종목 수집 시작 ===")
    try:
        import collect_market
        rows = collect_market.collect_new_high()
        logging.info(f"=== 신고가 수집 완료: {len(rows)}개 ===")
    except Exception as e:
        logging.error(f"❌ 신고가 수집 실패: {e}")


def job_collect_us_etf():
    """미국 산업별 ETF 수집 → us_market 테이블"""
    if not _is_enabled('collect_macro'):
        return
    logging.info("=== US ETF 수집 시작 ===")
    try:
        import collect_us_etf
        collect_us_etf.collect_and_save(days=5)
        logging.info("=== US ETF 수집 완료 ===")
    except Exception as e:
        logging.error(f"❌ US ETF 수집 실패: {e}")


def job_collect_market():
    """평일 장중 — KIS 모니터링 종목(306개) 시장 데이터 수집"""
    if datetime.datetime.now().weekday() >= 5 or market_timer.is_kr_holiday():
        logging.info("⏸ [시장수집] 주말/공휴일 스킵")
        return
    if not _COLLECTOR_OK:
        return
    try:
        logging.info("📈 [시장수집] 시작")
        ok, fail = run_market()
        logging.info(f"📈 [시장수집] 완료: 성공 {ok}개, 실패 {fail}개")
        _log_notice("system", f"[시장수집] 완료 ({ok}개)")
        # 관리종목/투자유의/시장경보 체크
        _check_market_warnings()
    except Exception as e:
        logging.error(f"❌ [시장수집] 오류: {e}")


def _check_market_warnings():
    """
    모니터링 종목 중 관리종목·투자유의·시장경보 진입 종목 감지 → 개인 채팅방 알림.
    시장경보 코드: 00=정상, 01=주의, 02=경고, 03=위험
    """
    if not _BRIDGE_OK:
        return
    try:
        today = datetime.date.today().isoformat()
        sb = _bridge._get_client()

        # 오늘 수집된 모니터링 종목 중 경보 있는 것 조회
        res = sb.table('market_data') \
                .select('stock_code,corp_name,market_warn_code,is_caution,price,price_change_rate') \
                .eq('base_date', today) \
                .or_('is_caution.eq.true,market_warn_code.neq.00') \
                .execute()

        alerts = res.data or []
        if not alerts:
            return

        # 어제 경보 이력 조회 (신규 진입만 알림)
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        prev_res = sb.table('market_data') \
                     .select('stock_code,market_warn_code,is_caution') \
                     .eq('base_date', yesterday) \
                     .or_('is_caution.eq.true,market_warn_code.neq.00') \
                     .execute()
        prev_warn = {r['stock_code'] for r in (prev_res.data or [])}

        # 신규 진입만 필터
        new_alerts = [a for a in alerts if a['stock_code'] not in prev_warn]
        if not new_alerts:
            return

        target = _get_admin_chat_id(fallback=DEFAULT_CHAT_ID)

        WARN_LABEL = {'00': '정상', '01': '⚠️ 주의', '02': '🚨 경고', '03': '🆘 위험'}
        lines = []
        for a in new_alerts:
            warn = WARN_LABEL.get(a.get('market_warn_code', '00'), '')
            caution = '🔴 투자유의' if a.get('is_caution') else ''
            chg = f"{'+' if (a.get('price_change_rate') or 0) >= 0 else ''}{a.get('price_change_rate', 0):.2f}%"
            lines.append(
                f"• <b>{a['corp_name']}</b> {warn}{' ' + caution if caution else ''} "
                f"({a.get('price', 0):,}원 {chg})"
            )

        msg = (
            f"⚠️ <b>[시장경보 신규 진입] {today}</b>\n"
            f"{'─' * 20}\n"
            + "\n".join(lines)
        )
        stock_api.send_telegram(target, msg)
        logging.info(f"⚠️ [시장경보] 신규 진입 {len(new_alerts)}개 알림 발송")

    except Exception as e:
        logging.debug(f"시장경보 체크 오류: {e}")


def job_collect_market_closing():
    """평일 장 마감 후 (15:40) — 전체 상장사 시장 데이터 수집"""
    if market_timer.is_kr_holiday():  # 주말/공휴일 스킵
        logging.info("⏸ [시장수집-전체] 주말/공휴일 스킵")
        return
    if not _COLLECTOR_OK:
        return
    try:
        logging.info("📊 [시장수집-전체] 장 마감 후 전체 상장사 수집 시작")
        ok, fail = run_market(all_listed=True)
        logging.info(f"📊 [시장수집-전체] 완료: 성공 {ok}개, 실패 {fail}개")
        _log_notice("system", f"[시장수집-전체] 완료 ({ok}개)")

        # 수집 완료 후 기간별 수익률 일괄 계산
        try:
            from collect_market import calculate_returns
            _sb_ret = _bridge._get_client() if _BRIDGE_OK else None
            if _sb_ret:
                calculate_returns(_sb_ret)
        except Exception as _re:
            logging.error(f"❌ [수익률] 계산 오류: {_re}")
    except Exception as e:
        logging.error(f"❌ [시장수집-전체] 오류: {e}")

    # 시장 수집 완료 후 관심가/목표가 도달 알림 (장 마감 기준)
    job_watchlist_alert()


def job_sector_summary():
    """평일 장 마감 후 (17:15) — 산업별 일별 요약 집계 (sector_daily_summary)"""
    if market_timer.is_kr_holiday():
        logging.info("⏸ [섹터요약] 주말/공휴일 스킵")
        return
    try:
        logging.info("📊 [섹터요약] collect_sector_summary 실행")
        from collect_sector_summary import run as run_sector
        run_sector()
        logging.info("✅ [섹터요약] 완료")
    except Exception as e:
        logging.error(f"❌ [섹터요약] 오류: {e}")


def job_leading_stocks():
    """평일 장 마감 후 (17:30) — 주도주 탐색기 스코어 계산 및 저장"""
    if market_timer.is_kr_holiday():
        logging.info("⏸ [주도주] 주말/공휴일 스킵")
        return
    try:
        logging.info("🚀 [주도주] leading_stocks_generator 실행 시작")
        from leading_stocks_generator import run as run_leading
        run_leading()
        logging.info("✅ [주도주] 생성 완료")
    except Exception as e:
        logging.error(f"❌ [주도주] 생성 오류: {e}")


def job_watchlist_alert():
    """
    장 마감 후 — watchlist 종목의 관심가/목표가 도달 여부 체크 & 개별 알림.

    체크 조건:
      - watch_price:  현재가 ≤ 관심가 (매수 고려 구간 진입)
      - target_price: 현재가 ≥ 목표가 × 0.95 (목표가 5% 이내 근접 또는 도달)

    알림 발송 대상:
      - DEFAULT_CHAT_ID (메인 채팅방)
      - 해당 종목 전용 채팅방 (COMPANY_CHAT_IDS에 있을 경우)

    중복 방지:
      - app_config의 watchlist_alerted_today 키에 오늘 알림 발송한
        (stock_code, alert_type) 세트를 저장. 당일 중복 발송 방지.
    """
    if not _BRIDGE_OK:
        return

    today = datetime.date.today().isoformat()

    try:
        sb = _bridge._get_client()

        # ── 오늘 이미 발송한 알림 목록 조회 ──
        alerted_today = set()
        try:
            cfg_res = sb.table('app_config') \
                        .select('value').eq('key', 'watchlist_alerted_today').single().execute()
            if cfg_res.data:
                raw = json.loads(cfg_res.data['value'])
                # 오늘 날짜 것만 유지
                if raw.get('date') == today:
                    alerted_today = set(tuple(x) for x in raw.get('alerted', []))
        except Exception:
            pass  # 없으면 빈 세트로 시작

        # ── watchlist 전체 조회 ──
        wl_res = sb.table('watchlist') \
                   .select('stock_code,corp_name,watch_price,target_price,group_name') \
                   .execute()
        watchlist = [
            w for w in (wl_res.data or [])
            if w.get('watch_price') or w.get('target_price')
        ]
        if not watchlist:
            return

        # ── 최신 market_data 조회 (오늘 또는 최근 거래일) ──
        date_res = sb.table('market_data') \
                     .select('base_date').order('base_date', desc=True).limit(1).execute()
        max_date = (date_res.data or [{}])[0].get('base_date')
        if not max_date:
            return

        codes = list({w['stock_code'] for w in watchlist if w.get('stock_code')})
        mkt_res = sb.table('market_data') \
                    .select('stock_code,price,price_change_rate') \
                    .eq('base_date', max_date) \
                    .in_('stock_code', codes) \
                    .execute()
        price_map = {r['stock_code']: r for r in (mkt_res.data or [])}

        # ── 도달 여부 체크 ──
        alerts = []  # [(stock_code, alert_type, msg)]

        for w in watchlist:
            code  = w.get('stock_code')
            name  = w.get('corp_name', code)
            mkt   = price_map.get(code)
            if not mkt or not mkt.get('price'):
                continue

            price = mkt['price']
            chg   = mkt.get('price_change_rate', 0) or 0
            chg_str = f"{'+' if chg >= 0 else ''}{chg:.2f}%"

            # 관심가 도달: 현재가 ≤ 관심가
            watch_p = w.get('watch_price')
            if watch_p and price <= watch_p:
                key = (code, 'watch')
                if key not in alerted_today:
                    gap = (watch_p - price) / watch_p * 100
                    msg = (
                        f"🔔 <b>[관심가 도달] {name}</b>\n"
                        f"현재가 {price:,.0f}원 ({chg_str})\n"
                        f"관심가 {watch_p:,.0f}원 — {gap:.1f}% 하회\n"
                        f"📈 <a href='https://finance.naver.com/item/main.nhn?code={code}'>네이버 금융</a>"
                    )
                    alerts.append((code, name, 'watch', msg))
                    alerted_today.add(key)

            # 목표가 근접: 현재가 ≥ 목표가 × 0.95
            target_p = w.get('target_price')
            if target_p and price >= target_p * 0.95:
                key = (code, 'target')
                if key not in alerted_today:
                    gap = (price - target_p) / target_p * 100
                    reached = price >= target_p
                    label = "도달 🎯" if reached else f"근접 ({abs(gap):.1f}% 이내)"
                    msg = (
                        f"{'🎯' if reached else '⚠️'} <b>[목표가 {label}] {name}</b>\n"
                        f"현재가 {price:,.0f}원 ({chg_str})\n"
                        f"목표가 {target_p:,.0f}원\n"
                        f"📈 <a href='https://finance.naver.com/item/main.nhn?code={code}'>네이버 금융</a>"
                    )
                    alerts.append((code, name, 'target', msg))
                    alerted_today.add(key)

        # ── 알림 발송 ──
        if alerts:
            target = _get_admin_chat_id(fallback=DEFAULT_CHAT_ID)

            for code, name, alert_type, msg in alerts:
                stock_api.send_telegram(target, msg)
                logging.info(f"📢 [관심가알림] {name} ({alert_type}) → {target}")

            # 오늘 알림 목록 저장 (중복 방지)
            sb.table('app_config').upsert({
                'key':         'watchlist_alerted_today',
                'value':       json.dumps({
                    'date':    today,
                    'alerted': [list(k) for k in alerted_today],
                }, ensure_ascii=False),
                'description': f'{today} 관심가/목표가 알림 발송 이력'
            }, on_conflict='key').execute()

        logging.info(
            f"📋 [관심가알림] 체크 완료 — {len(watchlist)}개 종목 중 {len(alerts)}개 발송"
        )

    except Exception as e:
        logging.error(f"❌ [관심가알림] 오류: {e}")


def job_kind_ir():
    """매일 2회 (09:05, 18:05) — KIND IR자료실 수집 → @batiarchive 전송"""
    if not _KIND_IR_OK:
        return
    if not _is_enabled("kind_ir"):
        logging.info("⏸ KIND IR 수집 비활성화 (DB 설정)")
        return
    try:
        logging.info("📋 [KIND IR] 수집 시작")
        _kind_ir.run_kind_ir_job()
    except Exception as e:
        logging.error(f"❌ [KIND IR] 오류: {e}")


def job_pro_channel_check():
    """매일 09:00 — 프로 채널 구독 만료 멤버 퇴장 + D-3 예고 알림"""
    if not _PRO_OK:
        return
    try:
        logging.info("🔐 [프로채널] 구독 만료 체크 시작")
        result = _pro.check_expired()
        logging.info(
            f"🔐 [프로채널] 완료 — "
            f"퇴장 {len(result['kicked'])}명 / "
            f"예고 {len(result['notified'])}명 / "
            f"오류 {len(result['errors'])}건"
        )
    except Exception as e:
        logging.error(f"❌ [프로채널] 만료 체크 오류: {e}")


def job_saturday_main_ranking():
    # ✅ [추가] DB에서 스케줄 ON/OFF 확인
    if not _is_enabled("saturday"):
        logging.info("⏸ 토요일 주간 랭킹 비활성화 (DB 설정)")
        return
    logging.info("🏆 [주간 랭킹] 메인방 발송 시작")
    try:
        msg = stock_api.get_weekly_universe_ranking()
        stock_api.send_telegram(DEFAULT_CHAT_ID, msg, keyboard=COMMON_BUTTON)
        _log_notice(DEFAULT_CHAT_ID, "[토요일] 주간 랭킹 발송")
    except Exception as e:
        logging.error(f"주간 메인 랭킹 에러: {e}")


def job_saturday_industry_report():
    if not _is_enabled("saturday"):
        return
    logging.info("🏭 [주간 산업 리포트] 각 산업방 발송 시작")
    _broadcast_to_industries(
        stock_api.get_industry_weekly_ranking,
        sleep=1.0, keyboard=COMMON_BUTTON, label="주간 산업 리포트"
    )


def job_sunday_industry_recap():
    # ✅ [추가] DB에서 스케줄 ON/OFF 확인
    if not _is_enabled("sunday"):
        logging.info("⏸ 일요일 산업 리포트 비활성화 (DB 설정)")
        return
    logging.info("🗓 일요일 산업별 시총 리포트 발송 시작")
    _broadcast_to_industries(
        stock_api.get_industry_cap_ranking,
        sleep=2.0, label="일요일 시총 리포트"
    )


def job_sunday_company_diagnosis():
    if not _is_enabled("sunday"):
        return
    logging.info("🗓 일요일 종목별 기술적 진단 발송 시작")
    _broadcast_to_companies(
        stock_api.get_stock_chart,
        sleep=2.0, label="일요일 기술적 진단"
    )


# ==========================================
# ⏰ 스케줄러 실행
# ==========================================
def run_scheduler():
    logging.info("🚀 [스케줄러] 시작 (schedule 라이브러리 적용)")
    schedule.clear()  # 재시작 시 중복 job 방지 — 없으면 이미 지난 시각 job이 즉시 실행됨

    schedule.every().day.at("09:00").do(job_pro_channel_check)  # 프로 채널 구독 만료 체크
    schedule.every().day.at("09:05").do(job_kind_ir)             # KIND IR자료 오전 수집
    schedule.every().day.at("08:50").do(job_naver_report)
    schedule.every().day.at("08:55").do(job_collect_analyst_opinions)  # 투자의견 (장전)
    schedule.every().day.at("06:30").do(job_collect_macro)             # 글로벌 매크로 수집 + 메인 채널 브리핑 (서머타임 05:00/겨울 06:00 마감 → 06:30 안전)
    schedule.every().day.at("06:35").do(job_collect_us_etf)            # US ETF 수집 (미국 장 마감 직후)
    schedule.every().day.at("09:30").do(job_collect_market)            # 장 시작 모니터링 수집
    schedule.every().day.at("09:35").do(job_collect_foreign_institution)  # 기관/외국인 수급 ①
    schedule.every().day.at("11:25").do(job_collect_foreign_institution)  # 기관/외국인 수급 ②
    schedule.every().day.at("11:30").do(job_lunch_briefing)
    schedule.every().day.at("12:00").do(job_collect_market)            # 점심 모니터링 수집
    schedule.every().day.at("13:25").do(job_collect_foreign_institution)  # 기관/외국인 수급 ③
    schedule.every().day.at("14:35").do(job_collect_foreign_institution)  # 기관/외국인 수급 ④
    schedule.every().day.at("15:35").do(job_collect_foreign_institution)  # 기관/외국인 수급 ⑤ (장 마감 최종)
    schedule.every().day.at("16:10").do(job_collect_macro)             # 장 마감 후 매크로 수집
    schedule.every().day.at("16:20").do(job_collect_us_etf)            # US ETF 수집 (미장 전일 종가)
    schedule.every().day.at("16:30").do(job_collect_new_high)          # 신고가 종목 수집
    schedule.every().day.at("17:00").do(job_collect_market_closing)    # 장 마감 확정치 수집 (외국인 집계 완료 후)
    schedule.every().day.at("17:15").do(job_sector_summary)            # 산업별 일별 요약 집계
    schedule.every().day.at("17:30").do(job_leading_stocks)            # 주도주 탐색기 스코어 계산
    schedule.every().day.at("18:00").do(job_naver_report)
    schedule.every().day.at("18:10").do(job_kind_ir)             # KIND IR자료 오후 수집
    schedule.every().day.at("18:30").do(job_daily_closing)
    # 봇 시작 시 초기 재무 데이터 수집 (최초 1회만)
    # job_initial_financials 제거 — 필요시 수동 실행
    logging.info("🚀 [초기수집] 백그라운드 스레드 시작")

    schedule.every().saturday.at("00:30").do(job_cleanup_market_data)   # 새벽 market_data 정리
    schedule.every().saturday.at("01:00").do(job_sync_listed_companies) # 새벽 상장사 동기화
    schedule.every().day.at("18:30").do(job_collect_financials)        # 장 마감 후 재무수집 (공시 기반)
    schedule.every().day.at("18:35").do(job_collect_analyst_opinions)  # 투자의견 (장후)
    schedule.every().saturday.at("10:00").do(job_saturday_main_ranking)
    schedule.every().saturday.at("10:30").do(job_saturday_industry_report)
    schedule.every().sunday.at("10:00").do(job_sunday_industry_recap)
    schedule.every().sunday.at("10:30").do(job_sunday_company_diagnosis)

    loop_count = 0
    while True:
        try:
            schedule.run_pending()
            time.sleep(1)

            # ✅ [추가] 60초마다 heartbeat 전송
            loop_count += 1
            if loop_count % 60 == 0 and _BRIDGE_OK:
                try:
                    _bridge.heartbeat("scheduler_bot")
                except Exception:
                    pass

        except Exception as e:
            logging.error(f"❌ 스케줄러 실행 중 에러: {e}")
            time.sleep(60)


# ══════════════════════════════════════════
# 🔧 워치독 타임스탬프 플래그 유틸
#   app_config 테이블에 Unix ms 타임스탬프를 저장하는 트리거 플래그
#   값이 '0' 이거나 숫자가 아니면 미발동으로 간주
# ══════════════════════════════════════════
def _read_ts_flag(sb_client, key):
    """플래그 경과 시간(초) 반환. 미발동(0·빈값·비숫자·row없음)이면 None."""
    try:
        res = sb_client.table('app_config').select('value').eq('key', key).maybe_single().execute()
    except Exception:
        # maybe_single 미지원 구버전 supabase-py fallback
        try:
            res = sb_client.table('app_config').select('value').eq('key', key).limit(1).execute()
            if not res.data:
                return None
            res = type('R', (), {'data': res.data[0]})()
        except Exception:
            return None
    if res.data is None:
        return None
    val = res.data.get('value', '0') if isinstance(res.data, dict) else '0'
    if not val or val == '0':
        return None
    val_str = str(val).strip()
    if not val_str.isdigit():
        return None
    return (time.time() * 1000 - float(val_str)) / 1000

def _clear_ts_flag(sb_client, key):
    """플래그를 '0'으로 초기화 (중복 실행 방지)."""
    sb_client.table('app_config').upsert({'key': key, 'value': '0'}, on_conflict='key').execute()


# ==========================================
# 🚑 스레드 심폐소생술
# ==========================================
def restart_thread(target_func, name):
    logging.warning(f"🔄 [{name}] 재시작을 시도합니다...")
    new_thread = threading.Thread(target=target_func, name=name, daemon=True)
    new_thread.start()
    logging.info(f"✅ [{name}] 재시작 성공!")
    return new_thread


def _start_daemon(target, name: str, args: tuple = ()):
    """daemon=True 스레드 생성 후 즉시 시작. threading.Thread 4줄 스폰 패턴 통일."""
    threading.Thread(target=target, name=name, args=args, daemon=True).start()


def _run_watchdog_flags(threads: dict):
    """워치독 루프 1회 tick — 각종 앱 플래그 체크 및 배경 작업 트리거.
    main() while True 루프에서 매 60초마다 호출됨."""
    global _disclosure_running

    # ── reload_flag — 대시보드 종목 변경 시 봇 데이터 재로드 ──
    if _BRIDGE_OK:
        try:
            if _bridge.check_reload_flag():
                _sb_r     = _bridge._get_client()
                _prev_mon = getattr(_bridge, '_prev_mon_codes', set())

                from config import reload_company_data
                reload_company_data()
                logging.info("🔄 [Reload] 봇 종목 데이터 갱신 완료")
                # 산업 매핑 캐시 갱신 (프론트엔드 app_config 조회용)
                try:
                    _bridge.update_industry_map_cache()
                except Exception as _ice:
                    logging.debug(f"industry_map 캐시 갱신 오류 (무시): {_ice}")

                _all = _sb_r.table('companies').select('code,name') \
                           .eq('is_monitored', True).execute()
                _all_codes = {r['code'].split('.')[0]
                              for r in (_all.data or []) if r.get('code')}

                _added   = _all_codes - _prev_mon
                _removed = _prev_mon - _all_codes
                _bridge._prev_mon_codes = _all_codes

                # 제거된 종목 → 90일 초과 데이터 삭제
                if _removed:
                    _cutoff90 = (datetime.date.today() - datetime.timedelta(days=90)).isoformat()
                    _rm_list  = list(_removed)
                    for _i in range(0, len(_rm_list), 200):
                        _sb_r.table('market_data').delete() \
                             .lt('base_date', _cutoff90) \
                             .in_('stock_code', _rm_list[_i:_i+200]).execute()
                    logging.info(f"🗑️ [Reload] 제거 종목 {len(_removed)}개 — 90일 초과 데이터 삭제")

                # 신규 추가 종목 → 90일 시장 데이터 백필 (backfill_market_90d.run_for_codes 위임)
                if _added and _COLLECTOR_OK:
                    logging.info(f"📈 [Reload] 신규 모니터링 {len(_added)}개 — 90일 시장 데이터 백필 시작")
                    def _backfill_market(_codes):
                        try:
                            from backfill_market_90d import run_for_codes
                            run_for_codes(list(_codes))
                        except Exception as _be:
                            logging.error(f"❌ [Reload] 백필 오류: {_be}")
                    _start_daemon(_backfill_market, "Thread-MarketBackfill", (list(_added),))

                # 신규 종목 기업정보 자동수집
                if _added and _COLLECTOR_OK:
                    try:
                        _existing   = _sb_r.table('company_info').select('stock_code') \
                                           .in_('stock_code', list(_added)).execute()
                        _done       = {r['stock_code'] for r in (_existing.data or [])}
                        _new_codes  = list(_added - _done)
                        if _new_codes:
                            logging.info(f"📋 [기업정보] 신규 모니터링 종목 {len(_new_codes)}개 감지 → 자동수집")
                            def _collect_new():
                                try:
                                    _dart2 = __import__('OpenDartReader')(os.environ.get('DART_API_KEY',''))
                                    _sb3   = _bridge._get_client()
                                    for _c in _new_codes:
                                        try:
                                            _collect_company_one(_dart2, _sb3, _c, force=False)
                                            time.sleep(0.5)
                                        except Exception as _e:
                                            logging.error(f"  [{_c}] 기업정보 수집 오류: {_e}")
                                    logging.info(f"📋 [기업정보] 자동수집 완료 ({len(_new_codes)}개)")
                                except Exception as _fe:
                                    logging.error(f"❌ [기업정보] 자동수집 스레드 오류: {_fe}")
                            _start_daemon(_collect_new, "Thread-CompanyInfo")
                    except Exception as _cie:
                        logging.debug(f"기업정보 신규감지 오류: {_cie}")

                # 신규 종목 재무 데이터 자동수집
                if _added and _COLLECTOR_OK:
                    try:
                        _corp_res = _sb_r.table('companies').select('code,corp_code,name') \
                                         .eq('is_monitored', True).execute()
                        _new_corp_codes = [
                            r['corp_code'] for r in (_corp_res.data or [])
                            if r.get('corp_code') and r['code'].split('.')[0] in _added
                        ]
                        if _new_corp_codes:
                            logging.info(f"💰 [재무] 신규 종목 {len(_new_corp_codes)}개 — 2023년~현재 재무 수집 시작")
                            def _collect_fin_new(_corp_codes):
                                try:
                                    from collect_financials import run_by_corp_codes_all_history
                                    run_by_corp_codes_all_history(_corp_codes, from_year=2023, max_workers=2)
                                    logging.info(f"💰 [재무] 신규 종목 재무 수집 완료 ({len(_corp_codes)}개)")
                                except Exception as _fe2:
                                    logging.error(f"❌ [재무] 신규 종목 재무 수집 오류: {_fe2}")
                            _start_daemon(_collect_fin_new, "Thread-FinancialsNew", (_new_corp_codes,))
                    except Exception as _fie:
                        logging.debug(f"재무 신규감지 오류: {_fie}")

                if not _prev_mon:
                    _bridge._prev_mon_codes = _all_codes

        except Exception as _re:
            logging.debug(f"reload_flag 체크 오류: {_re}")

    # ── collect_company_info_request — 대시보드 DART 자동수집 버튼 ──
    if _BRIDGE_OK and _COLLECTOR_OK:
        try:
            _sb  = _bridge._get_client()
            _req = _sb.table('app_config').select('value') \
                      .eq('key', 'collect_company_info_request').single().execute()
            if _req.data and _req.data.get('value'):
                _req_data = json.loads(_req.data['value'])
                _req_code = _req_data.get('code', '')
                _req_time = _req_data.get('requested_at', '')
                if _req_code and _req_time:
                    _elapsed = (datetime.datetime.now(tz=datetime.timezone.utc) -
                               datetime.datetime.fromisoformat(_req_time.replace('Z','+00:00'))).total_seconds()
                    if _elapsed < 300:
                        logging.info(f"📋 [기업정보] 수동 수집 요청 감지: {_req_code}")
                        _dart = __import__('OpenDartReader')(os.environ.get('DART_API_KEY',''))
                        _collect_company_one(_dart, _sb, _req_code, force=True)
                        _sb.table('app_config').upsert(
                            {'key': 'collect_company_info_request', 'value': '{}'}, on_conflict='key').execute()
        except Exception as _ce:
            logging.debug(f"기업정보 수집 요청 체크 오류: {_ce}")

    # ── run_market_all_flag — 전체 상장사 시장 데이터 수동 수집 ──
    if _BRIDGE_OK and _COLLECTOR_OK:
        try:
            _sb_mkt      = _bridge._get_client()
            _mkt_elapsed = _read_ts_flag(_sb_mkt, 'run_market_all_flag')
            if _mkt_elapsed is not None and 0 < _mkt_elapsed < 300:
                _clear_ts_flag(_sb_mkt, 'run_market_all_flag')
                if market_timer.is_kr_holiday():
                    logging.info("⏸ [시장수집-전체] 주말/공휴일 — 수동 트리거 무시")
                else:
                    logging.info("📡 [시장수집-전체] 수동 트리거 감지 → 전체 상장사 수집 시작")
                    def _run_market_all():
                        try:
                            ok, fail = run_market(all_listed=True)
                            logging.info(f"📡 [시장수집-전체] 완료: 성공 {ok}개, 실패 {fail}개")
                        except Exception as _me:
                            logging.error(f"❌ [시장수집-전체] 오류: {_me}")
                    _start_daemon(_run_market_all, "Thread-ManualMarketAll")
        except Exception as _mktfe:
            logging.debug(f"market_all_flag 체크 오류: {_mktfe}")

    # ── run_macro_flag — 매크로 즉시 수집 ──
    if _BRIDGE_OK and _COLLECTOR_OK:
        try:
            _sb_m      = _bridge._get_client()
            _m_elapsed = _read_ts_flag(_sb_m, 'run_macro_flag')
            if _m_elapsed is not None and _m_elapsed < 180:
                logging.info("📡 [매크로] 수동 수집 트리거 감지 → job_collect_macro 실행")
                _clear_ts_flag(_sb_m, 'run_macro_flag')
                _start_daemon(job_collect_macro, "Thread-ManualMacro")
        except Exception as _mfe:
            logging.debug(f"macro_flag 체크 오류: {_mfe}")

    # ── etf_collect_flag — US ETF 수집 트리거 ──
    if _BRIDGE_OK:
        try:
            _sb_etf      = _bridge._get_client()
            _etf_elapsed = _read_ts_flag(_sb_etf, 'etf_collect_flag')
            if _etf_elapsed is not None and 0 < _etf_elapsed < 300:
                logging.info("📡 [US ETF] 수집 트리거 감지 → collect_us_etf 실행")
                _clear_ts_flag(_sb_etf, 'etf_collect_flag')
                def _run_etf_collect():
                    try:
                        import collect_us_etf
                        collect_us_etf.collect_and_save(days=90)
                        logging.info("✅ [US ETF] 수집 완료")
                    except Exception as _ee:
                        logging.error(f"❌ [US ETF] 수집 오류: {_ee}")
                _start_daemon(_run_etf_collect, "Thread-EtfCollect")
        except Exception as _etfe:
            logging.debug(f"etf_collect_flag 체크 오류: {_etfe}")

    # ── run_flow_flag — 기관/외국인 수급 즉시 수집 ──
    if _BRIDGE_OK and _COLLECTOR_OK:
        try:
            _sb_fl      = _bridge._get_client()
            _fl_elapsed = _read_ts_flag(_sb_fl, 'run_flow_flag')
            if _fl_elapsed is not None and 0 < _fl_elapsed < 300:
                logging.info("📡 [수급수집] 수동 트리거 감지 → job_collect_foreign_institution 실행")
                _clear_ts_flag(_sb_fl, 'run_flow_flag')
                _start_daemon(job_collect_foreign_institution, "Thread-ManualFlow")
        except Exception as _fle:
            logging.debug(f"run_flow_flag 체크 오류: {_fle}")

    # ── run_disclosure_flag — 공시수집 즉시 실행 ──
    if _BRIDGE_OK:
        try:
            if _bridge.check_disclosure_flag() and not _disclosure_running:
                _disclosure_running = True
                logging.info("🔄 [공시수집] 수동 트리거 — job_collect_financials 즉시 실행")
                def _run_disclosure():
                    global _disclosure_running
                    try:
                        job_collect_financials()
                    finally:
                        _disclosure_running = False
                _start_daemon(_run_disclosure, "Thread-ManualDisclosure")
        except Exception as _de:
            logging.debug(f"disclosure_flag 체크 오류: {_de}")

    # ── run_leading_stocks_flag — 주도주 탐색기 수동 트리거 ──
    if _BRIDGE_OK:
        try:
            _sb_ls      = _bridge._get_client()
            _ls_elapsed = _read_ts_flag(_sb_ls, 'run_leading_stocks_flag')
            if _ls_elapsed is not None and 0 < _ls_elapsed < 300:
                _clear_ts_flag(_sb_ls, 'run_leading_stocks_flag')
                logging.info("📡 [주도주] 수동 트리거 감지 → leading_stocks_generator 실행")
                def _run_leading_stocks():
                    try:
                        from leading_stocks_generator import run as run_leading
                        run_leading()
                        logging.info("✅ [주도주] 수동 생성 완료")
                    except Exception as _le:
                        logging.error(f"❌ [주도주] 수동 생성 오류: {_le}")
                _start_daemon(_run_leading_stocks, "Thread-LeadingStocks")
        except Exception as _lse:
            logging.debug(f"leading_stocks_flag 체크 오류: {_lse}")

    # ── run_sector_summary_flag — 산업별 요약·신호 수동 트리거 ──
    if _BRIDGE_OK:
        try:
            _sb_ss      = _bridge._get_client()
            _ss_elapsed = _read_ts_flag(_sb_ss, 'run_sector_summary_flag')
            if _ss_elapsed is not None and 0 < _ss_elapsed < 300:
                _clear_ts_flag(_sb_ss, 'run_sector_summary_flag')
                logging.info("📡 [섹터요약] 수동 트리거 감지 → collect_sector_summary 실행")
                def _run_sector_summary():
                    try:
                        from collect_sector_summary import run as run_ss
                        run_ss()
                        logging.info("✅ [섹터요약] 수동 집계 완료")
                    except Exception as _sse:
                        logging.error(f"❌ [섹터요약] 수동 집계 오류: {_sse}")
                _start_daemon(_run_sector_summary, "Thread-SectorSummary")
        except Exception as _ssfe:
            logging.debug(f"sector_summary_flag 체크 오류: {_ssfe}")

    # ── pro_action_flag — 프로 채널 초대/퇴장/연장 ──
    if _PRO_OK:
        try:
            _pro.process_pro_action_flag()
        except Exception as _proe:
            logging.debug(f"pro_action_flag 체크 오류: {_proe}")

    # ── 전체 봇 heartbeat 업데이트 ──
    if _BRIDGE_OK:
        try:
            for bot_name, key in [
                ("price_bot", "Thread-Price"), ("news_bot",  "Thread-News"),
                ("dart_bot",  "Thread-Dart"),  ("scheduler_bot", "Thread-Sched"),
            ]:
                if threads[key]["thread"].is_alive():
                    _bridge.heartbeat(bot_name)
        except Exception:
            pass


def main():
    logging.info("📢 바티인베스트 통합 시스템 가동 (Schedule Mode)")

    t_scanner   = threading.Thread(target=run_scanner_bot, name="Thread-Price", daemon=True)
    t_news      = threading.Thread(target=run_news_bot,    name="Thread-News",  daemon=True)
    t_dart      = threading.Thread(target=run_dart_bot,    name="Thread-Dart",  daemon=True)
    t_scheduler = threading.Thread(target=run_scheduler,   name="Thread-Sched", daemon=True)

    # SMS 웹훅 서버 (입금 자동 처리)
    if _SMS_WH_OK:
        _sms_wh.start_thread()
        logging.info("🌐 [SMS] 웹훅 서버 기동 (포트 5001)")

    # 봇 명령어 수신은 realtime_alert.py의 telegram_listener에서 처리
    # (getUpdates 중복 호출 방지 — bot_commands._handle을 직접 호출하는 방식으로 통합)

    threads = {
        "Thread-Price": {"thread": t_scanner,   "target": run_scanner_bot},
        "Thread-News":  {"thread": t_news,       "target": run_news_bot},
        "Thread-Dart":  {"thread": t_dart,       "target": run_dart_bot},
        "Thread-Sched": {"thread": t_scheduler,  "target": run_scheduler},
    }

    for name, info in threads.items():
        info["thread"].start()
        time.sleep(2)

    while True:
        try:
            time.sleep(60)

            for name, info in threads.items():
                if not info["thread"].is_alive():
                    logging.error(f"💀 [{name}] 응답 없음! 심폐소생술 실시.")
                    info["thread"] = restart_thread(info["target"], name)

            # 기존 파일 heartbeat 유지
            with open("heartbeat.txt", "w") as f:
                f.write(str(time.time()))

            _run_watchdog_flags(threads)

        except KeyboardInterrupt:
            logging.info("🛑 시스템 종료 요청 받음. 종료합니다.")
            break
        except Exception as e:
            logging.error(f"❌ 메인 루프 에러: {e}")
            time.sleep(60)


if __name__ == "__main__":
    main()
